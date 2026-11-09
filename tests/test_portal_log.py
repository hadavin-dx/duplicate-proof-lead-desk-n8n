"""Tests of GET /api/ui/log (the request record) and GET /api/ui/data-map (what the AI step can read). Made-up data
only. Shapes are checked against tests/portal_fixtures/log*.json and data-map.json."""

from __future__ import annotations

import inspect
import sqlite3
from dataclasses import replace
from typing import Any

import pytest
from support import HOST, call, lead
from test_portal_api import EVIL, fixture, fresh, get, raw, same_shape, seed, sql

from demo_only.fakes import RecordedQualifier
from lead_desk import contract, portal_api
from lead_desk.constants import (
    AI_INPUT_FIELDS,
    AI_STEP_NOTE,
    LEAD_BOOKKEEPING_COLUMNS,
    LEAD_CONTENT_FIELDS,
    LOG_LIMIT_DEFAULT,
    LOG_LIMIT_MAX,
    PORTAL_ACTIONS,
    QUALIFIER_MODE_FALLBACK,
    QUALIFIER_MODES,
)

LOG_KEYS = {
    "request_id",
    "received_at",
    "door",
    "result",
    "lead_id",
    "lead_name",
    "followup_state",
    "new_details",
    "state_view",
}
SEEDED = 20  # receipts made by seed(): 7 new, 10 duplicate, 2 review, 1 open


def page_all(base: str, limit: int) -> list[dict[str, Any]]:
    """Every row, following `next` until it is null."""
    out: list[dict[str, Any]] = []
    path = f"/api/ui/log?limit={limit}"
    for _ in range(SEEDED + 5):
        status, body = get(base, path)
        assert status == 200, body
        out += body["items"]
        if body["next"] is None:
            return out
        path = f"/api/ui/log?limit={limit}&before={body['next']}"
    raise AssertionError("next never became null")


# -- /api/ui/log
def test_log_shape_and_results(env) -> None:
    rig, base = fresh(env)
    ids = seed(rig)
    status, out = get(base, "/api/ui/log")
    assert status == 200 and same_shape(out, fixture("log"))
    assert out["total"] == SEEDED and len(out["items"]) == SEEDED and out["next"] is None
    assert all(set(i) == LOG_KEYS for i in out["items"])
    results = [i["result"] for i in out["items"]]
    assert {r: results.count(r) for r in ("new", "duplicate", "review", "open")} == {
        "new": 7,
        "duplicate": 10,
        "review": 2,
        "open": 1,
    }
    keys = [(i["received_at"], i["request_id"]) for i in out["items"]]
    assert [i["received_at"] for i in out["items"]] == sorted((k[0] for k in keys), reverse=True)  # newest first
    for i in out["items"]:
        assert i["door"] in ("form", "webhook")
        if i["result"] in ("new", "duplicate"):
            assert i["lead_id"] and i["lead_name"] and i["followup_state"] in portal_api.FOLLOWUP_STATES
        else:
            assert (i["lead_id"], i["lead_name"], i["followup_state"]) == (None, None, None)
    by_ref = {i["request_id"]: i for i in out["items"]}
    assert by_ref["ui-alan"]["followup_state"] == "failed" and by_ref["ui-alan"]["lead_id"] == ids["alan"]
    assert by_ref["ui-g-5"]["result"] == "duplicate" and by_ref["ui-g-5"]["lead_name"] == "Grace Sample"
    assert by_ref["ui-open"]["result"] == "open" and by_ref["ui-fault"]["result"] == "review"


def test_log_empty_book(env) -> None:
    _, base = fresh(env)
    status, out = get(base, "/api/ui/log")
    assert status == 200 and out == fixture("log-empty") and same_shape(out, fixture("log-empty"))


