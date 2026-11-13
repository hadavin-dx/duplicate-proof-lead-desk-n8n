"""Findings about the stand-in servers of demo_only/ (made-up data, loopback only): the fake HubSpot reads and changes
its contacts only under its lock (every handler, not only the search); its fault counters count exactly, also for
requests that arrive together; the fake AI server answers a bad control value with a 400 and starts "mixed" with hot."""

from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from demo_only import fake_hubspot as fh
from demo_only import fake_model_server as fm

KEY = "k" + "e" * 15 + "-for-tests-0002"  # built from parts; not a real key
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SEARCH = {"filterGroups": [{"filters": [{"propertyName": "hadavin_lead_id", "operator": "EQ", "value": "L1"}]}]}
BASE = "/crm/v3/objects/contacts"


def _call(method: str, url: str, body: Any = None, timeout: float = 10) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310 (loopback)
        url, data, {"Content-Type": "application/json", "Authorization": "Bearer " + KEY}, method=method
    )
    try:
        with OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


class _OwnerLock:
    """A lock that knows which thread holds it (threading.Lock cannot say)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.owner: int | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        got = self._lock.acquire(blocking, timeout)
        if got:
            self.owner = threading.get_ident()
        return got

    def release(self) -> None:
        self.owner = None
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    def held_by_me(self) -> bool:
        return self.owner == threading.get_ident()


class _WatchedDict(dict):  # type: ignore[type-arg]
    """A dict that notes every read or change made by a thread that does NOT hold its owner's lock."""

    def __init__(self, lock: _OwnerLock) -> None:
        super().__init__()
        self._owner_lock = lock
        self.unlocked: list[str] = []

    def _note(self, what: str) -> None:
        if not self._owner_lock.held_by_me():
            self.unlocked.append(what)

    def __getitem__(self, k: Any) -> Any:
        self._note("getitem")
        return super().__getitem__(k)

    def __contains__(self, k: object) -> bool:
        self._note("contains")
        return super().__contains__(k)

    def __iter__(self) -> Any:
        self._note("iter")
        return super().__iter__()

    def items(self) -> Any:
        self._note("items")
        return super().items()

    def get(self, k: Any, default: Any = None) -> Any:
        self._note("get")
        return super().get(k, default)

    def pop(self, *a: Any) -> Any:
        self._note("pop")
        return super().pop(*a)


def _watched_state(mode: str = "ok") -> tuple[fh.HubspotState, _WatchedDict]:
    state = fh.HubspotState(KEY, mode)
    lock = _OwnerLock()
    state.lock = lock  # type: ignore[assignment]
    state.contacts = watched = _WatchedDict(lock)
    return state, watched


def test_every_read_and_change_of_the_contacts_holds_the_lock() -> None:
    """Get by id, get by email, list, search, patch and delete over HTTP: no handler touches the contact dict while
    its lock is not held (the old get, list and patch handlers did, and the search read its hits after the lock)."""
    state, watched = _watched_state()
    srv, th, url = fh.serve(state, "127.0.0.1")
    try:
        for n in (1, 2):
            state.create_if_absent({"email": f"a{n}@example.test", "hadavin_lead_id": "L1"})
        (first, second) = list(dict.keys(watched))
        statuses = [
            _call("GET", f"{url}{BASE}/{first}")[0],
            _call("GET", f"{url}{BASE}/a2@example.test?idProperty=email")[0],
            _call("GET", f"{url}{BASE}/{first}-gone")[0],
            _call("GET", f"{url}{BASE}?limit=1")[0],
            _call("POST", f"{url}{BASE}/search", SEARCH)[0],
            _call("PATCH", f"{url}{BASE}/a1@example.test?idProperty=email", {"properties": {"hs_lead_status": "X"}})[0],
            _call(
                "PATCH", f"{url}{BASE}/nobody@example.test?idProperty=email", {"properties": {"hs_lead_status": "X"}}
            )[0],
            _call("DELETE", f"{url}{BASE}/{second}")[0],
        ]
        assert statuses == [200, 200, 404, 200, 200, 200, 404, 204]
        assert watched.unlocked == []
    finally:
        fh.stop(srv, th, state)


def test_a_delete_between_the_find_and_the_answer_cannot_break_a_read() -> None:
    """The old search and get found an id and read the contact again after the lock was released: a delete in between
    raised KeyError in the handler. The locked view finds and copies in one step, and its copy does not change later."""
    state, _ = _watched_state()
    cid, _ = state.create_if_absent({"email": "a@example.test", "hadavin_lead_id": "L1"})
    view = state.search("L1")[0]
    with state.lock:
        state.bin[cid] = state.contacts.pop(cid)
    assert view["properties"]["email"] == "a@example.test"  # a copy: still whole after the delete
    assert state.public(cid, by_email=False) is None  # and a read of the deleted id is a clean "not there"
    assert state.search("L1") == [] and state.patch("a@example.test", {"hs_lead_status": "X"}) is None


