"""Approval mode for later messages (DESK_LATER_MODE=approval), desk side: one decision per later message, the first
writer wins, the decision changes nothing else. Made-up data only (example.test, neutral approver names)."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from edge_case_helpers import mk_desk
from support import call, lead, run_threads
from test_desk_http_edge_cases import hold_write_lock

from demo_only.fakes import FakeCrm, KeyedOutbox
from lead_desk import db, loader, portal_api, server
from lead_desk import desk as desk_mod
from lead_desk.config import Config, read_approvers
from lead_desk.constants import APPROVER_NAME_MAX
from lead_desk.desk import DECISION_KEYS, DeskSettingRefused
from lead_desk.envnum import SettingRefused

APPROVERS = (("appr-01", "Approver One (test)"), ("appr-02", "Approver Two (test)"))
HINT_D = desk_mod.HINTS["D"]
SIX = ("item_id", *DECISION_KEYS)
T = 1_700_000_000
HALF_CLOSE_PAUSE_SECONDS = 0.25  # test hook: holds the server thread after the half-close
DRAIN_WAIT_SECONDS = 5.0  # upper bound for the server thread to reach _drain after the client saw the whole reply


def approval_rig(env: Any, **over: Any) -> Any:
    rig = env.rig(later_mode="approval", approvers=APPROVERS, **over)
    rig.clock.t = T
    return rig


def make_later(rig: Any, ref: str = "m1", **over: Any) -> int:
    """A first lead, then a later message with another phone number: returns the receipt number of the later one."""
    if not rig.query("SELECT 1 FROM leads"):
        assert rig.desk.process("form", "first-" + ref, lead(email="ada@example.test")).status == 201
    reply = rig.desk.process("webhook", ref, lead(email="ada@example.test", phone="555 0199", **over))
    assert reply.status == 200 and reply.body["verdict"] == "duplicate" and reply.body["differing_fields"] == ["phone"]
    return int(reply.body["receipt_id"])


def decisions(rig: Any) -> list[tuple[Any, ...]]:
    return rig.query(
        "SELECT receipt_id, decision, approver_id, approver_name, channel, decided_at FROM later_decisions"
        " ORDER BY receipt_id"
    )


def test_mode_off_d_item_unchanged(env) -> None:
    rig = env.rig()  # default: off
    rid = make_later(rig)
    for word in ("approve:appr-01", "sent", "bogus", "deny:appr-01"):
        r = rig.desk.resolve(f"D{rid}", word)
        assert (r.status, r.body) == (409, {"error": "not_actionable", "hint": HINT_D}), word
    assert decisions(rig) == []  # the table exists, nothing in it
    assert rig.config.later_mode == "off" and rig.desk._approvers == {}


def test_approve_writes_one_row(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    r = rig.desk.resolve(f"D{rid}", "approve:appr-01")
    assert r.status == 200
    assert r.body == {
        "item_id": f"D{rid}",
        "decision": "approved",
        "decided_by": "appr-01",
        "decided_by_name": "Approver One (test)",
        "channel": "portal",
        "decided_at": T,
    }
    assert decisions(rig) == [(rid, "approved", "appr-01", "Approver One (test)", "portal", T)]


def test_deny_writes_one_row(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    r = rig.desk.resolve(f"D{rid}", "deny:appr-02")
    assert (r.status, r.body["decision"], r.body["decided_by_name"]) == (200, "denied", "Approver Two (test)")
    assert decisions(rig) == [(rid, "denied", "appr-02", "Approver Two (test)", "portal", T)]


def test_check_order_404_409_400_403(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    # a same-number replay with other content: a D item of kind replay_payload_differs, never decidable
    first = rig.desk.process("form", "solo", lead(email="solo@example.test")).body["receipt_id"]
    rig.desk.process("form", "solo", lead(email="solo@example.test", name="Different Name"))
    desk = rig.desk
    # 404: unknown / malformed ids come before everything, also with a bad word
    for item in ("D99999", "X1", "D", "D0" + str(rid), "Dabc"):
        assert desk.resolve(item, "bogus") == desk_mod.Reply(404, {"error": "not_found"}), item
    # 409 not_actionable: an R item, an O item, a replay-only D item; the word is not looked at
    rev = desk.process("form", "rv", lead(email=None)).body["receipt_id"]
    for item in (f"R{rev}", f"D{first}"):
        for word in ("approve:appr-01", "bogus", "approve:nobody"):
            r = desk.resolve(item, word)
            assert r.status == 409 and r.body["error"] == "not_actionable", (item, word)
    # 400: a word that does not fullmatch (checked before the approver is looked up)
    bad = ["sent", "approve", "approve:", "Approve:appr-01", "approve:appr-01 ", "approve:appr-01:x", "accept:appr-01",
           "approve:" + "a" * 33, "approve:appr-0١", "approve:Appr-01", "approve:-x", "", " approve:appr-01",
           "approve:appr-01\n"]  # fmt: skip
    for word in bad:
        assert desk.resolve(f"D{rid}", word) == desk_mod.Reply(400, {"error": "bad_decision"}), repr(word)
    # 403: a well-formed word with an id that is not on the list (also an id that only differs by a name)
    for word in ("approve:appr-09", "deny:nobody", "approve:" + "a" * 32):
        assert desk.resolve(f"D{rid}", word) == desk_mod.Reply(403, {"error": "not_an_approver"}), word
    assert decisions(rig) == []  # not one of these wrote anything
    assert desk.resolve(f"D{rid}", "approve:appr-01").status == 200  # and the item was decidable all along


def test_first_writer_wins_and_409_names_who_where_when(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    rig.clock.t = T + 60
    r = rig.desk.resolve(f"D{rid}", "deny:appr-02")
    assert r.status == 409
    assert r.body == {
        "error": "already_decided",
        "item_id": f"D{rid}",
        "decision": "approved",
        "decided_by": "appr-01",
        "decided_by_name": "Approver One (test)",
        "channel": "portal",
        "decided_at": T,
    }
    assert decisions(rig) == [(rid, "approved", "appr-01", "Approver One (test)", "portal", T)]


def test_decided_item_409_never_takes_the_write_gate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """An already decided item answers 409 from a plain read: no BEGIN IMMEDIATE, no write-gate turn."""
    rig = approval_rig(env)
    rid = make_later(rig)
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    taken: list[int] = []
    real = rig.desk._write

    def counting(conn: Any) -> Any:
        taken.append(1)
        return real(conn)

    monkeypatch.setattr(rig.desk, "_write", counting)
    for word in ("approve:appr-02", "deny:appr-01"):
        r = rig.desk.resolve(f"D{rid}", word)
        assert (r.status, r.body["error"], r.body["decided_by"]) == (409, "already_decided", "appr-01")
    assert taken == []


def test_other_approver_same_word_is_409(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    r = rig.desk.resolve(f"D{rid}", "approve:appr-02")
    assert (r.status, r.body["error"], r.body["decided_by"]) == (409, "already_decided", "appr-01")
    assert len(decisions(rig)) == 1


def test_same_approver_repeat_is_409(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    for word in ("approve:appr-01", "deny:appr-01"):
        r = rig.desk.resolve(f"D{rid}", word)
        assert (r.status, r.body["error"], r.body["decision"]) == (409, "already_decided", "approved"), word
    assert len(decisions(rig)) == 1


def test_unknown_approver_is_403(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    r = rig.desk.resolve(f"D{rid}", "approve:appr-77")
    assert (r.status, r.body) == (403, {"error": "not_an_approver"})
    assert decisions(rig) == []
    # the name is never taken from the caller: a name in the word is just an id that is not on the list
    assert rig.desk.resolve(f"D{rid}", "approve:approver-one-test").status == 403


def test_ten_racing_decisions_one_row(env) -> None:
    """Proves the logic through the write gate (one writer at a time, first writer wins), not cross-connection SQLite
    locking; the rowcount of a second INSERT is pinned by test_second_insert_changes_zero_rows."""
    for round_ in range(20):
        rig = approval_rig(env)
        rid = make_later(rig, f"race{round_}")
        words = [("approve:appr-01", "deny:appr-02", "approve:appr-02", "deny:appr-01")[i % 4] for i in range(10)]
        out = run_threads([lambda w=w: rig.desk.resolve(f"D{rid}", w) for w in words])
        assert all(isinstance(r, desk_mod.Reply) for r in out), out
        ok = [r for r in out if r.status == 200]
        lost = [r for r in out if r.status == 409]
        assert (len(ok), len(lost)) == (1, 9), [r.status for r in out]
        stored = decisions(rig)
        assert len(stored) == 1
        win = ok[0].body
        assert (win["decision"], win["decided_by"]) == (stored[0][1], stored[0][2])
        for r in lost:  # every loser names the winner, not itself
            assert r.body["error"] == "already_decided"
            assert {k: r.body[k] for k in SIX} == {k: win[k] for k in SIX}


def test_second_insert_changes_zero_rows(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    conn = db.connect(rig.db_path)
    try:
        args = (rid, "approved", "appr-01", "Approver One (test)", T)
        assert conn.execute(desk_mod._DECIDE, args).rowcount == 1
        assert conn.execute(desk_mod._DECIDE, (rid, "denied", "appr-02", "Approver Two (test)", T + 1)).rowcount == 0
        assert conn.execute("SELECT decision FROM later_decisions").fetchall() == [("approved",)]
    finally:
        conn.close()


def test_decision_row_append_only(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    before = decisions(rig)
    conn = db.connect(rig.db_path)  # recursive_triggers is ON on this connection, as on the desk's own
    try:
        row = (rid, "denied", "appr-02", "Approver Two (test)", "portal", 1)
        for sql, args in (
            ("UPDATE later_decisions SET decision='denied'", ()),
            ("UPDATE later_decisions SET approver_name='Someone Else'", ()),
            ("DELETE FROM later_decisions", ()),
            ("INSERT OR REPLACE INTO later_decisions VALUES (?,?,?,?,?,?)", row),
        ):
            with pytest.raises(sqlite3.DatabaseError, match="never"):
                conn.execute(sql, args)
    finally:
        conn.close()
    assert decisions(rig) == before


def test_decision_changes_nothing_and_sends_nothing(env) -> None:
    rig = approval_rig(env)
    first = rig.desk.process("form", "base", lead(email="ada@example.test")).body["lead_id"]
    rid = make_later(rig)
    rig.desk.run_followup(first)  # the follow-up goes out before the decision, so the counts are not zero
    tables = ("leads", "receipts", "followups")

    def snapshot() -> dict[str, list[tuple[Any, ...]]]:
        return {t: rig.query(f"SELECT * FROM {t} ORDER BY 1") for t in tables}  # noqa: S608 (fixed names)

    before = (snapshot(), rig.outbox.count, rig.crm.count)
    assert before[1] >= 1 and before[2] >= 1
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    assert rig.desk.resolve(f"D{rid}", "deny:appr-02").status == 409
    assert (snapshot(), rig.outbox.count, rig.crm.count) == before


def test_kept_book_gets_the_decision_table(env, tmp_path: Path) -> None:
    """A book made before the table existed (the shipped schema text minus the new block, passed as schema_sql) is
    brought up to date when a desk on the shipped schema starts: table and triggers exist, rows are untouched."""
    sql = db._schema_text()
    old_sql, n = re.subn(r"-- approval mode.*?later_decisions_no_delete.*?\n(?=\n)", "", sql, flags=re.DOTALL)
    assert n == 1 and "later_decisions" not in old_sql
    rig = env.rig(schema_sql=old_sql, later_mode="approval", approvers=APPROVERS)
    rid = make_later(rig)
    names = {r[0] for r in rig.query("SELECT name FROM sqlite_master")}
    assert "later_decisions" not in names and "later_decisions_no_update" not in names
    leads_before = rig.query("SELECT * FROM leads")
    receipts_before = rig.query("SELECT * FROM receipts")
    rig.restart()  # a new Desk with schema_sql=None: init_schema migrates the kept book
    names = {r[0] for r in rig.query("SELECT name FROM sqlite_master")}
    assert {"later_decisions", "later_decisions_no_update", "later_decisions_no_delete"} <= names
    assert rig.query("PRAGMA user_version") == [(1,)]
    assert (rig.query("SELECT * FROM leads"), rig.query("SELECT * FROM receipts")) == (leads_before, receipts_before)
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200  # and a decision works on the kept book


def test_old_book_trigger_repair_both_new_triggers(env) -> None:
    """A kept book that lost both new triggers gets them back at the next start, with the stored decision kept, and
    after the repair an UPDATE and a DELETE of that row are refused again."""
    rig = approval_rig(env)
    rid = make_later(rig)
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    before = decisions(rig)
    con = sqlite3.connect(rig.db_path, isolation_level=None)
    con.execute("DROP TRIGGER later_decisions_no_update")
    con.execute("DROP TRIGGER later_decisions_no_delete")
    con.close()
    rig.restart()  # init_schema repairs the book
    names = {r[0] for r in rig.query("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"later_decisions_no_update", "later_decisions_no_delete"} <= names
    assert decisions(rig) == before
    conn = db.connect(rig.db_path)
    try:
        for sql in ("UPDATE later_decisions SET decision='denied'", "DELETE FROM later_decisions"):
            with pytest.raises(sqlite3.DatabaseError, match="never"):
                conn.execute(sql)
    finally:
        conn.close()
    assert decisions(rig) == before


def _http(env: Any, rig: Any) -> tuple[str, int]:
    base = env.serve(rig)
    return base, int(base.rsplit(":", 1)[1])


def test_foreign_origin_decision_is_403(env) -> None:
    from test_portal_api import EVIL, raw  # the same raw-socket helper the other gate tests use

    rig = approval_rig(env)
    rid = make_later(rig)
    base, port = _http(env, rig)
    host = f"127.0.0.1:{port}"
    body = json.dumps({"decision": "approve:appr-01"}).encode()
    for origin in (f"http://{EVIL}", f"http://{EVIL}:{port}", "null", f"https://{host}"):
        got = raw(
            base,
            f"POST /review/D{rid}/resolve HTTP/1.1",
            f"Host: {host}",
            f"Origin: {origin}",
            f"Content-Length: {len(body)}",
            body=body,
        )
        assert got == (403, {"error": "forbidden_origin"}), origin
    assert decisions(rig) == []
    ok = raw(
        base,
        f"POST /review/D{rid}/resolve HTTP/1.1",
        f"Host: {host}",
        f"Origin: http://{host}",
        f"Content-Length: {len(body)}",
        body=body,
    )
    assert ok[0] == 200 and len(decisions(rig)) == 1


def _post_decision(base: str, rid: int, *headers: str) -> tuple[int, Any]:
    from test_portal_api import raw

    body = json.dumps({"decision": "approve:appr-01"}).encode()
    return raw(base, f"POST /review/D{rid}/resolve HTTP/1.1", *headers, f"Content-Length: {len(body)}", body=body)


def test_cross_site_fetch_decision_is_403(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    base, port = _http(env, rig)
    for site in ("cross-site", "same-site", "bogus"):
        got = _post_decision(base, rid, f"Host: 127.0.0.1:{port}", f"Sec-Fetch-Site: {site}")
        assert got == (403, {"error": "forbidden_origin"}), site
    assert decisions(rig) == []
    assert _post_decision(base, rid, f"Host: 127.0.0.1:{port}", "Sec-Fetch-Site: same-origin")[0] == 200


def test_wrong_host_decision_is_403(env) -> None:
    from test_portal_api import EVIL

    rig = approval_rig(env)
    rid = make_later(rig)
    base, port = _http(env, rig)
    for host in (f"{EVIL}:{port}", EVIL):
        assert _post_decision(base, rid, f"Host: {host}") == (403, {"error": "forbidden_host"}), host
    assert decisions(rig) == []


def test_decision_waits_in_the_write_gate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The decision insert runs inside Desk._write (the one write line): while another writer holds the gate the
    call answers 503 busy and writes nothing."""
    rig = approval_rig(env)
    rid = make_later(rig)
    monkeypatch.setattr(desk_mod, "GATE_WAIT_SECONDS", 0.3)
    rig.desk._gate.acquire(1)
    try:
        r = rig.desk.resolve(f"D{rid}", "approve:appr-01")
    finally:
        rig.desk._gate.release()
    assert (r.status, r.body) == (503, {"error": "busy"})
    assert decisions(rig) == []


