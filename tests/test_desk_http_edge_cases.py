"""Edge cases that go through the HTTP server or the reaper thread. Made-up data only; ports are 0 or free ports;
nothing is written outside tmp_path.

Names the tests rely on:
  Desk.start_background() / stop_background()   the stoppable reaper thread (the `bg` fixture stops it at teardown)
  followups.crm_done                            column of the intent mark
  review item id "D<receipt_id>"                a replay with a different payload
  GET /counts keys failed_followups, unknown_followups, sending_followups
Tests for the same topics in test_desk_edge_cases.py use the desk directly.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from edge_case_helpers import mk_desk
from support import (
    HOST,
    Rig,
    call,
    child_env,
    free_port,
    lead,
    run_threads,
    sql_exec,
    start_proc,
    stop_proc,
    wait_until,
)

from demo_only.fakes import BlindOutbox, FakeCrm, KeyedOutbox, RecordedQualifier
from lead_desk import db, gate, lockfile
from lead_desk.adapters import CrmNothingStored, HttpCrm, HttpOutbox
from lead_desk.config import Config
from lead_desk.constants import EXIT_ANOTHER_DESK
from lead_desk.contract import SendAmbiguous, SendRefused, SendUnreachable
from lead_desk.desk import Desk
from lead_desk.hooks import Hooks

TIME_LIMIT = 5.0  # seconds a start-up or a /counts call may take, however busy the machine


# -- helpers
@pytest.fixture
def bg(env):  # depends on env so the reaper is stopped BEFORE env closes the desks
    started: list[Desk] = []

    def start(desk: Desk) -> None:
        desk.start_background()
        started.append(desk)

    yield start
    for d in started:
        try:
            d.stop_background()
        except Exception:  # noqa: BLE001, S110 (teardown must reach every desk)
            pass


def wait_advancing(rig: Rig, fn: Callable[[], Any], what: str, timeout: float = 15.0, step: int = 5) -> Any:
    """Like wait_until, but the test clock (which the desk reads) moves forward `step` seconds on every poll, so
    backoff, grace and stall times pass without the test sleeping for them."""

    def poll() -> Any:
        val = fn()
        if not val:
            rig.clock.advance(step)  # only when the answer is not there yet, so a result is never jumped over
        return val

    return wait_until(poll, timeout=timeout, what=what)


def reaper_rig(env, bg, **kw: Any) -> Rig:
    """A rig whose desk runs the reaper; started in the documented order (start_background, then startup)."""
    rig = env.rig(start=False, **kw)
    bg(rig.desk)
    rig.desk.startup()
    return rig


def hold_write_lock(db_path: Path, seconds: float) -> Callable[[], None]:
    """Another writer: BEGIN IMMEDIATE on its own connection, released after `seconds` (or at once by the returned
    function, which the test calls in a finally block so no lock outlives its test). The timer is a daemon."""
    con = sqlite3.connect(db_path, timeout=10, isolation_level=None, check_same_thread=False)
    con.execute("BEGIN IMMEDIATE")
    done = threading.Lock()

    def release() -> None:
        if not done.acquire(blocking=False):
            return  # already released
        try:
            con.execute("ROLLBACK")
        finally:
            con.close()

    timer = threading.Timer(seconds, release)
    timer.daemon = True
    timer.start()
    return release


def new_lead(rig: Rig, ref: str = "r1", **over: Any) -> str:
    """Receipt and decision only (no follow-up drive): returns the lead id of the new lead."""
    reply = rig.desk.process("form", ref, lead(**over))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


# =====
# runtime repair without a restart
# =====
def test_reaper_decides_stalled(env, bg) -> None:
    """Expected: an open receipt (no verdict) older than stall_seconds gets its verdict from the stored payload while
    the desk keeps running (no restart); a receipt younger than stall_seconds is left alone."""
    rig = reaper_rig(env, bg)
    old_at, fresh_at = rig.clock.t - 1000, rig.clock.t
    payload = '{"email":"ada@example.test","name":"Ada"}'
    fresh_payload = '{"email":"bob@example.test","name":"Bob"}'
    for ref, at, pl in (("old-1", old_at, payload), ("fresh-1", fresh_at, fresh_payload)):
        sql_exec(
            rig.db_path,
            "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES('form', ?, ?, ?)",
            (ref, at, pl),
        )
    wait_until(
        lambda: rig.query("SELECT outcome FROM receipts WHERE source_ref='old-1'")[0][0] is not None,
        what="the reaper to decide the stalled receipt (no restart)",
    )
    assert rig.query("SELECT outcome FROM receipts WHERE source_ref='old-1'")[0][0] == "new"
    assert rig.query("SELECT outcome FROM receipts WHERE source_ref='fresh-1'")[0][0] is None, (
        "a receipt younger than stall_seconds must not be decided by the reaper"
    )


def test_reaper_requeues_keyed_sending(env, bg) -> None:
    """Expected: a keyed follow-up stuck in 'sending' longer than send-timeout + 5 busy waits (30 s here) goes back
    to pending and is then sent, with no restart; one stuck for less than that is left alone."""
    rig = reaper_rig(env, bg)
    lid = new_lead(rig)
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='sending', attempts=1, updated_at=? WHERE lead_id=?",
        (rig.clock.t, lid),
    )
    rig.clock.advance(10)  # younger than the 30 s limit
    for _ in range(3):  # three whole passes, run now (the clock does not move, so no waiting is needed)
        rig.desk.reap_once()
    assert rig.state(lid)[0] == "sending", "a 'sending' row younger than the limit must be left alone"
    rig.clock.advance(3600)
    wait_advancing(rig, lambda: rig.state(lid)[0] == "sent", what="the stuck 'sending' row to be re-sent")
    assert rig.outbox.count == 1


def test_reaper_blind_sending_unknown(env, bg) -> None:
    """Expected: a blind-sender follow-up stuck in 'sending' becomes 'unknown' (a person decides); the desk never
    sends it again by itself."""
    rig = reaper_rig(env, bg, outbox="blind")
    lid = new_lead(rig)
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='sending', crm_done=1, send_started=1, attempts=1, updated_at=? WHERE lead_id=?",
        (rig.clock.t, lid),
    )
    rig.clock.advance(3600)
    wait_advancing(rig, lambda: rig.state(lid)[0] == "unknown", what="the stuck blind 'sending' row to be unknown")
    for _ in range(3):
        rig.desk.reap_once()
    assert rig.state(lid)[0] == "unknown"
    assert rig.outbox.count == 0, "a blind sender must not be asked again"


def test_receipt_write_retries_7s(env) -> None:
    """Expected: if another writer holds the database for 7 s (longer than one 5 s wait, shorter than three), the
    receipt write retries like the other writes and the request is answered 201, not 503 busy."""
    rig = env.rig()
    release = hold_write_lock(rig.db_path, 7.0)
    try:
        reply = rig.desk.process("form", "busy-1", lead())
    finally:
        release()
    assert reply.status == 201, f"expected 201 after retry, got {reply.status} {reply.body}"
    assert rig.query("SELECT outcome FROM receipts")[0][0] == "new"


def test_decision_write_retries_7s(env) -> None:
    """Expected: the same for the decision write (transaction 2): busy for 7 s after the receipt is stored still
    ends with the verdict, 201, and no open receipt."""

    releases: list[Callable[[], None]] = []

    class HoldAtA(Hooks):
        def __init__(self, db_path: Path) -> None:
            self.db_path, self.done = db_path, False

        def crash_point(self, name: str, conn: sqlite3.Connection | None) -> None:
            if name == "A" and not self.done:
                self.done = True
                releases.append(hold_write_lock(self.db_path, 7.0))

    rig = env.rig(start=False)
    rig.desk.hooks = HoldAtA(rig.db_path)
    rig.desk.startup()
    try:
        reply = rig.desk.process("form", "busy-2", lead())
    finally:
        for release in releases:
            release()
    assert reply.status == 201, f"expected 201 after retry, got {reply.status} {reply.body}"
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0, "receipt left open"


def test_retry_not_200_sending(env, monkeypatch) -> None:
    """Expected: when the result of the send cannot be written (the record step gives up on a busy database), Retry
    must not answer 200 with state 'sending'. (The record step is replaced by a no-op to stand for the swallowed
    busy error; private name _record_send_result as in the code.)"""
    rig = env.rig()
    lid = new_lead(rig)
    sql_exec(rig.db_path, "UPDATE followups SET state='failed', last_error='refused' WHERE lead_id=?", (lid,))
    monkeypatch.setattr(rig.desk, "_record_send_result", lambda *a, **k: None)
    reply = rig.desk.retry("F" + lid)
    assert (reply.status, reply.body.get("error")) == (409, "in_progress"), reply.body
    assert rig.state(lid)[0] == "sending"  # the row is still the claimed one; nothing pretended it was finished


# =====
# mode mismatch after a 2xx reply
# =====
def test_mode_mismatch_unknown(env) -> None:
    """Expected: the sender was restarted in the other mode while the desk runs and answers 2xx in that other mode:
    the email may be stored, so the follow-up is 'unknown' (last_error mode_mismatch), never 'failed'; Retry of it
    is refused (409) and no second email is sent."""
    srv, url = env.fakes("keyed")
    rig = env.rig(outbox=HttpOutbox(url, 5.0))
    srv.state.outbox_mode = "blind"  # the sender restarted in the other mode; the desk's cached capability is keyed
    reply = rig.desk.handle("form", "mm-1", lead())
    lid = reply.body["lead_id"]
    assert call(url, "GET", "/fake/outbox")[1]["count"] == 1, "test set-up: the email was stored"
    assert rig.state(lid) == ("unknown", "mode_mismatch"), f"got {rig.state(lid)}"
    retry = rig.desk.retry("F" + lid)
    assert retry.status == 409, f"Retry of an unknown follow-up must be refused, got {retry.status} {retry.body}"
    assert call(url, "GET", "/fake/outbox")[1]["count"] == 1, "a second email was sent"


def test_capability_read_at_startup(env) -> None:
    """Expected: each start-up sweep re-reads the sender's capability; after the sender restarted blind, a restarted
    desk reports capability 'blind'."""
    srv, url = env.fakes("keyed")
    rig = env.rig(outbox=HttpOutbox(url, 5.0))
    assert rig.outbox.capability == "keyed"
    srv.state.outbox_mode = "blind"
    rig.restart()
    assert rig.outbox.capability == "blind", "start-up did not re-read /fake/info"


# =====
# automatic bounded retry of a send that sent nothing
# =====
class FlakyOutbox(KeyedOutbox):
    """Refuses (or cannot reach) the next `refuse` sends, then behaves."""

    def __init__(self, refuse: int, exc: type[Exception] = SendRefused) -> None:
        super().__init__()
        self.refuse, self.exc, self.tries = refuse, exc, 0

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Any:
        self.tries += 1
        if self.refuse > 0:
            self.refuse -= 1
            raise self.exc("scripted outage")
        return super().send(send_key, to, subject, body, timeout)


@pytest.mark.parametrize("exc", [SendRefused, SendUnreachable])
def test_outage_is_retried_automatically(env, bg, exc) -> None:
    """Expected: the sender refuses (or cannot be reached) for the first 2 sends, then recovers: the follow-up ends
    'sent' with exactly one email and nobody pressed Retry."""
    rig = reaper_rig(env, bg, outbox=FlakyOutbox(2, exc))
    rig.desk.handle("form", "req-1", lead())
    wait_advancing(rig, lambda: rig.state()[0] == "sent", what="automatic retry to send the email")
    assert rig.outbox.count == 1
    assert rig.outbox.tries == 3, f"expected 3 tries, got {rig.outbox.tries}"


def test_failed_only_after_cap(env, bg) -> None:
    """Expected: with the sender always refusing, the desk tries several times (bounded) before 'failed', and stops
    trying once it is 'failed'."""
    rig = reaper_rig(env, bg, outbox=FlakyOutbox(10**6))
    rig.desk.handle("form", "req-2", lead())
    wait_advancing(rig, lambda: rig.state()[0] == "failed", what="the follow-up to end 'failed'")
    tries = rig.outbox.tries
    assert tries > 1, f"'failed' after only {tries} try: nothing retried automatically"
    assert tries <= 10, f"{tries} tries: the retry is not bounded by a small cap"
    rig.clock.advance(3600)
    for _ in range(3):
        rig.desk.reap_once()
    assert rig.state()[0] == "failed"
    assert rig.outbox.tries == tries, "tries continued after 'failed'"


def test_person_retry_resets_the_count(env, bg) -> None:
    """Expected: after the cap ('failed'), a person's Retry starts a fresh count: if the sender refuses once more,
    the follow-up is 'pending' again (more automatic tries follow), not 'failed' at once; then it is sent."""
    rig = reaper_rig(env, bg, outbox=FlakyOutbox(10**6))
    rig.desk.handle("form", "req-3", lead())
    wait_advancing(rig, lambda: rig.state()[0] == "failed", what="the follow-up to end 'failed'")
    assert rig.outbox.tries > 1, "no automatic retry happened before 'failed'"
    rig.outbox.refuse = 1  # one more refusal, then the sender recovers
    lid = rig.query("SELECT lead_id FROM followups")[0][0]
    reply = rig.desk.retry("F" + lid)
    assert reply.status == 200 and reply.body["state"] == "pending", f"after Retry: {reply.body}"
    wait_advancing(rig, lambda: rig.state()[0] == "sent", what="the retried follow-up to be sent")


# =====
# the CRM call runs once per lead even with a blind CRM
# =====
class BlindCrm:
    """A CRM that cannot say 'already there': every put adds a row (the fakes server in crm blind mode)."""

    def __init__(self, delay: float = 0.0) -> None:
        self.rows: list[str] = []
        self.delay = delay
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return len(self.rows)

    def put(self, lead_id: str, record: Any) -> str:
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.rows.append(lead_id)
        return "PUT"


def test_retry_after_failed_one_row(env) -> None:
    """Expected: with a blind CRM, a send that failed and was then retried by a person leaves exactly 1 CRM row for
    the lead."""
    rig = env.rig()
    rig.crm = rig.desk.crm = BlindCrm()
    lid = new_lead(rig)
    rig.outbox.fail_next("refuse")
    rig.desk.run_followup(lid)
    sql_exec(rig.db_path, "UPDATE followups SET state='failed', last_error='refused' WHERE lead_id=?", (lid,))
    reply = rig.desk.retry("F" + lid)
    assert reply.status == 200 and reply.body["state"] == "sent", reply.body
    assert rig.crm.count == 1, f"{rig.crm.count} CRM rows for one lead"
    assert rig.query("SELECT crm_done FROM followups WHERE lead_id=?", (lid,))[0][0] == 1


def test_concurrent_drives_one_row(env) -> None:
    """Expected: six threads drive the same lead at the same moment with a blind (and slow) CRM: 1 CRM row and 1
    email."""
    rig = env.rig()
    rig.crm = rig.desk.crm = BlindCrm(delay=0.2)
    lid = new_lead(rig)
    results = run_threads([lambda: rig.desk.run_followup(lid) for _ in range(6)])
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert rig.crm.count == 1, f"{rig.crm.count} CRM rows for one lead"
    assert rig.outbox.count == 1
    assert rig.state(lid)[0] == "sent"


# =====
# /counts ok
# =====
def test_counts_ok_false_unfinished(env) -> None:
    """Expected: GET /counts?expected=1 has ok false while the only follow-up is pending, failed, unknown or
    sending (and the body names the numbers in the keys failed_followups, unknown_followups and sending_followups, like
    the existing pending_followups); ok is true once it is sent."""
    rig = env.rig()
    base = env.serve(rig)
    lid = new_lead(rig)

    def counts() -> dict[str, Any]:
        status, body = call(base, "GET", "/counts?expected=1")
        assert status == 200
        return body  # type: ignore[no-any-return]

    c = counts()
    assert c["pending_followups"] == 1
    assert c["ok"] is False, f"ok is {c['ok']} while a follow-up is pending"
    assert (c.get("failed_followups"), c.get("unknown_followups"), c.get("sending_followups")) == (0, 0, 0), (
        f"keys: {c}"
    )
    for st in ("failed", "unknown", "sending"):
        sql_exec(rig.db_path, "UPDATE followups SET state=? WHERE lead_id=?", (st, lid))
        c = counts()
        assert c["ok"] is False, f"ok is {c['ok']} while a follow-up is {st}"
        assert c.get(f"{st}_followups") == 1, f"key {st}_followups is {c.get(f'{st}_followups')}, expected 1"
    sql_exec(rig.db_path, "UPDATE followups SET state='sent' WHERE lead_id=?", (lid,))
    c = counts()
    assert c["ok"] is True, f"ok is {c['ok']} when everything is finished: {c}"


# =====
# apostrophe in the local part
# =====
def test_apostrophe_email_accepted(env) -> None:
    """Expected: o'brien@example.test is an accepted lead (RFC 5322 allows the apostrophe); the characters
    " , ; < > and a space inside the address are still refused as invalid_email."""
    rig = env.rig()
    ok = rig.desk.process("form", "ap-1", lead(email="o'brien@example.test"))
    assert (ok.status, ok.body.get("verdict")) == (201, "new"), f"got {ok.status} {ok.body}"
    bad_emails = ['a"b@example.test', "a,b@example.test", "a;b@example.test", "a<b@example.test",
                  "a>b@example.test", "a b@example.test"]  # fmt: skip
    for i, bad in enumerate(bad_emails):
        r = rig.desk.process("form", f"bad-{i}", lead(email=bad))
        assert (r.status, r.body.get("reason")) == (202, "invalid_email"), f"{bad!r}: {r.status} {r.body}"


