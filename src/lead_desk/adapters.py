"""HTTP adapters for the fakes server. Standard library only.

The opener has no proxy handler, so a laptop proxy setting cannot change local errors or successes. The base URL is
built by Config from host and port with the http scheme only, so the scheme audit (S310 / B310) is met by
construction."""

from __future__ import annotations

import itertools
import json
import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Literal, Mapping

from lead_desk.constants import HTTP_BAD_REQUEST, HTTP_INTERNAL_ERROR, HTTP_REQUEST_TIMEOUT, REPLY_MAX_BYTES
from lead_desk.contract import (
    OutboxModeMismatch,
    SendAmbiguous,
    SendNoDescriptors,
    SendRefused,
    SendUnreachable,
    is_fd_exhaustion,
)

# Order of calls: a strictly increasing number, never a clock. A coarse or frozen clock gives two calls the same
# reading, and a late answer of an older call would then overwrite a newer one. next() on itertools.count is atomic.
_SEQ = itertools.count(1)


def _next_seq() -> int:
    return next(_SEQ)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx answer is an error here, never followed: a redirected POST would turn into a GET (the write would be
    lost without a word) and a redirect could leave the base URL."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class FakesUnreachable(RuntimeError):
    """HttpOutbox cannot read GET /fake/info."""


class CrmError(RuntimeError):
    """The CRM call failed or answered something unexpected (the desk records it as internal:CrmError). It may or may
    not have stored the row: the desk treats it as unclear unless `nothing_stored` is true."""

    nothing_stored = False


class CrmNothingStored(CrmError):
    """The CRM call failed in a way that proves the CRM stored nothing (refused with a 4xx answer, or not reached)."""

    nothing_stored = True


def _exchange(url: str, obj: Mapping[str, Any] | None, timeout: float) -> dict[str, Any]:
    """GET (obj None) or POST JSON; returns the parsed JSON object of a 2xx reply. Raises the urllib errors raw."""
    data = None if obj is None else json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if obj is None else "POST")  # noqa: S310  # nosec B310
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=timeout) as r:  # noqa: S310  # nosec B310
        raw = r.read(REPLY_MAX_BYTES + 1)
        if len(raw) > REPLY_MAX_BYTES:
            raise ValueError("reply too long")
        reply = json.loads(raw.decode("utf-8"))
    if not isinstance(reply, dict):
        raise ValueError("reply is not a JSON object")
    return reply


class HttpCrm:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._base = base_url.rstrip("/")
        self._url = self._base + "/fake/crm"
        self._timeout = timeout
        self._capability: Literal["keyed", "blind"] | None = None
        self._cap_lock = threading.Lock()
        self._cap_started = 0  # order number of the call whose answer is stored

    def _remember(self, value: Literal["keyed", "blind"] | None, started: int) -> None:
        """Store the mode named by a call that STARTED at `started`, unless a call that started later has stored one
        already: a late answer of an abandoned call must not put an old mode back."""
        with self._cap_lock:
            if started >= self._cap_started:
                self._cap_started = started
                self._capability = value

    def refresh(self) -> None:
        """Read the CRM's mode from GET /fake/info (key `crm_capability`) so a restarted desk, or a CRM restarted in
        the other mode, is known before the first put. Raises CrmError when it cannot be read (the old value is
        forgotten: None counts as not keyed) or when the answer names no mode (the last good value stays)."""
        started = _next_seq()
        try:
            info = _exchange(self._base + "/fake/info", None, self._timeout)
            cap = info["crm_capability"]
        except Exception as e:
            self._remember(None, started)  # an old answer is not kept: the mode is unknown until it is read again
            raise CrmError(type(e).__name__) from e
        if cap not in ("keyed", "blind"):
            raise CrmError("unexpected /fake/info")
        self._remember("keyed" if cap == "keyed" else "blind", started)

    def forget_capability(self) -> None:
        self._remember(None, _next_seq())

    @property
    def capability(self) -> Literal["keyed", "blind"] | None:
        """The CRM's mode as named in its last good answer; None until it has answered once (the desk then treats
        it as not keyed)."""
        return self._capability

    def put(self, lead_id: str, record: Mapping[str, str]) -> Literal["PUT", "ALREADY_PRESENT"]:
        started = _next_seq()
        try:
            reply = _exchange(self._url, {"lead_id": lead_id, "record": dict(record)}, self._timeout)
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code != HTTP_REQUEST_TIMEOUT and HTTP_BAD_REQUEST <= code < HTTP_INTERNAL_ERROR:
                raise CrmNothingStored(f"http {code}") from e
            raise CrmError(f"http {code}") from e
        except urllib.error.URLError as e:
            if is_fd_exhaustion(e) or isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
                raise CrmNothingStored(type(e.reason).__name__) from e  # no connection could be made: nothing stored
            raise CrmError(type(e.reason).__name__) from e
        except Exception as e:
            if is_fd_exhaustion(e):
                raise CrmNothingStored(type(e).__name__) from e
            raise CrmError(type(e).__name__) from e
        mode = reply.get("mode")
        if mode in ("keyed", "blind"):
            self._remember("keyed" if mode == "keyed" else "blind", started)
        if reply.get("status") == "put":
            return "PUT"
        if reply.get("status") == "already_present":
            return "ALREADY_PRESENT"
        raise CrmError("unexpected reply")

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        body = {"lead_id": lead_id, "email_key": email_key, "owner_id": crm_owner_id, "status": crm_status}
        try:
            reply = _exchange(self._base + "/fake/crm/fields", body, self._timeout)
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code != HTTP_REQUEST_TIMEOUT and HTTP_BAD_REQUEST <= code < HTTP_INTERNAL_ERROR:
                raise CrmNothingStored(f"http {code}") from e
            raise CrmError(f"http {code}") from e
        except urllib.error.URLError as e:
            if is_fd_exhaustion(e) or isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):
                raise CrmNothingStored(type(e.reason).__name__) from e
            raise CrmError(type(e.reason).__name__) from e
        except Exception as e:
            if is_fd_exhaustion(e):
                raise CrmNothingStored(type(e).__name__) from e
            raise CrmError(type(e).__name__) from e
        if reply.get("status") != "set":
            raise CrmError("unexpected reply")