def test_readonly_portal_cannot_write_decisions(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    conn = portal_api.connect_readonly(rig.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(desk_mod._DECIDE, (rid, "approved", "appr-01", "Approver One (test)", T))
        assert conn.execute("SELECT count(*) FROM later_decisions").fetchone()[0] == 0  # it can read the table
    finally:
        conn.close()


def test_decision_busy_is_503(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    release = hold_write_lock(rig.db_path, 60)
    try:
        r = rig.desk.resolve(f"D{rid}", "approve:appr-01")
        assert (r.status, r.body) == (503, {"error": "busy"})
    finally:
        release()
    assert decisions(rig) == []
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200


def test_approver_settings_are_checked(tmp_path: Path) -> None:
    ok = (("appr-01", "Approver One (test)"),)
    assert Config(later_mode="approval", approvers=ok).approvers == ok
    bad_mode = ["on", "Approval", "", "approval "]
    for mode in bad_mode:
        with pytest.raises(SettingRefused, match="DESK_LATER_MODE"):
            Config(later_mode=mode)
    ids = ["Appr-01", "-x", "a" * 33, "", "a b", "appr_01", "appr-0١"]
    for bad_id in ids:
        with pytest.raises(SettingRefused, match="approver 1"):
            Config(later_mode="approval", approvers=((bad_id, "Name"),))
    lone = chr(0xD800)  # a lone surrogate: not a control character, but it cannot be written as UTF-8
    names = ["", "x" * (APPROVER_NAME_MAX + 1), "tab\there", "line\nbreak", "nul\x00", "bad" + lone]
    for name in names:
        with pytest.raises(SettingRefused, match="approver 1"):
            Config(later_mode="approval", approvers=(("appr-01", name),))
    for bad_label in ("a" * (APPROVER_NAME_MAX + 1), "two\nlines", "bad" + lone):
        with pytest.raises(SettingRefused, match="label"):
            Config(later_mode="approval", approvers=ok, approvers_label=bad_label)
    with pytest.raises(SettingRefused, match="approver 2"):  # duplicate id
        Config(later_mode="approval", approvers=(("appr-01", "One"), ("appr-01", "Two")))
    with pytest.raises(SettingRefused, match="approver 2"):  # duplicate name
        Config(later_mode="approval", approvers=(("appr-01", "One"), ("appr-02", "One")))
    Config(later_mode="approval", approvers=(("appr-01", "x" * APPROVER_NAME_MAX),))  # the longest name passes
    # mode off with a file path is refused (one place: Config); from_env too
    with pytest.raises(SettingRefused, match="DESK_APPROVERS_FILE"):
        Config(approvers_path=tmp_path / "a.json")
    with pytest.raises(SettingRefused, match="DESK_APPROVERS_FILE"):
        Config.from_env({"DESK_APPROVERS_FILE": str(tmp_path / "a.json")})
    assert Config.from_env({"DESK_LATER_MODE": "approval"}).later_mode == "approval"
    # the Desk refuses approval mode with nobody to approve
    with pytest.raises(DeskSettingRefused, match="at least one approver"):
        mk_desk(tmp_path, later_mode="approval")
    # files
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"label": "L", "approvers": [{"id": "appr-01", "name": "One"}]}), encoding="utf-8")
    assert read_approvers(f) == ((("appr-01", "One"),), "L")
    f.write_text(json.dumps({"approvers": [{"id": "appr-01", "name": "One"}]}), encoding="utf-8")
    assert read_approvers(f) == ((("appr-01", "One"),), "")
    for text in ("not json", "[]", "{}", '{"approvers": {}}', '{"approvers": [1]}', '{"approvers": [{"id": "a"}]}',
                 '{"approvers": [{"id": 1, "name": "x"}]}', '{"label": 3, "approvers": []}', "\xff"):  # fmt: skip
        f.write_bytes(text.encode("latin-1"))
        with pytest.raises(SettingRefused, match="approvers? "):
            read_approvers(f)
    f.write_bytes(b" " * 16385)
    with pytest.raises(SettingRefused, match="too big"):
        read_approvers(f)
    with pytest.raises(SettingRefused, match="cannot be read"):
        read_approvers(tmp_path / "missing.json")
    # an empty list in the file is read, then refused by the Desk (approval mode needs at least one)
    f.write_text('{"approvers": []}', encoding="utf-8")
    assert read_approvers(f) == ((), "")


