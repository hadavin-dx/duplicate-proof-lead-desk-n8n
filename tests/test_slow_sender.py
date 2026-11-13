"""Slow-sender defence: one wall-clock deadline for the headers and one for the body, a cap on connections that are
still being read, eviction of stalled header senders at the cap, one 408 shape, a clean 503 busy, and no log noise
when the client vanishes. Made-up data only; real sockets on loopback."""

from __future__ import annotations

import errno
import json
import logging
import select
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from support import HOST, BlockAt, addr, call, lead, post_lead, reset_on_close, start_serving, stop_serving, wait_until

from lead_desk import constants, server
from lead_desk.hooks import Hooks

BODY = json.dumps({"source": "form", "source_ref": "slow1", "payload": lead()}).encode()
TIMEOUT_REPLY = {"error": "request_timeout", "receipt_id": None}
BUSY_REPLY = {"error": "busy", "reason": "busy"}


def connect(base: str) -> socket.socket:
    return socket.create_connection(addr(base), timeout=8)


def drip(sock: socket.socket, data: bytes, gap: float, stop_after: float) -> None:
    """Send `data` one byte at a time, until the peer closes or `stop_after` seconds pass."""
    end = time.monotonic() + stop_after
    for i in range(len(data)):
        if time.monotonic() > end:
            return
        try:
            sock.sendall(data[i : i + 1])
        except OSError:
            return
        time.sleep(gap)


def read_end(sock: socket.socket) -> tuple[bytes, str]:
    """Everything the server sent, and how the connection ended: "eof" (orderly close), "reset" (the server reset it)
    or "timeout" (still open when the test's socket timeout ran out)."""
    out = b""
    try:
        while chunk := sock.recv(4096):
            out += chunk
    except ConnectionResetError:
        return out, "reset"
    except TimeoutError:
        return out, "timeout"
    return out, "eof"


def read_all(sock: socket.socket) -> bytes:
    """What the server sent, however the connection ended. Where a test claims an orderly close, use read_end."""
    return read_end(sock)[0]


