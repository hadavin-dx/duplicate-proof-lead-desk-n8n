"""Portal tests: the read-only JSON under /api/ui, the fixed files under /ui, and the Host and Origin gate.
Made-up data only. The response shapes are checked against tests/portal_fixtures/*.json ."""

from __future__ import annotations

import importlib
import ipaddress
import json
import socket
import sqlite3
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from support import (
    HOST,
    Rig,
    call,
    child_env,
    free_port,
    lead,
    raw_exchange,
    run_threads,
    start_proc,
    start_serving,
    stop_proc,
    stop_serving,
    wait_until,
)

from lead_desk import loader, portal_api, server
from lead_desk.config import Config, is_unspecified
from lead_desk.constants import (
    DEMO_BODY_MAX,
    DUPLICATES_MAX,
    FEED_ITEMS,
    LEAD_ID_HEX_CHARS,
    LEAD_ID_PREFIX,
    LEADS_LIMIT_DEFAULT,
    PORTAL_MAX_INFLIGHT,
    SECURITY_HEADERS,
    SERIES_BUCKET_SECONDS,
    SERIES_BUCKETS,
    STATIC_FILE_MAX_BYTES,
)
from lead_desk.server import make_server

FIXTURES = Path(__file__).resolve().parent / "portal_fixtures"
STALE = 1000  # seconds: far older than stall_seconds
RESTARTS = 20
READY_SECONDS = 30.0
BIG_LEADS = 3000
PERF_LIMIT_SECONDS = 10.0
BURST = 10
PLACEHOLDER = b"<!doctype html><title>placeholder</title>"
NEW_OUTSIDE = {"form": "ui-1", "webhook": "ui-2"}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# -- shape comparison: keys and types, recursive; null matches any type (a nullable field)
def _fits(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_fits(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return all(any(_fits(x, y) for y in b) for x in a)
    return isinstance(a, bool) == isinstance(b, bool) and (type(a) is type(b))


def same_shape(mine: Any, fix: Any) -> bool:
    """Every part of mine fits a part of the fixture, and every list element shape of the fixture is met by mine."""
    return _fits(mine, fix) and _covers(mine, fix)


def _covers(mine: Any, fix: Any) -> bool:
    if isinstance(mine, dict) and isinstance(fix, dict):
        return all(_covers(mine[k], fix[k]) for k in fix if k in mine)
    if isinstance(mine, list) and isinstance(fix, list):
        return all(any(_fits(x, y) and _covers(x, y) for x in mine) for y in fix)
    return True


# -- state
def sql(rig: Rig, text: str, args: tuple[Any, ...] = ()) -> None:
    con = sqlite3.connect(rig.db_path, timeout=10)
    try:
        con.execute(text, args)
        con.commit()
    finally:
        con.close()


def fresh(env: Any, **over: Any) -> tuple[Rig, str]:
    rig = env.rig(**over)
    rig.clock.t = int(time.time())  # wall-clock: the desk's own calendar stamps
    return rig, env.serve(rig)


def get(base: str, path: str) -> tuple[int, Any]:
    return call(base, "GET", path)  # type: ignore[no-any-return]


def stale_receipt(rig: Rig, ref: str, outcome: str | None, reason: str | None = None) -> None:
    at = rig.clock.t - STALE
    sql(
        rig,
        "INSERT INTO receipts(source, source_ref, received_at, payload, outcome, outcome_reason, decided_at)"
        " VALUES ('webhook', ?, ?, '{}', ?, ?, ?)",
        (ref, at, outcome, reason, None if outcome is None else at),
    )


def seed(rig: Rig) -> dict[str, str]:
    """A lead book with every kind of item: sent, duplicates (with and without new details), a replay with other
    data, an incomplete request, a desk fault, a failed follow-up, both kinds of UNKNOWN, and stalled work."""
    d = rig.desk
    ids: dict[str, str] = {}
    ids["ada"] = d.handle("form", "ui-1", lead()).body["lead_id"]
    d.handle("webhook", "ui-2", lead(phone="555 0199"))  # same person, new phone: duplicate with new details
    d.handle("form", "ui-1", lead(phone="555 0000"))  # same request number, other data: first answer stands
    ids["grace"] = d.handle("form", "ui-g-0", lead(name="Grace Sample", email="grace@example.test")).body["lead_id"]
    for i in range(1, 10):
        d.handle("form", f"ui-g-{i}", lead(name="Grace Sample", email="grace@example.test"))
    d.handle("form", "ui-bad", lead(email=None))  # incomplete: no email
    for key, name in (("linus", "Linus Demo"), ("alan", "Alan Birch"), ("grete", "Grete Holm")):
        ids[key] = d.process("form", f"ui-{key}", lead(name=name, email=f"{key}@example.test")).body["lead_id"]
    for key, name in (("edsger", "Edsger Lund"), ("kurt", "Kurt Sample")):
        ids[key] = d.process("form", f"ui-{key}", lead(name=name, email=f"{key}@example.test")).body["lead_id"]
    old = rig.clock.t - STALE
    mark = "UPDATE followups SET state=?, last_error=?, updated_at=? WHERE lead_id=?"
    sql(rig, mark, ("unknown", "ambiguous_prior", old, ids["linus"]))
    sql(rig, mark, ("failed", "refused", old, ids["alan"]))
    sql(rig, mark, ("unknown", "crm_unconfirmed", old, ids["grete"]))
    sql(rig, mark, ("pending", None, old, ids["edsger"]))
    sql(rig, mark, ("sending", None, old, ids["kurt"]))
    stale_receipt(rig, "ui-fault", "review", "internal_error")
    stale_receipt(rig, "ui-open", None)
    return ids


# -- the read-only connection
def test_read_connection_cannot_write(env) -> None:
    rig, _ = fresh(env)
    seed(rig)
    conn = portal_api.connect_readonly(rig.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] > 0
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1  # second lock, on top of mode=ro
        for stmt in (
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at)"
            " VALUES ('Lffffffffffff', 1, 'form', 'x', 'x@example.test', 'x@example.test', 1)",
            "UPDATE followups SET state = 'failed'",
            "DELETE FROM followups",
            "CREATE TABLE t(a)",
        ):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute(stmt)
        with pytest.raises(sqlite3.OperationalError):  # switching query_only off does not reopen the file for writing
            conn.execute("PRAGMA query_only=OFF")
            conn.execute("UPDATE followups SET state = 'failed'")
    finally:
        conn.close()
    assert rig.query("SELECT count(*) FROM followups WHERE state = 'failed'")[0][0] == 1  # only the seeded one


def test_every_reply_comes_from_one_transaction(env, monkeypatch) -> None:
    """The reads of one reply share one BEGIN ... COMMIT on one connection, and the connection is closed after."""
    rig, base = fresh(env)
    ids = seed(rig)
    seen: list[list[str]] = []
    real = portal_api.connect_readonly

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a portal reply used the desk's own (write-capable) connection")

    monkeypatch.setattr(rig.desk, "_conn", boom)
    monkeypatch.setattr(rig.desk, "review", boom)

    class Spy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn, self.log = conn, []
            seen.append(self.log)

        def execute(self, text: str, *a: Any) -> Any:
            self.log.append(text.split()[0])
            return self.conn.execute(text, *a)

        def close(self) -> None:
            self.log.append("CLOSE")
            self.conn.close()

    monkeypatch.setattr(portal_api, "connect_readonly", lambda p: Spy(real(p)))
    for path in (
        "/api/ui/summary",
        "/api/ui/leads",
        "/api/ui/leads?q=ada",
        f"/api/ui/leads/{ids['ada']}",
        f"/api/ui/leads/{ids['alan']}",
        "/api/ui/review",
    ):
        seen.clear()
        assert get(base, path)[0] == 200
        assert len(seen) == 1, (path, "more than one connection for one reply")
        log = seen[-1]
        assert log[0] == "BEGIN" and log[-2:] == ["COMMIT", "CLOSE"], (path, log)
        assert log.count("BEGIN") == 1 and log.count("COMMIT") == 1
        assert {s for s in log[1:-2]} <= {"SELECT"}


# -- shapes
def test_summary_shape_and_numbers(env) -> None:
    rig, base = fresh(env)
    seed(rig)
    status, out = get(base, "/api/ui/summary")
    assert status == 200
    assert same_shape(out, fixture("summary")), "summary differs from the fixture shape"
    c = out["counts"]
    assert c == {"received": 20, "new": 7, "duplicate": 10, "review": 2, "open": 1}
    assert out["reconciled"] is True
    assert sum(d["received"] for d in out["doors"]) == c["received"]  # the doors query agrees with the counts query
    assert sum(out["followups"].values()) == c["new"]  # one follow-up per new lead, read by another statement
    assert out["needs_action"] == 5  # R (incomplete), R (fault), failed, unknown, unknown (CRM)
    assert out["waiting"] == 3  # one open receipt, one pending and one sending follow-up, all older than stall
    f = out["followups"]
    assert f["unknown"] + f["crm_unconfirmed"] == 2 and f["failed"] + f["did_not_finish"] == 1
    assert [d["source"] for d in out["doors"]] == ["form", "webhook"]
    assert out["series"]["bucket_seconds"] == SERIES_BUCKET_SECONDS
    ts = [b["t"] for b in out["series"]["buckets"]]
    assert len(ts) == SERIES_BUCKETS and ts == sorted(ts) and ts[-1] <= out["now"] < ts[-1] + SERIES_BUCKET_SECONDS
    assert all(b - a == SERIES_BUCKET_SECONDS for a, b in zip(ts, ts[1:], strict=False))
    in_series = sum(sum(b[k] for k in ("new", "duplicate", "review")) for b in out["series"]["buckets"])
    assert in_series == c["new"] + c["duplicate"] + c["review"] - 1  # the stale desk-fault receipt was decided long ago
    assert len(out["recent"]) == FEED_ITEMS
    ats = [(e["at"], e["receipt_id"]) for e in out["recent"]]
    assert ats == sorted(ats, reverse=True)


def test_summary_empty_and_started_at(env) -> None:
    _, base = fresh(env)
    status, out = get(base, "/api/ui/summary")
    assert status == 200 and same_shape(out, fixture("summary-empty"))
    assert out["counts"] == {"received": 0, "new": 0, "duplicate": 0, "review": 0, "open": 0}
    assert out["doors"][0]["last_seen"] is None and out["recent"] == [] and out["reconciled"] is True
    assert abs(out["desk_started_at"] - int(time.time())) < 60  # wall-clock: calendar stamp


def test_series_bucket_holds_the_decided_time(env) -> None:
    rig, base = fresh(env)
    rig.desk.handle("form", "ui-1", lead())
    decided = int(rig.query("SELECT decided_at FROM receipts")[0][0])
    out = get(base, "/api/ui/summary")[1]
    hit = [b for b in out["series"]["buckets"] if b["t"] == (decided // SERIES_BUCKET_SECONDS) * SERIES_BUCKET_SECONDS]
    assert len(hit) == 1 and hit[0]["new"] == 1 and hit[0]["duplicate"] == 0


def test_open_receipts_counted_young_not_listed(env) -> None:
    """An open receipt is never hidden. Young: only in counts.open. Old: also in Needs (waiting)."""
    rig, base = fresh(env)
    sql(
        rig,
        "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form', 'young', ?, '{}')",
        (rig.clock.t,),
    )
    out = get(base, "/api/ui/summary")[1]
    assert out["counts"]["open"] == 1 and out["waiting"] == 0 and out["reconciled"] is True
    assert get(base, "/api/ui/review")[1]["items"] == []
    stale_receipt(rig, "old", None)
    out = get(base, "/api/ui/summary")[1]
    assert out["counts"]["open"] == 2 and out["waiting"] == 1
    items = get(base, "/api/ui/review")[1]["items"]
    assert [i["item_id"][0] for i in items] == ["O"] and items[0]["tab"] == "waiting"


def test_young_followups_are_not_stalled(env) -> None:
    """The age rule for follow-ups: a pending or sending row younger than stall_seconds is neither waiting nor listed;
    one second past the limit it is both."""
    rig, base = fresh(env)
    mark = "UPDATE followups SET state=?, updated_at=? WHERE lead_id=?"
    a = rig.desk.process("form", "y-1", lead(email="y1@example.test")).body["lead_id"]
    b = rig.desk.process("form", "y-2", lead(email="y2@example.test")).body["lead_id"]
    stall = rig.desk.config.stall_seconds
    sql(rig, mark, ("pending", rig.clock.t - stall + 5, a))
    sql(rig, mark, ("sending", rig.clock.t - stall + 5, b))
    out = get(base, "/api/ui/summary")[1]
    assert out["waiting"] == 0 and out["needs_action"] == 0
    assert get(base, "/api/ui/review")[1]["items"] == []
    assert get(base, f"/api/ui/leads/{a}")[1]["review_items"] == []
    sql(rig, mark, ("pending", rig.clock.t - stall - 1, a))
    sql(rig, mark, ("sending", rig.clock.t - stall - 1, b))
    assert get(base, "/api/ui/summary")[1]["waiting"] == 2
    assert sorted(i["item_id"] for i in get(base, "/api/ui/review")[1]["items"]) == sorted(["F" + a, "F" + b])
    assert get(base, f"/api/ui/leads/{a}")[1]["review_items"] == ["F" + a]


def test_reconciled_formula_with_injected_counts() -> None:
    assert portal_api.reconciled(5, 2, 1, 1, 1) is True
    assert portal_api.reconciled(0, 0, 0, 0, 0) is True
    assert portal_api.reconciled(6, 2, 1, 1, 1) is False  # a receipt with no state
    assert portal_api.reconciled(5, 2, 2, 1, 1) is False  # a receipt counted twice


def test_search_treats_like_characters_as_text(env) -> None:
    """The three LIKE characters are text: a lead whose name holds all of them is found by that text, and by nothing
    that only matches as a wildcard."""
    rig, base = fresh(env)
    rig.desk.process("form", "w-1", lead(name="Half 50%_off\\ Deal", email="half@example.test"))
    rig.desk.process("form", "w-2", lead(name="Half 50xyoffz Deal", email="other@example.test"))
    found = get(base, "/api/ui/leads?q=" + quote("50%_off\\"))[1]
    assert found["total"] == 1 and found["rows"][0]["name"] == "Half 50%_off\\ Deal"
    assert get(base, "/api/ui/leads?q=%25")[1]["total"] == 1  # only the lead that holds a percent sign
    assert get(base, "/api/ui/leads?q=_")[1]["total"] == 1
    assert get(base, "/api/ui/leads?q=" + quote("50%off"))[1]["total"] == 0  # not a wildcard
    assert get(base, "/api/ui/leads?q=" + quote("50_off"))[1]["total"] == 0
    assert get(base, "/api/ui/leads?q=%5C")[1]["total"] == 1


def test_refused_at_door_counts_envelopes_without_receipt(env) -> None:
    rig, base = fresh(env)
    assert call(base, "POST", "/leads", raw=b"not json")[0] == 400
    out = get(base, "/api/ui/summary")[1]
    assert out["refused_at_door"] == 1 == rig.desk.rejected_envelopes() == get(base, "/counts")[1]["rejected_envelopes"]
    assert out["counts"]["received"] == 0


def test_leads_shape_search_filter_and_paging(env) -> None:
    rig, base = fresh(env)
    ids = seed(rig)
    status, out = get(base, "/api/ui/leads")
    assert status == 200 and same_shape(out, fixture("leads"))
    assert out["total"] == 7 and out["limit"] == LEADS_LIMIT_DEFAULT and out["offset"] == 0
    created = [r["created_at"] for r in out["rows"]]
    assert created == sorted(created, reverse=True)
    grace = next(r for r in out["rows"] if r["lead_id"] == ids["grace"])
    assert (grace["repeats"], grace["arrivals"], grace["first_request_id"]) == (9, 1, "ui-g-0")
    ada = next(r for r in out["rows"] if r["lead_id"] == ids["ada"])
    assert (ada["repeats"], ada["arrivals"]) == (1, 2)  # one repeat from the other door; the form id came twice
    assert [r["lead_id"] for r in get(base, "/api/ui/leads?q=GRACE")[1]["rows"]] == [ids["grace"]]
    assert [r["lead_id"] for r in get(base, f"/api/ui/leads?q={ids['ada'][1:7]}")[1]["rows"]] == [ids["ada"]]
    assert [r["lead_id"] for r in get(base, "/api/ui/leads?q=ui-g-0")[1]["rows"]] == [ids["grace"]]
    assert get(base, "/api/ui/leads?q=%25")[1]["total"] == 0  # a percent sign is text, not a wildcard
    assert get(base, "/api/ui/leads?q=_")[1]["total"] == 0
    assert get(base, "/api/ui/leads?q=%5C")[1]["total"] == 0
    assert [r["lead_id"] for r in get(base, "/api/ui/leads?state=failed")[1]["rows"]] == [ids["alan"]]
    both = get(base, "/api/ui/leads?state=unknown&q=linus")[1]
    assert both["total"] == 1 and both["rows"][0]["lead_id"] == ids["linus"]
    page1, page2 = get(base, "/api/ui/leads?limit=3")[1], get(base, "/api/ui/leads?limit=3&offset=3")[1]
    assert len(page1["rows"]) == 3 and len(page2["rows"]) == 3 and page1["total"] == page2["total"] == 7
    assert not {r["lead_id"] for r in page1["rows"]} & {r["lead_id"] for r in page2["rows"]}
    assert get(base, "/api/ui/leads?offset=100000")[1]["rows"] == []


def test_leads_empty(env) -> None:
    _, base = fresh(env)
    status, out = get(base, "/api/ui/leads")
    assert status == 200 and same_shape(out, fixture("leads-empty")) and out["rows"] == [] and out["total"] == 0


def test_lead_detail_shape_and_differences(env) -> None:
    rig, base = fresh(env)
    ids = seed(rig)
    status, out = get(base, f"/api/ui/leads/{ids['ada']}")
    assert status == 200 and same_shape(out, fixture("lead-detail"))
    assert out["lead"]["name"] == "Ada Example" and out["followup"]["state"] == "sent"
    assert out["first_receipt"]["source_ref"] == "ui-1" and out["first_receipt"]["arrivals"] == 2
    assert out["first_receipt"]["payload_conflict"] == 1
    assert out["duplicates_total"] == 1 and len(out["duplicates"]) == 1
    assert out["duplicates"][0]["differing"] == [{"field": "phone", "first": "555 0100", "this": "555 0199"}]
    assert out["duplicates"][0]["seconds_after_first"] == 0  # the rig clock does not move in seed()
    assert [i[0] for i in out["review_items"]] == ["D", "D"]  # the repeat with new details, the changed replay
    # the stalled and failed work of other leads is listed on their own lead
    assert get(base, f"/api/ui/leads/{ids['edsger']}")[1]["review_items"] == ["F" + ids["edsger"]]
    assert get(base, f"/api/ui/leads/{ids['alan']}")[1]["review_items"] == ["F" + ids["alan"]]


def test_lead_detail_lists_at_most_the_cap(env) -> None:
    rig, base = fresh(env)
    first = rig.desk.handle("form", "ui-0", lead()).body["lead_id"]
    for i in range(DUPLICATES_MAX + 3):
        rig.desk.handle("webhook", f"ui-w-{i}", lead())
    out = get(base, f"/api/ui/leads/{first}")[1]
    assert out["duplicates_total"] == DUPLICATES_MAX + 3 and len(out["duplicates"]) == DUPLICATES_MAX
    ids = [d["receipt_id"] for d in out["duplicates"]]
    assert ids == sorted(set(ids), reverse=True)  # strictly descending receipt numbers
    assert ids[0] == max(ids) and ids[0] == rig.query("SELECT max(receipt_id) FROM receipts")[0][0]


def test_lead_id_grammar_checked_before_db(env, monkeypatch) -> None:
    """The regex itself: a real id passes, every near miss is a 404 that never opens the database."""
    rig, base = fresh(env)
    real = rig.desk.process("form", "g-1", lead()).body["lead_id"]
    assert portal_api._LEAD_ID.fullmatch(real)
    assert portal_api.LEAD_ID_PATTERN == rf"^{LEAD_ID_PREFIX}[0-9a-f]{{{LEAD_ID_HEX_CHARS}}}$"
    assert get(base, f"/api/ui/leads/{real}")[0] == 200
    opened: list[str] = []
    real_connect = portal_api.connect_readonly
    monkeypatch.setattr(portal_api, "connect_readonly", lambda p: opened.append("x") or real_connect(p))
    for bad in (
        real.lower(),  # the prefix letter changes case; real.upper() is the id itself when it has no hex letters
        real[:-1],
        real + "0",
        real + "%0A",
        "l" + real[1:],
        real[:-1] + "g",
        real.replace("L", "M", 1),
        "%20" + real,
        real + "%20",
    ):
        assert get(base, "/api/ui/leads/" + bad) == (404, {"error": "not_found"}), bad
    assert opened == []


def test_lead_id_grammar_and_not_found(env) -> None:
    rig, base = fresh(env)
    seed(rig)
    status, out = get(base, "/api/ui/leads/Lffffffffffff")  # well formed, no such lead
    assert (status, out) == (404, fixture("lead-detail-not-found"))
    for bad in (
        "L123",
        "l0c4d8e2f6a19",
        "L0C4D8E2F6A19",
        "L0c4d8e2f6a1",
        "L0c4d8e2f6a19x",
        "..",
        "%27%20OR%201%3D1",
        "L",
    ):
        assert get(base, "/api/ui/leads/" + bad) == (404, {"error": "not_found"}), bad
    assert get(base, "/api/ui/leads/Lffffffffffff/extra")[0] == 404
    assert get(base, "/api/ui/nothing")[0] == 404
    assert get(base, "/api/ui/")[0] == 404


def test_review_shape_kinds_tabs_and_actions(env) -> None:
    rig, base = fresh(env)
    ids = seed(rig)
    status, out = get(base, "/api/ui/review")
    assert status == 200 and same_shape(out, fixture("review")), "review differs from the fixture shape"
    items = {i["item_id"]: i for i in out["items"]}
    assert out["tabs"] == {"action": 5, "waiting": 3, "info": 2}
    tabs = [i["tab"] for i in out["items"]]
    assert tabs == sorted(tabs, key=("action", "waiting", "info").index)
    alan = items["F" + ids["alan"]]
    assert (alan["title"], alan["lead_name"], [a["id"] for a in alan["actions"]]) == (
        "The follow-up email was not sent",
        "Alan Birch",
        ["retry"],
    )
    assert alan["actions"][0]["endpoint"] == f"/review/F{ids['alan']}/retry" and alan["actions"][0]["confirm"] is None
    linus = items["F" + ids["linus"]]
    assert [a["id"] for a in linus["actions"]] == ["retry", "resolve_sent", "resolve_not_sent"]
    assert [a["body"] for a in linus["actions"]] == [None, {"decision": "sent"}, {"decision": "not_sent"}]
    assert linus["actions"][0]["confirm"] is None
    assert all(a["confirm"] and a["confirm"]["verb"] for a in linus["actions"][1:])
    grete = items["F" + ids["grete"]]
    assert [a["id"] for a in grete["actions"]] == [
        "retry",
        "resolve_crm_present",
        "resolve_not_sent",
    ]  # `sent` never offered
    assert "hint" in grete and grete["title"].startswith("The CRM may or may not")
    r_incomplete = next(i for i in out["items"] if i["reason"].startswith("incomplete:"))
    assert (r_incomplete["title"], r_incomplete["actions"]) == ("Incomplete request: email is missing", [])
    fault = next(i for i in out["items"] if i["reason"] == "internal_error")
    assert fault["title"] == "The desk could not decide this request" and fault["actions"] == []
    assert items["F" + ids["edsger"]]["title"] == "Queued longer than 120 s"
    assert [a["id"] for a in items["F" + ids["edsger"]]["actions"]] == ["retry"]
    assert items["F" + ids["kurt"]]["actions"] == [] and items["F" + ids["kurt"]]["title"].startswith("Sending longer")
    assert next(i for i in out["items"] if i["item_id"][0] == "O")["tab"] == "waiting"
    info = [i for i in out["items"] if i["tab"] == "info"]
    assert sorted(i["kind"] for i in info) == ["duplicate_with_new_info", "replay_payload_differs"]
    assert next(i for i in info if i["kind"] == "duplicate_with_new_info")["title"] == "Repeat with new details: phone"
    assert all(i["lead_name"] == "Ada Example" for i in info)
    assert {i["item_id"] for i in out["items"]} == set(items)


@pytest.mark.parametrize("reason", ["refused", "unreachable", "internal:KeyError"])
def test_failed_card_after_maybe_sent(env, reason: str) -> None:
    """A follow-up that may have sent once (ambiguous_prior) and then ended refused, unreachable or internal."""
    rig, base = fresh(env)
    lid = rig.desk.process("form", "fp-1", lead()).body["lead_id"]
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error=?, ambiguous_prior=1, updated_at=? WHERE lead_id=?",
        (reason, 5, lid),
    )
    card = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["item_id"] == "F" + lid)
    said = card["title"] + " " + card["reason_text"]
    assert card["title"] == "Follow-up did not finish" and "nothing was sent" not in said.lower()
    assert "may already have been sent" in said and [a["id"] for a in card["actions"]] == ["retry"]


def test_clock_read_inside_snapshot(env, monkeypatch) -> None:
    rig, base = fresh(env)
    seen: list[int] = [0]
    real = portal_api.connect_readonly

    class Count:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def execute(self, text: str, *a: Any) -> Any:
            seen[0] += 1
            return self.conn.execute(text, *a)

        def close(self) -> None:
            self.conn.close()

    monkeypatch.setattr(portal_api, "connect_readonly", lambda p: Count(real(p)))
    at: list[int] = []
    clock_now = rig.desk.now
    monkeypatch.setattr(rig.desk, "now", lambda: (at.append(seen[0]), clock_now())[1])
    for path in ("/api/ui/summary", "/api/ui/leads", "/api/ui/review"):
        at.clear()
        seen[0] = 0
        assert get(base, path)[0] == 200
        assert at == [2], (path, at)  # BEGIN and the first read came first
    rig.clock.t = 1_000_000
    server_obj = make_server(rig.desk, replace(rig.config, port=free_port()))
    try:
        assert server_obj.started_at == 1_000_000
    finally:
        server_obj.server_close()


def test_unready_db_gives_fixed_503(env, tmp_path) -> None:
    from support import build_rig

    (tmp_path / "x").mkdir()
    rig = build_rig(tmp_path / "x", start=False)  # the desk has not run startup yet
    base = env.serve(rig)
    assert get(base, "/api/ui/summary") == (503, {"error": "unavailable"})
    assert get(base, "/api/ui/review") == (503, {"error": "unavailable"})
    rig.desk.startup()
    assert get(base, "/api/ui/summary")[0] == 200
    (tmp_path / "y").mkdir()
    other = build_rig(tmp_path / "y", start=True)
    other_base = env.serve(other)
    for path in (":memory:", ""):
        object.__setattr__(other.desk.config, "db_path", Path(path))
        assert get(other_base, "/api/ui/summary") == (503, {"error": "unavailable"}), path


def test_other_database_errors_stay_500(env, monkeypatch) -> None:
    """Only the "not ready" faults are a 503; a bad column or any other database error is a bug and stays a 500."""
    _, base = fresh(env)

    def broken(desk: Any, started_at: int, *rest: Any) -> Any:
        raise sqlite3.OperationalError("no such column: nope")

    monkeypatch.setattr(portal_api, "_summary", broken)
    assert get(base, "/api/ui/summary") == (500, {"error": "internal"})


@pytest.mark.parametrize(
    ("note", "title", "said"),
    [
        (
            "person: not sent",
            "Follow-up failed; a person marked the email as not sent",
            "marked that the email was not sent",
        ),
        ("person: crm row present", "Follow-up not finished; the CRM entry is confirmed", "CRM entry is there"),
    ],
)
def test_failed_card_after_a_person_decides(env, note: str, title: str, said: str) -> None:
    """The reason of the card is the follow-up's last_error (review_items), which Desk.resolve sets for a person's
    choice; the card says what the person marked, never that the email may have left."""
    rig, base = fresh(env)
    lid = rig.desk.process("form", "pd-1", lead()).body["lead_id"]
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error=?, crm_done=1, updated_at=? WHERE lead_id=?",
        (note, 5, lid),
    )
    assert rig.query("SELECT reason FROM review_items WHERE lead_id=?", (lid,)) == [(note,)]
    card = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["item_id"] == "F" + lid)
    assert card["title"] == title and said in card["reason_text"] and "gone out" not in card["reason_text"]
    assert [a["id"] for a in card["actions"]] == ["retry"] and "Retry" in card["reason_text"]


