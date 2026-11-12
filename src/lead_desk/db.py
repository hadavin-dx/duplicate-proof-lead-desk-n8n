"""Database connection factory and schema loader."""

from __future__ import annotations

import importlib.resources
import sqlite3
from pathlib import Path

from lead_desk.constants import BUSY_WAIT_SECONDS, SCHEMA_VERSION, SQLITE_BUSY, SQLITE_LOCKED

LOW_BYTE = 0xFF  # sqlite_errorcode & 0xFF is the primary result code (extended codes such as 261 or 517 count)

# A lead book kept from a previous run is brought up to this schema at start-up: it gets any of these columns it lacks.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("receipts", "payload_conflict", "ALTER TABLE receipts ADD COLUMN payload_conflict INTEGER NOT NULL DEFAULT 0"),
    ("followups", "crm_done", "ALTER TABLE followups ADD COLUMN crm_done INTEGER NOT NULL DEFAULT 0"),
    ("followups", "auto_attempts", "ALTER TABLE followups ADD COLUMN auto_attempts INTEGER NOT NULL DEFAULT 0"),
    ("followups", "ambiguous_prior", "ALTER TABLE followups ADD COLUMN ambiguous_prior INTEGER NOT NULL DEFAULT 0"),
    ("followups", "send_started", "ALTER TABLE followups ADD COLUMN send_started INTEGER NOT NULL DEFAULT 0"),
)
# Run right after the column is added (same transaction). A sent row is final (a trigger refuses any update), so the
# backfills leave it alone.
_BACKFILL: dict[str, str] = {
    # A kept book may hold rows where the CRM was asked only after the claim (on a Retry): "tried" does not prove
    # "asked".
    # `sending`: unknowable -> 2 (a put may have started; the safe path decides). A row that failed or waits with an
    # error that came from the send (refused, unreachable, ambiguous, crash): the CRM came first -> 1. An error that
    # provably happened before the CRM put (the CRM stored nothing, or the AI step's input was bad): stays 0. Any other
    # desk-side error (including an unclear CRM put) and any other tried row: unknowable -> 2 (a person decides).
    "crm_done": "UPDATE followups SET crm_done = CASE"
    " WHEN state = 'sending' THEN 2"
    " WHEN last_error IN ('refused', 'unreachable', 'ambiguous', 'crash_while_sending') THEN 1"
    " WHEN last_error IN ('internal:CrmNothingStored', 'internal:KeyError', 'internal:ValueError',"
    " 'internal:LookupError') THEN 0"
    " WHEN COALESCE(last_error, '') LIKE 'internal:%' THEN 2"
    " WHEN state = 'failed' AND ai_label IS NOT NULL THEN 1"
    " ELSE 2 END"
    " WHERE state <> 'sent' AND (attempts > 0 OR state = 'sending')",
    # a keyed row waiting after a crash while sending is an "earlier send may have left" row
    "ambiguous_prior": "UPDATE followups SET ambiguous_prior=1"
    " WHERE state = 'pending' AND last_error = 'crash_while_sending'",
    # a `sending` row whose CRM step was finished may have sent, so it counts as started (the safe side); the mark is
    # cleared by every claim, so no other row needs it
    "send_started": "UPDATE followups SET send_started = 1 WHERE state = 'sending' AND crm_done = 1",
}
_VIEW_AND_INDEXES = (
    "review_items",
    "receipts_open_ix",
    "receipts_time_ix",
    "followups_active_ix",
    "leads_email_key_uq",
    # every trigger of schema.sql: a book that lacks one (for example followups_no_delete) is repaired too.
    # The receipts CHECK is NOT repaired: SQLite cannot add a CHECK to an existing table, so it exists only in
    # freshly created books. The receipts triggers guard the immutable fields, the write-once verdict (only once an
    # outcome is set) and deletes, not the CHECK's rule that a `new` verdict names its lead.
    "receipts_immutable",
    "receipts_outcome_once",
    "receipts_no_delete",
    "leads_no_update",
    "leads_no_delete",
    "followups_sent_final",
    "later_decisions_no_update",
    "later_decisions_no_delete",
    "lead_state_no_delete",
    "lead_state_owner_immutable",
    "lead_state_no_regress",
    "nudges_no_delete",
    "nudges_sent_final",
    "followups_no_delete",
)
_MIGRATE_TRIES = 3