def parse_reply(raw: bytes) -> tuple[int, dict[str, str], Any]:
    """(status, lower-case headers, parsed JSON body) of a complete reply; fails on a partial one."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    assert sep, f"no complete reply, got {raw[:80]!r}"
    lines = head.decode().split("\r\n")
    headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return int(lines[0].split(" ", 2)[1]), headers, json.loads(body.decode() or "null")


def run_drip(base: str, data: bytes, gap: float = 0.2) -> tuple[bytes, float]:
    """Drip `data` from a thread; return what the server sent and how long until it closed."""
    s = connect(base)
    t0 = time.monotonic()
    th = threading.Thread(target=drip, args=(s, data, gap, 6.0), daemon=True)
    th.start()
    got = read_all(s)
    took = time.monotonic() - t0
    s.close()
    th.join(timeout=8)
    return got, took


def get_ok(base: str, path: str = "/counts", seconds: float = 15.0) -> int:
    """GET that retries while the server answers 503 busy or drops the connection (both are honest answers while
    other connections hold places). Returns the first other status."""
    end = time.monotonic() + seconds
    while True:
        try:
            status = int(call(base, "GET", path)[0])
        except OSError:
            status = 503
        if status != 503 or time.monotonic() > end:
            return status
        time.sleep(0.05)


def post_head(length: int, ref: str = "x") -> bytes:
    return (
        f"POST /leads HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {length}\r\nConnection: close\r\n\r\n"
    ).encode()


def lead_body(ref: str, email: str) -> bytes:
    return json.dumps({"source": "form", "source_ref": ref, "payload": lead(email=email)}).encode()


@pytest.fixture(autouse=True)
def evict_at_once(monkeypatch) -> None:
    """These tests are about WHO is closed for room, not about how long a stall must last: any reading connection
    may be closed here. The stall rule itself is tested in test_stall_eviction.py."""
    monkeypatch.setattr(server, "EVICT_MIN_IDLE_SECONDS", 0.0)


@pytest.fixture
def short(monkeypatch) -> None:
    monkeypatch.setattr(server, "HEADER_READ_DEADLINE_SECONDS", 1.0)
    monkeypatch.setattr(server, "BODY_READ_DEADLINE_SECONDS", 1.0)


class Live:
    """A server built directly, so a test can read the connection count."""

    def __init__(self, env, hooks=None) -> None:
        self.rig = env.rig(hooks=hooks)
        cfg = replace(self.rig.desk.config, port=0)
        self.srv = server.make_server(self.rig.desk, cfg)
        self.taken = 0  # places handed out so far (a count that never goes down)
        real_take = self.srv._take_slot  # type: ignore[attr-defined]

        def counting_take(request: Any) -> Any:
            slot = real_take(request)
            self.taken += 1
            return slot

        self.srv._take_slot = counting_take  # type: ignore[attr-defined]
        self.thread = start_serving(self.srv)
        host, port = self.srv.server_address[0], self.srv.server_address[1]
        self.base = f"http://{host}:{port}"

    @property
    def count(self) -> int:
        """Connections that still take a place under the cap."""
        fn = getattr(self.srv, "live_connections", None)
        return int(fn()) if fn else int(getattr(self.srv, "_live", -1))

    @property
    def in_header(self) -> int:
        fn = getattr(self.srv, "header_phase_connections", None)
        return int(fn()) if fn else -1

    def stalled_header(self) -> socket.socket:
        """A connection that has sent part of a request line and then nothing."""
        s = connect(self.base)
        s.sendall(b"GET /cou")
        return s

    def body_phase(self, length: int = 100) -> socket.socket:
        """A connection whose headers are complete and whose body has not come."""
        s = connect(self.base)
        s.sendall(post_head(length))
        return s

    def protected(self, n: int) -> list[tuple[socket.socket, Any]]:
        """n places held by connections that are past the point where they could be closed (the desk is acting on
        them). Made directly on the server, so no thread and no real request is involved."""
        out = []
        for _ in range(n):
            sock = socket.socket()
            slot = self.srv._take_slot(sock)  # type: ignore[attr-defined]
            assert slot is not None
            slot.start_work()
            out.append((sock, slot))
        return out

    def free(self, held: list[tuple[socket.socket, Any]]) -> None:
        for sock, slot in held:
            slot.release()
            sock.close()

    @property
    def threads(self) -> int:
        return int(self.srv.handler_threads())  # type: ignore[attr-defined]

    def stop(self) -> None:
        stop_serving(self.srv)


@pytest.fixture
def live(env) -> Iterator[Live]:
    lv = Live(env)
    yield lv
    lv.stop()


def cap(monkeypatch, n: int) -> None:
    monkeypatch.setattr(server, "MAX_CONNECTIONS", n)  # a test cap below the validated range of the real constant


# -- deadlines (items: header, body)
def test_slow_request_line_closed(env, short) -> None:
    base = env.serve(env.rig())
    got, took = run_drip(base, f"POST /leads HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    assert took < 3
    assert b" 408 " in got.split(b"\r\n", 1)[0]


def test_slow_headers_closed(env, short) -> None:
    base = env.serve(env.rig())
    s = connect(base)
    s.sendall(f"POST /leads HTTP/1.1\r\nHost: {HOST}\r\n".encode())
    t0 = time.monotonic()
    th = threading.Thread(target=drip, args=(s, b"X-Pad: " + b"a" * 40 + b"\r\n\r\n", 0.2, 6.0), daemon=True)
    th.start()
    got = read_all(s)
    took = time.monotonic() - t0
    s.close()
    th.join(timeout=8)
    assert took < 3
    status, _, body = parse_reply(got)
    assert status == 408 and body == TIMEOUT_REPLY


def test_slow_body_closed_with_408(env, short) -> None:
    base = env.serve(env.rig())
    s = connect(base)
    s.sendall(post_head(len(BODY)))
    t0 = time.monotonic()
    th = threading.Thread(target=drip, args=(s, BODY, 0.2, 6.0), daemon=True)
    th.start()
    got = read_all(s)
    took = time.monotonic() - t0
    s.close()
    th.join(timeout=8)
    assert took < 3
    assert parse_reply(got)[0] == 408


# -- one 408 for a stalled header, body and resolve body; the log names the phase
STALLS = {
    "header": (b"POST /leads HTTP/1.1\r\nHost: ", "read timed out in the header phase"),
    "body": (post_head(100) + b"{", "read timed out in the body phase"),
    "resolve": (
        (
            f"POST /review/F1/resolve HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{{"
        ).encode(),
        "read timed out in the resolve body phase",
    ),
}


@pytest.mark.parametrize("kind", sorted(STALLS))
def test_stall_gives_the_same_408(env, short, caplog, kind) -> None:
    data, phase_text = STALLS[kind]
    base = env.serve(env.rig())
    with caplog.at_level(logging.INFO, logger="lead_desk"):
        s = connect(base)
        s.sendall(data)  # then silence
        got = read_all(s)
        s.close()
    status, headers, body = parse_reply(got)
    assert status == 408, (kind, got[:120])  # a slow client gets 408, not 500, on the resolve route
    assert body == TIMEOUT_REPLY
    assert headers["connection"] == "close" and int(headers["content-length"]) > 0
    assert [r for r in caplog.records if r.getMessage() == phase_text], [r.getMessage() for r in caplog.records]
    assert not [r for r in caplog.records if "handler failed" in r.getMessage()]


# -- the cap refusal is a complete reply
def test_busy_503_is_complete_and_clean(live, monkeypatch) -> None:
    cap(monkeypatch, 2)
    held = live.protected(2)
    big = b"x" * 300_000  # on its way while the refusal is written
    s = connect(live.base)
    t0 = time.monotonic()
    try:
        s.sendall(post_head(len(big)) + big)
    except OSError:  # the refusal came and the server closed before all of it was sent: that is allowed
        pass
    got, ending = read_end(s)
    took = time.monotonic() - t0
    s.close()
    # Whether bytes that arrive after the refusal reset the connection depends on timing; the complete-reply case
    # with the bytes already there is pinned without timing in test_refuse_busy_drains_arrived_bytes.
    assert ending in ("eof", "reset")
    if not got:
        assert ending == "reset"  # nothing was read: the reset came first
        live.free(held)
        return
    status, headers, body = parse_reply(got)
    assert status == 503 and body == BUSY_REPLY
    assert headers["retry-after"] == str(constants.BUSY_RETRY_AFTER_SECONDS)
    assert headers["connection"] == "close"
    assert headers["content-type"] == "application/json"
    assert int(headers["content-length"]) == len(json.dumps(BUSY_REPLY).encode())
    assert took < 1.0
    live.free(held)
    wait_until(lambda: live.count == 0, what="places freed")


def test_busy_503_silent_client_is_quick(live, monkeypatch) -> None:
    cap(monkeypatch, 2)
    held = live.protected(2)
    s = connect(live.base)  # connects and says nothing: the accept loop must not wait for it
    t0 = time.monotonic()
    assert call(live.base, "GET", "/counts")[0] == 503  # a second one behind the silent one still gets its answer
    assert time.monotonic() - t0 < 0.5
    s.close()
    live.free(held)


def test_busy_503_trickler_no_stall(live, monkeypatch) -> None:
    """A refused client that keeps sending (a byte every 20 ms) must not keep the accept loop busy for a whole second;
    the next connection must be answered about as fast as when nobody trickles."""
    cap(monkeypatch, 2)
    held = live.protected(2)
    trickler = connect(live.base)
    stop = threading.Event()

    def trickle() -> None:
        while not stop.is_set():
            try:
                trickler.sendall(b"x")
            except OSError:
                return
            time.sleep(0.02)

    th = threading.Thread(target=trickle, daemon=True)
    th.start()
    try:
        # the loop has taken the trickler first: its refusal (the 503 answer) is readable on the trickler's socket
        assert select.select([trickler], [], [], 5)[0], "the desk never answered the trickler"
        t0 = time.monotonic()
        assert call(live.base, "GET", "/counts") == (503, BUSY_REPLY)
        assert time.monotonic() - t0 < 0.4
    finally:
        stop.set()
        th.join(timeout=5)
        trickler.close()
        live.free(held)


# -- a place covers only the reading
def test_follow_up_does_not_hold_a_place(env, monkeypatch) -> None:
    hook = BlockAt("D1")  # holds the follow-up, which runs after the reply is written
    lv = Live(env, hooks=hook)
    try:
        cap(monkeypatch, 1)
        assert post_lead(lv.base, "form", "held1", lead())[0] == 201
        assert hook.reached.wait(timeout=15)
        try:
            wait_until(lambda: lv.count == 0, timeout=5, what="place freed while the follow-up is still running")
            assert call(lv.base, "GET", "/counts")[0] == 200  # with a cap of 1 this is 503 if the place were kept
        finally:
            hook.release.set()
        wait_until(lambda: lv.rig.state()[0] == "sent", what="follow-up finished")
    finally:
        lv.stop()


def test_place_released_exactly_once(live) -> None:
    srv = live.srv
    a, b = socket.socket(), socket.socket()
    try:
        slot_a, slot_b = srv._take_slot(a), srv._take_slot(b)
        assert slot_a is not None and slot_b is not None
        assert live.count == 2
        slot_a.release()
        slot_a.release()  # a second release must not free anyone else's place
        assert live.count == 1
        srv._abandon(a, slot_a)
        assert live.count == 1
        srv._abandon(b, slot_b)
        assert live.count == 0
    finally:
        a.close()
        b.close()


def test_evicted_released_abandoned(live, monkeypatch) -> None:
    """A place that was closed for room is already given back: a later release or abandon must not free the place of
    the connection that took its room."""
    cap(monkeypatch, 1)
    srv = live.srv
    a, b = socket.socket(), socket.socket()
    try:
        slot_a = srv._take_slot(a)
        slot_b = srv._take_slot(b)  # closes a for room
        assert slot_a is not None and slot_b is not None and slot_a.evicted
        assert live.count == 1
        slot_a.release()
        srv._abandon(a, slot_a)
        assert live.count == 1  # still b's place
        srv._abandon(b, slot_b)
        assert live.count == 0 and srv._pending == {}
    finally:
        a.close()
        b.close()


def test_slot_survives_every_end(live, monkeypatch) -> None:
    """Many connections that end in every possible way: no place leaks, the cap is never passed, nothing is left
    waiting to be picked up by a thread."""
    cap(monkeypatch, 3)
    for i in range(60):
        s = connect(live.base)
        kind = i % 6
        if kind == 1:
            s.sendall(f"GET /counts HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
        elif kind == 2:
            s.sendall(b"GET /cou")
        elif kind == 3:
            s.sendall(b"POST /leads HT")
            reset_on_close(s)  # reset on close
        elif kind == 4:
            s.sendall(b"\x00\xff garbage not http\r\n\r\n")
        elif kind == 5:
            s.sendall(post_head(50))  # headers, then no body
        s.close()
        assert live.count <= 3
    # accepting is first in, first out: once this one is answered, every connection above has been accepted
    assert get_ok(live.base) == 200
    wait_until(lambda: live.count == 0, what="all places freed")
    assert live.srv._pending == {}  # type: ignore[attr-defined]


# -- stalled header senders cannot lock real callers out
def test_cap_closes_oldest_header_sender(live, monkeypatch) -> None:
    cap(monkeypatch, 3)
    held = []
    for k in range(3):
        held.append(live.stalled_header())
        wait_until(lambda k=k: live.count == k + 1, what="place taken")  # accepted in this order
    assert call(live.base, "GET", "/counts")[0] == 200  # the new one is served, not refused
    assert read_end(held[0]) == (b"", "eof")  # the oldest was closed to make room, with no reply
    for s in held[1:]:  # the younger ones are untouched and can still finish
        s.sendall(f"nts HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
        assert parse_reply(read_all(s))[0] == 200
    for s in held:
        s.close()
    wait_until(lambda: live.count == 0, what="places freed")


def test_many_stalled_senders_cannot_block(live, monkeypatch) -> None:
    cap(monkeypatch, 3)
    stalled = []
    for k in range(12):
        stalled.append(live.stalled_header())
        wait_until(lambda k=k: live.taken == k + 1, what="the server took the connection in")
        assert live.count == min(k + 1, 3)  # the cap held, and was not passed
    assert call(live.base, "POST", "/leads", {"source": "form", "source_ref": "real1", "payload": lead()})[0] == 201
    assert [read_end(s) for s in stalled[:9]] == [(b"", "eof")] * 9  # the 9 oldest were closed, in an orderly way
    for s in stalled:
        s.close()


class Hold:
    """Replaces desk.process so that a request stays in the processing state until released."""

    def __init__(self, desk: Any) -> None:
        self.original = desk.process
        self.entered = 0
        self.release = threading.Event()
        desk.process = self

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.entered += 1
        self.release.wait(timeout=15)
        return self.original(*args, **kwargs)


def full_post(live: Live, ref: str) -> socket.socket:
    s = connect(live.base)
    s.sendall(post_head(len(lead_body(ref, f"{ref}@example.test"))) + lead_body(ref, f"{ref}@example.test"))
    return s


def test_processing_is_never_closed(live, monkeypatch) -> None:
    """A connection whose request is complete and being acted on keeps its place, whatever else arrives."""
    cap(monkeypatch, 2)
    hold = Hold(live.rig.desk)
    held = [full_post(live, "pr1"), full_post(live, "pr2")]
    wait_until(lambda: hold.entered == 2, what="both requests in the desk")
    s, got = connect(live.base), b""
    s.sendall(f"GET /counts HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    got, ending = read_end(s)
    s.close()
    assert parse_reply(got)[0] == 503 and ending == "eof"  # nothing can be closed: the new one is told to come back
    hold.release.set()
    for h in held:  # both are still alive and finish normally
        assert parse_reply(read_all(h))[0] == 201
        h.close()
    assert live.rig.leads() == 2


def test_body_reader_closed_work_stays(live, monkeypatch) -> None:
    cap(monkeypatch, 2)
    hold = Hold(live.rig.desk)
    busy = full_post(live, "pr3")
    wait_until(lambda: hold.entered == 1, what="the request in the desk")
    reader = live.body_phase()  # headers in, body never comes
    wait_until(lambda: live.count == 2 and live.in_header == 0, what="both places taken")
    assert call(live.base, "GET", "/counts")[0] == 200  # the body reader was closed to make room
    assert read_end(reader) == (b"", "eof")
    hold.release.set()
    assert parse_reply(read_all(busy))[0] == 201  # the processing one was not touched
    busy.close()
    reader.close()
    assert live.rig.leads() == 1


def test_body_phase_crowd_cannot_block(live, monkeypatch) -> None:
    """The cap filled with senders that finished their headers and then stall: a real request is still served."""
    cap(monkeypatch, 3)
    crowd = [live.body_phase() for _ in range(3)]
    wait_until(lambda: live.count == 3 and live.in_header == 0, what="three connections past their headers")
    assert call(live.base, "POST", "/leads", {"source": "form", "source_ref": "real2", "payload": lead()})[0] == 201
    assert read_end(crowd[0]) == (b"", "eof")  # the oldest was closed to make room
    assert live.rig.leads() == 1
    for s in crowd:
        s.close()


def handler_finished(monkeypatch) -> threading.Event:
    """Set when the handler thread of the next connection has run its last step (the client sees the close earlier,
    because the server shuts the socket down when it closes a connection for room)."""
    done = threading.Event()
    original = server._Handler.finish

    def finish(self: Any) -> None:
        try:
            original(self)
        finally:
            done.set()

    monkeypatch.setattr(server._Handler, "finish", finish)
    return done


def evict_once(live: Live, monkeypatch, method: str) -> list[Any]:
    """Wrap _Slot.<method> so that, on its first call, a new connection takes the only place first (closing this one
    for room), then the real method runs. Returns the list of extra (socket, slot) pairs to give back."""
    extra: list[Any] = []
    original = getattr(server._Slot, method)

    def wrapped(self: Any) -> Any:
        if not extra:
            sock = socket.socket()
            extra.append((sock, live.srv._take_slot(sock)))  # type: ignore[attr-defined]
        return original(self)

    monkeypatch.setattr(server._Slot, method, wrapped)
    return extra


def test_evicted_request_buffered(live, monkeypatch) -> None:
    """The whole request arrives in one piece; the connection is closed for room right after its headers were read:
    nothing may be stored and the connection is closed (no reply is possible)."""
    cap(monkeypatch, 1)
    extra = evict_once(live, monkeypatch, "header_done")
    done = handler_finished(monkeypatch)
    s = full_post(live, "buf1")
    got, ending = read_end(s)
    s.close()
    assert done.wait(timeout=10)  # the handler has really ended: only then can "nothing stored" be asserted
    assert extra and got == b"" and ending == "eof"
    assert live.rig.leads() == 0
    live.free(extra)
    assert call(live.base, "POST", "/leads", {"source": "form", "source_ref": "buf1", "payload": lead()})[0] == 201


def test_evicted_after_body_no_store(live, monkeypatch) -> None:
    """Closed for room after the body was read but before the desk acts: the receipt is written only after that
    point, so nothing is stored (a committed request can never be closed)."""
    cap(monkeypatch, 1)
    extra = evict_once(live, monkeypatch, "start_work")
    done = handler_finished(monkeypatch)
    s = full_post(live, "buf2")
    got, ending = read_end(s)
    s.close()
    assert done.wait(timeout=10)
    assert extra and got == b"" and ending == "eof"
    assert live.rig.leads() == 0 and live.rig.query("SELECT COUNT(*) FROM receipts")[0][0] == 0
    live.free(extra)


# -- the total number of handler threads
class BlockFollowUps(Hooks):
    """Holds every follow-up after its reply was written, so the handler thread stays alive."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = 0

    def crash_point(self, name: str, conn: Any) -> None:
        if name == "D1":
            self.entered += 1
            self.release.wait(timeout=15)