def test_log_paging_with_ties(env) -> None:
    rig, base = fresh(env)
    seed(rig)
    whole = get(base, f"/api/ui/log?limit={LOG_LIMIT_MAX}")[1]["items"]
    stamps = [i["received_at"] for i in whole]
    assert len(set(stamps)) < len(stamps), "the seed must hold receipts with the same received_at"
    for size in (1, 2, 3, 7, SEEDED - 1, SEEDED, SEEDED + 1):
        paged = page_all(base, size)
        assert [i["request_id"] for i in paged] == [i["request_id"] for i in whole], size  # no gap, no repeat
    last = get(base, f"/api/ui/log?limit={SEEDED}")[1]
    assert last["next"] is None  # exactly one full page: nothing more
    assert get(base, f"/api/ui/log?limit={SEEDED - 1}")[1]["next"] is not None
    first = get(base, "/api/ui/log?limit=3")[1]
    at, rid = first["next"].split(":")
    assert (int(at), int(rid)) == tuple(
        rig.query(
            "SELECT received_at, receipt_id FROM receipts WHERE source_ref = ?", (first["items"][-1]["request_id"],)
        )[0]
    )


def test_log_default_and_cap(env) -> None:
    rig, base = fresh(env)
    for i in range(LOG_LIMIT_DEFAULT + 5):
        sql(
            rig,
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form', ?, ?, '{}')",
            (f"cap-{i}", rig.clock.t - i),
        )
    out = get(base, "/api/ui/log")[1]
    assert len(out["items"]) == LOG_LIMIT_DEFAULT and out["next"] is not None and out["total"] == LOG_LIMIT_DEFAULT + 5
    assert len(get(base, f"/api/ui/log?limit={LOG_LIMIT_MAX}")[1]["items"]) == LOG_LIMIT_DEFAULT + 5
    assert get(base, f"/api/ui/log?limit={LOG_LIMIT_MAX + 1}") == (400, {"error": "bad_query", "field": "limit"})


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/api/ui/log?limit=0", "limit"),
        ("/api/ui/log?limit=-1", "limit"),
        ("/api/ui/log?limit=", "limit"),
        ("/api/ui/log?limit=x", "limit"),
        ("/api/ui/log?limit=" + "9" * 40, "limit"),
        ("/api/ui/log?limit=" + "0" * 5000, "limit"),  # leading zeros must not reach int()
        ("/api/ui/log?before=" + "0" * 5000 + ":1", "before"),
        ("/api/ui/log?limit=1&limit=2", "limit"),
        ("/api/ui/log?before=", "before"),
        ("/api/ui/log?before=abc", "before"),
        ("/api/ui/log?before=1", "before"),
        ("/api/ui/log?before=1:", "before"),
        ("/api/ui/log?before=:1", "before"),
        ("/api/ui/log?before=" + ":".join(["1", "2", "3"]), "before"),  # three parts
        ("/api/ui/log?before=-1:2", "before"),
        ("/api/ui/log?before=1:-2", "before"),
        ("/api/ui/log?before=1.5:2", "before"),
        ("/api/ui/log?before=%201:2", "before"),
        ("/api/ui/log?before=1:2%20", "before"),
        ("/api/ui/log?before=%D9%A1:%D9%A2", "before"),  # non-ASCII digits
        ("/api/ui/log?before=" + "9" * 19 + ":1", "before"),
        ("/api/ui/log?before=1:" + "9" * 19, "before"),
        ("/api/ui/log?before=1:2&before=3:4", "before"),
        ("/api/ui/log?sort=name", "sort"),
        ("/api/ui/log?limit=1&q=a", "q"),
        ("/api/ui/data-map?x=1", "x"),
    ],
)
def test_log_bad_query_is_400(env, path: str, field: str) -> None:
    _, base = fresh(env)
    assert get(base, path) == (400, {"error": "bad_query", "field": field})


def test_log_cursor_edges_are_fine(env) -> None:
    rig, base = fresh(env)
    seed(rig)
    top = "9" * 18
    assert get(base, f"/api/ui/log?before={top}:{top}")[1]["total"] == SEEDED  # nothing is above it: all rows
    assert get(base, "/api/ui/log?before=0:0")[1]["items"] == []  # nothing is below it
    assert get(base, f"/api/ui/log?before={top}:{top}&limit=2")[1]["items"] != []


