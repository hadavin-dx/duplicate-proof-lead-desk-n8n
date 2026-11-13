"""Stage stamps and the model step: a lock on a stage stamp gives the try back, the call order and the
failure path of a Retry, and the limit of the model step. Made-up data only; the test clock is explicit."""

from __future__ import annotations

import errno
import logging
import socket
import sqlite3
import time
import urllib.error
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest
from support import HOST, is_write, lead, sql_exec, start_serving, stop_serving

from demo_only.fakes import BlindOutbox, RecordedQualifier
from lead_desk import adapters, constants, db, gate, server
from lead_desk.adapters import CrmNothingStored, HttpCrm
from lead_desk.config import Config
from lead_desk.contract import SendNoDescriptors
from lead_desk.desk import Desk
from lead_desk.gate import SEND_MAX_ATTEMPTS

LOCKED = "database is locked"


def new_lead(rig: Any, ref: str) -> str:
    reply = rig.desk.process("form", ref, lead(email=f"{ref}@example.test"))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def follow(rig: Any, lid: str, cols: str) -> tuple[Any, ...]:
    return rig.query(f"SELECT {cols} FROM followups WHERE lead_id=?", (lid,))[0]  # noqa: S608 (fixed column text)


def set_follow(rig: Any, lid: str, **cols: Any) -> None:
    names = ", ".join(f"{k}=?" for k in cols)
    sql_exec(rig.db_path, f"UPDATE followups SET {names} WHERE lead_id=?", (*cols.values(), lid))  # noqa: S608


# =====
# a lock on the first stage stamp gives the try back
# =====
def test_first_stamp_lock_gives_try_back(env, monkeypatch) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-1")

    def locked(lead_id: str, token: int, send_started: bool = False) -> bool:
        raise sqlite3.OperationalError(LOCKED)

    monkeypatch.setattr(rig.desk, "_touch", locked)
    for _ in range(SEND_MAX_ATTEMPTS):  # as many locks as there are automatic tries
        rig.desk.run_followup(lid)
        assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 0, None)
    assert rig.qualifier.calls == 0 and rig.crm.count == 0 and rig.outbox.count == 0


def test_real_touch_lock_gives_try_back(env, monkeypatch) -> None:
    """The lock comes from the real `_touch` write (through `_locked_retry`), five drives in a row."""
    rig = env.rig()
    lid = new_lead(rig, "req-2")
    real = rig.desk._locked_retry
    fired = {"n": 0}

    def retry(fn: Any) -> Any:
        if is_write(fn, "_touch"):
            fired["n"] += 1
            raise sqlite3.OperationalError(LOCKED)
        return real(fn)

    monkeypatch.setattr(rig.desk, "_locked_retry", retry)
    for _ in range(SEND_MAX_ATTEMPTS):
        rig.desk.run_followup(lid)
        assert follow(rig, lid, "state, auto_attempts") == ("pending", 0)
    assert fired["n"] == SEND_MAX_ATTEMPTS  # the fault really fired on every drive
    assert rig.qualifier.calls == 0 and rig.crm.count == 0 and rig.outbox.count == 0


def test_stamp_not_ours_stays_silent(env, monkeypatch) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-3")
    assert rig.desk._claim(None, lid, ("pending",))
    real_token = rig.desk._take_token(lid)
    rig.desk._local.token = (lid, real_token + 7)  # a drive whose claim was taken over
    before = follow(rig, lid, "state, auto_attempts, attempts, last_error")
    rig.desk._after_claim(lid, auto=True)
    assert follow(rig, lid, "state, auto_attempts, attempts, last_error") == before
    assert rig.qualifier.calls == 0 and rig.crm.count == 0 and rig.outbox.count == 0


def test_stamp_before_send_lock_gives_back(env, monkeypatch) -> None:
    """Sibling: a real lock on the third stamp (first stamp, stamp after the AI step, then the one that also writes
    the send mark). The CRM row is made, the email is not, and the try comes back."""
    rig = env.rig()
    lid = new_lead(rig, "req-4")
    real = rig.desk._locked_retry
    stamps = {"n": 0}

    def retry(fn: Any) -> Any:
        if is_write(fn, "_touch"):
            stamps["n"] += 1
            if stamps["n"] == 3:  # the stamp that also writes the send mark
                raise sqlite3.OperationalError(LOCKED)
        return real(fn)

    monkeypatch.setattr(rig.desk, "_locked_retry", retry)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, send_started, crm_done") == ("pending", 0, 0, 1)
    assert rig.outbox.count == 0
    monkeypatch.setattr(rig.desk, "_locked_retry", real)
    rig.desk.run_followup(lid)
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1 and rig.crm.count == 1