# =====
# replay with a different payload
# =====
def test_replay_other_payload(env) -> None:
    """Expected: the same request id with a different payload gets the original answer with payload_matches false,
    the stored lead is unchanged, and the review list gets an informational item 'D<receipt id>' (a replay with the
    same payload makes none)."""
    rig = env.rig()
    first = rig.desk.handle("form", "rp-1", lead())
    rid, lid = first.body["receipt_id"], first.body["lead_id"]
    same = rig.desk.process("form", "rp-1", lead())
    assert same.body["payload_matches"] is True
    ids = [i["item_id"] for i in rig.desk.review().body]
    assert f"D{rid}" not in ids, "a matching replay must not make a review item"
    other = rig.desk.process("form", "rp-1", lead(name="Ada Changed", phone="999 0000"))
    assert other.body["replayed"] is True and other.body["payload_matches"] is False
    assert other.body["verdict"] == "new" and other.body["lead_id"] == lid
    assert rig.query("SELECT name FROM leads WHERE lead_id=?", (lid,))[0][0] == "Ada Example"
    items = {i["item_id"]: i for i in rig.desk.review().body}
    assert f"D{rid}" in items, f"no D item for receipt {rid}; review list: {list(items)}"
    assert items[f"D{rid}"]["item_type"] == "receipt"


