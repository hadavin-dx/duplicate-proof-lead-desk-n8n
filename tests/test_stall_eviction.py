"""Closing a connection for room only when it has stalled, a total limit for writing a reply, the
open-file check, no automatic try used up by a lack of file handles, a refusal that never waits for a silent client,
and SIGTERM. Made-up data only."""

from __future__ import annotations

import errno
import logging
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.error
from dataclasses import replace
from typing import Any

import pytest
from support import HOST, addr, call, lead, raw_http, wait_until

from lead_desk import adapters, constants, lockfile, server
from lead_desk.adapters import HttpOutbox
from lead_desk.contract import SendNoDescriptors, SendUnreachable, is_fd_exhaustion
from lead_desk.desk import Desk


class _Res:
    """A stand-in for the `resource` module."""

    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, soft: int, hard: int, can_raise: bool = True) -> None:
        self.limits = (soft, hard)
        self.can_raise = can_raise

    def getrlimit(self, which: int) -> tuple[int, int]:
        assert which == self.RLIMIT_NOFILE
        return self.limits

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        if not self.can_raise:
            raise ValueError("not allowed")
        self.limits = limits


# -- eviction by stall, not by age
class _Sock:
    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def small_server(env, monkeypatch):
    rig = env.rig()
    srv = server.make_server(rig.desk, replace(rig.desk.config, port=0))
    monkeypatch.setattr(server, "MAX_CONNECTIONS", 3)
    monkeypatch.setattr(server, "EVICT_MIN_IDLE_SECONDS", 2.0, raising=False)
    yield srv
    srv.server_close()


def test_full_cap_sending_slots_stay(small_server) -> None:
    held = [small_server._take_slot(_Sock()) for _ in range(3)]
    assert all(h is not None for h in held)
    for h in held:
        h.touch()  # all three just sent bytes
    assert small_server._take_slot(_Sock()) is None  # the newcomer gets the 503, nobody is closed
    assert not any(h.evicted for h in held)


def test_stalled_slot_is_closed_for_room(small_server) -> None:
    held = [small_server._take_slot(_Sock()) for _ in range(3)]
    held[1].last_progress -= 10  # the middle one sent nothing for 10 s
    held[0].touch()
    newcomer = small_server._take_slot(_Sock())
    assert newcomer is not None
    assert [h.evicted for h in held] == [False, True, False]  # the stalled one, not the oldest


def test_working_slot_is_never_closed(small_server) -> None:
    held = [small_server._take_slot(_Sock()) for _ in range(3)]
    for h in held:
        h.last_progress -= 10  # all stalled for a long time ...
        h.start_work()  # ... but the desk is acting on them
    assert small_server._take_slot(_Sock()) is None
    assert not any(h.evicted for h in held)


