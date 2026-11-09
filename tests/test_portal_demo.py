"""Tests of the demo extension of the portal (demo_only/portal_demo.py). Made-up data only.

The server routes and the loader are tested in test_portal_api.py (the deleted-folder case, the body
and origin gates)."""

from __future__ import annotations

import importlib
import json
import os
import random
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from conftest import Env
from support import HOST, REPO, Rig, free_port, wait_until

from lead_desk.adapters import HttpCrm, HttpOutbox
from lead_desk.config import Config
from lead_desk.constants import (
    HTTP_BAD_REQUEST,
    HTTP_BUSY,
    HTTP_CONFLICT,
    HTTP_CREATED,
    HTTP_METHOD_NOT_ALLOWED,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    REPLY_MAX_BYTES,
)
from lead_desk.desk import Desk
from lead_desk.hooks import HOOK_POINTS, FlagFileHooks, Hooks


def _demo_module(name: str) -> Any:
    """Import a demo_only module. Only a missing demo_only package skips this file (the folder was deleted);
    any other import failure, a renamed product module for example, is a real error."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name is not None and e.name.split(".")[0] == "demo_only":
            pytest.skip(f"{e.name} is not there: demo_only/ was removed", allow_module_level=True)
        raise


portal_demo = _demo_module("demo_only.portal_demo")
RecordedQualifier = _demo_module("demo_only.fakes").RecordedQualifier
BURST_MAX, FIELD_MAX, GET, POST = portal_demo.BURST_MAX, portal_demo.FIELD_MAX, portal_demo.GET, portal_demo.POST
REF_PREFIX_RE, ROWS_MAX, PortalDemo, make = (
    portal_demo.REF_PREFIX_RE,
    portal_demo.ROWS_MAX,
    portal_demo.PortalDemo,
    portal_demo.make,
)

FIXTURES = REPO / "tests" / "portal_fixtures"
SERVER_ERROR_MIN = 500
FUZZ_ROUNDS = 60
FUZZ_SEED = 424242
PEOPLE_COUNT = 6
BURST_N = 10
SAMPLE_REF_PREFIX = "ui-1000000000000"
LONG_ROWS = ROWS_MAX + 5
PORT_SAMPLE = 8765
WILDCARD_V4 = ".".join(["0"] * 4)  # built from parts, never a typed address
SHORT_RETRIES = 2  # a few busy answers: the retries go on by time until one is accepted
RETRY_WINDOW = 1.2  # seconds a desk that stays busy or down is asked again (patched in: the real window is 30 s)
SHORT_DEADLINE = 0.5
SLOW_REPLY = 3.0
FAKES_TIMEOUT = 5.0
HARD_STOP = 30.0


def flag_in_own_folder(tmp_path: Path) -> Path:
    """A flag path whose folder holds nothing else, so a leftover temporary file would show."""
    (tmp_path / "flags").mkdir()
    return tmp_path / "flags" / "crash.flag"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def shape(v: Any) -> Any:
    """Keys and types only. A list keeps the shape of its first item (items of one list share a shape)."""
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0])] if v else []
    return type(v).__name__


def body(obj: Any) -> bytes:
    return json.dumps(obj).encode()


def fakes_config(url: str) -> dict[str, Any]:
    parts = urlsplit(url)
    return {"fakes_host": parts.hostname, "fakes_port": parts.port}


def symlink_or_skip(link: Path, target: Path) -> None:
    """Windows without the symlink privilege cannot make one: the test needs one, so skip there."""
    try:
        link.symlink_to(target)
    except OSError as e:  # WinError 1314
        pytest.skip(f"cannot create a symlink here: {e}")


class KeyedCrm(HttpCrm):
    capability = "keyed"  # type: ignore[assignment]


class BlindCrm(HttpCrm):
    capability = "blind"  # type: ignore[assignment]


class NoCallsQualifier(RecordedQualifier):
    def __init__(self, replies_path: Path) -> None:
        super().__init__(replies_path)
        del self.calls  # a recorded AI that keeps no counter


def wired(
    env: Env,
    furl: str,
    crm_cls: type[HttpCrm] = HttpCrm,
    qual_cls: type[RecordedQualifier] = RecordedQualifier,
    hooks: Hooks | None = None,
    config_url: str | None = None,
    **cfg: Any,
) -> Rig:
    """A started desk on the made-up CRM, sender and recorded AI over HTTP: what the demo loader builds.
    `config_url` aims the extension's reads at another server than the one the adapters use."""
    r = env.rig(start=False, **fakes_config(config_url or furl), **cfg)
    crm, outbox = crm_cls(furl, FAKES_TIMEOUT), HttpOutbox(furl, FAKES_TIMEOUT)
    if config_url:  # the guard compares the adapters' base with the config: make a test's stand-in agree
        crm._base = outbox._base = config_url
    desk = Desk(
        r.config,
        crm,
        outbox,
        qual_cls(r.replies_path),
        hooks=hooks,
        now=r.clock,
    )
    r.desk, r.qualifier = desk, desk.qualifier  # type: ignore[assignment]
    r.desks.append(desk)
    desk.startup()
    return r


@pytest.fixture(autouse=True)
def no_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal_demo, "BURST_COOLDOWN_SECONDS", 0.0)


@pytest.fixture
def parts(env: Env) -> tuple[PortalDemo, Rig, str]:
    """A started desk behind the real HTTP server, a fakes server, and the demo extension aimed at both."""
    srv, furl = env.fakes("blind", "keyed")
    with srv.state.lock:  # what the sender and the CRM hold after a few follow-ups (made-up rows)
        srv.state.outbox.append({"key": "k1", "to": "Ada@Example.test", "subject": "Thanks for your message"})
        srv.state.crm.append(
            {"lead_id": "L3f9a1c2b4d5e", "record": {"name": "Ada Example", "email": "Ada@Example.test"}}
        )
    rig = wired(env, furl, crm_cls=KeyedCrm)
    demo = make(rig.desk)
    demo.desk_url = env.serve(rig)
    return demo, rig, furl