def test_log_shows_no_personal_data(env) -> None:
    rig, base = fresh(env)
    canary = {
        "name": "Cana Ry-Name",
        "email": "canary.mail@example.test",
        "phone": "555 0142",
        "company": "Canary Works Ltd",
        "message": "canary message text",
    }
    first = rig.desk.process("form", "pii-1", lead(**canary)).body["lead_id"]
    other = {**canary, "phone": "555 0143", "message": "second canary message", "company": "Canary Two"}
    rig.desk.process("webhook", "pii-2", lead(**other))  # a repeat with new details: its payload differs
    rig.desk.process("form", "pii-bad", lead(**{**canary, "email": None}))  # incomplete request, kept as review
    body = get(base, "/api/ui/log")[1]
    blob = repr(body)
    hidden = [v for d in (canary, other) for k, v in d.items() if k != "name"]
    for secret in (*hidden, "canary.mail", "0142", "0143", "message text"):
        assert secret not in blob, secret
    assert rig.query("SELECT count(*) FROM leads WHERE phone = ?", (canary["phone"],))[0][0] == 1  # really stored
    names = {i["lead_name"] for i in body["items"]}
    assert names == {"Cana Ry-Name", None}
    assert {i["lead_id"] for i in body["items"]} == {first, None}
    assert all(set(i) == LOG_KEYS for i in body["items"])
    details = {i["request_id"]: i["new_details"] for i in body["items"]}
    assert details == {
        "pii-1": False,
        "pii-2": True,
        "pii-bad": False,
    }  # the repeat that differs is flagged, nothing else


def test_log_unmapped_values_do_not_leak() -> None:
    """The table's own checks refuse other values, so this is the guard behind the guard: an unknown result is shown
    as `review` (a person must look), an unknown follow-up state or door as `unknown`; the stored text never leaves."""
    row = (5, 9, "stray-door", "r-1", "stray-outcome", "L0c4d8e2f6a19", "Ada", "stray-state", "stray-fields", None, 0)
    out = portal_api._log_row(row)
    assert out["result"] == "review" and out["followup_state"] == "unknown" and out["door"] == "unknown"
    assert "stray" not in repr(out)
    assert portal_api._log_row((5, 9, "form", "r-1", None, None, None, None, None, None, 0))["result"] == "open"
    assert out["new_details"] is False  # only a duplicate can carry new details


def test_log_one_transaction_and_snapshot(env, monkeypatch) -> None:
    """One connection, one BEGIN..COMMIT, only SELECTs; a receipt written after the page was read is not in `total`."""
    rig, base = fresh(env)
    seed(rig)
    seen: list[list[str]] = []
    real = portal_api.connect_readonly
    added: list[int] = []

    class Spy:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn, self.log = conn, []
            seen.append(self.log)

        def execute(self, text: str, *a: Any) -> Any:
            self.log.append(text.split()[0])
            result = self.conn.execute(text, *a)
            if "ORDER BY r.received_at" in text and not added:  # the page is read: now a writer commits a new receipt
                sql(
                    rig,
                    "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES ('form', 'late', ?, '{}')",
                    (rig.clock.t,),
                )
                added.append(1)
            return result

        def close(self) -> None:
            self.log.append("CLOSE")
            self.conn.close()

    monkeypatch.setattr(portal_api, "connect_readonly", lambda p: Spy(real(p)))
    status, out = get(base, "/api/ui/log")
    assert status == 200 and added == [1]
    assert len(seen) == 1 and seen[0][0] == "BEGIN" and seen[0][-2:] == ["COMMIT", "CLOSE"]
    assert seen[0].count("BEGIN") == 1 and set(seen[0][1:-2]) == {"SELECT"} and len(seen[0]) == 6
    assert out["total"] == SEEDED and len(out["items"]) == SEEDED, "total and page must be from one snapshot"
    assert get(base, "/api/ui/log")[1]["total"] == SEEDED + 1  # the next reply sees the new receipt