def test_dripping_crowd_is_not_evicted_live(env, monkeypatch) -> None:
    """Real sockets: two connections that keep sending a byte every 0.1 s hold the cap; the third is refused with a
    503. Once they stop sending and 0.6 s pass, a newcomer is served and the oldest stalled one is closed."""
    monkeypatch.setattr(server, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(server, "EVICT_MIN_IDLE_SECONDS", 0.6, raising=False)
    base = env.serve(env.rig())
    socks = [socket.create_connection(addr(base), timeout=8) for _ in range(2)]
    stop = threading.Event()

    def drip(s: socket.socket) -> None:
        while not stop.is_set():
            try:
                s.sendall(b"G")
            except OSError:
                return
            time.sleep(0.1)

    threads = [threading.Thread(target=drip, args=(s,), daemon=True) for s in socks]
    for t in threads:
        t.start()
    time.sleep(0.3)
    status, body = call(base, "GET", "/counts")
    assert (status, body) == (503, {"error": "busy", "reason": "busy"})  # nobody stalled: nobody closed
    stop.set()
    for t in threads:
        t.join(timeout=5)
    time.sleep(0.8)  # now both have been silent for longer than the minimum
    assert call(base, "GET", "/counts")[0] == 200
    for s in socks:
        s.close()


def test_idle_sockets_closed_at_deadline(env, monkeypatch) -> None:
    """More silent connections than the cap hold every place at first (a valid request gets 503 busy), but the header
    deadline answers each of them 408 and closes it, and then the places are free again."""
    monkeypatch.setattr(server, "MAX_CONNECTIONS", 4)
    monkeypatch.setattr(server, "HEADER_READ_DEADLINE_SECONDS", 1.5)
    monkeypatch.setattr(server, "EVICT_MIN_IDLE_SECONDS", 60.0, raising=False)  # no eviction: only the deadline frees
    base = env.serve(env.rig())
    idle = [socket.create_connection(addr(base), timeout=8) for _ in range(12)]
    try:
        assert call(base, "GET", "/counts")[0] == 503  # the places are taken by silent clients: backpressure
        deadline = time.monotonic() + 1.5 + 4.0
        closed = 0
        for sock in idle:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data = b""
                while chunk := sock.recv(4096):
                    data += chunk
                closed += 1 if (b"408" in data or data == b"" or b"503" in data) else 0
            except OSError:
                pass
        assert closed == len(idle), closed  # every idle socket got its answer and was closed within deadline + margin
        status, _ = call(base, "POST", "/leads", {"source": "form", "source_ref": "idle-1", "payload": lead()})
        assert status == 201
    finally:
        for sock in idle:
            sock.close()


# -- a total limit for writing a reply
def test_reply_to_a_non_reader_ends_in_time(env, monkeypatch) -> None:
    """The client has a tiny receive buffer and never reads: the handler gives up within about the reply deadline,
    not after the 30 s read limit."""
    monkeypatch.setattr(server, "REPLY_WRITE_TIMEOUT_SECONDS", 0.5, raising=False)
    a, b = socket.socketpair()
    try:
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        b.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)

        class Slot:
            def release(self) -> None:
                pass

        h = server._Handler.__new__(server._Handler)
        h.connection, h.wfile, h._slot = a, socketserver._SocketWriter(a), Slot()  # type: ignore[assignment]
        h.request_version, h.requestline, h.command = "HTTP/1.1", "", "GET"
        h.timeout = 3.0  # a wait well past the deadline under test
        t0 = time.monotonic()
        h._send_bytes(200, b"x" * 8_000_000, "text/plain")
        took = time.monotonic() - t0
    finally:
        a.close()
        b.close()
    assert took < 1.5, took
    assert h.close_connection is True


# -- the open-file check at start-up
def test_budget_enough_changes_nothing() -> None:
    res = _Res(soft=10_000, hard=10_000)
    server.ensure_descriptor_budget(100, res)
    assert res.limits == (10_000, 10_000)


def test_budget_unlimited_changes_nothing() -> None:
    res = _Res(soft=_Res.RLIM_INFINITY, hard=_Res.RLIM_INFINITY)
    server.ensure_descriptor_budget(60_000, res)
    assert res.limits == (-1, -1)


def test_budget_raises_soft_within_hard() -> None:
    res = _Res(soft=256, hard=100_000)
    server.ensure_descriptor_budget(1000, res)
    need = constants.FDS_PER_HANDLER_THREAD * 1000 + constants.FD_RESERVE
    assert res.limits == (need, 100_000)


def test_budget_refused_names_the_setting() -> None:
    res = _Res(soft=1024, hard=1024)
    with pytest.raises(server.DescriptorBudgetError, match="MAX_HANDLER_THREADS=1024") as info:
        server.ensure_descriptor_budget(1024, res)
    need = 1024 * constants.FDS_PER_HANDLER_THREAD + constants.FD_RESERVE  # derived, not typed
    text = str(info.value)
    assert f"to at least {need}" in text and "ulimit -n" in text and "Lower MAX_HANDLER_THREADS" not in text
    assert res.limits == (1024, 1024)


def test_budget_refused_when_raise_fails() -> None:
    res = _Res(soft=256, hard=100_000, can_raise=False)
    with pytest.raises(server.DescriptorBudgetError, match="MAX_HANDLER_THREADS"):
        server.ensure_descriptor_budget(1000, res)


def test_main_exits_3_on_a_too_small_limit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(server, "resource", _Res(soft=100, hard=100))
    with pytest.raises(SystemExit) as info:
        server.main()
    assert info.value.code == constants.EXIT_SETTING_REFUSED == 3
    assert "MAX_HANDLER_THREADS" in capsys.readouterr().out


# -- no automatic try is used up by a lack of file handles
def _outbox_rig(env, monkeypatch, exc: BaseException):
    srv, url = env.fakes("keyed")
    ob = HttpOutbox(url, 1.0)

    def refuse(*a: Any, **k: Any) -> Any:
        raise exc

    monkeypatch.setattr(adapters, "_exchange", refuse)
    rig = env.rig(outbox=ob)
    return rig


def _try_once(rig) -> tuple[str, str | None, int]:
    lead_id = rig.desk.process("form", "fd1", lead()).body["lead_id"]
    rig.desk.run_followup(lead_id)
    state, err = rig.state()
    return state, err, int(rig.query("SELECT auto_attempts FROM followups")[0][0])


