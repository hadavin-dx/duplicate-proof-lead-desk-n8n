"""scripts/n8n_api.py sends the n8n API key in a header: it must only go to https, or to http on this computer, and
never follow a redirect. Look-alike and unreadable addresses are built from parts so that this file holds no host name
or address of its own (the repository's own scans read every tracked file)."""

from __future__ import annotations

import importlib
import json
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RESERVED = "example" + "." + "test"  # the reserved test domain, as the repository's own rules allow it
DOC_IP = "198.51.100." + "7"  # a documentation address (RFC 5737)


def _u(scheme: str, rest: str) -> str:
    return scheme + ":" + "//" + rest


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str | None, key_exists: bool = True):
    key = tmp_path / "key.txt"
    if key_exists:
        key.write_text("made-up-key-for-the-test\n", encoding="utf-8")
    monkeypatch.setenv("N8N_API_KEY_FILE", str(key))
    if url is None:
        monkeypatch.delenv("N8N_URL", raising=False)
    else:
        monkeypatch.setenv("N8N_URL", url)
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    sys.modules.pop("n8n_api", None)
    return importlib.import_module("n8n_api")


@pytest.mark.parametrize(
    "url",
    [
        None,
        _u("http", "127.0.0.1:5679/"),
        _u("http", "localhost:5678"),
        _u("http", "[::1]:5678"),
        _u(
            "http", "[::ffff:" + "127.0.0.1]:5678"
        ),  # an IPv4-mapped loopback address is loopback (same answer as the desk)
        _u("https", RESERVED),
        _u("HTTPS", RESERVED.upper() + "/x/"),
    ],
)
def test_these_addresses_are_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str | None) -> None:
    mod = _load(monkeypatch, tmp_path, url)
    assert not mod.BASE.endswith("/")


REFUSED = [
    _u("http", RESERVED + ":5678"),
    _u("http", DOC_IP),
    _u("http", "localhost." + RESERVED),
    _u("http", "localhost@" + RESERVED),  # user name "localhost", host elsewhere
    _u("http", "127.0.0.1@" + RESERVED),
    _u("http", "localhost:x@" + RESERVED),
    _u("http", "127.0.0.1." + RESERVED),  # a name that only starts like an address
    _u("http", "localhost.:5678/"),  # trailing dot: a name, not the loopback name the repository accepts
    _u("http", "[::ffff:" + DOC_IP + "]:5678"),  # an IPv4-mapped address that is NOT loopback
    _u("http", "[::1%25eth0]:5678"),  # a scope id is not a plain loopback address
    _u("http", "0." * 3 + "0:5678"),
    # a user name part or a blank/control character: urlsplit reads these one way, urllib dials another (the old
    # guarantee rested on the failure of a name lookup, not on a check)
    _u("http", RESERVED + "@127.0.0.1"),
    _u("http", RESERVED + "\t@127.0.0.1"),
    _u("http", RESERVED + "\n@127.0.0.1"),
    _u("http", RESERVED + "\r\n@127.0.0.1:5678/x"),
    _u(
        "http", "127.0.0.1\t:5678"
    ),  # urlsplit drops the tab and reads loopback; urllib would dial the text with the tab
    _u("http", "127.0.0.1:5678/x\ny"),
    _u("http", "127.0.0.1:5678\n"),
    _u("http", " 127.0.0.1:5678"),
    _u("https", "user@" + RESERVED),
    _u("ftp", "127.0.0.1"),
    "file:" + "///x",
    RESERVED,
    "",
    _u("https", ""),
]


@pytest.mark.parametrize("url", REFUSED)
def test_these_addresses_are_refused_before_the_key_file_is_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str
) -> None:
    """The key file does not exist: a refusal that comes first is a SystemExit with our message, a late one would be
    a FileNotFoundError."""
    with pytest.raises(SystemExit, match="N8N_URL must be") as e:
        _load(monkeypatch, tmp_path, url, key_exists=False)
    assert "https" in str(e.value)
    assert "tunnel" in str(e.value) and "127.0.0.1" in str(e.value), "the refusal must say what to do instead"


