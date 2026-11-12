"""Fair write gate, in-flight limit and the background-repair constants (standard library only).

Why: SQLite's own busy wait is not fair (a waiting writer sleeps and polls, so a late writer can win again and again).
With many simultaneous requests some writers can starve until the 5 s wait runs out. The gate puts every writer of
this process in one first-come-first-served line BEFORE it asks SQLite for the write lock, so inside one desk process
SQLite itself is never contended. Another process (a second desk is refused by the lock file; a tool holding the file)
still gets the ordinary SQLite busy wait."""

from __future__ import annotations

import collections
import threading
from pathlib import Path
from typing import Final

from lead_desk.constants import ADAPTER_WORKER_CAPS, BUSY_WAIT_SECONDS

# -- limits (names, not settings; one place)
INFLIGHT_MAX: Final = 32  # Desk.process calls running at once; more wait, then get 503 busy (no receipt written)
INFLIGHT_WAIT_SECONDS: Final = BUSY_WAIT_SECONDS  # how long a request waits for a place before 503 busy
DRIVE_MAX: Final = 8  # follow-up drives running at once (they have their own places: they cannot crowd out requests)
DRIVE_WAIT_SECONDS: Final = BUSY_WAIT_SECONDS  # a drive that finds no place in this time is left to the reaper
GATE_WAIT_SECONDS: Final = BUSY_WAIT_SECONDS  # how long a writer waits in the gate line before "database is locked"
WRITE_DEADLINE_SECONDS: Final = GATE_WAIT_SECONDS + BUSY_WAIT_SECONDS  # all tries of ONE write together (one deadline)
REQUEST_DEADLINE_SECONDS: Final = 3 * BUSY_WAIT_SECONDS  # one request, its inflight wait and all its writes together

# -- background reaper
REAPER_INTERVAL_SECONDS: Final = 1.0  # pause between two reaper passes
# Real seconds one pass may spend STARTING drives (checked between drives, never inside one: a drive that has started
# runs to its end). A pass can therefore overrun: by one drive (at most three bounded adapter calls of CALL_DEADLINE
# seconds or MODEL_CALL_BUDGET_SECONDS, plus its database writes) and by the two capability reads at its start (two
# deadlines). With the defaults and no lock trouble: 10 + 27 + 18 = 55 s at the very worst. stop_background waits only
# its own timeout (10 s) and then lets the reaper end by itself at its next check; the desk tracks it until it has
# ended (reaper_running, start_background).
REAPER_PASS_SECONDS: Final = 10.0
# The CRM copy part and the reminder part of a pass each have their own clock, so a follow-up backlog never starves them
# (checked between calls, like the drives).
MIRROR_PASS_SECONDS: Final = 2.5
NUDGE_PASS_SECONDS: Final = 2.5
DECIDE_FAIL_LIMIT: Final = 3  # consecutive non-busy decision failures of one receipt, then it gets verdict review
# a `sending` row is stuck after send timeout + this many busy waits. The result write after a send is bounded by
# WRITE_DEADLINE_SECONDS plus one more SQLite busy wait (a lock held by another process): 3 busy waits. The grace is one
# more than that, so a live drive that is still writing its result is never taken for a stuck one. The AI step needs
# room too: its answer time (MODEL_CALL_BUDGET_SECONDS, enforced by the desk) plus the write of the stamp that follows
# (WRITE_DEADLINE_SECONDS plus one busy wait, MODEL_GRACE_MARGIN_SECONDS) must stay below the grace (constants.py).
SENDING_GRACE_BUSY_WAITS: Final = 5
PENDING_GRACE_SECONDS: Final = 2  # a fresh `pending` row is left to the request that made it for this long

# -- automatic retry of a try that failed in a way that can be tried again (see Desk._send_claimed / _after_claim)
SEND_MAX_ATTEMPTS: Final = 5  # tries in total (the first drive counts), then the follow-up becomes `failed`
OUTBOX_REFRESH_SECONDS: Final = 30  # the reaper reads the sender's mode at most this often
SEND_BACKOFF_SECONDS: Final = (1, 2, 4, 8)  # pause after try 1, 2, 3, 4 (the last value repeats if the cap is raised)


def backoff_after(attempt: int) -> int:
    """Seconds to wait after the given try (1-based) before the next automatic try."""
    return SEND_BACKOFF_SECONDS[min(max(attempt, 1), len(SEND_BACKOFF_SECONDS)) - 1]