def test_loader_supplies_demo_approvers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    people, label = loader.load_approvers(Config(later_mode="approval"))
    assert [p[0] for p in people] == ["appr-01", "appr-02", "appr-03"] and label == "demo approver"
    Config(later_mode="approval", approvers=people, approvers_label=label)  # the file passes the config's own check
    f = tmp_path / "mine.json"
    f.write_text(json.dumps({"approvers": [{"id": "me-1", "name": "Me One"}]}), encoding="utf-8")
    assert loader.load_approvers(Config(later_mode="approval", approvers_path=f)) == ((("me-1", "Me One"),), "")
    # the demo folder is removable: a missing folder is a plain refusal, not a traceback
    import importlib

    real = importlib.import_module

    def gone(name: str, *a: Any, **k: Any) -> Any:
        if name == "demo_only.fakes":
            raise ModuleNotFoundError("No module named 'demo_only'", name="demo_only")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", gone)
    with pytest.raises(SettingRefused, match="approvers file"):
        loader.load_approvers(Config(later_mode="approval"))


def test_main_loads_approvers_and_refuses_bad_ones(env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def capture(config: Config, *a: Any, **k: Any) -> None:
        seen["config"] = config
        raise Stop

    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "m1.sqlite3"))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setenv("DESK_LATER_MODE", "approval")
    rig = env.rig()
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (FakeCrm(), KeyedOutbox(), rig.qualifier))
    monkeypatch.setattr(server, "Desk", capture)
    with pytest.raises(Stop):
        server.main()
    assert [a[0] for a in seen["config"].approvers] == ["appr-01", "appr-02", "appr-03"]
    assert seen["config"].approvers_label == "demo approver"
    # a bad file: exit 4 with the setting named, before any desk is built
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "m2.sqlite3"))
    monkeypatch.setenv("DESK_APPROVERS_FILE", str(bad))
    seen.clear()
    with pytest.raises(SystemExit) as e:
        server.main()
    assert e.value.code == 4 and "config" not in seen
    # a file while the mode is off: exit 4 too
    monkeypatch.setenv("DESK_LATER_MODE", "off")
    with pytest.raises(SystemExit) as e2:
        server.main()
    assert e2.value.code == 4 and "config" not in seen