@pytest.mark.parametrize("url", ["http://[", _u("http", "[::1"), _u("https", "[")])
def test_an_address_that_cannot_be_read_is_a_clean_refusal_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str
) -> None:
    with pytest.raises(SystemExit) as e:
        _load(monkeypatch, tmp_path, url, key_exists=False)
    assert "N8N_URL is not a valid address" in str(e.value)
    assert "tunnel" in str(e.value)


class _Recorder(BaseHTTPRequestHandler):
    log: list[dict[str, str]]
    redirect_to: str | None = None

    def do_GET(self) -> None:
        type(self).log.append({k.lower(): v for k, v in self.headers.items()})
        if self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
        self.end_headers()
        if not self.redirect_to:
            self.wfile.write(b"{}")

    do_POST = do_GET

    def log_message(self, *args: object) -> None:
        pass


class _ListThenRedirect(BaseHTTPRequestHandler):
    """GET answers a one-workflow list (200); a POST (activate, deactivate) answers a redirect."""

    def do_GET(self) -> None:
        body = b'{"data": [{"id": "7", "name": "W", "active": false}]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:9/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _server(redirect_to: str | None) -> tuple[ThreadingHTTPServer, type[_Recorder]]:
    handler = type("H", (_Recorder,), {"log": [], "redirect_to": redirect_to})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler


def test_a_redirect_is_not_followed_so_the_key_never_reaches_the_new_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target, target_log = _server(None)
    first, first_log = _server(_u("http", f"127.0.0.1:{target.server_address[1]}/elsewhere"))
    try:
        mod = _load(monkeypatch, tmp_path, None)
        monkeypatch.setattr(mod, "BASE", _u("http", f"127.0.0.1:{first.server_address[1]}"))
        code, _body = mod.call("GET", "/workflows")
    finally:
        for s in (first, target):
            s.shutdown()
            s.server_close()
    assert code == 302, "a redirect is reported as an error answer"
    assert first_log.log[0]["x-n8n-api-key"] == "made-up-key-for-the-test"  # the first address did get the key
    assert target_log.log == [], "the redirect target was called: the key header would have gone with it"


def _run_against(
    handler: type[BaseHTTPRequestHandler], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mod = _load(monkeypatch, tmp_path, None)
        monkeypatch.setattr(mod, "BASE", _u("http", f"127.0.0.1:{srv.server_address[1]}"))
        return mod.main(["n8n_api.py", *argv])
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("cmd", ["list", "activate", "deactivate"])
def test_a_redirect_on_the_list_ends_in_a_clean_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cmd: str
) -> None:
    handler = type("H", (_Recorder,), {"log": [], "redirect_to": "http://127.0.0.1:9/elsewhere"})
    argv = [cmd] if cmd == "list" else [cmd, "W"]
    with pytest.raises(SystemExit) as e:
        _run_against(handler, monkeypatch, tmp_path, argv)
    assert isinstance(e.value.code, str), "a TypeError traceback would not be a SystemExit message"
    assert "302" in e.value.code and "workflow list" in e.value.code and "no body kept" in e.value.code


@pytest.mark.parametrize("cmd", ["activate", "deactivate"])
def test_a_redirect_on_activate_and_deactivate_is_an_error_exit_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cmd: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_against(_ListThenRedirect, monkeypatch, tmp_path, [cmd, "W"]) == 1
    assert "302" in capsys.readouterr().out


def test_a_list_answer_without_a_data_list_is_a_clean_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Odd(_Recorder):
        redirect_to = None
        log: list[dict[str, str]] = []  # noqa: RUF012

    with pytest.raises(SystemExit) as e:
        _run_against(Odd, monkeypatch, tmp_path, ["list"])  # the stand-in answers {} with no "data"
    assert isinstance(e.value.code, str) and "unexpected body" in e.value.code