@pytest.mark.parametrize(
    ("last_error", "crm_done", "prior", "decision", "title"),
    [
        ("ambiguous", 1, 0, "not_sent", "Follow-up failed; a person marked the email as not sent"),
        ("ambiguous", 1, 1, "not_sent", "Follow-up failed; a person marked the email as not sent"),
        ("crm_unconfirmed", 2, 0, "not_sent", "Follow-up failed; a person marked the CRM entry as missing"),
        ("crm_unconfirmed", 2, 0, "crm_present", "Follow-up not finished; the CRM entry is confirmed"),
        ("crm_unconfirmed", 2, 1, "crm_present", "Follow-up not finished; the CRM entry is confirmed"),
    ],
)
def test_person_cards_after_the_real_resolve(env, last_error, crm_done, prior, decision, title) -> None:
    """Drives the real Desk.resolve (not hand-written last_error) and reads the card the portal then shows: the words
    match what resolve wrote, and a CRM answer never produces a claim about the email."""
    rig, base = fresh(env)
    lid = rig.desk.process("form", "pr-1", lead()).body["lead_id"]
    sql(
        rig,
        "UPDATE followups SET state='unknown', last_error=?, crm_done=?, ambiguous_prior=?, updated_at=?"
        " WHERE lead_id=?",
        (last_error, crm_done, prior, 5, lid),
    )
    assert rig.desk.resolve("F" + lid, decision).status == 200
    card = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["item_id"] == "F" + lid)
    assert card["title"] == title
    assert "gone out" not in card["reason_text"] and "has not been tried" not in card["reason_text"]
    crm_question = last_error == "crm_unconfirmed"
    # the card speaks about the thing the person answered: a CRM answer says nothing about the email being unsent
    assert ("CRM entry" in card["reason_text"]) == crm_question
    assert ("email was not sent" in card["reason_text"]) == (not crm_question and decision == "not_sent")
    assert "mail step" not in card["reason_text"]  # the cue beside the Retry button says it, once
    if crm_question and decision == "not_sent":
        assert "Retry sends it now" not in card["reason_text"]