def test_no_file_handles_is_not_a_try(env, monkeypatch) -> None:
    rig = _outbox_rig(env, monkeypatch, urllib.error.URLError(OSError(errno.EMFILE, "too many open files")))
    state, err, tries = _try_once(rig)
    assert (state, err, tries) == ("pending", "no_descriptors", 0)  # waits, and no automatic try is used up


def test_file_table_full_is_not_a_try(env, monkeypatch) -> None:
    rig = _outbox_rig(env, monkeypatch, OSError(errno.ENFILE, "file table overflow"))
    assert _try_once(rig) == ("pending", "no_descriptors", 0)


def test_unreachable_sender_is_still_a_try(env, monkeypatch) -> None:
    rig = _outbox_rig(env, monkeypatch, urllib.error.URLError(ConnectionRefusedError()))
    state, err, tries = _try_once(rig)
    assert (state, err, tries) == ("pending", "unreachable", 1)


def test_fd_exhaustion_is_seen_through_the_chain() -> None:
    inner = OSError(errno.EMFILE, "x")
    assert is_fd_exhaustion(urllib.error.URLError(inner))
    assert is_fd_exhaustion(SendNoDescriptors("x"))
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert is_fd_exhaustion(outer)
    assert not is_fd_exhaustion(urllib.error.URLError(ConnectionRefusedError()))
    assert not is_fd_exhaustion(None) and not is_fd_exhaustion(SendUnreachable("x"))


def test_adapter_maps_emfile_to_no_descriptors(env, monkeypatch) -> None:
    srv, url = env.fakes("keyed")
    ob = HttpOutbox(url, 1.0)

    def refuse(*a: Any, **k: Any) -> Any:
        raise urllib.error.URLError(OSError(errno.EMFILE, "x"))

    monkeypatch.setattr(adapters, "_exchange", refuse)
    with pytest.raises(SendNoDescriptors):
        ob.send("L000000000001", "ada@example.test", "s", "b", 1.0)


# -- a refusal never waits for a silent client
def _tcp_pair() -> tuple[socket.socket, socket.socket]:
    ls = socket.socket()
    ls.bind((HOST, 0))
    ls.listen(1)
    c = socket.create_connection(ls.getsockname(), timeout=8)
    s, _ = ls.accept()
    ls.close()
    return c, s


class _Refuser:
    def __init__(self) -> None:
        self._linger = server._Lingerer()  # one per object, closed with close()

    def close(self) -> None:
        self._linger.close_all()

    def shutdown_request(self, request: socket.socket) -> None:
        request.close()


def test_refusals_are_not_paced_by_the_drain() -> None:
    """40 silent clients: a refusal that waited QUIET_DRAIN_SECONDS (0.05 s) for each would take 2 s in all."""
    pairs = [_tcp_pair() for _ in range(40)]
    refuser = _Refuser()
    try:
        t0 = time.monotonic()
        for _, srv_side in pairs:
            server._Server._refuse_busy(refuser, srv_side)  # type: ignore[arg-type]
        took = time.monotonic() - t0
        assert took < 40 * constants.QUIET_DRAIN_SECONDS / 4, took
        c = pairs[0][0]
        assert c.recv(4096).startswith(b"HTTP/1.0 503")
    finally:
        refuser.close()
        for c, _ in pairs:
            c.close()


def test_refuse_busy_drains_arrived_bytes() -> None:
    """Bytes the client already sent are read and dropped, so closing does not reset the connection under the
    reply: the client reads the whole 503 and an orderly end."""
    c, srv_side = _tcp_pair()
    refuser = _Refuser()
    try:
        c.sendall(b"x" * 100_000)
        time.sleep(0.3)  # all of it has arrived on the server side
        server._Server._refuse_busy(refuser, srv_side)  # type: ignore[arg-type]
        data = b""
        while chunk := c.recv(65536):
            data += chunk
        assert data.startswith(b"HTTP/1.0 503") and data.endswith(b'"busy"}')
    finally:
        refuser.close()
        c.close()


def test_handler_cap_503_is_complete(env, monkeypatch) -> None:
    monkeypatch.setattr(server, "MAX_HANDLER_THREADS", 0)
    base = env.serve(env.rig())
    raw = raw_http(base, f"GET /counts HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 503") and b"Retry-After: 1" in head
    assert body == b'{"error": "busy", "reason": "busy"}'