class HttpOutbox:
    def __init__(self, base_url: str, timeout: float) -> None:
        """Reads GET /fake/info; raises FakesUnreachable if it cannot. refresh() reads it again."""
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._cm: tuple[Literal["keyed", "blind"], str] = ("keyed", "keyed")  # (capability, mode): swapped as one
        self._cm_lock = threading.Lock()
        self._cm_started = 0  # order number of the read whose answer is stored
        self.refresh()

    def refresh(self) -> None:
        """Read GET /fake/info again (the sender can be restarted in the other mode while the desk runs). On any
        failure raises FakesUnreachable and keeps the values from the last good read."""
        started = _next_seq()
        try:
            info = _exchange(self._base + "/fake/info", None, self._timeout)
            cap, mode = info["outbox_capability"], info["mode"]
        except Exception as e:
            raise FakesUnreachable(type(e).__name__) from e
        if cap not in ("keyed", "blind") or mode not in ("keyed", "blind"):
            raise FakesUnreachable("unexpected /fake/info")
        with self._cm_lock:
            if started >= self._cm_started:  # a late answer of an abandoned read must not put an old mode back
                self._cm_started = started
                self._cm = ("keyed" if cap == "keyed" else "blind", mode)  # one assignment: no half-written pair

    @property
    def capability(self) -> Literal["keyed", "blind"]:
        return self._cm[0]

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Literal["SENT", "ALREADY_SENT"]:
        """Errors: see contract.md. HTTPError is a URLError subclass, so it is caught first."""
        payload = {"key": send_key, "to": to, "subject": subject, "body": body}
        try:
            reply = _exchange(self._base + "/fake/outbox", payload, timeout)
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code != HTTP_REQUEST_TIMEOUT and HTTP_BAD_REQUEST <= code < HTTP_INTERNAL_ERROR:
                raise SendRefused(f"http {code}") from e
            raise SendAmbiguous(f"http {code}") from e
        except urllib.error.URLError as e:
            if is_fd_exhaustion(e):  # no connection could be made: nothing was sent
                raise SendNoDescriptors(type(e.reason).__name__) from e
            if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror)):  # nothing was sent
                raise SendUnreachable(type(e.reason).__name__) from e
            raise SendAmbiguous(type(e.reason).__name__) from e
        except Exception as e:  # timeouts, resets, unreadable reply: request bytes may have left
            if is_fd_exhaustion(e):
                raise SendNoDescriptors(type(e).__name__) from e
            raise SendAmbiguous(type(e).__name__) from e
        expected_mode = self._cm[1]
        if reply.get("mode") != expected_mode:
            raise OutboxModeMismatch(f"mode {reply.get('mode')!r}, expected {expected_mode!r}")
        if reply.get("status") == "sent":
            return "SENT"
        if reply.get("status") == "already_sent":
            return "ALREADY_SENT"
        raise SendAmbiguous("unexpected reply")