# =====
# start-up must not block serving
# =====
def test_startup_50_pending(env, bg) -> None:
    """Expected: with 50 pending follow-ups and a slow sender (0.1 s per email), startup() with the reaper returns
    in under 2 s, GET /counts answers within 2 s, and the reaper then sends all 50 by itself."""
    rig = env.rig()
    for i in range(50):
        new_lead(rig, f"bulk-{i}", email=f"user{i}@example.test")
    rig.desk.close()
    slow = KeyedOutbox()
    slow.delay = 0.3  # all 50 take about 15 s: a start-up that waited for them would be far past the limit
    desk2 = Desk(rig.config, FakeCrm(), slow, RecordedQualifier(rig.replies_path), now=rig.clock)
    rig.desks.append(desk2)
    rig.desk, rig.outbox = desk2, slow
    bg(desk2)
    t0 = time.monotonic()
    desk2.startup()
    took = time.monotonic() - t0
    assert took < TIME_LIMIT, f"startup() took {took:.1f} s with 50 pending follow-ups"
    base = env.serve(rig)
    t1 = time.monotonic()
    status, body = call(base, "GET", "/counts", timeout=10)
    took = time.monotonic() - t1
    assert status == 200 and took < TIME_LIMIT, f"/counts took {took:.1f} s"
    wait_advancing(
        rig,
        lambda: rig.query("SELECT count(*) FROM followups WHERE state='sent'")[0][0] == 50,
        what="the reaper to send all 50",
        timeout=90,
    )
    assert slow.count == 50


# =====
# lock file
# =====
def two_free_ports() -> tuple[int, int]:
    a, b = free_port(), free_port()
    while b == a:  # the first socket is closed again, so the OS may hand out the same port twice
        b = free_port()
    return a, b


def _server_env(db: Path, port: int, fakes_url: str, replies: Path) -> dict[str, str]:
    return child_env(
        DESK_DB_PATH=str(db),
        DESK_BIND=HOST,
        DESK_PORT=str(port),
        DESK_FAKES_URL=fakes_url,
        DESK_REPLIES_PATH=str(replies),
    )


def test_symlinked_second_desk(env, tmp_path: Path) -> None:
    """Expected: a second desk started on the same lead book through a symlink (another name for the same file)
    exits with code 2 and says another desk is using the database."""
    _, fakes_url = env.fakes("keyed")
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    db = tmp_path / "desk.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    try:
        alias.symlink_to(db)
    except OSError:
        pytest.skip("this account cannot make symbolic links (Windows privilege); the test runs on Linux")
    port_a, port_b = two_free_ports()
    first = start_proc(["-m", "lead_desk.server"], _server_env(db, port_a, fakes_url, replies))
    second = None
    try:
        wait_until(lambda: first.poll() is not None or call(f"http://{HOST}:{port_a}", "GET", "/counts")[0] == 200)
        assert first.poll() is None, "the first desk must start"
        second = start_proc(["-m", "lead_desk.server"], _server_env(alias, port_b, fakes_url, replies))
        try:
            second.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("a second desk is serving the same lead book through a symlink (it did not exit)")
        out = stop_proc(second)
        assert second.returncode == EXIT_ANOTHER_DESK, out.decode(errors="replace")[-300:]
        assert b"another desk is using this database" in out
    finally:
        if second is not None:
            stop_proc(second)
        stop_proc(first)


def test_lock_file_unopenable(env, tmp_path: Path) -> None:
    """Expected: when the lock file itself cannot be opened (here its name is a directory), the desk exits cleanly
    (not 0) with a message that does NOT say 'another desk is using this database'."""
    _, fakes_url = env.fakes("keyed")
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    db = tmp_path / "desk.sqlite3"
    (tmp_path / "desk.sqlite3.lock").mkdir()
    p = start_proc(["-m", "lead_desk.server"], _server_env(db, free_port(), fakes_url, replies))
    try:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("the desk kept running although it could not take the lock")
    finally:
        out = stop_proc(p)
    assert p.returncode not in (0, None)
    assert b"another desk is using this database" not in out, out.decode(errors="replace")[-300:]