# ---- a small scripted HTTP server for the cases the real fakes never produce
class Stub:
    def __init__(self, script: Callable[[BaseHTTPRequestHandler, str, bytes, int], None]) -> None:
        self.calls: dict[str, int] = {}
        lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

            def _go(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n) if n else b""
                key = f"{method} {self.path}"
                with lock:
                    outer.calls[key] = outer.calls.get(key, 0) + 1
                    count = outer.calls[key]
                try:
                    script(self, key, data, count)
                except OSError:
                    pass

            def do_GET(self) -> None:
                self._go(GET)

            def do_POST(self) -> None:
                self._go(POST)

        self.srv = ThreadingHTTPServer((HOST, 0), H)
        self.srv.daemon_threads = True
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://{HOST}:{self.srv.server_address[1]}"

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)


def send(h: BaseHTTPRequestHandler, status: int, obj: Any, headers: dict[str, str] | None = None) -> None:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    h.send_response(status)
    h.send_header("Content-Length", str(len(raw)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(raw)


@pytest.fixture
def stub() -> Iterator[Callable[[Callable[..., None]], Stub]]:
    made: list[Stub] = []

    def build(script: Callable[..., None]) -> Stub:
        st = Stub(script)
        made.append(st)
        return st

    yield build
    for st in made:
        st.stop()


def info_ok(h: BaseHTTPRequestHandler, key: str) -> bool:
    """Answer the three sender reads with small valid replies; True when this call was one of them."""
    if key == "GET /fake/info":
        send(h, HTTP_OK, {"outbox_capability": "keyed", "mode": "keyed"})
    elif key == "GET /fake/outbox":
        send(h, HTTP_OK, {"mode": "keyed", "count": 0, "messages": []})
    elif key == "GET /fake/crm":
        send(h, HTTP_OK, {"count": 0, "rows": []})
    else:
        return False
    return True


def burst_demo(env: Env, desk_stub: Stub) -> PortalDemo:
    """A wired extension whose burst posts to `desk_stub` instead of the real desk."""
    _, furl = env.fakes()
    demo = make(wired(env, furl).desk)
    demo.desk_url = desk_stub.url
    return demo


# ---- the extension and its files
def test_make_protocol(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo = parts[0]
    assert callable(demo.handle)
    assert isinstance(demo.assets, dict)
    assert set(demo.assets) <= {"portal_demo.js", "portal_demo.css"}


def test_assets_missing_file_left_out(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "ext"
    folder.mkdir()
    (folder / "canned_leads.json").write_text((REPO / "demo_only" / "canned_leads.json").read_text("utf-8"), "utf-8")
    (folder / "portal_demo.js").write_bytes(b"// js")
    monkeypatch.setattr(portal_demo, "HERE", folder)
    demo = make(env.rig().desk)
    assert demo.assets == {"portal_demo.js": (b"// js", "text/javascript; charset=utf-8")}  # css missing: left out
    (folder / "portal_demo.css").write_bytes(b"/* css */")
    assert make(env.rig().desk).assets["portal_demo.css"] == (b"/* css */", "text/css; charset=utf-8")


@pytest.mark.parametrize(
    "canned",
    ["{}", "[]", "[1]", '[{"name": "", "email": "a@example.test"}]', '[{"name": "A"}]', '[{"name": 5, "email": "x"}]'],
)
def test_canned_data_is_checked(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canned: str) -> None:
    (tmp_path / "canned_leads.json").write_text(canned, encoding="utf-8")
    monkeypatch.setattr(portal_demo, "HERE", tmp_path)
    with pytest.raises(ValueError):
        make(env.rig().desk)


def test_only_loader_names_demo_only() -> None:
    offenders = [
        str(p.relative_to(REPO))
        for p in sorted((REPO / "src").rglob("*.py"))
        if "demo_only" in p.read_text(encoding="utf-8") and p.name != "loader.py"
    ]
    assert not offenders, offenders


# ---- the demo is wired to made-up systems, or it says so and does nothing
def stand_in(**over: Any) -> Any:
    cfg = over.get("config") or Config(fakes_port=PORT_SAMPLE)
    outbox = HttpOutbox.__new__(HttpOutbox)
    outbox._base = cfg.fakes_url
    base = {
        "config": cfg,
        "crm": HttpCrm(cfg.fakes_url, 1.0),
        "outbox": outbox,
        "qualifier": RecordedQualifier(REPO / "demo_only" / "replies.json"),
        "hooks": FlagFileHooks(Path("flag")),
    }
    return SimpleNamespace(**{**base, **over})


@pytest.mark.parametrize(
    "over",
    [
        {"crm": object()},
        {"outbox": object()},
        {"qualifier": object()},
    ],
)
def test_not_wired_desk_is_refused(over: dict[str, Any]) -> None:
    demo = make(stand_in(**over))
    assert demo.wired is False
    assert demo.handle(POST, "burst", body({"n": 1})) == (HTTP_CONFLICT, {"error": "demo_not_wired"})
    assert demo.handle(POST, "crash", body({"point": "A"})) == (HTTP_CONFLICT, {"error": "demo_not_wired"})
    status, st = demo.handle(GET, "state", b"")
    assert status == HTTP_OK and st["simulated"] is False
    assert st["fakes"] == {"reachable": False, "error": "demo_not_wired"}
    assert "labels" not in st and st["people"] == []  # no "made-up" wording for systems that may be real


def test_not_wired_reads_nothing(stub: Callable[..., Stub]) -> None:
    seen = stub(lambda h, key, data, n: send(h, HTTP_OK, {}))
    demo = make(stand_in(config=Config(**fakes_config(seen.url)), crm=object()))
    demo.handle(GET, "state", b"")
    assert seen.calls == {}


def test_wired_demo_is_wired(parts: tuple[PortalDemo, Rig, str]) -> None:
    assert parts[0].wired is True


# ---- routing
def test_unknown_path_and_wrong_method(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo = parts[0]
    assert demo.handle(GET, "nope", b"")[0] == HTTP_NOT_FOUND
    assert demo.handle(GET, "../state", b"")[0] == HTTP_NOT_FOUND
    assert demo.handle(GET, "state/", b"")[0] == HTTP_NOT_FOUND
    for path, wrong in (("state", POST), ("burst", GET), ("crash", GET), ("burst", "PUT"), ("crash", "DELETE")):
        assert demo.handle(wrong, path, b"")[0] == HTTP_METHOD_NOT_ALLOWED, (path, wrong)


# ---- state
def test_state_shape_matches_fixture(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    status, obj = demo.handle(GET, "state", b"")
    assert status == HTTP_OK
    want = fixture("demo-state-no-crash.json")  # plain Hooks: crash not available
    assert shape(obj) == shape(want)
    assert obj["crash"] == want["crash"]
    assert obj["people"] == want["people"]
    assert obj["simulated"] is True
    assert obj["fakes"]["reachable"] is True
    assert obj["fakes"]["crm_mode"] == "keyed"  # read from the CRM adapter
    assert obj["ai_calls"] == rig.qualifier.calls == 0  # type: ignore[attr-defined]


def test_state_with_crash_hook(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    flag = tmp_path / "crash.flag"
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)
    assert demo.handle(GET, "state", b"")[1]["crash"] == fixture("demo-state.json")["crash"]
    flag.write_text("D1", encoding="utf-8")
    assert demo.handle(GET, "state", b"")[1]["crash"]["armed"] == "D1"
    flag.write_text("not a point; secret", encoding="utf-8")
    assert demo.handle(GET, "state", b"")[1]["crash"]["armed"] is None  # never echoes other content


def test_crash_then_state_shows_armed(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    demo = make(wired(env, furl, hooks=FlagFileHooks(tmp_path / "crash.flag")).desk)
    assert demo.handle(POST, "crash", body({"point": "A2"})) == (HTTP_OK, {"armed": "A2"})
    assert demo.handle(GET, "state", b"")[1]["crash"]["armed"] == "A2"
    demo.handle(POST, "crash", body({"point": "none"}))
    assert demo.handle(GET, "state", b"")[1]["crash"]["armed"] is None


def test_state_offline_matches_fixture(env: Env) -> None:
    _, furl = env.fakes()
    dead = f"http://{HOST}:{free_port()}"
    rig = wired(env, furl, config_url=dead)
    status, obj = make(rig.desk).handle(GET, "state", b"")
    want = fixture("demo-state-fakes-offline.json")
    assert status == HTTP_OK and obj["fakes"] == want["fakes"]
    assert shape(obj) == shape(want) | {"crash": shape(obj["crash"])}


def test_state_caps_rows(env: Env) -> None:
    srv, furl = env.fakes()
    demo = make(wired(env, furl).desk)
    with srv.state.lock:
        for i in range(LONG_ROWS):
            srv.state.outbox.append({"key": f"k{i}", "to": f"p{i}@example.test", "subject": "Hello"})
            srv.state.crm.append({"lead_id": f"L{i:012x}", "record": {"name": "Made Up"}})
    f = demo.handle(GET, "state", b"")[1]["fakes"]
    assert len(f["outbox"]["messages"]) == len(f["crm"]["rows"]) == ROWS_MAX
    assert f["outbox"]["count"] == f["crm"]["count"] == LONG_ROWS  # the true count is still shown
    assert set(f["outbox"]["messages"][0]) == {"to", "subject"}  # no idempotency key leaves the extension


def test_state_writes_nothing(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    for _ in range(5):
        demo.handle(GET, "state", b"")
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0


def test_ai_calls_none_without_counter(env: Env) -> None:
    _, furl = env.fakes()
    demo = make(wired(env, furl, qual_cls=NoCallsQualifier).desk)
    assert demo.handle(GET, "state", b"")[1]["ai_calls"] is None


def silent_crm(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
    """A sender that answers the three reads but never names its CRM mode (the repo's own fakes do name it)."""
    if not info_ok(h, key):
        send(h, HTTP_OK, {})


# crm mode: the sender's own word wins; else what the CRM adapter learned; else "unknown"
@pytest.mark.parametrize(("cls", "label"), [(KeyedCrm, "Leads in the demo CRM"), (BlindCrm, "Posts to the demo CRM")])
def test_crm_tile_follows_adapter_mode(env: Env, stub: Callable[..., Stub], cls: type[HttpCrm], label: str) -> None:
    _, furl = env.fakes()
    st = make(wired(env, furl, crm_cls=cls, config_url=stub(silent_crm).url).desk).handle(GET, "state", b"")[1]
    assert st["fakes"]["crm_mode"] == cls.capability
    assert st["labels"]["tiles"]["kpi-crm"] == label


def test_crm_mode_unknown_when_nobody_knows(env: Env, stub: Callable[..., Stub]) -> None:
    _, furl = env.fakes()
    st = make(wired(env, furl, config_url=stub(silent_crm).url).desk).handle(GET, "state", b"")[
        1
    ]  # plain HttpCrm: nothing learned yet
    assert st["fakes"]["crm_mode"] == "unknown"
    assert st["labels"]["tiles"]["kpi-crm"] == "Leads in the demo CRM"


def test_crm_mode_named_by_sender_wins(env: Env, stub: Callable[..., Stub]) -> None:
    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/info":
            send(h, HTTP_OK, {"outbox_capability": "keyed", "mode": "keyed", "crm_capability": "blind"})
        else:
            info_ok(h, key)

    _, furl = env.fakes()
    st = make(wired(env, furl, crm_cls=KeyedCrm, config_url=stub(script).url).desk).handle(GET, "state", b"")[1]
    assert st["fakes"]["crm_mode"] == "blind"
    assert st["labels"]["tiles"]["kpi-crm"] == "Posts to the demo CRM"


# sender misbehaviour: every case gives the same "unreachable" answer, quickly, never an error
def reads_offline(env: Env, stub: Callable[..., Stub], script: Callable[..., None]) -> None:
    _, furl = env.fakes()
    demo = make(wired(env, furl, config_url=stub(script).url).desk)
    status, obj = demo.handle(GET, "state", b"")
    assert status == HTTP_OK
    assert obj["fakes"] == {"reachable": False, "error": "unreachable"}


def test_state_one_read_fails(env: Env, stub: Callable[..., Stub]) -> None:
    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/crm":
            send(h, 500, {"error": "x"})
        else:
            info_ok(h, key)

    reads_offline(env, stub, script)


@pytest.mark.parametrize("raw", [b"[1, 2]", b"null", b"not json", b'{"messages": 5, "rows": 5}'])
def test_state_odd_reply(env: Env, stub: Callable[..., Stub], raw: bytes) -> None:
    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/outbox":
            send(h, HTTP_OK, raw)
        else:
            info_ok(h, key)

    reads_offline(env, stub, script)


def test_state_over_limit_reply(env: Env, stub: Callable[..., Stub]) -> None:
    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/outbox":
            send(h, HTTP_OK, b'{"x": "' + b"a" * (REPLY_MAX_BYTES + 10) + b'"}')
        else:
            info_ok(h, key)

    reads_offline(env, stub, script)


def test_state_truncated_reply(env: Env, stub: Callable[..., Stub]) -> None:
    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/info":
            h.send_response(HTTP_OK)
            h.send_header("Content-Length", "500")
            h.end_headers()
            h.wfile.write(b"{")
            h.close_connection = True
        else:
            info_ok(h, key)

    reads_offline(env, stub, script)


def test_state_redirect_is_not_followed(env: Env, stub: Callable[..., Stub]) -> None:
    elsewhere = stub(lambda h, key, data, n: send(h, HTTP_OK, {"count": 0, "rows": [], "messages": []}))

    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/crm":
            send(h, 302, b"", {"Location": elsewhere.url + "/fake/crm"})
        else:
            info_ok(h, key)

    reads_offline(env, stub, script)
    assert elsewhere.calls == {}


def test_state_one_overall_deadline(env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal_demo, "STATE_DEADLINE_SECONDS", SHORT_DEADLINE)

    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        time.sleep(SHORT_DEADLINE * 2)  # every read is slower than the whole deadline
        info_ok(h, key)

    _, furl = env.fakes()
    demo = make(wired(env, furl, config_url=stub(script).url, send_timeout_seconds=SLOW_REPLY).desk)
    t0 = time.monotonic()
    obj = demo.handle(GET, "state", b"")[1]
    assert obj["fakes"] == {"reachable": False, "error": "unreachable"}
    assert time.monotonic() - t0 < SHORT_DEADLINE * 2.5  # without a shared deadline: one read alone takes this long


def test_state_clips_long_text(env: Env, stub: Callable[..., Stub]) -> None:
    long = "x" * (FIELD_MAX * 5)

    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/crm":
            rec = {"name": long, "nested": {"a": [1]}, **{f"k{i}": i for i in range(50)}}
            send(h, HTTP_OK, {"count": 1, "rows": [{"lead_id": long, "record": rec}]})
        else:
            info_ok(h, key)

    _, furl = env.fakes()
    demo = make(wired(env, furl, config_url=stub(script).url).desk)
    row = demo.handle(GET, "state", b"")[1]["fakes"]["crm"]["rows"][0]
    assert len(row["lead_id"]) == FIELD_MAX and len(row["record"]["name"]) == FIELD_MAX
    assert len(row["record"]) <= portal_demo.RECORD_KEYS_MAX
    assert row["record"].get("nested") is None


# ---- burst
def test_burst_matches_fixture(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    status, obj = demo.handle(POST, "burst", body({"n": BURST_N, "person": 1, "door": "form"}))
    assert status == HTTP_OK
    assert shape(obj) == shape(fixture("demo-burst.json"))
    assert obj["n"] == BURST_N and REF_PREFIX_RE.fullmatch(obj["ref_prefix"])
    assert [a["ref"] for a in obj["answers"]] == [f"{obj['ref_prefix']}-{i}" for i in range(1, BURST_N + 1)]
    assert sorted(a["status"] for a in obj["answers"]) == [HTTP_OK] * (BURST_N - 1) + [HTTP_CREATED]
    assert sorted(a["verdict"] for a in obj["answers"]) == ["duplicate"] * (BURST_N - 1) + ["new"]
    assert rig.leads() == 1  # ten requests, one lead record


def test_burst_asks_the_ai_once(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    demo.handle(POST, "burst", body({"n": BURST_N}))
    wait_until(lambda: rig.qualifier.calls == 1, what="the follow-up to ask the recorded AI")  # type: ignore[attr-defined]
    assert demo.handle(GET, "state", b"")[1]["ai_calls"] == 1  # ten requests, one lead, one AI call


def test_burst_replay(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    first = demo.handle(POST, "burst", body({"n": 1, "ref_prefix": SAMPLE_REF_PREFIX}))[1]
    status, again = demo.handle(POST, "burst", body({"n": 1, "ref_prefix": SAMPLE_REF_PREFIX}))
    assert status == HTTP_OK
    assert shape(again) == shape(fixture("demo-burst-replay.json"))
    assert first["answers"][0]["replayed"] is False
    assert again["answers"][0] == {
        "ref": f"{SAMPLE_REF_PREFIX}-1",
        "status": HTTP_CREATED,
        "verdict": "new",
        "replayed": True,
    }
    assert rig.leads() == 1


def test_burst_door_person(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    demo.handle(POST, "burst", body({"n": 2, "person": PEOPLE_COUNT, "door": "webhook"}))
    assert rig.query("SELECT name, email FROM leads") == [("Edsger Lund", "edsger@example.com")]
    assert rig.query("SELECT DISTINCT source FROM receipts") == [("webhook",)]


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        b"7",
        b'{"n": 0}',
        b'{"n": %d}' % (BURST_MAX + 1),
        b'{"n": -1}',
        b'{"n": true}',
        b'{"n": "3"}',
        b'{"n": 1.5}',
        b"{}",
        b'{"n": 1, "person": 0}',
        b'{"n": 1, "person": %d}' % (PEOPLE_COUNT + 1),
        b'{"n": 1, "person": false}',
        b'{"n": 1, "door": "fax"}',
        b'{"n": 1, "door": null}',
        b'{"n": 1, "ref_prefix": "ui-123"}',
        b'{"n": 1, "ref_prefix": "ui-10000000000000"}',
        b'{"n": 1, "ref_prefix": "xx-1000000000000"}',
        b'{"n": 1, "ref_prefix": "ui-1000000000000\\n"}',
        b'{"n": 1, "ref_prefix": 5}',
        b'{"n": 1, "url": "http://example.test/leads"}',  # a caller can never choose the target
        b'{"n": 1, "extra": 1}',
        b"\xff\xfe",
        pytest.param(b"[" * 100_000, id="deep-brackets"),
    ],
)
def test_burst_bad_bodies(parts: tuple[PortalDemo, Rig, str], bad: bytes) -> None:
    demo, rig, _ = parts
    status, obj = demo.handle(POST, "burst", bad)
    assert status == HTTP_BAD_REQUEST and obj == {"error": "bad_body"}
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0


def test_burst_upper_limit(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    status, obj = demo.handle(POST, "burst", body({"n": BURST_MAX}))
    assert status == HTTP_OK and len(obj["answers"]) == BURST_MAX
    assert rig.leads() == 1


def test_second_burst_refused(parts: tuple[PortalDemo, Rig, str]) -> None:
    demo, rig, _ = parts
    assert demo._burst_lock.acquire(blocking=False)  # a burst is "running"
    try:
        status, obj = demo.handle(POST, "burst", body({"n": 1}))
    finally:
        demo._burst_lock.release()
    assert status == HTTP_TOO_MANY_REQUESTS and obj == {"error": "burst_in_progress"}
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0
    assert demo.handle(POST, "burst", body({"n": 1}))[0] == HTTP_OK  # the lock was given back


def test_burst_cooldown(parts: tuple[PortalDemo, Rig, str], monkeypatch: pytest.MonkeyPatch) -> None:
    demo, rig, _ = parts
    monkeypatch.setattr(portal_demo, "BURST_COOLDOWN_SECONDS", HARD_STOP)
    assert demo.handle(POST, "burst", body({"n": 1}))[0] == HTTP_OK
    assert demo.handle(POST, "burst", body({"n": 1})) == (HTTP_TOO_MANY_REQUESTS, {"error": "burst_too_soon"})
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 1  # the refused one sent nothing
    demo._next_burst_at = 0.0  # the wait is over
    assert demo.handle(POST, "burst", body({"n": 1}))[0] == HTTP_OK


def test_two_bursts_no_overlap(stub: Callable[..., Stub], env: Env) -> None:
    """The desk stub answers slowly, so both callers are inside handle() together; one must be turned away."""
    inside: list[int] = []

    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        inside.append(n)
        time.sleep(0.3)
        send(h, HTTP_CREATED, {"verdict": "new", "replayed": False})

    demo = burst_demo(env, stub(script))
    out: list[int] = []
    barrier = threading.Barrier(2)

    def go() -> None:
        barrier.wait()
        out.append(demo.handle(POST, "burst", body({"n": 1}))[0])

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(out) == [HTTP_OK, HTTP_TOO_MANY_REQUESTS]
    assert len(inside) == 1  # exactly one burst reached the desk


def test_burst_no_desk_address(env: Env) -> None:
    _, furl = env.fakes()
    demo = make(wired(env, furl).desk)  # Config.port is 0 (ephemeral): the extension cannot know the address
    assert demo.desk_url is None
    assert demo.handle(POST, "burst", body({"n": 1})) == (HTTP_CONFLICT, {"error": "desk_url_unknown"})


def test_burst_desk_down(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A desk that stays down is asked again for the whole retry window, not for a count of tries."""
    monkeypatch.setattr(portal_demo, "BURST_RETRY_DEADLINE_SECONDS", RETRY_WINDOW)
    _, furl = env.fakes()
    demo = make(wired(env, furl).desk)
    demo.desk_url = f"http://{HOST}:{free_port()}"
    n = 3
    t0 = time.monotonic()
    status, obj = demo.handle(POST, "burst", body({"n": n}))
    assert status == HTTP_OK
    assert [a["status"] for a in obj["answers"]] == ["refused"] * n
    assert time.monotonic() - t0 >= RETRY_WINDOW - 0.2
    assert obj["busy_retries"] >= n  # every refused try is counted


@pytest.mark.parametrize("k", [SHORT_RETRIES, SHORT_RETRIES + 3])  # the second needs more than 5 tries
def test_burst_busy_then_created(env: Env, stub: Callable[..., Stub], k: int) -> None:
    seen: dict[str, int] = {}

    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        ref = json.loads(data)["source_ref"]
        seen[ref] = seen.get(ref, 0) + 1
        if seen[ref] <= k:
            send(h, HTTP_BUSY, {"error": "busy", "receipt_id": None})
        else:
            send(h, HTTP_CREATED, {"verdict": "new", "replayed": False})

    n = 4
    obj = burst_demo(env, stub(script)).handle(POST, "burst", body({"n": n}))[1]
    assert [a["status"] for a in obj["answers"]] == [HTTP_CREATED] * n
    assert obj["busy_retries"] == n * k


def test_burst_busy_forever(env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal_demo, "BURST_RETRY_DEADLINE_SECONDS", RETRY_WINDOW)
    n = 3
    desk = stub(lambda h, key, data, c: send(h, HTTP_BUSY, {"error": "busy"}))
    t0 = time.monotonic()
    obj = burst_demo(env, desk).handle(POST, "burst", body({"n": n}))[1]
    assert [a["status"] for a in obj["answers"]] == [HTTP_BUSY] * n
    assert time.monotonic() - t0 >= RETRY_WINDOW - 0.2  # by time, not by count
    assert obj["busy_retries"] >= n * 3


def test_burst_honours_the_desks_retry_after(
    env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desk's 503 says Retry-After: 1. Two busy answers then cost at least 2 s (not 0.1 + 0.2 s)."""
    seen: dict[str, int] = {}

    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        ref = json.loads(data)["source_ref"]
        seen[ref] = seen.get(ref, 0) + 1
        if seen[ref] <= 2:
            send(h, HTTP_BUSY, {"error": "busy"}, {"Retry-After": "1"})
        else:
            send(h, HTTP_CREATED, {"verdict": "new", "replayed": False})

    t0 = time.monotonic()
    obj = burst_demo(env, stub(script)).handle(POST, "burst", body({"n": 1}))[1]
    assert [a["status"] for a in obj["answers"]] == [HTTP_CREATED]
    assert time.monotonic() - t0 >= 1.9
    assert obj["busy_retries"] == 2


def hang_up(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
    h.close_connection = True  # no reply at all


def cut_body(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
    h.send_response(HTTP_CREATED)
    h.send_header("Content-Length", "500")
    h.end_headers()
    h.wfile.write(b'{"verdict"')
    h.close_connection = True


def cut_error_body(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
    h.send_response(HTTP_CONFLICT)
    h.send_header("Content-Length", "500")
    h.end_headers()
    h.wfile.write(b'{"err')
    h.close_connection = True


@pytest.mark.parametrize(
    ("script", "status"),
    [(hang_up, "RemoteDisconnected"), (cut_body, HTTP_CREATED), (cut_error_body, HTTP_CONFLICT)],
)
def test_burst_keeps_every_answer(
    env: Env, stub: Callable[..., Stub], script: Callable[..., None], status: int | str
) -> None:
    n = 5
    obj = burst_demo(env, stub(script)).handle(POST, "burst", body({"n": n}))[1]
    assert len(obj["answers"]) == n  # a failed request is shown as one, never dropped
    assert [a["ref"] for a in obj["answers"]] == [f"{obj['ref_prefix']}-{i}" for i in range(1, n + 1)]
    for a in obj["answers"]:
        assert a["verdict"] is None and a["replayed"] is False
        assert a["status"] == status


def test_burst_deadline(env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal_demo, "BURST_DEADLINE_SECONDS", SHORT_DEADLINE)

    def slow(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        time.sleep(SLOW_REPLY)
        send(h, HTTP_CREATED, {})

    demo = burst_demo(env, stub(slow))
    t0 = time.monotonic()
    status, obj = demo.handle(POST, "burst", body({"n": 3}))
    assert status == HTTP_OK and time.monotonic() - t0 < SLOW_REPLY
    assert [a["status"] for a in obj["answers"]] == ["timeout"] * 3


def test_burst_busy_fixture(env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch) -> None:
    """demo-burst-busy.json: the reply of a burst whose desk keeps answering busy (a number status). The number of
    tries depends on the retry window (by time), so `busy_retries` is checked as a whole number, not its value."""
    monkeypatch.setattr(portal_demo, "BURST_RETRY_DEADLINE_SECONDS", 0.4)
    desk = stub(lambda h, key, data, c: send(h, HTTP_BUSY, {"error": "busy"}))
    obj = burst_demo(env, desk).handle(POST, "burst", body({"n": 1, "ref_prefix": SAMPLE_REF_PREFIX}))[1]
    want = fixture("demo-burst-busy.json")
    assert isinstance(obj["busy_retries"], int) and obj["busy_retries"] >= 2
    assert {k: v for k, v in obj.items() if k != "busy_retries"} == {
        k: v for k, v in want.items() if k != "busy_retries"
    }


def test_desk_url_from_config() -> None:
    def url(**cfg: Any) -> str | None:
        return make(stand_in(config=Config(**cfg))).desk_url

    assert url(port=PORT_SAMPLE, bind=WILDCARD_V4) == f"http://127.0.0.1:{PORT_SAMPLE}"
    assert url(port=PORT_SAMPLE, bind="::") == f"http://[::1]:{PORT_SAMPLE}"
    assert url(port=PORT_SAMPLE, bind="::1") == f"http://[::1]:{PORT_SAMPLE}"
    assert url(port=PORT_SAMPLE, bind="127.0.0.1") == f"http://127.0.0.1:{PORT_SAMPLE}"
    assert url(port=0) is None


# ---- crash
@pytest.mark.parametrize("point", HOOK_POINTS)
def test_crash_arms_point(env: Env, tmp_path: Path, point: str) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)
    status, obj = demo.handle(POST, "crash", body({"point": point}))
    assert (status, obj) == (HTTP_OK, {"armed": point})
    assert flag.read_text(encoding="utf-8") == point
    assert list(flag.parent.iterdir()) == [flag]  # no leftover temporary file
    assert obj.keys() == fixture("demo-crash.json").keys()
    assert demo.handle(POST, "crash", body({"point": "none"})) == (HTTP_OK, {"armed": None})
    assert not flag.exists()
    assert demo.handle(POST, "crash", body({"point": "none"}))[0] == HTTP_OK  # clearing twice is harmless


@pytest.mark.parametrize(
    "bad",
    [
        b'{"point": "a"}',
        b'{"point": "A "}',
        b'{"point": ""}',
        b'{"point": "A;rm -rf /"}',
        b'{"point": "../../etc/passwd"}',
        b'{"point": "NONE"}',
        b'{"point": null}',
        b'{"point": 1}',
        b'{"point": ["A"]}',
        b"{}",
        b'{"point": "A", "path": "other"}',  # the flag path is never taken from a request
        b'{"point": "A", "flag_path": "x"}',
        b"[]",
        b"x",
        b"",
    ],
)
def test_crash_off_list(env: Env, tmp_path: Path, bad: bytes) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)
    assert demo.handle(POST, "crash", bad) == (HTTP_BAD_REQUEST, {"error": "bad_body"})
    assert list(flag.parent.iterdir()) == []


def test_crash_plain_hooks(env: Env) -> None:
    _, furl = env.fakes()
    demo = make(wired(env, furl, hooks=Hooks()).desk)
    assert demo.handle(POST, "crash", body({"point": "A"})) == (HTTP_CONFLICT, {"error": "crash_unavailable"})
    assert demo.handle(POST, "crash", body({"point": "none"}))[0] == HTTP_CONFLICT


def test_crash_write_failure(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    flag = tmp_path / "missing-folder" / "crash.flag"  # parent folder does not exist
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)
    assert demo.handle(POST, "crash", body({"point": "A"})) == (HTTP_CONFLICT, {"error": "crash_unavailable"})


# ---- property: no 5xx, no exception, whatever the caller sends; only allowlisted words can reach the flag file
def test_no_server_error(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    rig = wired(env, furl, hooks=FlagFileHooks(flag))
    demo = make(rig.desk)
    rng = random.Random(FUZZ_SEED)  # noqa: S311 (a test seed, not a secret)
    pieces = [b"{", b"}", b'"point"', b":", b'"A"', b'"D2"', b'"none"', b'"n"', b"0", b",", b"null", b"[", b'"x"']
    seen: set[str] = set()
    for _ in range(FUZZ_ROUNDS):
        blob = b"".join(rng.choice(pieces) for _ in range(rng.randint(0, 12)))
        for path, method in (("crash", POST), ("burst", POST), ("burst", GET), ("x", GET)):
            if path == "burst" and method == POST:
                try:
                    n = json.loads(blob).get("n")
                except (ValueError, AttributeError, RecursionError):
                    n = None
                if n not in (None, 0):
                    continue  # a valid-looking burst would really fire; real bursts are tested above
            status, obj = demo.handle(method, path, blob)
            assert status < SERVER_ERROR_MIN, (method, path, blob)
            json.dumps(obj)
        if flag.exists():
            seen.add(flag.read_text(encoding="utf-8"))
    assert seen <= set(HOOK_POINTS)
    assert list(flag.parent.iterdir()) in ([], [flag])
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0


# ---- real loader (the product function itself, not a copy)
def test_real_loader_same_rule(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.import_module("lead_desk.loader").load_demo_portal
    _, furl = env.fakes()
    desk = wired(env, furl).desk
    assert isinstance(real(desk), PortalDemo)
    monkeypatch.setitem(sys.modules, "demo_only.portal_demo", None)
    assert real(desk) is None


# ---- fixed on-screen wording pinned word for word
def test_wording_pinned(parts: tuple[PortalDemo, Rig, str]) -> None:
    labels = parts[0].handle(GET, "state", b"")[1]["labels"]
    assert labels["top_line"] == (
        "Demo: Every lead is demo data. The demo CRM (customer records) and the demo mail are stand-ins. "
        "AI ratings are pre-written. "
        "Nothing here is sent to real customers or real systems."
    )
    assert labels["short_line"] == (
        "Every lead is demo data. Demo CRM, demo mail, pre-written AI ratings. Nothing reaches real customers."
    )
    assert labels["tiles"] == {
        "kpi-leads": "Leads",
        "kpi-outbox": "Emails in demo mail",
        "kpi-duplicates": "Repeats caught",
        "kpi-crm": "Leads in the demo CRM",
        "kpi-ai-calls": "AI ratings given",
    }
    assert labels == fixture("demo-state.json")["labels"]


# ---- more checks: adapters, concurrent posts
def test_adapter_aimed_elsewhere_not_wired(env: Env) -> None:
    _, furl = env.fakes()
    for base in ("http://example.test", "http://127.0.0.1:1"):  # a remote name; a loopback port that differs
        desk = wired(env, furl).desk
        assert make(desk).wired is True
        desk.crm._base = base  # type: ignore[attr-defined]
        demo = make(desk)
        assert demo.wired is False
        assert demo.handle(POST, "burst", body({"n": 1})) == (HTTP_CONFLICT, {"error": "demo_not_wired"})
        assert demo.handle(POST, "crash", body({"point": "A"})) == (HTTP_CONFLICT, {"error": "demo_not_wired"})
    desk = wired(env, furl).desk
    desk.outbox._base = "http://example.test"  # type: ignore[attr-defined]
    assert make(desk).wired is False


def test_crash_concurrent_posts(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)
    results: list[tuple[int, Any]] = []
    lock = threading.Lock()
    go = threading.Barrier(8)

    def post(point: str) -> None:
        go.wait(timeout=FAKES_TIMEOUT)
        r = demo.handle(POST, "crash", body({"point": point}))
        with lock:
            results.append(r)

    threads = [threading.Thread(target=post, args=(HOOK_POINTS[i % len(HOOK_POINTS)],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(HARD_STOP)
    statuses = [s for s, _ in results]
    # Windows: os.replace can fail while another writer holds the flag; the code reports 409 and the caller retries
    allowed = {HTTP_OK, HTTP_CONFLICT} if sys.platform == "win32" else {HTTP_OK}
    assert len(statuses) == 8 and set(statuses) <= allowed and HTTP_OK in statuses
    assert flag.read_text(encoding="utf-8") in HOOK_POINTS
    assert [p.name for p in flag.parent.iterdir()] == ["crash.flag"]


def test_crash_temp_name_is_not_guessable(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    for guess in ("crash.flag.tmp", "crash.flag.new", ".crash.flag.tmp"):
        symlink_or_skip(flag.parent / guess, victim)
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)
    assert demo.handle(POST, "crash", body({"point": "B"})) == (HTTP_OK, {"armed": "B"})
    assert victim.read_text(encoding="utf-8") == "keep"
    assert flag.read_text(encoding="utf-8") == "B"


def test_crash_failed_replace_cleans_up(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)

    def broken(src: Any, dst: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(portal_demo.os, "replace", broken)
    assert demo.handle(POST, "crash", body({"point": "A"})) == (HTTP_CONFLICT, {"error": "crash_unavailable"})
    assert list(flag.parent.iterdir()) == []  # no temporary file left behind


def test_straggler_extends_cooldown(env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal_demo, "BURST_DEADLINE_SECONDS", SHORT_DEADLINE)

    def slow(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        time.sleep(SLOW_REPLY)
        send(h, HTTP_CREATED, {})

    demo = burst_demo(env, stub(slow))
    assert demo.handle(POST, "burst", body({"n": 2}))[1]["answers"][0]["status"] == "timeout"
    assert demo.handle(POST, "burst", body({"n": 1})) == (HTTP_TOO_MANY_REQUESTS, {"error": "burst_too_soon"})


def test_burst_desk_url_must_be_local(env: Env) -> None:
    _, furl = env.fakes()
    demo = make(wired(env, furl).desk)
    demo.desk_url = "http://example.test:80"
    assert demo.handle(POST, "burst", body({"n": 1})) == (HTTP_CONFLICT, {"error": "desk_url_not_local"})
    assert demo._desk_url_is_local("http://[::1]:5") and demo._desk_url_is_local("http://127.0.0.1:5")
    assert not demo._desk_url_is_local("http://example.test:5") and not demo._desk_url_is_local("http://[")
    bound = make(stand_in(config=Config(bind="example.test", port=0, fakes_port=PORT_SAMPLE)))
    assert bound._desk_url_is_local("http://example.test:5")
    assert not bound._desk_url_is_local("http://" + "other-host" + ":6")


def test_state_nan_and_none_fields(env: Env) -> None:
    assert portal_demo._clip(float("nan")) is None
    assert portal_demo._clip(float("inf")) is None
    assert portal_demo._clip(1.5) == 1.5
    srv, furl = env.fakes("blind", "keyed")
    with srv.state.lock:
        srv.state.outbox.append({"key": "k2", "to": None, "subject": None})
    obj = make(wired(env, furl, crm_cls=KeyedCrm).desk).handle(GET, "state", b"")[1]
    row = obj["fakes"]["outbox"]["messages"][0]
    assert row["to"] == "" and row["subject"] == ""
    json.dumps(obj, allow_nan=False)  # valid JSON for the page


def garbage_status(h: BaseHTTPRequestHandler) -> None:
    h.wfile.write(b"garbage not http\r\n\r\n")
    h.close_connection = True


def cut_chunked(h: BaseHTTPRequestHandler) -> None:
    h.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\n{")
    h.close_connection = True


@pytest.mark.parametrize(("bad", "word"), [(garbage_status, "BadStatusLine"), (cut_chunked, "IncompleteRead")])
def test_burst_http_protocol_errors(env: Env, stub: Callable[..., Stub], bad: Callable[..., None], word: str) -> None:
    demo = burst_demo(env, stub(lambda h, key, data, n: bad(h)))
    status, obj = demo.handle(POST, "burst", body({"n": 2}))
    assert status == HTTP_OK
    assert [a["status"] for a in obj["answers"]] == [word, word]


@pytest.mark.parametrize("bad", [garbage_status, cut_chunked])
def test_state_http_protocol_errors(env: Env, stub: Callable[..., Stub], bad: Callable[..., None]) -> None:
    def script(h: BaseHTTPRequestHandler, key: str, data: bytes, n: int) -> None:
        if key == "GET /fake/info":
            bad(h)
        else:
            info_ok(h, key)

    reads_offline(env, stub, script)


def test_barrier_abort_when_thread_fails(env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch) -> None:
    demo = burst_demo(env, stub(lambda h, key, data, n: send(h, HTTP_CREATED, {})))
    real_start, made = threading.Thread.start, [0]

    def start(self: threading.Thread) -> None:
        if getattr(self._target, "__name__", "") == "one":  # type: ignore[attr-defined]
            made[0] += 1
            if made[0] > 2:
                raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    t0 = time.monotonic()
    status, obj = demo.handle(POST, "burst", body({"n": 4}))
    assert status == HTTP_OK and time.monotonic() - t0 < SLOW_REPLY  # the two waiting ones were let go at once
    assert [a["status"] for a in obj["answers"]] == [HTTP_CREATED, HTTP_CREATED, "error", "error"]


def test_armed_read_is_bounded(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    hooks = FlagFileHooks(flag)
    demo = make(wired(env, furl, hooks=hooks).desk)
    flag.write_bytes(b"A" + b" " * 64 + b"x" * 100_000)
    assert demo._armed(hooks) is None  # only the first bytes are read; the rest is not a point
    flag.write_bytes(b"D2\n")
    assert demo._armed(hooks) == "D2"


# ---- more checks: desk url, burst budget, temp files
@pytest.mark.parametrize(
    "url", ["file://localhost/leads", "ftp://127.0.0.1/leads", "data://localhost/x", "//localhost"]
)
def test_desk_url_needs_http(env: Env, url: str) -> None:
    _, furl = env.fakes()
    demo = make(wired(env, furl).desk)
    assert not demo._desk_url_is_local(url)
    demo.desk_url = url
    assert demo.handle(POST, "burst", body({"n": 1})) == (HTTP_CONFLICT, {"error": "desk_url_not_local"})


def test_burst_budget(env: Env, stub: Callable[..., Stub], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal_demo, "BURST_BUDGET_REQUESTS", 5)
    desk = stub(lambda h, key, data, c: send(h, HTTP_CREATED, {}))
    demo = burst_demo(env, desk)
    assert demo.handle(POST, "burst", body({"n": 3}))[0] == HTTP_OK
    assert demo.handle(POST, "burst", body({"n": 3})) == (HTTP_TOO_MANY_REQUESTS, {"error": "burst_budget_used"})
    assert demo.handle(POST, "burst", body({"n": 2}))[0] == HTTP_OK  # exactly what is left
    assert demo.handle(POST, "burst", body({"n": 1}))[1] == {"error": "burst_budget_used"}
    assert sum(desk.calls.values()) == 5  # the refused bursts sent nothing


def test_crash_sweeps_old_temp_files(env: Env, tmp_path: Path) -> None:
    _, furl = env.fakes()
    flag = flag_in_own_folder(tmp_path)
    old, fresh, other = flag.parent / ".crash-old.tmp", flag.parent / ".crash-new.tmp", flag.parent / "keep.txt"
    for f in (old, fresh, other):
        f.write_text("x", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    link, folder = flag.parent / ".crash-link.tmp", flag.parent / ".crash-dir.tmp"
    symlink_or_skip(link, victim)
    folder.mkdir()
    (folder / "inner.txt").write_text("keep", encoding="utf-8")
    long_ago = time.time() - portal_demo.FLAG_TEMP_MAX_AGE_SECONDS - 60  # wall-clock: file age
    for f in (old, folder):
        os.utime(f, (long_ago, long_ago))
    os.utime(link, (long_ago, long_ago), follow_symlinks=False)
    os.utime(victim, (long_ago, long_ago))
    demo = make(wired(env, furl, hooks=FlagFileHooks(flag)).desk)
    assert demo.handle(POST, "crash", body({"point": "A"})) == (HTTP_OK, {"armed": "A"})
    assert sorted(p.name for p in flag.parent.iterdir()) == [
        ".crash-dir.tmp",
        ".crash-link.tmp",
        ".crash-new.tmp",
        "crash.flag",
        "keep.txt",
    ]  # only the plain file went
    assert victim.read_text(encoding="utf-8") == "keep" and (folder / "inner.txt").is_file()


def test_adapter_base_must_be_http() -> None:
    for url in ("file://localhost", "ftp://127.0.0.1"):
        adapter = SimpleNamespace(_base=url)
        assert portal_demo.PortalDemo._adapter_points_at(adapter, url) is False
    assert portal_demo.PortalDemo._adapter_points_at(SimpleNamespace(_base="http://localhost:5"), "http://localhost:5")


def test_desk_url_port_must_match_config() -> None:
    fixed = make(stand_in(config=Config(port=PORT_SAMPLE, fakes_port=PORT_SAMPLE + 1)))
    assert fixed._desk_url_is_local(f"http://127.0.0.1:{PORT_SAMPLE}")
    assert not fixed._desk_url_is_local(f"http://127.0.0.1:{PORT_SAMPLE + 2}")
    assert not fixed._desk_url_is_local("http://127.0.0.1")
    system_chosen = make(stand_in(config=Config(port=0, fakes_port=PORT_SAMPLE)))
    assert system_chosen._desk_url_is_local(f"http://127.0.0.1:{PORT_SAMPLE + 2}")


def test_ai_replies_survive_a_desk_restart(parts: tuple[PortalDemo, Rig, str]) -> None:
    """the counter lives in memory and restarts at 0, the ratings live in the database; the tile must not
    show 0 beside stored ratings."""
    demo, rig, _ = parts
    demo.handle(POST, "burst", body({"n": BURST_N}))
    wait_until(lambda: rig.qualifier.calls == 1, what="the follow-up to ask the recorded AI")  # type: ignore[attr-defined]
    wait_until(lambda: demo.handle(GET, "state", b"")[1]["ai_calls"] == 1, what="the rating to be stored")
    rig.qualifier.calls = 0  # type: ignore[attr-defined]  # what a desk restart does to the counter
    assert demo.handle(GET, "state", b"")[1]["ai_calls"] == 1