def _is_memory(path: Path | str) -> bool:
    return str(path) in ("", ":memory:")


def _require_wal(conn: sqlite3.Connection, memory_ok: bool) -> None:
    """The write-ahead log lets readers and the one writer work together. SQLite answers a refused switch with the
    old mode instead of an error (for example on a file system that cannot share memory), and the desk would then run
    in rollback-journal mode with busy storms. So read the answer and refuse unless it is 'wal'. A private :memory:
    database cannot use WAL and is exempt (memory_ok)."""
    row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = str(row[0]).lower() if row else ""
    if mode != "wal":
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()  # only a connection that finds another mode writes
        mode = str(row[0]).lower() if row else ""
    if mode != "wal" and not (memory_ok and mode == "memory"):
        raise RuntimeError(f"database is not in WAL mode (journal_mode={mode or 'unknown'})")


def connect(path: Path | str) -> sqlite3.Connection:
    """One connection per request thread: isolation_level=None, timeout=BUSY_WAIT_SECONDS, foreign_keys=ON,
    recursive_triggers=ON, synchronous=FULL. Raises RuntimeError unless the file is in WAL mode."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_WAIT_SECONDS)
    try:
        _require_wal(conn, _is_memory(path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute("PRAGMA synchronous=FULL")
    except BaseException:
        conn.close()
        raise
    return conn


def is_busy(exc: BaseException) -> bool:
    """True for 'database is locked' after the wait: OperationalError with primary code 5 or 6, or its message."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & LOW_BYTE) in (SQLITE_BUSY, SQLITE_LOCKED):
        return True
    return "database is locked" in str(exc)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def _schema_text() -> str:
    return importlib.resources.files("lead_desk").joinpath("schema.sql").read_text("utf-8")


def _book_current(conn: sqlite3.Connection) -> bool:
    """True when every added column, the review view, the indexes and the triggers are there."""
    if not all(c in _columns(conn, t) for t, c, _ in _ADDED_COLUMNS):
        return False
    have = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('view','index','trigger')")}
    return all(n in have for n in _VIEW_AND_INDEXES)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a user_version 1 book up to date in ONE transaction: the missing columns (and their backfills), then
    schema.sql again (every statement of it is IF NOT EXISTS, and the view is dropped and made again). A crash or an
    error anywhere rolls the whole thing back, so the book is never left with columns but no view. A book that has the
    columns but lost the view or an index (a partly applied upgrade) is repaired the same way. If another process
    adds a column between the check and the lock, the script fails on the duplicate and is tried again."""
    for attempt in range(_MIGRATE_TRIES):
        if _book_current(conn):
            return
        missing = [(t, c, d) for t, c, d in _ADDED_COLUMNS if c not in _columns(conn, t)]
        steps = [d for _, _, d in missing] + [_BACKFILL[c] for _, c, _ in missing if c in _BACKFILL]
        script = "BEGIN IMMEDIATE;\n" + "".join(f"{st};\n" for st in steps)
        script += "DROP VIEW IF EXISTS review_items;\n" + _schema_text() + "\nCOMMIT;"
        try:
            conn.executescript(script)
            return
        except sqlite3.OperationalError as e:
            if conn.in_transaction:
                conn.rollback()
            if "duplicate column" not in str(e) or attempt == _MIGRATE_TRIES - 1:
                raise
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


def init_schema(conn: sqlite3.Connection, sql: str | None = None) -> None:
    """Refuse user_version other than 0 or 1; WAL first (the answer is checked); a user_version 1 book gets any
    missing column added; else run schema.sql.

    executescript leaves its transaction open when the script fails after BEGIN IMMEDIATE, so the real body is:
    try: conn.executescript(BEGIN IMMEDIATE; sql; PRAGMA user_version=1; COMMIT)
    except BaseException: conn.rollback() (guarded by conn.in_transaction); raise.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, SCHEMA_VERSION):
        raise RuntimeError(f"unsupported schema version {version}")
    memory = conn.execute("PRAGMA database_list").fetchone()
    _require_wal(conn, memory is None or not str(memory[2]))
    if version == SCHEMA_VERSION:
        if sql is None:
            _migrate(conn)
        return
    text = sql if sql is not None else _schema_text()
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{text}\nPRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
