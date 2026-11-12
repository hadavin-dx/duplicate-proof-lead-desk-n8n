"""Desk tests that drive the real HTTP server and child processes. Made-up data only.
Ports: the server and the fakes use port 0; a child process needs a number, so free_port() asks the OS."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from support import (
    HOST,
    REPO,
    BlockAt,
    call,
    child_env,
    free_port,
    lead,
    post_lead,
    raw_exchange,
    start_proc,
    stop_proc,
    wait_until,
)

from demo_only import burst
from lead_desk import lockfile
from lead_desk.constants import BIND_RETRY_SECONDS, EXIT_ANOTHER_DESK, EXIT_PORT_UNAVAILABLE, EXIT_SIMULATED_KILL

HINT_R = "Send a corrected submission with a NEW request number."
HINT_D = "Informational only, no action needed."
HINT_O = "Send the same request number again, or restart the desk."
HINT_NOT_UNKNOWN = "Only a follow-up in state unknown can be resolved."


def pending_zero(base: str) -> bool:
    return bool(call(base, "GET", "/counts")[1]["pending_followups"] == 0)


# ten parallel
def test_ten_parallel(env, capsys) -> None:
    rig = env.rig()
    base = env.serve(rig)
    rc = burst.main(
        ["--url", base + "/leads", "--n", "10", "--email", "ada@example.test", "--ref-prefix", "run1"]
        + ["--source", "form", "--expect", "10"]
    )
    out = capsys.readouterr().out
    print(out)  # shown on failure; the RETRIES line is the 503 retry report
    assert rc == 0
    assert "SENT n=10" in out
    assert re.search(r"^STATUS .*201=1", out, re.M)
    assert re.search(r"^STATUS .*200=9", out, re.M)
    assert re.search(r"^RETRIES busy503=\d+ refused=\d+$", out, re.M)  # reported whether zero or not
    assert "RESULT sent=10 expected=10" in out
    wait_until(lambda: pending_zero(base), what="follow-ups finished")
    status, c = call(base, "GET", "/counts?expected=10")
    assert status == 200
    assert (c["received"], c["new"], c["duplicate"], c["review"], c["open"]) == (10, 1, 9, 0, 0)
    assert c["expected_match"] is True
    assert c["ok"] is True
    assert rig.leads() == 1
    assert rig.outbox.count == 1
    assert rig.crm.count == 1


def test_ten_simultaneous_requests_other_door_one_lead_one_follow_up(env, capsys) -> None:
    """The same burst of ten, now through the webhook door and with a mixed-case address: 10 distinct request ids at
    the same moment, a client that sends again after a 503, and afterwards one `new`, nine `duplicate`, one follow-up,
    nothing open, and the counts reconcile."""
    rig = env.rig()
    base = env.serve(rig)
    rc = burst.main(
        ["--url", base + "/leads", "--n", "10", "--email", "Mixed.Case@example.test", "--ref-prefix", "door2"]
        + ["--source", "webhook", "--expect", "10"]
    )
    out = capsys.readouterr().out
    print(out)
    assert rc == 0 and "RESULT sent=10 expected=10" in out
    wait_until(lambda: pending_zero(base), what="follow-ups finished")
    status, c = call(base, "GET", "/counts?expected=10")
    assert status == 200
    assert (c["received"], c["new"], c["duplicate"], c["review"], c["open"]) == (10, 1, 9, 0, 0)
    assert (c["expected_match"], c["ok"]) == (True, True)
    assert rig.leads() == 1 and rig.query("SELECT count(*) FROM followups")[0][0] == 1
    assert rig.outbox.count == 1 and rig.crm.count == 1


# reconcile counts
def test_reconcile_counts(env) -> None:
    hook = BlockAt("A")  # holds the first request's receipt open
    rig = env.rig(hooks=hook)
    base = env.serve(rig)
    t = threading.Thread(target=lambda: post_lead(base, "form", "sub-1", lead()))
    t.start()
    try:
        assert hook.reached.wait(timeout=15)
        _, c = call(base, "GET", "/counts?expected=1")
        assert (c["received"], c["open"], c["new"], c["duplicate"], c["review"]) == (1, 1, 0, 0, 0)
        assert c["expected_match"] is True
        assert c["ok"] is False  # an open receipt
        _, c0 = call(base, "GET", "/counts")
        assert (c0["expected_match"], c0["ok"]) == (None, None)  # without `expected` a lost request cannot be seen
    finally:
        hook.release.set()
        t.join(timeout=30)
    wait_until(lambda: pending_zero(base), what="follow-ups finished")
    _, c = call(base, "GET", "/counts?expected=1")
    assert (c["received"], c["open"], c["new"], c["pending_followups"], c["rejected_envelopes"]) == (1, 0, 1, 0, 0)
    assert c["received"] == c["new"] + c["duplicate"] + c["review"] + c["open"]
    assert (c["expected_match"], c["ok"]) == (True, True)
    _, c2 = call(base, "GET", "/counts?expected=2")
    assert (c2["expected_match"], c2["ok"]) == (False, False)  # a request lost before the desk
    status, body = call(base, "POST", "/leads", raw=b"this is not json")
    assert (status, body) == (400, {"error": "bad_envelope", "receipt_id": None})
    _, c3 = call(base, "GET", "/counts?expected=1")
    assert c3["rejected_envelopes"] == 1
    assert c3["received"] == 1  # a rejected envelope is not a receipt
    assert (c3["expected_match"], c3["ok"]) == (True, False)  # a rejected envelope makes ok false


# review and envelope errors
def test_review_and_envelope_errors(env) -> None:
    rig = env.rig(size_cap_bytes=1024)
    base = env.serve(rig)
    bad_env = {"error": "bad_envelope", "receipt_id": None}
    ok_payload = {"name": "Ada", "email": "ada@example.test"}
    envelopes: list[bytes] = [
        b"this is not json",
        b"[]",
        b"null",
        json.dumps({"source": "form", "source_ref": "x"}).encode(),
        json.dumps({"source_ref": "x", "payload": ok_payload}).encode(),
        json.dumps({"source": "email", "source_ref": "x", "payload": ok_payload}).encode(),
        json.dumps({"source": "form", "source_ref": "", "payload": ok_payload}).encode(),
        json.dumps({"source": "form", "source_ref": "r" * 129, "payload": ok_payload}).encode(),
        json.dumps({"source": "form", "source_ref": 5, "payload": ok_payload}).encode(),
        json.dumps({"source": "form", "source_ref": "x", "payload": []}).encode(),
        json.dumps({"source": "form", "source_ref": "x", "payload": "text"}).encode(),
    ]
    for raw in envelopes:
        assert call(base, "POST", "/leads", raw=raw) == (400, bad_env), raw
    no_length = f"POST /leads HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode()
    assert raw_exchange(base, no_length) == (400, bad_env)
    big = json.dumps({"source": "form", "source_ref": "x", "payload": {**ok_payload, "message": "m" * 2000}}).encode()
    assert call(base, "POST", "/leads", raw=big) == (413, {"error": "too_large", "receipt_id": None})
    rejected = len(envelopes) + 2
    _, c = call(base, "GET", "/counts?expected=0")
    assert (c["received"], c["rejected_envelopes"]) == (0, rejected)
    assert (c["expected_match"], c["ok"]) == (True, False)
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0  # nothing was stored
    # 404 and 405
    not_found = (404, {"error": "not_found"})
    assert call(base, "GET", "/nope") == not_found
    assert call(base, "POST", "/nope", {}) == not_found
    assert call(base, "GET", "/leads/Lnope") == not_found
    wrong = (405, {"error": "method_not_allowed"})
    assert call(base, "GET", "/leads") == wrong
    assert call(base, "POST", "/counts", {}) == wrong
    assert call(base, "POST", "/leads/Lxxx", {}) == wrong
    assert call(base, "GET", "/review/Fabc/retry") == wrong
    # bad expected
    for q in ("x", "", "-1", "1.5"):
        assert call(base, "GET", f"/counts?expected={q}") == (400, {"error": "bad_expected"}), q
    assert call(base, "GET", "/counts?expected=0")[0] == 200
    review_409_and_bad_decision_rows(env)


def review_409_and_bad_decision_rows(env) -> None:
    """The 409 and bad_decision rows; run at the end of test_review_and_envelope_errors."""
    # --- rows that need no blocking hook (blind outbox)
    rig = env.rig(outbox="blind", size_cap_bytes=4096)
    base = env.serve(rig)
    ob = rig.outbox
    l1 = post_lead(base, "form", "s1", lead(email="l1@example.test"))[1]["lead_id"]
    wait_until(lambda: rig.state(l1)[0] == "sent", what="first follow-up sent")  # it runs after the reply
    ob.fail_next("record_then_drop")
    l2 = post_lead(base, "form", "s2", lead(email="l2@example.test"))[1]["lead_id"]
    wait_until(lambda: pending_zero(base) and rig.state(l2)[0] == "unknown", what="unknown")
    wait_until(lambda: rig.state(l1)[0] == "sent", what="sent")
    rev = post_lead(base, "form", "s3", lead(email=None))[1]
    dup = post_lead(base, "webhook", "m1", lead(email="l1@example.test", phone="555 0199"))[1]
    r_item, d_item = "R" + str(rev["receipt_id"]), "D" + str(dup["receipt_id"])
    ids = {x["item_id"] for x in call(base, "GET", "/review")[1]}
    assert {r_item, d_item, "F" + l2} <= ids
    sent_ctx = (409, {"error": "not_retryable"})
    assert call(base, "POST", f"/review/F{l1}/retry") == sent_ctx
    assert call(base, "POST", f"/review/F{l2}/retry") == (409, {"error": "unknown_needs_person"})
    assert call(base, "POST", f"/review/F{l1}/resolve", {"decision": "sent"}) == (
        409,
        {"error": "not_unknown", "hint": HINT_NOT_UNKNOWN},
    )
    for item, hint in ((r_item, HINT_R), (d_item, HINT_D)):
        want = (409, {"error": "not_actionable", "hint": hint})
        assert call(base, "POST", f"/review/{item}/retry") == want
        assert call(base, "POST", f"/review/{item}/resolve", {"decision": "sent"}) == want
        assert call(base, "POST", f"/review/{item}/resolve", {"decision": "bogus"}) == want  # decision not looked at
    # bad_decision: the server answers before any lookup
    bad = (400, {"error": "bad_decision"})
    assert call(base, "POST", f"/review/F{l2}/resolve", raw=b"not json") == bad
    assert call(base, "POST", "/review/FLnope/resolve", raw=b"not json") == bad  # even for an unknown item
    assert call(base, "POST", "/review/X1/resolve", raw=b"not json") == bad
    assert call(base, "POST", f"/review/F{l2}/resolve", {}) == bad
    assert call(base, "POST", f"/review/F{l2}/resolve", {"decision": 5}) == bad
    assert call(base, "POST", f"/review/F{l2}/resolve", {"decision": "maybe"}) == bad
    assert call(base, "POST", f"/review/F{l1}/resolve", {"decision": "maybe"}) == bad  # before not_unknown
    # 404: unknown id, malformed id
    gone = (404, {"error": "not_found"})
    assert call(base, "POST", "/review/FLnope/resolve", {"decision": "sent"}) == gone
    assert call(base, "POST", "/review/X1/resolve", {"decision": "sent"}) == gone
    assert call(base, "POST", "/review/X1/retry", {}) == gone
    assert call(base, "POST", "/review/FLnope/retry", {}) == gone
    assert call(base, "POST", f"/review/F{l2}/resolve", {"decision": "sent"}) == (
        200,
        {"item_id": f"F{l2}", "state": "sent"},
    )
    # --- in_progress: a follow-up held in `sending`
    hold = BlockAt("D1")
    rig2 = env.rig(hooks=hold)
    base2 = env.serve(rig2)
    lid = post_lead(base2, "form", "s1", lead())[1]["lead_id"]
    assert hold.reached.wait(timeout=15)
    try:
        assert rig2.state(lid)[0] == "sending"
        assert call(base2, "POST", f"/review/F{lid}/retry") == (409, {"error": "in_progress"})
        assert call(base2, "POST", f"/review/F{lid}/resolve", {"decision": "sent"})[1]["error"] == "not_unknown"
    finally:
        hold.release.set()
    wait_until(lambda: rig2.state(lid)[0] == "sent", what="released follow-up sent")
    # --- O item: an open receipt that has gone stale
    open_hook = BlockAt("A")
    rig3 = env.rig(hooks=open_hook)
    base3 = env.serve(rig3)
    t = threading.Thread(target=lambda: post_lead(base3, "form", "s1", lead()))
    t.start()
    try:
        assert open_hook.reached.wait(timeout=15)
        rig3.clock.advance(rig3.config.stall_seconds + 1)
        o_items = [x for x in call(base3, "GET", "/review")[1] if x["kind"] == "stalled"]
        assert len(o_items) == 1
        o = o_items[0]["item_id"]
        assert o.startswith("O")
        want = (409, {"error": "not_actionable", "hint": HINT_O})
        assert call(base3, "POST", f"/review/{o}/retry") == want
        assert call(base3, "POST", f"/review/{o}/resolve", {"decision": "sent"}) == want
    finally:
        open_hook.release.set()
        t.join(timeout=30)


# reply does not wait for send
def test_reply_does_not_wait_for_send(env) -> None:
    rig = env.rig()
    rig.outbox.delay = 3.0
    base = env.serve(rig)
    body = json.dumps({"source": "form", "source_ref": "sub-1", "payload": lead()}).encode()
    req = (
        f"POST /leads HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body
    t0 = time.monotonic()
    status, reply = raw_exchange(base, req)  # reads until the server ends the connection
    elapsed = time.monotonic() - t0
    assert status == 201
    assert reply["verdict"] == "new"
    assert elapsed < 1.0, f"the reply took {elapsed:.2f} s while the send takes 3 s"
    wait_until(lambda: pending_zero(base), timeout=20, what="slow follow-up finished")
    assert rig.outbox.count == 1


# crash d2 subprocess
def launch(args: list[str], env_vars: dict[str, str], base: str):  # type: ignore[no-untyped-def]
    """Start a desk child; retry the start for BIND_RETRY_SECONDS when it exits with the bind-failure code."""
    deadline = time.monotonic() + BIND_RETRY_SECONDS
    while True:
        p = start_proc(args, env_vars)
        try:
            wait_until(lambda: p.poll() is not None or call(base, "GET", "/counts")[0] == 200, what="desk ready")
        except AssertionError:
            stop_proc(p)
            raise
        if p.poll() is None:
            return p
        out = stop_proc(p)
        if p.returncode != EXIT_PORT_UNAVAILABLE or time.monotonic() > deadline:
            raise AssertionError(f"desk child exited with {p.returncode}: {out.decode(errors='replace')[-400:]}")
        time.sleep(0.2)


@pytest.mark.parametrize("kind", ["keyed", "blind"])
def test_crash_d2_subprocess(env, tmp_path: Path, kind: str) -> None:
    _, fakes_url = env.fakes(kind)
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    flag = tmp_path / "crash.flag"
    port = free_port()
    base = f"http://{HOST}:{port}"
    env_vars = child_env(
        DESK_DB_PATH=str(tmp_path / "desk.sqlite3"),
        DESK_BIND=HOST,
        DESK_PORT=str(port),
        DESK_FAKES_URL=fakes_url,
        DESK_REPLIES_PATH=str(replies),
    )
    launcher = [str(REPO / "tests" / "crash_launcher.py"), str(flag)]
    flag.write_text("D2", encoding="utf-8")
    p1 = launch(launcher, env_vars, base)
    p2 = None
    try:
        status, reply = post_lead(base, "form", "sub-1", lead())
        assert status == 201
        lid = reply["lead_id"]
        p1.wait(timeout=20)  # the real os._exit at D2, after the email left and before the result was recorded
        out1 = stop_proc(p1)
        assert p1.returncode == EXIT_SIMULATED_KILL, out1.decode(errors="replace")[-400:]
        assert b"simulate crash" in out1
        assert not flag.exists()  # the hook is one-shot
        assert call(fakes_url, "GET", "/fake/outbox")[1]["count"] == 1  # the email did leave
        p2 = launch(launcher, env_vars, base)  # a restart on the same port and the same lead book
        final = "sent" if kind == "keyed" else "unknown"
        wait_until(lambda: call(base, "GET", f"/leads/{lid}")[1]["followup"]["state"] == final, what=f"state {final}")
        assert call(fakes_url, "GET", "/fake/outbox")[1]["count"] == 1  # never two
        assert call(fakes_url, "GET", "/fake/crm")[1]["count"] == 1
        replay = post_lead(base, "form", "sub-1", lead())[1]
        assert replay["replayed"] is True
        assert replay["lead_id"] == lid
    finally:
        if p2 is not None:
            stop_proc(p2)
        stop_proc(p1)


# crash d2 subprocess smtp blind
def test_crash_d2_subprocess_with_the_blind_smtp_sender(env, tmp_path: Path) -> None:
    """The same real kill at D2, but with the SMTP sender (blind: the desk cannot ask whether a mail arrived) and a
    real SMTP catcher process: the mail left, the desk was killed before it wrote the result, the restarted desk must
    NOT send again and ends the follow-up `unknown`."""
    _, fakes_url = env.fakes("blind")  # the CRM and the model replies still come from the fakes server
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    flag = tmp_path / "crash.flag"
    port, smtp_port = free_port(), free_port()
    base = f"http://{HOST}:{port}"
    mail_log = tmp_path / "mail.jsonl"
    catcher = start_proc(
        [str(REPO / "load_tools" / "catcher.py"), "--out", str(mail_log), "--host", HOST, "--port", str(smtp_port)],
        child_env(),
    )
    env_vars = child_env(
        DESK_DB_PATH=str(tmp_path / "desk.sqlite3"),
        DESK_BIND=HOST,
        DESK_PORT=str(port),
        DESK_FAKES_URL=fakes_url,
        DESK_REPLIES_PATH=str(replies),
        DESK_OUTBOX="smtp",
        DESK_SMTP_HOST=HOST,
        DESK_SMTP_PORT=str(smtp_port),
    )
    launcher = [str(REPO / "tests" / "crash_launcher.py"), str(flag)]
    flag.write_text("D2", encoding="utf-8")
    p1 = p2 = None
    try:
        wait_until(lambda: catcher.poll() is not None or _listens(HOST, smtp_port), what="catcher ready")
        assert catcher.poll() is None
        p1 = launch(launcher, env_vars, base)
        status, reply = post_lead(base, "form", "sub-1", lead())
        assert status == 201
        lid = reply["lead_id"]
        p1.wait(timeout=20)
        out1 = stop_proc(p1)
        assert p1.returncode == EXIT_SIMULATED_KILL, out1.decode(errors="replace")[-400:]
        assert len(mail_log.read_text(encoding="utf-8").splitlines()) == 1  # the mail did leave
        assert call(fakes_url, "GET", "/fake/crm")[1]["count"] == 1  # and the CRM row was written before it
        p2 = launch(launcher, env_vars, base)
        wait_until(lambda: call(base, "GET", f"/leads/{lid}")[1]["followup"]["state"] == "unknown", what="unknown")
        assert len(mail_log.read_text(encoding="utf-8").splitlines()) == 1  # never a second mail
        assert call(fakes_url, "GET", "/fake/crm")[1]["count"] == 1  # never a second CRM row
        assert call(base, "GET", f"/leads/{lid}")[1]["followup"]["last_error"] == "crash_while_sending"
    finally:
        for proc in (p2, p1, catcher):
            if proc is not None:
                stop_proc(proc)


def _listens(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


# second desk refuses to start
def test_second_desk_refuses_to_start(env, tmp_path: Path) -> None:
    _, fakes_url = env.fakes("keyed")
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    port_a, port_b = free_port(), free_port()
    db = tmp_path / "desk.sqlite3"

    def vars_for(db_path: Path, port: int) -> dict[str, str]:
        return child_env(
            DESK_DB_PATH=str(db_path),
            DESK_BIND=HOST,
            DESK_PORT=str(port),
            DESK_FAKES_URL=fakes_url,
            DESK_REPLIES_PATH=str(replies),
        )

    first = start_proc(["-m", "lead_desk.server"], vars_for(db, port_a))
    try:
        wait_until(lambda: first.poll() is not None or call(f"http://{HOST}:{port_a}", "GET", "/counts")[0] == 200)
        assert first.poll() is None, "the first desk must start"
        message = b"another desk is using this database"
        # same port, same lead book; and a different port, same lead book (the lock file comes first)
        for port in (port_a, port_b):
            second = start_proc(["-m", "lead_desk.server"], vars_for(db, port))
            try:
                second.wait(timeout=20)
            finally:
                out = stop_proc(second)
            assert second.returncode == EXIT_ANOTHER_DESK, out.decode(errors="replace")[-400:]
            assert message in out
        # same port, other lead book: the bind fails (any OSError) after the retry time, with its own exit code
        third = start_proc(["-m", "lead_desk.server"], vars_for(tmp_path / "other.sqlite3", port_a))
        try:
            third.wait(timeout=20)
        finally:
            out = stop_proc(third)
        assert third.returncode == EXIT_PORT_UNAVAILABLE, out.decode(errors="replace")[-400:]
        assert b"cannot bind" in out
        assert call(f"http://{HOST}:{port_a}", "GET", "/counts")[0] == 200  # the first desk is untouched
    finally:
        stop_proc(first)
    # the lock itself: a second acquire on a held lead book raises OSError; after release it works
    held = lockfile.acquire(tmp_path / "lock-test.sqlite3")
    try:
        with pytest.raises(OSError):
            lockfile.acquire(tmp_path / "lock-test.sqlite3")
    finally:
        held.close()
    lockfile.acquire(tmp_path / "lock-test.sqlite3").close()
