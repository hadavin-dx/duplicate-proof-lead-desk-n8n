"""The stamp after the AI step, the reaper rules for a row that sent nothing, file-handle shortages
at the capability reads, the open-file arithmetic, the exit code of a refused desk setting and the wait after a
"no file handle" give-back. Made-up data only; the test clock is explicit."""

from __future__ import annotations

import errno
import os
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from support import is_write, lead, lock_writes_after_answer, sql_exec

from demo_only.fakes import FakeCrm, KeyedOutbox, RecordedQualifier
from lead_desk import constants, db, gate, portal_api, server
from lead_desk.adapters import FakesUnreachable, HttpCrm
from lead_desk.gate import SEND_MAX_ATTEMPTS

PLENTY = 10_000  # a clock step larger than any grace


def new_lead(rig: Any, ref: str) -> str:
    reply = rig.desk.process("form", ref, lead(email=f"{ref}@example.test"))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def follow(rig: Any, lid: str, cols: str) -> tuple[Any, ...]:
    return rig.query(f"SELECT {cols} FROM followups WHERE lead_id=?", (lid,))[0]  # noqa: S608 (fixed column text)


def set_follow(rig: Any, lid: str, **cols: Any) -> None:
    names = ", ".join(f"{k}=?" for k in cols)
    sql_exec(rig.db_path, f"UPDATE followups SET {names} WHERE lead_id=?", (*cols.values(), lid))  # noqa: S608


def no_handle() -> urllib.error.URLError:
    return urllib.error.URLError(OSError(errno.EMFILE, "x"))


# =====
# the row is stamped again when the model has answered, before its label is written
# =====
def test_model_answer_is_stamped_again(env, monkeypatch) -> None:
    """The model answers late but inside the grace, then the label write is slow too. Counted from the claim the two
    add up past the grace and the reaper would take the row (asking the model a second time); counted from the stamp
    after the answer they do not."""
    rig = env.rig()
    grace = rig.desk.stuck_grace_seconds
    model_seconds, write_seconds = grace - 6, 8  # each inside the grace, together past it

    class Late(RecordedQualifier):
        def qualify(self, lead_id: str, email_key: str) -> Any:
            rig.clock.advance(model_seconds)
            return super().qualify(lead_id, email_key)

    late = Late(rig.replies_path)
    rig.desk.qualifier = late
    real = rig.desk._locked_retry
    slow = {"n": 0}

    def slow_store(fn: Any) -> Any:
        if is_write(fn, "store"):  # the label write waits for the busy time, and the reaper passes meanwhile
            slow["n"] += 1
            rig.clock.advance(write_seconds)
            rig.desk.reap_once()
        return real(fn)

    monkeypatch.setattr(rig.desk, "_locked_retry", slow_store)
    lid = new_lead(rig, "req-1")
    rig.desk.run_followup(lid)
    assert model_seconds + write_seconds > grace
    assert slow["n"] == 1 and late.calls == 1
    assert rig.state(lid)[0] == "sent" and rig.crm.count == 1 and rig.outbox.count == 1


def test_claim_lost_in_model_stores_none(env) -> None:
    """The row is taken over while the model works: after the answer nothing is stored and nothing else is asked."""
    rig = env.rig()
    lid = new_lead(rig, "req-2")

    class TakenOver(RecordedQualifier):
        def qualify(self, lead_id: str, email_key: str) -> Any:
            set_follow(rig, lid, attempts=99)  # another drive claimed the row meanwhile
            return super().qualify(lead_id, email_key)

    rig.desk.qualifier = TakenOver(rig.replies_path)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "ai_label, state, crm_done, send_started") == (None, "sending", 0, 0)
    assert rig.crm.count == 0 and rig.outbox.count == 0


@pytest.mark.parametrize("where", ["_touch", "store"])
def test_model_stamp_lock_spends_the_try(env, monkeypatch, where: str) -> None:
    """A lock on the write after the model answered. The model WAS asked, so the try stays used up
    (`internal:ModelAnswerLost`) and the 5-try cap bounds how often the model is asked again. The real
    `_locked_retry` runs (only the write itself is refused) and both writes after the answer are covered: the
    stamp (`_touch`) and the label write (`store`)."""
    rig = env.rig()
    lid = new_lead(rig, "req-3")

    class Answers(RecordedQualifier):
        answered = False

        def qualify(self, lead_id: str, email_key: str) -> Any:
            label = super().qualify(lead_id, email_key)
            self.answered = True  # from now on a write is one after the AI step
            return label

    qualifier = Answers(rig.replies_path)
    rig.desk.qualifier = qualifier
    lock_writes_after_answer(rig, qualifier, monkeypatch, where)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, ai_label, crm_done, last_error") == (
        "pending",
        1,
        None,
        0,
        "internal:ModelAnswerLost",
    )
    assert rig.crm.count == 0 and rig.outbox.count == 0
    assert qualifier.calls == 1