def test_decision_over_http(env) -> None:
    rig = approval_rig(env)
    rid = make_later(rig)
    base, _ = _http(env, rig)
    path = f"/review/D{rid}/resolve"
    status, body = call(base, "POST", path, {"decision": "approve:appr-02"})
    assert status == 200 and set(body) == set(SIX) and body["decided_by_name"] == "Approver Two (test)"
    status2, body2 = call(base, "POST", path, {"decision": "deny:appr-01"})
    assert status2 == 409 and body2 == {"error": "already_decided", **body}
    assert call(base, "POST", path, {"decision": 5})[0] == 400  # not a string: the server answers before any lookup
    assert call(base, "POST", path, {"decision": "approve:zz"})[0] == 403  # the approver check comes before the write
    rid2 = make_later(rig, "m2")
    assert call(base, "POST", f"/review/D{rid2}/resolve", {"decision": "approve:zz"}) == (
        403,
        {"error": "not_an_approver"},
    )
    assert call(base, "POST", f"/review/D{rid2}/resolve", {"decision": "approve"}) == (400, {"error": "bad_decision"})
    assert call(base, "POST", "/review/D999999/resolve", {"decision": "approve:appr-01"}) == (
        404,
        {"error": "not_found"},
    )
    # retry on a D item stays 409 not_actionable with today's hint, in both modes
    assert call(base, "POST", f"/review/D{rid2}/retry") == (409, {"error": "not_actionable", "hint": HINT_D})


