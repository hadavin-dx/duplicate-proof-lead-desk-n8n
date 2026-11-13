"""Class sweep, part 2 (part 1 is test_fakes_body_before_answer.py): the stand-in servers that tests build for
themselves must also read the request body BEFORE they answer. A server that answers and closes with the body still on
its way makes the system reset the connection (Windows: WinError 10053) and the client loses the answer. Each importable
stand-in is probed with a client that sends the headers, waits, then sends the body in two parts. `reset` is the
measurement that is the same on every system (see test_fakes_body_before_answer). A control handler that answers
without reading proves the probe can see the fault. Stand-ins written inside one test function (test_followup_edge_cases
and the retry stand-in of test_load_tools) cannot be imported; they read the body on their first line. Made-up data."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from support import HOST, StubSender
from test_fakes_body_before_answer import late_body, request_head, status_of

BODY = json.dumps(
    {"source": "form", "source_ref": "late-1", "payload": {"email": "a@example.test", "message": "x" * 300}}
).encode()


def _probe(addr: tuple[str, int], line: str, *more: str) -> tuple[int, bool]:
    got, reset = late_body(addr, request_head(line, HOST, len(BODY), "Connection: close", *more), BODY)
    return status_of(got), reset


class _AnswersFirst(BaseHTTPRequestHandler):
    """The fault itself: answers and closes without reading the body (a control for the probe)."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def do_POST(self) -> None:  # noqa: N802
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


def test_the_probe_sees_a_server_that_answers_before_reading_the_body() -> None:
    srv = ThreadingHTTPServer((HOST, 0), _AnswersFirst)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, reset = _probe(srv.server_address[:2], "POST /x HTTP/1.1")
        assert reset, "the control handler was not seen answering before the body was in"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("behaviour", ["status", "garbage"])
def test_support_stub_sender_reads_the_body_first(behaviour: str) -> None:
    stub = StubSender(behaviour, 503)
    try:
        status, reset = _probe(stub.srv.server_address[:2], "POST /fake/outbox HTTP/1.1")
        assert not reset and status == (503 if behaviour == "status" else 200)
    finally:
        stub.stop()


@pytest.mark.parametrize(
    ("line", "want"),
    [("POST /crm/v3/objects/contacts HTTP/1.1", 201), ("PATCH /crm/v3/objects/contacts/1 HTTP/1.1", 200)],
)
def test_hubspot_probe_stub_reads_the_body_first(line: str, want: int) -> None:
    import test_hubspot_probe as thp

    stub = thp.Stub({**thp.HAPPY})
    try:
        status, reset = _probe(stub.srv.server_address[:2], line)
        assert not reset and status == want
    finally:
        stub.close()


def test_load_tools_desk_stand_in_reads_the_body_first() -> None:
    import test_load_tools as tlt

    tlt._Desk.seen = {}
    srv = ThreadingHTTPServer((HOST, 0), tlt._Desk)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        late = json.dumps({"source": "form", "source_ref": "late-2", "payload": {}}).encode()
        got, reset = late_body(
            srv.server_address[:2], request_head("POST /leads HTTP/1.1", HOST, len(late), "Connection: close"), late
        )
        assert not reset and status_of(got) == 201
    finally:
        srv.shutdown()
        srv.server_close()


def test_portal_demo_scripted_stub_reads_the_body_first() -> None:
    import test_portal_demo as tpd

    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, count: int) -> None:
        tpd.send(h, 500, {"error": "scripted"})

    st = tpd.Stub(script)
    try:
        status, reset = _probe(st.srv.server_address[:2], "POST /x HTTP/1.1")
        assert not reset and status == 500
    finally:
        st.stop()
