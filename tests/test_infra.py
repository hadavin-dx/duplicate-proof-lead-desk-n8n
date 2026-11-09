"""Desk tests for the adapters, the database factory and the demo wiring. Made-up data only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from support import HOST, REPO, StubSender, call, free_port, lead

from demo_only.fakes import RecordedQualifier
from lead_desk import db
from lead_desk.adapters import FakesUnreachable, HttpCrm, HttpOutbox
from lead_desk.config import Config
from lead_desk.contract import OutboxModeMismatch, SendAmbiguous, SendRefused, SendUnreachable
from lead_desk.desk import Desk
from lead_desk.loader import load_demo_adapters

SUBJECT, BODY = "Thanks for getting in touch, Ada", "Hi Ada, we received your message. (DEMO EMAIL)"
FAST = 1.0  # adapter timeout for the cases that must not wait long
CONNECT_REFUSED_WAIT = 5.0  # long enough for the system to report "connection refused"


def send(ob: HttpOutbox, key: str = "L000000000001", timeout: float = FAST) -> str:
    return ob.send(key, "ada@example.test", SUBJECT, BODY, timeout)


# -- test_error_mapping
def test_error_mapping(env, monkeypatch: pytest.MonkeyPatch) -> None:
    srv, url = env.fakes("keyed")
    ob = HttpOutbox(url, FAST)
    crm = HttpCrm(url, FAST)
    assert ob.capability == "keyed"
    # 200 sent / already_sent
    assert send(ob) == "SENT"
    assert send(ob) == "ALREADY_SENT"
    assert call(url, "GET", "/fake/outbox")[1]["count"] == 1
    # the CRM adapter: put, then ALREADY_PRESENT
    record = {"lead_id": "L1", "name": "Ada", "email": "ada@example.test", "company": "", "source": "form"}
    assert crm.put("L000000000001", record) == "PUT"
    assert crm.put("L000000000001", record) == "ALREADY_PRESENT"
    # control: refuse (HTTP 422, nothing stored) -> SendRefused
    assert call(url, "POST", "/fake/control", {"fail_next": "refuse", "delay_ms": 0})[0] == 200
    with pytest.raises(SendRefused):
        send(ob, "L000000000002")
    assert call(url, "GET", "/fake/outbox")[1]["count"] == 1
    # drop (closed without a reply, nothing stored), record_then_drop (stored, no reply) -> SendAmbiguous
    call(url, "POST", "/fake/control", {"fail_next": "drop", "delay_ms": 0})
    with pytest.raises(SendAmbiguous):
        send(ob, "L000000000003")
    assert call(url, "GET", "/fake/outbox")[1]["count"] == 1
    call(url, "POST", "/fake/control", {"fail_next": "record_then_drop", "delay_ms": 0})
    with pytest.raises(SendAmbiguous):
        send(ob, "L000000000004")
    assert call(url, "GET", "/fake/outbox")[1]["count"] == 2
    # read timeout: the server waits longer than the adapter -> SendAmbiguous
    call(url, "POST", "/fake/control", {"fail_next": "none", "delay_ms": 1500})
    with pytest.raises(SendAmbiguous):
        send(ob, "L000000000005", timeout=0.3)
    # control target: a CRM-only control does not touch the next outbox post
    call(url, "POST", "/fake/control", {"fail_next": "refuse", "delay_ms": 0, "target": "crm"})
    assert send(ob, "L000000000006") == "SENT"
    with pytest.raises(Exception):  # noqa: B017, PT011 (the CRM adapter's error type is not fixed by the contract)
        crm.put("L000000000009", record)
    assert call(url, "POST", "/fake/control", {"fail_next": "refuse", "target": "disk"})[0] == 400
    # mode mismatch: the adapter saw "keyed" at start, the server now answers "blind"
    srv.state.outbox_mode = "blind"
    with pytest.raises(OutboxModeMismatch):
        send(ob, "L000000000007")
    assert issubclass(OutboxModeMismatch, SendRefused)
    srv.state.outbox_mode = "keyed"
    # a proxy variable set in the environment must not change local errors or successes
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://{HOST}:{free_port()}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert send(ob, "L000000000008") == "SENT"
    monkeypatch.undo()
    # HTTP status rows from a stand-in sender
    for status, exc in [(400, SendRefused), (404, SendRefused), (422, SendRefused), (429, SendRefused)]:
        stub = StubSender("status", status)
        try:
            with pytest.raises(exc):
                send(HttpOutbox(stub.url, FAST))
        finally:
            stub.stop()
    for status in (408, 500, 502, 503):
        stub = StubSender("status", status)
        try:
            with pytest.raises(SendAmbiguous):
                send(HttpOutbox(stub.url, FAST))
        finally:
            stub.stop()
    for behaviour in ("reset", "garbage"):  # connection reset while reading; unreadable reply
        stub = StubSender(behaviour)
        try:
            with pytest.raises(SendAmbiguous):
                send(HttpOutbox(stub.url, FAST))
        finally:
            stub.stop()
    # a closed port: connection refused -> SendUnreachable (nothing was sent)
    stub = StubSender("status", 200, b'{"status":"sent","mode":"keyed"}')
    closed = HttpOutbox(stub.url, FAST)
    stub.stop()
    with pytest.raises(SendUnreachable):
        send(closed, timeout=CONNECT_REFUSED_WAIT)  # a refused local connect can take about 2 s to show on some systems
    # no reachable /fake/info at construction
    with pytest.raises(FakesUnreachable):
        HttpOutbox(stub.url, FAST)
    assert issubclass(FakesUnreachable, RuntimeError)


# -- test_connection_pragmas_and_busy_503
def test_connection_pragmas_and_busy_503(env, tmp_path: Path) -> None:
    path = tmp_path / "pragmas.sqlite3"
    con = db.connect(path)
    try:
        assert con.isolation_level is None
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000  # the one busy-wait setting, 5.0 s
        db.init_schema(con)
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        db.init_schema(con)  # a second call is a no-op
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        con.execute("PRAGMA user_version=7")
        with pytest.raises(Exception, match=r"(?i)version"):  # any other version is refused
            db.init_schema(con)
    finally:
        con.close()
    bad = db.connect(tmp_path / "bad.sqlite3")
    try:
        with pytest.raises(sqlite3.Error):
            db.init_schema(bad, "CREATE TABLE t(x); THIS IS NOT SQL;")
        assert not bad.in_transaction  # a failed script leaves no open transaction
        assert bad.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        bad.close()
    # a forced lock timeout gives 503 busy, with no verdict recorded; a retry afterwards works
    rig = env.rig()
    holder = sqlite3.connect(rig.db_path, isolation_level=None, timeout=1)
    holder.execute("BEGIN IMMEDIATE")
    try:
        r = rig.desk.process("form", "sub-1", lead())  # waits the 5 s busy time, then answers
        assert r.status == 503
        assert r.body["error"] == "busy"
        assert r.body["receipt_id"] is None  # the receipt was never written
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    ok = rig.desk.handle("form", "sub-1", lead())
    assert ok.status == 201
    assert rig.leads() == 1
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0


# -- test_demo_adapters
def test_demo_adapters(env, tmp_path: Path) -> None:
    # replies.json and the canned leads: every canned email has an entry
    replies = json.loads((REPO / "demo_only" / "replies.json").read_text(encoding="utf-8"))
    canned = json.loads((REPO / "demo_only" / "canned_leads.json").read_text(encoding="utf-8"))
    assert canned, "no canned leads"
    for item in canned:
        assert "source" not in item and "source_ref" not in item  # payloads only
        assert item["email"].strip().lower() in replies, f"no recorded reply for {item['email']}"
    assert set(replies.values()) <= {"hot", "warm", "cold"}
    # RecordedQualifier rules: fail loud
    with pytest.raises(OSError):
        RecordedQualifier(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        RecordedQualifier(broken)
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"x@example.test": "scorching"}), encoding="utf-8")
    q = RecordedQualifier(odd)
    with pytest.raises(KeyError, match="scorching"):
        q.qualify("L1", "x@example.test")
    assert q.qualify("L1", "nobody@example.test") == "warm"
    # start-up refuses when /fake/info is unreachable
    dead = Config(fakes_port=free_port(), replies_path=REPO / "demo_only" / "replies.json")
    with pytest.raises(FakesUnreachable):
        load_demo_adapters(dead)
    # the wiring: adapters on a live fakes server; every canned lead is processed end to end
    srv, url = env.fakes("keyed")
    host, port = url.removeprefix("http://").split(":")
    cfg = Config(
        db_path=tmp_path / "demo.sqlite3",
        port=0,
        fakes_host=host,
        fakes_port=int(port),
        replies_path=REPO / "demo_only" / "replies.json",
    )
    crm, outbox, qualifier = load_demo_adapters(cfg)
    assert isinstance(crm, HttpCrm) and isinstance(outbox, HttpOutbox) and isinstance(qualifier, RecordedQualifier)
    assert outbox.capability == "keyed"
    desk = Desk(cfg, crm, outbox, qualifier)
    desk.startup()
    try:
        for i, payload in enumerate(canned, start=1):
            r = desk.handle("form", f"canned-{i}", payload)
            assert r.status == 201, payload
        assert call(url, "GET", "/fake/crm")[1]["count"] == len(canned)
        assert call(url, "GET", "/fake/outbox")[1]["count"] == len(canned)
        labels = {row["record"]["ai_label"] for row in call(url, "GET", "/fake/crm")[1]["rows"]}
        assert labels == set(replies.values())  # the recorded replies reached the CRM
        # mode mismatch: the server's mode changes after start-up -> unknown / mode_mismatch (the sender answered 2xx,
        # so the email is stored in the wrong mode: a person decides), nothing recorded as sent
        srv.state.outbox_mode = "blind"
        r = desk.handle("form", "extra-1", lead(email="extra@example.test"))
        lid = r.body["lead_id"]
        assert desk.lead(lid).body["followup"]["state"] == "unknown"
        assert desk.lead(lid).body["followup"]["last_error"] == "mode_mismatch"
    finally:
        desk.close()


def test_seeded_demo_addresses_end_in_example_com() -> None:
    """HubSpot refuses a `.test` address (probe 2026-11-11), so every made-up address the demo can send on to a real CRM
    ends in example.com: the canned people, the recorded-reply keys, the proof flow and the load driver."""
    canned = json.loads((REPO / "demo_only" / "canned_leads.json").read_text(encoding="utf-8"))
    replies = json.loads((REPO / "demo_only" / "replies.json").read_text(encoding="utf-8"))
    for address in [p["email"] for p in canned] + list(replies):
        assert address.lower().endswith("@example.com"), address
    for rel in ("n8n/proof.json", "load_tools/load_driver.py", "demo_only/burst.py"):
        assert "example.test" not in (REPO / rel).read_text(encoding="utf-8"), rel
