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
A create checks the email and stores under ONE lock. Every other read or change of `state.contacts` (get, list, search,
patch) also goes through a method of HubspotState that holds the same lock for the whole step and hands out a COPY, so
a handler never touches the shared dict outside the lock (a delete or create from another thread cannot break it).
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
        """Call with the lock held (the handlers use the locked methods below)."""
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

    def take_failure(self) -> bool:
        """True while fewer than `times` faults were answered; the check and the count are one step under the lock."""
        with self.lock:
            if self.failed < self.times:
                self.failed += 1
                return True
            return False

    def take_hang(self) -> bool:
        """True exactly once: the first create that gets here is the one that hangs."""
        with self.lock:
            if self.hung_once:
                return False
            self.hung_once = True
            return True

    # -- the locked "public view": the ONLY way a handler reads or changes the contacts. Each method holds the lock for
    # the whole step (find and copy together) and returns copies, so what the handler then serializes cannot change.
    def _view(self, cid: str) -> dict[str, Any]:
        """Call with the lock held."""
        return {
            **self.contacts[cid],
            "properties": dict(self.contacts[cid]["properties"]),
            "createdAt": "x",
            "updatedAt": "x",
            "archived": False,
        }

    def public(self, ident: str, by_email: bool) -> dict[str, Any] | None:
        """One contact as the API shows it, found by id or by email; None when there is none (or it was deleted)."""
        with self.lock:
            cid = self.by_email(ident) if by_email else ident
            return self._view(cid) if cid in self.contacts else None

    def contacts_page(self, limit: int, after: int) -> tuple[list[dict[str, Any]], str | None]:
        """(rows of one LIST page, the `after` cursor of the next page or None)."""
        with self.lock:
            ids = sorted(self.contacts, key=int)
            rows = [i for i in ids if int(i) > after][:limit]
            more = len(rows) == limit and any(int(i) > int(rows[-1]) for i in ids)
            return [self._view(i) for i in rows], (rows[-1] if more else None)

    def search(self, want: str) -> list[dict[str, Any]]:
        with self.lock:
            now = time.monotonic()
            return [
                self._view(i)
                for i, c in self.contacts.items()
                if c["properties"].get("hadavin_lead_id") == want
                and (self.mode != "search_lag" or now - self.created_at[i] >= self.lag)
            ]

    def patch(self, email: str, props: dict[str, str]) -> dict[str, Any] | None:
        """Change the properties of the contact with this email; the changed contact, or None when there is none."""
        with self.lock:
            cid = self.by_email(email)
            if cid is None:
                return None
            self.contacts[cid]["properties"].update(props)
            return self._view(cid)


MAX_BODY_BYTES = 1_048_576  # a request body above this (or a Content-Length that is not a plain count) is a 400


class _Handler(BaseHTTPRequestHandler):
    state: HubspotState
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def _length(self) -> int | None:
        """The body size the client announced: None for a value that is not a plain count (negative, text) or is above
        MAX_BODY_BYTES (the fake would read it all, or until the client closes). The length of the text is compared
        first: Python refuses `int()` of a digit string of more than 4300 characters, and that error would kill the
        handler thread with no answer."""
        text = self.headers.get("Content-Length") or "0"
        return (
            int(text)
            if len(text) <= len(str(MAX_BODY_BYTES))
            and text.isascii()
            and text.isdigit()
            and int(text) <= MAX_BODY_BYTES
            else None
        )

    def _send(
        self, status: int, body: Any = None, raw: bytes | None = None, headers: dict[str, str] | None = None
    ) -> None:
        if not getattr(self, "_body_read", False):
            # The answer is followed by a close. A server that closes with the request body still unread makes the
            # operating system reset the connection (Windows: ConnectionAbortedError, WinError 10053), so the client
            # sometimes never sees the answer, most often when the machine is busy. Read the body first.
            self._body_read = True
            # None (negative, not a number, above 1 MiB): the 400 is sent WITHOUT reading the body. A deliberate
            # trade-off: reading it would mean reading until the client stops; a client that really sends 1 MiB or
            # more may therefore see a connection reset instead of the 400.
            n = self._length()
            if n:
                self.rfile.read(n)
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
        n = self._length() or 0  # _gate has already refused a length that is not usable
        self._body_read = True
        return json.loads(self.rfile.read(n) or b"null")

    def _gate(self) -> bool:
        st = self.state
        if self._length() is None:
            self._send(400, {"status": "error", "message": "bad Content-Length"})
            return False
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
            limit = min(int((q.get("limit") or ["100"])[0]), st.page_max)
            after = int((q.get("after") or ["0"])[0])
            rows, next_after = st.contacts_page(limit, after)
            self._send(200, {"results": rows, "paging": {"next": {"after": next_after}} if next_after else None})
            return
        if parts.path.startswith(_BASE + "/"):
            ident = unquote(parts.path[len(_BASE) + 1 :])
            found = st.public(ident, q.get("idProperty") == ["email"])
            if found is not None:
                self._send(200, found)
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
        changed = st.patch(unquote(parts.path[len(_BASE) + 1 :]), props)
        if changed is None:
            self._send(404, {"status": "error", "message": "not found"})
        else:
            self._send(200, changed)

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
        hits = st.search(want)
        self._send(200, {"total": len(hits), "results": hits})

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
        if st.mode in ("r429", "r5xx") and st.take_failure():
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
        if st.mode == "timeout_after_create" and st.take_hang():
            st.release.wait(HANG_SECONDS)
            self.close_connection = True
            return
        if st.mode == "r5xx_after_store" and st.take_failure():
            self._send(500, {"status": "error"})
            return
        if st.mode == "slow":
            time.sleep(st.delay)
        if st.mode == "malformed":
            self._send(201, raw=b"<html>not json</html>")
            return
        # a contact deleted by another thread in the meantime still gets its 201 (as a real create that was answered)
        self._send(201, st.public(cid, by_email=False) or {"id": cid, "properties": dict(props)})


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
