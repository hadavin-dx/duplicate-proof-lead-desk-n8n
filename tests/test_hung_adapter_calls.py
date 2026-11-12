"""Every adapter call the desk makes is bounded by ONE total deadline (the CRM put, the outbox send and the
capability reads, like the AI step), the worker caps are exact, a give-back for a full cap does not hide the row, and
the small traps around them. Made-up data only; the desk clock is explicit, the call deadline is real time
(shortened here); a hung stand-in is released by the test at the end."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from support import lead, sql_exec

from demo_only.fakes import BlindOutbox, FakeCrm, KeyedOutbox, RecordedQualifier
from lead_desk import constants, db, gate, portal_api
from lead_desk import desk as desk_module
from lead_desk.contract import AdapterBusy, ModelBusy, ModelTimeout
from lead_desk.desk import Desk, DeskSettingRefused, review_rows

PLENTY = 10_000  # a clock step larger than any grace or backoff
SHORT = 0.5  # the real-time deadline used in these tests, in seconds


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


def drive(rig: Any, lid: str) -> None:
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)


class HangCrm(FakeCrm):
    """A CRM whose put never returns for the lead ids in `hang_ids` until `release` is set (the wait is capped at 8 s so
    a failing test cannot leave a thread behind for long). `once`: only the first put of a lead hangs."""

    def __init__(self, keyed: bool) -> None:
        super().__init__()
        self.release = threading.Event()
        self.hang_ids: set[str] = set()
        self.once = False
        self.entered: list[str] = []
        self._hung: set[str] = set()
        if keyed:
            self.capability = "keyed"

    def put(self, lead_id: str, record: Any) -> Any:
        self.entered.append(lead_id)
        if lead_id in self.hang_ids and not (self.once and lead_id in self._hung):
            self._hung.add(lead_id)
            self.release.wait(8)
        return super().put(lead_id, record)


def hang_outbox(base: type) -> Any:
    """An outbox whose send never returns for the keys in `hang_ids` until `release` is set; the message is stored when
    it is released (an abandoned send that completes late). `hang_refresh`: the capability read never returns."""

    class Hangs(base):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()
            self.hang_ids: set[str] = set()
            self.once = False
            self.hang_refresh = False
            self.entered: list[str] = []
            self.refreshes = 0
            self._hung: set[str] = set()

        def refresh(self) -> None:
            self.refreshes += 1
            if self.hang_refresh:
                self.release.wait(8)

        def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Any:
            self.entered.append(send_key)
            if send_key in self.hang_ids and not (self.once and send_key in self._hung):
                self._hung.add(send_key)
                self.release.wait(8)
            return super().send(send_key, to, subject, body, timeout)

    return Hangs()


@pytest.fixture
def quick(monkeypatch) -> None:
    """The total deadline of a CRM put, a send and a read is SHORT seconds in these tests."""
    monkeypatch.setattr(desk_module, "CALL_DEADLINE_MAX_SECONDS", SHORT)


def rig_with(env: Any, *, crm: Any = None, outbox: Any = None) -> Any:
    rig = env.rig(outbox=outbox) if outbox is not None else env.rig()
    if crm is not None:
        rig.desk.crm = crm
    return rig


def drained(rig: Any) -> bool:
    return all(n == 0 for n in rig.desk.hung_workers().values()) and not any(rig.desk._workers.values())


# =====
# a CRM put, a send and a capability read cannot hold the reaper
# =====
def test_reaper_survives_hung_crm_put(env, quick) -> None:
    crm = HangCrm(keyed=True)
    rig = rig_with(env, crm=crm)
    stuck = new_lead(rig, "req-1")
    crm.hang_ids.add(stuck)
    rig.clock.advance(PLENTY)
    rig.desk.start_background(0.05)
    try:
        assert wait_for(lambda: follow(rig, stuck, "last_error")[0] == "internal:CrmTimeout")
        other = new_lead(rig, "req-2")
        rig.clock.advance(PLENTY)
        assert wait_for(lambda: follow(rig, other, "state")[0] == "sent")
        assert rig.desk.reaper_running()
    finally:
        rig.desk.stop_background()
        crm.release.set()
    assert follow(rig, other, "state")[0] == "sent" and rig.outbox.count == 1
    assert follow(rig, stuck, "state")[0] in ("pending", "failed") and "req-1" not in str(rig.outbox.keys())


def test_reaper_survives_hung_send(env, quick) -> None:
    outbox = hang_outbox(KeyedOutbox)
    rig = rig_with(env, outbox=outbox)
    stuck = new_lead(rig, "req-3")
    outbox.hang_ids.add(stuck)
    rig.clock.advance(PLENTY)
    rig.desk.start_background(0.05)
    try:
        assert wait_for(lambda: follow(rig, stuck, "last_error")[0] == "ambiguous")
        other = new_lead(rig, "req-4")
        rig.clock.advance(PLENTY)
        assert wait_for(lambda: follow(rig, other, "state")[0] == "sent")
        assert rig.desk.reaper_running()
    finally:
        rig.desk.stop_background()
        outbox.release.set()
    assert follow(rig, other, "state")[0] == "sent"
    assert wait_for(lambda: drained(rig))


def test_reaper_survives_hung_refresh(env, quick) -> None:
    outbox = hang_outbox(KeyedOutbox)
    rig = rig_with(env, outbox=outbox)
    outbox.hang_refresh = True
    lid = new_lead(rig, "req-5")
    rig.clock.advance(PLENTY)  # the capability read is due again
    rig.desk.start_background(0.05)
    try:
        assert wait_for(lambda: follow(rig, lid, "state")[0] == "sent")
        assert outbox.refreshes >= 2 and rig.desk.reaper_running()  # the read was asked, and cut off
    finally:
        rig.desk.stop_background()
        outbox.release.set()
    assert wait_for(lambda: drained(rig))


def test_hung_read_means_unreadable(env, quick, caplog) -> None:
    outbox = hang_outbox(KeyedOutbox)
    rig = rig_with(env, outbox=outbox)
    outbox.hang_refresh = True
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="lead_desk"):
        assert rig.desk._refresh_outbox() is False  # the mode could not be read: the same as a failed read
    assert time.monotonic() - began < 5
    assert any("RefreshTimeout" in r.getMessage() for r in caplog.records)
    outbox.release.set()
    assert wait_for(lambda: drained(rig))


# =====
# Outcome mapping: a timeout is ambiguous, an abandoned worker cannot cause a second email or CRM row
# =====
def test_blind_send_timeout_is_unknown(env, quick) -> None:
    outbox = hang_outbox(BlindOutbox)
    rig = rig_with(env, outbox=outbox)
    lid = new_lead(rig, "req-6")
    outbox.hang_ids.add(lid)
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, last_error, ambiguous_prior") == ("unknown", "ambiguous", 1)
    assert outbox.entered == [lid]
    for _ in range(3):  # the reaper and a replay never send it again blind
        rig.clock.advance(PLENTY)
        rig.desk.reap_once()
        rig.desk.run_followup(lid)
    assert outbox.entered == [lid] and outbox.count == 0
    outbox.release.set()  # the abandoned send completes late: exactly one message, none from the desk
    assert wait_for(lambda: outbox.count == 1 and drained(rig))
    assert follow(rig, lid, "state, last_error")[0] == "unknown"
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert outbox.count == 1 and outbox.entered == [lid]


def test_blind_retry_waits_for_old_send(env, quick) -> None:
    """A blind send timed out and its call is still running: a person's `not_sent` is refused (the old send may still
    land, so "nothing there yet" proves nothing), Retry cannot start a second send, and once the old send has ended
    the person looks again and answers `sent`: exactly one message ever."""
    outbox = hang_outbox(BlindOutbox)
    rig = rig_with(env, outbox=outbox)
    lid = new_lead(rig, "req-7")
    outbox.hang_ids.add(lid)
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state")[0] == "unknown"
    refused = rig.desk.resolve(f"F{lid}", "not_sent")
    assert refused.status == 409 and refused.body["error"] == "call_still_running"
    assert follow(rig, lid, "state")[0] == "unknown"  # nothing changed
    assert rig.desk.retry(f"F{lid}").status == 409  # an unknown row cannot be retried
    outbox.release.set()
    assert wait_for(lambda: drained(rig))
    assert outbox.count == 1  # only the first, abandoned send reached the sender
    assert rig.desk.resolve(f"F{lid}", "sent").status == 200
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert outbox.count == 1 and outbox.entered == [lid]


def test_blind_call_for_lead_blocks_claim(env) -> None:
    """While a blind call for the lead is still running, the row is not claimed and given back every few seconds
    (each claim would restamp it): it stays as it is, keeps its age, and ages into the stalled list."""
    rig = env.rig(outbox=BlindOutbox())
    lid = new_lead(rig, "req-8")
    before = follow(rig, lid, "state, attempts, updated_at, auto_attempts")
    set_follow(rig, lid, crm_done=1, ai_label="warm")
    rig.desk._tagged[("send", lid)] = 1  # an old blind send for this lead is still running
    assert rig.desk._closed_kind(lid) == "send"
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    state, attempts, _, auto = follow(rig, lid, "state, attempts, updated_at, auto_attempts")
    assert (state, attempts, auto) == (before[0], before[1], before[3])  # never claimed
    del rig.desk._tagged[("send", lid)]
    assert rig.desk._closed_kind(lid) is None


def test_full_healthy_cap_closes_the_row_without_a_claim(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-9")
    set_follow(rig, lid, crm_done=1, ai_label="warm")
    rig.desk._workers["send"] = desk_module.SEND_WORKERS_MAX  # healthy slow sends hold every place
    try:
        assert rig.desk._closed_kind(lid) == "send"
    finally:
        rig.desk._workers["send"] = 0


def test_keyed_send_timeout_same_key(env, quick) -> None:
    outbox = hang_outbox(KeyedOutbox)
    rig = rig_with(env, outbox=outbox)
    lid = new_lead(rig, "req-10")
    outbox.hang_ids.add(lid)
    outbox.once = True  # only the first send hangs
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, last_error") == ("pending", "ambiguous")
    drive(rig, lid)  # the next try asks again with the same key and goes through
    assert follow(rig, lid, "state")[0] == "sent"
    assert outbox.entered == [lid, lid] and outbox.count == 1
    outbox.release.set()  # the abandoned first send completes late with the same key: no second message
    assert wait_for(lambda: drained(rig))
    assert outbox.keys() == [lid]


def test_late_send_writes_nothing(env, quick) -> None:
    outbox = hang_outbox(KeyedOutbox)
    rig = rig_with(env, outbox=outbox)
    lid = new_lead(rig, "req-11")
    outbox.hang_ids.add(lid)
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    cols = "state, auto_attempts, last_error, ai_label, updated_at, attempts, crm_done, send_started, sent_at"
    before = follow(rig, lid, cols)
    outbox.release.set()
    assert wait_for(lambda: drained(rig) and outbox.count == 1)
    assert follow(rig, lid, cols) == before  # the late completion wrote nothing to the database


def test_blind_crm_timeout_is_unknown(env, quick) -> None:
    crm = HangCrm(keyed=False)
    rig = rig_with(env, crm=crm)
    lid = new_lead(rig, "req-12")
    crm.hang_ids.add(lid)
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, last_error, crm_done") == ("unknown", "crm_unconfirmed", 2)
    assert rig.outbox.count == 0 and crm.entered == [lid]
    # a person says the row is not there while the old put is still running: refused, no second put can follow
    refused = rig.desk.resolve(f"F{lid}", "not_sent")
    assert refused.status == 409 and refused.body["error"] == "call_still_running"
    assert crm.entered == [lid] and rig.outbox.count == 0
    crm.release.set()
    assert wait_for(lambda: drained(rig) and crm.count == 1)
    assert rig.desk.resolve(f"F{lid}", "crm_present").status == 200  # the person looks again: the row is there


def test_keyed_crm_timeout_same_key(env, quick) -> None:
    crm = HangCrm(keyed=True)
    rig = rig_with(env, crm=crm)
    lid = new_lead(rig, "req-13")
    crm.hang_ids.add(lid)
    crm.once = True
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, last_error, crm_done") == ("pending", "internal:CrmTimeout", 2)
    drive(rig, lid)  # asked again with the same key: one row
    assert follow(rig, lid, "state")[0] == "sent"
    crm.release.set()  # the abandoned put completes late: the key keeps it to one row
    assert wait_for(lambda: drained(rig))
    assert crm.count == 1 and crm.entered == [lid, lid] and rig.outbox.count == 1


def test_late_crm_put_writes_nothing(env, quick) -> None:
    crm = HangCrm(keyed=True)
    rig = rig_with(env, crm=crm)
    lid = new_lead(rig, "req-14")
    crm.hang_ids.add(lid)
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    cols = "state, auto_attempts, last_error, ai_label, updated_at, attempts, crm_done, send_started"
    before = follow(rig, lid, cols)
    crm.release.set()
    assert wait_for(lambda: drained(rig) and crm.count == 1)
    assert follow(rig, lid, cols) == before


# =====
# The caps: exact, visible, and a full cap does not hide the row
# =====
def test_workers_stay_within_the_cap(env, monkeypatch) -> None:
    """8 drives ask the model at the same moment: at most the cap of workers is ever started (the slot is taken
    before the worker starts; a check that only reads a count raised later lets all 8 through)."""
    rig = env.rig()
    monkeypatch.setattr(desk_module, "MODEL_CALL_BUDGET_SECONDS", SHORT)
    started = 0
    gate_open = threading.Event()
    lock = threading.Lock()

    class Counted(RecordedQualifier):
        def qualify(self, lead_id: str, email_key: str) -> Any:
            nonlocal started
            with lock:
                started += 1
            gate_open.wait(8)
            return "warm"

    rig.desk.qualifier = Counted(rig.replies_path)
    parties = 8
    start = threading.Barrier(parties, timeout=15)
    outcomes: list[str] = []

    def one() -> None:
        start.wait()
        try:
            rig.desk._ask_model("L0", "a@example.test")
            outcomes.append("answered")
        except ModelBusy:
            outcomes.append("busy")
        except ModelTimeout:
            outcomes.append("timeout")

    threads = [threading.Thread(target=one) for _ in range(parties)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    try:
        assert not any(t.is_alive() for t in threads)
        assert started == gate.MODEL_HUNG_MAX
        assert sorted(outcomes) == ["busy"] * (parties - gate.MODEL_HUNG_MAX) + ["timeout"] * gate.MODEL_HUNG_MAX
        assert rig.desk.hung_workers()["model"] == gate.MODEL_HUNG_MAX
    finally:
        gate_open.set()
    assert wait_for(lambda: drained(rig))


def test_busy_give_back_keeps_the_age(env, monkeypatch) -> None:
    """A give-back for a full cap does not move updated_at (a give-back for a locked database does)."""
    rig = env.rig()
    lid = new_lead(rig, "req-15")
    assert rig.desk._claim(None, lid, ("pending",))
    token = rig.desk._take_token(lid)
    stamped = follow(rig, lid, "updated_at")[0]
    rig.clock.advance(50)
    rig.desk._give_back(lid, token, gate.MODEL_BUSY_ERROR)
    assert follow(rig, lid, "state, last_error, updated_at") == ("pending", gate.MODEL_BUSY_ERROR, stamped)
    assert rig.desk._claim(None, lid, ("pending",))
    token = rig.desk._take_token(lid)
    rig.desk._give_back(lid, token, None)
    assert follow(rig, lid, "updated_at")[0] == stamped + 50  # every other cause keeps the plain stamp


def test_full_model_cap_shows_the_row(env, monkeypatch) -> None:
    """With every model slot held by an abandoned call, a row that needs the model is not claimed and given back every
    few seconds: it keeps its age and appears in the review desk's stalled list after stall_seconds."""
    rig = env.rig()
    monkeypatch.setattr(desk_module, "MODEL_CALL_BUDGET_SECONDS", SHORT)
    monkeypatch.setattr(desk_module, "MODEL_HUNG_MAX", 1)
    hangs = _Hangs(rig.replies_path)
    rig.desk.qualifier = hangs
    first, waiting = new_lead(rig, "req-16"), new_lead(rig, "req-17")
    hangs.hang_ids.add(first)
    drive(rig, first)
    assert rig.desk.hung_workers()["model"] == 1
    created = follow(rig, waiting, "updated_at")[0]
    try:
        for _ in range(6):
            rig.clock.advance(30)
            rig.desk.reap_once()
        assert follow(rig, waiting, "state, attempts, updated_at") == ("pending", 0, created)  # never claimed
        assert hangs.calls == 1
        items = review_rows(rig.desk._conn(), rig.desk.stalled_before())
        assert any(i["item_id"] == "F" + waiting and i["kind"] == "stalled" for i in items)
    finally:
        hangs.release.set()
    assert wait_for(lambda: drained(rig))