def test_touch_lock_raises_not_false(env, monkeypatch) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-5")
    set_follow(rig, lid, state="sending", attempts=1)

    def busy(fn: Any) -> Any:
        raise sqlite3.OperationalError(LOCKED)

    monkeypatch.setattr(rig.desk, "_locked_retry", busy)
    with pytest.raises(sqlite3.OperationalError) as info:
        rig.desk._touch(lid, 1)
    assert db.is_busy(info.value)


def test_attempts_counts_claims_not_tries(env, monkeypatch) -> None:
    """A give-back returns the automatic try, never the claim number: it is the ownership token, so a drive that was
    put back can never match a later claim."""
    rig = env.rig()
    lid = new_lead(rig, "req-6")
    real = rig.desk._touch
    monkeypatch.setattr(rig.desk, "_touch", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError(LOCKED)))
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "attempts, auto_attempts") == (1, 0)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "attempts, auto_attempts") == (2, 0)
    monkeypatch.setattr(rig.desk, "_touch", real)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, attempts, auto_attempts") == ("sent", 3, 1)


# =====
# the give-back retries, and an empty file table on the CRM put is not "unknown"
# =====
class NoHandleOutbox(BlindOutbox):
    def __init__(self) -> None:
        super().__init__()
        self.armed = False

    def send(self, *a: Any, **k: Any) -> Any:
        self.armed = True
        raise SendNoDescriptors("EMFILE")


def test_give_back_retries_after_a_lock(env, monkeypatch) -> None:
    rig = env.rig(outbox=NoHandleOutbox())
    lid = new_lead(rig, "req-7")
    real_write = rig.desk._write
    state = {"locked_once": False}

    @contextmanager
    def write(conn: sqlite3.Connection) -> Any:
        if rig.outbox.armed and not state["locked_once"]:  # the first write after the failed send is the give-back
            state["locked_once"] = True
            raise sqlite3.OperationalError(LOCKED)
        with real_write(conn):
            yield

    monkeypatch.setattr(rig.desk, "_write", write)
    rig.desk.run_followup(lid)
    assert state["locked_once"]
    assert follow(rig, lid, "state, auto_attempts, send_started, last_error") == ("pending", 0, 0, "no_descriptors")


def test_crm_no_handle_is_nothing_stored(env, monkeypatch) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise urllib.error.URLError(OSError(errno.EMFILE, "x"))

    monkeypatch.setattr(adapters, "_exchange", refuse)
    with pytest.raises(CrmNothingStored):
        HttpCrm(f"http://{HOST}:1", 1.0).put("L000000000001", {"a": "b"})


def test_crm_no_handle_gives_try_back(env, monkeypatch) -> None:
    rig = env.rig()
    rig.desk.crm = HttpCrm(f"http://{HOST}:1", 1.0)  # no capability read: counts as not keyed

    def refuse(*a: Any, **k: Any) -> Any:
        raise urllib.error.URLError(OSError(errno.EMFILE, "x"))

    monkeypatch.setattr(adapters, "_exchange", refuse)
    lid = new_lead(rig, "req-8")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, crm_done, last_error") == ("pending", 0, 0, "no_descriptors")


# =====
# the failure path after a Retry claim and after a first claim, and the writer that gives an unsent row back
# =====
class BrokenQualifier:
    mode = "recorded"
    calls = 0

    def qualify(self, lead_id: str, email_key: str) -> Any:
        raise ValueError("the AI step is down")


def test_retry_step_error_is_pending(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-9")
    rig.desk.qualifier = BrokenQualifier()  # type: ignore[assignment]
    set_follow(rig, lid, state="failed", attempts=5, auto_attempts=SEND_MAX_ATTEMPTS, last_error="refused")
    reply = rig.desk.retry(f"F{lid}")
    assert (reply.status, reply.body["state"]) == (200, "pending")
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 1, "internal:ValueError")
    assert rig.crm.count == 0 and rig.outbox.count == 0


def test_first_drive_step_error_is_pending(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-10")
    rig.desk.qualifier = BrokenQualifier()  # type: ignore[assignment]
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 1, "internal:ValueError")


