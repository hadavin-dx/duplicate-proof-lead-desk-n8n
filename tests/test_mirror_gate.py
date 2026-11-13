"""The wait before a failed CRM copy is tried again (the desk's in-memory gate) stays in step with the database:
a new stage starts the copy at once, a stage move during a failing call leaves no stale wait, a lead that is
waiting never blocks the others, and a lead that was given up on leaves no entry. A fake clock moves time; nothing
really waits. Made-up data only."""

from __future__ import annotations

import copy
import sqlite3
from typing import Any

from support import DEMO_LEADFLOW, CountingCrm, FakeClock, lead

from lead_desk.adapters import CrmError
from lead_desk.constants import MIRROR_MAX_TRIES, MIRROR_RETRY_WAIT_SECONDS


def _flow() -> dict[str, Any]:
    flow = copy.deepcopy(DEMO_LEADFLOW)
    flow["nudge_after_seconds"] = None
    return flow


class _Crm(CountingCrm):
    """set_fields fails for the lead ids in `down` (all leads when `down_all`); `inside` runs inside the call."""

    def __init__(self) -> None:
        super().__init__()
        self.down: set[str] = set()
        self.down_all = False
        self.inside: Any = None
        self.at: list[tuple[str, float]] = []

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        import time

        self.at.append((lead_id, time.monotonic()))
        if self.inside:
            self.inside()
        if self.down_all or lead_id in self.down:
            raise CrmError("http 503")
        super().set_fields(lead_id, email_key, crm_owner_id, crm_status)


def _tries(rig: Any, lead_id: str) -> tuple[Any, ...]:
    return rig.query("SELECT crm_tries, crm_step_done, step, step_rank FROM lead_state WHERE lead_id=?", (lead_id,))[0]


def _pass(rig: Any) -> int:
    done = {"mirrored": 0}
    rig.desk._mirror_pass(done)
    return done["mirrored"]


def _sql(rig: Any, sql: str, *args: Any) -> None:
    con = sqlite3.connect(rig.db_path)
    con.execute(sql, args)
    con.commit()
    con.close()


def test_a_new_stage_after_the_follow_up_is_copied_at_once_while_the_old_wait_runs(env: Any, monkeypatch: Any) -> None:
    clock = FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lid = str(rig.desk.handle("form", "g-1", lead()).body["lead_id"])
    for trigger in ("followups_sent_final", "lead_state_no_regress"):  # (test only) put the lead back to mid-send
        _sql(rig, f"DROP TRIGGER IF EXISTS {trigger}")
    _sql(rig, "UPDATE lead_state SET step='new', step_rank=0, crm_tries=2, crm_error='CrmError' WHERE lead_id=?", lid)
    _sql(rig, "UPDATE followups SET state='sending', crm_done=1 WHERE lead_id=?", lid)
    (token,) = rig.query("SELECT attempts FROM followups WHERE lead_id=?", (lid,))[0]
    rig.desk._mirror_not_before.set(lid, clock.now + 400, 0)  # the copy of stage `new` failed, the lead waits
    rig.desk._record_send_result(lid, "sent", token)
    assert _tries(rig, lid)[0] == 0 and _tries(rig, lid)[3] > 0  # the database says: new stage, fresh start
    assert lid not in rig.desk._mirror_not_before  # and the desk's wait is gone with it
    assert _pass(rig) == 1 and crm.at[-1][0] == lid  # copied now, not 400 s later


def test_a_stage_move_between_the_read_and_a_failed_call_leaves_no_stale_wait(env: Any, monkeypatch: Any) -> None:
    FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lid = str(rig.desk.handle("form", "g-2", lead()).body["lead_id"])
    rig.desk.run_followup(lid)
    crm.down_all = True
    crm.inside = lambda: rig.desk.set_step(lid, "qualified")  # the move lands inside the failing call
    assert _pass(rig) == 0
    crm.inside = None
    assert (
        _tries(rig, lid)[0] == 0 and _tries(rig, lid)[2] == "qualified"
    )  # the failure is not charged to the new stage
    crm.down_all = False
    assert _pass(rig) == 1 and _tries(rig, lid)[1] == 3  # the new stage is copied in the very next pass