class _BarrierFields(fh.HubspotState):
    """Two threads that read a counter together both see the old value (a barrier holds the first reader until the
    second arrives). Under the lock only one thread is ever inside, so the barrier times out instead and the second
    thread then sees the new value."""

    def __init__(self, *a: Any) -> None:
        super().__init__(*a)
        self._gate = threading.Barrier(2)
        self._raw_failed = 0
        self._raw_hung = False

    def _meet(self) -> None:
        try:
            self._gate.wait(0.4)
        except threading.BrokenBarrierError:
            pass

    @property  # type: ignore[override]
    def failed(self) -> int:
        value = self._raw_failed
        self._meet()
        return value

    @failed.setter
    def failed(self, v: int) -> None:
        self._raw_failed = v

    @property  # type: ignore[override]
    def hung_once(self) -> bool:
        value = self._raw_hung
        self._meet()
        return value

    @hung_once.setter
    def hung_once(self, v: bool) -> None:
        self._raw_hung = v


def _two_creates_at_once(state: fh.HubspotState, url: str) -> list[int]:
    out: list[int] = []
    gate = threading.Barrier(2, timeout=10)

    def one(n: int) -> None:
        gate.wait()
        try:
            out.append(_call("POST", url + BASE, {"properties": {"email": f"p{n}@example.test"}}, timeout=2.5)[0])
        except OSError:  # a hung request is cut by the client's own timeout
            out.append(0)

    threads = [threading.Thread(target=one, args=(n,), daemon=True) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return sorted(out)


def test_two_creates_that_arrive_together_get_exactly_one_fault_when_one_is_configured() -> None:
    state = _BarrierFields(KEY, "r429")
    state.times = 1
    srv, th, url = fh.serve(state, "127.0.0.1")
    try:
        assert _two_creates_at_once(state, url) == [201, 429]
    finally:
        fh.stop(srv, th, state)


def test_two_creates_that_arrive_together_get_exactly_one_fault_after_the_store() -> None:
    state = _BarrierFields(KEY, "r5xx_after_store")
    state.times = 1
    srv, th, url = fh.serve(state, "127.0.0.1")
    try:
        assert _two_creates_at_once(state, url) == [201, 500]
    finally:
        fh.stop(srv, th, state)


def test_only_one_of_two_creates_that_arrive_together_hangs() -> None:
    state = _BarrierFields(KEY, "timeout_after_create")
    srv, th, url = fh.serve(state, "127.0.0.1")
    try:
        assert _two_creates_at_once(state, url) == [0, 201]  # one is held (cut by the client at 2.5 s), one answers
    finally:
        fh.stop(srv, th, state)


@pytest.mark.parametrize("mode", ["r429", "r5xx", "r5xx_after_store", "timeout_after_create"])
def test_the_create_handler_asks_the_state_for_its_fault(mode: str) -> None:
    """The handler must go through take_failure / take_hang (a revert to the old inline check, which has no lock, leaves
    these calls out)."""
    asked: list[str] = []

    class Spy(fh.HubspotState):
        def take_failure(self) -> bool:
            asked.append("failure")
            return super().take_failure()

        def take_hang(self) -> bool:
            asked.append("hang")
            return super().take_hang()

    state = Spy(KEY, mode)
    state.times = 1
    state.release.set()  # a hold ends at once
    srv, th, url = fh.serve(state, "127.0.0.1")
    try:
        _call(
            "POST", url + BASE, {"properties": {"email": "q@example.test"}}, timeout=3
        ) if mode != "timeout_after_create" else None
        if mode == "timeout_after_create":
            try:
                _call("POST", url + BASE, {"properties": {"email": "q@example.test"}}, timeout=3)
            except OSError:
                pass
    finally:
        fh.stop(srv, th, state)
    assert asked == (["hang"] if mode == "timeout_after_create" else ["failure"])


def test_the_counters_are_exact_for_many_threads_under_a_tiny_switch_interval() -> None:
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(100):
            state = fh.HubspotState(KEY, "r429")
            state.times = 2
            took: list[bool] = []
            hung: list[bool] = []
            gate = threading.Barrier(16, timeout=10)

            def one(state: fh.HubspotState = state, took: list[bool] = took, hung: list[bool] = hung) -> None:
                gate.wait()
                took.append(state.take_failure())
                hung.append(state.take_hang())

            threads = [threading.Thread(target=one, daemon=True) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            assert not any(t.is_alive() for t in threads), "a counter thread did not finish"
            assert took.count(True) == 2 and state.failed == 2
            assert hung.count(True) == 1
    finally:
        sys.setswitchinterval(old)


@pytest.mark.parametrize("length", ["-1", "-5", "x", "1e3", "99999999999", "1048577", "\u00b2", "9" * 5000, "0" * 5000])
def test_the_hubspot_fake_refuses_a_content_length_that_is_not_a_usable_count(length: str) -> None:
    """A negative length used to make the fake read until the client closed, a huge one read for as long as the
    client sent: both are a 400 now, answered at once, without reading a byte of the body (a deliberate trade-off: a
    client that really sends 1 MiB or more may see a reset instead). A digit string of 5000 characters is a 400 too:
    `int()` of it raises ValueError in Python, which would have killed the handler with no answer."""
    state = fh.HubspotState(KEY, "ok")
    srv, th, url = fh.serve(state, "127.0.0.1")
    try:
        head = (
            f"POST {BASE} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {KEY}\r\n"
            f"Content-Length: {length}\r\nConnection: close\r\n\r\n"
        ).encode()
        with socket.create_connection(srv.server_address[:2], timeout=5) as c:
            c.sendall(head)  # no body follows: a fake that tried to read one would wait until the 5 s run out
            reply = b""
            while chunk := c.recv(4096):
                reply += chunk
        assert reply.startswith(b"HTTP/1.1 400 "), reply[:60]
    finally:
        fh.stop(srv, th, state)


def _ai_call(url: str) -> tuple[int, Any]:
    body = {"messages": [{"role": "user", "content": "lead id: L1\ne-mail: x@example.test\nAnswer?"}]}
    req = urllib.request.Request(  # noqa: S310 (loopback)
        url + "/v1/messages", json.dumps(body).encode(), {"x-api-key": "fake", "Content-Type": "application/json"}
    )
    with OPENER.open(req, timeout=10) as r:
        return r.status, json.loads(r.read())


def _control(url: str, body: Any) -> int:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url + "/control", data, {"Content-Type": "application/json"})  # noqa: S310
    try:
        with OPENER.open(req, timeout=10) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def ai(tmp_path: Path):
    srv, state = fm.make_model_server(control_file=tmp_path / "mode.json")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state, tmp_path / "mode.json"
    state.stop.set()
    srv.shutdown()
    srv.server_close()
    th.join(5)


def test_a_list_as_mode_in_the_control_file_falls_back_to_the_port_mode(ai: Any) -> None:
    url, state, control_file = ai
    control_file.write_text('{"mode": []}', encoding="utf-8")
    assert state.current_mode() == state.mode
    assert _ai_call(url)[0] == 200  # the request is answered, not dropped


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": []},
        {"mode": {"a": 1}},
        {"slow_seconds": "3"},
        {"hang_seconds": None},
        {"slow_seconds": -1},
        {"hang_seconds": True},
        b'{"hang_seconds": Infinity}',  # json.loads accepts it; Event.wait(inf) would raise OverflowError
        b'{"slow_seconds": NaN}',
        b'{"hang_seconds": -Infinity}',
        {"slow_seconds": 10**400},
        {"hang_seconds": 86_401},
        [],
        ["mode"],
        "mode",
        b"not json",
    ],
)
def test_a_control_value_of_the_wrong_kind_is_a_400_not_a_dropped_connection(ai: Any, bad: Any) -> None:
    url, state, _ = ai
    assert _control(url, bad) == 400
    assert isinstance(state.slow_seconds, float) and isinstance(state.hang_seconds, float)
    assert state.slow_seconds == 3.0 and state.hang_seconds == 30.0  # nothing was changed