def test_handler_threads_are_capped(env, monkeypatch) -> None:
    hook = BlockFollowUps()
    lv = Live(env, hooks=hook)
    try:
        monkeypatch.setattr(server, "MAX_HANDLER_THREADS", 2)
        for ref in ("th1", "th2"):
            assert (
                post_lead(lv.base, "form", ref, lead(email=f"{ref}@example.test"))[0] == 201
            )  # replied; the thread is still in its follow-up
        wait_until(lambda: lv.threads == 2 and lv.count == 0, what="two threads alive, no place held")
        s = connect(lv.base)
        s.sendall(post_head(len(lead_body("th3", "t3@example.test"))) + lead_body("th3", "t3@example.test"))
        got, ending = read_end(s)
        s.close()
        status, headers, body = parse_reply(got)
        assert (status, body, ending) == (503, BUSY_REPLY, "eof")  # a complete reply, not a dropped connection
        assert headers["retry-after"] == str(constants.BUSY_RETRY_AFTER_SECONDS)
        assert lv.rig.leads() == 2  # nothing was stored for the refused request
        hook.release.set()
        wait_until(lambda: lv.threads == 0, what="threads ended")
        assert post_lead(lv.base, "form", "th3", lead())[0] == 201  # the same request id now works
    finally:
        hook.release.set()
        lv.stop()