def test_a_waiting_lead_is_skipped_while_the_others_are_copied(env: Any, monkeypatch: Any) -> None:
    clock = FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    a = str(rig.desk.handle("form", "g-3a", lead(email="a@example.test")).body["lead_id"])
    b = str(rig.desk.handle("form", "g-3b", lead(email="b@example.test")).body["lead_id"])
    for x in (a, b):
        rig.desk.run_followup(x)
    crm.down = {a, b}
    assert _pass(rig) == 0  # the first lead in line fails and waits
    first = crm.at[0][0]
    other = b if first == a else a
    crm.down = {first}
    assert _pass(rig) == 1  # the waiting lead is skipped, the other one is copied
    assert [x for x, _ in crm.at].count(first) == 1 and _tries(rig, other)[1] == 1
    clock.now += MIRROR_RETRY_WAIT_SECONDS[0] + 1
    assert _pass(rig) == 0 and [x for x, _ in crm.at].count(first) == 2  # after its wait it is tried again


def test_the_copy_keeps_the_promised_waits_then_gives_up_and_leaves_no_entry(env: Any, monkeypatch: Any) -> None:
    clock = FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lid = str(rig.desk.handle("form", "g-4", lead()).body["lead_id"])
    rig.desk.run_followup(lid)
    crm.down_all = True
    t0 = clock.now
    gave_up_at_once = None
    for _ in range(2000):  # one pass per fake second for 2000 s
        _pass(rig)
        if gave_up_at_once is None and len(crm.at) == MIRROR_MAX_TRIES:
            gave_up_at_once = lid not in rig.desk._mirror_not_before  # right after the last try, no waiting
        clock.now += 1
    offsets = [round(t - t0) for _, t in crm.at]
    starts = [0.0]
    for w in MIRROR_RETRY_WAIT_SECONDS:
        starts.append(starts[-1] + w)
    assert len(offsets) == MIRROR_MAX_TRIES and offsets == [int(s) for s in starts], offsets  # 0, 10, 70, 250, 670
    assert offsets[-1] >= 600  # about eleven minutes of trying in all
    assert gave_up_at_once is True, "the gate still holds an entry right after the last try"
    assert _tries(rig, lid)[0] == MIRROR_MAX_TRIES and lid not in rig.desk._mirror_not_before  # given up, no leak


def test_a_crm_that_recovers_inside_the_waits_gets_the_stage(env: Any, monkeypatch: Any) -> None:
    clock = FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lid = str(rig.desk.handle("form", "g-5", lead()).body["lead_id"])
    rig.desk.run_followup(lid)
    crm.down_all = True
    for _ in range(300):
        _pass(rig)
        clock.now += 1
    crm.down_all = False
    got = 0
    for _ in range(400):
        got += _pass(rig)
        clock.now += 1
    assert got == 1 and _tries(rig, lid)[:2] == (0, 1)


def test_a_stage_move_between_the_commit_and_the_gate_write_leaves_no_stale_wait(env: Any, monkeypatch: Any) -> None:
    """The failed try is committed (old stage), then a request moves the stage, then the reaper sets its wait."""
    FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lid = str(rig.desk.handle("form", "g-6", lead()).body["lead_id"])
    rig.desk.run_followup(lid)
    crm.down_all = True
    real = rig.desk._finish_update

    def move_after_commit(attempt: Any, what: str) -> None:
        real(attempt, what)
        if what == "mirror_failed":
            rig.desk.set_step(lid, "qualified")  # lands after the commit, before the gate is set

    monkeypatch.setattr(rig.desk, "_finish_update", move_after_commit)
    assert _pass(rig) == 0
    monkeypatch.setattr(rig.desk, "_finish_update", real)
    crm.down_all = False
    assert _tries(rig, lid)[2] == "qualified"
    assert _pass(rig) == 1 and _tries(rig, lid)[1] == 3  # copied in the very next pass, not up to 420 s later