def test_person_not_sent_card_by_crm_done(env) -> None:
    """The same last_error reads differently by crm_done: 0 is the CRM answer, 1 is the email answer (raw rows)."""
    rig, base = fresh(env)
    words = {}
    for crm_done in (0, 1):
        lid = rig.desk.process("form", f"cd-{crm_done}", lead(email=f"cd{crm_done}@example.test")).body["lead_id"]
        sql(
            rig,
            "UPDATE followups SET state='failed', last_error='person: not sent', crm_done=?, updated_at=?"
            " WHERE lead_id=?",
            (crm_done, 5, lid),
        )
        words[crm_done] = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["item_id"] == "F" + lid)["title"]
    assert words[0].endswith("the CRM entry as missing") and words[1].endswith("the email as not sent")


@pytest.mark.parametrize(
    ("reason", "prior", "nothing_sent"),
    [
        ("refused", 0, True),
        ("unreachable", 0, True),
        ("internal:KeyError", 0, True),
        ("ambiguous", 1, False),
        ("crash_while_sending", 1, False),
        ("crash_while_sending", 0, True),
        ("something_new", 1, False),
    ],
)
def test_failed_card_honest_about_sending(env, reason: str, prior: int, nothing_sent: bool) -> None:
    rig, base = fresh(env)
    lid = rig.desk.process("form", "fc-1", lead()).body["lead_id"]
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error=?, ambiguous_prior=?, updated_at=? WHERE lead_id=?",
        (reason, prior, 5, lid),
    )
    card = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["item_id"] == "F" + lid)
    assert [a["id"] for a in card["actions"]] == ["retry"] and card["tab"] == "action"
    said = card["title"] + " " + card["reason_text"]
    if nothing_sent:
        assert (
            card["title"] == "The follow-up email was not sent"
            and "check the mail step" not in card["reason_text"].lower()
        )
    else:
        assert card["title"] == "Follow-up did not finish"
        assert "nothing was sent" not in said.lower()
        assert "may already have been sent" in said and "keyed outbox" not in said and "Check the mail step" not in said