# -- the server never shuts a socket down after its own thread closed it
class _FakeSock:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def shutdown(self, how: int) -> None:
        self.calls.append(f"shutdown{how}")

    def close(self) -> None:
        self.calls.append("close")


def test_no_shutdown_after_close(live, monkeypatch) -> None:
    cap(monkeypatch, 1)
    srv = live.srv
    sock = _FakeSock()
    slot = srv._take_slot(sock)
    srv._tls.slot = slot  # this thread plays the victim's own handler thread
    try:
        srv.shutdown_request(sock)  # the victim closes its socket first
        other = socket.socket()
        newer = srv._take_slot(other)  # then it is picked as the one to close for room
        assert newer is not None and slot.evicted
        assert sock.calls == [f"shutdown{socket.SHUT_WR}", "close"]  # no shutdown on a socket number given back
        newer.release()
        other.close()
    finally:
        del srv._tls.slot


# -- no short timeout left over on the socket when the reply is written
def test_reply_write_uses_reply_timeout(env, monkeypatch) -> None:
    seen: list[float | None] = []
    original = server._Handler.send_response

    def spy(self, *args: Any, **kwargs: Any) -> None:
        seen.append(self.connection.gettimeout())
        original(self, *args, **kwargs)

    monkeypatch.setattr(server._Handler, "send_response", spy)
    base = env.serve(env.rig())
    assert post_lead(base, "form", "tmo1", lead())[0] == 201
    assert call(base, "GET", "/counts")[0] == 200
    assert seen == [server.REPLY_WRITE_TIMEOUT_SECONDS] * 2