def test_lock_file_replaced(tmp_path: Path, monkeypatch) -> None:
    """Expected: if the lock file is deleted and made again between opening and locking (so the file we locked is
    no longer the file at the path), acquire() notices and returns a handle on the file that is at the path now."""
    fcntl = pytest.importorskip("fcntl")  # POSIX only; the Windows lock path has its own tests

    db = tmp_path.resolve() / "desk.sqlite3"
    path = Path(f"{db}.lock")
    real_flock, calls = fcntl.flock, []

    def flock(fd: int, op: int) -> None:
        if not calls:
            calls.append(1)
            os.unlink(path)
            path.write_bytes(b"")  # a new file with a new inode
        real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", flock)
    f = lockfile.acquire(db)
    try:
        assert os.fstat(f.fileno()).st_ino == os.stat(path).st_ino, "the lock is held on a file no longer at the path"
    finally:
        f.close()


# =====
# startup() twice, or after serving started
# =====
def test_startup_twice_raises(env) -> None:
    """Expected: calling startup() a second time raises."""
    rig = env.rig()  # already started once
    with pytest.raises(RuntimeError, match="(?i)startup"):
        rig.desk.startup()


def test_startup_after_serving(env) -> None:
    """Expected: calling startup() on a desk that has already handled a request raises."""
    rig = env.rig()
    desk2 = Desk(rig.config, FakeCrm(), KeyedOutbox(), RecordedQualifier(rig.replies_path), now=rig.clock)
    rig.desks.append(desk2)
    assert desk2.handle("form", "srv-1", lead()).status == 201
    with pytest.raises(RuntimeError, match="(?i)startup"):
        desk2.startup()


