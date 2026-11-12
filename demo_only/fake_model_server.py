"""Fake model server for drills: a local stand-in for the live model service (same request and reply shape as
lead_desk.live_claude uses). Demo-only code. Standard library only. Never receives a real key.
Run: python -m demo_only.fake_model_server --mode ok --port 48767 [--key-file PATH] [--call-log FILE]
Tests start it in a thread with make_model_server(port=0, mode=...). Change the mode while it runs with
POST /control {"mode": "..."}; GET /stats shows the counters.

Modes: ok (always the label in `label`), mixed (hot, warm, cold in turn: for flip-rate tests), slow (answers after
`slow_seconds`, default 3), timeout (answers after `hang_seconds`, default 30, or when the server stops), r429 (429
with Retry-After), malformed (200 with a body that is not JSON), refusal (200, stop reason refusal), r5xx (500),
badlabel (200 with text that is not a label), drip (a valid-looking 200 whose body arrives one byte every
0.3 s: each read is fast, the whole call is slow). The load-run names are accepted too: rate_limit = r429,
malformed_json = malformed, malformed_label = badlabel, server_error = r5xx.
`--call-log FILE` writes one JSON line per call ({ts, lead_id, mode, status}) BEFORE the answer is written (a call
that is held for a timeout is logged after the hold: status 499 when the caller had already gone), for the invariant
checker. `--control-file FILE` holds one mode word (or {"mode": ...}); it is read again on every call and wins over
POST /control.
Every POST /v1/messages counts as one call (`calls`), also failures. A request without the expected key header gets 401
and counts in `auth_failures`; the key itself is never stored or printed."""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODES = ("ok", "mixed", "slow", "timeout", "r429", "malformed", "refusal", "r5xx", "badlabel", "drip")
ALIASES = {"rate_limit": "r429", "malformed_json": "malformed", "malformed_label": "badlabel", "server_error": "r5xx"}
CYCLE = ("hot", "warm", "cold")
FAKE_IN_TOKENS = 40
FAKE_OUT_TOKENS = 2
LEAD_ID_LINE = re.compile(r"^lead id: (.*)$", re.MULTILINE)
STATUS_CLIENT_GONE = 499
LOGGED_STATUS = {"r429": 429, "r5xx": 500}  # every other mode that logs before answering answers 200


