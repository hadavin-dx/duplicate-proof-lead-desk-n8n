"""Desk.start_background / stop_background: one reaper at a time, however the calls are timed. Made-up data only."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from support import wait_until

from lead_desk import desk as desk_module


def reapers(d: Any) -> list[threading.Thread]:
    """The reaper threads of THIS desk that are still alive (the current one and any stopped one still finishing).
    Not every thread of the process named desk-reaper: another test's leftover must not count here."""
    known = {t for t in (d._thread, *d._retiring) if t is not None}
    return [t for t in known if t.is_alive()]


class Counting:
    """Replaces desk.reap_once: counts passes and how many run at the same time; can hold a pass open."""

    def __init__(self) -> None:
        self.passes = 0
        self.active = 0
        self.max_active = 0
        self.hold = threading.Event()
        self.hold.set()  # a pass does not wait unless a test clears this
        self._lock = threading.Lock()

    def __call__(self) -> dict[str, int]:
        with self._lock:
            self.passes += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.hold.wait(timeout=15)
        finally:
            with self._lock:
                self.active -= 1
        return {}


@pytest.fixture
def rig_desk(env):
    d = env.rig().desk
    d._started.set()  # start-up is not what these tests are about
    counting = Counting()
    d.reap_once = counting  # type: ignore[method-assign]
    yield d, counting
    counting.hold.set()
    d.stop_background(timeout=5)


def test_stop_leaves_background_mode(rig_desk) -> None:
    d, _ = rig_desk
    d.start_background(0.05)
    assert d._background is True
    d.stop_background()
    assert d._background is False and d._thread is None


def test_second_start_is_a_noop(rig_desk) -> None:
    d, _ = rig_desk
    d.start_background(0.05)
    first = d._thread
    d.start_background(0.05)
    assert d._thread is first
    assert len(reapers(d)) == 1


def test_simultaneous_starts_one_reaper(rig_desk, monkeypatch) -> None:
    """Two threads call start_background at the same moment (thread creation made slow to widen the window)."""
    d, _ = rig_desk
    real = threading.Thread

    class SlowThread(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            time.sleep(0.3)
            super().__init__(*a, **k)

    monkeypatch.setattr(desk_module.threading, "Thread", SlowThread)
    callers = [real(target=d.start_background, args=(0.05,)) for _ in range(2)]
    for c in callers:
        c.start()
    for c in callers:
        c.join(timeout=10)
    monkeypatch.undo()
    assert len(reapers(d)) == 1


def test_start_after_stop_waits_for_old(rig_desk) -> None:
    """The old reaper is still inside a pass when it is stopped and a new one is started: the old one must end
    first, and no two passes ever run at once."""
    d, counting = rig_desk
    counting.hold.clear()
    d.start_background(0.05)
    wait_until(lambda: counting.active == 1, what="a pass in progress")
    old = d._thread
    d.stop_background(timeout=0.2)  # the join times out: the pass is still held
    assert old is not None and old.is_alive() and d._background is False
    d.start_background(0.05)
    assert d._background is True
    time.sleep(0.4)
    assert counting.passes == 1 and counting.max_active == 1  # the new one has not started a pass
    counting.hold.set()
    wait_until(lambda: not old.is_alive(), what="the old reaper ended")
    wait_until(lambda: counting.passes >= 3, what="the new reaper runs")
    assert counting.max_active == 1
    assert len(reapers(d)) == 1


def test_stop_start_stop_start_one_pass(rig_desk) -> None:
    """Two stop/start cycles while the first reaper is held inside a pass. The second reaper ends at once (it was
    still waiting), and the third must still wait for the FIRST: every not-yet-ended reaper stays tracked."""
    d, counting = rig_desk
    counting.hold.clear()
    d.start_background(0.05)
    wait_until(lambda: counting.active == 1, what="a pass in progress")
    first = d._thread
    d.stop_background(timeout=0.2)  # the first one is still held
    d.start_background(0.05)  # the second waits for the first
    d.stop_background(timeout=2)  # the second ends at once; the first still runs
    assert first is not None and first.is_alive()
    assert first in d._retiring  # still tracked, though a later stop found nothing wrong with its own reaper
    d.start_background(0.05)  # the third
    time.sleep(0.4)
    assert counting.passes == 1 and counting.max_active == 1, (counting.passes, counting.max_active)
    assert d.reaper_running()
    counting.hold.set()
    wait_until(lambda: not first.is_alive(), what="the first reaper ended")
    wait_until(lambda: counting.passes >= 3, what="the third reaper runs")
    assert counting.max_active == 1 and len(reapers(d)) == 1


def test_start_during_a_stop_waits(rig_desk) -> None:
    """A start that arrives while another thread is still inside stop_background's join must wait for that reaper
    too (a reaper must be recorded before its join, not only after the join timed out)."""
    d, counting = rig_desk
    counting.hold.clear()
    d.start_background(0.05)
    wait_until(lambda: counting.active == 1, what="a pass in progress")
    old = d._thread
    stopper = threading.Thread(target=d.stop_background, kwargs={"timeout": 5})
    stopper.start()
    wait_until(lambda: d._thread is None, what="the stop began")
    d.start_background(0.05)  # during the stop's join
    time.sleep(0.4)
    assert counting.passes == 1 and counting.max_active == 1
    counting.hold.set()
    stopper.join(timeout=10)
    assert old is not None
    wait_until(lambda: not old.is_alive(), what="the old reaper ended")
    wait_until(lambda: counting.passes >= 3, what="the new reaper runs")
    assert counting.max_active == 1 and len(reapers(d)) == 1


def test_reaper_outlives_a_base_exception_and_shows_on_counts(rig_desk) -> None:
    d, _ = rig_desk
    calls: list[int] = []

    def pass_that_dies_once() -> dict[str, int]:
        calls.append(1)
        if len(calls) == 1:
            raise SystemExit(1)  # not an Exception: the loop must survive it
        return {}

    d.reap_once = pass_that_dies_once  # type: ignore[method-assign]
    assert d._counts(None).body["reaper_running"] is False and d._counts(None).body["last_reaper_pass_at"] is None
    d.start_background(0.05)
    wait_until(lambda: len(calls) >= 3, what="passes after the one that died")
    body = d._counts(None).body
    assert body["reaper_running"] is True and isinstance(body["last_reaper_pass_at"], int)
    d.stop_background()
    assert d._counts(None).body["reaper_running"] is False