def test_busy_give_back_race_keeps_age(env, monkeypatch) -> None:
    """The look before the claim can be a moment too early: the row is claimed, finds no slot (ModelBusy) and is given
    back with the age of its stamp, nothing asked, no try spent."""
    rig = env.rig()
    monkeypatch.setattr(desk_module, "MODEL_HUNG_MAX", 0)  # no slot at all
    monkeypatch.setattr(rig.desk, "_closed_kind", lambda lead_id: None)
    lid = new_lead(rig, "req-18")
    real = rig.desk._touch

    def touch(lead_id: str, token: int, send_started: bool = False) -> bool:
        ok = real(lead_id, token, send_started)
        rig.clock.advance(50)  # time passes between the stamp and the give-back
        return ok

    monkeypatch.setattr(rig.desk, "_touch", touch)
    stamped = rig.clock()
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, last_error, updated_at") == (
        "pending",
        0,
        gate.MODEL_BUSY_ERROR,
        stamped,
    )
    assert rig.crm.count == 0 and rig.outbox.count == 0 and rig.qualifier.calls == 0


def test_full_send_cap_gives_back(env, quick, monkeypatch) -> None:
    monkeypatch.setattr(desk_module, "SEND_WORKERS_MAX", 2)
    outbox = hang_outbox(KeyedOutbox)
    rig = rig_with(env, outbox=outbox)
    first, second, third = (new_lead(rig, f"hang-a-{c}") for c in "abc")
    outbox.hang_ids.update((first, second))
    drive(rig, first)
    drive(rig, second)
    assert rig.desk.hung_workers()["send"] == 2
    drive(rig, third)  # no slot: nothing is sent, the try comes back, the CRM row stays done
    assert outbox.entered == [first, second]
    assert follow(rig, third, "state, auto_attempts, last_error, crm_done, send_started") == (
        "pending",
        0,
        gate.SEND_BUSY_ERROR,
        1,
        0,
    )
    outbox.release.set()
    assert wait_for(lambda: drained(rig))
    drive(rig, third)
    assert follow(rig, third, "state")[0] == "sent" and outbox.keys().count(third) == 1