@pytest.mark.parametrize(
    ("reason", "must", "must_not"),
    [
        ("internal_unstorable", "Send the form again", "nothing was sent"),
        ("internal_unstorable:integrity", "a value broke a storage rule; nothing was sent", "could not decide"),
    ],
)
def test_unstorable_reasons_match_by_prefix(env, reason: str, must: str, must_not: str) -> None:
    rig, base = fresh(env)
    stale_receipt(rig, "ui-unstorable", "review", reason)
    _, out = get(base, "/api/ui/review")
    item = next(i for i in out["items"] if i["reason"] == reason)
    assert item["title"] == "The desk could not store this request" and item["actions"] == []
    assert must in item["reason_text"] and must_not not in item["reason_text"]


def test_review_empty(env) -> None:
    _, base = fresh(env)
    status, out = get(base, "/api/ui/review")
    assert status == 200 and same_shape(out, fixture("review-empty")) and out["items"] == []


# -- bad queries
@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/api/ui/leads?limit=101", "limit"),
        ("/api/ui/leads?limit=0", "limit"),
        ("/api/ui/leads?limit=abc", "limit"),
        ("/api/ui/leads?limit=", "limit"),
        ("/api/ui/leads?limit=-1", "limit"),
        ("/api/ui/leads?limit=" + "9" * 40, "limit"),
        ("/api/ui/leads?offset=100001", "offset"),
        ("/api/ui/leads?offset=x", "offset"),
        ("/api/ui/leads?q=" + "a" * 101, "q"),
        ("/api/ui/leads?state=bogus", "state"),
        ("/api/ui/leads?q=a%00b", "q"),
        ("/api/ui/leads?q=%0A", "q"),
        ("/api/ui/leads?sort=name", "sort"),
        ("/api/ui/leads?limit=1&limit=2", "limit"),
        ("/api/ui/leads?q=a&q=b", "q"),
        ("/api/ui/summary?x=1", "x"),
        ("/api/ui/review?limit=1", "limit"),
        ("/api/ui/leads/Lffffffffffff?x=1", "x"),
    ],
)
def test_bad_query_is_400_with_field(env, path: str, field: str) -> None:
    _, base = fresh(env)
    assert get(base, path) == (400, {"error": "bad_query", "field": field})


def test_error_envelope_matches_fixture(env) -> None:
    _, base = fresh(env)
    assert get(base, "/api/ui/leads?limit=101") == (400, fixture("error-envelope"))


def test_limit_and_search_edges_are_accepted(env) -> None:
    _, base = fresh(env)
    assert get(base, "/api/ui/leads?limit=100&offset=100000&q=" + "a" * 100)[0] == 200
    assert get(base, "/api/ui/leads?limit=1")[0] == 200


@pytest.mark.parametrize(
    "text",
    [
        "'; DROP TABLE leads; --",
        "' OR '1'='1",
        '" OR 1=1 --',
        "%27%3B%20DELETE%20FROM%20receipts",
        "\\",
        "a\x00b",
        "é€😀",
    ],
)
def test_injection_strings_in_q_and_state(env, text: str) -> None:
    rig, base = fresh(env)
    seed(rig)
    before = rig.query("SELECT (SELECT count(*) FROM leads), (SELECT count(*) FROM receipts)")
    for field in ("q", "state"):
        status, out = get(base, f"/api/ui/leads?{field}={quote(text)}")
        assert status in (200, 400) and ("rows" in out or out["error"] == "bad_query")
        if status == 200:
            assert out["rows"] == []
        else:
            assert out["field"] == field
    assert rig.query("SELECT (SELECT count(*) FROM leads), (SELECT count(*) FROM receipts)") == before


def test_busy_lock_is_503(env, monkeypatch) -> None:
    _, base = fresh(env)

    def locked(_: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(portal_api, "connect_readonly", locked)
    assert get(base, "/api/ui/summary") == (503, fixture("error-busy"))


def test_only_get_on_ui_routes(env) -> None:
    _, base = fresh(env)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        for path in ("/api/ui/summary", "/ui/", "/ui"):
            status, body = call(base, method, path, body={})
            assert (status, body) == (405, {"error": "method_not_allowed"}), (method, path)


# -- static files and headers
def test_static_files_and_redirect(env, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: None)  # the demo_only folder is cut
    root = tmp_path / "portal"
    (root / "pages").mkdir(parents=True)
    (root / "index.html").write_bytes(PLACEHOLDER)
    (root / "app.css").write_bytes(b"body{}")
    (root / "pages" / "x.js").write_bytes(b"void 0;")
    (root / "notes.txt").write_bytes(b"skipped")
    (root / "font.woff2").write_bytes(b"skipped")
    static = portal_api.build_static(root)
    assert sorted(static) == ["app.css", "index.html", "pages/x.js"]
    monkeypatch.setattr(portal_api, "STATIC", static)
    _, base = fresh(env)

    def fetch(target: str) -> tuple[int, dict[str, str], bytes]:
        host, port = base.removeprefix("http://").split(":")
        with socket.create_connection((host, int(port)), timeout=10) as s:
            s.sendall(f"GET {target} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode())
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.lower(): v for k, _, v in (ln.partition(": ") for ln in lines[1:])}
        return int(lines[0].split(" ")[1]), headers, body

    for target in ("/ui/", "/ui/index.html"):
        status, headers, body = fetch(target)
        assert (status, body) == (200, PLACEHOLDER) and headers["content-type"] == "text/html; charset=utf-8"
    assert fetch("/ui/app.css")[1]["content-type"] == "text/css; charset=utf-8"
    status, headers, body = fetch("/ui/pages/x.js")
    assert (status, body) == (200, b"void 0;") and headers["content-type"] == "text/javascript; charset=utf-8"
    status, headers, body = fetch("/ui")
    assert (status, headers["location"], body) == (308, "/ui/", b"")
    for target in (
        "/ui/../README.md",
        "/ui/%2e%2e/README.md",
        "/ui/%2E%2E%2Fpyproject.toml",
        "/ui/pages/../../README.md",
        "/ui/%5cwindows",
        "/ui/..%5c..%5cx",
        "/ui//etc/passwd",
        "/ui/%2fetc/passwd",
        "/ui/notes.txt",
        "/ui/font.woff2",
        "/ui/missing.js",
        "/ui/pages/",
        "/ui/pages",
        "/ui/index.html%00.js",
        "/ui/demo/portal_demo.js",
        "/ui/demo/",
    ):
        status, headers, body = fetch(target)
        assert status == 404, target
        assert json.loads(body) == {"error": "not_found"}, target


def test_static_map_skips_links_and_big_files(tmp_path) -> None:
    root, outside = tmp_path / "portal", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "ok.js").write_bytes(b"void 0;")
    (outside / "secret.js").write_bytes(b"secret")
    (outside / "dir").mkdir()
    (outside / "dir" / "deep.css").write_bytes(b"x")
    (root / "big.js").write_bytes(b"x" * (STATIC_FILE_MAX_BYTES + 1))
    (root / "edge.js").write_bytes(b"x" * STATIC_FILE_MAX_BYTES)
    try:
        (root / "link.js").symlink_to(outside / "secret.js")
        (root / "inside.js").symlink_to(root / "ok.js")  # a link to a file inside is still a link: skipped
        (root / "dirlink").symlink_to(outside / "dir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot make symbolic links here")
    assert sorted(portal_api.build_static(root)) == ["edge.js", "ok.js"]


def test_security_headers_on_every_kind_of_reply(env, monkeypatch) -> None:
    monkeypatch.setattr(portal_api, "STATIC", {"index.html": (PLACEHOLDER, "text/html; charset=utf-8")})
    rig, base = fresh(env)
    seed(rig)
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    csp = dict(SECURITY_HEADERS)["Content-Security-Policy"]
    for needle in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'none'"):
        assert needle in csp
    assert "data:" not in csp and "unsafe" not in csp
    for method, path, data in (
        ("GET", "/api/ui/summary", None),
        ("GET", "/api/ui/leads?limit=999", None),
        ("GET", "/ui/", None),
        ("GET", "/ui/missing.js", None),
        ("GET", "/counts", None),
        ("GET", "/nothing", None),
        ("POST", "/leads", b"{}"),
    ):
        req = urllib.request.Request(base + path, data=data, method=method)  # noqa: S310
        try:
            resp = opener.open(req, timeout=10)  # noqa: S310
        except urllib.error.HTTPError as e:
            resp = e
        for key, value in SECURITY_HEADERS:
            assert resp.headers.get(key) == value, (path, key)
        resp.close()


