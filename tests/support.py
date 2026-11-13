"""Shared test helpers. Made-up data only; hosts and ports come from Config; no secrets."""

from __future__ import annotations

import faulthandler
import itertools
import json
import os
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, cast

from demo_only.fakes import BlindOutbox, FakeCrm, KeyedOutbox, RecordedQualifier
from lead_desk.adapters import CrmNothingStored
from lead_desk.config import Config, read_leadflow
from lead_desk.contract import SimulatedCrash
from lead_desk.desk import Desk
from lead_desk.hooks import Hooks

REPO = Path(__file__).resolve().parents[1]
WAIT_SECONDS = 15.0
HOST = Config().bind  # the one source of the loopback host for tests
# the demo lead owner list, steps and reminder wait, read once; a test changes a copy.deepcopy, never this original
DEMO_LEADFLOW: Final[dict[str, Any]] = json.loads((REPO / "demo_only" / "leadflow.json").read_text(encoding="utf-8"))


def lead_ids_containing(monkeypatch: Any, fragment: str) -> None:
    """Every lead number made from now on holds `fragment` in its random hex part (a random draw can do this by
    chance: "L" and 12 hex characters can spell ada, bad, face...). Lets a test prove that a probe made of hex letters
    never decides its result by chance. Only the 6-byte token of a lead number is changed."""
    real, counter = secrets.token_hex, itertools.count()

    def forced(nbytes: int | None = None) -> str:
        if nbytes != 6:  # LEAD_ID_HEX_CHARS // 2: the lead number
            return real(nbytes)
        return fragment + f"{next(counter):0{12 - len(fragment)}d}"  # unique: the counter fills the rest

    monkeypatch.setattr(secrets, "token_hex", forced)


NUDGE_TO_TEST: Final = "reminder@example.test"  # the one reminder address of the tests

LEAD = {
    "name": "Ada Example",
    "email": "Ada@Example.test",
    "phone": "555 0100",
    "company": "Example Ltd",
    "message": "Please call me.",
}


def lead(**over: Any) -> dict[str, Any]:
    """A valid payload; a keyword set to None removes that field."""
    d: dict[str, Any] = {**LEAD, **over}
    return {k: v for k, v in d.items() if v is not None}