def test_full_cap_logs_once(env, monkeypatch, caplog) -> None:
    rig = env.rig()
    monkeypatch.setattr(desk_module, "MODEL_CALL_BUDGET_SECONDS", SHORT)
    monkeypatch.setattr(desk_module, "MODEL_HUNG_MAX", 2)
    hangs = _Hangs(rig.replies_path)
    rig.desk.qualifier = hangs
    stuck = [new_lead(rig, f"hang-b-{c}") for c in "ab"]
    waiting = [new_lead(rig, f"hang-b-{c}") for c in "cde"]
    hangs.hang_ids.update(stuck)
    try:
        with caplog.at_level(logging.WARNING, logger="lead_desk"):
            for lid in stuck:
                drive(rig, lid)
            for _ in range(4):  # several passes over three waiting rows
                rig.clock.advance(30)
                rig.desk.reap_once()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        warned = [r for r in caplog.records if "at their cap" in r.getMessage()]
        assert len(errors) == 1 and "every model slot" in errors[0].getMessage()
        assert len(warned) == 1  # one line for the burst, not one per row per pass
        assert hangs.calls == 2 and all(follow(rig, w, "attempts")[0] == 0 for w in waiting)
    finally:
        hangs.release.set()
    assert wait_for(lambda: drained(rig))