def test_a_failed_try_whose_charge_was_lost_to_a_locked_database_still_waits(env: Any, monkeypatch: Any) -> None:
    clock = FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lid = str(rig.desk.handle("form", "g-7", lead()).body["lead_id"])
    rig.desk.run_followup(lid)
    crm.down_all = True
    real = rig.desk._finish_update

    def swallowed(attempt: Any, what: str) -> None:
        if what != "mirror_failed":
            real(attempt, what)  # the charge write stayed locked: it does not happen (as _finish_update swallows it)

    monkeypatch.setattr(rig.desk, "_finish_update", swallowed)
    assert _pass(rig) == 0 and len(crm.at) == 1
    assert _pass(rig) == 0 and len(crm.at) == 1  # no hot loop: the lead waits although nothing was charged
    clock.now += MIRROR_RETRY_WAIT_SECONDS[0] + 1
    assert _pass(rig) == 0 and len(crm.at) == 2  # and is tried again after its wait


def test_the_wait_belongs_to_the_stage_it_was_set_for(env: Any, monkeypatch: Any) -> None:
    clock = FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lid = str(rig.desk.handle("form", "g-8", lead()).body["lead_id"])
    rig.desk.run_followup(lid)
    (rank,) = rig.query("SELECT step_rank FROM lead_state WHERE lead_id=?", (lid,))[0]
    rig.desk._mirror_not_before.set(lid, clock.now + 400, rank)
    assert _pass(rig) == 0 and not crm.at  # same stage: waits
    rig.desk._mirror_not_before.set(lid, clock.now + 400, rank - 1)  # an entry left by an older stage
    assert _pass(rig) == 1 and crm.at  # never holds the current stage


def test_the_gate_survives_threads_moving_stages_while_the_reaper_reads_it(env: Any, monkeypatch: Any) -> None:
    import threading

    clock = FakeClock(monkeypatch)
    crm = _Crm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    ids = [str(rig.desk.handle("form", f"g-9{i}", lead(email=f"t{i}@example.test")).body["lead_id"]) for i in range(4)]
    for x in ids:
        rig.desk.run_followup(x)
    crm.down_all = True
    errors: list[BaseException] = []
    stop = threading.Event()

    def reaper() -> None:
        try:
            while not stop.is_set():
                _pass(rig)
        except BaseException as e:  # noqa: BLE001 - the test reports any fault
            errors.append(e)

    t = threading.Thread(target=reaper)
    t.start()
    for step in ("replied", "qualified", "won"):
        for x in ids:
            rig.desk.set_step(x, step)
    stop.set()
    t.join(30)
    assert not t.is_alive() and not errors, errors
    crm.down_all = False
    clock.now += MIRROR_RETRY_WAIT_SECONDS[-1] + 1  # every honest wait has run out
    for _ in range(3):
        _pass(rig)
    assert all(_tries(rig, x)[0] == 0 and _tries(rig, x)[1] == _tries(rig, x)[3] for x in ids)  # all copied


def test_the_gate_lets_one_thread_in_at_a_time() -> None:
    """The gate's own dict is touched by request threads and the reaper: every touch holds the gate's lock."""
    import sys
    import threading
    import time

    from lead_desk.desk import _MirrorWait

    class Watching(dict):  # type: ignore[type-arg]
        inside = 0
        overlap = False

        def _touch(self) -> None:
            Watching.inside += 1
            if Watching.inside > 1:
                Watching.overlap = True
            time.sleep(0)  # give the other thread its turn inside the call
            Watching.inside -= 1

        def __setitem__(self, k: Any, v: Any) -> None:
            self._touch()
            super().__setitem__(k, v)

        def pop(self, *a: Any) -> Any:
            self._touch()
            return super().pop(*a)

        def items(self) -> Any:
            self._touch()
            return super().items()

    gate = _MirrorWait()
    gate._d = Watching()
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:

        def hammer(n: int) -> None:
            for i in range(300):
                gate.set(f"l{n}{i % 5}", 5.0, i)
                gate.waiting(0.0)
                gate.clear(f"l{n}{i % 5}")

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
    finally:
        sys.setswitchinterval(old)
    assert not Watching.overlap


def test_the_gate_drops_an_entry_whose_time_has_passed() -> None:
    from lead_desk.desk import _MirrorWait

    gate = _MirrorWait()
    gate.set("a", 5.0, 1)
    gate.set("b", 50.0, 1)
    assert gate.waiting(10.0) == [["b", 1]] and "a" not in gate and "b" in gate  # no entry outlives its time