def child_env(**extra: str) -> dict[str, str]:
    """Environment for a child process: PYTHONPATH = src and the repo root (pytest's pythonpath is not inherited)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(REPO)])
    env.update(extra)
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def sql_exec(db_path: Path, sql: str, args: tuple[Any, ...] = ()) -> None:
    """Run one statement on the lead book through a separate connection and commit (a test's own write)."""
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def sql(rig: Rig, text: str, args: tuple[Any, ...] = ()) -> None:
    sql_exec(rig.db_path, text, args)


def reset_on_close(sock: socket.socket) -> None:
    """Make closing `sock` send a reset (SO_LINGER on, zero seconds) instead of a normal close."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def wait_until(fn: Callable[[], Any], timeout: float = WAIT_SECONDS, what: str = "condition") -> Any:
    """Bounded poll. Returns the first truthy value of fn(); raises AssertionError after the timeout."""
    end = time.monotonic() + timeout
    while True:
        try:
            val = fn()
        except (OSError, ValueError):
            val = None
        if val:
            return val
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


# -- clock and hooks
class Clock:
    def __init__(self, start: int = 1_700_000_000) -> None:
        self.t = start

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += seconds


MAX_FAKE_SLEEPS = 5000  # a loop on the fake clock that sleeps more often than this is stuck
MIN_FAKE_STEP = 1e-6  # seconds a fake sleep always advances the clock


class FakeClock:
    """time.monotonic and time.sleep for the module under test: sleeping moves the clock, nothing really waits."""

    def __init__(self, monkeypatch: Any) -> None:  # a pytest MonkeyPatch
        self.now = 1000.0
        self.sleeps: list[float] = []
        monkeypatch.setattr(time, "monotonic", lambda: self.now)
        monkeypatch.setattr(time, "sleep", self._sleep)

    def _sleep(self, seconds: float) -> None:
        # A sleep always moves the clock by a visible step: a wait of 1e-13 added to 1030.0 changes nothing in a
        # float, and a retry loop whose `left` never reaches 0 then never ends. The cap on
        # the list makes such a loop fail loudly instead.
        assert len(self.sleeps) < MAX_FAKE_SLEEPS, "the loop under test never ends"
        self.sleeps.append(seconds)
        self.now += max(seconds, MIN_FAKE_STEP)


class LiveClock(Clock):
    """Whole seconds of real time. For a desk that runs the reaper: the reaper drives a row only when the clock has
    passed the row's wait, so a frozen Clock would leave every row the request's own drive gave back for ever."""

    def __init__(self) -> None:
        super().__init__(int(time.time()))  # wall-clock: the desk's own calendar stamps

    def __call__(self) -> int:
        return int(time.time())  # wall-clock: the desk's own calendar stamps


class CrashAt(Hooks):
    """Raise SimulatedCrash once at each named point (one-shot per point). An open transaction is rolled back first,
    as a kill would."""

    def __init__(self, *points: str) -> None:
        self.points = set(points)
        self.fired: list[str] = []

    def crash_point(self, name: str, conn: sqlite3.Connection | None) -> None:
        if name in self.points and name not in self.fired:
            self.fired.append(name)
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise SimulatedCrash(name)


class BlockAt(Hooks):
    """Block once at `point` until release is set (holds a receipt open, or a follow-up in `sending`)."""

    def __init__(self, point: str) -> None:
        self.point = point
        self.reached = threading.Event()
        self.release = threading.Event()
        self._used = False

    def crash_point(self, name: str, conn: sqlite3.Connection | None) -> None:
        if name == self.point and not self._used:
            self._used = True
            self.reached.set()
            self.release.wait(timeout=WAIT_SECONDS)


class CountingCrm:
    """An in-process CRM that tells a double put apart from a harmless repeat. capability "keyed": a second put with
    the same lead id is ALREADY_PRESENT and stores nothing. "blind": it cannot tell, so EVERY put stores one more
    row. `calls` is the log of every put (lead id, record); `rows` the stored ones, so a double put on either kind
    shows as len(rows) == 2 (blind) or len(calls) > the allowed number (keyed)."""

    def __init__(self, mode: str = "keyed") -> None:
        if mode not in ("keyed", "blind"):
            raise ValueError(mode)
        self.capability = mode
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.rows: list[tuple[str, dict[str, str]]] = []
        # every set_fields call (lead id, email key, CRM owner id, CRM status); kept apart from `calls` because tests
        # assert on the number of puts there
        self.set_fields_calls: list[tuple[str, str, str, str]] = []
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return len(self.rows)

    def put(self, lead_id: str, record: Any) -> str:
        with self._lock:
            self.calls.append((lead_id, dict(record)))
            if self.capability == "keyed" and any(k == lead_id for k, _ in self.rows):
                return "ALREADY_PRESENT"
            self.rows.append((lead_id, dict(record)))
            return "PUT"

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        """Logs into set_fields_calls only; a lead with no stored put has no contact (as FakeCrm and HubSpot)."""
        with self._lock:
            self.set_fields_calls.append((lead_id, email_key, crm_owner_id, crm_status))
            if not any(k == lead_id for k, _ in self.rows):
                raise CrmNothingStored("no contact")


# -- desk builder
@dataclass
class Rig:
    desk: Desk
    config: Config
    crm: Any
    outbox: Any
    qualifier: RecordedQualifier
    clock: Clock
    db_path: Path
    replies_path: Path
    desks: list[Desk] = field(default_factory=list)

    def query(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return rows(self.db_path, sql, args)

    def leads(self) -> int:
        return int(self.query("SELECT count(*) FROM leads")[0][0])

    def state(self, lead_id: str | None = None) -> tuple[str, str | None]:
        sql = "SELECT state, last_error FROM followups" + (" WHERE lead_id=?" if lead_id else "")  # noqa: S608
        r = self.query(sql, (lead_id,) if lead_id else ())
        assert len(r) == 1, f"expected one follow-up row, got {len(r)}"
        return r[0][0], r[0][1]

    def restart(self, hooks: Hooks | None = None) -> Desk:
        """A new Desk on the same lead book and the same fakes, then startup() (the sweep). The old one is closed."""
        try:
            self.desk.close()
        except Exception:  # noqa: BLE001, S110 (a crashed desk may fail to close; the file is what matters)
            pass
        self.desk = Desk(
            self.config, self.crm, self.outbox, self.qualifier, hooks=hooks, now=self.clock, schema_sql=None
        )
        self.desks.append(self.desk)
        self.desk.startup()
        return self.desk


def rows(db_path: Path, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(db_path, timeout=10)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def build_rig(
    tmp_path: Path,
    outbox: str | Any = "keyed",
    hooks: Hooks | None = None,
    schema_sql: str | None = None,
    replies: dict[str, str] | None = None,
    start: bool = True,
    clock: Clock | None = None,
    crm: Any = None,
    leadflow: Mapping[str, Any] | None = None,
    outbox_kind: str | None = None,
    crm_kind: str | None = None,
    **config_over: Any,
) -> Rig:
    """Config on tmp_path, in-process fakes, a Desk (started unless start=False). `leadflow` (a dict shaped like the
    leadflow file, for example a deepcopy of DEMO_LEADFLOW) turns the lead owner, lead stage and reminder feature
    on: it is written to tmp_path and read back by the real parser, so a test can never build a Leadflow the desk
    would refuse. None (the default) leaves the feature off."""
    if outbox_kind is not None:  # the sender KIND the Config names (`outbox` is the sender object)
        config_over["outbox"] = outbox_kind
    if crm_kind is not None:  # the CRM KIND the Config names (`crm` is the CRM object)
        config_over["crm"] = crm_kind
    replies_path = tmp_path / "replies.json"
    replies_path.write_text(json.dumps(replies or {}), encoding="utf-8")
    if leadflow is not None:
        flow_path = tmp_path / "leadflow.json"
        flow_path.write_text(json.dumps(leadflow), encoding="utf-8")
        lf = read_leadflow(flow_path)
        config_over.setdefault("leadflow_path", flow_path)
        config_over["leadflow"] = lf
        config_over.setdefault("nudge_to", NUDGE_TO_TEST if lf.nudge_after_seconds is not None else "")
    config = Config(db_path=tmp_path / "desk.sqlite3", port=0, replies_path=replies_path, **config_over)
    ob = {"keyed": KeyedOutbox, "blind": BlindOutbox}[outbox]() if isinstance(outbox, str) else outbox
    crm, qual, clock = crm or FakeCrm(), RecordedQualifier(replies_path), clock or Clock()
    desk = Desk(config, crm, ob, qual, hooks=hooks, now=clock, schema_sql=schema_sql)
    rig = Rig(desk, config, crm, ob, qual, clock, config.db_path, replies_path, [desk])
    if start:
        desk.startup()
    return rig


# -- HTTP helpers
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(base: str, method: str, path: str, body: Any = None, raw: bytes | None = None, timeout: float = 30.0) -> Any:
    """Returns (status, parsed JSON). `body` is JSON-encoded; `raw` is sent as is."""
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method=method)  # noqa: S310 (loopback test URL)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=timeout) as r:  # noqa: S310
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def post_lead(base: str, source: str, ref: str, payload: Any) -> tuple[int, dict[str, Any]]:
    return call(base, "POST", "/leads", {"source": source, "source_ref": ref, "payload": payload})  # type: ignore[no-any-return]


def addr(base: str) -> tuple[str, int]:
    """(host, port) of a base URL such as http://127.0.0.1:8080."""
    host, port = base.removeprefix("http://").split(":")
    return host, int(port)


def raw_http(base: str, request_bytes: bytes, timeout: float = 10.0) -> bytes:
    """Send raw bytes on a socket and read until the server ends the connection. A reset after some bytes arrived (the
    server closed with unread request bytes, as it does for an oversize body) ends the read: what came is returned."""
    chunks: list[bytes] = []
    with socket.create_connection(addr(base), timeout=timeout) as s:
        try:
            s.sendall(request_bytes)
        except (BrokenPipeError, ConnectionResetError):  # the server already answered and closed
            pass
        try:
            while chunk := s.recv(65536):
                chunks.append(chunk)
        except ConnectionResetError:
            pass
    return b"".join(chunks)


def raw_exchange(base: str, request_bytes: bytes, timeout: float = 10.0) -> tuple[int, Any]:
    """Send raw bytes on a socket, read to the end of the connection, return (status, JSON body)."""
    data = raw_http(base, request_bytes, timeout)
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, json.loads(body.decode() or "null")


def run_threads(fns: list[Callable[[], Any]]) -> list[Any]:
    """Run each fn in its own thread behind one start barrier. Returns results (or the exception object) in order."""
    barrier = threading.Barrier(len(fns))
    out: list[Any] = [None] * len(fns)

    def wrap(i: int) -> None:
        barrier.wait()
        try:
            out[i] = fns[i]()
        except BaseException as e:  # noqa: BLE001 (collected and returned to the caller)
            out[i] = e

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(len(fns))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    return out


# -- a tiny stand-in sender with scripted misbehaviour (for test_error_mapping)
class StubSender:
    """Serves GET /fake/info normally; POST /fake/outbox does what `behaviour` says."""

    def __init__(self, behaviour: str, status: int = 200, body: bytes = b"{}") -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

            def _json(self, status_: int, raw: bytes) -> None:
                self.send_response(status_)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                self._json(200, json.dumps({"outbox_capability": "keyed", "mode": "keyed"}).encode())

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if outer.behaviour == "status":
                    return self._json(outer.status, outer.body)
                if outer.behaviour == "reset":  # TCP reset instead of a reply
                    reset_on_close(self.connection)
                    self.close_connection = True
                    self.connection.close()
                    return None
                if outer.behaviour == "garbage":
                    return self._json(200, b"this is not json")
                return None

        self.behaviour, self.status, self.body = behaviour, status, body
        self.srv = ThreadingHTTPServer((HOST, 0), H)
        self.srv.daemon_threads = True
        self.thread = start_serving(self.srv)
        self.url = f"http://{HOST}:{self.srv.server_address[1]}"

    def stop(self) -> None:
        stop_serving(self.srv, self.thread, "stub server")


# A plain stub server stops in milliseconds, so 20 s is far beyond any real stop: a server still running then is
# stuck, and the guard's job is to catch a wait with NO end, not a slow one. A stop of a PRODUCT server (the desk's
# serve_in_thread.stop) is allowed longer: it ends with thread.join(timeout=REQUEST_TIMEOUT_SECONDS) (30 s), so a
# legitimate stop may last that long; conftest.py gives those stops twice that value.
STOP_SECONDS = 20.0
_POLL_SECONDS = 0.05  # serve_forever polls for a stop request this often (the library default is 0.5 s)


class ServerDidNotStop(AssertionError):
    """A test server did not stop in time (a deadlock in the server or a stuck request thread)."""


def start_serving(srv: Any, *, ready_within: float = WAIT_SECONDS) -> threading.Thread:
    """Run srv.serve_forever in a daemon thread and return only once its loop has really started.

    A stop with no time limit (the standard library's shutdown()) can stall a whole test run; this helper and
    stop_serving() bound it and print the stacks of all threads. The cause of the one suspected stall in an earlier
    full run is UNPROVEN (that run was most probably only slow, or its buffered progress file looked stale): this is
    a diagnostic guard, not a fix of a known cause. The loop calls service_actions() after every poll, so the first
    call proves it is serving. If the loop does not start, the listening socket is closed before the error is raised,
    so a failed start leaks no socket."""
    serving = threading.Event()
    inner = srv.service_actions

    def service_actions() -> None:
        serving.set()
        inner()

    srv.service_actions = service_actions
    thread = threading.Thread(target=srv.serve_forever, args=(_POLL_SECONDS,), daemon=True, name="test-serve")
    srv.serve_thread = thread
    thread.start()
    if not serving.wait(ready_within):
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001, S110 (the start error below is the one to report)
            pass
        raise ServerDidNotStop(f"the server loop did not start within {ready_within} s (alive={thread.is_alive()})")
    return thread


def _dump_all_stacks() -> None:
    """Print the stack of every thread. Under `--capture=sys` or capsys, sys.stderr has no file number and
    faulthandler refuses it: fall back to the process's own stderr, and never let the dump hide ServerDidNotStop."""
    for target in (sys.stderr, sys.__stderr__):
        try:
            faulthandler.dump_traceback(file=cast(Any, target), all_threads=True)
            return
        except Exception:  # noqa: BLE001, S112 (try the next target)
            continue


def stop_within(stop: Callable[[], object], what: str = "server", *, seconds: float = STOP_SECONDS) -> None:
    """Call stop() (a shutdown, a server_close, a join) on a helper thread and fail with a message, and with the
    stack of every thread on stderr, when it has not returned after `seconds`; the test run goes on."""
    done = threading.Event()
    problem: list[BaseException] = []

    def run() -> None:
        try:
            stop()
        except BaseException as e:  # noqa: BLE001 (handed to the calling thread below)
            problem.append(e)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name=f"stop-{what}").start()
    if not done.wait(seconds):
        _dump_all_stacks()
        raise ServerDidNotStop(f"{what} did not stop within {seconds} s")
    if problem:
        raise problem[0]


