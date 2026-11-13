"""Tests for the in-flight limit, the intent mark before a send, resolve answers, input characters and adapter
replies. Made-up data only."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from support import CrashAt, lead, start_serving, stop_serving

from lead_desk import db, gate, identity
from lead_desk import desk as desk_module
from lead_desk.contract import SimulatedCrash


def new_lead(rig: Any, ref: str = "req-1", **over: Any) -> str:
    reply = rig.desk.process("form", ref, lead(**over))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def set_row(rig: Any, lid: str, **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    con = sqlite3.connect(rig.db_path, timeout=10)
    try:
        con.execute(f"UPDATE followups SET {sets} WHERE lead_id=?", (*cols.values(), lid))  # noqa: S608
        con.commit()
    finally:
        con.close()


def col(rig: Any, name: str) -> Any:
    return rig.query(f"SELECT {name} FROM followups")[0][0]  # noqa: S608


# =====
# the in-flight limit is first come, first served
# =====
def test_inflight_places_go_in_arrival_order() -> None:
    sem = gate.FairSemaphore(1)
    assert sem.acquire(timeout=1)
    order: list[int] = []

    def waiter(i: int) -> None:
        if sem.acquire(timeout=5):
            order.append(i)
            sem.release()

    threads = []
    for i in range(5):
        t = threading.Thread(target=waiter, args=(i,))
        t.start()
        threads.append(t)
        deadline = time.monotonic() + 5
        while (
            len(sem._waiters) < i + 1 and time.monotonic() < deadline
        ):  # this waiter has its ticket: the order is fixed
            time.sleep(0.005)
        assert len(sem._waiters) == i + 1
    sem.release()
    for t in threads:
        t.join(5)
    assert order == [0, 1, 2, 3, 4]


def _wait_until(done: Callable[[], bool]) -> None:
    """Poll for a condition (never a fixed pause): the test goes on as soon as it holds."""
    deadline = time.monotonic() + 5
    while not done() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert done()


def test_a_late_arrival_cannot_take_a_released_place() -> None:
    sem = gate.FairSemaphore(1)
    sem.acquire(timeout=1)
    got: list[str] = []
    t = threading.Thread(target=lambda: got.append("old") if sem.acquire(timeout=5) else None)
    t.start()
    deadline = time.monotonic() + 5
    while not sem._waiters and time.monotonic() < deadline:  # the old waiter has its ticket
        time.sleep(0.005)
    assert len(sem._waiters) == 1
    sem.release()  # handed to the old waiter at once
    assert sem.acquire(timeout=0.05) is False, "a late arrival barged in"
    t.join(5)
    assert got == ["old"]


# =====
# follow-up drives cannot crowd out request writes
# =====
def test_a_request_write_is_served_before_a_follow_up_drive() -> None:
    g = gate.FairGate()
    g.acquire(1)
    served: list[str] = []

    def take(name: str, low: bool) -> None:
        g.acquire(5, low=low)
        served.append(name)
        g.release()

    t1 = threading.Thread(target=take, args=("drive", True))
    t1.start()
    _wait_until(lambda: len(g._low) == 1)  # the drive stands in the low line
    t2 = threading.Thread(target=take, args=("request", False))
    t2.start()
    _wait_until(lambda: len(g._waiters) == 1)  # the request stands in the normal line
    g.release()
    t1.join(5)
    t2.join(5)
    assert served == ["request", "drive"]


def test_drives_have_their_own_places(env, monkeypatch) -> None:
    rig = env.rig()
    lid = new_lead(rig)
    monkeypatch.setattr(desk_module, "DRIVE_WAIT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(rig.desk, "_drives", gate.FairSemaphore(0), raising=False)
    monkeypatch.setattr(rig.desk, "_background", True)  # a reaper is running: it takes over what is not driven now
    rig.desk.run_followup(lid)
    assert rig.state(lid)[0] == "pending" and col(rig, "attempts") == 0, "no place: the row must stay untouched"


def test_without_a_reaper_a_drive_waits_for_its_place(env, monkeypatch) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-2")
    places = gate.FairSemaphore(1)
    places.acquire(timeout=1)
    monkeypatch.setattr(rig.desk, "_drives", places, raising=False)
    monkeypatch.setattr(desk_module, "DRIVE_WAIT_SECONDS", 0.05, raising=False)  # must not apply: no reaper
    threading.Timer(0.3, places.release).start()
    rig.desk.run_followup(lid)
    assert rig.state(lid)[0] == "sent", "with no reaper the drive gave up its row"


# =====
# a busy answer is not an error of the follow-up
# =====
def test_busy_after_the_claim_gives_the_try_back(env, monkeypatch) -> None:
    rig = env.rig()
    lid = new_lead(rig)

    def busy(*a: Any, **k: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rig.desk, "_mark_crm", busy)
    rig.desk.run_followup(lid)
    state, err = rig.state(lid)
    assert state == "pending" and err != "internal:OperationalError", (state, err)
    assert col(rig, "auto_attempts") == 0, "the busy answer burned an automatic try"
    assert rig.outbox.count == 0


# =====
# one deadline, no re-queue at the tail
# =====
def test_a_gate_wait_that_ran_out_is_not_queued_again(env, monkeypatch) -> None:
    rig = env.rig()
    monkeypatch.setattr(desk_module, "GATE_WAIT_SECONDS", 0.3)
    rig.desk._gate.acquire(1)
    try:
        t0 = time.monotonic()
        reply = rig.desk.process("form", "d-busy", lead())
        took = time.monotonic() - t0
    finally:
        rig.desk._gate.release()
    assert reply.status == 503, reply.body
    assert took < 0.7, f"the busy answer took {took:.2f}s: the wait was queued again"
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0


def test_the_whole_request_has_one_deadline(env, monkeypatch) -> None:
    rig = env.rig()
    monkeypatch.setattr(desk_module, "REQUEST_DEADLINE_SECONDS", 0.2, raising=False)
    rig.desk._gate.acquire(1)
    try:
        t0 = time.monotonic()
        reply = rig.desk.process("form", "d-dl", lead())
        took = time.monotonic() - t0
    finally:
        rig.desk._gate.release()
    assert reply.status == 503 and took < 1.0, (reply.status, took)


# =====
# the reaper's grace is longer than the longest result write
# =====
def test_grace_exceeds_the_longest_result_write() -> None:
    from lead_desk.constants import BUSY_WAIT_SECONDS

    longest = gate.WRITE_DEADLINE_SECONDS + BUSY_WAIT_SECONDS  # all tries of one write + one SQLite busy wait
    assert gate.SENDING_GRACE_BUSY_WAITS * BUSY_WAIT_SECONDS > longest


# =====
# the intent mark before the send
# =====
def test_blind_kill_before_the_send_goes(env) -> None:
    rig = env.rig(outbox="blind", hooks=CrashAt("D1"))
    lid = new_lead(rig)
    with pytest.raises(SimulatedCrash):
        rig.desk.run_followup(lid)
    assert col(rig, "crm_done") == 1 and col(rig, "send_started") == 0
    rig.restart()
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1, rig.state(lid)


def test_blind_kill_after_the_mark_is_unknown(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-3")
    set_row(rig, lid, state="sending", crm_done=1, send_started=1, attempts=1, auto_attempts=1)
    rig.desk._requeue_sending(None)
    assert rig.state(lid)[0] == "unknown" and col(rig, "ambiguous_prior") == 1


def test_requeue_without_a_read_sender_mode_keeps_marked_rows(env) -> None:
    rig = env.rig(outbox="blind")
    a, b = new_lead(rig, "req-4"), new_lead(rig, "req-5", email="other@example.test")
    set_row(rig, a, state="sending", crm_done=1, send_started=0, attempts=1, auto_attempts=1)
    set_row(rig, b, state="sending", crm_done=1, send_started=1, attempts=1, auto_attempts=1)
    rig.desk._requeue_sending(None, mode_known=False)
    assert rig.state(a)[0] == "pending" and rig.state(b)[0] == "sending"


def test_the_claim_clears_the_mark_and_the_send_sets_it(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-6")
    set_row(rig, lid, send_started=1)
    seen: list[int] = []
    real = rig.outbox.send

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(int(col(rig, "send_started")))
        return real(*a, **k)

    rig.outbox.send = spy
    rig.desk.run_followup(lid)
    assert seen == [1] and rig.state(lid)[0] == "sent"


def test_migration_adds_the_column_and_marks_a_started_send(tmp_path) -> None:
    con = sqlite3.connect(tmp_path / "book.sqlite3", isolation_level=None)
    db.init_schema(con)
    con.execute("INSERT INTO receipts(source, source_ref, received_at, payload) VALUES('form','x',1,'{}')")
    con.execute(
        "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at)"
        " VALUES('Lx',1,'form','n','a@example.test','a@example.test',1)"
    )
    con.execute("INSERT INTO followups(lead_id, state, updated_at, crm_done, attempts) VALUES('Lx','sending',1,1,1)")
    con.execute("ALTER TABLE followups DROP COLUMN send_started")
    db.init_schema(con)
    assert con.execute("SELECT send_started FROM followups").fetchone()[0] == 1
    con.close()


# =====
# resolve answers fit the row
# =====
@pytest.mark.parametrize("crm", [0, 2])
def test_sent_is_refused_when_the_crm_step_is_not_done(env, crm) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, f"unsent-{crm}")
    set_row(rig, lid, state="unknown", last_error="crash_while_sending", crm_done=crm, attempts=1)
    reply = rig.desk.resolve(f"F{lid}", "sent")
    assert reply.status == 409 and reply.body["error"] == "wrong_decision", reply.body
    assert rig.state(lid)[0] == "unknown"


def test_sent_is_accepted_when_the_crm_step_is_done(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-7")
    set_row(rig, lid, state="unknown", last_error="crash_while_sending", crm_done=1, attempts=1)
    assert rig.desk.resolve(f"F{lid}", "sent").status == 200


def test_crm_present_is_accepted_when_a_put_was_started(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-8")
    set_row(rig, lid, state="unknown", last_error="crash_while_sending", crm_done=2, attempts=1)
    reply = rig.desk.resolve(f"F{lid}", "crm_present")
    assert reply.status == 200 and col(rig, "crm_done") == 1 and rig.state(lid)[0] == "failed"


# =====
# more checks on the follow-up writers
# =====
def test_no_second_email_when_the_sender_cannot_be_read(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-9")
    set_row(rig, lid, ambiguous_prior=1)

    def down() -> None:
        raise OSError("down")

    rig.outbox.refresh = down
    rig.desk.run_followup(lid)
    assert rig.outbox.count == 0 and rig.state(lid)[0] == "pending", rig.state(lid)


def test_an_error_before_the_send_is_not_an_unclear_send(env, monkeypatch) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-10")

    def boom(*a: Any, **k: Any) -> bool:
        raise ValueError("before the send")

    monkeypatch.setattr(rig.desk, "_touch", boom)
    rig.desk.run_followup(lid)
    # _touch also runs once before the AI step: that one raises first, nothing was sent either way
    assert rig.state(lid)[0] == "pending" and rig.outbox.count == 0
    assert col(rig, "ambiguous_prior") == 0


def test_pre_send_failure_in_send_claimed_leaves_the_row_pending(env, monkeypatch) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-11")
    calls = {"n": 0}
    real = rig.desk._touch

    def second_fails(lead_id: str, token: int, send_started: bool = False) -> bool:
        calls["n"] += 1
        if send_started:
            raise ValueError("before the send")
        return real(lead_id, token, send_started)

    monkeypatch.setattr(rig.desk, "_touch", second_fails)
    rig.desk.run_followup(lid)
    assert rig.state(lid) == ("pending", "internal:ValueError") and rig.outbox.count == 0
    assert col(rig, "ambiguous_prior") == 0


def test_a_row_stamped_in_the_future_ages_normally(env) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-12")
    now = rig.clock()
    set_row(rig, lid, state="sending", crm_done=0, attempts=1, auto_attempts=1, updated_at=now + 100_000)
    rig.clock.advance(60)
    rig.desk.reap_once()  # the stamp is set to now here
    rig.clock.advance(60)
    rig.desk.reap_once()  # and now the row is old enough
    assert rig.state(lid)[0] in ("pending", "sent"), "a clock set back froze the recovery"


def test_a_touch_that_hit_a_lock_is_not_answered_false(env, monkeypatch) -> None:
    """A lock is not "not ours": `_touch` raises the busy error so the drive gives its try back (answering False
    would leave the row `sending` with the try spent). Only a real token or state change answers False."""
    rig = env.rig()
    lid = new_lead(rig, "req-13")
    set_row(rig, lid, state="sending", attempts=1)

    def busy(fn: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rig.desk, "_locked_retry", busy)
    with pytest.raises(sqlite3.OperationalError):
        rig.desk._touch(lid, 1)
    monkeypatch.undo()
    assert rig.desk._touch(lid, 2) is False  # a token that is not the owner's


# =====
# Kelvin sign and friends
# =====
@pytest.mark.parametrize("local", ["Kevin", "keKin", "İvan"])
def test_a_letter_that_lower_cases_to_ascii_is_refused(local) -> None:
    chk = identity.check_payload({"name": "Kev", "email": f"{local}@market.example"})
    assert chk.kind == "review" and chk.reason == "invalid_email"


def test_other_non_ascii_letters_are_still_accepted() -> None:
    chk = identity.check_payload({"name": "Ana", "email": "Éva@market.example.test"})
    assert chk.kind == "ok" and chk.email_key == "éva@market.example.test"


# =====
# the AI label is checked
# =====
@pytest.mark.parametrize("bad", [None, "lukewarm", ""])
def test_an_invalid_ai_label_is_an_error(env, monkeypatch, bad) -> None:
    rig = env.rig()
    lid = new_lead(rig, f"label-{bad}")
    monkeypatch.setattr(rig.qualifier, "qualify", lambda *a, **k: bad)
    rig.desk.run_followup(lid)
    assert col(rig, "ai_label") is None
    assert rig.crm.count == 0
    assert rig.state(lid)[0] == "pending" and rig.state(lid)[1] == "internal:ValueError"
    assert rig.outbox.count == 0


# =====
# item numbers
# =====
@pytest.mark.parametrize("ref", ["0" * 5000 + "1", "007", "00", "0"])
def test_an_item_number_with_leading_zeros_is_not_found(env, ref) -> None:
    rig = env.rig()
    assert rig.desk.process("form", "req-14", lead(email="bad")).status == 202  # a review item R1 exists
    assert rig.desk.resolve(f"R{ref}", "sent").status == 404
    assert rig.desk.retry(f"R{ref}").status == 404


def test_r1_is_found_but_r01_is_not(env) -> None:
    rig = env.rig()
    rig.desk.process("form", "req-15", lead(email="bad"))
    assert rig.desk.retry("R1").status == 409
    assert rig.desk.retry("R01").status == 404


# =====
# process never raises
# =====
@pytest.mark.parametrize("payload", [{1: "a", "b": 2}, {"name": {1, 2}}, {"name": 10**4400}, {"n": object()}])
def test_input_json_that_cannot_be_written_is_a_bad_envelope(env, payload) -> None:
    rig = env.rig()
    reply = rig.desk.process("form", "req-16", payload)
    assert reply.status == 400 and reply.body["error"] == "bad_envelope", reply.body


# =====
# reads have the error shell
# =====
def test_reads_do_not_raise(env) -> None:
    rig = env.rig()
    assert rig.desk.lead("L\ud800x").status in (404, 400)
    for fn in (rig.desk.review, lambda: rig.desk.counts(None)):
        rig.desk.close()
        rig.desk._db_path = "/nonexistent-dir/x.sqlite3"
        assert fn().status == 500


# =====
# invisible and direction-changing characters
# =====
@pytest.mark.parametrize("field", ["name", "company", "phone"])
@pytest.mark.parametrize("ch", ["​", "\u202e", " ", " ", "﻿"])
def test_invisible_characters_in_short_fields_are_refused(field, ch) -> None:
    payload = {"name": "Ada", "email": "ada@example.test", field: f"x{ch}y"}
    assert identity.check_payload(payload).reason == f"bad_field:{field}"


@pytest.mark.parametrize("ch", ["\u202e", " ", " "])
def test_message_refuses_separators_and_direction_marks(ch) -> None:
    base = {"name": "Ada", "email": "ada@example.test"}
    assert identity.check_payload({**base, "message": f"hi{ch}there"}).reason == "bad_field:message"
    assert identity.check_payload({**base, "message": "family \U0001f468‍\U0001f469"}).kind == "ok"


# =====
# adapters read a bounded reply and follow no redirect
# =====
def test_a_redirect_is_not_followed_and_a_long_reply_is_cut() -> None:
    import http.server
    import socketserver

    from lead_desk.adapters import _exchange
    from lead_desk.constants import REPLY_MAX_BYTES

    hits: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()
            else:
                body = b'{"a":"' + b"x" * (REPLY_MAX_BYTES + 10) + b'"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_GET = do_POST

    with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
        port = srv.server_address[1]
        start_serving(srv)
        try:
            import urllib.error

            with pytest.raises(urllib.error.HTTPError):
                _exchange(f"http://127.0.0.1:{port}/redir", {"a": 1}, 5)
            assert hits == ["/redir"], "the redirect was followed"
            with pytest.raises(ValueError):
                _exchange(f"http://127.0.0.1:{port}/big", {"a": 1}, 5)
        finally:
            stop_serving(srv)