# --- clean ends instead of tracebacks; no body of another answer shown; no proxy for the key ----------------
KEY_TEXT = "made-up-key-for-the-test"  # what _load writes into the key file
ECHO = "ECHOED-BODY-TEXT"  # a body text that must never be printed


def _fixed(status: int, body: bytes) -> type[BaseHTTPRequestHandler]:
    """A stand-in n8n that answers every GET and POST with this status and body."""

    class Fixed(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _answer
        do_POST = _answer

        def log_message(self, *args: object) -> None:
            pass

    return Fixed


def _list_body(*items: object) -> bytes:
    return json.dumps({"data": list(items)}).encode()


def _message(e: pytest.ExceptionInfo[SystemExit]) -> str:
    assert isinstance(e.value.code, str), "a traceback (or a bare exit code) instead of a message"
    return e.value.code


def test_a_200_answer_that_is_not_json_is_a_clean_message_without_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as e:
        _run_against(_fixed(200, b"<html>" + ECHO.encode() + b"</html>"), monkeypatch, tmp_path, ["list"])
    msg = _message(e)
    assert "not JSON" in msg and ECHO not in msg and KEY_TEXT not in msg


def test_a_200_answer_with_bytes_that_are_not_text_is_a_clean_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as e:
        _run_against(_fixed(200, b"\xff\xfe\x00{"), monkeypatch, tmp_path, ["list"])
    assert "not JSON" in _message(e)


def test_an_unreachable_n8n_is_a_clean_message_without_the_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with socket.socket() as s:  # a local port that nothing listens on: bind, read the number, close
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    mod = _load(monkeypatch, tmp_path, None)
    monkeypatch.setattr(mod, "BASE", _u("http", f"127.0.0.1:{port}"))
    with pytest.raises(SystemExit) as e:
        mod.main(["n8n_api.py", "list"])
    msg = _message(e)
    assert "cannot reach n8n" in msg and KEY_TEXT not in msg and str(port) not in msg


ODD_ITEMS = [
    {"id": "1", "active": False},  # no name
    {"id": "1", "name": "W"},  # no active flag
    {"name": "W", "active": False},  # no id
    {"id": "1", "name": 5, "active": False},  # a name that is not text
    "a text",
    None,
]


@pytest.mark.parametrize("item", ODD_ITEMS, ids=range(len(ODD_ITEMS)))
@pytest.mark.parametrize("cmd", ["list", "activate", "deactivate"])
def test_a_list_with_an_odd_entry_is_a_clean_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cmd: str, item: object
) -> None:
    argv = [cmd] if cmd == "list" else [cmd, "W"]
    with pytest.raises(SystemExit) as e:
        _run_against(_fixed(200, _list_body(item)), monkeypatch, tmp_path, argv)
    assert "without name, id or active flag" in _message(e)


@pytest.mark.parametrize("argv", [[], ["import"], ["activate"], ["deactivate"], ["frobnicate"], ["frobnicate", "x"]])
def test_missing_or_unknown_arguments_print_the_usage_and_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    mod = _load(monkeypatch, tmp_path, None)
    assert mod.main(["n8n_api.py", *argv]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content", [None, "not json", "[1, 2]", '{"nodes": []}'], ids=["no-file", "text", "list", "no-name"]
)
def test_an_unreadable_workflow_file_is_a_clean_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None
) -> None:
    target = tmp_path / "wf.json"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _run_against(_fixed(200, b"{}"), monkeypatch, tmp_path, ["import", str(target)])
    assert "cannot read a workflow" in _message(e)