def stop_serving(
    srv: Any, thread: threading.Thread | None = None, what: str = "server", *, seconds: float = STOP_SECONDS
) -> None:
    """Stop a server started with start_serving (or any serve_forever thread): shutdown, close, join, all bounded."""
    thread = thread or srv.serve_thread

    def stop() -> None:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=seconds)
        if thread.is_alive():
            raise ServerDidNotStop(f"{what}: the serve_forever thread is still alive after the shutdown")

    stop_within(stop, what, seconds=seconds)


def stop_all(servers: Iterable[Any], what: str = "server", *, seconds: float = STOP_SECONDS) -> None:
    """Stop every server in `servers` even when an earlier stop fails; then raise: several stuck servers as one
    ServerDidNotStop that names them all, otherwise the first error as it is."""
    problems: list[BaseException] = []
    for srv in servers:
        try:
            stop_serving(srv, what=what, seconds=seconds)
        except Exception as e:  # noqa: BLE001 (collected, raised below)
            problems.append(e)
    if problems and all(isinstance(p, ServerDidNotStop) for p in problems) and len(problems) > 1:
        raise ServerDidNotStop("; ".join(str(p) for p in problems))
    if problems:
        raise problems[0]


# -- subprocess helpers
def start_proc(args: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 (this repository's interpreter and modules)
        [sys.executable, *args], cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )


def stop_proc(p: subprocess.Popen[bytes]) -> bytes:
    """Stop a child the test started; returns its output."""
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=10)
    if p.stdout is None or p.stdout.closed:  # stopping twice (as test_crash_d2_subprocess does) is harmless
        return b""
    out = p.stdout.read()
    p.stdout.close()
    return out