def test_thread_start_failure_is_busy(env, monkeypatch) -> None:
    """No thread can be started (RuntimeError): nothing was asked, no try is spent, the slot is given back."""
    rig = env.rig()
    lid = new_lead(rig, "req-19")
    rig.clock.advance(PLENTY)

    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", refuse)
        rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 0, gate.MODEL_BUSY_ERROR)
    assert rig.qualifier.calls == 0 and not any(rig.desk._workers.values())
    with pytest.raises(AdapterBusy) as raised:
        with monkeypatch.context() as m:
            m.setattr(threading.Thread, "start", refuse)
            rig.desk._bounded("crm", lambda: None, 1.0, lambda: TimeoutError())
    assert raised.value.kind == "crm" and not any(rig.desk._workers.values())


def test_hung_workers_are_counted(env, quick) -> None:
    crm = HangCrm(keyed=True)
    rig = rig_with(env, crm=crm)
    lid = new_lead(rig, "req-20")
    crm.hang_ids.add(lid)
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    try:
        body = rig.desk.counts(None).body
        assert body["hung_workers"] == {"model": 0, "crm": 1, "send": 0, "refresh": 0}
        assert portal_api._summary(rig.desk, 0)["hung_workers"] == body["hung_workers"]
    finally:
        crm.release.set()
    assert wait_for(lambda: rig.desk.counts(None).body["hung_workers"]["crm"] == 0)