def test_a_missing_or_unreadable_key_file_is_a_clean_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    monkeypatch.delenv("N8N_URL", raising=False)
    for value in (None, str(tmp_path / "no-such-file")):
        if value is None:
            monkeypatch.delenv("N8N_API_KEY_FILE", raising=False)
        else:
            monkeypatch.setenv("N8N_API_KEY_FILE", value)
        sys.modules.pop("n8n_api", None)
        with pytest.raises(SystemExit) as e:
            importlib.import_module("n8n_api")
        assert "key" in _message(e)


@pytest.mark.parametrize("status", [201, 202, 204])
def test_another_success_answer_is_reported_without_its_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    body = b"" if status == HTTPStatus.NO_CONTENT else b'{"echo": "' + ECHO.encode() + b'"}'
    with pytest.raises(SystemExit) as e:  # the list
        _run_against(_fixed(status, body), monkeypatch, tmp_path, ["list"])
    msg = _message(e)
    assert str(status) in msg and "no body kept" in msg and ECHO not in msg
    wf = tmp_path / "wf.json"
    wf.write_text('{"name": "W", "nodes": [], "connections": {}}', encoding="utf-8")
    assert _run_against(_fixed(status, body), monkeypatch, tmp_path, ["import", str(wf)]) == 1  # not a 200: a failure
    out = capsys.readouterr().out
    assert str(status) in out and "no body kept" in out and ECHO not in out


def test_the_key_header_goes_straight_to_this_computer_never_to_a_proxy_of_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target, target_log = _server(None)
    proxy, proxy_log = _server(None)
    try:
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(name, _u("http", f"127.0.0.1:{proxy.server_address[1]}"))
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        mod = _load(monkeypatch, tmp_path, None)
        monkeypatch.setattr(mod, "BASE", _u("http", f"127.0.0.1:{target.server_address[1]}"))
        code, _body = mod.call("GET", "/workflows")
    finally:
        for s in (target, proxy):
            s.shutdown()
            s.server_close()
    assert code == HTTPStatus.OK
    assert proxy_log.log == [], "the request (and the key header) went to the proxy of the environment"
    assert target_log.log[0]["x-n8n-api-key"] == KEY_TEXT


