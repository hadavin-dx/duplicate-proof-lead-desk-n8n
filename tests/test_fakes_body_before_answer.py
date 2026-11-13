"""Class sweep: a server that answers a request and closes while the request body is still on its way makes the
operating system reset the connection, and on some systems (Windows: WinError 10053) the client then never sees the
answer. Every answer path of the stand-in servers (demo_only/) and of the desk's own early replies is probed with a
client that sends the headers, waits, then sends the body in two parts with a pause between them.
What is measured (the same on every system, unlike the loss itself): `reset` is True when the second part of the body
cannot be written or the answer cannot be read to its end, that is, when the server closed before the body was in.
Made-up data, loopback only."""

from __future__ import annotations

import json
import socket
import time
from typing import Any

PAUSE = 0.3


def late_body(addr: tuple[str, int], head: bytes, body: bytes) -> tuple[bytes, bool]:
    """Send `head`, pause, half of `body`, pause, the rest; return (what was read, reset seen)."""
    reset = False
    got = b""
    with socket.create_connection(addr, timeout=5) as c:
        c.sendall(head)
        half = max(len(body) // 2, 1)
        for part in (body[:half], body[half:]):
            time.sleep(PAUSE)
            if part:
                try:
                    c.sendall(part)
                except OSError:
                    reset = True
        c.settimeout(2)
        try:
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                got += chunk
        except OSError:
            reset = True
    return got, reset


def early_answer(addr: tuple[str, int], head: bytes) -> bytes:
    """What the server says before any body byte was sent (b"" when it waits for the body)."""
    with socket.create_connection(addr, timeout=5) as c:
        c.sendall(head)
        c.settimeout(PAUSE)
        try:
            return c.recv(200)
        except TimeoutError:
            return b""
        except OSError:
            return b"reset"


def request_head(line: str, host: str, length: int, *more: str) -> bytes:
    return "\r\n".join([line, f"Host: {host}", f"Content-Length: {length}", *more, "", ""]).encode()


def status_of(answer: bytes) -> int:
    return int(answer.split(b" ", 2)[1]) if answer.startswith(b"HTTP/") else 0


def json_of(answer: bytes) -> Any:
    return json.loads(answer.split(b"\r\n\r\n", 1)[1] or b"null")


# -- the stand-in servers: each answer path is probed twice
#    1. late body: the server must take the whole body in before it closes (no reset seen, the answer arrives);
#    2. no early answer: a server that answers and closes BEFORE the body is in has no way to avoid the reset, so the
#       fake answers only after it read the body (it waits for it).
import threading  # noqa: E402

import pytest  # noqa: E402

from demo_only import fake_hubspot as fh  # noqa: E402
from demo_only import fake_model_server as fm  # noqa: E402
from demo_only import fakes_server as fs  # noqa: E402

KEY = "k" + "e" * 15 + "-for-tests-0002"  # built from parts; not a real key
SMALL = b'{"properties":{"email":"a@example.test"}}' * 5


@pytest.fixture
def hubspot():
    state = fh.HubspotState(KEY, "ok")
    srv, th, _ = fh.serve(state, "127.0.0.1")
    yield srv.server_address[:2], state
    fh.stop(srv, th, state)


HUBSPOT_PATHS = [
    ("POST /crm/v3/objects/contacts", "Bearer wrong"),  # 401 from the gate
    ("POST /crm/v3/objects/contacts/search", "Bearer wrong"),
    ("PATCH /crm/v3/objects/contacts/a@example.test?idProperty=email", "Bearer wrong"),
    ("DELETE /crm/v3/objects/contacts/1000", "Bearer wrong"),
    ("POST /crm/v3/objects/nothing", "Bearer " + KEY),  # 404 after the gate
    ("PATCH /crm/v3/objects/contacts/a@example.test", "Bearer " + KEY),  # 400: idProperty missing
]


@pytest.mark.parametrize(("line", "auth"), HUBSPOT_PATHS)
def test_fake_hubspot_takes_the_body_in_before_it_closes(hubspot: Any, line: str, auth: str) -> None:
    addr, _ = hubspot
    head = request_head(line + " HTTP/1.1", "x", len(SMALL), f"Authorization: {auth}", "Connection: close")
    got, reset = late_body(addr, head, SMALL)
    assert not reset and status_of(got) in (400, 401, 404), (got[:60], reset)
    assert early_answer(addr, head) == b""  # it waits for the body: nothing comes before it


@pytest.fixture
def model():
    srv, state = fm.make_model_server()
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv.server_address[:2]
    state.stop.set()
    srv.shutdown()
    srv.server_close()
    th.join(5)


MODEL_PATHS = [
    ("POST /v1/messages", ()),  # no key: 401
    ("POST /v1/messages", ("x-api-key: fake",)),  # answered 200 after the body
    ("POST /control", ()),  # a bad control body: 400
    ("POST /nothing", ()),  # 404
]


@pytest.mark.parametrize(("line", "more"), MODEL_PATHS)
def test_fake_model_server_takes_the_body_in_before_it_answers(model: Any, line: str, more: tuple[str, ...]) -> None:
    body = (
        b'{"messages":[{"role":"user","content":"lead id: L1\\ne-mail: x@example.test"}]}' if "v1" in line else b"[1]"
    )
    head = request_head(line + " HTTP/1.1", "x", len(body), *more)
    got, reset = late_body(model, head, body)
    assert not reset and status_of(got) in (200, 400, 401, 404), (got[:60], reset)
    assert early_answer(model, head) == b""


@pytest.fixture
def fakes():
    srv, _, th = fs.serve_fakes_in_thread("keyed", 0, "keyed")
    yield srv.server_address[:2]
    fs.stop_fakes(srv, th)


FAKES_PATHS = [
    ("POST /fake/outbox", b"[1]"),  # 400 bad body
    ("POST /fake/crm", b"not json"),
    ("POST /fake/control", b'{"fail_next":"nothing"}'),
    ("POST /fake/nothing", b"{}"),  # 404
    ("POST /fake/reset", b"{}"),
]


@pytest.mark.parametrize(("line", "body"), FAKES_PATHS)
def test_fakes_server_takes_the_body_in_before_it_answers(fakes: Any, line: str, body: bytes) -> None:
    head = request_head(line + " HTTP/1.1", "x", len(body))
    got, reset = late_body(fakes, head, body)
    assert not reset and status_of(got) in (200, 400, 404), (got[:60], reset)
    assert early_answer(fakes, head) == b""


# -- the desk's own early replies (src/lead_desk/server.py): it answers some requests at once on purpose (a body over
#    the cap may never arrive), so the contract here is different: it half-closes, then keeps reading and dropping the
#    body for a moment (`_drain`), so the close never resets the connection under the reply.
def test_the_desks_early_replies_leave_no_reset_under_a_late_body(env: Any) -> None:
    from support import lead

    rig = env.rig()
    base = env.serve(rig)
    host, port = base.split("//")[1].split(":")
    addr, shown = (host, int(port)), f"{host}:{port}"
    lead_id = rig.desk.handle("form", "p-1", lead()).body["lead_id"]
    cap = rig.desk.config.size_cap_bytes
    small = b'{"source":"form"}' * 4
    big = b"x" * (cap + 1)
    cases = {
        "POST /leads over the cap (413)": (request_head("POST /leads HTTP/1.1", shown, len(big)), big, 413),
        "POST /leads/<id>/step over the cap (413)": (
            request_head(f"POST /leads/{lead_id}/step HTTP/1.1", shown, len(big)),
            big,
            413,
        ),
        "POST /review/<id>/resolve over the cap (413)": (
            request_head("POST /review/D1/resolve HTTP/1.1", shown, len(big)),
            big,
            413,
        ),
        "PUT /leads (405)": (request_head("PUT /leads HTTP/1.1", shown, len(small)), small, 405),
        "POST /nothing (404)": (request_head("POST /nothing HTTP/1.1", shown, len(small)), small, 404),
        "POST /leads, foreign Host (403)": (
            request_head("POST /leads HTTP/1.1", "evil.example", len(small)),
            small,
            403,
        ),
        "FOO /leads, unknown method (501)": (request_head("FOO /leads HTTP/1.1", shown, len(small)), small, 501),
    }
    resets = {}
    for name, (head, body, want) in cases.items():
        got, reset = late_body(addr, head, body)
        assert status_of(got) == want, (name, got[:60])
        resets[name] = reset
    assert not any(resets.values()), resets  # the names of the paths that closed on a body still on its way


def test_the_desks_busy_reply_leaves_no_reset_under_a_late_body(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 503 for a connection that gets no thread: the lingerer keeps reading the body while the reply is out."""
    from lead_desk import server

    rig = env.rig()
    base = env.serve(rig)
    host, port = base.split("//")[1].split(":")
    addr, shown = (host, int(port)), f"{host}:{port}"
    small = b'{"source":"form"}' * 4
    monkeypatch.setattr(server, "MAX_HANDLER_THREADS", 1)
    stalled = socket.create_connection(addr)  # holds the only handler thread (a head that never ends)
    try:
        stalled.sendall(b"POST /leads HTTP/1.1\r\nHost: x\r\n")
        time.sleep(PAUSE)
        got, reset = late_body(addr, request_head("POST /leads HTTP/1.1", shown, len(small)), small)
        assert status_of(got) == 503 and not reset, (got[:60], reset)
    finally:
        stalled.close()