def test_stop_waits_only_its_timeout(env, monkeypatch) -> None:
    """A pass that is inside a hung call does not end when stop_background asks: stop returns after its own timeout, the
    desk keeps tracking the reaper, and the reaper ends by itself when the call has been cut off."""
    outbox = hang_outbox(KeyedOutbox)
    monkeypatch.setattr(desk_module, "CALL_DEADLINE_MAX_SECONDS", 1.5)
    rig = rig_with(env, outbox=outbox)
    lid = new_lead(rig, "req-21")
    outbox.hang_ids.add(lid)
    rig.clock.advance(PLENTY)
    rig.desk.start_background(0.05)
    try:
        assert wait_for(lambda: outbox.entered == [lid])  # the reaper is inside the hung send
        began = time.monotonic()
        rig.desk.stop_background(timeout=0.2)
        assert time.monotonic() - began < 1.2
        assert rig.desk.reaper_running()  # still finishing its drive
        assert wait_for(lambda: not rig.desk.reaper_running(), 10)  # the deadline cut the call, the reaper ended
    finally:
        outbox.release.set()
    assert wait_for(lambda: drained(rig))
    assert follow(rig, lid, "state, last_error") == ("pending", "ambiguous")


# =====
# The declared value, huge numbers, the descriptor reserve, the deadline from the config
# =====
class _Declares(RecordedQualifier):
    def __init__(self, path: Any, timeout: Any) -> None:
        super().__init__(path)
        self.timeout_seconds = timeout


