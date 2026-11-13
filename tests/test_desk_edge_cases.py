"""Edge cases that drive the desk directly: text that cannot be stored, repair and retries, replies from the mail
system and CRM, load, replays, start-up and the lock file. Some are plain guards (for
example that a repeated send is a no-op, or that ordinary text is still accepted). Made-up data only; ports are always 0
(the OS picks).
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from edge_case_helpers import RESULT_WRITE, mk_desk
from support import REPO, BlockAt, Clock, call, lead, rows, sql_exec, wait_until

from demo_only.fakes import KeyedOutbox, RecordedQualifier
from lead_desk import db, gate, identity, lockfile
from lead_desk.config import Config
from lead_desk.contract import SendUnreachable
from lead_desk.desk import Desk, default_now
from lead_desk.loader import load_demo_adapters

BURST = 150
MIN_SERVED = 32  # one full in-flight line of the 150 must be served
STARTUP_LIMIT = 5.0  # seconds; start-up and /counts must answer well inside this even on a busy machine
WAIT_LIMIT = 30.0  # a held call gives up after this
SLOW_COMMIT_SECONDS = 0.02


# -- helpers
class ConnProxy:
    """A sqlite3 connection that runs `rules` (functions of the SQL text) before every execute. A rule may sleep or
    raise. Everything else is passed through to the real connection."""

    def __init__(self, real: sqlite3.Connection, rules: list[Any]) -> None:
        self._c = real
        self._rules = rules

    def execute(self, sql: str, *args: Any) -> Any:
        for rule in self._rules:
            rule(sql)
        return self._c.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)


def patch_connect(monkeypatch: pytest.MonkeyPatch, *rules: Any) -> None:
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda p: ConnProxy(real(p), list(rules)))


def fail_when(match: str, exc: Exception, times: int | None = None, armed: list[bool] | None = None) -> Any:
    """Rule: raise exc for an execute whose SQL contains `match` (`times` times, or while armed[0])."""
    left = [times]
    lock = threading.Lock()

    def rule(sql: str) -> None:
        if match not in sql:
            return
        with lock:
            if armed is not None and not armed[0]:
                return
            if left[0] is not None:
                if left[0] <= 0:
                    return
                left[0] -= 1
        raise exc

    return rule


def busy() -> sqlite3.OperationalError:
    return sqlite3.OperationalError("database is locked")


def rows_set(rig: Any, state: str, lid: str) -> None:
    con = sqlite3.connect(rig.db_path, isolation_level=None)
    con.execute("UPDATE followups SET state=? WHERE lead_id=?", (state, lid))
    con.close()


def counts_sql(path: Path) -> dict[str, int]:
    r = rows(
        path,
        "SELECT (SELECT count(*) FROM receipts WHERE outcome IS NULL), (SELECT count(*) FROM leads),"
        " (SELECT count(*) FROM followups WHERE state='sending'),"
        " (SELECT count(*) FROM followups WHERE state='pending'),"
        " (SELECT count(*) FROM followups WHERE state='sent'), (SELECT count(*) FROM followups WHERE state='failed'),"
        " (SELECT count(*) FROM followups WHERE state='unknown')",
    )[0]
    return dict(zip(("open", "leads", "sending", "pending", "sent", "failed", "unknown"), r, strict=True))


def settled(path: Path) -> bool:
    c = counts_sql(path)
    return c["open"] == 0 and c["sending"] == 0 and c["pending"] == 0


class BlindCrm:
    """A CRM that cannot tell a repeat: every put adds a row."""

    def __init__(self) -> None:
        self.puts: list[str] = []
        self._lock = threading.Lock()

    def put(self, lead_id: str, record: Any) -> str:
        with self._lock:
            self.puts.append(lead_id)
        return "PUT"


# ============================== text that cannot be stored
BAD_TEXT_CASES = [
    ("name", "\x00Ada"),
    ("name", "Ada\x00"),
    ("name", "Ad\x1fa"),
    ("phone", "55\x000100"),
    ("company", "\x00"),
    ("message", "hello\x00world"),
    ("message", "bell\x07"),
    ("name", "Ada\x7f"),  # DEL
    ("message", "next\x85line"),  # C1 control NEL
    ("company", "Ex\x9fLtd"),  # last C1 control
    ("email", "a\x00@example.test"),
]


@pytest.mark.parametrize(("field", "value"), BAD_TEXT_CASES)
def test_control_characters_give_review(env, field: str, value: str) -> None:
    rig = env.rig()
    payload = lead(email="f1@example.test")
    payload[field] = value
    r = rig.desk.handle("form", "f1-ref", payload)
    assert (r.status, r.body["verdict"], r.body["reason"]) == (202, "review", f"bad_field:{field}")
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0
    assert rig.leads() == 0


def test_tab_newline_only_in_message(env) -> None:
    rig = env.rig()
    ok = rig.desk.handle("form", "m1", lead(email="m1@example.test", message="line one\r\n\tline two"))
    assert ok.status == 201
    bad = rig.desk.handle("form", "m2", lead(email="m2@example.test", name="Ada\nLovelace"))
    assert (bad.status, bad.body["reason"]) == (202, "bad_field:name")


@pytest.mark.parametrize("ref", ["\x00x", "x\x00", "", "r" * 129, "a\x1fb", "a\x7fb", "a\x85b", "lone\ud800surrogate"])
def test_bad_source_ref_is_400(env, ref: str) -> None:
    rig = env.rig()
    r = rig.desk.process("form", ref, lead())
    assert r.status == 400
    assert r.body == {"error": "bad_envelope", "receipt_id": None}
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0


def test_fuzz_all_codepoints(env) -> None:
    """Every code point class in every text field and in the request id: an answer below 500 and no open receipt."""
    rig = env.rig()
    rng = random.Random(424242)
    cps = set(range(0x300)) | {0xD7FF, 0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0xE000, 0xFFFE, 0xFFFF, 0x10000, 0x10FFFF}
    cps |= {rng.randrange(0x300, 0x110000) for _ in range(500)}
    n = 0
    for cp in sorted(cps):
        ch = chr(cp)
        fields = ["name"] + (["phone", "company", "message", "email"] if cp < 0x80 or rng.random() < 0.15 else [])
        for field in fields:
            n += 1
            payload = lead(email=f"fz{n}@example.test")
            payload[field] = f"a{ch}b" if field != "email" else f"a{ch}b@example.test"
            r = rig.desk.process("form", f"fz{n}", payload)
            assert r.status < 500, (hex(cp), field, r.body)
        n += 1
        r = rig.desk.process("form", f"x{ch}y", lead(email=f"fr{n}@example.test"))
        assert r.status < 500, (hex(cp), "source_ref", r.body)
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0


def test_unstorable_gets_verdict(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a check ever misses something the database refuses, the receipt still gets a verdict (review)."""
    rig = env.rig()

    def lenient(obj: Any) -> identity.Checked:
        return identity.Checked("ok", None, "\x00x", "net@example.test", "net@example.test", None, None, None)

    monkeypatch.setattr(identity, "check_payload", lenient)
    r = rig.desk.process("form", "net-1", lead())
    assert r.status == 202, r.body
    assert r.body["reason"] == "internal_unstorable:integrity"
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0


