"""The AI step is asked only after the claim: one claimed row, one model call. Made-up data only."""

from __future__ import annotations

import threading
from typing import Any

from support import lead, run_threads

from lead_desk.gate import PENDING_GRACE_SECONDS, SEND_MAX_ATTEMPTS


class SlowQualifier:
    """Counts calls, signals the first, then waits (up to `hold` seconds) before answering."""

    mode = "recorded"

    def __init__(self, hold: float, fail: bool = False) -> None:
        self.calls = 0
        self.hold = hold
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def qualify(self, lead_id: str, email_key: str) -> Any:
        with self._lock:
            self.calls += 1
        self.entered.set()
        self.release.wait(timeout=self.hold)
        if self.fail:
            raise RuntimeError("model down")
        return "warm"


def new_lead(rig: Any, ref: str) -> str:
    reply = rig.desk.process("form", ref, lead())
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def test_reaper_does_not_ask_model_again(env: Any) -> None:
    rig = env.rig()
    q = SlowQualifier(hold=3.0)
    rig.desk.qualifier = q
    lid = new_lead(rig, "req-1")
    drive = threading.Thread(target=rig.desk.run_followup, args=(lid,))
    drive.start()
    assert q.entered.wait(timeout=10)
    rig.clock.advance(PENDING_GRACE_SECONDS + 1)  # past the grace: the reaper treats a pending row as due
    rig.desk.reap_once()
    q.release.set()
    drive.join(timeout=30)
    assert q.calls == 1
    assert rig.state(lid) == ("sent", None)


def test_two_drives_ask_model_once(env: Any) -> None:
    rig = env.rig()
    q = SlowQualifier(hold=0.5)
    rig.desk.qualifier = q
    lid = new_lead(rig, "req-2")
    out = run_threads([lambda: rig.desk.run_followup(lid), lambda: rig.desk.run_followup(lid)])
    assert all(r is None for r in out), out
    assert q.calls == 1
    assert rig.state(lid) == ("sent", None)


def test_model_error_is_one_try_each(env: Any) -> None:
    rig = env.rig()
    q = SlowQualifier(hold=0.0, fail=True)
    rig.desk.qualifier = q
    lid = new_lead(rig, "req-3")
    for n in range(1, SEND_MAX_ATTEMPTS + 1):
        if n == 1:
            rig.desk.run_followup(lid)
        else:
            rig.clock.advance(20)  # longer than the longest wait between tries
            rig.desk.reap_once()
        tries, claims = rig.query("SELECT auto_attempts, attempts FROM followups")[0]
        assert (tries, claims, q.calls) == (n, n, n)
        want = "failed" if n == SEND_MAX_ATTEMPTS else "pending"
        assert rig.state(lid) == (want, "internal:RuntimeError")
    rig.clock.advance(20)
    rig.desk.reap_once()
    assert q.calls == SEND_MAX_ATTEMPTS  # a failed row is not driven again