def test_declared_above_budget_is_refused(env) -> None:
    rig = env.rig()
    budget = constants.MODEL_CALL_BUDGET_SECONDS
    Desk(
        rig.config, rig.crm, rig.outbox, _Declares(rig.replies_path, budget)
    ).close()  # the largest accepted limit: 9 s
    Desk(rig.config, rig.crm, rig.outbox, _Declares(rig.replies_path, 1)).close()
    for too_much in (budget + 0.1, 12, 14.9, 15, 10**6):
        with pytest.raises(DeskSettingRefused, match="above"):
            Desk(rig.config, rig.crm, rig.outbox, _Declares(rig.replies_path, too_much))


def test_huge_number_is_a_refusal(env) -> None:
    """math.isfinite(10**400) raises OverflowError: it must be a clean refusal, not a traceback."""
    rig = env.rig()
    for huge in (10**400, -(10**400)):
        with pytest.raises(DeskSettingRefused, match="timeout_seconds"):
            Desk(rig.config, rig.crm, rig.outbox, _Declares(rig.replies_path, huge))
    assert not constants.is_positive_seconds(10**400)
    assert constants.is_positive_seconds(10**20)
    with pytest.raises(ValueError, match="must be a number of seconds above 0"):
        constants.validate_model_budget(10**400, 15, 25, 10, 5)
    with pytest.raises(ValueError, match="must be a number of seconds above 0"):
        constants.validate_http_limits(
            max_connections=256,
            header=10**400,
            body=10.0,
            busy_write=1.0,
            retry_after=1,
        )


def test_call_deadline_follows_the_config(env) -> None:
    from dataclasses import replace

    rig = env.rig()
    assert rig.desk._call_deadline() == min(rig.config.send_timeout_seconds, constants.CALL_DEADLINE_MAX_SECONDS)
    short = Desk(replace(rig.config, send_timeout_seconds=3.0), rig.crm, rig.outbox, rig.qualifier)
    long = Desk(replace(rig.config, send_timeout_seconds=30.0), rig.crm, rig.outbox, rig.qualifier)
    try:
        assert short._call_deadline() == 3.0
        assert long._call_deadline() == constants.CALL_DEADLINE_MAX_SECONDS
    finally:
        short.close()
        long.close()


def test_deadline_fits_the_grace() -> None:
    floor = gate.SENDING_GRACE_BUSY_WAITS * constants.BUSY_WAIT_SECONDS
    for budget in (constants.MODEL_CALL_BUDGET_SECONDS, constants.CALL_DEADLINE_MAX_SECONDS):
        assert budget + constants.MODEL_GRACE_MARGIN_SECONDS < floor
        constants.validate_model_budget(
            budget,
            constants.MODEL_GRACE_MARGIN_SECONDS,
            floor,
            gate.WRITE_DEADLINE_SECONDS,
            constants.BUSY_WAIT_SECONDS,
        )
    with pytest.raises(ValueError, match="below the stuck grace"):
        constants.validate_model_budget(10, constants.MODEL_GRACE_MARGIN_SECONDS, floor, 10, 5)


def test_fd_reserve_counts_abandoned() -> None:
    items = dict(constants.FD_RESERVE_ITEMS)
    assert items["abandoned adapter workers"] == constants.ADAPTER_WORKERS_TOTAL == 24
    assert sum(items.values()) <= constants.FD_RESERVE
    with pytest.raises(ValueError, match="FD_RESERVE is"):
        constants.validate_descriptor_budget(4, 20, constants.FD_RESERVE_ITEMS, constants.PORTAL_MAX_INFLIGHT)


