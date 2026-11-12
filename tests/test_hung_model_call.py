"""A model call that never returns (the desk bounds it itself), the stamp margin with the extra busy wait, a lock
after the model answered, and the CRM-down path with a leftover intent mark. Made-up data only;
the desk clock is explicit, the model bound is real time (shortened here)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from support import lead, lock_writes_after_answer, sql_exec

from demo_only.fakes import BlindOutbox, FakeCrm, KeyedOutbox, RecordedQualifier
from lead_desk import constants, gate, portal_api
from lead_desk import desk as desk_module
from lead_desk.adapters import FakesUnreachable
from lead_desk.desk import Desk, DeskSettingRefused
from lead_desk.gate import SEND_MAX_ATTEMPTS

PLENTY = 10_000  # a clock step larger than any grace or backoff
SHORT = 0.2  # the real-time model bound used in these tests, in seconds


def new_lead(rig: Any, ref: str) -> str:
    reply = rig.desk.process("form", ref, lead(email=f"{ref}@example.test"))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def follow(rig: Any, lid: str, cols: str) -> tuple[Any, ...]:
    return rig.query(f"SELECT {cols} FROM followups WHERE lead_id=?", (lid,))[0]  # noqa: S608 (fixed column text)


def set_follow(rig: Any, lid: str, **cols: Any) -> None:
    names = ", ".join(f"{k}=?" for k in cols)
    sql_exec(rig.db_path, f"UPDATE followups SET {names} WHERE lead_id=?", (*cols.values(), lid))  # noqa: S608


def wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    """A bounded poll (never an open loop)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class Hangs(RecordedQualifier):
    """Never answers for the lead ids in `hang_ids` until the test sets `release` (the wait is capped at 8 s so a
    failing test cannot leave a thread behind for long)."""

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self.release = threading.Event()
        self.hang_ids: set[str] = set()
        self.hang_all = False

    def qualify(self, lead_id: str, email_key: str) -> Any:
        label = super().qualify(lead_id, email_key)
        if self.hang_all or lead_id in self.hang_ids:
            self.release.wait(8)
        return label


@pytest.fixture
def hung(env, monkeypatch):
    """A rig whose qualifier can hang, with a short real-time model bound; the hung calls are released at the end."""
    rig = env.rig()
    qualifier = Hangs(rig.replies_path)
    rig.desk.qualifier = qualifier
    monkeypatch.setattr(desk_module, "MODEL_CALL_BUDGET_SECONDS", SHORT)
    yield rig, qualifier
    qualifier.release.set()
    wait_for(lambda: rig.desk.hung_workers()["model"] == 0, 5)


# =====
# a model call that never returns cannot freeze the reaper
# =====
def test_reaper_survives_hung_model(hung) -> None:
    rig, qualifier = hung
    stuck_a = new_lead(rig, "req-1")
    qualifier.hang_ids.add(stuck_a)
    rig.clock.advance(PLENTY)
    rig.desk.start_background(0.05)
    try:
        assert wait_for(lambda: follow(rig, stuck_a, "last_error")[0] == "internal:ModelTimeout")
        # while the first lead's model call is still hung: new work is driven, and a stuck `sending` row is put back
        other = new_lead(rig, "req-2")
        stuck_row = new_lead(rig, "req-3")
        set_follow(rig, stuck_row, state="sending", attempts=1, auto_attempts=1, updated_at=rig.clock() - PLENTY)
        rig.clock.advance(PLENTY)
        assert wait_for(lambda: follow(rig, other, "state")[0] == "sent")
        assert wait_for(lambda: follow(rig, stuck_row, "last_error")[0] == "crash_while_sending")  # put back
        rig.clock.advance(PLENTY)  # its backoff is over: it is driven too
        assert wait_for(lambda: follow(rig, stuck_row, "state")[0] == "sent")
        assert rig.desk.reaper_running()
    finally:
        # Stop the reaper FIRST and let the hung call go afterwards: released earlier, the first lead's backoff
        # (the clock was advanced) is over and a last reaper pass could drive it too, a third send (a flaky test).
        rig.desk.stop_background()
        qualifier.release.set()
    assert rig.outbox.count == 2 and rig.crm.count == 2  # the two leads that could be driven, once each
    assert follow(rig, stuck_a, "state")[0] == "pending"  # the hung one was never sent