# -- SIGTERM
class _TermStub:
    def __init__(self, calls: list[str], handlers: dict[int, Any], go: threading.Event) -> None:
        self.calls, self.handlers, self.go = calls, handlers, go

    @property
    def server_address(self) -> tuple[str, int]:
        self.calls.append("address")  # main() reads it just before it prints the listening line
        return (HOST, 0)

    def serve_forever(self) -> None:
        self.go.wait(timeout=20)
        self.handlers[signal.SIGTERM](signal.SIGTERM, None)  # what the operating system does in the main thread
        raise AssertionError("serve_forever must not return normally")

    def server_close(self) -> None:
        self.calls.append("server_close")


def _run_main_term(monkeypatch, tmp_path, rig, reaper_stuck: bool, again: bool = False):
    calls: list[str] = []
    handlers: dict[int, Any] = {}
    go = threading.Event()
    seen: list[Desk] = []

    class Recording(Desk):
        def stop_background(self, *a: Any, **k: Any) -> None:
            calls.append("stop_background")
            if again:  # a second signal while the shutdown steps run
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                handlers[signal.SIGINT](signal.SIGINT, None)
            super().stop_background(*a, **k)

        def reaper_running(self) -> bool:
            return True if reaper_stuck else super().reaper_running()

    def make_desk(*args: Any, **kwargs: Any) -> Desk:
        d = Recording(*args, **kwargs)
        seen.append(d)
        return d

    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "term.sqlite3"))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setattr(server, "Desk", make_desk)
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (rig.desk.crm, rig.desk.outbox, rig.desk.qualifier))
    monkeypatch.setattr(server, "make_server", lambda desk, cfg: _TermStub(calls, handlers, go))
    monkeypatch.setattr(
        server.signal, "signal", lambda sig, fn: (calls.append("signal"), handlers.__setitem__(sig, fn))
    )
    t = threading.Thread(target=server.main, daemon=True)
    t.start()
    wait_until(lambda: signal.SIGTERM in handlers, what="the SIGTERM handler was installed")
    return calls, go, t


def test_sigterm_stops_like_ctrl_c(env, monkeypatch, tmp_path) -> None:
    calls, go, t = _run_main_term(monkeypatch, tmp_path, env.rig(), reaper_stuck=False)
    assert server.signal.getsignal(signal.SIGTERM) is not None
    go.set()
    t.join(timeout=20)
    assert not t.is_alive()
    assert [c for c in calls if c not in ("address", "signal")] == ["stop_background", "server_close"]  # reaper first
    lockfile.acquire(tmp_path / "term.sqlite3").close()  # the lock was given back


def test_sigterm_handler_before_listening(env, monkeypatch, tmp_path) -> None:
    calls, go, t = _run_main_term(monkeypatch, tmp_path, env.rig(), reaper_stuck=False)
    go.set()
    t.join(timeout=20)
    assert "signal" in calls and "address" in calls
    assert calls.index("signal") < calls.index("address"), calls  # installed before the listening line is printed


def test_second_signal_is_harmless(env, monkeypatch, tmp_path) -> None:
    calls, go, t = _run_main_term(monkeypatch, tmp_path, env.rig(), reaper_stuck=False, again=True)
    go.set()
    t.join(timeout=20)
    assert not t.is_alive()
    assert [c for c in calls if c not in ("address", "signal")] == ["stop_background", "server_close"]
    lockfile.acquire(tmp_path / "term.sqlite3").close()  # every step ran, the lock was given back


def test_sigterm_stuck_reaper_is_logged(env, monkeypatch, tmp_path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="lead_desk"):
        calls, go, t = _run_main_term(monkeypatch, tmp_path, env.rig(), reaper_stuck=True)
        go.set()
        t.join(timeout=20)
    assert not t.is_alive()
    assert [r for r in caplog.records if "reaper did not end in time" in r.getMessage()]
    lockfile.acquire(tmp_path / "term.sqlite3").close()


def test_sigterm_handler_raises_ctrl_c() -> None:
    with pytest.raises(KeyboardInterrupt):
        server._on_sigterm(signal.SIGTERM, None)


# -- the HTTP limits are validated
GOOD = {
    "max_connections": constants.MAX_CONNECTIONS,
    "header": constants.HEADER_READ_DEADLINE_SECONDS,
    "body": constants.BODY_READ_DEADLINE_SECONDS,
    "busy_write": constants.BUSY_WRITE_TIMEOUT_SECONDS,
    "retry_after": constants.BUSY_RETRY_AFTER_SECONDS,
}


