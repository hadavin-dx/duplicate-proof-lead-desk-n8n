"""Fakes server (separate process; contract.md, HTTP table section, the paragraph that starts "Fakes server").
Demo-only code.
Run: python -m demo_only.fakes_server --outbox keyed|blind [--crm keyed|blind] [--port 8766]
Default host and port come from lead_desk.config.Config. Tests start it in a thread with make_fakes_server(port=0)."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from lead_desk.config import Config
from lead_desk.constants import HTTP_BAD_REQUEST, HTTP_NOT_FOUND, HTTP_OK, HTTP_UNPROCESSABLE, NUDGE_KEY_PREFIX

FAIL_MODES = ("none", "refuse", "drop", "record_then_drop", "store_then_delay")
MODES = ("keyed", "blind")
TARGETS = ("outbox", "crm")


class FakesState:
    """All counters and controls. Tests read/flip these directly when the server runs in their process."""

    def __init__(self, outbox_mode: str = "keyed", crm_mode: str = "keyed") -> None:
        if outbox_mode not in MODES or crm_mode not in MODES:
            raise ValueError("mode must be keyed or blind")
        self.lock = threading.Lock()
        self.outbox_mode = outbox_mode
        self.crm_mode = crm_mode
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.outbox: list[dict[str, str]] = []
            self.crm: list[dict[str, Any]] = []
            self.crm_fields: dict[str, dict[str, str]] = {}  # lead_id -> {"owner_id", "status"}
            # one-shot control per target ("outbox" or "crm"): [fail_next, delay_ms]
            self.control: dict[str, list[Any]] = {"outbox": ["none", 0], "crm": ["none", 0]}


class _Handler(BaseHTTPRequestHandler):
    server_version = "fakes"
    state: FakesState  # set on the subclass by make_fakes_server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    # -- helpers
    def _reply(self, status: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _json_body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def _hang_up(self) -> None:
        self.close_connection = True  # no reply at all: the client sees the connection closed

    # -- routes
    def do_GET(self) -> None:
        st = self.state
        path = self.path.split("?")[0]
        with st.lock:
            if path == "/fake/info":
                return self._reply(
                    HTTP_OK,
                    {"outbox_capability": st.outbox_mode, "crm_capability": st.crm_mode, "mode": st.outbox_mode},
                )
            if path == "/fake/outbox":
                msgs = [{"key": m["key"], "to": m["to"], "subject": m["subject"]} for m in st.outbox]
                # reminders are counted over every stored message (the capped list is not used for a number)
                reminders = sum(1 for m in st.outbox if str(m["key"]).startswith(NUDGE_KEY_PREFIX))
                return self._reply(
                    HTTP_OK, {"mode": st.outbox_mode, "count": len(msgs), "reminders": reminders, "messages": msgs}
                )
            if path == "/fake/crm":
                rows = [{"lead_id": r["lead_id"], "record": r["record"]} for r in st.crm]
                return self._reply(HTTP_OK, {"count": len(rows), "rows": rows, "fields": dict(st.crm_fields)})
        self._reply(HTTP_NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        obj = self._json_body()
        if path == "/fake/outbox":
            return self._outbox(obj)
        if path == "/fake/crm":
            return self._crm(obj)
        if path == "/fake/crm/fields":
            return self._crm_fields(obj)
        if path == "/fake/control":
            return self._control(obj)
        if path == "/fake/reset":
            self.state.reset()
            return self._reply(HTTP_OK, {"status": "reset"})
        self._reply(HTTP_NOT_FOUND, {"error": "not_found"})

    def _control(self, obj: Any) -> None:
        st = self.state
        delay = obj.get("delay_ms", 0) if isinstance(obj, dict) else None
        target = obj.get("target", "outbox") if isinstance(obj, dict) else None
        ok = (
            isinstance(obj, dict)
            and obj.get("fail_next") in FAIL_MODES
            and target in TARGETS
            and isinstance(delay, int)
            and not isinstance(delay, bool)
            and delay >= 0
        )
        if not ok:
            return self._reply(HTTP_BAD_REQUEST, {"error": "bad_control"})
        with st.lock:
            st.control[target] = [obj["fail_next"], delay]
        self._reply(HTTP_OK, {"status": "ok"})

    def _take_control(self, target: str) -> tuple[str, int]:
        """The one-shot control of `target`, cleared by taking it."""
        with self.state.lock:
            mode, delay = self.state.control[target]
            self.state.control[target] = ["none", 0]
        return mode, delay

    def _crm(self, obj: Any) -> None:
        st = self.state
        if not (isinstance(obj, dict) and isinstance(obj.get("lead_id"), str) and isinstance(obj.get("record"), dict)):
            return self._reply(HTTP_BAD_REQUEST, {"error": "bad_body"})
        mode, delay = self._take_control("crm")
        if mode == "none" and delay:
            time.sleep(delay / 1000)  # wait BEFORE storing (slow CRM step)
        if mode == "refuse":
            return self._reply(HTTP_UNPROCESSABLE, {"error": "refused", "mode": st.crm_mode})
        if mode == "drop":
            return self._hang_up()
        with st.lock:
            seen = any(r["lead_id"] == obj["lead_id"] for r in st.crm)
            already = st.crm_mode == "keyed" and seen
            if not already:
                st.crm.append({"lead_id": obj["lead_id"], "record": obj["record"]})
        if mode == "record_then_drop":
            return self._hang_up()
        if mode == "store_then_delay":
            time.sleep(delay / 1000)
        self._reply(HTTP_OK, {"status": "already_present" if already else "put", "mode": st.crm_mode})

    def _crm_fields(self, obj: Any) -> None:
        st = self.state
        keys = ("lead_id", "email_key", "owner_id", "status")
        if not (isinstance(obj, dict) and all(isinstance(obj.get(k), str) for k in keys)):
            return self._reply(HTTP_BAD_REQUEST, {"error": "bad_body"})
        mode, delay = self._take_control("crm")
        if mode == "none" and delay:
            time.sleep(delay / 1000)  # wait BEFORE storing (slow CRM step)
        if mode == "refuse":
            return self._reply(HTTP_UNPROCESSABLE, {"error": "refused", "mode": st.crm_mode})
        if mode == "drop":
            return self._hang_up()
        with st.lock:
            if not any(r["lead_id"] == obj["lead_id"] for r in st.crm):
                return self._reply(HTTP_NOT_FOUND, {"error": "no_contact"})
            st.crm_fields[obj["lead_id"]] = {"owner_id": obj["owner_id"], "status": obj["status"]}
        if mode == "record_then_drop":
            return self._hang_up()  # stored, but the adapter sees no answer
        if mode == "store_then_delay":
            time.sleep(delay / 1000)
        self._reply(HTTP_OK, {"status": "set"})

    def _outbox(self, obj: Any) -> None:
        st = self.state
        keys = ("key", "to", "subject", "body")
        if not (isinstance(obj, dict) and all(isinstance(obj.get(k), str) for k in keys)):
            return self._reply(HTTP_BAD_REQUEST, {"error": "bad_body"})
        mode, delay = self._take_control("outbox")  # the control applies to this post only
        out_mode = st.outbox_mode
        if mode == "none" and delay:
            time.sleep(delay / 1000)  # wait BEFORE storing
        if mode == "refuse":
            return self._reply(HTTP_UNPROCESSABLE, {"error": "refused", "mode": out_mode})
        if mode == "drop":
            return self._hang_up()
        with st.lock:
            seen = any(m["key"] == obj["key"] for m in st.outbox)
            already = out_mode == "keyed" and seen
            if not already:
                st.outbox.append({k: obj[k] for k in keys})
        if mode == "record_then_drop":
            return self._hang_up()
        if mode == "store_then_delay":
            time.sleep(delay / 1000)  # store, then wait, then answer
        self._reply(HTTP_OK, {"status": "already_sent" if already else "sent", "mode": out_mode})


class FakesServer(ThreadingHTTPServer):
    daemon_threads = True
    state: FakesState


def make_fakes_server(
    outbox_mode: str = "keyed", port: int = 0, crm_mode: str = "keyed", host: str | None = None
) -> FakesServer:
    """Build (not start) a fakes server; port 0 = ephemeral. Host defaults to Config.fakes_host."""
    state = FakesState(outbox_mode, crm_mode)
    handler = type("Handler", (_Handler,), {"state": state})
    srv = FakesServer((host or Config().fakes_host, port), handler)
    srv.state = state
    return srv


def serve_fakes_in_thread(
    outbox_mode: str = "keyed", port: int = 0, crm_mode: str = "keyed"
) -> tuple[FakesServer, str, threading.Thread]:
    """Start in a daemon thread. Returns (server, base_url, thread). Stop with stop_fakes(server, thread)."""
    srv = make_fakes_server(outbox_mode, port, crm_mode)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    host, bound = srv.server_address[0], srv.server_address[1]
    return srv, f"http://{host}:{bound}", th


def stop_fakes(srv: FakesServer, th: threading.Thread) -> None:
    srv.shutdown()
    srv.server_close()
    th.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    """Parse --outbox keyed|blind and --port (default Config().fakes_port), serve the /fake/* routes until stopped."""
    cfg = Config()
    ap = argparse.ArgumentParser(prog="demo_only.fakes_server")
    ap.add_argument("--outbox", choices=MODES, required=True)
    ap.add_argument("--crm", choices=MODES, default="keyed")
    ap.add_argument("--port", type=int, default=cfg.fakes_port)
    args = ap.parse_args(argv)
    srv = make_fakes_server(args.outbox, args.port, args.crm)
    print(f"fakes server {args.outbox} on port {srv.server_address[1]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