# -- the Host and Origin gate
def wire(base: str, head: str, body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    """One request as typed bytes; (status, lower-cased headers, body)."""
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(head.encode("latin-1") + b"\r\n\r\n" + body)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    top, _, rest = data.partition(b"\r\n\r\n")
    lines = top.decode("latin-1").split("\r\n")
    return int(lines[0].split(" ")[1]), {k.lower(): v for k, _, v in (ln.partition(": ") for ln in lines[1:])}, rest


def raw(base: str, request_line: str, *headers: str, body: bytes = b"") -> tuple[int, Any]:
    head = "\r\n".join([request_line, *headers, "Connection: close", ""])
    return raw_exchange(base, head.encode() + b"\r\n" + body)


def port_of(base: str) -> str:
    return base.rsplit(":", 1)[1]


EVIL = "evil.example"
LOCAL = "localhost"
WILDCARD4 = str(ipaddress.IPv4Address(0))  # built from parts, never a typed address
LOOP6_SHORT = ":".join(["0"] * 7 + ["1"])
LOOP6_FULL = ipaddress.IPv6Address(1).exploded


def test_headers_and_no_cors_on_error_replies(env) -> None:
    """403, 308, 405, 404 and the replies of the standard library's own error path (bad request line, unknown method,
    over-long line) carry every fixed header and never an Access-Control header."""
    _, base = fresh(env)
    ok = f"Host: {HOST}"
    cases = {
        "403 host": (f"GET /api/ui/summary HTTP/1.1\r\nHost: {EVIL}", 403),
        "403 origin": (f"GET /api/ui/summary HTTP/1.1\r\n{ok}\r\nOrigin: http://{EVIL}", 403),
        "308": (f"GET /ui HTTP/1.1\r\n{ok}", 308),
        "405": (f"POST /api/ui/summary HTTP/1.1\r\n{ok}\r\nContent-Length: 0", 405),
        "404": (f"GET /api/ui/nothing HTTP/1.1\r\n{ok}", 404),
        "unknown method": (f"BREW /ui/ HTTP/1.1\r\n{ok}", 501),
        "bad request line": ("GET\r\nHost: x", 400),
        "long line": (f"GET /{'a' * 70000} HTTP/1.1\r\n{ok}", 414),
    }
    for name, (head, status) in cases.items():
        got, headers, body = wire(base, head)
        assert got == status, (name, got)
        for key, value in SECURITY_HEADERS:
            assert headers.get(key.lower()) == value, (name, key)
        assert not any(k.startswith("access-control-") for k in headers), name
        if body:
            assert isinstance(json.loads(body), dict), name  # JSON, never an HTML error page


@pytest.mark.parametrize(
    "extra",
    [
        [f"Origin: http://{EVIL}"],
        ["Origin: null"],
        ["Sec-Fetch-Site: cross-site"],
        ["Sec-Fetch-Site: same-site"],
    ],
)
@pytest.mark.parametrize("kind", ["summary", "ui", "lead", "post"])
def test_foreign_origin_refused_on_get_and_post(env, kind: str, extra: list[str]) -> None:
    rig, base = fresh(env)
    body = json.dumps({"source": "form", "source_ref": "g1", "payload": lead()}).encode()
    line, more, payload = {
        "summary": ("GET /api/ui/summary HTTP/1.1", [], b""),
        "ui": ("GET /ui/ HTTP/1.1", [], b""),
        "lead": ("GET /api/ui/leads/Lffffffffffff HTTP/1.1", [], b""),
        "post": ("POST /leads HTTP/1.1", [f"Content-Length: {len(body)}"], body),
    }[kind]
    status, out = raw(base, line, f"Host: {HOST}", *extra, *more, body=payload)
    assert (status, out) == (403, {"error": "forbidden_origin"})
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
@pytest.mark.parametrize(
    "extra",
    [[f"Host: {EVIL}"], [f"Host: {HOST}", f"Origin: http://{EVIL}"], [f"Host: {HOST}", "Sec-Fetch-Site: cross-site"]],
)
@pytest.mark.parametrize("path", ["/api/ui/summary", "/ui/", "/leads", "/nothing"])
def test_head_options_foreign_headers_403_no_cors(env, method: str, extra: list[str], path: str) -> None:
    _, base = fresh(env)
    status, headers, body = wire(base, "\r\n".join([f"{method} {path} HTTP/1.1", *extra, "Connection: close"]))
    assert status == 403
    assert not any(k.startswith("access-control-") for k in headers)
    assert "allow" not in headers
    if method == "HEAD":
        assert body == b""


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_options_this_host_no_cors(env, method: str) -> None:
    _, base = fresh(env)
    status, headers, _ = wire(base, f"{method} /api/ui/summary HTTP/1.1\r\nHost: {HOST}\r\nConnection: close")
    assert status == 405
    assert not any(k.startswith("access-control-") for k in headers)


@pytest.mark.parametrize(
    ("host", "status"),
    [
        ("localhost.", 403),  # trailing dot is another name
        ("LOCALHOST", 200),  # names are not case sensitive
        (f"[{LOOP6_SHORT}]", 403),  # expanded IPv6 is another spelling: refused
        (f"[{LOOP6_FULL}]", 403),
        ("[::1]", 200),
        ("[::1", 403),
        ("localhost:99999", 403),  # not a port
        ("localhost:65536", 403),
        ("localhost:65535", 200),
        ("localhost:0", 200),
        ("local\x01host", 403),
        ("localhost\x00", 403),
        ("localhost\t", 200),  # surrounding blanks are not part of the name
        ("localhost:", 403),
        ("localhost:1:2", 403),
        ("127.1", 403),
        ("2130706433", 403),
        (WILDCARD4, 403),  # a Host header, never bound
    ],
)
def test_host_variants(env, host: str, status: int) -> None:
    _, base = fresh(env)
    assert wire(base, f"GET /counts HTTP/1.1\r\nHost: {host}\r\nConnection: close")[0] == status


def test_foreign_host_is_refused_on_every_route(env, monkeypatch) -> None:
    monkeypatch.setattr(portal_api, "STATIC", {"index.html": (PLACEHOLDER, "text/html; charset=utf-8")})
    rig, base = fresh(env)
    body = json.dumps({"source": "form", "source_ref": "x1", "payload": lead()}).encode()
    cl = f"Content-Length: {len(body)}"
    for line, extra, payload in (
        ("POST /leads HTTP/1.1", [cl], body),
        ("POST /review/Fx/retry HTTP/1.1", [], b""),
        ("POST /review/Fx/resolve HTTP/1.1", ["Content-Length: 2"], b"{}"),
        ("POST /leads/Lx/step HTTP/1.1", ["Content-Length: 2"], b"{}"),
        ("GET /ui/ HTTP/1.1", [], b""),
        ("GET /ui HTTP/1.1", [], b""),
        ("GET /api/ui/summary HTTP/1.1", [], b""),
        ("GET /api/demo/state HTTP/1.1", [], b""),
        ("GET /counts HTTP/1.1", [], b""),
        ("GET /review HTTP/1.1", [], b""),
        ("GET /nothing HTTP/1.1", [], b""),  # the gate runs before the route is known
        ("DELETE /leads HTTP/1.1", [], b""),
    ):
        for host in (EVIL, f"{EVIL}:{port_of(base)}", f"127.0.0.1.{EVIL}", f"user@{HOST}", "", f"{HOST}:abc"):
            assert raw(base, line, f"Host: {host}", *extra, body=payload) == (403, {"error": "forbidden_host"}), (
                line,
                host,
            )
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0, "a refused request wrote a receipt"
    assert rig.desk.rejected_envelopes() == 0


def test_error_bodies_match_the_fixtures(env) -> None:
    """The two error bodies the pages show are the ones in the fixtures (error-forbidden, error-retry-409)."""
    rig, base = fresh(env)
    ids = seed(rig)
    assert raw(base, "GET /api/ui/summary HTTP/1.1", f"Host: {HOST}", "Origin: null") == (
        403,
        fixture("error-forbidden"),
    )
    status, out = call(base, "POST", f"/review/F{ids['linus']}/retry", body={})
    assert (status, out) == (409, fixture("error-retry-409"))


def test_missing_and_repeated_host_header(env) -> None:
    _, base = fresh(env)
    assert raw(base, "GET /counts HTTP/1.0") == (403, {"error": "forbidden_host"})
    two = raw(base, "GET /counts HTTP/1.1", f"Host: {HOST}", f"Host: {EVIL}")
    assert two == (403, {"error": "forbidden_host"})


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", "[::1]", "127.0.0.1:1", "[::1]:9"])
def test_allowed_hosts_pass(env, host: str) -> None:
    _, base = fresh(env)
    assert raw(base, "GET /counts HTTP/1.1", f"Host: {host}")[0] == 200


def test_foreign_origin_refused_book_unchanged(env) -> None:
    rig, base = fresh(env)
    port = port_of(base)
    body = json.dumps({"source": "form", "source_ref": "o1", "payload": lead()}).encode()
    cl = f"Content-Length: {len(body)}"
    for origin in (
        f"http://{EVIL}",
        f"http://{EVIL}:{port}",
        "null",
        "",
        f"https://{HOST}:{port}",
        f"http://{HOST}",
        f"http://{HOST}:{int(port) + 1}",
        f"http://{HOST}:{port}/",
        f"http://{LOCAL}.{EVIL}:{port}",
    ):
        got = raw(base, "POST /leads HTTP/1.1", f"Host: {HOST}", f"Origin: {origin}", cl, body=body)
        assert got == (403, {"error": "forbidden_origin"}), origin
    two = raw(
        base, "POST /leads HTTP/1.1", f"Host: {HOST}", f"Origin: http://{HOST}:{port}", "Origin: null", cl, body=body
    )
    assert two == (403, {"error": "forbidden_origin"})
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0
    assert rig.desk.rejected_envelopes() == 0


@pytest.mark.parametrize("site", ["cross-site", "same-site", "bogus", ""])
def test_sec_fetch_site_other_is_refused(env, site: str) -> None:
    rig, base = fresh(env)
    body = json.dumps({"source": "form", "source_ref": "s1", "payload": lead()}).encode()
    got = raw(
        base,
        "POST /leads HTTP/1.1",
        f"Host: {HOST}",
        f"Sec-Fetch-Site: {site}",
        f"Content-Length: {len(body)}",
        body=body,
    )
    assert got == (403, {"error": "forbidden_origin"})
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0


def test_same_origin_and_no_origin_pass(env) -> None:
    rig, base = fresh(env)
    port = port_of(base)
    body = json.dumps({"source": "form", "source_ref": "p1", "payload": lead()}).encode()
    cl = f"Content-Length: {len(body)}"
    ok = raw(base, "POST /leads HTTP/1.1", f"Host: {HOST}", cl, body=body)  # n8n, scripts: no browser headers
    assert ok[0] == 201
    for extra in (
        [f"Origin: http://{HOST}:{port}", "Sec-Fetch-Site: same-origin"],
        [f"Origin: http://localhost:{port}"],
        ["Sec-Fetch-Site: none"],
    ):
        again = raw(base, "POST /leads HTTP/1.1", f"Host: {HOST}", *extra, cl, body=body)
        assert again[0] in (200, 201), extra  # a replay of the same request number is fine
    assert raw(base, "GET /api/ui/summary HTTP/1.1", f"Host: {HOST}", f"Origin: http://{HOST}:{port}")[0] == 200


def test_extra_allowed_host_from_config(env) -> None:
    name = "dock.example"
    rig, base = fresh(env, allowed_hosts=(name,))
    assert raw(base, "GET /counts HTTP/1.1", f"Host: {name}:{port_of(base)}")[0] == 200
    assert raw(base, "GET /counts HTTP/1.1", f"Host: {name}x")[0] == 403
    port = port_of(base)
    assert raw(base, "GET /counts HTTP/1.1", f"Host: {name}", f"Origin: http://{name}:{port}")[0] == 200
    _, plain = fresh(env)
    assert raw(plain, "GET /counts HTTP/1.1", f"Host: {name}")[0] == 403


def test_bind_allowed_wildcard_is_not() -> None:
    assert "[::1]" in server._allowed_hosts(Config(bind="::1"))
    assert "192.0.2.7" in server._allowed_hosts(Config(bind="192.0.2.7"))
    for wildcard in (WILDCARD4, "::"):
        names = server._allowed_hosts(Config(bind=wildcard))
        assert names == {"127.0.0.1", "localhost", "[::1]"}


def test_allowed_hosts_setting_is_validated() -> None:
    cfg = Config.from_env({"DESK_ALLOWED_HOSTS": "dock.example, [fe80::1] ,a-b.c"})
    assert cfg.allowed_hosts == ("dock.example", "[fe80::1]", "a-b.c")
    assert Config.from_env({}).allowed_hosts == () and Config.from_env({"DESK_ALLOWED_HOSTS": " "}).allowed_hosts == ()
    wildcards = (WILDCARD4, "[::]", "::", "[" + ":".join(["0"] * 8) + "]")
    for bad in (
        "Dock.Example",
        "a b",
        "a,,b",
        "a_b",
        "x/y",
        "http:" + "//a",
        "a:80",
        "*",
        "a" * 254,
        "[zz]",
        "[]",
        ".",
        "-",
        "--",
        "a..b",
        ".a",
        "a.",
        "-a",
        "a-",
        "a.-b.c",
        "a" * 64,
        *wildcards,
    ):
        with pytest.raises(ValueError, match="DESK_ALLOWED_HOSTS"):
            Config.from_env({"DESK_ALLOWED_HOSTS": bad})
    with pytest.raises(ValueError, match="DESK_ALLOWED_HOSTS"):
        Config(allowed_hosts=("UPPER",))
    assert Config(allowed_hosts=("a" * 63, "192.0.2.7", "x-y.z9")).allowed_hosts[0] == "a" * 63


def test_startup_warning_non_loopback_bind() -> None:
    for quiet in ("127.0.0.1", "localhost", "::1", "[::1]"):
        assert server._is_loopback_bind(quiet), quiet
    for loud in (WILDCARD4, "::", "192.0.2.7", "198.51.100.4", "desk.example"):
        assert not server._is_loopback_bind(loud), loud


# -- the demo extension door
class FakeDemo:
    assets = {"portal_demo.js": (b"/*x*/", "text/javascript; charset=utf-8")}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        self.calls.append((method, path, body))
        return 200, {"ok": True}


def test_no_demo_extension_means_404(env, monkeypatch) -> None:
    """A desk without the demo extension (the folder deleted) answers 404 on every demo path; the loader
    is forced to find none, so the test holds whether or not `demo_only/` is present in the tree."""
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: None)
    _, base = fresh(env)
    assert call(base, "GET", "/api/demo/state") == (404, {"error": "not_found"})
    assert call(base, "POST", "/api/demo/burst", body={"n": 1}) == (404, {"error": "not_found"})
    assert get(base, "/ui/demo/portal_demo.js") == (404, {"error": "not_found"})
    assert get(base, "/api/demo/")[0] == 404