def is_write(fn: Any, name: str) -> bool:
    """True when `fn` (a function handed to `Desk._locked_retry`) belongs to the method or inner function called
    `name`. Whole dotted parts are compared, never a substring, and a test that selects a fault this way must also
    assert that the fault fired (a rename would otherwise turn the test into a silent no-op)."""
    return name in fn.__qualname__.split(".")


def lock_writes_after_answer(rig: Any, qualifier: Any, monkeypatch: Any, where: str) -> None:
    """Make the real write path (`Desk._write`, behind the real `_locked_retry`) refuse with a busy error for the one
    write named by `where` ("_touch" or "store") once `qualifier.answered` is set."""
    real = rig.desk._write

    @contextmanager
    def write(conn: Any) -> Iterator[None]:
        caller = sys._getframe(2).f_code.co_qualname  # frame 1 is contextlib's __enter__; 2 is the writing function
        if qualifier.answered and where in caller:
            raise sqlite3.OperationalError("database is locked")
        with real(conn):
            yield

    monkeypatch.setattr(rig.desk, "_write", write)


def stall_report(rig: Rig) -> str:
    """What the desk looks like when a wait for the follow-ups ran out: row states, the oldest pending rows, the
    adapter worker counts, the reaper and the drive places. Read-only; used in the failure message."""
    d = rig.desk
    by_state = rig.query("SELECT state, last_error, count(*) FROM followups GROUP BY 1, 2")
    oldest = rig.query(
        "SELECT lead_id, state, auto_attempts, last_error, updated_at FROM followups WHERE state != 'sent'"
        " ORDER BY updated_at LIMIT 5"
    )
    free = getattr(d._drives, "_free", None)  # noqa: SLF001
    waiting = len(getattr(d._drives, "_waiters", ()))  # noqa: SLF001
    return (
        f"states={by_state}\noldest={oldest}\nnow={d._now()} workers={dict(d._workers)} "  # noqa: SLF001
        f"abandoned={dict(d._abandoned)}\nreaper_running={d.reaper_running()} drive_free={free} "  # noqa: SLF001
        f"drive_waiters={waiting} threads={threading.active_count()}"
    )