def test_hung_model_spends_the_try(hung) -> None:
    rig, qualifier = hung
    lid = new_lead(rig, "req-4")
    qualifier.hang_all = True
    began = time.monotonic()
    rig.desk.run_followup(lid)
    assert time.monotonic() - began < 5  # the desk stopped waiting by itself
    assert follow(rig, lid, "state, auto_attempts, last_error, ai_label") == (
        "pending",
        1,
        "internal:ModelTimeout",
        None,
    )
    assert rig.crm.count == 0 and rig.outbox.count == 0


def test_late_answer_writes_nothing(hung) -> None:
    rig, qualifier = hung
    lid = new_lead(rig, "req-5")
    qualifier.hang_all = True
    rig.desk.run_followup(lid)
    assert rig.desk.hung_workers()["model"] == 1
    before = follow(rig, lid, "state, auto_attempts, last_error, ai_label, updated_at, attempts")
    qualifier.release.set()  # the abandoned call now answers
    assert wait_for(lambda: rig.desk.hung_workers()["model"] == 0)
    assert follow(rig, lid, "state, auto_attempts, last_error, ai_label, updated_at, attempts") == before
    qualifier.hang_all = False
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)  # the next try asks again and finishes: one CRM row, one email
    assert follow(rig, lid, "state")[0] == "sent"
    assert rig.crm.count == 1 and rig.outbox.count == 1


def test_hung_calls_are_capped(hung, monkeypatch) -> None:
    rig, qualifier = hung
    monkeypatch.setattr(desk_module, "MODEL_HUNG_MAX", 2)
    lid = new_lead(rig, "req-6")
    qualifier.hang_all = True
    for _ in range(2):
        rig.clock.advance(PLENTY)
        rig.desk.run_followup(lid)
    assert (qualifier.calls, rig.desk.hung_workers()["model"]) == (2, 2)
    rig.clock.advance(PLENTY)
    before = follow(rig, lid, "state, auto_attempts, attempts, last_error, updated_at")
    rig.desk.run_followup(lid)  # the cap is reached: nothing is asked, no try is spent, the row is not even claimed
    assert qualifier.calls == 2
    # a path closed by abandoned calls is seen BEFORE the claim and the row keeps its age,
    # so it ages into the stalled list.
    assert follow(rig, lid, "state, auto_attempts, attempts, last_error, updated_at") == before
    qualifier.release.set()
    assert wait_for(lambda: rig.desk.hung_workers()["model"] == 0)
    qualifier.hang_all = False
    rig.clock.advance(PLENTY)
    assert rig.desk.reap_once()["driven"] == 1
    assert follow(rig, lid, "state")[0] == "sent" and rig.outbox.count == 1


def test_hung_tries_end_in_failed(hung, monkeypatch) -> None:
    rig, qualifier = hung
    monkeypatch.setattr(desk_module, "MODEL_HUNG_MAX", 100)
    lid = new_lead(rig, "req-7")
    qualifier.hang_all = True
    for _ in range(SEND_MAX_ATTEMPTS + 2):
        rig.clock.advance(PLENTY)
        rig.desk.reap_once()
    assert qualifier.calls == SEND_MAX_ATTEMPTS  # the cap of tries bounds how often the model is asked
    assert follow(rig, lid, "state, last_error") == ("failed", "internal:ModelTimeout")
    assert rig.crm.count == 0 and rig.outbox.count == 0


def test_model_error_reaches_the_drive(hung) -> None:
    """An exception of the model call is raised again on the driving thread: it is one used-up try."""
    rig, qualifier = hung

    def boom(lead_id: str, email_key: str) -> Any:
        raise KeyError("bad label")

    qualifier.qualify = boom  # type: ignore[method-assign]
    lid = new_lead(rig, "req-8")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 1, "internal:KeyError")


