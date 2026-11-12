"""The real entry point (`lead_desk.server.main`) runs the reaper while it serves and stops it on exit.

main() calls Desk.start_background(), so with a model that fails for a while every follow-up is retried after its
backoff, and `sending` or stalled rows are repaired while the desk runs, not only at a restart."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from support import lead, wait_until

from lead_desk import lockfile, server
from lead_desk.desk import Desk


class _StubServer:
    server_address = ("127.0.0.1", 0)

    def __init__(self, hold: threading.Event) -> None:
        self.hold = hold

    def serve_forever(self) -> None:
        self.hold.wait(timeout=20)
        raise KeyboardInterrupt

    def server_close(self) -> None:
        pass


def _run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rig: Any, interval: float | None = None
) -> tuple[list[str], threading.Event, Desk, threading.Thread]:
    calls: list[str] = []
    hold = threading.Event()
    seen: list[Desk] = []

    class RecordingDesk(Desk):
        def start_background(self, *a: Any, **k: Any) -> None:
            calls.append("start_background")
            if interval is None:
                super().start_background(*a, **k)
            else:
                super().start_background(interval)

        def startup(self) -> None:
            calls.append("startup")
            super().startup()

        def stop_background(self, *a: Any, **k: Any) -> None:
            calls.append("stop_background")
            super().stop_background(*a, **k)

    def make_desk(*args: Any, **kwargs: Any) -> Desk:
        d = RecordingDesk(*args, **kwargs)
        seen.append(d)
        return d

    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "main.sqlite3"))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setattr(server, "Desk", make_desk)
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (rig.desk.crm, rig.desk.outbox, rig.desk.qualifier))
    monkeypatch.setattr(server, "make_server", lambda desk, cfg: _StubServer(hold))
    t = threading.Thread(target=server.main, daemon=True)
    t.start()
    wait_until(lambda: seen and seen[0]._thread is not None, what="the desk was built and the reaper started")
    return calls, hold, seen[0], t


def test_main_runs_reaper_while_serving(env, monkeypatch, tmp_path) -> None:
    rig = env.rig()
    calls, hold, desk, thread = _run_main(monkeypatch, tmp_path, rig)
    try:
        wait_until(lambda: desk._started.is_set(), what="startup finished")
        assert calls[:2] == ["start_background", "startup"]  # the documented order
        assert desk._thread is not None and desk._thread.is_alive()
        assert desk._background is True
    finally:
        hold.set()
        thread.join(timeout=20)
    assert not thread.is_alive()
    assert calls[-1] == "stop_background"
    assert desk._thread is None  # the reaper is stopped when the desk exits


def test_main_reaper_sends_and_frees_lock(env, monkeypatch, tmp_path) -> None:
    """While main() serves, a follow-up that was left pending (its request thread never drove it) is sent by the
    reaper, with no restart; and after main() returns the lock file can be taken again."""
    rig = env.rig()
    calls, hold, desk, thread = _run_main(monkeypatch, tmp_path, rig, interval=0.05)
    try:
        wait_until(lambda: desk._started.is_set(), what="startup finished")
        lead_id = desk.process("form", "main-reaper-1", lead()).body["lead_id"]  # decided; nobody drives it
        assert desk.lead(lead_id).body["followup"]["state"] == "pending"
        wait_until(
            lambda: desk.lead(lead_id).body["followup"]["state"] == "sent",
            timeout=20,
            what="the reaper drove the pending follow-up",
        )
    finally:
        hold.set()
        thread.join(timeout=20)
    assert not thread.is_alive()
    lock = lockfile.acquire(tmp_path / "main.sqlite3")  # main() gave the lock back
    lock.close()