# Migration of a book at schema version 1, and follow-up edge cases. Made-up data only.
def make_book_at_schema_version_1(tmp: Path, leads: int = 0) -> Path:
    """A lead book at schema version 1: user_version 1, no added column, no added index, the version-1 view."""
    d = mk_desk(tmp)
    d.startup()
    for i in range(leads):
        assert d.process("form", f"seed-{i}", lead(email=f"seed{i}@example.test")).status == 201
    d.close()
    con = sqlite3.connect(tmp / "desk.sqlite3", isolation_level=None)
    con.execute("DROP VIEW review_items")
    con.execute("DROP INDEX IF EXISTS receipts_open_ix")
    con.execute("DROP INDEX IF EXISTS followups_active_ix")
    # a dropped table takes its triggers with it, so the migration really has to create tables and triggers again
    for table in ("nudges", "lead_state", "later_decisions"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column in (
        ("receipts", "payload_conflict"),
        ("followups", "crm_done"),
        ("followups", "auto_attempts"),
        ("followups", "ambiguous_prior"),
        ("followups", "send_started"),
    ):
        if column in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    con.execute(
        "CREATE VIEW review_items AS SELECT 'R' || receipt_id AS item_id, 'receipt' AS item_type, 'review' AS kind,"
        " outcome_reason AS reason, NULL AS lead_id, received_at AS since, source FROM receipts WHERE outcome='review'"
    )
    con.close()
    return tmp / "desk.sqlite3"


def sql_names(path: Path, kind: str) -> set[str]:
    return {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def col_names(path: Path, table: str) -> set[str]:
    return {r[1] for r in sqlite3.connect(path).execute(f"PRAGMA table_info({table})")}


def book_shape(path: Path) -> tuple[Any, ...]:
    """Everything a half-done migration could change: columns, indexes, views, the schema version, the stored rows."""
    con = sqlite3.connect(path)
    try:
        return (
            sorted((t, c) for t in ("receipts", "followups", "leads") for c in col_names(path, t)),
            sorted(sql_names(path, "index")),
            sorted(sql_names(path, "view")),
            sorted(sql_names(path, "table")),
            sorted(sql_names(path, "trigger")),
            con.execute("PRAGMA user_version").fetchone()[0],
            con.execute("SELECT count(*) FROM followups").fetchone()[0],
        )
    finally:
        con.close()


ADDED = {"payload_conflict", "crm_done", "auto_attempts", "ambiguous_prior"}


@pytest.mark.parametrize("where", ["start", "end"])
def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """The failure comes right after the added columns (start of the schema text) or at its very end. Either way the
    book is exactly as it was: none of the added columns, no new index, the version-1 view, same version and rows."""
    path = make_book_at_schema_version_1(tmp_path, leads=2)
    before = book_shape(path)
    assert not ADDED & (col_names(path, "followups") | col_names(path, "receipts")), "set-up: not a version-1 book"
    real = db._schema_text
    bad = "SELECT * FROM no_such_table;\n"
    monkeypatch.setattr(db, "_schema_text", lambda: bad + real() if where == "start" else real() + "\n" + bad)
    with pytest.raises(sqlite3.OperationalError, match="no_such_table"):
        mk_desk(tmp_path).startup()
    assert book_shape(path) == before, "a failed migration left part of its work behind"
    monkeypatch.undo()
    d = mk_desk(tmp_path)
    d.startup()
    assert d.review().status == 200
    assert ADDED <= col_names(path, "followups") | col_names(path, "receipts")


def test_book_without_the_view_is_fixed(tmp_path: Path) -> None:
    path = make_book_at_schema_version_1(tmp_path)
    d = mk_desk(tmp_path)
    d.startup()
    d.close()
    sqlite3.connect(path).execute("DROP VIEW review_items").connection.commit()  # a half-finished migration
    d2 = mk_desk(tmp_path)
    d2.startup()
    assert d2.review().status == 200


class ParamProxy:
    """A connection that shows every statement and its parameters to `rule` before it runs."""

    def __init__(self, real: sqlite3.Connection, rule: Callable[[str, tuple[Any, ...]], None]) -> None:
        self._c, self._rule = real, rule

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self._rule(sql, params)
        return self._c.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)


def refuse_crm_done_one(armed: list[bool]) -> Callable[[str, tuple[Any, ...]], None]:
    def rule(sql: str, params: tuple[Any, ...]) -> None:
        writes_one = "SET crm_done=1" in sql or ("SET crm_done=?" in sql and params and params[0] == 1)
        if armed[0] and writes_one:
            raise sqlite3.OperationalError("database is locked")

    return rule


class KeyedCrm:
    """A CRM that keeps one row per lead id and says so."""

    capability = "keyed"

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.calls = 0

    def put(self, lead_id: str, record: Any) -> str:
        self.calls += 1
        if lead_id in self.rows:
            return "ALREADY_PRESENT"
        self.rows[lead_id] = record
        return "PUT"


def lose_crm_done(monkeypatch: pytest.MonkeyPatch, armed: list[bool]) -> None:
    real = db.connect
    monkeypatch.setattr(db, "connect", lambda p: ParamProxy(real(p), refuse_crm_done_one(armed)))


def run_passes(rig: Rig, n: int, step: int = 20) -> None:
    for _ in range(n):
        rig.clock.advance(step)
        rig.desk.reap_once()


def test_blind_crm_not_asked_twice(env, monkeypatch: pytest.MonkeyPatch) -> None:
    armed = [True]
    lose_crm_done(monkeypatch, armed)
    rig = env.rig(outbox=FlakyOutbox(1, SendRefused))
    rig.desk.crm = crm = BlindCrm()
    lid = rig.desk.handle("form", "req-4", lead()).body["lead_id"]
    assert crm.count == 1 and rig.state(lid)[0] == "sending" and rig.outbox.count == 0  # the mark was lost: no send
    armed[0] = False
    run_passes(rig, 3)
    assert crm.count == 1, "a blind CRM was asked a second time"
    assert rig.state(lid) == ("unknown", "crm_unconfirmed")
    assert rig.query("SELECT crm_done FROM followups")[0][0] == 2  # the put started, its end was not recorded
    assert rig.outbox.count == 0


def test_keyed_crm_may_be_asked_again(env, monkeypatch: pytest.MonkeyPatch) -> None:
    armed = [True]
    lose_crm_done(monkeypatch, armed)
    rig = env.rig(outbox=FlakyOutbox(1, SendRefused))
    rig.desk.crm = crm = KeyedCrm()
    lid = rig.desk.handle("form", "req-5", lead()).body["lead_id"]
    assert rig.state(lid)[0] == "sending" and rig.outbox.count == 0 and crm.calls == 1
    armed[0] = False
    run_passes(rig, 4)
    assert rig.state(lid)[0] == "sent" and len(crm.rows) == 1 and crm.calls == 2
    assert rig.query("SELECT crm_done FROM followups")[0][0] == 1


def test_unclear_blind_crm_put_unknown(env) -> None:
    class DropsReply(BlindCrm):
        def put(self, lead_id: str, record: Any) -> str:
            super().put(lead_id, record)
            raise TimeoutError("reply lost")

    rig = env.rig()
    rig.desk.crm = crm = DropsReply()
    lid = rig.desk.handle("form", "req-6", lead()).body["lead_id"]
    run_passes(rig, 3)
    assert crm.count == 1 and rig.state(lid) == ("unknown", "crm_unconfirmed") and rig.outbox.count == 0


def test_person_can_go_on_after_not_sent(env) -> None:
    class DropsOnce(BlindCrm):
        def put(self, lead_id: str, record: Any) -> str:
            super().put(lead_id, record)
            if self.count == 1:
                raise TimeoutError("reply lost")
            return "PUT"

    rig = env.rig()
    rig.desk.crm = DropsOnce()
    lid = rig.desk.handle("form", "req-7", lead()).body["lead_id"]
    assert rig.state(lid) == ("unknown", "crm_unconfirmed")
    assert rig.desk.resolve(f"F{lid}", "not_sent").status == 200
    assert rig.query("SELECT crm_done FROM followups")[0][0] == 0
    r = rig.desk.retry(f"F{lid}")
    assert (r.status, r.body["state"]) == (200, "sent")


class AmbiguousOutbox(KeyedOutbox):
    """Ends unclear (the email did not leave) for the next `bad` sends."""

    def __init__(self, bad: int) -> None:
        super().__init__()
        self.bad, self.tries = bad, 0

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Any:
        self.tries += 1
        if self.bad > 0:
            self.bad -= 1
            raise SendAmbiguous("scripted unclear end")
        return super().send(send_key, to, subject, body, timeout)


def test_keyed_unclear_send_is_retried(env) -> None:
    rig = env.rig(outbox=AmbiguousOutbox(2))
    lid = rig.desk.handle("form", "req-8", lead()).body["lead_id"]
    assert rig.state(lid) == ("pending", "ambiguous")
    run_passes(rig, 4)
    assert rig.state(lid)[0] == "sent" and rig.outbox.tries == 3 and rig.outbox.count == 1


def test_keyed_unclear_send_cap(env) -> None:
    rig = env.rig(outbox=AmbiguousOutbox(10**6))
    lid = rig.desk.handle("form", "req-9", lead()).body["lead_id"]
    run_passes(rig, 30)
    assert rig.state(lid) == ("failed", "ambiguous") and rig.outbox.tries == 5


def test_sent_reply_lost_sent_once(env) -> None:
    rig = env.rig()
    rig.outbox.fail_next("record_then_drop")
    lid = rig.desk.handle("form", "req-10", lead()).body["lead_id"]
    run_passes(rig, 3)
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1


def test_blind_unclear_unknown(env) -> None:
    rig = env.rig(outbox="blind")
    rig.outbox.fail_next("drop")
    lid = rig.desk.handle("form", "req-11", lead()).body["lead_id"]
    run_passes(rig, 3)
    assert rig.state(lid) == ("unknown", "ambiguous") and rig.outbox.count == 0


class FlakyCrm(KeyedCrm):
    def __init__(self, bad: int) -> None:
        super().__init__()
        self.bad = bad

    def put(self, lead_id: str, record: Any) -> str:
        if self.bad > 0:
            self.bad -= 1
            self.calls += 1
            raise CrmNothingStored("scripted outage")
        return super().put(lead_id, record)


def test_crm_outage_is_retried(env) -> None:
    rig = env.rig()
    rig.desk.crm = crm = FlakyCrm(2)
    lid = rig.desk.handle("form", "req-12", lead()).body["lead_id"]
    assert rig.state(lid) == ("pending", "internal:CrmNothingStored")
    run_passes(rig, 4)
    assert rig.state(lid)[0] == "sent" and crm.calls == 3 and len(crm.rows) == 1


def test_crm_outage_fails_at_the_cap(env) -> None:
    rig = env.rig()
    rig.desk.crm = crm = FlakyCrm(10**6)
    lid = rig.desk.handle("form", "req-13", lead()).body["lead_id"]
    run_passes(rig, 30)
    assert rig.state(lid) == ("failed", "internal:CrmNothingStored") and crm.calls == 5


class FlakyQualifier(RecordedQualifier):
    def __init__(self, path: Path, bad: int) -> None:
        super().__init__(path)
        self.bad, self.asked = bad, 0

    def qualify(self, lead_id: str, email_key: str) -> Any:
        self.asked += 1
        if self.bad > 0:
            self.bad -= 1
            raise ConnectionError("scripted outage")
        return super().qualify(lead_id, email_key)


def test_qualifier_outage_is_retried(env) -> None:
    rig = env.rig()
    rig.desk.qualifier = q = FlakyQualifier(rig.replies_path, 2)
    lid = rig.desk.handle("form", "req-14", lead()).body["lead_id"]
    assert rig.state(lid) == ("pending", "internal:ConnectionError")
    run_passes(rig, 4)
    assert rig.state(lid)[0] == "sent" and q.asked == 3


def test_qualifier_outage_cap(env) -> None:
    rig = env.rig()
    rig.desk.qualifier = q = FlakyQualifier(rig.replies_path, 10**6)
    lid = rig.desk.handle("form", "req-15", lead()).body["lead_id"]
    run_passes(rig, 30)
    assert rig.state(lid) == ("failed", "internal:ConnectionError") and q.asked == 5


class GateQualifier(RecordedQualifier):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.reached, self.release = threading.Event(), threading.Event()

    def qualify(self, lead_id: str, email_key: str) -> Any:
        self.reached.set()
        self.release.wait(10)
        return super().qualify(lead_id, email_key)


def test_auto_driver_skips_failed(env) -> None:
    rig = env.rig(start=False)
    rig.desk.startup()
    rig.desk.qualifier = q = GateQualifier(rig.replies_path)
    lid = new_lead(rig, "req-16")
    t = threading.Thread(target=lambda: (rig.desk.run_followup(lid), rig.desk.close()))
    t.start()
    assert q.reached.wait(10)
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='failed', last_error='refused', attempts=5, auto_attempts=5 WHERE lead_id=?",
        (lid,),
    )
    q.release.set()
    t.join(15)
    assert rig.state(lid) == ("failed", "refused")
    assert rig.query("SELECT auto_attempts FROM followups")[0][0] == 5
    assert rig.outbox.count == 0