def test_log_cannot_write(env) -> None:
    rig, base = fresh(env)
    seed(rig)
    before = rig.query("SELECT count(*), sum(attempts) FROM receipts")
    assert get(base, "/api/ui/log")[0] == 200
    assert rig.query("SELECT count(*), sum(attempts) FROM receipts") == before


def test_log_routes_and_methods(env) -> None:
    _, base = fresh(env)
    for path in ("/api/ui/log", "/api/ui/data-map"):
        assert call(base, "POST", path, raw=b"")[0] == 405
        assert call(base, "DELETE", path)[0] == 405
        assert get(base, path + "/x") == (404, {"error": "not_found"})
    for path in ("/api/ui/logs", "/api/ui/Log", "/api/ui/data_map", "/api/ui/datamap"):
        assert get(base, path) == (404, {"error": "not_found"})
    assert raw(base, "GET /api/ui/log HTTP/1.1", f"Host: {EVIL}")[0] == 403
    assert raw(base, "GET /api/ui/log HTTP/1.1", f"Host: {HOST}", f"Origin: http://{EVIL}")[0] == 403


# -- /api/ui/data-map
def test_data_map_matches_fixture(env) -> None:
    _, base = fresh(env)
    status, out = get(base, "/api/ui/data-map")
    assert status == 200 and out == fixture("data-map") and same_shape(out, fixture("data-map"))
    assert out["ai_step"]["receives"] == ["lead_id", "email_key"]
    assert out["ai_step"]["never_receives"] == ["name", "phone", "company", "message"]


def test_data_map_reads_the_signature(env) -> None:
    rig, _ = fresh(env)
    names = [n for n in inspect.signature(contract.Qualifier.qualify).parameters if n != "self"]
    assert portal_api._data_map(rig.desk)["ai_step"]["receives"] == names
    assert set(names) <= set(AI_INPUT_FIELDS)


def test_new_ai_input_is_refused(env, monkeypatch) -> None:
    """The mutant: the contract's qualify gains an input (a lead's name). The page must refuse, not guess."""
    rig, base = fresh(env)

    def qualify(self: Any, lead_id: str, email_key: str, name: str) -> str:
        return "warm"

    monkeypatch.setattr(contract.Qualifier, "qualify", qualify)
    with pytest.raises(portal_api.DataMapRefused, match="name"):
        portal_api._data_map(rig.desk)
    assert get(base, "/api/ui/data-map") == (500, {"error": "internal"})  # nothing is described from a stale list


def test_variable_ai_inputs_are_refused(env, monkeypatch) -> None:
    rig, _ = fresh(env)

    def qualify(self: Any, *args: str) -> str:
        return "warm"

    monkeypatch.setattr(contract.Qualifier, "qualify", qualify)
    with pytest.raises(portal_api.DataMapRefused):
        portal_api._data_map(rig.desk)


def test_loaded_step_must_match_contract(env, monkeypatch) -> None:
    """The loaded AI step takes one more input than the contract says: refused."""
    rig, _ = fresh(env)

    class Wider:
        def qualify(self, lead_id: str, email_key: str, company: str) -> str:
            return "hot"

    monkeypatch.setattr(rig.desk, "qualifier", Wider())
    with pytest.raises(portal_api.DataMapRefused, match="loaded"):
        portal_api._data_map(rig.desk)


def test_data_map_mode(env, monkeypatch) -> None:
    rig, _ = fresh(env)
    assert portal_api._data_map(rig.desk)["ai_step"]["mode"] == "recorded"  # the class says so

    class Silent:
        def qualify(self, lead_id: str, email_key: str) -> str:
            return "hot"

    class Odd(Silent):
        mode = "something else"

    class Live(Silent):
        mode = "live"

    for qualifier, mode in ((Silent(), QUALIFIER_MODE_FALLBACK), (Odd(), QUALIFIER_MODE_FALLBACK), (Live(), "live")):
        monkeypatch.setattr(rig.desk, "qualifier", qualifier)
        assert portal_api._data_map(rig.desk)["ai_step"]["mode"] == mode
    assert QUALIFIER_MODE_FALLBACK in QUALIFIER_MODES and QUALIFIER_MODE_FALLBACK != "recorded"