def test_demo_extension_gets_capped_body(env, monkeypatch) -> None:
    demo = FakeDemo()
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: demo)
    _, base = fresh(env)
    assert call(base, "GET", "/api/demo/state") == (200, {"ok": True})
    assert call(base, "POST", "/api/demo/burst", body={"n": 1}) == (200, {"ok": True})
    for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):  # only GET and POST reach the extension
        assert call(base, method, "/api/demo/x/y", body={"a": 1} if method != "OPTIONS" else None) == (
            405,
            {"error": "method_not_allowed"},
        ), method
    assert [(m, p) for m, p, _ in demo.calls] == [("GET", "state"), ("POST", "burst")]
    assert demo.calls[1][2] == b'{"n": 1}' and demo.calls[0][2] == b""
    exact = json.dumps({"pad": "x" * (DEMO_BODY_MAX - len('{"pad": ""}'))}).encode()
    assert len(exact) == DEMO_BODY_MAX
    assert call(base, "POST", "/api/demo/burst", raw=exact)[0] == 200 and demo.calls[-1][2] == exact
    big = json.dumps({"pad": "x" * (DEMO_BODY_MAX * 2)}).encode()
    assert call(base, "POST", "/api/demo/burst", raw=big) == (413, {"error": "too_large"})
    assert len(demo.calls) == 3  # the over-long body never reached the extension
    status, out = raw(
        base,
        "POST /api/demo/burst HTTP/1.1",
        f"Host: {HOST}",
        "Content-Type: text/plain",
        "Content-Length: 2",
        body=b"{}",
    )
    assert (status, out) == (400, {"error": "bad_body"})
    assert len(demo.calls) == 3  # the refused call never reached the extension


@pytest.mark.parametrize("path", ["/api/demo/burst", "/api/demo/crash"])
def test_demo_posts_refuse_text_plain_and_foreign_origin(env, monkeypatch, path: str) -> None:
    """A text/plain body is a 400, and a foreign Origin, Origin null or a cross-site fetch is a 403, before the
    extension sees anything: no request reaches it."""
    demo = FakeDemo()
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: demo)
    _, base = fresh(env)
    body = b'{"point": "A"}'
    cl = f"Content-Length: {len(body)}"
    status, out = raw(base, f"POST {path} HTTP/1.1", f"Host: {HOST}", "Content-Type: text/plain", cl, body=body)
    assert (status, out) == (400, {"error": "bad_body"})
    for extra in ([f"Origin: http://{EVIL}"], ["Origin: null"], ["Sec-Fetch-Site: cross-site"]):
        got = raw(
            base, f"POST {path} HTTP/1.1", f"Host: {HOST}", "Content-Type: application/json", cl, *extra, body=body
        )
        assert got == (403, {"error": "forbidden_origin"}), extra
    assert demo.calls == []


def test_demo_door_408_when_body_stalls(env, monkeypatch) -> None:
    demo = FakeDemo()
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: demo)
    monkeypatch.setattr(server._Handler, "timeout", 1)
    _, base = fresh(env)
    host, port = base.removeprefix("http://").split(":")
    head = (
        f"POST /api/demo/burst HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
        "Content-Length: 50\r\nConnection: close\r\n\r\n{"
    )
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(head.encode())  # 1 of 50 announced bytes, then silence
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    assert data.split(b" ", 2)[1] == b"408" and b"request_timeout" in data
    assert demo.calls == []


