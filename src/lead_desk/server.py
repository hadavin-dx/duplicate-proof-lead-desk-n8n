"""HTTP layer. Standard library only.

`python -m lead_desk.server` reads its settings from the environment (config.Config.from_env), checks the
open-file limit, takes the lock file, reads the sender's capability, builds the desk, binds, starts the reaper, runs
the start-up repairs (schema and sweep) and serves until stopped (Ctrl+C or SIGTERM). It prints one line
`desk listening on http://<host>:<port>` once it accepts requests: the port is the real one, so DESK_PORT=0
(ephemeral) can be learned from that line."""

from __future__ import annotations

import dataclasses
import io
import ipaddress
import json
import logging
import os
import re
import select
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import local
from types import FrameType
from typing import Any, NoReturn, cast
from urllib.parse import parse_qs, unquote, urlsplit

from lead_desk import db, lockfile, portal_api
from lead_desk.adapters import FakesUnreachable
from lead_desk.config import Config, is_loopback_host, is_unspecified, read_leadflow
from lead_desk.constants import (
    ACCEPT_PAUSE_SECONDS,
    BIND_RETRY_SECONDS,
    BIND_RETRY_STEP_SECONDS,
    BODY_READ_DEADLINE_SECONDS,
    BUSY_RETRY_AFTER_SECONDS,
    BUSY_WRITE_TIMEOUT_SECONDS,
    DEMO_BODY_MAX,
    DEMO_METHODS,
    DRAIN_MAX_BYTES,
    DRAIN_TOTAL_SECONDS,
    EVICT_MIN_IDLE_SECONDS,
    EXIT_ADAPTERS_UNREACHABLE,
    EXIT_ANOTHER_DESK,
    EXIT_DESK_SETTING_REFUSED,
    EXIT_PORT_UNAVAILABLE,
    EXIT_SETTING_REFUSED,
    FD_RESERVE,
    FD_RESERVE_ITEMS,
    FDS_PER_HANDLER_THREAD,
    HEADER_READ_DEADLINE_SECONDS,
    HTTP_BAD_REQUEST,
    HTTP_BUSY,
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_ERROR,
    HTTP_METHOD_NOT_ALLOWED,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_PERMANENT_REDIRECT,
    HTTP_REQUEST_TIMEOUT,
    HTTP_TOO_LARGE,
    LINGER_MAX_SOCKETS,
    LISTEN_QUEUE,
    LOOPBACK_HOSTS,
    MAX_CONNECTIONS,
    MAX_DIGITS,
    MAX_HANDLER_THREADS,
    PORT_MAX,
    PORTAL_MAX_INFLIGHT,
    QUIET_DRAIN_SECONDS,
    REPLY_WRITE_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SAME_SITE_VALUES,
    SECURITY_HEADERS,
    SOURCE_REF_MAX,
    SOURCE_REF_MIN,
    SOURCES,
    WILDCARD_BINDS,
    validate_descriptor_budget,
)
from lead_desk.contract import is_fd_exhaustion
from lead_desk.desk import Desk, DeskSettingRefused, Reply
from lead_desk.envnum import SettingRefused
from lead_desk.hooks import Hooks
from lead_desk.loader import DemoPortal, load_approvers, load_demo_adapters, load_demo_portal

try:
    import resource  # not on Windows
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

log = logging.getLogger("lead_desk")

_HOST_HEADER = re.compile(r"^(?P<host>\[[0-9A-Fa-f:]+\]|[^:\[\]]+)(?::(?P<port>[0-9]{1,5}))?$")


_GONE = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)
"""Errors that only mean the other side left or stalled. They are answered or dropped quietly, never as a failure."""


class _DeadlineReader(io.RawIOBase):
    """Raw socket reader with one wall-clock deadline for a whole phase (headers, or body). Before each read the
    socket timeout is set to the time left, so a client sending one byte at a time cannot hold the thread: the
    per-read socket timeout alone restarts on every byte. With no deadline armed it behaves like the plain reader.
    After every read the socket timeout goes back to the normal per-read limit, so a later reply write never runs
    on a stale, shorter timeout."""

    def __init__(
        self,
        sock: socket.socket,
        idle: Callable[[], float | None],
        aborted: Callable[[], bool],
        progress: Callable[[], None] = lambda: None,
    ) -> None:
        self._sock = sock
        self._idle = idle  # the per-read limit (the handler's `timeout`)
        self._aborted = aborted  # True once the server closed this connection to make room for a newer one
        self._progress = progress  # called when bytes arrive: the slot's proof that the sender is still sending
        self.deadline: float | None = None
        self.phase = "header"
        self.timed_out_in = ""  # the phase that ran out of time, or ""

    def arm(self, phase: str, seconds: float | None) -> None:
        self.phase = phase
        self.deadline = None if seconds is None else time.monotonic() + seconds

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        if self._aborted():
            raise ConnectionAbortedError("connection closed to make room")
        limit = self._idle()
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                self._expire()
            limit = left if limit is None else min(limit, left)
        try:
            self._sock.settimeout(limit)
            got = self._sock.recv_into(b)
        except TimeoutError:
            self._expire()
        self._restore_timeout()
        if got:
            self._progress()
        if got == 0 and self._aborted():  # the server shut the socket down under a blocked read
            raise ConnectionAbortedError("connection closed to make room")
        return got

    def _restore_timeout(self) -> None:
        try:
            self._sock.settimeout(self._idle())
        except OSError:
            pass

    def _expire(self) -> NoReturn:
        self.timed_out_in = self.phase
        self.deadline = None
        self._restore_timeout()  # the 408 reply still has to be written
        raise TimeoutError("read deadline")