class _TimesOut:
    """A demo extension whose handler itself raises a TimeoutError (not one of the server's read limits)."""

    assets: dict[str, tuple[bytes, str]] = {}

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        raise TimeoutError("the extension's own wait")


def test_drain_expiry_is_not_a_408_later(live, monkeypatch) -> None:
    """A GET whose announced body never arrives: the drain runs out of time. A TimeoutError from the extension
    afterwards is the extension's failure (500), not the server's stalled-read 408."""
    monkeypatch.setattr(server, "DRAIN_TOTAL_SECONDS", 0.2)
    live.srv.demo = _TimesOut()
    s = connect(live.base)
    s.sendall(f"GET /api/demo/x HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 100\r\n\r\nxy".encode())
    got, ending = read_end(s)
    s.close()
    status, _, body = parse_reply(got)
    assert (status, body, ending) == (500, {"error": "internal"}, "eof")


def test_drain_phase_expiry_is_quiet(live, monkeypatch, caplog) -> None:
    """The drain's own time limit (a sender that announced more than it sends after an early reply) ends the drain
    quietly: the early reply stands, nothing is logged as a failure, and the connection ends in an orderly way."""
    monkeypatch.setattr(server, "DRAIN_TOTAL_SECONDS", 0.2)
    s = connect(live.base)
    t0 = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="lead_desk"):
        s.sendall(f"POST /nowhere HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 100\r\n\r\nxy".encode())
        got, ending = read_end(s)
    s.close()
    assert parse_reply(got)[0] == 404 and ending == "eof"
    assert time.monotonic() - t0 < 3
    assert not [r for r in caplog.records if "handler failed" in r.getMessage()]


