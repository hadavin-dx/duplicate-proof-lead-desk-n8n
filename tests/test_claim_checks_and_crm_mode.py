"""The whole-second tie at the stuck grace, the label write checks the claim, a CRM that cannot be read,
the clean-up chain at exit and the narrow exit code 4. Made-up data only; the test clock is explicit."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from support import is_write, lead, sql_exec

from demo_only.fakes import FakeCrm, RecordedQualifier
from lead_desk import constants, gate, lockfile, server
from lead_desk.desk import Desk, DeskSettingRefused
from lead_desk.gate import NO_DESCRIPTORS_ERROR


def new_lead(rig: Any, ref: str) -> str:
    reply = rig.desk.process("form", ref, lead(email=f"{ref}@example.test"))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def set_follow(rig: Any, lid: str, **cols: Any) -> None:
    names = ", ".join(f"{k}=?" for k in cols)
    sql_exec(rig.db_path, f"UPDATE followups SET {names} WHERE lead_id=?", (*cols.values(), lid))  # noqa: S608


def row(rig: Any, lid: str, cols: str) -> tuple[Any, ...]:
    return rig.query(f"SELECT {cols} FROM followups WHERE lead_id=?", (lid,))[0]  # noqa: S608 (fixed column text)


# =====
# a row exactly as old as the grace is not taken
# =====
def test_reaper_needs_age_above_grace(env) -> None:
    """The clocks count whole seconds, so a difference equal to the grace can be a real age just over 29 s. The reaper
    takes a row only when it is strictly older than the grace."""
    rig = env.rig()
    lid = new_lead(rig, "req-1")
    grace = rig.desk.stuck_grace_seconds
    set_follow(rig, lid, state="sending", crm_done=0, send_started=0, updated_at=rig.clock() - grace)
    assert rig.desk.reap_once()["requeued"] == 0
    assert row(rig, lid, "state")[0] == "sending"
    rig.clock.advance(1)  # now one second older than the grace
    assert rig.desk.reap_once()["requeued"] == 1
    assert row(rig, lid, "state")[0] == "pending"


def test_equal_budget_sum_is_refused() -> None:
    # arguments: budget, margin, smallest grace, write deadline, busy wait
    constants.validate_model_budget(9, 15, 25, 10, 5)
    with pytest.raises(ValueError, match="below the stuck grace"):
        constants.validate_model_budget(10, 15, 25, 10, 5)  # equal sum: a tie on whole-second clocks
    floor = gate.SENDING_GRACE_BUSY_WAITS * constants.BUSY_WAIT_SECONDS
    assert constants.MODEL_CALL_BUDGET_SECONDS + constants.MODEL_GRACE_MARGIN_SECONDS < floor


def test_declared_timeout_above_budget_refused(env) -> None:
    """The longest time a declared call limit may have is the model call budget."""
    rig = env.rig()
    budget = constants.MODEL_CALL_BUDGET_SECONDS

    class Declares(RecordedQualifier):
        def __init__(self, path: Any, timeout: float) -> None:
            super().__init__(path)
            self.timeout_seconds = timeout

    with pytest.raises(DeskSettingRefused, match="timeout_seconds"):
        Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path, budget + 0.5))
    Desk(rig.config, rig.crm, rig.outbox, Declares(rig.replies_path, budget)).close()


# =====
# the label write checks the claim
# =====
def test_label_store_needs_the_claim(env, monkeypatch) -> None:
    """The row is put back and claimed again between the stamp and the label write: the stale drive stores nothing
    and goes no further."""
    rig = env.rig()
    lid = new_lead(rig, "req-2")
    real = rig.desk._locked_retry
    fired = {"n": 0}

    def retry(fn: Any) -> Any:
        if is_write(fn, "store"):
            fired["n"] += 1
            set_follow(rig, lid, attempts=99)  # a new claim happened first
        return real(fn)

    monkeypatch.setattr(rig.desk, "_locked_retry", retry)
    rig.desk.run_followup(lid)
    assert fired["n"] == 1
    assert row(rig, lid, "ai_label, state, crm_done, attempts") == (None, "sending", 0, 99)
    assert rig.crm.count == 0 and rig.outbox.count == 0


# =====
# a CRM whose mode cannot be read is not judged
# =====
class DownCrm(FakeCrm):
    capability = "keyed"

    def refresh(self) -> None:
        raise OSError("down")

    def forget_capability(self) -> None:
        self.capability = None  # type: ignore[assignment]  # as HttpCrm does


def test_crm_down_leftover_mark_not_unknown(env) -> None:
    """A leftover crm_done=2 and a CRM that is down: nothing was asked in this try, so no person is sent to look. The
    try is used up with a backoff (the cap then ends it `failed`, which Retry starts again) and nothing is put."""
    crm = DownCrm()
    rig = env.rig()
    rig.desk.crm = crm
    lid = new_lead(rig, "req-3")
    set_follow(rig, lid, crm_done=2, ai_label="warm")
    rig.desk.run_followup(lid)
    state, last_error, crm_done = row(rig, lid, "state, last_error, crm_done")
    assert (state, crm_done) == ("pending", 2) and str(last_error).startswith("internal:")
    assert crm.count == 0 and rig.outbox.count == 0


# =====
# every exit step runs; only the one refusal is exit 4
# =====
def test_exit_steps_run_when_one_raises(env, monkeypatch, tmp_path) -> None:
    rig = env.rig()
    seen: dict[str, Any] = {"closed": False, "socket": False}

    class Stub:
        server_address = ("127.0.0.1", 0)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            seen["socket"] = True

    class Failing(Desk):
        def stop_background(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("cannot stop")

        def close(self) -> None:
            seen["closed"] = True
            super().close()

    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "exit.sqlite3"))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setattr(server, "Desk", Failing)
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (rig.desk.crm, rig.desk.outbox, rig.desk.qualifier))
    monkeypatch.setattr(server, "make_server", lambda desk, cfg: Stub())
    errors: list[BaseException] = []

    def run() -> None:
        try:
            server.main()
        except BaseException as e:  # noqa: BLE001 (the test reports it)
            errors.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive() and not errors
    assert seen == {"closed": True, "socket": True}
    lockfile.acquire(tmp_path / "exit.sqlite3").close()  # the lock was given back


def test_other_value_error_is_not_exit_4(env, monkeypatch, tmp_path) -> None:
    rig = env.rig()
    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "other.sqlite3"))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (rig.desk.crm, rig.desk.outbox, rig.desk.qualifier))

    def broken(*a: Any, **k: Any) -> Any:
        raise ValueError("some other problem")

    monkeypatch.setattr(server, "Desk", broken)
    with pytest.raises(ValueError, match="some other problem"):  # a traceback, not a refusal
        server.main()


# =====
# a later lock give-back replaces the "no file handle" marker
# =====
def test_give_back_replaces_stale_marker(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-4")
    set_follow(rig, lid, state="sending", attempts=3, auto_attempts=1, last_error=NO_DESCRIPTORS_ERROR)
    rig.desk._give_back(lid, 3, None)  # a lock give-back
    assert row(rig, lid, "state, last_error") == ("pending", None)
    set_follow(rig, lid, state="sending", attempts=4, auto_attempts=1, last_error="unreachable")
    rig.desk._give_back(lid, 4, None)
    assert row(rig, lid, "last_error")[0] == "unreachable"  # a real earlier error is kept
