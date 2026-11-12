"""HTTP layer: framing, header and target abuse, a lost reply, and many simultaneous connections.
Made-up data only. Each test is one hostile-input case."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import replace

import pytest
from support import (
    HOST,
    BlockAt,
    LiveClock,
    addr,
    call,
    lead,
    raw_exchange,
    reset_on_close,
    run_threads,
    stall_report,
    wait_until,
)

from lead_desk import server
from lead_desk.constants import LISTEN_QUEUE

BODY = json.dumps({"source": "form", "source_ref": "r1", "payload": lead()}).encode()


def pending_zero(base: str) -> bool:
    return bool(call(base, "GET", "/counts")[1]["pending_followups"] == 0)


def settled(base: str) -> bool:
    """No follow-up waits and none is being sent: `pending` alone is also zero while a claimed row is mid-send."""
    c = call(base, "GET", "/counts")[1]
    return bool(c["pending_followups"] == 0 and c["sending_followups"] == 0)


def rejected(base: str) -> int:
    return int(call(base, "GET", "/counts")[1]["rejected_envelopes"])


def head(extra: str, length: str | None = None) -> bytes:
    cl = f"Content-Length: {length}\r\n" if length is not None else ""
    return f"POST /leads HTTP/1.1\r\nHost: {HOST}\r\n{cl}{extra}Connection: close\r\n\r\n".encode()


def test_short_body_socket_held_open_is_408(env, monkeypatch) -> None:
    monkeypatch.setattr(server._Handler, "timeout", 1.0)
    base = env.serve(env.rig())
    host, port = addr(base)
    t0 = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(head("", str(len(BODY) + 50)) + BODY)  # announces 50 more bytes than it sends, then stays silent
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    assert time.monotonic() - t0 < 8
    assert data.startswith(b"HTTP/1.0 408") or data.startswith(b"HTTP/1.1 408")
    assert json.loads(data.partition(b"\r\n\r\n")[2]) == {"error": "request_timeout", "receipt_id": None}
    assert rejected(base) == 1


def test_short_body_client_closes_is_400(env) -> None:
    base = env.serve(env.rig())
    host, port = addr(base)
    with socket.create_connection((host, int(port)), timeout=10) as s:
        s.sendall(head("", str(len(BODY) + 50)) + BODY)
        s.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    assert b" 400 " in data.split(b"\r\n", 1)[0]
    assert rejected(base) == 1


@pytest.mark.parametrize("length", ["9" * 20, "9" * 4301, "0" * 5000 + "9" * 20, "9" * 19])
def test_huge_content_length_is_413_without_waiting(env, length) -> None:
    base = env.serve(env.rig())
    t0 = time.monotonic()
    status, body = raw_exchange(base, head("", length), timeout=8)  # sends no body at all
    assert (status, body) == (413, {"error": "too_large", "receipt_id": None})
    assert time.monotonic() - t0 < 8
    assert rejected(base) == 1


def test_zero_padded_small_length_still_works(env) -> None:
    base = env.serve(env.rig())
    status, body = raw_exchange(base, head("", "000" + str(len(BODY))) + BODY)
    assert status == 201 and body["verdict"] == "new"


def test_conflicting_content_length_is_400(env) -> None:
    base = env.serve(env.rig())
    req = head(f"Content-Length: {len(BODY) + 5}\r\n", str(len(BODY))) + BODY
    status, body = raw_exchange(base, req)
    assert (status, body) == (400, {"error": "bad_envelope", "receipt_id": None})
    assert rejected(base) == 1
    assert call(base, "GET", "/counts")[1]["received"] == 0


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("http://[/", (400, "bad_request")),  # the standard library's URL parser refuses an open bracket
        ("//[", (404, "not_found")),
        ("http://[::1/x", (400, "bad_request")),
        ("/leads/%ff%fe/../[", (404, "not_found")),
        ("http://" + ":".join("abc") + "/", (404, "not_found")),
    ],
)
def test_unparsable_target_is_json(env, target, expected) -> None:
    base = env.serve(env.rig())
    status, body = raw_exchange(base, f"GET {target} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode())
    assert (status, body) == (expected[0], {"error": expected[1]})


@pytest.mark.parametrize("item", ["R" + "9" * 20, "D" + "1" * 4301, "O" + "9" * 19, "F" + "9" * 20])
@pytest.mark.parametrize("action", ["retry", "resolve"])
def test_huge_item_id_is_not_500(env, item, action) -> None:
    base = env.serve(env.rig())
    status, body = call(base, "POST", f"/review/{item}/{action}", body={"decision": "sent"})
    assert (status, body) == (404, {"error": "not_found"})  # no such item (an id too long for any number, or unknown)


def test_lost_reply_followup_still_runs(env) -> None:
    hook = BlockAt("A")  # holds the request after it has read the body, before the commit
    rig = env.rig(hooks=hook)
    base = env.serve(rig)
    host, port = addr(base)
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall(head("", str(len(BODY))) + BODY)
    assert hook.reached.wait(timeout=15)
    reset_on_close(s)
    s.close()  # sends a reset: the reply can no longer be written
    time.sleep(0.3)
    hook.release.set()
    # pending_zero alone is true before the request has committed; wait for the follow-up's own end state
    wait_until(lambda: rig.leads() == 1 and rig.state()[0] == "sent", what="follow-up driven although the reply failed")
    assert pending_zero(base)
    assert rig.outbox.count == 1


def _burst(env, n: int, retry: bool, reaper: bool = True) -> tuple[int, list[object]]:
    """n connections at the same moment, each a different person (own e-mail address and request id). Returns
    (clients whose first try was not answered with a stored result, (status, reply) per client). With retry, a 503
    or a dropped connection is tried again with the SAME request id (safe: the desk treats it as a replay) until it
    is answered. Then what the clients were told is compared with what the desk stored: every client has exactly one
    lead, every 201 names a stored lead once, and one e-mail was sent per lead.

    The desk runs as it does in production: with the reaper and a clock that moves. At most 4 AI calls run at once
    (the model cap) while up to 8 follow-up drives do (DRIVE_MAX), so under a burst a drive can be turned away
    (`model_busy`, the try given back). Only the reaper takes such a row again; a desk without it (reaper=False)
    leaves it `pending` for ever, and on a frozen clock the reaper would never find it due."""
    rig = env.rig(clock=LiveClock() if reaper else None)
    if reaper:
        rig.desk.start_background(0.2)
        env.stop_on_close(rig.desk.stop_background)
    base = env.serve(rig)
    first_try_failed = [0]
    lock = threading.Lock()

    def one(i: int):  # type: ignore[no-untyped-def]
        def go():  # type: ignore[no-untyped-def]
            body = {"source": "form", "source_ref": f"b{i}", "payload": lead(email=f"burst{i}@example.test")}
            for attempt in range(40 if retry else 1):
                try:
                    answer = call(base, "POST", "/leads", body=body, timeout=60)
                except OSError:  # refused or dropped before an answer
                    answer = (None, None)
                if answer[0] is not None and answer[0] != 503:
                    return answer
                if attempt == 0:
                    with lock:
                        first_try_failed[0] += 1
                time.sleep(min(0.05 * (attempt + 1), 0.5))
            raise AssertionError(f"b{i} got no stored answer")

        return go

    results = run_threads([one(i) for i in range(n)])
    errors = [r for r in results if isinstance(r, BaseException) or r is None]
    assert not errors, (
        f"{len(errors)} of {n} got no answer; first try not answered {first_try_failed[0]}: {errors[:1]!r}"
    )
    assert {r[0] for r in results} <= {200, 201}  # type: ignore[index]
    try:
        wait_until(lambda: settled(base), what="follow-ups finished", timeout=90)
    except AssertionError as e:
        raise AssertionError(f"{e}" + chr(10) + stall_report(rig)) from None
    stored = {
        row[0]: row[1] for row in rig.query("SELECT source_ref, lead_id FROM receipts JOIN leads USING (receipt_id)")
    }
    assert sorted(stored) == sorted(f"b{i}" for i in range(n))  # one lead for every client, none twice
    told = [r[1]["lead_id"] for r in results if r[0] == 201]  # type: ignore[index]
    assert len(told) == len(set(told)) and set(told) <= set(stored.values())  # a 201 names a stored lead, once
    assert rig.leads() == n
    assert rig.outbox.count == n  # one e-mail per lead, never a second
    return first_try_failed[0], results


def test_150_burst_all_answered(env) -> None:
    assert LISTEN_QUEUE >= 1000
    rig = env.rig()
    srv = server.make_server(rig.desk, replace(rig.desk.config, port=0))
    try:
        assert srv.request_queue_size == LISTEN_QUEUE  # the server really uses the constant
    finally:
        srv.server_close()
    _burst(env, 150, retry=True)  # a refused first try (full queue, 503) is tried again with the same request id


def test_300_burst_with_retry(env) -> None:
    """A burst of 300 may be refused in part (the operating system before accept, or a 503 busy at the cap); a
    client that retries with the same request id gets an answer, and each lead is stored once."""
    _burst(env, 300, retry=True)


@pytest.mark.skipif(not os.environ.get("LEAD_DESK_LOAD_N"), reason="measurement run: set LEAD_DESK_LOAD_N")
def test_measure_burst(env) -> None:
    n = int(os.environ["LEAD_DESK_LOAD_N"])
    refused, results = _burst(env, n, retry=os.environ.get("LEAD_DESK_LOAD_RETRY") == "1")
    counts: dict[int, int] = {}
    for r in results:
        counts[r[0]] = counts.get(r[0], 0) + 1  # type: ignore[index]
    print(f"MEASURED n={n} first_try_refused={refused} status_counts={dict(sorted(counts.items()))}")