# =====
# what the reaper does with a row that never asked or sent anything
# =====
def test_unsent_row_at_cap_fails_visibly(env) -> None:
    """A `sending` row that sent nothing and has used all its tries goes to `failed` (not back to pending for ever, so
    a hung model is not asked without end); `last_error` says why and a person's Retry starts it again."""
    rig = env.rig()
    lid = new_lead(rig, "req-4")
    set_follow(rig, lid, state="sending", attempts=SEND_MAX_ATTEMPTS, auto_attempts=SEND_MAX_ATTEMPTS)
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert follow(rig, lid, "state, last_error, ai_label, crm_done, send_started") == (
        "failed",
        "crash_while_sending",
        None,
        0,
        0,
    )
    assert rig.qualifier.calls == 0 and rig.crm.count == 0 and rig.outbox.count == 0
    assert rig.desk.retry(f"F{lid}").status == 200  # a person can start it again
    assert rig.state(lid)[0] == "sent"


def test_unsent_row_below_cap_is_pending(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-5")
    set_follow(rig, lid, state="sending", attempts=3, auto_attempts=SEND_MAX_ATTEMPTS - 1)
    rig.clock.advance(PLENTY)
    assert rig.desk.reap_once()["requeued"] == 1
    assert follow(rig, lid, "state, auto_attempts, last_error") == (
        "pending",
        SEND_MAX_ATTEMPTS - 1,
        "crash_while_sending",
    )
    assert rig.qualifier.calls == 0 and rig.crm.count == 0 and rig.outbox.count == 0


# =====
# no file handle at a capability read gives the try back
# =====
class NoHandleOutbox(KeyedOutbox):
    """Keyed sender whose capability read finds no file handle."""

    def refresh(self) -> None:
        raise FakesUnreachable("URLError") from no_handle()


def test_ambiguous_no_handle_gives_back(env) -> None:
    rig = env.rig(outbox=NoHandleOutbox())
    lid = new_lead(rig, "req-6")
    set_follow(rig, lid, ambiguous_prior=1, crm_done=1, ai_label="warm")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, last_error, send_started") == ("pending", 0, "no_descriptors", 0)
    assert rig.outbox.count == 0


class UnreadableOutbox(KeyedOutbox):
    """Keyed sender whose capability read fails for another reason (the sender is down)."""

    def refresh(self) -> None:
        raise FakesUnreachable("URLError")


def test_ambiguous_read_down_still_counts(env) -> None:
    rig = env.rig(outbox=UnreadableOutbox())
    lid = new_lead(rig, "req-7")
    set_follow(rig, lid, ambiguous_prior=1, crm_done=1, ai_label="warm")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 1, "unreachable")


class RereadCrm(FakeCrm):
    """Says it is keyed; its mode read fails the way the test sets."""

    capability = "keyed"

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def refresh(self) -> None:
        raise self.error

    def forget_capability(self) -> None:
        self.capability = None  # type: ignore[assignment]  # as HttpCrm does


def test_crm_read_no_handle_gives_back(env) -> None:
    """A leftover crm_done=2 and no file handle to read the CRM's mode: nothing can be judged, so the try comes back
    (not `unknown`, which would send a person to look for nothing)."""
    crm = RereadCrm(FakesUnreachable("URLError"))
    crm.error.__cause__ = no_handle()
    rig = env.rig()
    rig.desk.crm = crm
    lid = new_lead(rig, "req-8")
    set_follow(rig, lid, crm_done=2, ai_label="warm")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, crm_done, last_error") == ("pending", 0, 2, "no_descriptors")
    assert crm.count == 0 and rig.outbox.count == 0


def test_crm_read_down_spends_a_try(env) -> None:
    """A leftover crm_done=2 and a CRM mode read that fails for another reason (the CRM is down): the row is not
    `unknown`; nothing was asked, so the try is used up with a backoff and the mark stays 2."""
    crm = RereadCrm(FakesUnreachable("URLError"))
    rig = env.rig()
    rig.desk.crm = crm
    lid = new_lead(rig, "req-9")
    set_follow(rig, lid, crm_done=2, ai_label="warm")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, crm_done, last_error") == (
        "pending",
        1,
        2,
        "internal:CrmUnreachable",
    )
    assert crm.count == 0 and rig.outbox.count == 0


# =====
# the open-file arithmetic
# =====
@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts open files through /proc")
def test_reserve_covers_its_items(tmp_path: Path) -> None:
    """The portal item of the reserve is measured, not only added up: PORTAL_MAX_INFLIGHT read-only connections that
    each ran a query hold FD_PORTAL_ITEM files together. The other items are counted by reading the code (the
    listening socket, the lock file, the streams and the reaper's connection: not measured here)."""
    assert constants.FDS_PER_HANDLER_THREAD >= constants.FDS_PER_THREAD_MINIMUM
    assert sum(n for _, n in constants.FD_RESERVE_ITEMS) <= constants.FD_RESERVE
    path = tmp_path / "portal.sqlite3"
    writer = db.connect(path)
    writer.execute("CREATE TABLE t (a)")
    writer.execute("INSERT INTO t VALUES (1)")
    before = len(os.listdir("/proc/self/fd"))
    readers = [portal_api.connect_readonly(path) for _ in range(constants.PORTAL_MAX_INFLIGHT)]
    for reader in readers:
        reader.execute("SELECT a FROM t").fetchall()
    held = len(os.listdir("/proc/self/fd")) - before
    for reader in readers:
        reader.close()
    writer.close()
    assert held <= dict(constants.FD_RESERVE_ITEMS)[constants.FD_PORTAL_ITEM]
    assert held >= constants.PORTAL_MAX_INFLIGHT  # at least the database file of each (the -shm file is shared)


