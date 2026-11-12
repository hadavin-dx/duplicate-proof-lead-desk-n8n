"""Trace and extra crash windows, as wrappers around the adapters.

`Tracer` writes one JSON line per event to a file: {"ev", "id", "stage", "t_start_ms", "t_end_ms", "outcome", "pid",
"thread"}. Times are wall-clock milliseconds since the epoch (the lead book itself keeps whole seconds only).
* TracedQualifier / TracedCrm / TracedOutbox wrap an adapter and time each call (stage "ai", "crm", "send"); the lead
  id (the send key for the outbox) is the id. Other attributes (`capability`, `mode`, `calls`) are passed through.
* TraceHooks is a Hooks subclass: each crash_point name becomes a zero-length event (stage "hook:<name>").
* CrashWindows adds crash points the desk has no hook for, as one-shot flag-file windows that end the process with
  os._exit(137) (the same effect as FlagFileHooks; on Windows os._exit is a clean exit of the process, not kill -9,
  so open files are not flushed by the OS the way a power cut would). Windows covered:
    ai_after       after Qualifier.qualify returned, before the label is stored
    crm_before_put after the desk wrote the intent mark (crm_done=2), before Crm.put is called
    crm_after_put  after Crm.put returned, before the desk records crm_done=1
    send_before    before Outbox.send is called (a second window next to hook D1)
    send_after     after Outbox.send returned, before the desk records it (next to D2)
  The flag file holds one window name; the first thread that deletes it ends the process.
A trace line that cannot be written is counted (`Tracer.write_failures`) and dropped; it never changes the result of
the traced call.
Not covered: a crash inside the model call, inside the mail protocol exchange, or in the database write itself."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from lead_desk.constants import EXIT_SIMULATED_KILL
from lead_desk.hooks import Hooks

WINDOWS = ("ai_after", "crm_before_put", "crm_after_put", "send_before", "send_after")


class Tracer:
    """Append-only JSON lines; one write per event, so lines of different threads do not mix."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.write_failures = 0  # events that could not be written; the traced call is never changed by it

    def event(self, ev_id: str, stage: str, t_start_ms: int, t_end_ms: int, outcome: str = "ok") -> None:
        row = {
            "id": ev_id,
            "stage": stage,
            "t_start_ms": t_start_ms,
            "t_end_ms": t_end_ms,
            "outcome": outcome,
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
        }
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception:  # noqa: BLE001 (a full disk or a locked file must not turn a good call into a failed one)
            with self._lock:
                self.write_failures += 1


def _now_ms() -> int:
    return time.time_ns() // 1_000_000  # wall-clock: calendar stamp


class _Traced:
    def __init__(self, inner: Any, tracer: Tracer) -> None:
        self._inner = inner
        self._tracer = tracer

    def __getattr__(self, name: str) -> Any:  # capability, mode, calls, ...
        return getattr(self._inner, name)

    def _timed(self, ev_id: str, stage: str, fn: Any, *args: Any) -> Any:
        t0 = _now_ms()
        outcome = "ok"
        try:
            return fn(*args)
        except BaseException as e:  # SimulatedCrash is a BaseException: it must be recorded too
            outcome = type(e).__name__
            raise
        finally:
            self._tracer.event(ev_id, stage, t0, _now_ms(), outcome)


class TracedQualifier(_Traced):
    def qualify(self, lead_id: str, email_key: str) -> Literal["hot", "warm", "cold"]:
        return self._timed(lead_id, "ai", self._inner.qualify, lead_id, email_key)  # type: ignore[no-any-return]


class TracedCrm(_Traced):
    def put(self, lead_id: str, record: Mapping[str, str]) -> Literal["PUT", "ALREADY_PRESENT"]:
        return self._timed(lead_id, "crm", self._inner.put, lead_id, record)  # type: ignore[no-any-return]

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        self._timed(lead_id, "crm", self._inner.set_fields, lead_id, email_key, crm_owner_id, crm_status)


class TracedOutbox(_Traced):
    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Literal["SENT", "ALREADY_SENT"]:
        return self._timed(send_key, "send", self._inner.send, send_key, to, subject, body, timeout)  # type: ignore[no-any-return]


class TraceHooks(Hooks):
    """Logs every crash point the desk reaches; wraps another Hooks (for example FlagFileHooks) so both work."""

    def __init__(self, tracer: Tracer, inner: Hooks | None = None) -> None:
        self._tracer = tracer
        self._inner = inner or Hooks()

    def crash_point(self, name: str, conn: sqlite3.Connection | None) -> None:
        t = _now_ms()
        self._tracer.event("-", f"hook:{name}", t, t)
        self._inner.crash_point(name, conn)


class CrashWindows:
    """The flag-file one-shot crash windows (see the module text). Wrap the adapters with `qualifier()`, `crm()`
    and `outbox()` and give the same flag path to every wrapper."""

    def __init__(self, flag_path: Path) -> None:
        self.flag_path = Path(flag_path)

    def hit(self, window: str) -> None:
        if window not in WINDOWS:
            raise ValueError(window)
        try:
            wanted = self.flag_path.read_text(encoding="utf-8").strip()
            if wanted != window:
                return
            self.flag_path.unlink()
        except OSError:
            return  # no flag, or another thread took the one-shot flag first
        print("simulate crash", window, flush=True)
        os._exit(EXIT_SIMULATED_KILL)

    def qualifier(self, inner: Any) -> Any:
        return _WindowQualifier(inner, self)

    def crm(self, inner: Any) -> Any:
        return _WindowCrm(inner, self)

    def outbox(self, inner: Any) -> Any:
        return _WindowOutbox(inner, self)


class _Window:
    def __init__(self, inner: Any, w: CrashWindows) -> None:
        self._inner = inner
        self._w = w

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _WindowQualifier(_Window):
    def qualify(self, lead_id: str, email_key: str) -> Literal["hot", "warm", "cold"]:
        label = self._inner.qualify(lead_id, email_key)
        self._w.hit("ai_after")
        return label  # type: ignore[no-any-return]


class _WindowCrm(_Window):
    def put(self, lead_id: str, record: Mapping[str, str]) -> Literal["PUT", "ALREADY_PRESENT"]:
        self._w.hit("crm_before_put")
        out = self._inner.put(lead_id, record)
        self._w.hit("crm_after_put")
        return out  # type: ignore[no-any-return]


class _WindowOutbox(_Window):
    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Literal["SENT", "ALREADY_SENT"]:
        self._w.hit("send_before")
        out = self._inner.send(send_key, to, subject, body, timeout)
        self._w.hit("send_after")
        return out  # type: ignore[no-any-return]