class _Slot:
    """One connection's place under the cap. `release` is idempotent: whoever calls it first frees the place.

    A slot is `reading` from accept until the whole request has been read, parsed and checked and the handler starts
    to act on it (`start_work`). Only a reading slot that has received no byte for EVICT_MIN_IDLE_SECONDS may be
    closed to make room for a newer connection; the check and the
    decision to close are made under the server's lock, so once `start_work` has returned a connection is never
    closed by the server. `header_phase` is the part of `reading` before the end of the headers."""

    def __init__(self, server: _Server, sock: socket.socket) -> None:
        self.server = server
        self.sock = sock
        self.header_phase = True  # still sending the request line and headers
        self.reading = True  # still sending the request (headers or body): may be closed to make room
        self.released = False
        self.evicted = False
        self.last_progress = time.monotonic()  # accept time, then the time of the last bytes received

        self.sock_lock = threading.Lock()  # the server's shutdown of this socket and its close never overlap
        self.sock_closed = False

    def touch(self) -> None:
        """Bytes arrived: the sender is not stalled. (One float write; the eviction check reads it under the lock.)"""
        self.last_progress = time.monotonic()

    def header_done(self) -> bool:
        """The headers are in. True when the server has already closed this connection to make room."""
        with self.server._slot_lock:
            self.header_phase = False
            return self.evicted

    def start_work(self) -> None:
        """The whole request has been read and the desk is about to act on it: from here the connection is never
        closed to make room. Raises ConnectionAbortedError, before anything is stored, when it already was."""
        with self.server._slot_lock:
            if self.evicted:
                raise ConnectionAbortedError("connection closed to make room")
            self.reading = False

    def release(self) -> None:
        with self.server._slot_lock:
            if self.released:
                return
            self.released = True
            self.server._slots.pop(self, None)