@pytest.mark.parametrize("bad", [{"slow_seconds": "x"}, {"hang_seconds": -1}, {"mode": "nonsense"}])
def test_a_control_body_with_one_bad_key_changes_none_of_its_keys(ai: Any, bad: Any) -> None:
    """The keys are applied one by one only after every key has been checked: `mode` used to be set before a later bad
    key made the answer a 400."""
    url, state, _ = ai
    before = (state.mode, state.label, state.slow_seconds, state.hang_seconds, state.retry_after)
    assert _control(url, {"mode": "slow", "label": "changed", "retry_after": 7, **bad}) == 400
    assert (state.mode, state.label, state.slow_seconds, state.hang_seconds, state.retry_after) == before


def test_the_largest_allowed_wait_is_accepted(ai: Any) -> None:
    url, state, _ = ai
    assert _control(url, {"hang_seconds": fm.MAX_CONTROL_SECONDS, "slow_seconds": 0}) == 200
    assert state.hang_seconds == fm.MAX_CONTROL_SECONDS


def test_mixed_mode_starts_with_hot(ai: Any) -> None:
    url, state, _ = ai
    assert _control(url, {"mode": "mixed"}) == 200
    labels = []
    for _ in range(4):
        _, answer = _ai_call(url)
        labels.append(next(c["text"] for c in answer["content"] if c["type"] == "text"))
    assert labels == ["hot", "warm", "cold", "hot"]