def test_demo_asset_is_served_from_the_extension(env, monkeypatch) -> None:
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: FakeDemo())
    _, base = fresh(env)
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(f"GET /ui/demo/portal_demo.js HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    assert b" 200 " in head.split(b"\r\n")[0] and body == b"/*x*/"
    assert b"text/javascript; charset=utf-8" in head and b"nosniff" in head
    assert raw(base, "GET /ui/demo/other.js HTTP/1.1", f"Host: {HOST}") == (404, {"error": "not_found"})


# -- the loader
def test_loader_none_only_when_module_absent(monkeypatch) -> None:
    rig_desk = types.SimpleNamespace(config=types.SimpleNamespace(adapters="demo", real_services=()))
    for absent in ("demo_only", "demo_only.portal_demo"):

        def gone(name: str, _a: str = absent) -> Any:
            raise ModuleNotFoundError("no module", name=_a)

        monkeypatch.setattr(loader.importlib, "import_module", gone)
        assert loader.load_demo_portal(rig_desk) is None  # type: ignore[arg-type]

    def other(name: str) -> Any:
        raise ModuleNotFoundError("no module", name="somewhere_else")

    monkeypatch.setattr(loader.importlib, "import_module", other)
    with pytest.raises(ModuleNotFoundError):
        loader.load_demo_portal(rig_desk)  # type: ignore[arg-type]

    def broken(name: str) -> Any:
        raise ImportError("broken extension")

    monkeypatch.setattr(loader.importlib, "import_module", broken)
    with pytest.raises(ImportError, match="broken"):
        loader.load_demo_portal(rig_desk)  # type: ignore[arg-type]

    marker = types.SimpleNamespace(make=lambda desk: ("made", desk))
    monkeypatch.setattr(loader.importlib, "import_module", lambda name: marker)
    assert loader.load_demo_portal(rig_desk) == ("made", rig_desk)  # type: ignore[arg-type]
    other_adapters = types.SimpleNamespace(config=types.SimpleNamespace(adapters="live", real_services=()))
    assert loader.load_demo_portal(other_adapters) is None  # type: ignore[arg-type]  # no demo door off the demo adapters


# -- load: many rows, many readers
def test_over_the_cap_gets_503_busy(env, monkeypatch) -> None:
    _, base = fresh(env)
    monkeypatch.setattr(portal_api, "_INFLIGHT", threading.BoundedSemaphore(PORTAL_MAX_INFLIGHT))
    gate, inside = threading.Event(), threading.Semaphore(0)
    real = portal_api._summary

    def slow(desk: Any, started_at: int, *rest: Any) -> Any:  # *rest: the summary also takes the demo flag
        inside.release()
        gate.wait(timeout=30)
        return real(desk, started_at, *rest)

    monkeypatch.setattr(portal_api, "_summary", slow)
    results: list[tuple[int, Any]] = []
    held = [
        threading.Thread(target=lambda: results.append(get(base, "/api/ui/summary")), daemon=True)
        for _ in range(PORTAL_MAX_INFLIGHT)
    ]
    for t in held:
        t.start()
    for _ in range(PORTAL_MAX_INFLIGHT):
        assert inside.acquire(timeout=30), "the replies did not all start"
    try:
        assert get(base, "/api/ui/summary") == (503, fixture("error-busy"))  # one over the cap
        assert get(base, "/counts")[0] == 200  # the desk's own routes are not capped
    finally:
        gate.set()
        for t in held:
            t.join(timeout=30)
    assert [status for status, _ in results] == [200] * PORTAL_MAX_INFLIGHT
    assert get(base, "/api/ui/summary")[0] == 200  # the slots came back


def test_big_book_is_fast_and_cheap(env, monkeypatch) -> None:
    """3 000 leads with 2 repeats each (9 000 receipts). Timings are printed for the reader; the bound is generous
    so a slow machine does not fail it, but a per-row scan of all receipts (a repeats column kept per row) does."""
    rig, base = fresh(env)
    for i in range(BIG_LEADS):
        rig.desk.process("form", f"big-{i}", lead(name=f"Person {i:05d}", email=f"p{i}@example.test"))
    for i in range(BIG_LEADS):
        for door in ("webhook", "form"):
            rig.desk.process(door, f"big-{i}-{door}-r", lead(name=f"Person {i:05d}", email=f"p{i}@example.test"))
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 3 * BIG_LEADS
    ids = [r[0] for r in rig.query("SELECT lead_id FROM leads ORDER BY created_at, lead_id LIMIT 3")]
    sql(rig, "UPDATE followups SET state='failed', last_error='refused', updated_at=? WHERE lead_id=?", (5, ids[2]))
    seen: list[int] = []
    real = portal_api.connect_readonly

    class Count:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn, self.n = conn, 0
            seen.append(0)

        def execute(self, text: str, *a: Any) -> Any:
            seen[-1] += 1
            return self.conn.execute(text, *a)

        def close(self) -> None:
            self.conn.close()

    monkeypatch.setattr(portal_api, "connect_readonly", lambda p: Count(real(p)))
    timings: dict[str, float] = {}
    for name, path, budget in (
        ("summary", "/api/ui/summary", 8),
        ("leads", "/api/ui/leads", 6),
        ("leads?limit=100", "/api/ui/leads?limit=100", 6),
        ("leads?q=Person 0100", "/api/ui/leads?q=Person%200100", 6),
        ("leads?state=sent", "/api/ui/leads?state=sent&limit=100&offset=2000", 6),
        ("lead detail", f"/api/ui/leads/{ids[0]}", 10),  # one more read: the lead flow state row
        ("review", "/api/ui/review", 6),
        ("log", "/api/ui/log?limit=200", 5),
    ):
        t0 = time.monotonic()
        status, out = get(base, path)
        timings[name] = time.monotonic() - t0
        assert status == 200, (name, out)
        assert seen[-1] <= budget, (name, seen[-1], "statements incl. BEGIN and COMMIT")
        assert timings[name] < PERF_LIMIT_SECONDS, (name, timings[name])
    page = get(base, "/api/ui/leads?limit=100")[1]
    assert page["total"] == BIG_LEADS and all(r["repeats"] == 2 for r in page["rows"])
    print("big book timings (s): " + ", ".join(f"{k}={v:.3f}" for k, v in timings.items()))


# -- concurrency and restarts
def test_burst_while_summary_is_polled(env) -> None:
    """10 simultaneous leads while Overview polls: no 5xx anywhere, and every poll adds up."""
    rig, base = fresh(env)
    stop = threading.Event()
    polls: list[tuple[int, Any]] = []

    def poll() -> None:
        while not stop.is_set():
            polls.append(get(base, "/api/ui/summary"))
            polls.append(get(base, "/api/ui/review"))

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    try:
        fns = [
            (lambda i=i: call(base, "POST", "/leads", {"source": "form", "source_ref": f"b-{i}", "payload": lead()}))
            for i in range(BURST)
        ]
        results = run_threads(fns)
    finally:
        wait_until(lambda: len(polls) > 2, what="the poller to run")
        stop.set()
        poller.join(timeout=30)
    assert not poller.is_alive()
    statuses = [r[0] for r in results]
    assert all(s < 500 for s in statuses) and sorted(set(statuses)) == [200, 201], statuses
    assert all(status < 500 for status, _ in polls), [s for s, _ in polls if s >= 500]
    summaries = [body for status, body in polls if "counts" in body]
    assert summaries and all(s["reconciled"] is True for s in summaries)
    for s in summaries:
        c = s["counts"]
        assert c["received"] == c["new"] + c["duplicate"] + c["review"] + c["open"]
    final = get(base, "/api/ui/summary")[1]
    assert (
        final["counts"]["received"] == BURST
        and final["counts"]["new"] == 1
        and final["counts"]["duplicate"] == BURST - 1
    )
    assert final["reconciled"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="in-process variant; the real-process restart runs everywhere")
def test_restart_same_port_portal_open(env) -> None:
    """20 stops and starts on one fixed port while a page keeps polling. Every bind must succeed at once."""
    rig, _ = fresh(env)
    rig.desk.handle("form", "r-1", lead())
    port = free_port()
    cfg = Config(db_path=rig.db_path, port=port, replies_path=rig.replies_path)
    seconds: list[float] = []
    for _ in range(RESTARTS):
        t0 = time.monotonic()
        srv = make_server(rig.desk, cfg)  # raises OSError when the port cannot be bound
        seconds.append(time.monotonic() - t0)
        start_serving(srv)  # returns once the loop really runs: a shutdown can never wait on a loop not yet started
        url = f"http://{HOST}:{port}"
        try:
            for _poll in range(3):  # the open page: connections that end in TIME_WAIT on this port
                status, out = get(url, "/api/ui/summary")
                assert status == 200 and out["counts"]["received"] == 1
        finally:
            stop_serving(srv, what="restart test server")
    print(f"restart x{RESTARTS}: bind seconds max={max(seconds):.4f} mean={sum(seconds) / len(seconds):.4f}")
    assert max(seconds) < server.REQUEST_TIMEOUT_SECONDS


def _desk_env(db: Path, port: int, fakes_url: str, replies: Path, bind: str = HOST) -> dict[str, str]:
    return child_env(
        DESK_DB_PATH=str(db),
        DESK_BIND=bind,
        DESK_PORT=str(port),
        DESK_FAKES_URL=fakes_url,
        DESK_REPLIES_PATH=str(replies),
    )


def _wait_for_summary(proc: Any, port: int) -> float:
    """Seconds until GET /api/ui/summary answers 200 (the desk is bound and serving)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < READY_SECONDS:
        if proc.poll() is not None:
            raise AssertionError("the desk exited before it served: " + str(proc.returncode))
        try:
            if call(f"http://{HOST}:{port}", "GET", "/api/ui/summary", timeout=2)[0] == 200:
                return time.monotonic() - t0
        except OSError:
            time.sleep(0.01)
    raise AssertionError("the desk did not serve in time")


def test_real_process_restart_x20(env, tmp_path) -> None:
    """The real `python -m lead_desk.server` process, started and stopped 20 times on one fixed port while the page
    polls. Every start must bind and serve; the seconds from start to the first good answer are printed."""
    _, fakes_url = env.fakes("keyed")
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    db, port = tmp_path / "desk.sqlite3", free_port()
    ready: list[float] = []
    for n in range(RESTARTS):
        proc = start_proc(["-m", "lead_desk.server"], _desk_env(db, port, fakes_url, replies))
        try:
            ready.append(_wait_for_summary(proc, port))
            for _poll in range(3):  # the open page: connections that end in TIME_WAIT on this port
                assert call(f"http://{HOST}:{port}", "GET", "/api/ui/review")[0] == 200
        finally:
            out = stop_proc(proc)
        assert b"cannot bind" not in out, (n, out.decode(errors="replace")[-300:])
        assert b"warning:" not in out  # a loopback bind prints no exposure warning
    print(
        f"real-process restart x{RESTARTS}: ready seconds "
        + " ".join(f"{x:.3f}" for x in ready)
        + f" | max={max(ready):.3f} mean={sum(ready) / len(ready):.3f}"
    )
    assert max(ready) < READY_SECONDS


def test_real_process_warns_when_bound_beyond_loopback(env, tmp_path) -> None:
    _, fakes_url = env.fakes("keyed")
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    port = free_port()
    wildcard = str(ipaddress.IPv4Address(0))
    proc = start_proc(
        ["-m", "lead_desk.server"], _desk_env(tmp_path / "desk.sqlite3", port, fakes_url, replies, wildcard)
    )
    try:
        _wait_for_summary(proc, port)  # reached by loopback, which the gate allows even for a wildcard bind
        foreign = raw(f"http://{HOST}:{port}", "GET /api/ui/summary HTTP/1.1", f"Host: {WILDCARD4}")
        assert foreign == (403, {"error": "forbidden_host"})  # a wildcard is never a Host name
    finally:
        out = stop_proc(proc).decode(errors="replace")
    assert "warning: the desk is bound to" in out and "every lead" in out and "Docker" not in out


def test_broken_extension_leaves_no_socket(env, monkeypatch) -> None:
    rig, _ = fresh(env)
    port = free_port()

    def broken(_desk: Any) -> Any:
        raise ImportError("broken extension")

    monkeypatch.setattr(server, "load_demo_portal", broken)
    cfg = Config(db_path=rig.db_path, port=port, replies_path=rig.replies_path)
    with pytest.raises(ImportError):
        make_server(rig.desk, cfg)
    with socket.socket() as probe:  # the port is free: nothing was left listening
        probe.bind((HOST, port))


def test_feed_calls_loopback_for_wildcard() -> None:
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")  # feed.py imports its sibling http_guard
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    feed = importlib.import_module("scripts.feed")
    assert feed.target_host(str(ipaddress.IPv4Address(0))) == "127.0.0.1"
    assert feed.target_host("::") == "[::1]"
    assert feed.target_host("127.0.0.1") == "127.0.0.1"
    assert feed.target_host("::1") == "[::1]"
    assert feed.target_host("desk.example") == "desk.example"


def test_all_digit_bind_is_wildcard() -> None:
    assert server._allowed_hosts(Config(bind="0")) == {"127.0.0.1", "localhost", "[::1]"}
    for short in ("0.0", "00", "0x0", "0.0.0"):
        assert server._allowed_hosts(Config(bind=short)) == {"127.0.0.1", "localhost", "[::1]"}, short
    assert is_unspecified("0") and is_unspecified("0x0") and not is_unspecified("127.1")
    assert not is_unspecified("a\x00b") and not is_unspecified("a\udc80b")  # unreadable names: no traceback
    assert server._allowed_hosts(Config(bind="host.example")) >= {"host.example"}
    with pytest.raises(ValueError):
        Config(allowed_hosts=("0",))


def test_expect_refusal_does_not_wait(env) -> None:
    """The desk answers as HTTP/1.0 and never sends 100. A refused request that announced a body with Expect gets
    its 403 at once, without waiting for the body the client is holding back."""
    _, base = fresh(env)
    host, port = base.removeprefix("http://").split(":")
    head = (
        "POST /leads HTTP/1.1\r\nHost: evil.example\r\nExpect: 100-continue\r\n"
        "Content-Type: application/json\r\nContent-Length: 50\r\nConnection: close\r\n\r\n"
    )
    t0 = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(head.encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    first = data.split(b"\r\n", 1)[0]
    assert b" 403 " in first and b" 100 " not in first, first
    assert b"forbidden_host" in data and time.monotonic() - t0 < 3.0


def test_refusal_survives_a_body_sent_anyway(env) -> None:
    """A client that sends its body although it was refused still reads the whole 403 (no reset destroys it)."""
    _, base = fresh(env)
    host, port = base.removeprefix("http://").split(":")
    body = b"x" * 200_000
    for _ in range(20):
        head = (
            f"POST /leads HTTP/1.1\r\nHost: evil.example\r\nExpect: 100-continue\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        with socket.create_connection((host, int(port)), timeout=10) as s:
            s.sendall(head.encode() + body)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        assert data.startswith(b"HTTP/1.0 403 ") and data.endswith(b'"forbidden_host"}'), data[-80:]


def test_trickle_is_cut_at_drain_deadline(env) -> None:
    """A client that keeps sending one byte every 0.2 s while its request is refused gets the 403 within the total drain
    limit (per-read waits would add up without a bound). Covers `_drain` only: the client sees the half-close at
    once, so `finish()`'s own deadline cannot be observed from the client side."""
    _, base = fresh(env)
    host, port = base.removeprefix("http://").split(":")
    stop = threading.Event()
    head = "POST /leads HTTP/1.1\r\nHost: evil.example\r\nContent-Length: 100000\r\nConnection: close\r\n\r\n"
    t0 = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=15) as s:
        s.sendall(head.encode())

        def drip() -> None:
            try:
                while not stop.is_set():
                    s.sendall(b"x")
                    time.sleep(0.2)
            except OSError:
                pass

        t = threading.Thread(target=drip, daemon=True)
        t.start()
        data = b""
        try:
            while chunk := s.recv(65536):
                data += chunk
        finally:
            stop.set()
            t.join(timeout=2)
    assert data.startswith(b"HTTP/1.0 403 ") and data.endswith(b'"forbidden_host"}'), data[-80:]
    assert time.monotonic() - t0 < 2.5