class _Lingerer:
    """Keeps a refused connection open for a moment after its 503 so that request bytes still on their way are read and
    dropped. Closing a socket that still receives bytes sends a reset, and a reset can destroy a reply the client has
    not read yet (on Windows it always does: the client's read fails with "connection aborted"). One thread serves
    every such connection, so a silent client costs the accept loop nothing. At most LINGER_MAX_SOCKETS at once; a
    connection stays at most DRAIN_TOTAL_SECONDS and at most DRAIN_MAX_BYTES are read; it ends early when the client
    closes its side."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[socket.socket, list[float]] = {}  # socket -> [deadline, bytes still allowed]
        self._thread: threading.Thread | None = None
        self._stopped = False

    def add(self, sock: socket.socket) -> bool:
        """True when the socket is now ours to close; False when there is no room (the caller closes it)."""
        with self._lock:
            if self._stopped or len(self._items) >= LINGER_MAX_SOCKETS:
                return False
            try:
                sock.setblocking(False)
            except OSError:
                return False
            self._items[sock] = [time.monotonic() + DRAIN_TOTAL_SECONDS, float(DRAIN_MAX_BYTES)]
            if self._thread is None or not self._thread.is_alive():  # a thread that died is replaced, not waited for
                self._thread = threading.Thread(target=self._run, name="desk-linger", daemon=True)
                self._thread.start()
        return True

    def _drop(self, sock: socket.socket) -> None:
        with self._lock:
            self._items.pop(sock, None)
        try:
            sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        while True:
            with self._lock:
                socks = list(self._items)
                if self._stopped and not socks:
                    return
            if not socks:
                time.sleep(QUIET_DRAIN_SECONDS)
                continue
            try:
                readable = select.select(socks, [], [], QUIET_DRAIN_SECONDS)[0]
            except (OSError, ValueError):  # a socket closed from outside: drop the ones that are gone
                readable = []
                for s in socks:
                    if s.fileno() < 0:
                        self._drop(s)
            for s in readable:
                try:
                    chunk = s.recv(65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    self._drop(s)
                else:
                    with self._lock:
                        if s in self._items:
                            self._items[s][1] -= len(chunk)
            now = time.monotonic()
            with self._lock:
                due = [s for s, (deadline, left) in self._items.items() if now >= deadline or left <= 0]
            for s in due:
                self._drop(s)

    def close_all(self) -> None:
        with self._lock:
            self._stopped = True
            socks = list(self._items)
        for s in socks:
            self._drop(s)


class _Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would allow a second listener on the same port
    request_queue_size = LISTEN_QUEUE
    desk: Desk
    config: Config
    started_at: int
    demo: DemoPortal | None
    allowed_hosts: frozenset[str]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._slot_lock = threading.Lock()
        self._slots: dict[_Slot, None] = {}  # in the order the connections were accepted
        self._pending: dict[int, _Slot] = {}  # accepted, thread not yet running
        self._handlers_alive = 0  # handler threads started and not yet ended (see constants.MAX_HANDLER_THREADS)
        self._tls = local()
        self._linger = _Lingerer()
        super().__init__(*args, **kwargs)

    def server_close(self) -> None:
        self._linger.close_all()
        super().server_close()

    def live_connections(self) -> int:
        """Connections that still take a place under the cap (being read; see constants.MAX_CONNECTIONS)."""
        with self._slot_lock:
            return len(self._slots)

    def header_phase_connections(self) -> int:
        """Of those, the ones still sending their request line or headers."""
        with self._slot_lock:
            return sum(1 for s in self._slots if s.header_phase)

    def reading_connections(self) -> int:
        """Of those, the ones still sending their request (headers or body): the only ones that may be closed to make
        room."""
        with self._slot_lock:
            return sum(1 for s in self._slots if s.reading)

    def handler_threads(self) -> int:
        """Handler threads that have been started and have not ended yet."""
        with self._slot_lock:
            return self._handlers_alive

    def current_slot(self) -> _Slot:
        """The place of the connection the calling handler thread serves."""
        slot: _Slot = self._tls.slot
        return slot

    def _take_slot(self, request: socket.socket) -> _Slot | None:
        """A place for a new connection, or None when the cap is full and no connection can be closed. At the cap the
        oldest connection that is still being read and has sent nothing for EVICT_MIN_IDLE_SECONDS is closed to make
        room; a connection that is processing, replying or draining, or that is still sending, is never closed."""
        slot = _Slot(self, request)
        victim: _Slot | None = None
        with self._slot_lock:
            if len(self._slots) >= MAX_CONNECTIONS:
                idle_before = time.monotonic() - EVICT_MIN_IDLE_SECONDS
                victim = next((s for s in self._slots if s.reading and s.last_progress <= idle_before), None)
                if victim is None:
                    return None
                victim.evicted = victim.released = True
                del self._slots[victim]
            self._slots[slot] = None
            self._pending[id(request)] = slot
        if victim is not None:
            with victim.sock_lock:  # the victim's own thread closes the socket under this same lock
                if not victim.sock_closed:
                    try:
                        victim.sock.shutdown(socket.SHUT_RDWR)  # wakes its blocked read; the thread then ends
                    except OSError:
                        pass
        return slot

    def shutdown_request(self, request: Any) -> None:
        """Close a connection. A handler thread closes its own socket under its slot's lock, so the server's
        shutdown of a closed-for-room connection can never run on a socket number that was already given back."""
        slot: _Slot | None = getattr(self._tls, "slot", None)
        if slot is None or slot.sock is not request:
            super().shutdown_request(request)
            return
        with slot.sock_lock:
            slot.sock_closed = True
            super().shutdown_request(request)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Two caps. Handler threads in total (MAX_HANDLER_THREADS), and connections being read (MAX_CONNECTIONS). No
        place and nothing to close, or no thread to spare: a complete 503 busy at once, with no thread and nothing
        stored. If the thread cannot start, the place is given back and the caller gets the same 503."""
        with self._slot_lock:
            full = self._handlers_alive >= MAX_HANDLER_THREADS  # only this (the accept) thread adds one, so no race
        slot = None if full else self._take_slot(request)
        if slot is None:
            self._refuse_busy(request)
            return
        with self._slot_lock:
            self._handlers_alive += 1
        try:
            super().process_request(request, client_address)
        except Exception as e:  # the thread did not start
            log.warning("cannot start a handler thread: %s", type(e).__name__)
            self._abandon(request, slot)
            self._thread_ended()
            self._refuse_busy(request)
        except BaseException:
            self._abandon(request, slot)
            self._thread_ended()
            raise

    def _thread_ended(self) -> None:
        with self._slot_lock:
            self._handlers_alive -= 1

    def _abandon(self, request: Any, slot: _Slot) -> None:
        with self._slot_lock:
            self._pending.pop(id(request), None)
        slot.release()

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        with self._slot_lock:
            slot = self._pending.pop(id(request), None)
        counted = slot is not None
        if slot is None:  # not reachable through process_request; stay safe and take no place
            slot = _Slot(self, request)
            slot.released = True
        self._tls.slot = slot
        try:
            super().process_request_thread(request, client_address)
        finally:
            slot.release()  # a no-op when the reply already gave the place back
            if counted:
                self._thread_ended()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """The standard library prints a traceback to stderr for every exception. A client that vanished is not a
        failure; anything else still gets the default report."""
        if isinstance(sys.exception(), _GONE):
            return
        super().handle_error(request, client_address)

    def _refuse_busy(self, request: Any) -> None:
        """A complete 503 for a connection that gets no thread. This runs on the accept loop, so it must stay short:
        the reply is written (it is a few hundred bytes) and the desk's side is closed for writing. The socket then goes
        to the lingerer, which reads and drops request bytes still on their way and closes it (a silent client costs
        the loop no time). Closing a socket that still receives bytes sends a reset, and a reset can destroy the reply
        (on Windows it always does). When the lingerer is full, the bytes that have already arrived are read at once,
        for at most QUIET_DRAIN_SECONDS in total, and the socket is closed."""
        body = json.dumps({"error": "busy", "reason": "busy"}).encode()
        head = (
            f"HTTP/1.0 {HTTP_BUSY} Service Unavailable\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\nRetry-After: {BUSY_RETRY_AFTER_SECONDS}\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in SECURITY_HEADERS)
            + "\r\n"
        ).encode()
        lingering = False
        try:
            request.settimeout(BUSY_WRITE_TIMEOUT_SECONDS)
            request.sendall(head + body)
            request.shutdown(socket.SHUT_WR)
            if self._linger.add(request):  # the lingerer reads what is still on its way and closes the socket
                lingering = True
                return
            end = time.monotonic() + QUIET_DRAIN_SECONDS
            left = DRAIN_MAX_BYTES
            while left > 0:
                budget = end - time.monotonic()
                if budget <= 0:
                    break
                request.setblocking(False)  # only what has already arrived; nothing readable means stop now
                try:
                    chunk = request.recv(65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:  # the client is gone, or went quiet: nothing more to do
            pass
        finally:
            if not lingering:
                self.shutdown_request(request)

    def get_request(self) -> tuple[socket.socket, Any]:
        """Accept one connection. When the operating system has no file handle for it (EMFILE, ENFILE) the connection
        stays in the listen queue, so the accept loop would be told "ready" again at once and spin at full speed. It
        pauses briefly instead (ACCEPT_PAUSE_SECONDS) and lets the error go on to the usual handling."""
        try:
            return super().get_request()
        except OSError as e:
            if is_fd_exhaustion(e):
                time.sleep(ACCEPT_PAUSE_SECONDS)
            raise

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _allowed_hosts(config: Config) -> frozenset[str]:
    """Host names the portal gate accepts: loopback, the bind address (unless it is a wildcard), and the extra
    names from DESK_ALLOWED_HOSTS. The port is not compared: rebinding attacks are about the name."""
    names = {*LOOPBACK_HOSTS, *config.allowed_hosts}
    if is_unspecified(config.bind):
        return frozenset(names)  # every interface: no single name belongs to the bind
    try:
        ip = ipaddress.ip_address(config.bind.strip("[]"))
    except ValueError:
        names.add(config.bind.lower())  # a host name
    else:
        if not ip.is_unspecified and str(ip) not in WILDCARD_BINDS:
            names.add(f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip))
    return frozenset(names)


