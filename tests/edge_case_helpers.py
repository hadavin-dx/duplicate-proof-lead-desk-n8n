"""Helpers shared by the edge-case test files. Made-up data only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from demo_only.fakes import FakeCrm, KeyedOutbox, RecordedQualifier
from lead_desk.config import Config
from lead_desk.desk import Desk, default_now

# One place for the statement text that records the result of a send; the tests that make this write fail match on it.
RESULT_WRITE = "SET state=?, last_error=?"


def mk_desk(
    tmp: Path, outbox: Any = None, crm: Any = None, now: Any = default_now, hooks: Any = None, **cfg: Any
) -> Desk:
    """A Desk on tmp with in-process fakes and the real clock (not started)."""
    tmp.mkdir(parents=True, exist_ok=True)
    replies = tmp / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    config = Config(db_path=tmp / "desk.sqlite3", port=0, replies_path=replies, **cfg)
    return Desk(
        config,
        crm if crm is not None else FakeCrm(),
        outbox if outbox is not None else KeyedOutbox(),
        RecordedQualifier(replies),
        hooks=hooks,
        now=now,
    )