NO_DESCRIPTORS_ERROR: Final = "no_descriptors"  # last_error of a follow-up given back because no file handle was left
NO_DESCRIPTORS_BACKOFF_SECONDS: Final = 5  # such a row waits this long before the next drive (the try count stays)
MODEL_BUSY_ERROR: Final = "model_busy"  # last_error of a follow-up given back because too many AI calls are running
CRM_BUSY_ERROR: Final = "crm_busy"  # ... too many CRM puts are running (abandoned ones included)
SEND_BUSY_ERROR: Final = "send_busy"  # ... too many sends are running (abandoned ones included)
BUSY_ERRORS: Final = (MODEL_BUSY_ERROR, CRM_BUSY_ERROR, SEND_BUSY_ERROR)
MODEL_BUSY_BACKOFF_SECONDS: Final = 5  # such a row waits this long before the next drive (the try count stays)
_CAPS = dict(ADAPTER_WORKER_CAPS)
# Workers of one kind that may exist at once, running or abandoned (the slot is reserved before the worker starts, so
# this is the exact bound). The model cap is below DRIVE_MAX on purpose; validate_worker_caps keeps the CRM and sender
# caps at DRIVE_MAX or more (constants.py).
MODEL_HUNG_MAX: Final = _CAPS["model"]
CRM_WORKERS_MAX: Final = _CAPS["crm"]
SEND_WORKERS_MAX: Final = _CAPS["send"]
REFRESH_WORKERS_MAX: Final = _CAPS["refresh"]


def wait_before_drive(auto_attempts: int, last_error: str | None) -> int:
    """Seconds a `pending` follow-up waits (from its updated_at) before an automatic drive may start it. A row that was
    given back for want of file handles keeps its try count, so it would be due again after PENDING_GRACE_SECONDS on
    every reaper pass for as long as the shortage lasts; it waits NO_DESCRIPTORS_BACKOFF_SECONDS instead (and
    MODEL_BUSY_BACKOFF_SECONDS after a give-back for too many running adapter calls)."""
    if last_error == NO_DESCRIPTORS_ERROR:
        return NO_DESCRIPTORS_BACKOFF_SECONDS
    if last_error in BUSY_ERRORS:
        return MODEL_BUSY_BACKOFF_SECONDS
    return PENDING_GRACE_SECONDS if auto_attempts == 0 else backoff_after(auto_attempts)


class GateTimeout(Exception):
    """The gate line did not move within the wait."""


class FairGate:
    """FIFO ticket lock: waiters are served strictly in arrival order. Not re-entrant. Two lines: a writer that asks
    with low=True (a follow-up drive, the reaper) is served only when no normal writer (a request) is waiting."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._held = False
        self._waiters: collections.deque[threading.Event] = collections.deque()
        self._low: collections.deque[threading.Event] = collections.deque()

    def acquire(self, timeout: float, low: bool = False) -> None:
        with self._mutex:
            if not self._held and not self._waiters and not self._low:
                self._held = True
                return
            ticket = threading.Event()
            line = self._low if low else self._waiters
            line.append(ticket)
        try:
            if ticket.wait(max(timeout, 0.0)):
                return  # the releaser handed the gate over: _held stays True
        except BaseException:
            self._leave(ticket, line)  # an interrupt inside the wait must not leave the ticket in the line
            raise
        if self._leave(ticket, line):
            return
        raise GateTimeout

    def _leave(self, ticket: threading.Event, line: collections.deque[threading.Event]) -> bool:
        """Take the ticket out of its line. True when the gate was handed to it just before (it now holds the gate)."""
        with self._mutex:
            if ticket.is_set():
                return True
            line.remove(ticket)
            return False

    def release(self) -> None:
        with self._mutex:
            if self._waiters:
                self._waiters.popleft().set()  # hand over directly: no barging by a late arrival
            elif self._low:
                self._low.popleft().set()
            else:
                self._held = False


class FairSemaphore:
    """Counting semaphore with first-come-first-served hand-off (threading.BoundedSemaphore lets a late arrival take a
    freed place ahead of old waiters, so old waiters starve until their wait runs out). acquire(timeout=) -> bool."""

    def __init__(self, places: int) -> None:
        self._mutex = threading.Lock()
        self._free = places
        self._max = places
        self._waiters: collections.deque[threading.Event] = collections.deque()

    def acquire(self, timeout: float | None = None) -> bool:
        with self._mutex:
            if self._free > 0 and not self._waiters:
                self._free -= 1
                return True
            ticket = threading.Event()
            self._waiters.append(ticket)
        try:
            if ticket.wait(timeout):
                return True  # the releaser handed its place over
        except BaseException:
            self._leave(ticket)
            raise
        return self._leave(ticket)

    def _leave(self, ticket: threading.Event) -> bool:
        with self._mutex:
            if ticket.is_set():
                return True
            self._waiters.remove(ticket)
            return False

    def release(self) -> None:
        with self._mutex:
            if self._waiters:
                self._waiters.popleft().set()
            elif self._free < self._max:
                self._free += 1
            else:
                raise ValueError("released too many times")


_GATES: dict[str, FairGate] = {}
_GATES_LOCK = threading.Lock()


def gate_for(db_path: Path | str) -> FairGate:
    """The write gate of one lead book: every Desk on the same resolved path shares one line, desks on different
    books do not wait for each other. A private in-memory book (empty path or ':memory:') gets its own gate each time.
    The gate is NOT re-entrant: code holding it must never ask for it again (Desk._write is never nested)."""
    text = str(db_path)
    if text in ("", ":memory:"):
        return FairGate()
    key = str(Path(text).resolve())
    with _GATES_LOCK:
        found = _GATES.get(key)
        if found is None:
            found = _GATES[key] = FairGate()
        return found