def test_worker_caps_are_checked() -> None:
    caps = constants.ADAPTER_WORKER_CAPS
    constants.validate_worker_caps(gate.DRIVE_MAX, caps)
    with pytest.raises(ValueError, match="crm must not be below"):
        constants.validate_worker_caps(gate.DRIVE_MAX, (("model", 4), ("crm", 7), ("send", 8), ("refresh", 4)))
    with pytest.raises(ValueError, match="send must be a whole number"):
        constants.validate_worker_caps(gate.DRIVE_MAX, (("model", 4), ("crm", 8), ("send", 0), ("refresh", 4)))


# =====
# A lock through the result write of a model answer (the real lock path, not a patched one)
# =====
class _Hangs(RecordedQualifier):
    """Hangs for the lead ids in `hang_ids` until `release` is set (capped at 8 s)."""

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self.release = threading.Event()
        self.hang_ids: set[str] = set()

    def qualify(self, lead_id: str, email_key: str) -> Any:
        label = super().qualify(lead_id, email_key)
        if lead_id in self.hang_ids:
            self.release.wait(8)
        return label


@pytest.fixture
def locked_after_answer(env, monkeypatch) -> Iterator[Any]:
    """A rig whose database is really locked by another connection from the moment the model has answered (once)."""
    rig = env.rig()
    monkeypatch.setattr(db, "BUSY_WAIT_SECONDS", 0.05)  # connections opened from now on wait 50 ms, not 5 s
    monkeypatch.setattr(desk_module, "WRITE_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(desk_module, "GATE_WAIT_SECONDS", 0.2)
    rig.desk.close()  # this thread's connection is opened again with the short wait
    holder: dict[str, Any] = {}

    class LocksAfter(RecordedQualifier):
        lock_next = True

        def qualify(self, lead_id: str, email_key: str) -> Any:
            label = super().qualify(lead_id, email_key)
            if self.lock_next:
                self.lock_next = False
                other = sqlite3.connect(str(rig.db_path), timeout=0, isolation_level=None, check_same_thread=False)
                other.execute("BEGIN IMMEDIATE")  # holds the write lock until the test lets go
                holder["conn"] = other
            return label

    qualifier = LocksAfter(rig.replies_path)
    rig.desk.qualifier = qualifier
    holder["rig"] = rig

    def release() -> None:
        other = holder.pop("conn", None)
        if other is not None:
            other.rollback()
            other.close()

    holder["release"] = release
    yield holder
    release()


def test_answer_lost_real_lock_keeps_try(locked_after_answer) -> None:
    rig, release = locked_after_answer["rig"], locked_after_answer["release"]
    lid = new_lead(rig, "req-22")
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)  # the answer arrives, every write is refused by the real lock, nothing can be recorded
    assert "conn" in locked_after_answer
    release()
    assert follow(rig, lid, "state, auto_attempts, ai_label, crm_done, send_started") == ("sending", 1, None, 0, 0)
    assert rig.crm.count == 0 and rig.outbox.count == 0
    rig.clock.advance(PLENTY)
    assert rig.desk.reap_once()["requeued"] == 1  # put back by the reaper; the try stays used up (not given back)
    assert follow(rig, lid, "state, auto_attempts, last_error, ambiguous_prior") == (
        "pending",
        1,
        "crash_while_sending",
        0,
    )
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert follow(rig, lid, "state")[0] == "sent" and rig.crm.count == 1 and rig.outbox.count == 1


def test_answer_lost_card_not_email_left(locked_after_answer) -> None:
    rig, release = locked_after_answer["rig"], locked_after_answer["release"]
    lid = new_lead(rig, "req-23")
    rig.clock.advance(PLENTY)
    rig.desk.run_followup(lid)
    release()
    set_follow(rig, lid, auto_attempts=gate.SEND_MAX_ATTEMPTS)  # the tries are used up: the reaper ends it `failed`
    rig.clock.advance(PLENTY)
    rig.desk.reap_once()
    assert follow(rig, lid, "state, last_error, ambiguous_prior") == ("failed", "crash_while_sending", 0)
    _, body = portal_api._review(rig.desk)
    card = next(i for i in body["items"] if i["lead_id"] == lid)
    said = card["title"] + " " + card["reason_text"]
    assert card["title"] == "The follow-up email was not sent" and "may have left" not in said
    assert rig.crm.count == 0 and rig.outbox.count == 0


