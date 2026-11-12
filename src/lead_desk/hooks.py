"""Crash hooks. The base class does nothing in normal runs."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Final

from lead_desk.constants import EXIT_SIMULATED_KILL

# The five points the portal demo lets a visitor arm: A, A2, B (receipt and verdict), D1 (follow-up claimed,
# before the send mark), D2 (outbox.send returned, result not yet recorded).
HOOK_POINTS: Final = ("A", "A2", "B", "D1", "D2")
# More points for the crash script: L (the AI label is stored), C0 (the CRM intent mark 2 is written,
# crm.put not called yet), C1 (the CRM put came back, crm_done=1 not yet
# written), D1b (the send mark send_started=1 is written, outbox.send not yet called), D3 (the result is recorded),
# N1 (a reminder email was sent, its result not yet recorded).
MORE_HOOK_POINTS: Final = ("L", "C0", "C1", "D1b", "D3", "N1")
ALL_HOOK_POINTS: Final = (*HOOK_POINTS, *MORE_HOOK_POINTS)


class Hooks:
    def crash_point(self, name: str, conn: sqlite3.Connection | None) -> None:
        """No-op. Subclasses override it; names are ALL_HOOK_POINTS."""
        return None


class FlagFileHooks(Hooks):
    """The ONE shared crash hook for subprocess tests and the crash script. Reads the one-shot flag file `flag_path`
    (content = a point name from ALL_HOOK_POINTS; the path comes from the launcher, never from here). When
    crash_point(name, conn) is called and the file names `name`: delete the file, print "simulate crash" and call
    os._exit(137). Otherwise do nothing."""

    def __init__(self, flag_path: Path) -> None:
        self.flag_path = Path(flag_path)

    def crash_point(self, name: str, conn: sqlite3.Connection | None) -> None:
        try:
            wanted = self.flag_path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if wanted != name:
            return
        try:
            self.flag_path.unlink()
        except OSError:
            return  # another thread took the one-shot flag first
        print("simulate crash", flush=True)
        os._exit(EXIT_SIMULATED_KILL)