def test_abort_unsent_only_for_owner(env) -> None:
    """A drive that lost its claim writes nothing. The current claim holder gets `pending` with the error and its
    count unchanged (the claim already counted the try); at the cap it is `failed`."""
    rig = env.rig()
    lid = new_lead(rig, "req-11")
    set_follow(rig, lid, state="sending", attempts=2, auto_attempts=1)
    rig.desk._abort_unsent(lid, 1, ValueError("x"))  # a stale drive
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("sending", 1, None)
    rig.desk._abort_unsent(lid, 2, ValueError("x"))
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 1, "internal:ValueError")
    set_follow(rig, lid, state="sending", attempts=3, auto_attempts=SEND_MAX_ATTEMPTS, last_error=None)
    rig.desk._abort_unsent(lid, 3, ValueError("x"))
    assert follow(rig, lid, "state, auto_attempts") == ("failed", SEND_MAX_ATTEMPTS)


def test_abort_unsent_lock_gives_back_once(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-12")
    set_follow(rig, lid, state="sending", attempts=2, auto_attempts=2)
    rig.desk._abort_unsent(lid, 1, sqlite3.OperationalError(LOCKED))  # not this claim's token: nothing returned
    assert follow(rig, lid, "state, auto_attempts") == ("sending", 2)
    rig.desk._abort_unsent(lid, 2, sqlite3.OperationalError(LOCKED))
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 1, None)


# =====
# the model step is asked once only if it answers within the stuck grace
# =====
class SlowQualifier(RecordedQualifier):
    """The first call outlasts the grace while the reaper runs (on the desk clock); later calls answer at once."""

    def __init__(self, path: Any, rig_holder: dict[str, Any]) -> None:
        super().__init__(path)
        self.holder = rig_holder

    def qualify(self, lead_id: str, email_key: str) -> Any:
        label = super().qualify(lead_id, email_key)
        if self.calls == 1:
            rig = self.holder["rig"]
            rig.clock.advance(rig.desk.stuck_grace_seconds + 5)  # 30 s with the defaults
            rig.desk.reap_once()  # the row is put back: the first call has been overtaken
            rig.clock.advance(5)
            rig.desk.reap_once()  # and the next drive asks again
        return label


def test_slow_model_is_asked_twice(env, caplog) -> None:
    """Pins the limit: a model call that outlasts the stuck grace is asked a SECOND time. The CRM row and the email
    stay once, because every later write checks the claim's token."""
    holder: dict[str, Any] = {}
    rig = env.rig()
    slow = SlowQualifier(rig.replies_path, holder)
    rig.desk.qualifier = slow
    holder["rig"] = rig
    lid = new_lead(rig, "req-13")
    with caplog.at_level(logging.WARNING, logger="lead_desk"):
        rig.desk.run_followup(lid)
    assert slow.calls == 2
    assert rig.crm.count == 1 and rig.outbox.count == 1
    assert rig.state(lid)[0] == "sent"
    assert [r for r in caplog.records if "longer than its budget" in r.getMessage()]


def test_model_within_grace_asked_once(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-14")

    class Quick(RecordedQualifier):
        def qualify(self, lead_id: str, email_key: str) -> Any:
            rig.clock.advance(int(constants.MODEL_CALL_BUDGET_SECONDS))
            rig.desk.reap_once()  # a pass while the call runs: the row is not stuck yet
            return super().qualify(lead_id, email_key)

    quick = Quick(rig.replies_path)
    rig.desk.qualifier = quick
    rig.desk.run_followup(lid)
    assert quick.calls == 1 and rig.state(lid)[0] == "sent"


def test_declared_model_timeout_is_checked(env) -> None:
    """The accepted limit is the model budget itself (the desk cuts every call there), not grace minus margin,
    and a longer grace does not raise it."""
    rig = env.rig()
    budget = constants.MODEL_CALL_BUDGET_SECONDS

    class Declares(RecordedQualifier):
        def __init__(self, path: Any, timeout: float) -> None:
            super().__init__(path)
            self.timeout_seconds = timeout

    Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path, budget - 1)).close()  # below the budget: fine
    Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path, budget)).close()  # equal: accepted
    with pytest.raises(ValueError, match="timeout_seconds"):  # above the budget: refused
        Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path, budget + 0.5))
    longer = replace(rig.config, send_timeout_seconds=rig.config.send_timeout_seconds + 10)
    with pytest.raises(ValueError, match="timeout_seconds"):  # a longer grace does not allow more
        Desk(longer, rig.crm, rig.outbox, Declares(rig.replies_path, budget + 1))