def test_demo_body_stall_gives_408(env, monkeypatch, short) -> None:
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: _TimesOut())
    base = env.serve(env.rig())
    s = connect(base)
    head = f"POST /api/demo/x HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
    s.sendall((head + "Content-Length: 50\r\n\r\n{").encode())
    got, ending = read_end(s)
    s.close()
    status, _, body = parse_reply(got)
    assert (status, body, ending) == (408, TIMEOUT_REPLY, "eof")


def test_reply_timeout_restored(live, monkeypatch) -> None:
    """After a read that was limited by a short deadline, the socket timeout is back at the reply-write limit when the
    reply is written, for every reply on the way (this pins the restore inside _send_bytes, not the reader's)."""
    seen: list[float | None] = []
    original = server._Handler.send_response
    monkeypatch.setattr(server._DeadlineReader, "_restore_timeout", lambda self: None)

    def spy(self, *args: Any, **kwargs: Any) -> None:
        seen.append(self.connection.gettimeout())
        original(self, *args, **kwargs)

    monkeypatch.setattr(server._Handler, "send_response", spy)
    assert post_lead(live.base, "form", "rt1", lead())[0] == 201
    assert seen == [server.REPLY_WRITE_TIMEOUT_SECONDS]


# -- write errors and read resets
class _Boom:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def write(self, data: bytes) -> None:
        raise self.exc

    def flush(self) -> None:
        pass


