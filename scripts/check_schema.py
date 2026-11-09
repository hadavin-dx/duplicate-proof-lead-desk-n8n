"""Self-check: load schema.sql into sqlite3 (stdlib) and fire every trigger and constraint once.

Run: python scripts/check_schema.py   (prints one line per check, exits 1 if any check fails)
Made-up data only. Not a test suite; the tests are in tests/.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "src" / "lead_desk" / "schema.sql"
UNIQUE_STATEMENT = "CREATE UNIQUE INDEX IF NOT EXISTS leads_email_key_uq ON leads(email_key);"
FAILED: list[str] = []


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_schema(conn: sqlite3.Connection, sql: str) -> None:
    # same order as schema.sql
    conn.execute("PRAGMA journal_mode=WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] == 1:
        return
    conn.executescript("BEGIN IMMEDIATE;\n" + sql + "\nPRAGMA user_version=1;\nCOMMIT;")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (" -> " + detail if detail else ""))
    if not ok:
        FAILED.append(name)


def raises(conn: sqlite3.Connection, sql: str, args: tuple = (), text: str = "") -> tuple[bool, str]:
    try:
        conn.execute(sql, args)
    except sqlite3.DatabaseError as exc:
        return (text in str(exc)), f"{type(exc).__name__}: {exc}"
    return False, "no error raised"


def expect_error(name: str, conn: sqlite3.Connection, sql: str, args: tuple = (), text: str = "") -> None:
    ok, detail = raises(conn, sql, args, text)
    check(name, ok, detail)


def add_receipt(conn: sqlite3.Connection, ref: str, source: str = "form") -> int:
    conn.execute(
        "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES (?,?,?,?)",
        (source, ref, 1000, '{"email":"ada@example.test","name":"Ada Example"}'),
    )
    return int(conn.execute("SELECT receipt_id FROM receipts WHERE source_ref=?", (ref,)).fetchone()[0])


def add_lead(conn: sqlite3.Connection, lead_id: str, receipt_id: int, key: str) -> None:
    conn.execute(
        "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES (?,?,?,?,?,?,?)",
        (lead_id, receipt_id, "form", "Ada Example", key, key.lower(), 1000),
    )


def main() -> int:
    sql = SCHEMA.read_text(encoding="utf-8")
    check("the unique index statement is present exactly once", sql.count(UNIQUE_STATEMENT) == 1)
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(str(Path(tmp) / "a.sqlite3"))
        init_schema(conn, sql)
        check("schema loads, user_version=1", conn.execute("PRAGMA user_version").fetchone()[0] == 1)
        check("journal_mode is wal", conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
        init_schema(conn, sql)
        check("second init_schema is a no-op", conn.execute("PRAGMA user_version").fetchone()[0] == 1)
        before = conn.execute("SELECT count(*), group_concat(name) FROM sqlite_master").fetchone()
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")  # IF NOT EXISTS: re-running is harmless
            rerun_error = ""
        except sqlite3.DatabaseError as exc:
            rerun_error = f"{type(exc).__name__}: {exc}"
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        after = conn.execute("SELECT count(*), group_concat(name) FROM sqlite_master").fetchone()
        check(
            "schema text can be re-run (IF NOT EXISTS): no error, same objects",
            rerun_error == "" and before == after,
            rerun_error or f"{before[0]} objects before and after",
        )
        # lead_id is a TEXT PRIMARY KEY: SQLite allows NULL there unless NOT NULL is stated
        conn.execute("BEGIN")  # rolled back below so later checks see the same receipts as before
        conn.execute(
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form','null-lead',1,'{}')"
        )
        nr = int(conn.execute("SELECT receipt_id FROM receipts WHERE source_ref='null-lead'").fetchone()[0])
        expect_error(
            "leads: NULL lead_id refused",
            conn,
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) "
            "VALUES (NULL, ?, 'form', 'Ada Example', 'n@example.test', 'n@example.test', 1)",
            (nr,),
            "NOT NULL",
        )
        expect_error(
            "followups: NULL lead_id refused",
            conn,
            "INSERT INTO followups(lead_id, state, attempts, updated_at) VALUES (NULL, 'pending', 0, 1)",
            (),
            "NOT NULL",
        )
        conn.execute("ROLLBACK")

        # receipts
        r1 = add_receipt(conn, "sub-0001")
        expect_error(
            "receipts: bad source rejected",
            conn,
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('fax','x',1,'{}')",
            (),
            "CHECK",
        )
        expect_error(
            "receipts: empty source_ref rejected",
            conn,
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form','',1,'{}')",
            (),
            "CHECK",
        )
        expect_error(
            "receipts: 129-char source_ref rejected",
            conn,
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form',?,1,'{}')",
            ("x" * 129,),
            "CHECK",
        )
        expect_error(
            "receipts: UNIQUE(source, source_ref)",
            conn,
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form','sub-0001',1,'{}')",
            (),
            "UNIQUE",
        )
        conn.execute(
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form','sub-0001',2000,'{}') "
            "ON CONFLICT(source, source_ref) DO UPDATE SET attempts = attempts + 1"
        )
        row = conn.execute("SELECT attempts, payload FROM receipts WHERE receipt_id=?", (r1,)).fetchone()
        check(
            "receipts: upsert bumps attempts, receipts_immutable NOT fired, payload untouched",
            row[0] == 2 and row[1].startswith('{"email"'),
            str(row),
        )
        expect_error(
            "receipts: update payload aborts",
            conn,
            "UPDATE receipts SET payload='{}' WHERE receipt_id=?",
            (r1,),
            "never change",
        )
        expect_error(
            "receipts: update source_ref aborts",
            conn,
            "UPDATE receipts SET source_ref='z' WHERE receipt_id=?",
            (r1,),
            "never change",
        )
        expect_error(
            "receipts: update received_at aborts",
            conn,
            "UPDATE receipts SET received_at=1 WHERE receipt_id=?",
            (r1,),
            "never change",
        )
        expect_error("receipts: delete aborts", conn, "DELETE FROM receipts WHERE receipt_id=?", (r1,), "never deleted")
        expect_error(
            "receipts: INSERT OR REPLACE aborts (delete trigger fires with recursive_triggers=ON)",
            conn,
            "INSERT OR REPLACE INTO receipts(receipt_id, source, source_ref, received_at, payload) VALUES (?,?,?,?,?)",
            (r1, "form", "sub-0001", 1, "{}"),
            "never deleted",
        )
        expect_error(
            "receipts: bad outcome value rejected",
            conn,
            "UPDATE receipts SET outcome='failed' WHERE receipt_id=?",
            (r1,),
            "CHECK",
        )
        expect_error(
            "receipts: a `new` verdict without its lead id rejected (no partial verdict)",
            conn,
            "UPDATE receipts SET outcome='new', outcome_lead_id=NULL, decided_at=3000 WHERE receipt_id=?",
            (r1,),
            "CHECK",
        )
        conn.execute(
            "UPDATE receipts SET outcome='new', outcome_lead_id='L000000000001', outcome_reason=NULL, "
            "differing_fields=NULL, decided_at=3000 WHERE receipt_id=?",
            (r1,),
        )
        check(
            "receipts: first verdict write (one UPDATE, five columns) passes",
            conn.execute("SELECT outcome FROM receipts WHERE receipt_id=?", (r1,)).fetchone()[0] == "new",
        )
        expect_error(
            "receipts: second verdict write aborts",
            conn,
            "UPDATE receipts SET outcome='duplicate' WHERE receipt_id=?",
            (r1,),
            "written once",
        )
        conn.execute(
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form','sub-0001',9,'{}') "
            "ON CONFLICT(source, source_ref) DO UPDATE SET attempts = attempts + 1"
        )
        check(
            "receipts: attempts can still be bumped after a verdict",
            conn.execute("SELECT attempts FROM receipts WHERE receipt_id=?", (r1,)).fetchone()[0] == 3,
        )

        # leads
        add_lead(conn, "L000000000001", r1, "Ada@Example.test")
        r2 = add_receipt(conn, "sub-0002", "webhook")
        expect_error(
            "leads: unique email_key (second lead, same key)",
            conn,
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES ('L000000000002',?,'webhook','Ada','ada@example.test','ada@example.test',1)",
            (r2,),
            "UNIQUE",
        )
        expect_error(
            "leads: one lead per receipt (UNIQUE receipt_id)",
            conn,
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES ('L000000000003',?,'form','Bo','bo@example.test','bo@example.test',1)",
            (r1,),
            "UNIQUE",
        )
        expect_error(
            "leads: foreign key to receipts enforced",
            conn,
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES ('L000000000004',999,'form','Bo','bo@example.test','bo@example.test',1)",
            (),
            "FOREIGN KEY",
        )
        expect_error(
            "leads: empty name rejected",
            conn,
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES ('L000000000005',?,'form','','c@example.test','c@example.test',1)",
            (r2,),
            "CHECK",
        )
        expect_error(
            "leads: 201-char name rejected",
            conn,
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES ('L000000000005',?,'form',?,'c@example.test','c@example.test',1)",
            (r2, "n" * 201),
            "CHECK",
        )
        expect_error(
            "leads: email_key shorter than 3 rejected",
            conn,
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES ('L000000000005',?,'form','Cy','a@','a@',1)",
            (r2,),
            "CHECK",
        )
        expect_error(
            "leads: update aborts", conn, "UPDATE leads SET name='X' WHERE lead_id='L000000000001'", (), "write-once"
        )
        expect_error("leads: delete aborts", conn, "DELETE FROM leads WHERE lead_id='L000000000001'", (), "write-once")
        expect_error(
            "leads: INSERT OR REPLACE aborts",
            conn,
            "INSERT OR REPLACE INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) VALUES ('L000000000001',?,'form','Z','z@example.test','z@example.test',1)",
            (r1,),
            "write-once",
        )

        # followups
        conn.execute("INSERT INTO followups(lead_id, state, updated_at) VALUES ('L000000000001','pending',1000)")
        expect_error(
            "followups: bad state rejected",
            conn,
            "UPDATE followups SET state='done' WHERE lead_id='L000000000001'",
            (),
            "CHECK",
        )
        expect_error(
            "followups: delete aborts",
            conn,
            "DELETE FROM followups WHERE lead_id='L000000000001'",
            (),
            "never deleted",
        )
        expect_error(
            "followups: INSERT OR REPLACE aborts (delete trigger fires with recursive_triggers=ON)",
            conn,
            "INSERT OR REPLACE INTO followups(lead_id, state, updated_at) VALUES ('L000000000001','pending',9)",
            (),
            "never deleted",
        )
        expect_error(
            "followups: bad ai_label rejected",
            conn,
            "UPDATE followups SET ai_label='lukewarm' WHERE lead_id='L000000000001'",
            (),
            "CHECK",
        )
        conn.execute("UPDATE followups SET ai_label='warm' WHERE lead_id='L000000000001' AND ai_label IS NULL")
        n = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute("UPDATE followups SET ai_label='hot' WHERE lead_id='L000000000001' AND ai_label IS NULL")
        n2 = conn.execute("SELECT changes()").fetchone()[0]
        check(
            "followups: ai_label written once with WHERE ai_label IS NULL (1 row, then 0)",
            (n, n2) == (1, 0),
            str((n, n2)),
        )
        conn.execute(
            "UPDATE followups SET state='sending', attempts=attempts+1 WHERE lead_id='L000000000001' AND state IN ('pending','failed')"
        )
        c1 = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute(
            "UPDATE followups SET state='sending', attempts=attempts+1 WHERE lead_id='L000000000001' AND state IN ('pending','failed')"
        )
        c2 = conn.execute("SELECT changes()").fetchone()[0]
        check("followups: claim changes exactly one row, the second claim changes 0", (c1, c2) == (1, 0), str((c1, c2)))
        conn.execute("UPDATE followups SET state='sent', sent_at=5 WHERE lead_id='L000000000001' AND state='sending'")
        expect_error(
            "followups: sent is final (state)",
            conn,
            "UPDATE followups SET state='failed' WHERE lead_id='L000000000001'",
            (),
            "final",
        )
        expect_error(
            "followups: sent is final (any column)",
            conn,
            "UPDATE followups SET last_error='x' WHERE lead_id='L000000000001'",
            (),
            "final",
        )

        # later_decisions (approval mode): one row per later message, never changed, never deleted
        conn.execute(
            "INSERT INTO later_decisions(receipt_id, decision, approver_id, approver_name, channel, decided_at) "
            "VALUES (?,?,?,?,?,?)",
            (r1, "approved", "appr-01", "Approver One", "portal", 5),
        )
        expect_error(
            "later_decisions: update aborts",
            conn,
            "UPDATE later_decisions SET decision='denied' WHERE receipt_id=?",
            (r1,),
            "never changed",
        )
        expect_error(
            "later_decisions: delete aborts and INSERT OR REPLACE aborts (delete trigger, recursive_triggers=ON)",
            conn,
            "INSERT OR REPLACE INTO later_decisions(receipt_id, decision, approver_id, approver_name, channel, "
            "decided_at) VALUES (?,?,?,?,?,?)",
            (r1, "denied", "appr-02", "Approver Two", "portal", 6),
            "never deleted",
        )
        conn.execute(
            "INSERT INTO later_decisions(receipt_id, decision, approver_id, approver_name, channel, decided_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(receipt_id) DO NOTHING",
            (r1, "denied", "appr-02", "Approver Two", "portal", 6),
        )
        check(
            "later_decisions: a second decision for the same receipt writes nothing (the first wins)",
            conn.execute("SELECT changes()").fetchone()[0] == 0
            and conn.execute("SELECT decision FROM later_decisions WHERE receipt_id=?", (r1,)).fetchone()[0]
            == "approved",
        )

        # lead_state (lead owner and lead stage): rank never goes down, owner never changes, never deleted
        conn.execute(
            "INSERT INTO lead_state(lead_id, owner_id, owner_name, assigned_at, step, step_rank, changed_at) "
            "VALUES ('L000000000001','o-1','Asha',1,'new',0,1)"
        )
        expect_error(
            "lead_state: negative step_rank rejected",
            conn,
            "INSERT INTO lead_state(lead_id, owner_id, owner_name, assigned_at, step, step_rank, changed_at) "
            "VALUES ('L000000000001','o-1','Asha',1,'new',-1,1)",
            (),
            "CHECK",
        )
        expect_error(
            "lead_state: crm_owner_done 2 rejected",
            conn,
            "UPDATE lead_state SET crm_owner_done=2 WHERE lead_id='L000000000001'",
            (),
            "CHECK",
        )
        expect_error(
            "lead_state: owner never changes",
            conn,
            "UPDATE lead_state SET owner_id='o-2' WHERE lead_id='L000000000001'",
            (),
            "never changes",
        )
        conn.execute(
            "UPDATE lead_state SET step='contacted', step_rank=1 WHERE lead_id='L000000000001' AND step_rank < 1"
        )
        check("lead_state: a step moves forward", conn.execute("SELECT changes()").fetchone()[0] == 1)
        expect_error(
            "lead_state: a step never moves back",
            conn,
            "UPDATE lead_state SET step='new', step_rank=0 WHERE lead_id='L000000000001'",
            (),
            "only moves forward",
        )
        expect_error(
            "lead_state: the same rank with another step id aborts",
            conn,
            "UPDATE lead_state SET step='other' WHERE lead_id='L000000000001'",
            (),
            "only moves forward",
        )
        conn.execute("UPDATE lead_state SET crm_tries=crm_tries+1, crm_error='CrmError' WHERE lead_id='L000000000001'")
        check("lead_state: the crm_* columns can be updated", conn.execute("SELECT changes()").fetchone()[0] == 1)
        expect_error(
            "lead_state: delete aborts",
            conn,
            "DELETE FROM lead_state WHERE lead_id='L000000000001'",
            (),
            "never deleted",
        )
        expect_error(
            "lead_state: INSERT OR REPLACE aborts (delete trigger fires with recursive_triggers=ON)",
            conn,
            "INSERT OR REPLACE INTO lead_state(lead_id, owner_id, owner_name, assigned_at, step, step_rank, changed_at) "
            "VALUES ('L000000000001','o-2','Ravi',1,'new',0,1)",
            (),
            "never deleted",
        )

        # nudges (the one reminder of a lead): one row per lead, sent is final, never deleted
        conn.execute("INSERT INTO nudges(lead_id, state, updated_at) VALUES ('L000000000001','sending',10)")
        expect_error(
            "nudges: a second row for the same lead aborts",
            conn,
            "INSERT INTO nudges(lead_id, state, updated_at) VALUES ('L000000000001','pending',11)",
            (),
            "UNIQUE",
        )
        expect_error(
            "nudges: bad state rejected",
            conn,
            "UPDATE nudges SET state='done' WHERE lead_id='L000000000001'",
            (),
            "CHECK",
        )
        expect_error(
            "nudges: handled 2 rejected",
            conn,
            "UPDATE nudges SET handled=2 WHERE lead_id='L000000000001'",
            (),
            "CHECK",
        )
        expect_error(
            "nudges: delete aborts",
            conn,
            "DELETE FROM nudges WHERE lead_id='L000000000001'",
            (),
            "never deleted",
        )
        expect_error(
            "nudges: INSERT OR REPLACE aborts (delete trigger fires with recursive_triggers=ON)",
            conn,
            "INSERT OR REPLACE INTO nudges(lead_id, state, updated_at) VALUES ('L000000000001','pending',9)",
            (),
            "never deleted",
        )
        conn.execute("UPDATE nudges SET state='sent', sent_at=12 WHERE lead_id='L000000000001' AND state='sending'")
        expect_error(
            "nudges: sent is final (state)",
            conn,
            "UPDATE nudges SET state='failed' WHERE lead_id='L000000000001'",
            (),
            "final",
        )
        expect_error(
            "nudges: sent is final (any column)",
            conn,
            "UPDATE nudges SET handled=1 WHERE lead_id='L000000000001'",
            (),
            "final",
        )

        # review view
        r3 = add_receipt(conn, "sub-0003")
        conn.execute(
            "UPDATE receipts SET outcome='review', outcome_reason='incomplete:email', decided_at=1 WHERE receipt_id=?",
            (r3,),
        )
        r4 = add_receipt(conn, "sub-0004")
        conn.execute(
            "UPDATE receipts SET outcome='duplicate', outcome_lead_id='L000000000001', differing_fields='[\"phone\"]', decided_at=1 WHERE receipt_id=?",
            (r4,),
        )
        r5 = add_receipt(conn, "sub-0005")
        conn.execute(
            "UPDATE receipts SET outcome='duplicate', outcome_lead_id='L000000000001', differing_fields='[]', decided_at=1 WHERE receipt_id=?",
            (r5,),
        )
        r6 = add_receipt(conn, "sub-0006")  # open, received_at=1000 -> stalled
        add_lead(conn, "L000000000006", r6, "stuck@example.test")
        conn.execute("INSERT INTO followups(lead_id, state, updated_at) VALUES ('L000000000006','unknown',1000)")
        conn.execute("INSERT INTO nudges(lead_id, state, updated_at) VALUES ('L000000000006','unknown',1000)")
        items = {r[0]: r for r in conn.execute("SELECT * FROM review_items")}
        check(
            "view review_items: R, D, F(unknown) and N(unknown) rows; clean duplicate, sent follow-up and sent reminder absent",
            set(items) == {f"R{r3}", f"D{r4}", "FL000000000006", "NL000000000006"},
            str(sorted(items)),
        )
        check(
            "view review_items: the reminder row has kind reminder_to_check and the nudge state as reason",
            items["NL000000000006"][1:4] == ("reminder", "reminder_to_check", "unknown"),
            str(items["NL000000000006"]),
        )
        conn.execute("UPDATE nudges SET handled=1 WHERE lead_id='L000000000006'")
        check(
            "view review_items: a reminder marked seen leaves the list",
            "NL000000000006" not in {r[0] for r in conn.execute("SELECT item_id FROM review_items")},
        )
        conn.execute("UPDATE nudges SET handled=0 WHERE lead_id='L000000000006'")
        check("view review_items has 7 columns", len(next(iter(items.values()))) == 7)
        stalled = conn.execute(
            "SELECT 'O' || receipt_id, 'receipt', 'stalled', 'open receipt', NULL, received_at, source "
            "FROM receipts WHERE outcome IS NULL AND received_at < :before "
            "UNION ALL "
            "SELECT 'F' || f.lead_id, 'followup', 'stalled', f.state, f.lead_id, f.updated_at, l.source "
            "FROM followups f JOIN leads l USING (lead_id) WHERE f.state IN ('pending','sending') AND f.updated_at < :before",
            {"before": 5000},
        ).fetchall()
        check(
            "stalled branches run with a bound :before (open receipts r2, r6; no stalled follow-up)",
            sorted(s[0] for s in stalled) == sorted([f"O{r2}", f"O{r6}"]),
            str(stalled),
        )
        conn.close()

        # without the unique index statement, two leads may share an email_key
        mutant = sql.replace(UNIQUE_STATEMENT, "")
        m = connect(str(Path(tmp) / "m.sqlite3"))
        init_schema(m, mutant)
        ra, rb = add_receipt(m, "m-1"), add_receipt(m, "m-2")
        add_lead(m, "L00000000000a", ra, "dup@example.test")
        add_lead(m, "L00000000000b", rb, "dup@example.test")
        check(
            "without the unique index two leads share one email_key",
            m.execute("SELECT count(*) FROM leads").fetchone()[0] == 2,
        )
        check(
            "the unique index is absent in the mutant",
            m.execute("SELECT count(*) FROM sqlite_master WHERE name='leads_email_key_uq'").fetchone()[0] == 0,
        )
        m.close()

    print("RESULT", "ALL PASS" if not FAILED else "FAILED: " + ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