class ModelState:
    def __init__(
        self,
        mode: str = "ok",
        key: str | None = None,
        call_log: Path | None = None,
        control_file: Path | None = None,
    ) -> None:
        mode = ALIASES.get(mode, mode)
        if mode not in MODES:
            raise ValueError(mode)
        self.lock = threading.Lock()
        self.mode = mode
        self.key = key  # None = any non-empty key is accepted
        self.call_log = call_log
        self.control_file = control_file
        self.label = "warm"
        self.slow_seconds = 3.0
        self.hang_seconds = 30.0
        self.retry_after = "2"
        self.calls = 0
        self.auth_failures = 0
        self.stop = threading.Event()
        self.last_headers: dict[str, Any] = {}  # version header and whether a key came; never the key
        self.last_request: dict[str, Any] = {}  # the last JSON body (no headers, so no key)

    def current_mode(self) -> str:
        """The control file (when it holds a known mode) wins over the mode set through the port."""
        if self.control_file is not None:
            try:
                text = self.control_file.read_text(encoding="utf-8").strip()
                word = json.loads(text).get("mode") if text.startswith("{") else text
                word = ALIASES.get(word, word)
                if word in MODES:
                    return str(word)
            except (OSError, ValueError, AttributeError):
                pass
        return self.mode

    def log_call(self, request: dict[str, Any], mode: str, status: int) -> None:
        if self.call_log is None:
            return
        text = ""
        try:
            text = str(request["messages"][-1]["content"])
        except (KeyError, IndexError, TypeError):
            pass
        found = LEAD_ID_LINE.search(text)
        lead_id = found.group(1) if found else ""
        stamp = time.time()  # wall-clock: calendar stamp
        line = json.dumps({"ts": stamp, "lead_id": lead_id, "mode": mode, "status": status})
        with self.lock, self.call_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _handler(state: ModelState) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        server_version = "fakemodel"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return None

        def _send(self, status: int, raw: bytes, extra: dict[str, str] | None = None) -> int:
            """Write the answer; returns the status, or 499 when the caller had already gone."""
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
            except OSError:
                return STATUS_CLIENT_GONE
            return status

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/stats":
                with state.lock:
                    body = {"calls": state.calls, "auth_failures": state.auth_failures, "mode": state.mode}
                self._send(200, json.dumps(body).encode())
            else:
                self._send(404, b"{}")

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if self.path == "/control":
                obj = json.loads(raw or b"{}")
                if "mode" in obj:
                    obj["mode"] = ALIASES.get(obj["mode"], obj["mode"])
                with state.lock:
                    for k in ("mode", "label", "slow_seconds", "hang_seconds", "retry_after"):
                        if k in obj:
                            if k == "mode" and obj[k] not in MODES:
                                self._send(400, b"{}")
                                return
                            setattr(state, k, obj[k])
                self._send(200, b"{}")
                return
            if self.path != "/v1/messages":
                self._send(404, b"{}")
                return
            key = self.headers.get("x-api-key") or ""
            mode = state.current_mode()
            with state.lock:
                state.calls += 1
                call_no = state.calls
                label = state.label
                bad_key = not key or (state.key is not None and key != state.key)
                if bad_key:
                    state.auth_failures += 1
            try:
                request = json.loads(raw)
            except ValueError:
                request = {}
            if not isinstance(request, dict):
                request = {}
            if bad_key:
                state.log_call(request, mode, 401)
                self._send(401, b'{"type":"error"}')
                return
            state.last_headers = {"anthropic-version": self.headers.get("anthropic-version", ""), "has_key": bool(key)}
            state.last_request = request
            if mode == "slow":
                state.stop.wait(state.slow_seconds)
            elif mode == "timeout":
                state.stop.wait(state.hang_seconds)
            if mode not in ("timeout", "drip"):  # the line is written before the answer
                state.log_call(request, mode, LOGGED_STATUS.get(mode, 200))
            if mode == "r429":
                self._send(429, b'{"type":"error"}', {"Retry-After": str(state.retry_after)})
            elif mode == "r5xx":
                self._send(500, b'{"type":"error","error":{"type":"api_error","message":"boom"}}')
            elif mode == "drip":
                state.log_call(request, mode, 200)
                self.send_response(200)
                self.send_header("Content-Length", "40")
                self.end_headers()
                for _ in range(40):
                    if state.stop.wait(0.3):
                        break
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except OSError:
                        break
            elif mode == "malformed":
                self._send(200, b"{not json")
            else:
                text = {"mixed": CYCLE[call_no % 3], "refusal": "I can't help with that.", "badlabel": "maybe"}.get(
                    mode, label
                )
                body = {
                    "type": "message",
                    "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": text}],
                    "stop_reason": "refusal" if mode == "refusal" else "end_turn",
                    **({"stop_details": {"category": "x", "explanation": "y"}} if mode == "refusal" else {}),
                    "usage": {"input_tokens": FAKE_IN_TOKENS, "output_tokens": FAKE_OUT_TOKENS},
                }
                sent = self._send(200, json.dumps(body).encode())
                if mode == "timeout":  # held call: logged after the hold, 499 when the caller left meanwhile
                    state.log_call(request, mode, sent)

    return H


def make_model_server(
    host: str = "127.0.0.1",
    port: int = 0,
    mode: str = "ok",
    key: str | None = None,
    call_log: Path | None = None,
    control_file: Path | None = None,
) -> tuple[ThreadingHTTPServer, ModelState]:
    state = ModelState(mode, key, call_log, control_file)
    srv = ThreadingHTTPServer((host, port), _handler(state))
    srv.daemon_threads = True
    return srv, state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok", choices=(*MODES, *ALIASES))
    ap.add_argument("--port", type=int, default=48767)
    ap.add_argument("--key-file", default=None, help="file holding the fake key to require (fake keys only)")
    ap.add_argument("--call-log", type=Path, default=None, help="one JSON line per call, for the invariant checker")
    ap.add_argument("--control-file", type=Path, default=None, help="file with one mode word, read on every call")
    a = ap.parse_args()
    key = Path(a.key_file).read_text(encoding="utf-8").strip() if a.key_file else None
    srv, state = make_model_server(port=a.port, mode=a.mode, key=key, call_log=a.call_log, control_file=a.control_file)
    print(f"fake model listening on port {srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