@pytest.mark.parametrize(
    ("field", "value", "words"),
    [
        ("evict_min_idle", 0, "EVICT_MIN_IDLE_SECONDS"),
        ("evict_min_idle", True, "EVICT_MIN_IDLE_SECONDS"),
        ("evict_min_idle", float("nan"), "EVICT_MIN_IDLE_SECONDS"),
        ("evict_min_idle", constants.HEADER_READ_DEADLINE_SECONDS + 1, "EVICT_MIN_IDLE_SECONDS"),
        ("reply_write", 0, "REPLY_WRITE_TIMEOUT_SECONDS"),
        ("reply_write", "5", "REPLY_WRITE_TIMEOUT_SECONDS"),
        ("reply_write", constants.REQUEST_TIMEOUT_SECONDS + 1, "REPLY_WRITE_TIMEOUT_SECONDS"),
    ],
)
def test_bad_http_limit_is_refused(field, value, words) -> None:
    with pytest.raises(ValueError, match=words):
        constants.validate_http_limits(**{**GOOD, field: value})


def test_http_limits_default_values() -> None:
    assert constants.EVICT_MIN_IDLE_SECONDS == 2.0 and constants.REPLY_WRITE_TIMEOUT_SECONDS == 5.0


class _NoLinger(_Refuser):
    """A closer without a lingerer: the socket is closed as soon as the bytes that already arrived are read."""

    def __init__(self) -> None:
        self._linger = self  # type: ignore[assignment]

    @staticmethod
    def add(_sock: socket.socket) -> bool:
        return False

    def close(self) -> None:
        return


def _late_request_reply(refuser: object) -> bytes:
    """The client's request bytes arrive AFTER the refusal was written (the usual case on a fast loopback: the server
    answers before the request has even landed). Returns what the client managed to read."""
    c, srv_side = _tcp_pair()
    try:
        server._Server._refuse_busy(refuser, srv_side)  # type: ignore[arg-type]
        c.sendall(b"GET /counts HTTP/1.1\r\nHost: x\r\n\r\n")
        c.settimeout(3)
        data = b""
        try:
            while chunk := c.recv(65536):
                data += chunk
        except OSError:  # a reset: the reply may be gone
            pass
        return data
    finally:
        c.close()


def test_late_request_does_not_reset_the_refusal() -> None:
    """Windows class: closing a socket that then receives bytes sends a reset, and the client's read fails before it
    reads the 503. The lingerer keeps the socket until the bytes have been read and dropped."""
    refuser = _Refuser()
    try:
        data = _late_request_reply(refuser)
    finally:
        refuser.close()
    assert data.startswith(b"HTTP/1.0 503") and data.endswith(b'"busy"}')


@pytest.mark.skipif(sys.platform != "win32", reason="the reset that destroys the reply happens on Windows only")
def test_without_the_lingerer_the_reply_is_lost_on_windows() -> None:
    """Negative control of the test above: without the lingerer a Windows client loses the 503. On
    Linux the same behaviour passes the test above as well, so only Windows can show the difference."""
    lost = 0
    for _ in range(5):
        data = _late_request_reply(_NoLinger())
        lost += not (data.startswith(b"HTTP/1.0 503") and data.endswith(b'"busy"}'))
    assert lost >= 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # the thread dies on purpose
def test_dead_lingerer_thread_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    lingerer = server._Lingerer()
    pairs = [_tcp_pair() for _ in range(2)]
    try:
        real = server.select.select
        calls = {"n": 0}

        def boom(*a: object, **k: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the thread dies")
            return real(*a, **k)  # type: ignore[call-overload]

        monkeypatch.setattr(server.select, "select", boom)
        assert lingerer.add(pairs[0][1]) is True
        first = lingerer._thread
        assert first is not None
        first.join(5)
        assert not first.is_alive()
        assert lingerer.add(pairs[1][1]) is True
        assert lingerer._thread is not first and lingerer._thread.is_alive()  # type: ignore[union-attr]
    finally:
        lingerer.close_all()
        for c, s in pairs:
            c.close()
            s.close()


def test_lingerer_is_bounded_and_closes_its_sockets() -> None:
    lingerer = server._Lingerer()
    pairs = [_tcp_pair() for _ in range(constants.LINGER_MAX_SOCKETS + 3)]
    try:
        taken = [lingerer.add(srv_side) for _, srv_side in pairs]
        assert taken.count(True) == constants.LINGER_MAX_SOCKETS and taken[-1] is False  # the rest: the caller closes
        for c, _ in pairs[: constants.LINGER_MAX_SOCKETS]:
            c.close()  # the client's close ends the lingering at once
        wait_until(lambda: not lingerer._items, what="lingering sockets closed by the client's end")
        assert lingerer.add(pairs[-1][1]) is True  # room again
    finally:
        lingerer.close_all()
        for c, s in pairs:
            c.close()
            s.close()