def test_only_person_takes_failed(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-17")
    sql_exec(rig.db_path, "UPDATE followups SET state='failed', auto_attempts=5 WHERE lead_id=?", (lid,))
    assert rig.desk._claim(None, lid, ("pending",)) is False
    assert rig.desk._claim(None, lid, ("pending", "failed")) is True
    assert rig.query("SELECT state, auto_attempts FROM followups")[0] == ("sending", 1)
    for states in (("sent",), ("failed",), ("failed", "pending"), (), ("pending", "failed", "sent")):
        with pytest.raises(ValueError, match="claim takes"):
            rig.desk._claim(None, lid, states)


class StageClockQualifier(RecordedQualifier):
    def __init__(self, path: Path, rig_clock: Any) -> None:
        super().__init__(path)
        self.clock = rig_clock

    def qualify(self, lead_id: str, email_key: str) -> Any:
        self.clock.advance(15)  # a slow AI step
        return super().qualify(lead_id, email_key)


def test_stage_times_move_updated_at(env) -> None:
    rig = env.rig()
    seen: dict[str, tuple[int, int]] = {}

    def updated_at() -> int:
        return int(rig.query("SELECT updated_at FROM followups")[0][0])

    class SlowCrm(KeyedCrm):
        def put(self, lead_id: str, record: Any) -> str:
            seen["crm"] = (updated_at(), rig.clock.t)
            rig.clock.advance(15)  # a slow CRM
            return super().put(lead_id, record)

    class SeeingOutbox(KeyedOutbox):
        def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Any:
            seen["send"] = (updated_at(), rig.clock.t)
            return super().send(send_key, to, subject, body, timeout)

    rig.desk.crm, rig.desk.outbox = SlowCrm(), SeeingOutbox()
    rig.desk.qualifier = StageClockQualifier(rig.replies_path, rig.clock)
    rig.desk.handle("form", "req-18", lead())
    assert seen["crm"][0] == seen["crm"][1], seen
    assert seen["send"][0] == seen["send"][1], seen


def test_unclear_send_never_resent_blind(env) -> None:
    rig = env.rig(outbox=FlakyOutbox(1, SendRefused))
    lid = new_lead(rig, "req-19")
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='sending', crm_done=1, send_started=1, attempts=1, updated_at=? WHERE lead_id=?",
        (1, lid),
    )
    run_passes(rig, 1)  # stuck sending -> pending (crash_while_sending)
    assert rig.state(lid) == ("pending", "crash_while_sending")
    run_passes(rig, 1)  # the keyed sender refuses: the message text is overwritten
    assert rig.state(lid) == ("pending", "refused")
    blind = BlindOutbox()
    rig.desk.outbox = blind
    run_passes(rig, 2)
    assert rig.state(lid)[0] == "unknown" and blind.count == 0


class RefreshOutbox(KeyedOutbox):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.refreshes, self.fail = 0, fail

    def refresh(self) -> None:
        self.refreshes += 1
        if self.fail:
            raise RuntimeError("sender not reachable")


def test_no_requeue_when_mode_unknown(env) -> None:
    rig = env.rig(outbox=RefreshOutbox())
    lid = new_lead(rig, "req-20")
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='sending', crm_done=1, send_started=1, attempts=1, updated_at=? WHERE lead_id=?",
        (1, lid),
    )
    rig.outbox.fail = True
    run_passes(rig, 3)
    assert rig.state(lid)[0] == "sending", "a stuck row was decided without knowing the sender's mode"
    rig.outbox.fail = False
    run_passes(rig, 1)
    assert rig.state(lid)[0] in ("pending", "sent")


def test_startup_waits_when_mode_unknown(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-21")
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='sending', crm_done=1, send_started=1, attempts=1 WHERE lead_id=?",
        (lid,),
    )
    rig.outbox = RefreshOutbox(fail=True)
    rig.restart()
    assert rig.state(lid)[0] == "sending"