_is_loopback_bind = is_loopback_host  # the one rule lives in config.py (the tests name it here)


def _route(method: str, path: str) -> tuple[str, str | None] | int:
    """(route name, path argument) for a known route and method; else 405 or 404 (a status int)."""
    parts = [unquote(p) for p in path.strip("/").split("/")] if path != "/" else []
    match parts:
        case ["leads"]:
            name, arg, allowed = "post_lead", None, "POST"
        case ["leads", ref] if ref:
            name, arg, allowed = "get_lead", ref, "GET"
        case ["review"]:
            name, arg, allowed = "review", None, "GET"
        case ["review", item, ("retry" | "resolve") as action] if item:
            name, arg, allowed = action, item, "POST"
        case ["leads", ref, "step"] if ref:
            name, arg, allowed = "lead_step", ref, "POST"
        case ["counts"]:
            name, arg, allowed = "counts", None, "GET"
        case ["ui"] if path == "/ui":
            name, arg, allowed = "ui_redirect", None, "GET"
        case ["ui", *rest]:
            name, arg, allowed = "ui_static", "/".join(rest) or "index.html", "GET"
        case ["api", "ui", *rest] if rest:
            name, arg, allowed = "ui_api", "/".join(rest), "GET"
        case ["api", "demo", *rest] if rest:
            return ("demo", "/".join(rest))  # any method; the extension answers 405 itself
        case _:
            return HTTP_NOT_FOUND
    if arg and name in ("retry", "resolve") and arg[1:].isascii() and arg[1:].isdecimal() and len(arg[1:]) > MAX_DIGITS:
        return HTTP_NOT_FOUND  # no item has an id this long; the database could not even take it as a number
    return (name, arg) if method == allowed else HTTP_METHOD_NOT_ALLOWED