class _Conn:
    def settimeout(self, value: float | None) -> None:
        pass


def handler_with_failing_write(exc: BaseException) -> Any:
    h = object.__new__(server._Handler)
    h.request_version, h.command, h.requestline = "HTTP/1.0", "GET", "GET / HTTP/1.0"
    h.wfile = _Boom(exc)
    h.connection = _Conn()
    h.close_connection = False

    class NoSlot:
        def release(self) -> None:
            pass

    h._slot = NoSlot()
    return h


GONE = [
    ConnectionResetError(),
    BrokenPipeError(),
    ConnectionAbortedError(),
    TimeoutError(),
]


@pytest.mark.parametrize("exc", GONE, ids=lambda e: type(e).__name__)
def test_gone_client_write_is_quiet(caplog, exc) -> None:
    h = handler_with_failing_write(exc)
    with caplog.at_level(logging.DEBUG):
        h._send_bytes(200, b"{}", "application/json")
    assert h.close_connection is True
    assert not caplog.records


def test_other_write_error_is_logged(caplog) -> None:
    h = handler_with_failing_write(OSError(errno.EIO, "disk"))
    with caplog.at_level(logging.DEBUG):
        h._send_bytes(200, b"{}", "application/json")  # must not raise
    assert h.close_connection is True
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert msgs == ["reply write failed: OSError"]