def test_model_budget_is_validated() -> None:
    # arguments: budget, margin, smallest grace, write deadline, busy wait
    constants.validate_model_budget(9, 15, 25, 10, 5)
    bad_values = (
        (10, 15, 25, 10, 5),  # budget plus margin equal to the grace
        (11, 15, 25, 10, 5),  # budget plus margin over the grace
        (0, 15, 25, 10, 5),
        (float("nan"), 15, 25, 10, 5),
        (True, 15, 25, 10, 5),
        (10, -1, 25, 10, 5),
        ("10", 15, 25, 10, 5),
        (10, 15, 25, 0, 5),
    )
    for bad in bad_values:
        with pytest.raises(ValueError, match="MODEL|stuck grace|write deadline"):
            constants.validate_model_budget(*bad)
    with pytest.raises(ValueError, match="cover the write"):  # margin below the stamp's write window plus a busy wait
        constants.validate_model_budget(5, 14, 25, 10, 5)
    assert 0 < constants.MODEL_CALL_BUDGET_SECONDS


def test_default_config_fits_the_budget() -> None:
    config = Config()
    grace = int(config.send_timeout_seconds + gate.SENDING_GRACE_BUSY_WAITS * constants.BUSY_WAIT_SECONDS)
    assert constants.MODEL_CALL_BUDGET_SECONDS + constants.MODEL_GRACE_MARGIN_SECONDS < grace


# =====
# the accept loop does not spin on "no file handle left"
# =====
def test_accept_pauses_on_no_file_handle(env, monkeypatch) -> None:
    rig = env.rig()
    srv = server.make_server(rig.desk, replace(rig.desk.config, port=0))
    calls = {"n": 0}

    def refuse(self: Any) -> Any:
        calls["n"] += 1
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr("socketserver.TCPServer.get_request", refuse)
    host, port = srv.server_address[0], srv.server_address[1]
    thread = start_serving(srv)
    try:
        client = socket.create_connection((str(host), int(port)), timeout=5)  # stays in the listen queue, unaccepted
        time.sleep(0.5)
        client.close()
    finally:
        stop_serving(srv, thread)
    assert 1 <= calls["n"] <= 25, calls  # a spinning loop makes thousands of calls in half a second


def _accept_error(monkeypatch, code: int) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(server.time, "sleep", slept.append)

    def refuse(self: Any) -> Any:
        raise OSError(code, "refused")

    monkeypatch.setattr("socketserver.TCPServer.get_request", refuse)
    return slept


def test_accept_other_errors_do_not_pause(env, monkeypatch) -> None:
    srv = server.make_server(env.rig().desk, replace(Config(), port=0))
    slept = _accept_error(monkeypatch, errno.ECONNABORTED)
    try:
        with pytest.raises(OSError):
            srv.get_request()
    finally:
        srv.server_close()
    assert slept == []


def test_accept_pause_is_the_constant(env, monkeypatch) -> None:
    srv = server.make_server(env.rig().desk, replace(Config(), port=0))
    slept = _accept_error(monkeypatch, errno.ENFILE)
    try:
        with pytest.raises(OSError):
            srv.get_request()
    finally:
        srv.server_close()
    assert slept == [constants.ACCEPT_PAUSE_SECONDS]


# =====
# the open-file message
# =====
class _Res:
    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def getrlimit(self, which: int) -> tuple[int, int]:  # replaced per test
        raise AssertionError

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        raise ValueError("not allowed")


def test_unlimited_hard_limit_number(monkeypatch) -> None:
    res = _Res()
    monkeypatch.setattr(res, "getrlimit", lambda which: (256, -1))
    with pytest.raises(server.DescriptorBudgetError) as info:
        server.ensure_descriptor_budget(1000, res)
    text = str(info.value)
    need = 1000 * constants.FDS_PER_HANDLER_THREAD + constants.FD_RESERVE  # derived, not typed
    assert f"to at least {need}" in text and "limit on this computer is 256" in text


def test_a_tiny_limit_names_the_open_file_limit_only(monkeypatch) -> None:
    res = _Res()
    monkeypatch.setattr(res, "getrlimit", lambda which: (40, -1))
    with pytest.raises(server.DescriptorBudgetError) as info:
        server.ensure_descriptor_budget(1000, res)
    text = str(info.value)
    assert (
        "Lower MAX_HANDLER_THREADS" not in text
        and "to 0" not in text
        and "ulimit -n" in text
        and "constants.py" not in text
    )