class _Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_SECONDS
    server_version = "lead-desk"
    _follow: str | None = None

    _reader: _DeadlineReader
    _slot: _Slot

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self._slot = cast(_Server, self.server).current_slot()
        self._reader = _DeadlineReader(
            self.connection, lambda: self.timeout, lambda: self._slot.evicted, self._slot.touch
        )
        self.rfile = io.BufferedReader(self._reader)

    def handle_one_request(self) -> None:
        """Request line and headers must arrive within HEADER_READ_DEADLINE_SECONDS in total (parse_request then
        starts the body deadline). A stalled sender gets 408 if the reply can still be written, then the close."""
        self._reader.timed_out_in = ""
        self.requestline, self.request_version = "", "HTTP/0.9"  # a reply may precede parsing
        self.command = ""
        self._reader.arm("header", HEADER_READ_DEADLINE_SECONDS)
        super().handle_one_request()
        if self._reader.timed_out_in == "header":
            self._send_timeout("header")

    def parse_request(self) -> bool:
        try:
            parsed = super().parse_request()
        finally:
            evicted = self._slot.header_done()
            self._reader.arm("body", BODY_READ_DEADLINE_SECONDS)
        if (
            evicted
        ):  # closed to make room while the whole request may already sit in the desk's buffer: act on none of it
            raise ConnectionAbortedError("connection closed to make room")
        return parsed

    @property
    def _desk(self) -> Desk:
        return cast(_Server, self.server).desk

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None  # no request log: it would carry addresses

    # -- reply helpers
    def _send(self, status: int, obj: Any) -> None:
        self._send_bytes(status, json.dumps(obj, allow_nan=False).encode("utf-8"), "application/json")

    def _send_timeout(self, phase: str) -> None:
        """The one 408 for every stalled read (header, body, resolve body): the same JSON shape, `phase` in the log."""
        log.info("read timed out in the %s phase", phase)
        self.close_connection = True
        self._send(HTTP_REQUEST_TIMEOUT, {"error": "request_timeout", "receipt_id": None})

    def _send_bytes(self, status: int, body: bytes, content_type: str, extra: tuple[tuple[str, str], ...] = ()) -> None:
        """One writer for every reply: Content-Length, Connection: close and the fixed security headers. The
        connection's place under the cap ends here: the reading is over, whatever happens to the write."""
        self._slot.release()
        write_deadline = time.monotonic() + REPLY_WRITE_TIMEOUT_SECONDS  # one total limit for the whole reply
        try:
            self.connection.settimeout(REPLY_WRITE_TIMEOUT_SECONDS)  # not a short leftover from a deadline-limited read
        except OSError:
            pass
        if self.request_version == "HTTP/0.9":  # the standard library then sends the body alone, with no status line
            self.request_version = "HTTP/1.0"
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for key, value in (*SECURITY_HEADERS, *extra):
                self.send_header(key, value)
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                left = write_deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("reply write deadline")
                self.connection.settimeout(left)  # sendall's own limit is for the whole call: this is the rest
                self.wfile.write(body)
        except _GONE:  # the client left or stopped reading: nobody to answer, and not a handler failure
            pass
        except OSError as e:  # anything else is worth a line, then the thread ends cleanly
            log.warning("reply write failed: %s", type(e).__name__)
        self.close_connection = True

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """The standard library answers a malformed request line or an unknown method with an HTML page; answer in
        JSON like every other reply."""
        try:
            name = HTTPStatus(code).phrase.lower().replace(" ", "_")
        except ValueError:
            name = "error"
        self._send(code, {"error": name})

    def _flush_and_half_close(self) -> None:
        """The reply is complete for the client now; the follow-up must not keep it waiting."""
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _drain(self, remaining: int) -> None:
        """Read and drop request bytes the desk did not need, so closing does not reset the connection under the reply.
        Never read `rfile` after this: a timed-out read leaves the stream unusable (the reply is only written)."""
        self._reader.arm("drain", DRAIN_TOTAL_SECONDS)  # the same wall-clock limit the loop below keeps
        try:
            left = min(max(remaining, 0), DRAIN_MAX_BYTES)
            deadline = time.monotonic() + DRAIN_TOTAL_SECONDS
            while left > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    break
                self.connection.settimeout(budget)  # one wall-clock limit, not one wait per read
                chunk = self.rfile.read1(min(left, 65536))  # noqa: PLR2004
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:  # includes the timeout
            pass
        finally:
            self._reader.timed_out_in = ""  # a drain that ran out of time is not a stalled request
            try:
                self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            except OSError:
                pass

    def finish(self) -> None:
        """The one place every connection ends, whatever path answered it (a gate refusal, the standard library's own
        error replies, a 413). The reply is flushed and the write side closed, then request bytes the desk never read
        are dropped for a moment: closing a socket that still holds unread bytes sends a reset, and a reset can
        destroy a reply the client has not read yet."""
        self._flush_and_half_close()
        try:
            left = DRAIN_MAX_BYTES
            deadline = time.monotonic() + DRAIN_TOTAL_SECONDS
            while left > 0:
                budget = min(QUIET_DRAIN_SECONDS, deadline - time.monotonic())
                if budget <= 0:
                    break
                self.connection.settimeout(budget)
                chunk = self.connection.recv(min(left, 65536))  # noqa: PLR2004
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:  # includes the timeout: nothing more is arriving
            pass
        super().finish()

    def _content_length(self) -> int | None:
        """The body size, or None when the header is missing, not a plain number, or repeated with different values.
        A number longer than MAX_DIGITS (after leading zeros) is returned as 10**MAX_DIGITS: far over any cap, and
        never handed to int() at its real length."""
        values = {v.strip() for v in self.headers.get_all("Content-Length", [])}
        if len(values) != 1:
            return None
        raw = values.pop()
        if not (raw.isascii() and raw.isdecimal()):
            return None
        digits = raw.lstrip("0")
        return 10**MAX_DIGITS if len(digits) > MAX_DIGITS else int(digits or "0")

    # -- the Host and Origin gate
    def _gate_error(self) -> str | None:
        """None when the request may go on; else the error word of the 403. Runs for every route, before the path
        is read. Host is required and its name (port ignored) must be allowed: a page on another name that points at
        this address (DNS rebinding) is refused. A present Origin must be exactly one of this server's own origins;
        `null` is refused. A present Sec-Fetch-Site must be same-origin or none (a page on another local port, for
        example an n8n editor tab, sends same-site). Calls without these headers (n8n's server side, scripts) pass
        on Host alone."""
        server = cast(_Server, self.server)
        hosts = self.headers.get_all("Host", [])
        match = _HOST_HEADER.fullmatch(hosts[0].strip()) if len(hosts) == 1 else None
        if match is None or match.group("host").lower() not in server.allowed_hosts:
            return "forbidden_host"
        if match.group("port") is not None and int(match.group("port")) > PORT_MAX:
            return "forbidden_host"
        origins = self.headers.get_all("Origin", [])
        if origins:
            port = server.server_address[1]
            if len(origins) != 1 or origins[0] not in {f"http://{h}:{port}" for h in server.allowed_hosts}:
                return "forbidden_origin"
        fetch_site = self.headers.get_all("Sec-Fetch-Site", [])
        if fetch_site and (len(fetch_site) != 1 or fetch_site[0].strip().lower() not in SAME_SITE_VALUES):
            return "forbidden_origin"
        return None

    # -- dispatch
    def _dispatch(self, method: str) -> None:
        desk = self._desk
        self._follow = None
        try:
            denied = self._gate_error()
            if denied is not None:
                self._skip_body()
                self._send(HTTP_FORBIDDEN, {"error": denied})
                return
            try:
                parts = urlsplit(self.path)
            except ValueError:  # e.g. the target "http://[/"
                self._send(HTTP_BAD_REQUEST, {"error": "bad_request"})
                return
            routed = _route(method, parts.path)
            if isinstance(routed, int):
                self._skip_body()
                self._send(routed, {"error": "not_found" if routed == HTTP_NOT_FOUND else "method_not_allowed"})
                return
            name, arg = routed
            if name == "post_lead":
                self._post_lead()
            elif name == "get_lead":
                self._slot.start_work()
                self._send_reply(desk.lead(str(arg)))
            elif name == "review":
                self._slot.start_work()
                self._send_reply(desk.review())
            elif name == "counts":
                self._slot.start_work()
                self._counts(parts.query)
            elif name == "retry":
                self._skip_body()
                self._slot.start_work()  # the retry is a write: from here the connection is never closed for room
                self._send_reply(desk.retry(str(arg)))
            elif name.startswith("ui") or name == "demo":
                self._portal(name, str(arg), parts.query, method)
            elif name == "lead_step":
                self._step(str(arg))
            elif name == "resolve":
                self._resolve(str(arg))
            else:
                self._send(HTTP_NOT_FOUND, {"error": "not_found"})
            self._flush_and_half_close()
        except Exception as e:
            try:
                if isinstance(e, ConnectionAbortedError) and self._slot.evicted:
                    return  # closed to make room before anything was stored: no reply is possible, none is tried
                if (
                    isinstance(e, TimeoutError) and self._reader.timed_out_in
                ):  # the desk's own read limit, not a failure
                    self._send_timeout(self._reader.timed_out_in)
                else:
                    log.warning("handler failed: %s", type(e).__name__)
                    busy = db.is_busy(e)
                    self._send(HTTP_BUSY if busy else HTTP_INTERNAL_ERROR, {"error": "busy" if busy else "internal"})
                self._flush_and_half_close()
            except OSError:
                pass
        finally:
            follow, self._follow = self._follow, None
            if follow is not None:  # also when the reply could not be written: the desk has already committed
                try:
                    desk.run_followup(follow)
                except Exception as e:  # noqa: BLE001 (the reply is gone; the start-up sweep is the backstop)
                    log.warning("follow-up failed: %s", type(e).__name__)
            desk.close()  # this request thread's connection

    def _portal(self, name: str, arg: str, query: str, method: str) -> None:
        """The portal routes: the redirect, the fixed files, the read-only JSON and the demo extension."""
        server = cast(_Server, self.server)
        if name in ("ui_redirect", "ui_static", "ui_api"):
            self._slot.start_work()  # no request body is read for these
        if name == "ui_redirect":
            self._send_bytes(HTTP_PERMANENT_REDIRECT, b"", "text/plain", (("Location", "/ui/"),))
        elif name == "ui_static":
            if arg.startswith("demo/"):
                asset = server.demo.assets.get(arg[len("demo/") :]) if server.demo is not None else None
            else:
                asset = portal_api.STATIC.get(arg)
            if asset is None:
                self._send(HTTP_NOT_FOUND, {"error": "not_found"})
            else:
                self._send_bytes(HTTP_OK, asset[0], asset[1])
        elif name == "ui_api":
            self._send(
                *portal_api.handle(self._desk, arg, query, server.started_at, demo_controls=server.demo is not None)
            )
        elif server.demo is None:
            self._skip_body()
            self._send(HTTP_NOT_FOUND, {"error": "not_found"})
        elif method not in DEMO_METHODS:
            self._skip_body()
            self._send(HTTP_METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
        else:
            body = b""
            if method == "POST":
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    self._skip_body()
                    self._send(HTTP_BAD_REQUEST, {"error": "bad_body"})
                    return
                n = self._content_length() or 0
                if n > DEMO_BODY_MAX:
                    self._send(HTTP_TOO_LARGE, {"error": "too_large"})
                    self._flush_and_half_close()
                    self._drain(n)
                    return
                try:
                    body = self.rfile.read(n)
                except TimeoutError:  # fewer bytes than announced and the socket stayed open
                    self._send_timeout("demo body")
                    return
                if len(body) < n:
                    self._send(HTTP_BAD_REQUEST, {"error": "bad_body"})
                    return
            else:
                self._skip_body()
            self._slot.start_work()  # the whole request is read: the extension may act on it
            self._send(*server.demo.handle(method, arg, body))

    def _send_reply(self, reply: Reply) -> None:
        self._send(reply.status, reply.body)

    def _skip_body(self) -> None:
        """Drop the request body of an early reply. The desk answers as HTTP/1.0 and never sends `100 Continue`, so
        a client that sent `Expect: 100-continue` has not sent its body yet: waiting for it would hold the thread."""
        if self.headers.get("Expect", "").strip().lower() == "100-continue":
            return
        n = self._content_length()
        if n:
            self._drain(n)

    def _counts(self, query: str) -> None:
        q = parse_qs(query, keep_blank_values=True)
        expected: int | None = None
        if "expected" in q:
            raw = q["expected"][0]
            if not (raw.isascii() and raw.isdecimal()):  # empty, not an integer, or negative
                self._send(HTTP_BAD_REQUEST, {"error": "bad_expected"})
                return
            expected = int(raw)
        self._send_reply(self._desk.counts(expected))

    def _resolve(self, item_id: str) -> None:
        n = self._content_length() or 0
        if n > self._desk.config.size_cap_bytes:  # the same cap as POST /leads: answer before reading any body byte
            self._send(HTTP_TOO_LARGE, {"error": "too_large"})
            self._flush_and_half_close()
            self._drain(n)
            return
        try:
            raw = self.rfile.read(n) if n else b""
        except TimeoutError:  # fewer bytes than announced and the socket stayed open
            self._send_timeout("resolve body")
            return
        decision: Any = None
        try:
            obj = json.loads(raw.decode("utf-8"))
            decision = obj.get("decision") if isinstance(obj, dict) else None
        except (ValueError, RecursionError):
            decision = None
        if not isinstance(decision, str):  # before any lookup: even an unknown item id gives 400 here
            self._send(HTTP_BAD_REQUEST, {"error": "bad_decision"})
            return
        self._slot.start_work()  # the resolve is a write: from here the connection is never closed for room
        self._send_reply(self._desk.resolve(item_id, decision))

    def _step(self, lead_id: str) -> None:
        """POST /leads/<id>/step: body read like _resolve; a bad body gives step None (the desk answers 400)."""
        n = self._content_length() or 0
        if n > self._desk.config.size_cap_bytes:
            self._send(HTTP_TOO_LARGE, {"error": "too_large"})
            return
        try:
            raw = self.rfile.read(n) if n else b""
        except TimeoutError:
            self._send_timeout("step body")
            return
        value: Any = None
        try:
            obj = json.loads(raw.decode("utf-8"))
            value = obj.get("step") if isinstance(obj, dict) else None
        except (ValueError, RecursionError):
            value = None
        self._slot.start_work()  # a write: from here the connection is never closed for room
        self._send_reply(self._desk.set_step(lead_id, value))

    def _reject_envelope(self, status: int, error: str) -> None:
        self._desk.note_rejected_envelope()
        self._send(status, {"error": error, "receipt_id": None})

    def _post_lead(self) -> None:
        """Envelope check (no database), then desk.process. Sets self._follow to the lead id whose follow-up
        _dispatch runs; it is set BEFORE the reply is written, so a failed write cannot skip it."""
        desk, cap = self._desk, self._desk.config.size_cap_bytes
        n = self._content_length()
        if n is None:
            self._reject_envelope(HTTP_BAD_REQUEST, "bad_envelope")
            return
        if n > cap:  # answer at once: the client may never send the body it announced
            self._reject_envelope(HTTP_TOO_LARGE, "too_large")
            self._flush_and_half_close()
            self._drain(n)
            return
        try:
            raw = self.rfile.read(n)
        except TimeoutError:  # fewer bytes than announced and the socket stayed open
            self._desk.note_rejected_envelope()
            self._send_timeout("body")
            return
        if len(raw) < n:  # the client closed early
            self._reject_envelope(HTTP_BAD_REQUEST, "bad_envelope")
            return
        try:
            env = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # not JSON, or nested so deeply that the parser gives up
            env = None
        ref = env.get("source_ref") if isinstance(env, dict) else None
        if not (
            isinstance(env, dict)
            and env.get("source") in SOURCES
            and isinstance(ref, str)
            and SOURCE_REF_MIN <= len(ref) <= SOURCE_REF_MAX
            and isinstance(env.get("payload"), dict)
        ):
            self._reject_envelope(HTTP_BAD_REQUEST, "bad_envelope")
            return
        self._slot.start_work()  # the whole request is read; the receipt is written only after this point
        reply = desk.process(env["source"], ref, env["payload"])
        self._follow = reply.drive_lead_id
        self._send_reply(reply)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")


def make_server(desk: Desk, config: Config) -> ThreadingHTTPServer:
    """ThreadingHTTPServer subclass: allow_reuse_address = (os.name != "nt"), SO_EXCLUSIVEADDRUSE set in
    server_bind on Windows (`sys.platform == "win32"`), request_queue_size = constants.LISTEN_QUEUE.

    Handler order per request: read bytes and enforce the cap, parse JSON and the envelope, call desk.process,
    write the reply with Content-Length and Connection: close, then wfile.flush() and
    connection.shutdown(SHUT_WR) BEFORE desk.run_followup, so a client that reads until close is not kept waiting
    for the follow-up. For POST /review/{id}/resolve the handler parses the body and answers 400 bad_decision
    BEFORE any item lookup when the body is not JSON or `decision` is missing or not a string.

    Windows: a restart on the same fixed port may fail while the old connections sit in TIME_WAIT, and the
    conflicting bind may raise PermissionError instead of EADDRINUSE; both are OSError. `main` retries the bind
    for BIND_RETRY_SECONDS before it gives up."""
    demo = load_demo_portal(desk)  # before the bind: a broken extension must not leave a bound socket behind
    server = _Server((config.bind, config.port), _Handler)
    server.desk, server.config = desk, config
    server.started_at = desk.now()  # the desk's clock, like every other time the portal shows
    server.allowed_hosts = _allowed_hosts(config)
    server.demo = demo
    return server


def serve_in_thread(desk: Desk, port: int = 0) -> tuple[str, Callable[[], None]]:
    """Returns (base_url, stop). Does NOT call desk.startup(). A bind failure raises OSError (any subclass). Only
    `main` refuses a public bind (`Config.public_bind_problem`); a program that builds a Desk itself owns its bind."""
    cfg = replace(desk.config, port=port)
    server = make_server(desk, cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=REQUEST_TIMEOUT_SECONDS)

    host, bound = server.server_address[0], server.server_address[1]
    return f"http://{host!s}:{bound}", stop


class DescriptorBudgetError(ValueError):
    """MAX_HANDLER_THREADS does not fit the open-file limit of this computer."""


def ensure_descriptor_budget(max_threads: int = MAX_HANDLER_THREADS, res: Any = None) -> None:
    """Check at start-up that the open-file limit (RLIMIT_NOFILE) covers MAX_HANDLER_THREADS handler threads at
    FDS_PER_HANDLER_THREAD files each, plus FD_RESERVE. If the soft limit is too low but the hard limit is enough, the
    soft limit is raised (logged). Otherwise raises DescriptorBudgetError naming the setting, so the desk refuses to
    start instead of failing later with "too many open files" on accept. Does nothing where there is no such limit."""
    validate_descriptor_budget(FDS_PER_HANDLER_THREAD, FD_RESERVE, FD_RESERVE_ITEMS, PORTAL_MAX_INFLIGHT)
    res = resource if res is None else res
    if res is None:  # Windows: no such limit to check
        return
    need = FDS_PER_HANDLER_THREAD * max_threads + FD_RESERVE
    soft, hard = res.getrlimit(res.RLIMIT_NOFILE)
    if soft == res.RLIM_INFINITY or soft >= need:
        return
    if hard == res.RLIM_INFINITY or hard >= need:
        try:
            res.setrlimit(res.RLIMIT_NOFILE, (need, hard))
        except (OSError, ValueError):
            pass
        else:
            log.info("open-file limit raised from %d to %d for MAX_HANDLER_THREADS=%d", soft, need, max_threads)
            return
    advice = f"Raise the open-file limit (ulimit -n) to at least {need}."
    raise DescriptorBudgetError(
        f"MAX_HANDLER_THREADS={max_threads} needs about {need} open files ({FDS_PER_HANDLER_THREAD} per thread plus "
        f"{FD_RESERVE}), but the limit on this computer is {soft} (hard {hard}). {advice}"
    )


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    """SIGTERM stops the desk the same way Ctrl+C does: serve_forever ends, the reaper is stopped, the lock is given
    back."""
    raise KeyboardInterrupt


def _make_stop_handler() -> Callable[[int, FrameType | None], None]:
    """The handler main() installs for SIGTERM and SIGINT. The first signal stops the desk like Ctrl+C (it raises
    KeyboardInterrupt); every later one does nothing, so a second signal that arrives while the shutdown steps run
    (the reaper, the socket, the database, the lock file) cannot cut them short. This is deliberate. If a shutdown step
    blocks (the reaper join waits up to 10 s), a second Ctrl+C or SIGTERM does nothing; the operator waits those 10 s,
    then sends SIGKILL (the operating system drops the lock file lock, and the next start repairs every row). A signal
    that arrives after the lock file is taken and before this handler is installed is not caught: the process ends at
    once."""
    fired = threading.Event()

    def handler(signum: int, frame: FrameType | None) -> None:
        if fired.is_set():
            return
        fired.set()
        _on_sigterm(signum, frame)

    return handler


def main(hooks: Hooks | None = None) -> None:
    """python -m lead_desk.server: open-file check, lock file, adapters, desk, bind, reaper, startup (schema and
    sweep), serve. Exit code 2 if another desk holds the lock; exit code 5 if the port cannot be bound within
    BIND_RETRY_SECONDS (a restart on Windows can meet the old port in TIME_WAIT, so any OSError is retried first);
    exit code 1 if the sender cannot be reached; exit code 3 if MAX_HANDLER_THREADS does not fit the open-file limit;
    exit code 4 if the desk refuses a setting: a bad value in any DESK_* name (a `SettingRefused` from `Config.from_env`
    or from the adapter settings, printed with the setting's name; any other ValueError is a bug and is not
    turned into 4), or an AI adapter that declares a `timeout_seconds` that
    is not a number above 0 or is above MODEL_CALL_BUDGET_SECONDS (`DeskSettingRefused`): the message is printed,
    nothing is served. The checks of the constants themselves run when the modules are imported,
    so an edited constant that does not fit ends with a Python traceback (exit code 1) before `main` runs.
    SIGTERM stops it like Ctrl+C: the reaper is stopped, then the lock is given back. `hooks` goes to the Desk
    (the crash launcher and scripts/crash_desk.py pass `FlagFileHooks(path)`)."""
    try:
        ensure_descriptor_budget()
    except DescriptorBudgetError as e:
        print(str(e), flush=True)
        sys.exit(EXIT_SETTING_REFUSED)
    try:
        config = Config.from_env()
    except SettingRefused as e:  # a bad setting value: a plain refusal, not a traceback (any other error is a bug)
        print(f"the desk refuses a setting: {e}", flush=True)
        sys.exit(EXIT_DESK_SETTING_REFUSED)
    if (bind_problem := config.public_bind_problem()) is not None:  # no sign-in: a real service stays on this computer
        print(f"the desk refuses a setting: {bind_problem}", flush=True)
        sys.exit(EXIT_DESK_SETTING_REFUSED)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = lockfile.acquire(config.db_path)
    except lockfile.LockHeldError:
        print("another desk is using this database", flush=True)
        sys.exit(EXIT_ANOTHER_DESK)
    except OSError as e:  # the lock file cannot be used for another reason (a folder, no permission)
        print(f"cannot take the lock file: {type(e).__name__}", flush=True)
        sys.exit(EXIT_ANOTHER_DESK)
    if config.later_mode == "approval":
        try:
            approvers, label = load_approvers(config)
            config = dataclasses.replace(config, approvers=approvers, approvers_label=label)
        except SettingRefused as e:  # a bad approvers file: named, no traceback; the lock is released at exit
            print(f"the desk refuses a setting: {e}", flush=True)
            sys.exit(EXIT_DESK_SETTING_REFUSED)
    if config.leadflow_path is not None:  # lead owner, lead stage and reminder: the file is read here, once
        try:
            lf = read_leadflow(config.leadflow_path)
            config = dataclasses.replace(config, leadflow=lf)  # runs Config.__post_init__ again
            if (problem := config.reminder_setting_problem()) is not None:  # the one rule lives in Config
                raise SettingRefused(problem)
        except SettingRefused as e:  # a bad leadflow file or setting: named, no value, no traceback
            print(f"the desk refuses a setting: {e}", flush=True)
            sys.exit(EXIT_DESK_SETTING_REFUSED)
    try:
        crm, outbox, qualifier = load_demo_adapters(config)
    except FakesUnreachable:
        print("cannot reach the demo servers (are they running? see scripts/start_all)", flush=True)
        sys.exit(EXIT_ADAPTERS_UNREACHABLE)
    except SettingRefused as e:  # a bad adapter setting (DESK_SMTP*, DESK_HUBSPOT*, DESK_LIVE*): named, no traceback
        print(f"the desk refuses a setting: {e}", flush=True)
        sys.exit(EXIT_DESK_SETTING_REFUSED)
    try:
        desk = Desk(config, crm, outbox, qualifier, hooks=hooks)
    except DeskSettingRefused as e:  # an AI adapter whose time limit does not fit the stuck grace (no other error)
        print(f"the desk refuses to start: {e}", flush=True)
        sys.exit(EXIT_DESK_SETTING_REFUSED)
    bind_deadline = time.monotonic() + BIND_RETRY_SECONDS
    while True:
        try:
            server = make_server(desk, config)
            break
        except OSError as e:
            if time.monotonic() >= bind_deadline:
                print(f"cannot bind {config.bind}:{config.port}: {type(e).__name__}", flush=True)
                sys.exit(EXIT_PORT_UNAVAILABLE)
            time.sleep(BIND_RETRY_STEP_SECONDS)
    try:
        stop = _make_stop_handler()  # installed BEFORE the listening line: a signal sent once it is read always counts
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, stop)
            except ValueError:  # not the main thread: no handler, Ctrl+C handling is unchanged
                pass
        desk.start_background()  # before startup(): the reaper then drives due follow-ups, retries and repairs
        desk.startup()
        if not _is_loopback_bind(config.bind):
            print(
                f"warning: the desk is bound to {config.bind}, which other computers can reach. The portal shows every "
                "lead (names, email addresses, phone numbers, messages) to anyone who can connect; the Host and "
                "Origin check stops browsers on other sites only. Bind to 127.0.0.1, or to the one address that only "
                "your container network reaches.",
                file=sys.stderr,
                flush=True,
            )
        host, port = server.server_address[0], server.server_address[1]
        print(f"desk listening on http://{host!s}:{port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except DeskSettingRefused as e:  # the steps file does not fit the kept lead book; the finally below still cleans up
        print(f"the desk refuses to start: {e}", flush=True)
        sys.exit(EXIT_DESK_SETTING_REFUSED)
    finally:
        # every step runs even if an earlier one raises: the socket, the database and the lock are always released
        try:
            try:
                desk.stop_background()
            except Exception as e:
                log.warning("the reaper could not be stopped cleanly: %s", type(e).__name__)
            if desk.reaper_running():  # the lock is given back all the same; say so
                log.warning(
                    "the reaper did not end in time; the lock file is released while its last pass may still run"
                )
        finally:
            try:
                server.server_close()
            finally:
                try:
                    desk.close()
                finally:
                    lock.close()


if __name__ == "__main__":
    main()