def test_decision_logs_no_values(env, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    caplog.set_level("DEBUG")
    rig = approval_rig(env)
    canary = {"phone": "555 0142", "message": "canary" + " message text", "company": "Canary" + " Works Ltd"}
    rig.desk.process("form", "c-1", lead(email="canary.mail@example.test", **canary))
    reply = rig.desk.process(
        "webhook", "c-2", lead(email="canary.mail@example.test", phone="555 0143", message="second canary message")
    )
    rid = reply.body["receipt_id"]
    assert rig.desk.resolve(f"D{rid}", "approve:appr-01").status == 200
    assert rig.desk.resolve(f"D{rid}", "deny:appr-02").status == 409
    captured = capfd.readouterr()  # read ONCE: a second read would come back empty
    seen = caplog.text + captured.out + captured.err
    for secret in ("0142", "0143", "canary message", "canary.mail", "Canary Works"):
        assert secret not in seen, secret


def test_decision_limits_match_schema() -> None:
    """The 32 and 64 written in schema.sql are the numbers of constants.py (one source of truth, checked here)."""
    from lead_desk import constants

    sql = db._schema_text()
    assert f"BETWEEN 1 AND {constants.APPROVER_ID_MAX})" in sql.split("approver_id", 1)[1].split("\n", 1)[0]
    assert f"BETWEEN 1 AND {constants.APPROVER_NAME_MAX})" in sql.split("approver_name", 1)[1].split("\n", 1)[0]
    assert re.fullmatch(constants.APPROVER_ID_PATTERN, "a" * constants.APPROVER_ID_MAX)
    assert not re.fullmatch(constants.APPROVER_ID_PATTERN, "a" * (constants.APPROVER_ID_MAX + 1))


# -- the decision body has the same size cap as POST /leads (size_cap_bytes), checked before the body is read
def _padded_decision(size: int) -> bytes:
    head = json.dumps({"decision": "approve:appr-01", "pad": ""}).encode()
    body = head[:-2] + b"x" * (size - len(head)) + head[-2:]
    assert len(body) == size and json.loads(body)["decision"] == "approve:appr-01"
    return body


def test_resolve_413_drains_the_unread_body_like_post_leads(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Class: sibling routes answer an oversize body the same way. /leads sends the 413, half-closes and drains n bytes
    (so the close does not reset the connection under the reply); the resolve route must do the same."""
    from test_portal_api import raw

    from lead_desk import server

    drained: list[int] = []
    done = threading.Event()
    real = server._Handler._drain

    def spy(self: Any, remaining: int) -> None:
        drained.append(remaining)
        try:
            real(self, remaining)
        finally:
            done.set()

    monkeypatch.setattr(server._Handler, "_drain", spy)
    # Force the unlucky order on every run: the client has read the whole reply and the end of the stream (the
    # half-close) while the server thread has not yet reached _drain. A pause right after the half-close does this.
    half_close = server._Handler._flush_and_half_close

    def half_close_then_pause(self: Any) -> None:
        half_close(self)
        time.sleep(HALF_CLOSE_PAUSE_SECONDS)

    monkeypatch.setattr(server._Handler, "_flush_and_half_close", half_close_then_pause)
    rig = approval_rig(env)
    rid = make_later(rig)
    base, port = _http(env, rig)
    got = raw(
        base,
        f"POST /review/D{rid}/resolve HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Content-Length: 70000",
        body=_padded_decision(70000),
    )
    assert got == (413, {"error": "too_large"})
    # The client reads to the end of the reply, and the desk half-closes (that end) BEFORE it calls _drain: the server
    # thread may not have reached _drain yet. Wait for it to finish (a bounded wait, not a sleep), then look.
    assert done.wait(DRAIN_WAIT_SECONDS), "the resolve route never drained the unread body"
    assert drained == [70000]


def _gate_order_and_length_rules(env: Any, rig: Any, rid: int, base: str, port: int) -> None:
    from test_portal_api import EVIL, raw

    line = f"POST /review/D{rid}/resolve HTTP/1.1"
    host = f"Host: 127.0.0.1:{port}"
    # Host and Origin are checked first: a foreign caller learns nothing about the cap
    assert raw(base, line, f"Host: {EVIL}", "Content-Length: 70000") == (403, {"error": "forbidden_host"})
    assert raw(base, line, host, f"Origin: http://{EVIL}", "Content-Length: 70000") == (
        403,
        {"error": "forbidden_origin"},
    )
    # a missing or a conflicting Content-Length is still "no decision" (400 bad_decision), not 413
    assert raw(base, line, host) == (400, {"error": "bad_decision"})
    assert raw(base, line, host, "Content-Length: 70000", "Content-Length: 2", body=b"{}") == (
        400,
        {"error": "bad_decision"},
    )
    assert decisions(rig) == []


def test_oversize_decision_body_is_413_before_any_body_byte_is_read(env) -> None:
    from test_portal_api import raw

    rig = approval_rig(env)
    rid = make_later(rig)
    base, port = _http(env, rig)
    cap = rig.desk.config.size_cap_bytes
    assert cap == 65536
    # the head alone announces 70,000 bytes and no body byte follows: only a check before the read can answer
    got = raw(base, f"POST /review/D{rid}/resolve HTTP/1.1", f"Host: 127.0.0.1:{port}", "Content-Length: 70000")
    assert got == (413, {"error": "too_large"})
    assert decisions(rig) == []
    # a real 70,000-byte body that would be a valid decision if it were read
    body = _padded_decision(70000)
    got = raw(
        base, f"POST /review/D{rid}/resolve HTTP/1.1", f"Host: 127.0.0.1:{port}", "Content-Length: 70000", body=body
    )
    assert got[0] == 413 and decisions(rig) == []
    _gate_order_and_length_rules(env, rig, rid, base, port)
    # one byte over the cap is refused, the cap itself is accepted
    over = _padded_decision(cap + 1)
    assert (
        raw(
            base,
            f"POST /review/D{rid}/resolve HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            f"Content-Length: {cap + 1}",
            body=over,
        )[0]
        == 413
    )
    assert decisions(rig) == []
    exact = _padded_decision(cap)
    assert (
        raw(
            base,
            f"POST /review/D{rid}/resolve HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            f"Content-Length: {cap}",
            body=exact,
        )[0]
        == 200
    )
    assert len(decisions(rig)) == 1