# =====
# the stamp margin counts the extra busy wait
# =====
def test_margin_counts_the_busy_wait() -> None:
    write, busy = gate.WRITE_DEADLINE_SECONDS, constants.BUSY_WAIT_SECONDS
    assert constants.MODEL_GRACE_MARGIN_SECONDS >= write + busy
    with pytest.raises(ValueError, match="cover the write"):
        constants.validate_model_budget(1, write, 100, write, busy)  # a margin of the write deadline alone is refused
    floor = gate.SENDING_GRACE_BUSY_WAITS * busy
    assert constants.MODEL_CALL_BUDGET_SECONDS + constants.MODEL_GRACE_MARGIN_SECONDS < floor


def test_declared_just_over_budget_refused(env) -> None:
    """The desk cuts every model call at the budget, so the limit is the budget itself;
    a declaration at the budget is accepted, just above it is refused."""
    rig = env.rig()

    class Declares(RecordedQualifier):
        def __init__(self, path: Any, timeout: Any) -> None:
            super().__init__(path)
            self.timeout_seconds = timeout

    budget = constants.MODEL_CALL_BUDGET_SECONDS
    Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path, budget)).close()
    with pytest.raises(DeskSettingRefused, match="timeout_seconds"):
        Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path, budget + 0.1))
    assert 14.9 > budget  # 14.9 s is a declared time above the budget


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(0, id="zero_int"),
        pytest.param(0.0, id="zero_float"),
        pytest.param(-1, id="negative"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="minus_inf"),
        pytest.param(True, id="bool"),
        pytest.param("9", id="str"),
        pytest.param(10**400, id="huge_int"),
        pytest.param([], id="a_list"),
    ],
)
def test_bad_declared_timeout_refused(env, bad) -> None:
    rig = env.rig()

    class Declares(RecordedQualifier):
        timeout_seconds = bad

    with pytest.raises(DeskSettingRefused, match="timeout_seconds"):
        Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path))


def test_no_declared_timeout_is_accepted(env) -> None:
    rig = env.rig()
    assert not hasattr(rig.qualifier, "timeout_seconds")
    Desk(rig.config, rig.crm, rig.outbox, rig.qualifier).close()


# =====
# a lock after the model answered leaves the try spent, so the cap bounds the model calls
# =====
@pytest.mark.parametrize("where", ["_touch", "store"])
def test_locked_stamp_cannot_ask_for_ever(env, monkeypatch, where: str) -> None:
    rig = env.rig()

    class Answers(RecordedQualifier):
        answered = False

        def qualify(self, lead_id: str, email_key: str) -> Any:
            label = super().qualify(lead_id, email_key)
            self.answered = True
            return label

    qualifier = Answers(rig.replies_path)
    rig.desk.qualifier = qualifier
    lock_writes_after_answer(rig, qualifier, monkeypatch, where)  # the real retry loop runs
    lid = new_lead(rig, "req-9")
    for _ in range(SEND_MAX_ATTEMPTS + 3):
        qualifier.answered = False  # the first stamp of a drive comes before the model is asked: not locked
        rig.clock.advance(PLENTY)
        rig.desk.reap_once()
    assert qualifier.calls == SEND_MAX_ATTEMPTS
    assert follow(rig, lid, "state, last_error") == ("failed", "internal:ModelAnswerLost")
    assert rig.crm.count == 0 and rig.outbox.count == 0


# =====
# the CRM-down path with a leftover intent mark
# =====
class SwitchCrm(FakeCrm):
    """A CRM whose mode read fails while `down` and then says what `mode` is; it counts what it is asked."""

    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.down = True
        self.capability: str | None = None

    def refresh(self) -> None:
        if self.down:
            raise FakesUnreachable("URLError")
        self.capability = self.mode

    def forget_capability(self) -> None:
        self.capability = None