def test_read_reset_is_quiet(live, capsys, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        for _ in range(10):
            s = connect(live.base)
            s.sendall(b"POST /leads HTTP/1.1\r\nHost: x\r\nContent-Le")  # the server is waiting for the rest
            reset_on_close(s)
            s.close()  # reset while the server reads
        wait_until(lambda: live.count == 0, what="places freed")
        assert get_ok(live.base) == 200
        wait_until(lambda: live.threads == 0, what="every handler thread ended")  # a late traceback must be seen
    assert not [r for r in caplog.records if "handler failed" in r.getMessage()]
    assert "Traceback" not in capsys.readouterr().err  # the standard library prints one for a failed handler


# -- a thread that cannot start
def test_thread_start_failure_answers_busy(live, monkeypatch) -> None:
    def boom(self, request, client_address) -> None:
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(socketserver.ThreadingMixIn, "process_request", boom)
    s = connect(live.base)
    s.sendall(f"GET /counts HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    status, _, body = parse_reply(read_all(s))
    s.close()
    assert status == 503 and body == BUSY_REPLY
    assert live.count == 0 and live.srv._pending == {}  # type: ignore[attr-defined]
    assert live.threads == 0  # the thread that never started is not counted
    monkeypatch.undo()
    assert call(live.base, "GET", "/counts")[0] == 200


# -- the limits are checked
GOOD = {
    "max_connections": constants.MAX_CONNECTIONS,
    "header": constants.HEADER_READ_DEADLINE_SECONDS,
    "body": constants.BODY_READ_DEADLINE_SECONDS,
    "busy_write": constants.BUSY_WRITE_TIMEOUT_SECONDS,
    "retry_after": constants.BUSY_RETRY_AFTER_SECONDS,
    "max_threads": constants.MAX_HANDLER_THREADS,
    "quiet_drain": constants.QUIET_DRAIN_SECONDS,
    "drain_total": constants.DRAIN_TOTAL_SECONDS,
    "request_timeout": constants.REQUEST_TIMEOUT_SECONDS,
}


def test_default_limits_are_valid() -> None:
    constants.validate_http_limits(**GOOD)
    assert constants.MAX_CONNECTIONS_MIN <= constants.MAX_CONNECTIONS <= constants.MAX_CONNECTIONS_MAX
    assert constants.MAX_CONNECTIONS <= constants.MAX_HANDLER_THREADS <= constants.MAX_HANDLER_THREADS_MAX


@pytest.mark.parametrize("n", [constants.MAX_CONNECTIONS_MIN, constants.MAX_CONNECTIONS_MAX])
def test_conn_limit_bounds_accepted(n) -> None:
    constants.validate_http_limits(**{**GOOD, "max_connections": n, "max_threads": constants.MAX_HANDLER_THREADS_MAX})


@pytest.mark.parametrize("n", [7, 4097])
def test_conn_limit_outside_refused(n) -> None:
    with pytest.raises(ValueError, match="MAX_CONNECTIONS"):
        constants.validate_http_limits(**{**GOOD, "max_connections": n})


@pytest.mark.parametrize(
    ("field", "value", "words"),
    [
        ("max_connections", 64.0, "MAX_CONNECTIONS"),
        ("max_connections", True, "MAX_CONNECTIONS"),
        ("max_connections", 0, "MAX_CONNECTIONS"),
        ("header", 0, "HEADER_READ_DEADLINE_SECONDS"),
        ("header", True, "HEADER_READ_DEADLINE_SECONDS"),
        ("header", float("inf"), "HEADER_READ_DEADLINE_SECONDS"),
        ("body", float("nan"), "BODY_READ_DEADLINE_SECONDS"),
        ("busy_write", -1.0, "BUSY_WRITE_TIMEOUT_SECONDS"),
        ("busy_write", "1", "BUSY_WRITE_TIMEOUT_SECONDS"),
        ("retry_after", 0, "BUSY_RETRY_AFTER_SECONDS"),
        ("max_threads", 7, "MAX_HANDLER_THREADS"),
        ("max_threads", 65537, "MAX_HANDLER_THREADS"),
        ("max_threads", 1024.0, "MAX_HANDLER_THREADS"),
        ("max_threads", True, "MAX_HANDLER_THREADS"),
        ("max_threads", constants.MAX_CONNECTIONS - 1, "MAX_HANDLER_THREADS"),  # just below MAX_CONNECTIONS
        ("quiet_drain", 0, "QUIET_DRAIN_SECONDS"),
        ("quiet_drain", float("inf"), "QUIET_DRAIN_SECONDS"),
        ("drain_total", -1, "DRAIN_TOTAL_SECONDS"),
        ("drain_total", False, "DRAIN_TOTAL_SECONDS"),
        ("request_timeout", 0.0, "REQUEST_TIMEOUT_SECONDS"),
        ("request_timeout", float("nan"), "REQUEST_TIMEOUT_SECONDS"),
    ],
)
def test_bad_limit_is_refused(field, value, words) -> None:
    with pytest.raises(ValueError, match=words):
        constants.validate_http_limits(**{**GOOD, field: value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_CONNECTIONS", 0),
        ("MAX_HANDLER_THREADS", 7),
        ("HEADER_READ_DEADLINE_SECONDS", True),
        ("BODY_READ_DEADLINE_SECONDS", float("inf")),
        ("QUIET_DRAIN_SECONDS", 0),
        ("REQUEST_TIMEOUT_SECONDS", float("nan")),
    ],
)
def test_import_refuses_a_bad_constant(name, value) -> None:
    """The check runs when constants.py is imported: running its source afresh with one value replaced must raise."""
    import re
    from pathlib import Path

    src = Path(constants.__file__)
    literal = "float('nan')" if value != value else "float('inf')" if value == float("inf") else repr(value)
    pattern = re.compile(rf"^{name}: Final = [^#\n]*", re.M)
    text = src.read_text(encoding="utf-8")
    assert pattern.search(text), name
    patched = pattern.sub(f"{name}: Final = {literal}", text, count=1)
    with pytest.raises(ValueError, match=name):
        exec(compile(patched, str(src), "exec"), {"__name__": "constants_probe"})  # noqa: S102


def test_deadlines_fit_idle_timeout() -> None:
    with pytest.raises(ValueError, match="REQUEST_TIMEOUT_SECONDS"):
        constants.validate_http_limits(**{**GOOD, "header": constants.REQUEST_TIMEOUT_SECONDS + 1})
    with pytest.raises(ValueError, match="DRAIN_TOTAL_SECONDS"):
        constants.validate_http_limits(**{**GOOD, "quiet_drain": constants.DRAIN_TOTAL_SECONDS + 1})


def test_vanished_client_logs_no_failure(live, caplog, capsys) -> None:
    base = live.base
    with caplog.at_level(logging.DEBUG):
        for _ in range(10):
            s = connect(base)
            s.sendall(b"\x00\xff garbage not http\r\n\r\n")
            s.close()  # closes without reading the error reply
            s = connect(base)
            s.sendall(b"BREW /leads HTTP/1.1\r\nHost: x\r\n\r\n")
            reset_on_close(s)  # reset on close
            s.close()
        assert get_ok(base) == 200
        wait_until(lambda: live.threads == 0, what="every handler thread ended")
    assert not [r for r in caplog.records if "handler failed" in r.getMessage()]
    assert "Traceback" not in capsys.readouterr().err  # the standard library prints one when a reply write fails


def test_normal_get_and_post_work(env) -> None:
    base = env.serve(env.rig())
    assert call(base, "GET", "/counts")[0] == 200
    status, body = post_lead(base, "form", "normal1", lead())
    assert status == 201 and body["verdict"] == "new"
