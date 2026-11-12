"""A legacy lead book that lacks a trigger of schema.sql (for example followups_no_delete) is repaired when it is
opened. The receipts CHECK is the one thing that is not: SQLite cannot add a CHECK to an existing table."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from edge_case_helpers import mk_desk
from support import lead
from test_desk_http_edge_cases import make_book_at_schema_version_1

from lead_desk import db

TRIGGERS = ("receipts_immutable", "receipts_outcome_once", "receipts_no_delete", "leads_no_update", "leads_no_delete",
            "followups_sent_final", "later_decisions_no_update", "later_decisions_no_delete",
            "lead_state_no_delete", "lead_state_owner_immutable", "lead_state_no_regress", "nudges_no_delete",
            "nudges_sent_final", "followups_no_delete")  # fmt: skip


def _names(path: Path, kind: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}
    finally:
        con.close()


def test_the_trigger_list_is_every_trigger_of_schema_sql() -> None:
    """The ensure list in db.py cannot fall behind schema.sql."""
    declared = set(re.findall(r"CREATE TRIGGER IF NOT EXISTS (\w+)", db._schema_text()))
    assert declared == set(TRIGGERS)
    assert declared <= set(db._VIEW_AND_INDEXES)


def _book_with_every_column_but_one_trigger(tmp_path: Path) -> Path:
    """Every column, the view and the indexes are there: only the trigger is missing, so a migration that only
    looks at columns, the view and indexes does nothing and leaves the book unprotected."""
    d = mk_desk(tmp_path)
    d.startup()
    assert d.process("form", "legacy-0", lead(email="legacy0@example.test")).status == 201
    d.close()
    return tmp_path / "desk.sqlite3"


@pytest.mark.parametrize("legacy_columns", [False, True], ids=["current-columns", "legacy-columns-too"])
@pytest.mark.parametrize("missing", ["followups_no_delete", *TRIGGERS[:-1]])
def test_book_at_schema_version_1_without_a_trigger_is_repaired_and_then_refuses_deletes(
    tmp_path: Path, missing: str, legacy_columns: bool
) -> None:
    path = (
        make_book_at_schema_version_1(tmp_path, leads=1)
        if legacy_columns
        else _book_with_every_column_but_one_trigger(tmp_path)
    )
    con = sqlite3.connect(path, isolation_level=None)
    # IF EXISTS: on a legacy book the helper has already dropped the new tables and with them their triggers
    con.execute(f"DROP TRIGGER IF EXISTS {missing}")  # noqa: S608
    con.close()
    assert missing not in _names(path, "trigger")
    d = mk_desk(tmp_path)
    d.startup()  # opens the book with the current code: init_schema migrates it
    d.close()
    assert set(TRIGGERS) <= _names(path, "trigger")
    conn = db.connect(path)  # the desk's own pragmas: foreign_keys, recursive_triggers
    try:
        row = conn.execute("SELECT lead_id, state, attempts, updated_at FROM followups").fetchone()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM followups WHERE lead_id=?", (row[0],))
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "INSERT OR REPLACE INTO followups(lead_id, state, attempts, updated_at) VALUES(?, 'pending', 0, 1)",
                (row[0],),
            )
        assert conn.execute("SELECT count(*) FROM followups").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_book_that_has_every_trigger_is_not_rewritten(tmp_path: Path) -> None:
    d = mk_desk(tmp_path)
    d.startup()
    d.close()
    conn = db.connect(tmp_path / "desk.sqlite3")
    try:
        assert db._book_current(conn)
    finally:
        conn.close()