def test_data_map_bind_from_config(env, monkeypatch) -> None:
    rig, _ = fresh(env)
    monkeypatch.setattr(rig.desk, "config", replace(rig.config, bind="localhost"))
    assert portal_api._data_map(rig.desk)["access"]["bind"] == "localhost"


def test_lead_columns_are_all_listed(env) -> None:
    """A column added to the leads table must be put into one of the two lists, so the page cannot miss a field."""
    rig, _ = fresh(env)
    columns = {r[1] for r in rig.query("PRAGMA table_info(leads)")}
    assert columns == set(LEAD_CONTENT_FIELDS) | set(LEAD_BOOKKEEPING_COLUMNS)
    assert not set(LEAD_CONTENT_FIELDS) & set(LEAD_BOOKKEEPING_COLUMNS)
    never = portal_api._data_map(rig.desk)["ai_step"]["never_receives"]
    assert set(never) <= columns and set(never) | {"lead_id", "email"} == set(LEAD_CONTENT_FIELDS)


def test_data_map_flags_are_true(env) -> None:
    """The three flags the page states are checked by what the desk does, not by the flag alone."""
    rig, base = fresh(env)
    access = get(base, "/api/ui/data-map")[1]["access"]
    assert access["host_check"] is True and raw(base, "GET /api/ui/summary HTTP/1.1", f"Host: {EVIL}")[0] == 403
    assert access["origin_check"] is True
    assert raw(base, "GET /api/ui/summary HTTP/1.1", f"Host: {HOST}", f"Origin: http://{EVIL}")[0] == 403
    assert access["portal_pages_read_only"] is True  # the PAGES only read; the buttons write (next test)
    conn = portal_api.connect_readonly(rig.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM followups")
    finally:
        conn.close()
    assert (access["login"], access["encryption_at_rest"], access["masking"]) == (False, False, False)


def test_data_map_reads_no_database(env, monkeypatch) -> None:
    _, base = fresh(env)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("data-map opened the database")

    monkeypatch.setattr(portal_api, "connect_readonly", boom)
    assert get(base, "/api/ui/data-map")[0] == 200


def test_log_query_uses_the_time_index() -> None:
    """Both the first page and a page after a cursor read the index in order: no sort of the whole book."""
    from pathlib import Path

    import lead_desk

    con = sqlite3.connect(":memory:")
    con.executescript((Path(lead_desk.__file__).parent / "schema.sql").read_text())
    for first, at, rid in ((1, 0, 0), (0, 5, 5)):
        plan = " ".join(
            str(r[3])
            for r in con.execute(
                "EXPLAIN QUERY PLAN " + portal_api._LOG_ROWS, {"first": first, "at": at, "id": rid, "n": 101}
            )
        )
        assert "receipts_time_ix" in plan and "TEMP B-TREE" not in plan, plan


def test_log_values_match_the_book(env) -> None:
    """Door, time and follow-up state of every row equal what the book holds (read here by separate, plain queries)."""
    rig, base = fresh(env)
    seed(rig)
    _, out = get(base, "/api/ui/log")
    got = {i["request_id"]: i for i in out["items"]}
    for source_ref, source, at, lead_id in rig.query(
        "SELECT source_ref, source, received_at, outcome_lead_id FROM receipts"
    ):
        row = got[source_ref]
        assert (row["door"], row["received_at"]) == (source, at)
        want = rig.query("SELECT state FROM followups WHERE lead_id = ?", (lead_id,)) if lead_id else []
        assert row["followup_state"] == (want[0][0] if want else None), source_ref
    assert got["ui-2"]["door"] == "webhook" and got["ui-1"]["door"] == "form"
    states = {i["followup_state"] for i in out["items"]}
    assert {"failed", "unknown", "sending"} <= states  # the seed really covers the odd states
    assert got["ui-g-5"]["followup_state"] == got["ui-g-0"]["followup_state"]  # a repeat shows its lead's state


def test_stored_list_names_every_field(env) -> None:
    """Every lead field the desk keeps, and the request fields, appear in the page's stored list."""
    rig, _ = fresh(env)
    listed = {r["field"] for r in portal_api._data_map(rig.desk)["stored"]}
    wanted = set(LEAD_CONTENT_FIELDS)
    wanted |= {"request_payload", "request_id", "door", "times", "follow_up_state", "ai_label"}
    wanted |= {"later_decision", "lead_owner", "lead_stage", "reminder_state"}
    assert listed == wanted
    receipt_cols = {r[1] for r in rig.query("PRAGMA table_info(receipts)")}
    assert {"source", "source_ref", "received_at", "payload"} <= receipt_cols  # the columns the entries stand for


def test_data_map_lists_portal_actions(env) -> None:
    """`portal_actions` is the list of action ids the review page really offers; the demo controls are listed
    only when the demo extension is loaded (the desk passes that fact in), never by a fixed list."""
    rig, base = fresh(env)
    seed(rig)
    offered = {a["id"].split("_")[0] for i in get(base, "/api/ui/review")[1]["items"] for a in i["actions"]}
    access = get(base, "/api/ui/data-map")[1]["access"]
    assert list(PORTAL_ACTIONS) == ["retry", "resolve"]
    assert offered == set(PORTAL_ACTIONS), offered  # both kinds are offered; "resolve_sent" etc. are resolves
    with_demo = portal_api._data_map(rig.desk, demo_controls=True)["access"]["portal_actions"]
    assert with_demo == ["retry", "resolve", "demo_controls"]
    assert portal_api._data_map(rig.desk)["access"]["portal_actions"] == list(PORTAL_ACTIONS)  # no extension
    assert access["portal_actions"] == with_demo  # the test server loads the demo extension, as the demo does
    assert (
        portal_api.handle(rig.desk, "data-map", "", 0, demo_controls=True)[1]["access"]["portal_actions"] == with_demo
    )


def test_data_map_note_is_fixed_text(env) -> None:
    _, base = fresh(env)
    note = get(base, "/api/ui/data-map")[1]["ai_step"]["note"]
    assert note == AI_STEP_NOTE and "does not prove" in note and "looks up by id" in note


class _SpyQualifier(RecordedQualifier):
    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self.seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def qualify(self, *args: Any, **kwargs: Any) -> Any:
        self.seen.append((args, kwargs))
        return super().qualify(*args, **kwargs)


def test_qualify_call_site_inputs(env) -> None:
    """The data map lists what the step is given by its signature; here the real call site is checked too: the desk
    calls qualify with exactly (lead_id, email_key), positionally, and nothing else."""
    rig, _ = fresh(env)
    receives = portal_api._data_map(rig.desk)["ai_step"]["receives"]  # read before the spy (it takes *args)
    rig.desk.qualifier = _SpyQualifier(rig.replies_path)
    lid = rig.desk.process("form", "spy-1", lead(email="  Mixed.Case@Example.test ")).body["lead_id"]
    rig.desk.run_followup(lid)
    assert rig.desk.qualifier.seen == [((lid, "mixed.case@example.test"), {})]
    names = list(inspect.signature(contract.Qualifier.qualify).parameters)[1:]
    assert names == list(AI_INPUT_FIELDS) == receives


def test_qualify_call_site_on_retry(env) -> None:
    """The Retry button's path (a failed follow-up run again) gives the AI step the same two inputs."""
    rig, _ = fresh(env)
    lid = rig.desk.process("form", "spy-2", lead(email="Retry.Case@Example.test")).body["lead_id"]
    sql(rig, "UPDATE followups SET state='failed', last_error='refused' WHERE lead_id=?", (lid,))
    rig.desk.qualifier = _SpyQualifier(rig.replies_path)
    assert rig.desk.retry("F" + lid).status == 200
    assert rig.desk.qualifier.seen == [((lid, "retry.case@example.test"), {})]


# What every stored column stands for: a field of the page's stored list, or "internal: <reason>" when the column is
# the desk's own bookkeeping that no listed field describes. A new table or column fails the test below until it is
# put here on purpose.
_COLUMN_STANDS_FOR: dict[str, dict[str, str]] = {
    "receipts": {
        "receipt_id": "internal: record number of the request",
        "source": "door",
        "source_ref": "request_id",
        "received_at": "times",
        "payload": "request_payload",
        "attempts": "internal: how many times the desk tried to decide the request",
        "outcome": "internal: the desk's answer (new, duplicate, review)",
        "outcome_lead_id": "internal: the lead the answer points at",
        "outcome_reason": "internal: why the answer is review",
        "differing_fields": "internal: names of the fields a repeat changed",
        "decided_at": "times",
        "payload_conflict": "internal: marks a repeat with other data",
    },
    "leads": {
        "lead_id": "internal: lead number",
        "receipt_id": "internal: record number of the request that made the lead",
        "source": "door",
        "name": "name",
        "email": "email",
        "email_key": "email",
        "phone": "phone",
        "company": "company",
        "message": "message",
        "created_at": "times",
    },
    "followups": {
        "lead_id": "internal: lead number",
        "state": "follow_up_state",
        "ai_label": "ai_label",
        "attempts": "follow_up_state",
        "last_error": "follow_up_state",
        "updated_at": "times",
        "sent_at": "times",
        "crm_done": "follow_up_state",
        "auto_attempts": "follow_up_state",
        "ambiguous_prior": "follow_up_state",
        "send_started": "follow_up_state",
    },
    "later_decisions": {
        "receipt_id": "internal: receipt number",
        "decision": "later_decision",
        "approver_id": "later_decision",
        "approver_name": "later_decision",
        "channel": "later_decision",
        "decided_at": "later_decision",
    },
    "lead_state": {
        "lead_id": "internal: lead number",
        "owner_id": "lead_owner",
        "owner_name": "lead_owner",
        "assigned_at": "times",
        "step": "lead_stage",
        "step_rank": "lead_stage",
        "changed_at": "times",
        "crm_owner_done": "internal: whether the lead owner was copied to the CRM",
        "crm_step_done": "internal: the last step copied to the CRM",
        "crm_tries": "internal: failed CRM copy tries since the last success",
        "crm_error": "internal: class name of the last CRM copy error",
    },
    "nudges": {
        "lead_id": "internal: lead number",
        "state": "reminder_state",
        "attempts": "internal: how many times the reminder was claimed",
        "updated_at": "times",
        "sent_at": "times",
        "last_error": "reminder_state",
        "handled": "reminder_state",
    },
}


def test_stored_list_covers_every_column(env) -> None:
    """Every column of every table in the book is mapped on purpose to a listed field or named internal; a new
    table or column fails here until the list or this map is updated."""
    rig, _ = fresh(env)
    listed = {r["field"] for r in portal_api._data_map(rig.desk)["stored"]}
    tables = {r[0] for r in rig.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == set(_COLUMN_STANDS_FOR), tables ^ set(_COLUMN_STANDS_FOR)
    for table, mapping in _COLUMN_STANDS_FOR.items():
        columns = {r[1] for r in rig.query(f"PRAGMA table_info({table})")}
        assert columns == set(mapping), (table, columns ^ set(mapping))
        for col, stands in mapping.items():
            assert stands in listed or stands.startswith("internal: "), (table, col, stands)
    assert {v for m in _COLUMN_STANDS_FOR.values() for v in m.values() if not v.startswith("internal: ")} <= listed