def test_integrity_error_is_visible_and_counted_not_hidden(env, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """An IntegrityError while deciding is most likely a constraint bug of ours: the receipt still gets its visible
    review verdict, but it is logged at ERROR and counted apart on /counts; a DataError is not counted."""
    import logging
    import sqlite3

    rig = env.rig()
    real = rig.desk._decide_once

    def boom(rid: int) -> Any:
        raise sqlite3.IntegrityError("CHECK constraint failed: made up")

    monkeypatch.setattr(rig.desk, "_decide_once", boom)
    with caplog.at_level(logging.WARNING, logger="lead_desk"):
        r = rig.desk.process("form", "ie-1", lead())
    assert r.status == 202 and r.body["reason"] == "internal_unstorable:integrity"
    assert any(x.levelno == logging.ERROR and "IntegrityError" in x.getMessage() for x in caplog.records)
    assert rig.desk.counts(None).body["decide_integrity_errors"] == 1
    monkeypatch.setattr(rig.desk, "_decide_once", lambda rid: (_ for _ in ()).throw(sqlite3.DataError("made up")))
    assert rig.desk.process("form", "ie-2", lead(email="ie2@example.test")).body["reason"] == "internal_unstorable"
    assert rig.desk.counts(None).body["decide_integrity_errors"] == 1  # a DataError is a bad payload, not our bug
    monkeypatch.setattr(rig.desk, "_decide_once", real)
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0


def test_integrity_error_through_a_real_constraint_is_stored_apart(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name the checks let through but the leads table refuses (longer than its CHECK allows) breaks a real
    constraint inside the real decision: verdict review, a reason that tells it from a bad payload, counted, and the
    reconciliation is not ok."""
    rig = env.rig()

    def too_long(obj: Any) -> identity.Checked:
        return identity.Checked("ok", None, "n" * 201, "long@example.test", "long@example.test", None, None, None)

    monkeypatch.setattr(identity, "check_payload", too_long)
    r = rig.desk.process("form", "ie-real", lead())
    assert r.status == 202 and r.body["reason"] == "internal_unstorable:integrity", r.body
    assert rig.query("SELECT outcome, outcome_reason FROM receipts")[0] == ("review", "internal_unstorable:integrity")
    body = rig.desk.counts(1).body
    assert body["decide_integrity_errors"] == 1 and body["ok"] is False and body["open"] == 0


def test_a_disk_fault_never_becomes_a_verdict(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A database fault that is not a lock (disk I/O error, disk full, a file held by a virus scanner) is not the
    receipt's fault and a verdict is written once: three such faults in a row keep the receipt open, visible as
    stalled and counted; the next try decides it normally (one lead, one follow-up)."""
    rig = env.rig()
    real = rig.desk._decide_once
    seen = {"n": 0}

    def flaky(rid: int) -> Any:
        seen["n"] += 1
        if seen["n"] <= 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real(rid)

    monkeypatch.setattr(rig.desk, "_decide_once", flaky)
    assert rig.desk.process("form", "disk-1", lead()).status == 500  # the client retries with the same request id
    assert rig.desk.process("form", "disk-1", lead()).status == 500
    rig.clock.advance(rig.config.stall_seconds + 5)
    assert rig.desk.reap_once()["receipts"] == 0  # the third fault, met by the reaper
    assert seen["n"] == 3
    body = rig.desk.counts(None).body
    assert (body["open"], body["review"], body["decide_transient_errors"]) == (1, 0, 3), body
    assert any(i["kind"] == "stalled" and i["reason"] == "open receipt" for i in rig.desk.review().body)
    assert rig.query("SELECT outcome FROM receipts")[0][0] is None
    done = rig.desk.process("form", "disk-1", lead())
    assert (done.status, done.body["verdict"]) == (201, "new"), done.body
    assert rig.leads() == 1 and rig.query("SELECT count(*) FROM followups")[0][0] == 1
    assert rig.query("SELECT outcome FROM receipts")[0][0] == "new"


def test_the_reaper_keeps_trying_a_receipt_with_a_disk_fault(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    real = rig.desk._decide_once
    seen = {"n": 0}

    def flaky(rid: int) -> Any:
        seen["n"] += 1
        if seen["n"] <= 5:
            raise sqlite3.OperationalError("disk I/O error")
        return real(rid)

    monkeypatch.setattr(rig.desk, "_decide_once", flaky)
    assert rig.desk.process("form", "disk-2", lead()).status == 500
    for _ in range(5):  # no clock advance: the receipt is not waiting for stall_seconds
        rig.desk.reap_once()
    assert rig.query("SELECT outcome FROM receipts")[0][0] == "new"
    assert seen["n"] == 6 and rig.leads() == 1


def test_a_busy_startup_sweep_leaves_the_receipt_for_the_next_reaper_pass(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    payload = '{"name":"Ada","email":"sweep@example.test"}'
    sql_exec(
        rig.db_path,
        "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES('form','sweep-1',?,?)",
        (rig.clock.t, payload),
    )
    monkeypatch.setattr(Desk, "_decide_once", lambda self, rid: (_ for _ in ()).throw(busy()))
    rig.restart()
    assert rig.query("SELECT outcome FROM receipts")[0][0] is None
    monkeypatch.undo()
    rig.desk.reap_once()  # the clock did not move: only the poke can make the reaper take it
    assert rig.query("SELECT outcome FROM receipts")[0][0] == "new"


# ============================== runtime repair and retries inside the two transactions
def test_reaper_decides_open_receipt(env, monkeypatch: pytest.MonkeyPatch) -> None:
    armed = [True]
    patch_connect(monkeypatch, fail_when("INSERT INTO leads", busy(), armed=armed))
    rig = env.rig()
    r = rig.desk.process("form", "stuck-1", lead())
    assert r.status == 503 and r.body["receipt_id"] is not None
    armed[0] = False
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 1
    rig.clock.advance(rig.config.stall_seconds + 5)
    rig.desk.reap_once()
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0
    assert rig.leads() == 1


@pytest.mark.parametrize("sql_part", ["INSERT INTO receipts", "INSERT INTO leads"])
def test_both_writes_retry_locked(env, monkeypatch: pytest.MonkeyPatch, sql_part: str) -> None:
    patch_connect(monkeypatch, fail_when(sql_part, busy(), times=2))
    rig = env.rig()
    r = rig.desk.process("form", "retry-1", lead())
    assert r.status == 201, r.body


def test_stuck_sending_keyed_resent(env, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_connect(monkeypatch, fail_when(RESULT_WRITE, sqlite3.OperationalError("disk I/O error"), times=1))
    rig = env.rig()
    rig.desk.handle("form", "snd-1", lead())
    assert rig.state()[0] == "sending"  # the email was sent, the result could not be written
    assert rig.outbox.count == 1
    for _ in range(4):
        rig.clock.advance(60)
        rig.desk.reap_once()
    assert rig.state()[0] == "sent"
    assert rig.outbox.count == 1  # the key made the second send a no-op


def test_stuck_sending_blind_unknown(env, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_connect(monkeypatch, fail_when(RESULT_WRITE, sqlite3.OperationalError("disk I/O error"), times=1))
    rig = env.rig(outbox="blind")
    rig.desk.handle("form", "snd-2", lead())
    assert rig.state()[0] == "sending"
    for _ in range(3):
        rig.clock.advance(60)
        rig.desk.reap_once()
    assert rig.state() == ("unknown", "crash_while_sending")
    assert rig.outbox.count == 1  # never sent a second time blindly


def test_retry_not_200_sending(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    rig.outbox.fail_next("refuse")
    lid = rig.desk.handle("form", "rt-1", lead()).body["lead_id"]
    assert rig.state(lid)[0] in ("pending", "failed")
    armed = [True]
    patch_connect(monkeypatch, fail_when(RESULT_WRITE, busy(), armed=armed))
    rig.desk.close()  # next use opens a connection through the patched factory
    r = rig.desk.retry(f"F{lid}")
    armed[0] = False
    assert not (r.status == 200 and r.body.get("state") == "sending"), r.body
    assert (r.status, r.body["error"]) == (409, "in_progress")


# ============================== wrong-mode reply is ambiguous, capability is read again
def _http_desk(env, tmp: Path, url: str, **kw: Any) -> Desk:
    host, port = url.rsplit("://", 1)[1].rsplit(":", 1)
    cfg = Config(
        db_path=tmp / "desk.sqlite3",
        port=0,
        fakes_host=host,
        fakes_port=int(port),
        replies_path=REPO / "demo_only" / "replies.json",
        **kw,
    )
    crm, outbox, qualifier = load_demo_adapters(cfg)
    return Desk(cfg, crm, outbox, qualifier)


def test_mode_mismatch_is_unknown(env, tmp_path: Path) -> None:
    srv, url = env.fakes("keyed")
    desk = _http_desk(env, tmp_path, url)
    desk.startup()
    try:
        srv.state.outbox_mode = "blind"  # the sender was restarted in the other mode
        lid = desk.handle("form", "mm-1", lead(email="mm@example.test")).body["lead_id"]
        fu = desk.lead(lid).body["followup"]
        assert (fu["state"], fu["last_error"]) == ("unknown", "mode_mismatch")
        assert call(url, "GET", "/fake/outbox")[1]["count"] == 1
        r = desk.retry(f"F{lid}")
        assert (r.status, r.body["error"]) == (409, "unknown_needs_person")
        assert call(url, "GET", "/fake/outbox")[1]["count"] == 1
        desk.reap_once()
        assert desk.outbox.capability == "blind"  # /fake/info was read again
    finally:
        desk.close()


def test_startup_reads_capability_again(env, tmp_path: Path) -> None:
    srv, url = env.fakes("keyed")
    desk = _http_desk(env, tmp_path, url)  # built while the sender is keyed
    desk.startup()
    lid = desk.process("form", "mm-2", lead(email="mm2@example.test")).body["lead_id"]  # not driven
    desk.close()
    con = sqlite3.connect(tmp_path / "desk.sqlite3", isolation_level=None)
    con.execute(
        "UPDATE followups SET state='sending', crm_done=1, send_started=1 WHERE lead_id=?", (lid,)
    )  # as if killed mid-send, CRM done
    con.close()
    srv.state.outbox_mode = "blind"
    host, port = url.rsplit("://", 1)[1].rsplit(":", 1)
    cfg = Config(db_path=tmp_path / "desk.sqlite3", port=0, fakes_host=host, fakes_port=int(port))
    crm, outbox, qualifier = load_demo_adapters(
        Config(**{**cfg.__dict__, "replies_path": REPO / "demo_only" / "replies.json"})
    )
    outbox._cm = ("keyed", "keyed")  # the object a long-running desk would hold: built before the restart of the sender
    desk2 = Desk(
        Config(**{**cfg.__dict__, "replies_path": REPO / "demo_only" / "replies.json"}), crm, outbox, qualifier
    )
    try:
        desk2.startup()
        assert desk2.lead(lid).body["followup"]["state"] == "unknown"
        assert call(url, "GET", "/fake/outbox")[1]["count"] == 0  # nothing sent by the restart: no blind resend
    finally:
        desk2.close()


# ============================== the WAL answer is checked
def test_wal_refused_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = sqlite3.connect

    class Refusing:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._c = c

        def execute(self, sql: str, *a: Any) -> Any:
            if "JOURNAL_MODE" in sql.upper():  # any spelling of the switch, with or without a schema name
                return self._c.execute("SELECT 'delete'")  # SQLite's answer when it cannot switch
            return self._c.execute(sql, *a)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._c, name)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Refusing(real_connect(*a, **k)))
    with pytest.raises(RuntimeError, match="(?i)wal"):
        db.connect(tmp_path / "x.sqlite3")


def test_normal_and_memory_ok(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "ok.sqlite3")
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    c.close()
    m = db.connect(":memory:")
    m.close()


# ============================== automatic retry of a send that sent nothing
class FlakyOutbox(KeyedOutbox):
    """Fails the first `bad` calls with SendUnreachable (nothing stored), then works."""

    def __init__(self, bad: int) -> None:
        super().__init__()
        self.bad = bad
        self.calls = 0

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Any:
        self.calls += 1
        if self.calls <= self.bad:
            raise SendUnreachable("outage")
        return super().send(send_key, to, subject, body, timeout)


def test_outage_20_sends_all_sent(env) -> None:
    ob = FlakyOutbox(bad=20)
    rig = env.rig(outbox=ob)
    for i in range(20):
        rig.desk.handle("form", f"o{i}", lead(email=f"o{i}@example.test"))
    for _ in range(6):
        rig.clock.advance(3)
        rig.desk.reap_once()
    states = [r[0] for r in rig.query("SELECT state FROM followups")]
    assert states.count("sent") == 20, states
    assert rig.outbox.count == 20


def test_failed_only_after_cap(env) -> None:
    ob = FlakyOutbox(bad=10**6)
    rig = env.rig(outbox=ob)
    lid = rig.desk.handle("form", "cap-1", lead()).body["lead_id"]
    for _ in range(40):
        rig.clock.advance(20)
        rig.desk.reap_once()
    assert rig.state(lid) == ("failed", "unreachable")
    assert ob.calls == 5  # the cap: five tries in all
    assert rig.query("SELECT attempts FROM followups")[0][0] == 5  # the count is recorded in the row
    ob.bad = 0  # the sender is back
    r = rig.desk.retry(f"F{lid}")
    assert (r.status, r.body["state"]) == (200, "sent")
    assert rig.query("SELECT auto_attempts FROM followups")[0][0] == 1  # a person's Retry started the count again


def test_backoff_is_waited(env) -> None:
    """The pause after try 1, 2, 3 and 4 is 1, 2, 4 and 8 seconds: one second early nothing is tried, on time it is."""
    ob = FlakyOutbox(bad=10**6)
    rig = env.rig(outbox=ob)
    lid = rig.desk.handle("form", "bo-1", lead()).body["lead_id"]
    assert rig.state(lid) == ("pending", "unreachable")
    rig.desk.reap_once()  # same second: the backoff has not passed
    assert ob.calls == 1
    for pause in (1, 2, 4, 8):
        before = ob.calls
        rig.clock.advance(pause - 1)
        rig.desk.reap_once()
        assert ob.calls == before, f"tried again {pause - 1} s after try {before}, the pause is {pause} s"
        rig.clock.advance(1)
        rig.desk.reap_once()
        assert ob.calls == before + 1, f"no try {pause} s after try {before}"
    assert rig.state(lid) == ("failed", "unreachable") and ob.calls == 5


# ============================== a CRM that cannot tell a repeat is asked once per lead
class SlowQualifier(RecordedQualifier):
    """Only the thread that won the claim asks the model, so no more than one thread ever waits here; the wait is
    short (1 s, well inside the desk's own 9 s bound on a model call) and keeps the AI step open while the other
    threads try to claim the same follow-up."""

    def __init__(self, path: Path, parties: int) -> None:
        super().__init__(path)
        self.barrier = threading.Barrier(parties, timeout=1)

    def qualify(self, lead_id: str, email_key: str) -> Any:
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return super().qualify(lead_id, email_key)


def test_blind_crm_concurrent_once(env) -> None:
    rig = env.rig()
    crm = BlindCrm()
    rig.desk.crm = crm
    parties = 8
    rig.desk.qualifier = SlowQualifier(rig.replies_path, parties)
    lid = rig.desk.process("form", "bc-1", lead()).body["lead_id"]
    start = threading.Barrier(parties, timeout=15)  # all threads reach the claim together

    def drive() -> None:
        start.wait()
        rig.desk.run_followup(lid)
        rig.desk.close()

    threads = [threading.Thread(target=drive) for _ in range(parties)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), "a driver thread did not finish"  # the join result is checked
    assert len(crm.puts) == 1, crm.puts
    assert rig.outbox.count == 1


def test_blind_crm_retry_once(env) -> None:
    rig = env.rig()
    crm = BlindCrm()
    rig.desk.crm = crm
    rig.outbox.fail_next("refuse")
    lid = rig.desk.handle("form", "bc-2", lead()).body["lead_id"]
    assert rig.state(lid)[0] in ("pending", "failed")
    r = rig.desk.retry(f"F{lid}")
    assert (r.status, r.body["state"]) == (200, "sent")
    assert len(crm.puts) == 1, crm.puts
    assert rig.query("SELECT crm_done FROM followups")[0][0] == 1


# ============================== load. one fair line for writers, a bounded number of requests in flight
def _burst(desk: Desk, refs_emails: list[tuple[str, str]]) -> list[Any]:
    out: list[Any] = [None] * len(refs_emails)
    start = threading.Barrier(len(refs_emails) + 1, timeout=60)

    def one(i: int) -> None:
        ref, email = refs_emails[i]
        try:
            start.wait()
            r = desk.handle("form", ref, lead(email=email))
            out[i] = (r.status, r.body)
        except Exception as e:  # recorded, asserted below
            out[i] = ("exc", repr(e))
        finally:
            desk.close()

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(len(refs_emails))]
    for t in threads:
        t.start()
    start.wait()
    for t in threads:
        t.join(180)
    hung = sum(1 for t in threads if t.is_alive())
    assert hung == 0, f"{hung} burst threads did not finish"
    return out


def _slow_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_connect(monkeypatch, lambda sql: time.sleep(SLOW_COMMIT_SECONDS) if sql == "COMMIT" else None)


def _settle(path: Path, timeout: float = 120.0) -> None:
    wait_until(lambda: settled(path), timeout=timeout, what="no open receipt, no sending, no pending")


def _check_burst_answers(out: list[Any]) -> tuple[int, int]:
    """Every answer is a real one (never an exception or None); a 503 carries no receipt. Returns (created, served):
    created = 201 answers, served = any 2xx answer."""
    bad = [o for o in out if o is None or o[0] not in (200, 201, 202, 503) or (o[0] == 503 and o[1]["receipt_id"])]
    assert not bad, bad[:3]
    return sum(1 for o in out if o[0] == 201), sum(1 for o in out if 200 <= o[0] < 300)


def test_burst_distinct_emails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _slow_commit(monkeypatch)
    desk = mk_desk(tmp_path, stall_seconds=1)
    desk.start_background(0.1)
    desk.startup()
    try:
        out = _burst(desk, [(f"b{i}", f"b{i}@example.test") for i in range(BURST)])
        created, served = _check_burst_answers(out)
        # the desk must really serve under load: at least one full in-flight line of new leads, and most of the burst
        assert created >= MIN_SERVED, f"only {created} of {BURST} leads created: {[o[0] for o in out].count(503)} busy"
        assert served == created, "a distinct email must be answered 201, never 200 or 202"
        _settle(tmp_path / "desk.sqlite3")
        c = counts_sql(tmp_path / "desk.sqlite3")
        assert c["leads"] == created
        assert c["sent"] == created and desk.outbox.count == created
        assert (c["failed"], c["unknown"]) == (0, 0)
    finally:
        desk.stop_background()
        desk.close()


def test_burst_same_email(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _slow_commit(monkeypatch)
    desk = mk_desk(tmp_path, stall_seconds=1)
    desk.start_background(0.1)
    desk.startup()
    try:
        out = _burst(desk, [(f"s{i}", "same@example.test") for i in range(BURST)])
        _, served = _check_burst_answers(out)
        assert served >= MIN_SERVED, f"only {served} of {BURST} answered with success"
        _settle(tmp_path / "desk.sqlite3")
        assert counts_sql(tmp_path / "desk.sqlite3")["leads"] == 1
        assert desk.outbox.count == 1
        assert desk.crm.count == 1
    finally:
        desk.stop_background()
        desk.close()


class HoldAll(BlockAt):
    """Holds EVERY call at point "A" (after its receipt is stored) until released, and counts how many arrived."""

    def __init__(self, point: str, want: int) -> None:
        super().__init__(point)
        self.arrived, self.want, self._lock = 0, want, threading.Lock()
        self.all_in = threading.Event()

    def crash_point(self, name: str, conn: Any) -> None:
        if name != self.point:
            return
        with self._lock:
            self.arrived += 1
            if self.arrived >= self.want:
                self.all_in.set()
        self.release.wait(timeout=WAIT_LIMIT)


def _limit() -> int:
    return int(getattr(gate, "INFLIGHT_MAX", 32))


def _short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The place-wait before 503 busy shortened to 0.2 s, wherever the desk reads it from."""
    from lead_desk import desk as desk_module

    for mod in (desk_module, gate):
        monkeypatch.setattr(mod, "INFLIGHT_WAIT_SECONDS", 0.2, raising=False)


def test_inflight_limit_503_busy(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Public behaviour only: with the real limit (32) calls held inside the desk, call 33 is answered 503 busy with no
    receipt; the 32 held calls all finish with 201; afterwards a new call works (nothing leaked)."""
    n = _limit()
    assert n == 32, f"the documented limit is 32, the desk says {n}"
    _short_wait(monkeypatch)
    hook = HoldAll("A", n)
    rig = env.rig(hooks=hook)
    results: list[Any] = [None] * n

    def held(i: int) -> None:
        try:
            r = rig.desk.process("form", f"inf-{i}", lead(email=f"inf{i}@example.test"))
            results[i] = r.status
        finally:
            rig.desk.close()

    threads = [threading.Thread(target=held, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    try:
        assert hook.all_in.wait(20), f"only {hook.arrived} of {n} calls got inside the desk"
        extra = rig.desk.process("form", "inf-extra", lead(email="extra@example.test"))
        assert (extra.status, extra.body) == (503, {"error": "busy", "receipt_id": None})
        assert rig.query("SELECT count(*) FROM receipts WHERE source_ref='inf-extra'")[0][0] == 0
    finally:
        hook.release.set()
        for t in threads:
            t.join(30)
    assert not any(t.is_alive() for t in threads), "held calls did not finish"
    assert results == [201] * n, results
    again = rig.desk.process("form", "inf-after", lead(email="after@example.test"))
    assert again.status == 201, "a place was not given back after the held calls finished"


def test_inflight_places_return_after_errors(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """More failing calls than there are places, one after the other, must not use the places up."""
    _short_wait(monkeypatch)
    rig = env.rig()
    real = identity.check_payload

    def boom(obj: object) -> Any:
        raise ValueError("a bug of ours")

    monkeypatch.setattr(identity, "check_payload", boom)
    for i in range(_limit() + 5):
        assert rig.desk.process("form", f"err-{i}", lead(email=f"err{i}@example.test")).status == 500
    monkeypatch.setattr(identity, "check_payload", real)
    for i in range(_limit() + 5):  # busy answers on the way must not eat places either
        r = rig.desk.process("form", f"ok-{i}", lead(email=f"ok{i}@example.test"))
        assert r.status in (201, 200), (i, r.status, r.body)


def test_gate_is_first_come_first_served() -> None:
    g = gate.FairGate()
    g.acquire(1)
    order: list[int] = []
    threads = []
    for i in range(6):
        t = threading.Thread(target=lambda i=i: (g.acquire(10), order.append(i), g.release()))
        t.start()
        threads.append(t)
        wait_until(lambda n=i + 1: len(g._waiters) == n, timeout=5, what="waiter queued")
    g.release()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "a waiter never got its turn"
    assert order == [0, 1, 2, 3, 4, 5]
    g.acquire(1)
    with pytest.raises(gate.GateTimeout):
        g.acquire(0.05)
    g.release()
    g.acquire(1)  # still usable after a timeout
    g.release()


# ============================== /counts
def test_counts_ok_needs_clean(env) -> None:
    rig = env.rig()
    d = rig.desk
    d.handle("form", "req-1", lead(email="c1@example.test"))
    assert d.counts(1).body["ok"] is True
    # a failed follow-up (set directly: a refused send is retried by the desk first)
    lid = d.process("form", "req-2", lead(email="c2@example.test")).body["lead_id"]
    rows_set(rig, "failed", lid)
    body = d.counts(2).body
    assert body["failed_followups"] == 1
    assert body["ok"] is False
    # an unknown one (blind sender, reply lost)
    rig2 = env.rig(outbox="blind")
    rig2.outbox.fail_next("drop")
    rig2.desk.handle("form", "req-3", lead(email="c3@example.test"))
    b2 = rig2.desk.counts(1).body
    assert (b2["unknown_followups"], b2["ok"]) == (1, False)
    # a pending one (decided, never driven)
    rig3 = env.rig()
    rig3.desk.process("form", "req-4", lead(email="c4@example.test"))
    b3 = rig3.desk.counts(1).body
    assert (b3["pending_followups"], b3["ok"]) == (1, False)
    assert "sending_followups" in b3


# ============================== apostrophe in the part before the @
def test_apostrophe_local_part(env) -> None:
    assert identity.check_payload({"name": "N", "email": "o'brien@example.test"}).kind == "ok"
    for bad in ("a@exa'mple.test", "a@example.test'", "o'b rien@example.test", 'o"brien@example.test'):
        assert identity.check_payload({"name": "N", "email": bad}).reason == "invalid_email", bad
    rig = env.rig()
    r = rig.desk.handle("form", "ap-1", lead(email="o'brien@example.test"))
    assert r.status == 201


# ============================== a replay with other content is not lost silently
def test_replay_with_different_payload(env) -> None:
    rig = env.rig()
    d = rig.desk
    first = d.handle("form", "rp-1", lead(email="rp@example.test"))
    rid = first.body["receipt_id"]
    again = d.process("form", "rp-1", lead(email="rp@example.test", name="Someone Else"))
    assert again.status == 201 and again.body["payload_matches"] is False
    assert again.body["lead_id"] == first.body["lead_id"]  # the first answer stands
    items = [i for i in d.review().body if i["item_id"] == f"D{rid}"]
    assert len(items) == 1 and items[0]["kind"] == "replay_payload_differs"
    r = d.retry(f"D{rid}")
    assert (r.status, r.body["error"]) == (409, "not_actionable")
    # an identical replay adds nothing
    d.process("form", "rp-2", lead(email="rp2@example.test"))
    d.process("form", "rp-2", lead(email="rp2@example.test"))
    assert not [i for i in d.review().body if i["item_id"] != f"D{rid}" and i["kind"] == "replay_payload_differs"]


# ============================== start-up does not block serving
def test_startup_fast_reaper_sends(tmp_path: Path) -> None:
    n = 50
    old = Clock(1_000)  # the pending rows are old enough to be due at once
    d1 = mk_desk(tmp_path, now=old)
    d1.startup()
    for i in range(n):
        assert d1.process("form", f"p{i}", lead(email=f"p{i}@example.test")).status == 201
    d1.close()
    slow = KeyedOutbox()
    slow.delay = 0.3  # driving all 50 takes about 15 s, so a start-up that waited for them is far past the limit below
    d2 = mk_desk(tmp_path, outbox=slow, now=default_now)
    d2.start_background(0.05)
    t0 = time.monotonic()
    d2.startup()
    startup_seconds = time.monotonic() - t0
    try:
        assert startup_seconds < STARTUP_LIMIT, f"startup blocked for {startup_seconds:.1f} s"
        t1 = time.monotonic()
        assert d2.counts(None).body["received"] == n
        assert time.monotonic() - t1 < STARTUP_LIMIT
        wait_until(lambda: slow.count == n, timeout=90, what="reaper sends all 50")
        thread = d2._thread
        d2.stop_background()
        assert thread is not None and not thread.is_alive(), "the reaper thread is still running"
    finally:
        d2.stop_background()
        d2.close()


def test_reaper_drives_pending_after_grace(env) -> None:
    """A fresh pending follow-up is left to its request for the grace period; after that the reaper sends it."""
    rig = env.rig()
    lid = rig.desk.process("form", "gr-1", lead()).body["lead_id"]  # decided, not driven
    assert rig.state(lid)[0] == "pending"
    rig.desk.reap_once()  # same second: inside the grace
    assert rig.state(lid)[0] == "pending" and rig.outbox.count == 0
    rig.clock.advance(3)
    rig.desk.reap_once()
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1


# ============================== one desk per lead book, whatever the path
def test_symlink_is_same_book(tmp_path: Path) -> None:
    real = tmp_path / "book.sqlite3"
    link = tmp_path / "alias.sqlite3"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("this account cannot make symbolic links (Windows privilege); the test runs on Linux")
    first = lockfile.acquire(real)
    try:
        with pytest.raises(lockfile.LockHeldError):
            lockfile.acquire(link)
        with pytest.raises(lockfile.LockHeldError):
            lockfile.acquire(tmp_path / "sub" / ".." / "book.sqlite3")
    finally:
        first.close()
    again = lockfile.acquire(link)  # released with the handle
    again.close()


def test_other_errors_are_not_reported(tmp_path: Path) -> None:
    with pytest.raises(lockfile.LockUnavailableError) as info:
        lockfile.acquire(tmp_path / "missing-folder" / "book.sqlite3")
    assert not isinstance(info.value, BlockingIOError)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot delete a file that is held open; runs on Linux")
def test_replaced_lock_file(tmp_path: Path) -> None:
    book = tmp_path / "book.sqlite3"
    f = lockfile.acquire(book)
    try:
        path = lockfile.lock_path(book)
        assert lockfile._same_file(f, path)
        os.unlink(path)
        assert not lockfile._same_file(f, path)
        path.write_bytes(b"")
        assert not lockfile._same_file(f, path)  # a new file at the same name is not the file we hold
    finally:
        f.close()


# ============================== start-up is for a desk that has not served yet
def test_startup_once_only(env) -> None:
    rig = env.rig(start=False)
    rig.desk.process("form", "g-1", lead())
    with pytest.raises(RuntimeError, match="(?i)startup"):
        rig.desk.startup()
    rig2 = env.rig()
    with pytest.raises(RuntimeError, match="(?i)startup"):
        rig2.desk.startup()
    rig3 = env.rig(start=False)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):  # the book has no tables before startup()
        rig3.desk.reap_once()  # but a repair pass counts as serving all the same
    with pytest.raises(RuntimeError, match="(?i)startup"):
        rig3.desk.startup()


def test_stall_seconds_zero_the_reaper_and_a_handler_in_the_middle_decide_once(tmp_path: Path) -> None:
    """DESK_STALL_SECONDS=0 is a legal setting. With it every open receipt is "stalled" at once, so the reaper takes
    a receipt whose handler is still in the middle of deciding it. The verdict is written once: one lead, one CRM row,
    one email, and the handler's own answer is a normal one (not an error, not a second verdict)."""
    hook = BlockAt("A")  # holds the handler after its receipt is stored
    desk = mk_desk(tmp_path, hooks=hook, stall_seconds=0)
    desk.startup()
    db_path = tmp_path / "desk.sqlite3"
    replies: list[Any] = []
    t = threading.Thread(target=lambda: replies.append(desk.handle("form", "z1", lead(email="z1@example.test"))))
    t.start()
    try:
        assert hook.reached.wait(timeout=15)
        assert counts_sql(db_path)["open"] == 1  # the handler is held, its receipt is open
        done = desk.reap_once()  # stall 0: that receipt is already stalled, the reaper decides it
        assert done["receipts"] == 1
        assert counts_sql(db_path)["open"] == 0
    finally:
        hook.release.set()
        t.join(timeout=30)
        assert not t.is_alive()
    assert len(replies) == 1 and 200 <= replies[0].status < 300, replies
    wait_until(lambda: settled(db_path), what="follow-up finished")
    c = counts_sql(db_path)
    assert (c["open"], c["leads"], c["sent"], c["failed"], c["unknown"]) == (0, 1, 1, 0, 0)
    assert desk.outbox.count == 1 and desk.crm.count == 1
    decided = rows(db_path, "SELECT count(*) FROM receipts WHERE outcome IS NOT NULL")[0][0]
    assert decided == 1  # exactly one receipt, decided once
    desk.close()
