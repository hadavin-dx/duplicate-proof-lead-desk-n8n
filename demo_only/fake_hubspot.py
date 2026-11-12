"""A fake HubSpot contacts API for tests (loopback, plain http, in memory). Demo-only code.

Only what crm_hubspot.py calls: create, get by id, get by email (idProperty=email), list, search, archive. Modes
(`state.mode`) decide what a CREATE does:
  ok                    201; a second create with the same email answers 409 with "Existing ID: n"
  dup409                like ok, but the 409 body names no id (the adapter must ask by email)
  slow                  answers 201 after state.delay seconds (the contact is stored first)
  timeout_after_create  stores the contact, then never answers (the first create only; later ones behave as ok)
  r429                  the first state.times creates answer 429 (with Retry-After state.retry_after if set), then ok
  r5xx                  the first state.times creates answer 503 and store nothing, then ok
  r5xx_after_store      the first state.times creates store the contact and answer 500, then ok
  malformed             201 with a body that is not JSON (the contact is stored)
  unauthorized          every call answers 401
  forbidden_400         every create answers 400
  search_lag            ok, but search shows a contact only state.lag seconds after its creation
  list_hang             the LIST (GET of the contacts collection) never answers (until state.release is set)
  redirect              every create answers 302 with Location state.redirect_to (another host)
PATCH (the copy of lead owner and step; a create behaves as ok in these modes; failures count in state.patch_failed):
  patch_r429            the first state.times PATCHes answer 429 (Retry-After state.retry_after if set), then ok
  patch_r5xx            the first state.times PATCHes answer 503 and change nothing, then ok
  patch_400             every PATCH answers 400
A PATCH needs `?idProperty=email` and a body {"properties": {...}} with only hubspot_owner_id / hs_lead_status.
The LIST honours `limit` and `after` (at most state.page_max rows a page, paging.next.after for the next page). DELETE
moves a contact to a recycle bin (`state.bin`): its email stays reserved, so a create answers 409 "Existing ID: n" while
a GET of that id answers 404. That is how this demo stand-in behaves (an assumption,
not taken from HubSpot's documentation).
A create checks the email and stores under ONE lock.
`state.key` is the expected bearer key; a wrong key always answers 401. `state.calls` lists (method, path) of every
request, `state.auth_seen` the Authorization header values seen."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

MODES = (
    "ok",
    "dup409",
    "slow",
    "timeout_after_create",
    "r429",
    "r5xx",
    "r5xx_after_store",
    "malformed",
    "unauthorized",
    "forbidden_400",
    "search_lag",
    "list_hang",
    "redirect",
    "patch_r429",
    "patch_r5xx",
    "patch_400",
)
_PATCH_KEYS = ("hubspot_owner_id", "hs_lead_status")
_BASE = "/crm/v3/objects/contacts"
HANG_SECONDS = 30.0


class HubspotState:
    def __init__(self, key: str, mode: str = "ok") -> None:
        self.lock = threading.Lock()
        self.key = key
        self.mode = mode
        self.delay = 0.0
        self.times = 1
        self.retry_after: str | None = None
        self.lag = 0.0
        self.next_id = 1000
        self.page_max = 100
        self.redirect_to = ""
        self.bin: dict[str, dict[str, Any]] = {}  # archived contacts: gone from reads, email still reserved
        self.contacts: dict[str, dict[str, Any]] = {}
        self.created_at: dict[str, float] = {}
        self.calls: list[tuple[str, str]] = []
        self.auth_seen: list[str] = []
        self.hung_once = False
        # True: an address ending `.test` answers 400 INVALID_EMAIL (a real CRM may do this; not observed here)
        self.reject_test_domain = False
        self.failed = 0
        self.patch_failed = 0  # PATCH faults answered (kept apart from `failed`, which counts create faults)
        self.release = threading.Event()  # set to free hanging requests at test end

    def by_email(self, email: str) -> str | None:
        return next((i for i, c in self.contacts.items() if c["properties"].get("email") == email), None)

    def reserved_by(self, email: str) -> str | None:
        """The id that holds this email, live or in the bin. Call with the lock held."""
        live = self.by_email(email)
        if live is not None:
            return live
        return next((i for i, c in self.bin.items() if c["properties"].get("email") == email), None)

    def create_if_absent(self, props: dict[str, str]) -> tuple[str, bool]:
        """(id, created). One lock covers the email check and the store: two creates cannot both succeed."""
        with self.lock:
            held = self.reserved_by(props.get("email", ""))
            if held is not None:
                return held, False
            cid = str(self.next_id)
            self.next_id += 1
            self.contacts[cid] = {"id": cid, "properties": dict(props)}
            self.created_at[cid] = time.monotonic()
            return cid, True

    def store(self, props: dict[str, str]) -> str:
        return self.create_if_absent(props)[0]


class _Handler(BaseHTTPRequestHandler):
    state: HubspotState
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def _send(
        self, status: int, body: Any = None, raw: bytes | None = None, headers: dict[str, str] | None = None
    ) -> None:
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    def _json(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null")

    def _public(self, cid: str) -> dict[str, Any]:
        return {**self.state.contacts[cid], "createdAt": "x", "updatedAt": "x", "archived": False}

    def _gate(self) -> bool:
        st = self.state
        auth = self.headers.get("Authorization", "")
        with st.lock:
            st.calls.append((self.command, self.path))
            st.auth_seen.append(auth)
        if st.mode == "unauthorized" or auth != "Bearer " + st.key:
            self._send(401, {"status": "error", "message": "bad key", "category": "INVALID_AUTHENTICATION"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._gate():
            return
        parts = urlsplit(self.path)
        q = parse_qs(parts.query)
        st = self.state
        if parts.path == _BASE:
            if st.mode == "list_hang":
                st.release.wait(HANG_SECONDS)
                self.close_connection = True
                return
            ids = sorted(st.contacts, key=int)
            limit = min(int((q.get("limit") or ["100"])[0]), st.page_max)
            after = int((q.get("after") or ["0"])[0])
            page = [i for i in ids if int(i) > after][:limit]
            more = len(page) == limit and any(int(i) > int(page[-1]) for i in ids)
            paging = {"next": {"after": page[-1]}} if more else None
            self._send(200, {"results": [self._public(i) for i in page], "paging": paging})
            return
        if parts.path.startswith(_BASE + "/"):
            ident = unquote(parts.path[len(_BASE) + 1 :])
            cid = st.by_email(ident) if q.get("idProperty") == ["email"] else ident
            if cid in st.contacts:
                self._send(200, self._public(cid))
                return
        self._send(404, {"status": "error", "message": "not found"})

    def do_DELETE(self) -> None:
        if not self._gate():
            return
        cid = self.path.rsplit("/", 1)[-1]
        with self.state.lock:
            gone = self.state.contacts.pop(cid, None)
            if gone is not None:
                self.state.bin[cid] = gone
        if gone is None:
            self._send(404, {"status": "error"})
        else:
            self._send(204)

    def do_PATCH(self) -> None:
        if not self._gate():
            return
        st = self.state
        parts = urlsplit(self.path)
        if not parts.path.startswith(_BASE + "/") or parse_qs(parts.query).get("idProperty") != ["email"]:
            self._send(400, {"status": "error", "message": "idProperty=email is required"})
            return
        try:
            props = self._json()["properties"]
            ok = isinstance(props, dict) and all(k in _PATCH_KEYS and isinstance(v, str) for k, v in props.items())
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            self._send(400, {"status": "error", "message": "bad properties", "category": "VALIDATION_ERROR"})
            return
        if st.mode == "patch_400":
            self._send(400, {"status": "error", "message": "bad request"})
            return
        if st.mode in ("patch_r429", "patch_r5xx"):
            with st.lock:
                fail = st.patch_failed < st.times
                st.patch_failed += int(fail)
            if fail:
                if st.mode == "patch_r429":
                    self._send(429, {"status": "error", "errorType": "RATE_LIMIT"}, headers=_ra(st.retry_after))
                else:
                    self._send(503, {"status": "error"})
                return
        with st.lock:
            cid = st.by_email(unquote(parts.path[len(_BASE) + 1 :]))
            if cid is not None:
                st.contacts[cid]["properties"].update(props)
        if cid is None:
            self._send(404, {"status": "error", "message": "not found"})
        else:
            self._send(200, self._public(cid))

    def do_POST(self) -> None:
        if not self._gate():
            return
        if self.path == _BASE + "/search":
            self._search()
        elif self.path == _BASE:
            self._create()
        else:
            self._send(404, {"status": "error"})

    def _search(self) -> None:
        st = self.state
        want = self._json()["filterGroups"][0]["filters"][0]["value"]
        now = time.monotonic()
        hits = [
            i
            for i, c in st.contacts.items()
            if c["properties"].get("hadavin_lead_id") == want
            and (st.mode != "search_lag" or now - st.created_at[i] >= st.lag)
        ]
        self._send(200, {"total": len(hits), "results": [self._public(i) for i in hits]})

    def _create(self) -> None:
        st = self.state
        props = self._json()["properties"]
        if st.mode == "redirect":
            self._send(302, raw=b"", headers={"Location": st.redirect_to + _BASE})
            return
        if st.reject_test_domain and str(props.get("email", "")).lower().endswith(".test"):
            self._send(
                400, {"status": "error", "message": "Property values were not valid", "category": "VALIDATION_ERROR"}
            )
            return
        if st.mode == "forbidden_400":
            self._send(400, {"status": "error", "message": "bad request"})
            return
        if st.mode in ("r429", "r5xx") and st.failed < st.times:
            st.failed += 1
            if st.mode == "r429":
                self._send(429, {"status": "error", "errorType": "RATE_LIMIT"}, headers=_ra(st.retry_after))
            else:
                self._send(503, {"status": "error"})
            return
        cid, created = st.create_if_absent(props)
        if not created:
            msg = "Contact already exists." + ("" if st.mode == "dup409" else f" Existing ID: {cid}")
            self._send(409, {"status": "error", "message": msg, "category": "CONFLICT"})
            return
        if st.mode == "timeout_after_create" and not st.hung_once:
            st.hung_once = True
            st.release.wait(HANG_SECONDS)
            self.close_connection = True
            return
        if st.mode == "r5xx_after_store" and st.failed < st.times:
            st.failed += 1
            self._send(500, {"status": "error"})
            return
        if st.mode == "slow":
            time.sleep(st.delay)
        if st.mode == "malformed":
            self._send(201, raw=b"<html>not json</html>")
            return
        self._send(201, self._public(cid))


def _ra(v: str | None) -> dict[str, str]:
    return {} if v is None else {"Retry-After": v}


class FakeHubspot(ThreadingHTTPServer):
    daemon_threads = True


def serve(state: HubspotState, host: str, port: int = 0) -> tuple[FakeHubspot, threading.Thread, str]:
    """Start in a thread; returns (server, thread, base url)."""
    handler = type("H", (_Handler,), {"state": state})
    srv = FakeHubspot((host, port), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, th, f"http://{host}:{srv.server_address[1]}"


def stop(srv: FakeHubspot, th: threading.Thread, state: HubspotState) -> None:
    state.release.set()
    srv.shutdown()
    srv.server_close()
    th.join(5)