class PoisonOnce:
    """check_payload stand-in that raises `exc` for every call."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __call__(self, obj: object) -> Any:
        raise self.exc


@pytest.mark.parametrize("exc", [RecursionError("deep"), sqlite3.DataError("too big")])
def test_more_errors_get_a_verdict(env, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    rig = env.rig()
    monkeypatch.setattr("lead_desk.identity.check_payload", PoisonOnce(exc))
    r = rig.desk.process("form", "req-22", lead())
    assert (r.status, r.body["reason"]) == (202, "internal_unstorable")
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0


def test_own_value_error_is_500(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    monkeypatch.setattr("lead_desk.identity.check_payload", PoisonOnce(ValueError("a bug")))
    r = rig.desk.process("form", "req-23", lead())
    assert r.status == 500, "a ValueError of our own code must show as an error, not as a lead verdict"


def nested_list(depth: int) -> list[Any]:
    """A list nested `depth` deep. Built here, never kept in a test variable, so a failing assert cannot try to print
    it (repr of such a list raises RecursionError and would hide the real failure)."""
    deep: list[Any] = []
    for _ in range(depth):
        deep = [deep]
    return deep


def test_deeply_nested_extra_keys(env) -> None:
    rig = env.rig()
    r = rig.desk.process("form", "req-24", lead(extra=nested_list(100_000)))
    assert (r.status, r.body) == (400, {"error": "bad_envelope", "receipt_id": None})
    assert rig.desk.counts(None).body["rejected_envelopes"] == 1
    ok = rig.desk.process("form", "req-25", lead(extra={"a": [{"b": [1, 2, {"c": "d"}]}] * 50}))
    assert ok.status == 201


def test_stored_deep_payload_verdict(env) -> None:
    rig = env.rig()
    deep = '{"name":"A","email":"a@example.test","x":' + "[" * 100_000 + "]" * 100_000 + "}"
    sql_exec(
        rig.db_path,
        "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES('form','req-deep-1',?,?)",
        (rig.clock.t - 1000, deep),
    )
    rig.desk.reap_once()
    assert rig.query("SELECT outcome, outcome_reason FROM receipts")[0] == ("review", "internal_unstorable")


def test_refresh_not_every_pass(env) -> None:
    rig = env.rig(outbox=RefreshOutbox())
    base = rig.outbox.refreshes
    for _ in range(10):
        rig.desk.reap_once()
    assert rig.outbox.refreshes - base == 0, rig.outbox.refreshes - base
    rig.clock.advance(gate.OUTBOX_REFRESH_SECONDS + 1)
    rig.desk.reap_once()
    assert rig.outbox.refreshes - base == 1


def test_refresh_when_a_row_is_stuck(env) -> None:
    rig = env.rig(outbox=RefreshOutbox())
    lid = new_lead(rig, "req-26")
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='sending', crm_done=1, send_started=1, attempts=1, updated_at=? WHERE lead_id=?",
        (1, lid),
    )
    base = rig.outbox.refreshes
    rig.desk.reap_once()
    assert rig.outbox.refreshes - base == 1
    assert rig.state(lid)[0] == "pending"


def test_partial_indexes(tmp_path: Path) -> None:
    d = mk_desk(tmp_path)
    d.startup()
    path = tmp_path / "desk.sqlite3"
    assert {"receipts_open_ix", "followups_active_ix"} <= sql_names(path, "index")
    con = sqlite3.connect(path)
    plan = " ".join(
        str(r[3])
        for q in (
            "SELECT receipt_id FROM receipts WHERE outcome IS NULL AND received_at <= 5",
            "SELECT lead_id FROM followups WHERE state IN ('pending','sending') AND state='pending' ORDER BY 1",
            "SELECT count(*) FROM followups WHERE state IN ('pending','sending') AND state='sending' AND updated_at<9",
        )
        for r in con.execute("EXPLAIN QUERY PLAN " + q)
    )
    assert "receipts_open_ix" in plan and "followups_active_ix" in plan, plan


def test_book_at_schema_version_1_gets_the_indexes(tmp_path: Path) -> None:
    path = make_book_at_schema_version_1(tmp_path)
    assert "receipts_open_ix" not in sql_names(path, "index")
    mk_desk(tmp_path).startup()
    assert {"receipts_open_ix", "followups_active_ix"} <= sql_names(path, "index")


def test_gate_per_book(tmp_path: Path) -> None:
    a, b = mk_desk(tmp_path / "a"), mk_desk(tmp_path / "b")
    a.startup()
    b.startup()
    assert gate.gate_for(tmp_path / "a" / "desk.sqlite3") is gate.gate_for(tmp_path / "a" / ".." / "a" / "desk.sqlite3")
    assert gate.gate_for(tmp_path / "a" / "desk.sqlite3") is not gate.gate_for(tmp_path / "b" / "desk.sqlite3")
    out: list[Any] = []

    def other_book() -> None:
        out.append(b.process("form", "req-27", lead()).status)
        b.close()

    with a._write(a._conn()):  # the writer of book A is inside its write line
        t = threading.Thread(target=other_book)
        t.start()
        t.join(3)
        done = not t.is_alive()
    t.join(10)
    assert done and out == [201], "book B waited for book A's write line"


def fake_msvcrt(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    class Fake:
        LK_NBLCK = 2

        @staticmethod
        def locking(fd: int, mode: int, nbytes: int) -> None:
            raise OSError(code, "scripted")

    monkeypatch.setitem(sys.modules, "msvcrt", Fake)


def windows_acquire(monkeypatch: pytest.MonkeyPatch, path: Path) -> Any:
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        return lockfile.acquire(path)
    finally:
        monkeypatch.undo()


def test_windows_held_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for code in (errno.EACCES, errno.EDEADLK):
        fake_msvcrt(monkeypatch, code)
        with pytest.raises(lockfile.LockHeldError):
            windows_acquire(monkeypatch, tmp_path / "w.sqlite3")


def test_windows_other_not_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_msvcrt(monkeypatch, errno.EIO)
    with pytest.raises(lockfile.LockUnavailableError) as info:
        windows_acquire(monkeypatch, tmp_path / "w.sqlite3")
    assert not isinstance(info.value, lockfile.LockHeldError)


def test_endless_replace_unavail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lockfile, "_same_file", lambda f, p: False)
    with pytest.raises(lockfile.LockUnavailableError):
        lockfile.acquire(tmp_path / "x.sqlite3")


def test_entry_points_mark_used(env) -> None:
    for call_it in (
        lambda d: d.retry("Fnone"),
        lambda d: d.resolve("Fnone", "sent"),
        lambda d: d._claim(None, "Lnone", ("pending",)),
    ):
        rig = env.rig(start=False)
        try:
            call_it(rig.desk)
        except sqlite3.OperationalError as e:  # the book has no tables yet; only the "serving" mark matters
            assert "no such table" in str(e)
        with pytest.raises(RuntimeError, match="(?i)startup"):
            rig.desk.startup()


def test_book_at_schema_version_1_is_backfilled(tmp_path: Path) -> None:
    path = make_book_at_schema_version_1(tmp_path, leads=4)
    con = sqlite3.connect(path, isolation_level=None)
    ids = [r[0] for r in con.execute("SELECT lead_id FROM followups ORDER BY lead_id")]
    asked, fresh, sent, crashed = ids
    con.execute("UPDATE followups SET state='sending', attempts=1, ai_label='warm' WHERE lead_id=?", (asked,))
    con.execute("UPDATE followups SET state='sent', attempts=1, ai_label='warm', sent_at=1 WHERE lead_id=?", (sent,))
    con.execute("UPDATE followups SET last_error='crash_while_sending' WHERE lead_id=?", (crashed,))
    con.close()
    conn = db.connect(path)
    db.init_schema(conn)  # the migration alone, before any desk drives anything
    conn.close()
    rows_ = {
        r[0]: r[1:] for r in sqlite3.connect(path).execute("SELECT lead_id, crm_done, ambiguous_prior FROM followups")
    }
    assert rows_[asked][0] == 2 and rows_[fresh][0] == 0 and rows_[sent][0] == 0  # unknowable origin: intent mark
    assert rows_[crashed][1] == 1 and rows_[fresh][1] == 0
    crm = BlindCrm()
    mk_desk(tmp_path, crm=crm).startup()
    assert asked not in crm.rows, "the CRM was asked again for a lead the CRM already holds"


# More follow-up, CRM and reaper edge cases. Made-up data only.
def test_failed_internal_row_gets_crm(tmp_path: Path) -> None:
    path = make_book_at_schema_version_1(tmp_path, leads=2)
    con = sqlite3.connect(path, isolation_level=None)
    failed_id, waiting_id = [r[0] for r in con.execute("SELECT lead_id FROM followups ORDER BY lead_id")]
    con.execute(
        "UPDATE followups SET state='failed', attempts=1, ai_label='warm', last_error='internal:CrmError'"
        " WHERE lead_id=?",
        (failed_id,),
    )
    con.execute(
        "UPDATE followups SET state='pending', attempts=1, ai_label='warm', last_error='refused' WHERE lead_id=?",
        (waiting_id,),
    )
    con.close()
    crm = BlindCrm()
    desk = mk_desk(tmp_path, crm=crm)
    desk.startup()
    marks = dict(sqlite3.connect(path).execute("SELECT lead_id, crm_done FROM followups"))
    # internal:CrmError is an unclear put, so the safe mark 2; a send failure ('refused')
    # had its CRM step first, so 1
    assert marks[failed_id] == 2, "an unclear CRM put may have stored the row: the intent mark"
    assert marks[waiting_id] == 1, "a row that failed in the send had its CRM step first"
    r = desk.retry(f"F{failed_id}")
    assert r.status == 200 and r.body["state"] == "unknown"
    assert crm.rows == [], "a blind CRM is not asked again without a person"
    assert desk.resolve(f"F{failed_id}", "not_sent").status == 200
    r = desk.retry(f"F{failed_id}")
    assert (r.status, r.body["state"]) == (200, "sent")
    assert crm.rows == [failed_id], "after the person's answer the CRM row must be created, once"


class GateCrm(BlindCrm):
    """The first put waits until the test lets it go (a stalled or slow drive)."""

    def __init__(self) -> None:
        super().__init__()
        self.reached, self.release = threading.Event(), threading.Event()

    def put(self, lead_id: str, record: Any) -> str:
        if not self.reached.is_set():
            self.reached.set()
            self.release.wait(10)
        return super().put(lead_id, record)


def test_stalled_owner_writes_nothing(env) -> None:
    rig = env.rig()
    desk = rig.desk
    desk.crm = crm = GateCrm()
    lid = new_lead(rig, "req-28")
    assert desk._claim(None, lid, ("pending",))  # drive 1 claims, then stalls
    rig.clock.advance(60)
    desk.reap_once()  # the reaper puts the stuck row back
    assert rig.state(lid)[0] == "pending"
    rig.clock.advance(60)
    t2 = threading.Thread(target=desk.run_followup, args=(lid,))  # drive 2 claims and is inside the CRM call
    t2.start()
    try:
        assert crm.reached.wait(10)
        assert rig.state(lid)[0] == "sending"
        desk._after_claim(lid, auto=True)  # drive 1 wakes up
        desk._record_send_result(lid, "failed", 1, "stale")  # and tries every kind of write it owns
        assert rig.state(lid)[0] == "sending", "a stalled drive changed the row of the drive that owns it now"
    finally:
        crm.release.set()
        t2.join(10)
    assert rig.state(lid)[0] == "sent" and crm.count == 1 and rig.outbox.count == 1


def test_touch_comes_before_the_ai_step(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-29")
    seen: dict[str, int] = {}

    class SpyQualifier(RecordedQualifier):
        def qualify(self, lead_id: str, email_key: str) -> Any:
            seen["updated_at"] = int(rig.query("SELECT updated_at FROM followups")[0][0])
            return super().qualify(lead_id, email_key)

    rig.desk.qualifier = SpyQualifier(rig.replies_path)
    assert rig.desk._claim(None, lid, ("pending",))
    rig.clock.advance(15)
    rig.desk._after_claim(lid, auto=True)
    assert seen["updated_at"] == rig.clock.t, "updated_at was not moved before the AI step"


def test_poison_receipt_ends_in_review(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    calls: list[int] = []

    def boom(obj: object) -> Any:
        calls.append(1)
        raise ValueError("a bug of ours")

    monkeypatch.setattr("lead_desk.identity.check_payload", boom)
    assert rig.desk.process("form", "req-30", lead()).status == 500
    for _ in range(4):
        rig.clock.advance(rig.config.stall_seconds + 5)
        rig.desk.reap_once()
    body = rig.desk.counts(None).body
    assert (body["open"], body["review"]) == (0, 1), body
    assert rig.query("SELECT outcome_reason FROM receipts")[0][0] == "internal_error"
    assert len(calls) == 3, "the reaper kept running a decision that always fails"


def test_busy_does_not_count(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    real = rig.desk._decide_once

    def locked(receipt_id: int) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rig.desk, "_decide_once", locked)
    for _ in range(5):
        assert rig.desk.process("form", "req-31", lead()).status == 503
    monkeypatch.setattr(rig.desk, "_decide_once", real)
    r = rig.desk.process("form", "req-31", lead())
    assert (r.status, r.body["verdict"]) == (201, "new") and rig.leads() == 1


class DropsFirst(BlindCrm):
    """The first put is stored but its reply is lost."""

    def put(self, lead_id: str, record: Any) -> str:
        super().put(lead_id, record)
        if self.count == 1:
            raise TimeoutError("reply lost")
        return "PUT"


def crm_unconfirmed_lead(rig: Rig) -> str:
    rig.desk.crm = rig.crm = DropsFirst()
    lid = str(rig.desk.handle("form", "req-32", lead()).body["lead_id"])
    assert rig.state(lid) == ("unknown", "crm_unconfirmed")
    return lid


def test_crm_present_then_send(env) -> None:
    rig = env.rig()
    lid = crm_unconfirmed_lead(rig)
    r = rig.desk.resolve(f"F{lid}", "crm_present")
    assert (r.status, r.body["state"]) == (200, "failed")
    assert rig.query("SELECT crm_done FROM followups")[0][0] == 1
    r = rig.desk.retry(f"F{lid}")
    assert (r.status, r.body["state"]) == (200, "sent")
    assert rig.crm.count == 1 and rig.outbox.count == 1, "the CRM must not be asked a second time"


def test_sent_refused_for_crm_question(env) -> None:
    rig = env.rig()
    lid = crm_unconfirmed_lead(rig)
    r = rig.desk.resolve(f"F{lid}", "sent")
    assert (r.status, r.body["error"]) == (409, "wrong_decision")
    assert rig.state(lid) == ("unknown", "crm_unconfirmed") and rig.outbox.count == 0


def test_crm_present_needs_a_crm_question(env) -> None:
    rig = env.rig(outbox="blind")
    rig.outbox.fail_next("drop")
    lid = str(rig.desk.handle("form", "req-33", lead()).body["lead_id"])
    assert rig.state(lid) == ("unknown", "ambiguous")
    r = rig.desk.resolve(f"F{lid}", "crm_present")
    assert (r.status, r.body["error"]) == (409, "wrong_decision") and rig.state(lid)[0] == "unknown"
    assert rig.desk.resolve(f"F{lid}", "sent").status == 200  # the normal answers are unchanged


def test_http_crm_reads_its_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    crm = HttpCrm("http://127.0.0.1:1", 1.0)
    assert crm.capability is None
    answer = {"crm_capability": "keyed"}
    monkeypatch.setattr("lead_desk.adapters._exchange", lambda url, obj, timeout: dict(answer))
    crm.refresh()
    assert crm.capability == "keyed"
    answer["crm_capability"] = "blind"
    crm.refresh()
    assert crm.capability == "blind"
    answer["crm_capability"] = "odd"
    with pytest.raises(Exception, match="unexpected"):
        crm.refresh()
    assert crm.capability == "blind", "a bad answer must keep the last good value"


class ModeFlipCrm(BlindCrm):
    """Looks keyed until it is asked again, then says it is blind (it was restarted in the other mode)."""

    capability = "keyed"

    def refresh(self) -> None:
        self.capability = "blind"


def test_leftover_mark_refreshes_first(env) -> None:
    rig = env.rig()
    rig.desk.crm = crm = ModeFlipCrm()
    lid = new_lead(rig, "req-34")
    sql_exec(rig.db_path, "UPDATE followups SET crm_done=2 WHERE lead_id=?", (lid,))
    rig.desk.run_followup(lid)
    assert crm.count == 0, "a CRM that is blind now was asked again on the strength of an old answer"
    assert rig.state(lid) == ("unknown", "crm_unconfirmed")


def test_blind_again_says_why(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-35")
    sql_exec(rig.db_path, "UPDATE followups SET ambiguous_prior=1, updated_at=1 WHERE lead_id=?", (lid,))
    rig.desk.outbox = BlindOutbox()
    run_passes(rig, 1)
    assert rig.state(lid) == ("unknown", "ambiguous_prior")


def test_requeue_at_the_cap_fails(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-36")
    sql_exec(
        rig.db_path,
        "UPDATE followups SET state='sending', attempts=5, auto_attempts=5, updated_at=1 WHERE lead_id=?",
        (lid,),
    )
    run_passes(rig, 4)
    assert rig.state(lid) == ("failed", "crash_while_sending") and rig.outbox.count == 0


class FlipOnRefreshOutbox(KeyedOutbox):
    """Looks keyed until its mode is read again, then blind."""

    flipped = False

    @property
    def capability(self) -> Any:
        return "blind" if self.flipped else "keyed"

    def refresh(self) -> None:
        self.flipped = True


def test_ambiguous_row_reads_mode_first(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-37")
    sql_exec(rig.db_path, "UPDATE followups SET ambiguous_prior=1 WHERE lead_id=?", (lid,))
    rig.desk.outbox = rig.outbox = FlipOnRefreshOutbox()
    rig.desk.run_followup(lid)
    assert rig.outbox.count == 0, "an earlier unclear send was sent again on the strength of an old mode"
    assert rig.state(lid) == ("unknown", "ambiguous_prior")


def test_replays_cannot_burn_the_cap(env) -> None:
    rig = env.rig(outbox=FlakyOutbox(10**6, SendRefused))
    for _ in range(8):
        rig.desk.handle("form", "req-38", lead())
    assert rig.outbox.tries == 1, "a replay drove the follow-up before its backoff was over"
    assert rig.query("SELECT state, auto_attempts FROM followups")[0] == ("pending", 1)


def test_replay_drives_once_due(env) -> None:
    rig = env.rig(outbox=FlakyOutbox(1, SendRefused))
    rig.desk.handle("form", "req-39", lead())
    assert rig.state()[0] == "pending"
    rig.clock.advance(30)
    rig.desk.handle("form", "req-39", lead())
    assert rig.state()[0] == "sent"


def test_lost_mark_stops_send(env, monkeypatch: pytest.MonkeyPatch) -> None:
    armed = [True]
    lose_crm_done(monkeypatch, armed)
    rig = env.rig()
    rig.desk.crm = crm = KeyedCrm()
    lid = rig.desk.handle("form", "req-40", lead()).body["lead_id"]
    assert (rig.state(lid)[0], rig.outbox.count, crm.calls) == ("sending", 0, 1)
    armed[0] = False
    run_passes(rig, 4)
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1


def test_path_is_fixed_at_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(here)
    cfg = Config(db_path=Path("rel.sqlite3"), port=0, replies_path=replies)
    desk = Desk(cfg, FakeCrm(), KeyedOutbox(), RecordedQualifier(replies))
    desk.startup()
    monkeypatch.chdir(elsewhere)
    out: list[int] = []
    t = threading.Thread(target=lambda: out.append(desk.process("form", "req-41", lead()).status))
    t.start()
    t.join(10)
    desk.close()
    assert out == [201], "a new thread opened another file after the working folder changed"
    assert (here / "rel.sqlite3").exists() and not (elsewhere / "rel.sqlite3").exists()


def test_a_pass_has_a_time_budget(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    for i in range(4):
        new_lead(rig, f"burst-{i}", email=f"burst{i}@example.test")
    rig.clock.advance(30)
    driven: list[str] = []

    def slow(lead_id: str) -> None:
        driven.append(lead_id)
        time.sleep(0.06)

    monkeypatch.setattr("lead_desk.desk.REAPER_PASS_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(rig.desk, "run_followup", slow)
    rig.desk.reap_once()
    assert 1 <= len(driven) < 4, f"one pass drove {len(driven)} follow-ups with a 0.05 s budget"