def test_stored_nan_never_reaches_the_page(env) -> None:
    """The door stores a bare NaN token (Python's JSON reader accepts it); the page must stay strict JSON."""
    rig, base = fresh(env)
    ids = seed(rig)
    assert rig.desk.handle("webhook", "nan-1", {**lead(), "phone": float("nan")}).body["verdict"] == "review"
    port = base.rsplit(":", 1)[1]
    for path in (f"/api/ui/leads/{ids['ada']}", "/api/ui/review", "/api/ui/summary", "/api/ui/leads"):
        status, body = raw_exchange_text(
            base, f"GET {path} HTTP/1.1\r\nHost: {HOST}:{port}\r\nConnection: close\r\n\r\n"
        )
        assert status == 200 and "NaN" not in body and "Infinity" not in body
        json.loads(body, parse_constant=lambda c: pytest.fail(f"bare {c}"))
    assert portal_api._object('{"a": NaN, "b": -Infinity, "c": 1}') == {"a": None, "b": None, "c": 1}


def raw_exchange_text(base: str, request: str) -> tuple[int, str]:
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(request.encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body.decode()


def test_retry_on_unknown_is_409(env) -> None:
    """The UNKNOWN card offers Retry; the desk answers 409 unknown_needs_person and leaves the row as it was."""
    rig, base = fresh(env)
    ids = seed(rig)
    for key in ("linus", "grete"):
        card = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["item_id"] == "F" + ids[key])
        retry = card["actions"][0]
        assert retry["id"] == "retry" and retry["endpoint"] == f"/review/F{ids[key]}/retry"
        row = "SELECT * FROM followups WHERE lead_id=?"
        before, sent_before = rig.query(row, (ids[key],)), rig.outbox.count
        status, out = call(base, "POST", retry["endpoint"])
        assert status == 409 and out["error"] == "unknown_needs_person"
        assert rig.query(row, (ids[key],)) == before and rig.outbox.count == sent_before
        assert before[0][1] == "unknown"


# -- the lead panel's next step says to check the mail step first (the card says it beside its button instead)
def test_failed_lead_that_may_have_sent_gets_the_cards_caution_in_the_panel(env) -> None:
    """The panel says the email may have been sent and to check the mail step first, not only "press Retry"."""
    rig, base = fresh(env)
    ids = seed(rig)
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error='ambiguous', ambiguous_prior=1 WHERE lead_id=?",
        (ids["alan"],),
    )
    step = get(base, f"/api/ui/leads/{ids['alan']}")[1]["next_step"]
    assert "may already have been sent" in step and step.lower().count("check the mail step") == 1
    card = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["lead_id"] == ids["alan"])
    assert (
        card["reason_text"] == portal_api._DID_NOT_FINISH_TEXT
    )  # what happened, once; the card's button cue says "check"
    plain = get(base, f"/api/ui/leads/{ids['alan']}")[1]["next_step"]
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error='refused', ambiguous_prior=0 WHERE lead_id=?",
        (ids["alan"],),
    )
    refused = get(base, f"/api/ui/leads/{ids['alan']}")[1]["next_step"]
    assert plain != refused and "gone out" not in refused  # the refused-mail case keeps its simple line


def test_unknown_lead_says_which_part_is_unclear(env) -> None:
    rig, base = fresh(env)
    ids = seed(rig)
    assert get(base, f"/api/ui/leads/{ids['grete']}")[1]["next_step"].startswith("Unclear whether the CRM")
    assert get(base, f"/api/ui/leads/{ids['linus']}")[1]["next_step"].startswith("Unclear whether the email")
    assert get(base, f"/api/ui/leads/{ids['ada']}")[1]["next_step"] is None


def test_chip_state_separates_certain_refusal_maybe_left_and_crm_unconfirmed(env) -> None:
    """A failed send that may have left is not chipped Failed like a certain refusal, and an unknown about the CRM is
    not chipped Outcome unknown (the email's word). Every surface carries state_view."""
    rig, base = fresh(env)
    ids = seed(rig)
    assert get(base, f"/api/ui/leads/{ids['grete']}")[1]["state_view"] == "crm_unconfirmed"
    assert get(base, f"/api/ui/leads/{ids['linus']}")[1]["state_view"] == "unknown"
    assert get(base, f"/api/ui/leads/{ids['ada']}")[1]["state_view"] == "sent"
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error='ambiguous', ambiguous_prior=1 WHERE lead_id=?",
        (ids["alan"],),
    )
    assert get(base, f"/api/ui/leads/{ids['alan']}")[1]["state_view"] == "did_not_finish"
    rows = {r["lead_id"]: r for r in get(base, "/api/ui/leads?limit=100")[1]["rows"]}
    assert rows[ids["alan"]]["state_view"] == "did_not_finish" and "last_error" not in rows[ids["alan"]]
    assert rows[ids["grete"]]["state_view"] == "crm_unconfirmed"
    card = next(i for i in get(base, "/api/ui/review")[1]["items"] if i["lead_id"] == ids["alan"])
    assert card["state_view"] == "did_not_finish" and card["title"] == "Follow-up did not finish"
    log = {r["lead_id"]: r for r in get(base, "/api/ui/log?limit=100")[1]["items"] if r["lead_id"]}
    assert log[ids["alan"]]["state_view"] == "did_not_finish"
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error='refused', ambiguous_prior=0 WHERE lead_id=?",
        (ids["alan"],),
    )
    assert get(base, f"/api/ui/leads/{ids['alan']}")[1]["state_view"] == "failed"


def test_overview_counts_and_feed_use_the_same_state_words_as_the_other_pages(env) -> None:
    """The follow-up counts and the live feed say Did not finish / CRM not confirmed for a lead whose email may have
    left or whose CRM write is unclear, the same words as the lead pages."""
    rig, base = fresh(env)
    ids = seed(rig)
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error='ambiguous', ambiguous_prior=1 WHERE lead_id=?",
        (ids["alan"],),
    )
    sql(rig, "UPDATE followups SET updated_at = 9999999999 WHERE lead_id IN (?, ?)", (ids["alan"], ids["grete"]))
    out = get(base, "/api/ui/summary")[1]
    f = out["followups"]
    assert f["did_not_finish"] == 1 and f["crm_unconfirmed"] == 1  # alan and grete
    assert f["failed"] == 0 and f["unknown"] == 1  # only linus is the email's outcome unknown
    leads = get(base, "/api/ui/leads?limit=100")[1]["total"]
    assert sum(f.values()) == leads  # the groups still add up to the leads
    views = {r["name"]: r["state_view"] for r in out["recent"] if r["kind"] == "followup"}
    assert "did_not_finish" in views.values() and "crm_unconfirmed" in views.values()
    assert all(r["state_view"] is None for r in out["recent"] if r["kind"] != "followup")


def test_failed_lead_panel_banner_matches_its_chip_words(env) -> None:
    rig, base = fresh(env)
    ids = seed(rig)
    sql(
        rig,
        "UPDATE followups SET state='failed', last_error='ambiguous', ambiguous_prior=1 WHERE lead_id=?",
        (ids["alan"],),
    )
    step = get(base, f"/api/ui/leads/{ids['alan']}")[1]["next_step"]
    assert step.startswith("Did not finish, and the email may already have been sent.") and "Failed, but" not in step


def test_data_map_lists_the_lead_number(env) -> None:
    rig, _ = fresh(env)
    row = next(r for r in portal_api._data_map(rig.desk)["stored"] if r["field"] == "lead_id")
    assert "AI" in row["sent_to"]  # the lead number is given to the AI, so the page must list it