def test_crm_down_runs_to_failed(env) -> None:
    crm = SwitchCrm("keyed")
    rig = env.rig()
    rig.desk.crm = crm
    lid = new_lead(rig, "req-10")
    set_follow(rig, lid, crm_done=2, ai_label="warm")
    for _ in range(SEND_MAX_ATTEMPTS):
        assert rig.outbox.count == 0 and crm.count == 0
        rig.clock.advance(PLENTY)
        rig.desk.reap_once()
    assert follow(rig, lid, "state, last_error, crm_done") == ("failed", "internal:CrmUnreachable", 2)
    assert rig.outbox.count == 0 and crm.count == 0
    status, body = portal_api._review(rig.desk)
    card = next(i for i in body["items"] if i["lead_id"] == lid)
    assert card["title"] == "The follow-up email was not sent"
    assert "may already have written this lead into the CRM" in card["reason_text"]


def test_crm_back_keyed_puts_once(env) -> None:
    crm = SwitchCrm("keyed")
    rig = env.rig()
    rig.desk.crm = crm
    lid = new_lead(rig, "req-11")
    set_follow(rig, lid, crm_done=2, ai_label="warm")
    for _ in range(2):  # two tries while the CRM is down
        rig.clock.advance(PLENTY)
        rig.desk.reap_once()
        assert rig.outbox.count == 0 and crm.count == 0
    crm.down = False
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert follow(rig, lid, "state")[0] == "sent"
    assert crm.count == 1 and rig.outbox.count == 1


def test_crm_back_blind_is_unknown(env) -> None:
    crm = SwitchCrm("blind")
    rig = env.rig(outbox="blind")
    rig.desk.crm = crm
    lid = new_lead(rig, "req-12")
    set_follow(rig, lid, crm_done=2, ai_label="warm")
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert follow(rig, lid, "state")[0] == "pending" and rig.outbox.count == 0
    crm.down = False
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert follow(rig, lid, "state, last_error") == ("unknown", "crm_unconfirmed")
    assert crm.count == 0 and rig.outbox.count == 0 and isinstance(rig.outbox, BlindOutbox | KeyedOutbox)


# =====
# label store, claim and budget checks
# =====
def test_label_store_lost_claim_quiet(env, monkeypatch, caplog) -> None:
    """desk._label_step: the row is put back after the stamp and before the label is read back; the drive ends without
    an error (the row stays as the reaper left it) and nothing goes to the CRM or the sender."""
    rig = env.rig()
    lid = new_lead(rig, "req-13")
    real = rig.desk._touch
    calls = {"n": 0}

    def touch(lead_id: str, token: int, send_started: bool = False) -> bool:
        ok = real(lead_id, token, send_started)
        calls["n"] += 1
        if calls["n"] == 2:  # the stamp after the model answer: the reaper puts the row back right behind it
            sql_exec(rig.db_path, "UPDATE followups SET state='pending' WHERE lead_id=?", (lid,))
        return ok

    monkeypatch.setattr(rig.desk, "_touch", touch)
    with caplog.at_level(logging.WARNING, logger="lead_desk"):
        rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, last_error, ai_label") == ("pending", None, None)
    texts = [r.getMessage() for r in caplog.records]
    assert any("no longer ours after the AI step" in t for t in texts)  # the quiet end, not the LookupError path
    assert not any("LookupError" in t for t in texts)
    assert rig.crm.count == 0 and rig.outbox.count == 0


def test_requeued_counts_changed_rows(env) -> None:
    """The reaper's `requeued` is the number of rows changed: with the sender's mode unreadable a row whose send may
    have started is left as it is and is not counted."""

    class Unreadable(KeyedOutbox):
        def refresh(self) -> None:
            raise FakesUnreachable("URLError")

    rig = env.rig(outbox=Unreadable())
    left, moved = new_lead(rig, "req-14"), new_lead(rig, "req-15")
    old = rig.clock() - PLENTY
    set_follow(rig, left, state="sending", crm_done=1, send_started=1, attempts=1, auto_attempts=1, updated_at=old)
    set_follow(rig, moved, state="sending", crm_done=0, send_started=0, attempts=1, auto_attempts=1, updated_at=old)
    assert rig.desk.reap_once()["requeued"] == 1
    assert follow(rig, left, "state")[0] == "sending" and follow(rig, moved, "state")[0] == "pending"