def test_may_have_left_card_stays(env) -> None:
    """The other side of the rule: a put-back row whose send may have started still says the email may have left."""
    rig = env.rig()
    lid = new_lead(rig, "req-24")
    set_follow(rig, lid, state="failed", last_error="crash_while_sending", ambiguous_prior=1, crm_done=1)
    _, body = portal_api._review(rig.desk)
    card = next(i for i in body["items"] if i["lead_id"] == lid)
    assert card["title"] == "Follow-up did not finish" and "may already have been sent" in card["reason_text"]


def test_main_exit_text_names_both_cases() -> None:
    from lead_desk import server

    text = " ".join((server.main.__doc__ or "").split())
    assert "is not a number above 0" in text and "above MODEL_CALL_BUDGET_SECONDS" in text


# =====
# An abandoned refresh or put cannot put an old mode back
# =====
@pytest.fixture(params=["real_clock", "frozen_clock"])
def clock_kind(request, monkeypatch) -> str:
    """The order of calls must not come from a clock: a coarse or frozen clock gives two calls one reading."""
    if request.param == "frozen_clock":
        monkeypatch.setattr(time, "monotonic_ns", lambda: 1_000_000)
        monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    return str(request.param)


def test_late_refresh_keeps_newer_mode(monkeypatch, clock_kind) -> None:
    from lead_desk import adapters

    gate_one = threading.Event()
    answers = iter([{"crm_capability": "keyed"}, {"crm_capability": "blind"}])
    started_first = threading.Event()

    def exchange(url: str, payload: Any, timeout: float) -> Any:
        info = next(answers)
        if info["crm_capability"] == "keyed":
            started_first.set()
            gate_one.wait(5)  # refresh 1 is slow: it answers after refresh 2 has finished
        return info

    monkeypatch.setattr(adapters, "_exchange", exchange)
    crm = adapters.HttpCrm("http://127.0.0.1:9", 1.0)
    slow = threading.Thread(target=crm.refresh)
    slow.start()
    assert started_first.wait(5)
    crm.refresh()  # the newer read: blind
    assert crm.capability == "blind"
    gate_one.set()
    slow.join(5)
    assert crm.capability == "blind"  # the late answer of the older read is dropped


def test_late_put_keeps_newer_mode(monkeypatch, clock_kind) -> None:
    """The answer of an abandoned put names the mode of the time it started; it must not overwrite a newer read."""
    from lead_desk import adapters

    gate = threading.Event()
    put_started = threading.Event()

    def exchange(url: str, payload: Any, timeout: float) -> Any:
        if payload is not None:  # the put
            put_started.set()
            gate.wait(5)
            return {"status": "put", "mode": "keyed"}
        return {"crm_capability": "blind"}  # the newer read

    monkeypatch.setattr(adapters, "_exchange", exchange)
    crm = adapters.HttpCrm("http://127.0.0.1:9", 1.0)
    slow = threading.Thread(target=crm.put, args=("L1", {"a": "b"}))
    slow.start()
    assert put_started.wait(5)
    crm.refresh()
    assert crm.capability == "blind"
    gate.set()
    slow.join(5)
    assert crm.capability == "blind"  # the late put's mode is dropped


def test_late_outbox_refresh_keeps_newer(monkeypatch, clock_kind) -> None:
    from lead_desk import adapters

    gate_one = threading.Event()
    first = {"outbox_capability": "keyed", "mode": "keyed"}
    blind = {"outbox_capability": "blind", "mode": "blind"}
    calls = {"n": 0}
    started_second_call = threading.Event()

    def exchange(url: str, payload: Any, timeout: float) -> Any:
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            return first  # the constructor's read
        if n == 2:
            started_second_call.set()
            gate_one.wait(5)  # refresh 1 is slow
            return first
        return blind

    monkeypatch.setattr(adapters, "_exchange", exchange)
    ob = adapters.HttpOutbox("http://127.0.0.1:9", 1.0)
    slow = threading.Thread(target=ob.refresh)
    slow.start()
    assert started_second_call.wait(5)
    ob.refresh()
    assert ob.capability == "blind"
    gate_one.set()
    slow.join(5)
    assert ob.capability == "blind"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_send_timeout_must_be_finite_and_positive(env, bad: float) -> None:
    from dataclasses import replace

    rig = env.rig()
    with pytest.raises(ValueError, match="DESK_SEND_TIMEOUT_SECONDS"):
        replace(rig.config, send_timeout_seconds=bad)