def test_descriptor_arithmetic_is_validated() -> None:
    items = constants.FD_RESERVE_ITEMS
    portal = constants.PORTAL_MAX_INFLIGHT
    constants.validate_descriptor_budget(4, constants.FD_RESERVE, items, portal)
    with pytest.raises(ValueError, match="FDS_PER_HANDLER_THREAD"):
        constants.validate_descriptor_budget(3, constants.FD_RESERVE, items, portal)
    with pytest.raises(ValueError, match="FD_RESERVE is"):
        constants.validate_descriptor_budget(4, 10, items, portal)
    with pytest.raises(ValueError, match="PORTAL_MAX_INFLIGHT"):
        constants.validate_descriptor_budget(4, constants.FD_RESERVE, items, portal + 1)
    with pytest.raises(ValueError, match="whole number"):
        constants.validate_descriptor_budget(4, 1.5, items, portal)


def test_start_up_checks_the_arithmetic(monkeypatch) -> None:
    monkeypatch.setattr(server, "FD_RESERVE", 1)
    with pytest.raises(ValueError, match="FD_RESERVE is"):
        server.ensure_descriptor_budget(8, object())


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts open files through /proc")
def test_connection_holds_counted_files(tmp_path: Path) -> None:
    """A database connection in WAL mode holds the database file and the -wal file (2); the -shm file is shared and
    counted once in the reserve. A thread's budget is that plus its connection socket and one adapter socket."""
    path = tmp_path / "fd.sqlite3"
    first = db.connect(path)
    first.execute("CREATE TABLE t (a)")
    first.execute("INSERT INTO t VALUES (1)")
    before = len(os.listdir("/proc/self/fd"))
    second = db.connect(path)
    second.execute("INSERT INTO t VALUES (2)")
    held = len(os.listdir("/proc/self/fd")) - before
    second.close()
    first.close()
    assert held == 2
    assert constants.FDS_PER_HANDLER_THREAD == held + 1 + 1


# =====
# a refused desk setting has its own exit code
# =====
class LongQualifier(RecordedQualifier):
    timeout_seconds = float(PLENTY)


def test_main_exits_4_when_the_desk_refuses(env, monkeypatch, tmp_path, capsys) -> None:
    rig = env.rig()
    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "refused.sqlite3"))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setattr(
        server, "load_demo_adapters", lambda cfg: (FakeCrm(), KeyedOutbox(), LongQualifier(rig.replies_path))
    )
    with pytest.raises(SystemExit) as info:
        server.main()
    assert info.value.code == constants.EXIT_DESK_SETTING_REFUSED == 4
    assert constants.EXIT_DESK_SETTING_REFUSED != constants.EXIT_ADAPTERS_UNREACHABLE
    assert "timeout_seconds" in capsys.readouterr().out


# =====
# a "no file handle" give-back waits before the next drive
# =====
def test_wait_before_drive_rules() -> None:
    assert gate.wait_before_drive(0, None) == gate.PENDING_GRACE_SECONDS
    assert gate.wait_before_drive(2, None) == gate.backoff_after(2)
    assert gate.wait_before_drive(0, gate.NO_DESCRIPTORS_ERROR) == gate.NO_DESCRIPTORS_BACKOFF_SECONDS
    assert gate.wait_before_drive(3, gate.NO_DESCRIPTORS_ERROR) == gate.NO_DESCRIPTORS_BACKOFF_SECONDS
    assert gate.NO_DESCRIPTORS_BACKOFF_SECONDS > gate.PENDING_GRACE_SECONDS


def test_no_handle_give_back_waits(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-10")
    set_follow(rig, lid, last_error="no_descriptors", auto_attempts=0, updated_at=rig.clock())
    rig.clock.advance(gate.PENDING_GRACE_SECONDS)
    assert rig.desk.reap_once()["driven"] == 0  # not driven again at once: the shortage may last
    assert rig.state(lid)[0] == "pending"
    rig.clock.advance(gate.NO_DESCRIPTORS_BACKOFF_SECONDS)
    assert rig.desk.reap_once()["driven"] == 1
    assert rig.state(lid)[0] == "sent"


# =====
# the shipped fake answers the CRM's mode, and HttpCrm reads it
# =====
@pytest.mark.parametrize("mode", ["keyed", "blind"])
def test_shipped_fake_answers_crm_mode(env, mode: str) -> None:
    _, url = env.fakes("keyed", crm_mode=mode)
    crm = HttpCrm(url, 2.0)
    assert crm.capability is None
    crm.refresh()
    assert crm.capability == mode