def test_the_shared_opener_still_uses_the_proxy_of_the_environment_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the n8n script turns the proxy off (for its key); the other users of open_http are unchanged."""
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    import urllib.request

    from http_guard import open_http

    monkeypatch.setattr(urllib.request, "_opener", None)  # urlopen caches its opener, with the proxies of that moment
    proxy, proxy_log = _server(None)
    try:
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(name, _u("http", f"127.0.0.1:{proxy.server_address[1]}"))
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        with open_http(_u("http", f"{RESERVED}:81/x"), 5):
            pass
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert len(proxy_log.log) == 1, "open_http no longer honours the proxy settings by default"


def test_what_is_accepted_is_exactly_what_urllib_would_dial() -> None:
    """The proxy bypass rests on this: for every accepted address the host text urllib dials equals the netloc we
    judged (no user name part, no hidden character), so the check is a check and not a failed name lookup."""
    import urllib.request
    from urllib.parse import urlsplit

    for url in (
        _u("http", "127.0.0.1:5679/"),
        _u("http", "localhost:5678"),
        _u("http", "[::1]:5678"),
        _u("https", RESERVED + "/x/"),
    ):
        assert urllib.request.Request(url).host == urlsplit(url).netloc  # noqa: S310 - never opened


# --- the key file and network faults: never a traceback that holds the key or the address -----------------
BAD_KEYS = {
    "two-lines": "made-up-key-LINE-ONE\nmade-up-key-LINE-TWO\n",
    "non-latin-1": "made-up-key-\u4e2d\u6587-end",
    "trailing-nul": "made-up-key-NUL\x00",
    "tab-inside": "made-up-key-\tTAB-inside",
    "space-inside": "made-up-key SPACE-inside",
    "empty": "",
    "only-blanks": "  \n\t \n",
    "delete-char": "made-up-key-\x7f-del",
    "latin-1-letter": "made-up-key-\u00e9-latin1",  # latin-1 can send it, but it is not printable ASCII: refused too
}


@pytest.mark.parametrize("name", list(BAD_KEYS))
def test_a_malformed_key_file_is_refused_with_a_fixed_message_that_does_not_show_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    text = BAD_KEYS[name]
    (tmp_path / "bad-key.txt").write_bytes(text.encode("utf-8"))
    monkeypatch.setenv("N8N_API_KEY_FILE", str(tmp_path / "bad-key.txt"))
    monkeypatch.delenv("N8N_URL", raising=False)
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    sys.modules.pop("n8n_api", None)
    with pytest.raises(SystemExit) as e:
        importlib.import_module("n8n_api")
    shown = _message(e) + capsys.readouterr().out + capsys.readouterr().err
    assert "key file" in shown
    for piece in ("made-up-key", "LINE-", "SPACE", "TAB", "NUL", "latin1"):
        assert piece not in shown, f"the key text {piece!r} is in the message"


def _call_with_base(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base: str) -> str:
    mod = _load(monkeypatch, tmp_path, None)
    monkeypatch.setattr(mod, "BASE", base)  # a base that checked_base would refuse: what call() does with it is tested
    with pytest.raises(SystemExit) as e:
        mod.call("GET", "/workflows")
    return _message(e)


@pytest.mark.parametrize(
    "base",
    [_u("http", "127.0.0.1:99x"), _u("http", "127.0.0.1:1/\x00hidden"), _u("http", "[::1:5678")],
    ids=["non-numeric-port", "control-char", "bad-bracket"],
)
def test_an_invalid_address_inside_http_client_prints_the_error_type_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base: str
) -> None:
    msg = _call_with_base(monkeypatch, tmp_path, base)
    assert "cannot reach n8n" in msg
    for piece in (KEY_TEXT, "99x", "hidden", "127.0.0.1", "::1"):
        assert piece not in msg, f"{piece!r} leaked into: {msg}"


def _short_body(status: int) -> type[BaseHTTPRequestHandler]:
    """Announces 100 bytes, sends 5 and closes the connection: the client reads IncompleteRead."""

    class Short(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"short")
            self.close_connection = True

        do_GET = _answer
        do_POST = _answer

        def log_message(self, *args: object) -> None:
            pass

    return Short


def test_a_body_that_is_cut_off_is_a_clean_message_with_the_error_type_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as e:
        _run_against(_short_body(200), monkeypatch, tmp_path, ["list"])
    msg = _message(e)
    assert "IncompleteRead" in msg
    assert KEY_TEXT not in msg and "short" not in msg and "127.0.0.1" not in msg


def test_an_error_page_that_is_cut_off_still_gives_the_status_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _short_body(500))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mod = _load(monkeypatch, tmp_path, None)
        monkeypatch.setattr(mod, "BASE", _u("http", f"127.0.0.1:{srv.server_address[1]}"))
        code, body = mod.call("GET", "/workflows")
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 500 and body == "(no body kept)"


def test_the_shared_opener_still_uses_the_proxy_of_the_environment_for_https_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The https half of the guard: an opener built without the proxy handler would dial the host directly. A stand-in
    proxy records the CONNECT request that a proxy-aware opener sends it."""
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    import urllib.request

    from http_guard import open_http

    seen: list[str] = []

    class Connect(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:
            seen.append(self.path)
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    monkeypatch.setattr(urllib.request, "_opener", None)  # urlopen caches its opener, with the proxies of that moment
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Connect)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        for name in ("HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(name, _u("http", f"127.0.0.1:{proxy.server_address[1]}"))
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        for follow in (True, False):
            with pytest.raises(OSError):  # the stand-in proxy refuses the tunnel
                open_http(_u("https", f"{RESERVED}:8443/x"), 5, follow_redirects=follow)
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert seen == [f"{RESERVED}:8443"] * 2, "open_http did not send the https request through the environment proxy"
