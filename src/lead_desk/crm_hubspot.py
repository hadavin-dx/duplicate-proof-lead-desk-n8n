"""The CRM kind over a real HubSpot account (free plan), standard library only. Made-up leads only.

Everything this file says about how HubSpot answers comes from HubSpot's documentation and public posts as read on
2026-11-10; the adapter was exercised against the demo stand-in only, so each such statement is an assumption.

The adapter is KEYED: asking again with the same lead id is harmless. HubSpot has no idempotency key, so the key is
the pair (email, the property `hadavin_lead_id` this adapter sets):
  put = create one contact; on 409 ("a contact with this email exists") read that contact back by id and compare its
        `hadavin_lead_id` with this lead's id: equal -> ALREADY_PRESENT (this is also how a create whose answer was
        lost is recognised on the retry), different or empty -> CrmEmailTaken (stored nothing, a person looks).
  Search is NOT used to decide a put: according to HubSpot's search documentation ("It may take a few moments for
  newly created or updated CRM objects to appear in search results", read 2026-11-10) a new
  record can take some time to show in search, so a search could say "absent" for a contact that exists. It is used
  only to count (wait_visible).

Error table (what the desk does with it: see Desk._crm_step):
  stored nothing (CrmNothingStored, the desk resets its mark and asks again later): not reached (refused, name
      lookup, certificate), 4xx other than 408/409 (CrmRefused: 400/401/403/404/422...), 429 after the bounded waits
      (CrmBusy), key file missing/empty (CrmConfigError), call cap reached (CrmCapReached), email taken by another lead.
  unclear (CrmError, the contact may exist; a keyed CRM is simply asked again): timeout, hang-up, 5xx, 408, a reply
      that is not the expected JSON.
The key is read from a file at every call and never logged, never in an error text, never in the file path text.
Only the contacts endpoints are used (read, create, search, list, archive); no property is ever created.
Every request is bounded as a WHOLE by a wall-clock budget (settings.timeout_seconds, at most the desk's call
deadline): the request (name lookup, connect, send, reading the answer) runs in a worker thread that the caller waits
for at most that long (`_once`). A worker that outlives it is abandoned: it ends on its own when its socket times out
(each read is also given only what is left of the budget), and its late answer is thrown away (a create that arrived
late is found again by the next put through the 409 check). One put shares one budget over its calls (the create and
the read-back after a 409); every read-back call has its own budget of the same size and the whole listing has one
total budget (LIST_TOTAL_SECONDS).
The base address must be an https host of the vendor's own domains (HUBSPOT_HOST_SUFFIXES in constants.py),
unless `allow_any_base_url` is set on purpose
(a local stand-in or a proxy of your own): the service key is sent to that host.
The per-run call cap lives in this process's memory: a restarted desk process starts with a fresh cap (a run script
that restarts the desk counts each process on its own and states the total).
Recycle bin (an ASSUMPTION: no statement of it was found in HubSpot's documentation on 2026-11-10,
and it was not exercised against a real account; public community posts describe a create that answers 409 with an
existing id while a plain read of that id answers 404): an ARCHIVED contact is taken to keep its email
reserved. That case is raised as CrmConflictUnreadable (unclear, loud): a person restores or
permanently deletes the contact in HubSpot, then the desk asks again.
Thread bound: an abandoned worker lives until its own socket times out (at most the request budget plus the
per-read timeout), so under a hang the number of live workers can exceed the number of callers; it is not counted or
capped here. The desk's descriptor budget (constants.py FD_RESERVE) must leave room for about three times its CRM
callers' worth of sockets; the per-run call cap bounds the total number of workers ever started.
Counters (`stats()`): what this adapter made, counted on the desk's side of the wire.
Copy of lead owner and lead stage (`set_fields`): PATCH on contacts by email is new; `/crm/v3/owners` is not used by
the desk (the probe lists owners once). The copy may use at most floor(max_calls * MIRROR_CALL_SHARE) calls of the run
(every PATCH attempt counts, 429 retries included), so the creates keep the rest; a PATCH past that share raises
CrmCapReached before anything is sent and is not counted."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, NoReturn
from urllib.parse import quote

from lead_desk.adapters import _OPENER, CrmError, CrmNothingStored
from lead_desk.constants import (
    CALL_DEADLINE_MAX_SECONDS,
    CAP_COUNT_MAX,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_INTERNAL_ERROR,
    HTTP_REQUEST_TIMEOUT,
    HTTP_TOO_MANY_REQUESTS,
    HUBSPOT_BASE_URL,
    HUBSPOT_HOST_SUFFIXES,
    LIVE_KEY_MIN_CHARS,
    MIRROR_CALL_SHARE,
    REPLY_MAX_BYTES,
)
from lead_desk.envnum import SettingRefused, env_number, split_url
from lead_desk.run_stats import write_stats

LEAD_ID_PROPERTY: Final = "hadavin_lead_id"  # created by hand in the CRM account; this module never creates one
CONTACTS_PATH: Final = "/crm/v3/objects/contacts"
SEARCH_PATH: Final = CONTACTS_PATH + "/search"
DEFAULT_TIMEOUT_SECONDS: Final = 4.5
DEFAULT_429_RETRIES: Final = 2
BACKOFF_FIRST_SECONDS: Final = 0.5  # used when a 429 names no Retry-After
LIST_PAGE: Final = 100
LIST_PAGES_MAX: Final = 50
LIST_TOTAL_SECONDS: Final = 120.0  # the whole read-back listing, however many pages
SECRET_FILE_MAX_BYTES: Final = 4096
SEARCH_SPACING_SECONDS: Final = (
    0.25  # HubSpot's documentation (read 2026-11-10) gives the search API 5 requests a second
)
_LEAD_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_EXISTING_ID = re.compile(r"Existing ID: (\d{1,20})")
_HTTP_OK_MAX: Final = 299
_HTTP_OK_MIN: Final = 200
_HTTP_CREATED: Final = 201
_HTTP_NO_CONTENT: Final = 204
_HTTP_NOT_FOUND: Final = 404
_BAD_JSON: Final = object()  # a reply body that is not JSON: never a dict, so it is treated as unexpected


class CrmRefused(CrmNothingStored):
    """HubSpot answered a 4xx (not 408, 409, 429): the request was refused, nothing was stored."""


class CrmBusy(CrmNothingStored):
    """HubSpot answered 429 and the bounded waits are used up. `retry_after` is what it named (None if nothing)."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("hubspot said: too many requests")
        self.retry_after = retry_after


class CrmConfigError(CrmNothingStored):
    """The key file cannot be used (missing, unreadable, empty). The text never holds the path."""


class CrmCapReached(CrmNothingStored):
    """The per-run call cap is used up; nothing was sent."""


class CrmConflictUnreadable(CrmError):
    """HubSpot says a contact with this email exists (409) but that contact cannot be read (404): it is most likely in
    the recycle bin. Unclear on purpose; restore or permanently delete it in HubSpot, then ask again."""


class CrmEmailTaken(CrmNothingStored):
    """A contact with this email exists and is not this lead's (its lead id differs or is empty): nothing was stored."""


@dataclass(frozen=True)
class HubspotSettings:
    key_file: Path = field(
        repr=False
    )  # one line: the service key; read at every call, never an argument, never printed
    max_calls: int  # hard cap of calls this adapter may make in one run (no default: the caller states it)
    base_url: str = HUBSPOT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS  # total budget of one put
    max_429_retries: int = DEFAULT_429_RETRIES
    stats_file: Path | None = None  # counters are written here after every change (run scripts read it)
    allow_any_base_url: bool = False  # on purpose only: lets the key go to a host outside the vendor's own domains

    def __post_init__(self) -> None:
        if type(self.max_calls) is not int or not 1 <= self.max_calls <= CAP_COUNT_MAX:
            raise SettingRefused(f"DESK_HUBSPOT_MAX_CALLS must be a whole number between 1 and {CAP_COUNT_MAX}")
        if not (math.isfinite(self.timeout_seconds) and 0 < self.timeout_seconds <= CALL_DEADLINE_MAX_SECONDS):
            raise SettingRefused("DESK_HUBSPOT_TIMEOUT_SECONDS must be above 0 and at most the desk's call deadline")
        if type(self.max_429_retries) is not int or not 0 <= self.max_429_retries <= DEFAULT_429_RETRIES + 3:
            raise SettingRefused("DESK_HUBSPOT_MAX_429_RETRIES must be a small whole number")
        parts = split_url("DESK_HUBSPOT_BASE_URL", self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/"):
            raise SettingRefused("DESK_HUBSPOT_BASE_URL must be an http(s) address with a host and no path")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise SettingRefused("DESK_HUBSPOT_BASE_URL must hold no query, fragment or login")
        if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise SettingRefused("DESK_HUBSPOT_BASE_URL: plain http is allowed for a loopback address only")
        host = (parts.hostname or "").lower()
        if not self.allow_any_base_url and not (
            parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in HUBSPOT_HOST_SUFFIXES)
        ):
            raise SettingRefused("DESK_HUBSPOT_BASE_URL must be an https address at one of the vendor's own domains")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> HubspotSettings:
        """DESK_HUBSPOT_KEY_FILE and DESK_HUBSPOT_MAX_CALLS are required; DESK_HUBSPOT_BASE_URL,
        DESK_HUBSPOT_TIMEOUT_SECONDS, DESK_HUBSPOT_MAX_429_RETRIES,
        DESK_HUBSPOT_STATS_FILE, DESK_HUBSPOT_ALLOW_ANY_BASE_URL (the word yes) are optional. A bad value raises
        ValueError (naming the setting, never its value)."""
        for n in ("DESK_HUBSPOT_KEY_FILE", "DESK_HUBSPOT_MAX_CALLS"):
            if not env.get(n, "").strip():
                raise SettingRefused(f"{n} is required for the hubspot CRM")
        kw: dict[str, Any] = {}
        if "DESK_HUBSPOT_BASE_URL" in env:
            kw["base_url"] = env["DESK_HUBSPOT_BASE_URL"]
        if "DESK_HUBSPOT_TIMEOUT_SECONDS" in env:
            kw["timeout_seconds"] = env_number(
                env, "DESK_HUBSPOT_TIMEOUT_SECONDS", float, high=CALL_DEADLINE_MAX_SECONDS
            )
        if "DESK_HUBSPOT_MAX_429_RETRIES" in env:
            kw["max_429_retries"] = env_number(
                env, "DESK_HUBSPOT_MAX_429_RETRIES", int, low=0, high=DEFAULT_429_RETRIES + 3
            )
        if env.get("DESK_HUBSPOT_ALLOW_ANY_BASE_URL", "").strip().lower() == "yes":
            kw["allow_any_base_url"] = True
        if env.get("DESK_HUBSPOT_STATS_FILE"):
            kw["stats_file"] = Path(env["DESK_HUBSPOT_STATS_FILE"])
        return cls(
            Path(env["DESK_HUBSPOT_KEY_FILE"]),
            env_number(env, "DESK_HUBSPOT_MAX_CALLS", int, low=1, high=CAP_COUNT_MAX),
            **kw,
        )


class _Reply:
    __slots__ = ("body", "retry_after", "status")

    def __init__(self, status: int, body: Any, retry_after: float | None) -> None:
        self.status, self.body, self.retry_after = status, body, retry_after


def _retry_after(raw: str | None) -> float | None:
    try:
        v = float(raw) if raw is not None else None
    except ValueError:
        return None
    return v if v is not None and 0 <= v < float("inf") else None


def _split_name(name: str) -> tuple[str, str]:
    first, _, last = name.strip().partition(" ")
    return first.strip(), last.strip()


def _arm(r: Any, seconds: float) -> None:
    """Set the socket timeout of an open response to `seconds` (best effort: the response keeps its own timeout when
    the socket cannot be reached)."""
    try:
        r.fp.raw._sock.settimeout(seconds)  # noqa: SLF001
    except (AttributeError, OSError, ValueError):
        pass


class HubspotCrm:
    """Implements contract.Crm; capability "keyed". Thread-safe (counters under a lock, one request per call)."""

    def __init__(self, settings: HubspotSettings) -> None:
        self.s = settings
        self._lock = threading.Lock()
        self._n = dict.fromkeys(
            ("calls", "creates", "created", "conflicts", "gets", "searches", "archives", "rate_limited", "patch"), 0
        )

    @property
    def capability(self) -> Literal["keyed", "blind"]:
        return "keyed"

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._n)

    # -- the wire -----------------------------------------------------------------------------------------------

    def _key(self) -> str:
        try:
            with self.s.key_file.open("rb") as f:
                raw = f.read(SECRET_FILE_MAX_BYTES + 1)
        except OSError:
            raise CrmConfigError("the hubspot key file cannot be read") from None  # the OS error holds the path
        if len(raw) > SECRET_FILE_MAX_BYTES:
            raise CrmConfigError("the hubspot key file is too large: it must hold only the key")
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise CrmConfigError("the hubspot key file is UTF-16 text: save it as plain UTF-8 text")
        try:
            key = raw.decode("utf-8-sig").strip()
        except UnicodeError:
            raise CrmConfigError("the hubspot key file is not UTF-8 text: save it as plain UTF-8 text") from None
        if "\n" in key or "\r" in key:
            raise CrmConfigError("the hubspot key file has more than one line: it must hold only the key")
        if len(key) < LIVE_KEY_MIN_CHARS or any(c.isspace() or not c.isprintable() for c in key):
            raise CrmConfigError("the hubspot key file holds no key")
        return key

    def _count(self, *names: str) -> None:
        with self._lock:
            for n in names:
                self._n[n] += 1
            write_stats(self.s.stats_file, self._n)

    def _once(self, method: str, path: str, obj: Any, deadline: float, key: str) -> _Reply:
        """One request, bounded as a whole: it runs in a daemon worker thread and the caller waits at most `left`."""
        left = min(deadline - time.monotonic(), self.s.timeout_seconds)  # one request never outlives the budget size
        if left <= 0:
            raise CrmError("TimeoutError")
        deadline = min(deadline, time.monotonic() + left)
        data = None if obj is None else json.dumps(obj).encode("utf-8")
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(  # noqa: S310  # nosec B310  (scheme checked in HubspotSettings)
            self.s.base_url.rstrip("/") + path, data=data, method=method, headers=headers
        )
        box: list[tuple[bool, Any]] = []

        def work() -> None:
            try:
                box.append((True, self._request(req, left, deadline)))
            except BaseException as e:  # noqa: BLE001  (handed to the caller, which re-raises it)
                box.append((False, e))

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(left)
        if not box:
            raise CrmError("TimeoutError")  # abandoned; its late answer goes nowhere
        ok, value = box[0]
        if not ok:
            raise value
        return value  # type: ignore[no-any-return]

    def _request(self, req: urllib.request.Request, left: float, deadline: float) -> _Reply:
        try:
            with _OPENER.open(req, timeout=left) as r:  # noqa: S310  # nosec B310
                return _Reply(r.status, self._read(r, deadline), None)
        except urllib.error.HTTPError as e:
            ra = _retry_after(e.headers.get("Retry-After"))
            try:
                body = self._read(e, deadline)
            except (CrmError, OSError, http.client.HTTPException):
                body = None
            finally:
                e.close()
            return _Reply(e.code, body, ra)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)):
                raise CrmNothingStored(type(e.reason).__name__) from None  # no request reached HubSpot
            raise CrmError(type(e.reason).__name__) from None
        except CrmError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise CrmError(type(e).__name__) from None

    @staticmethod
    def _read(r: Any, deadline: float) -> Any:
        raw = b""
        while len(raw) <= REPLY_MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CrmError("TimeoutError")
            _arm(r, remaining)  # each read gets only what is left of the TOTAL budget
            chunk = r.read1(4096)
            if not chunk:
                break
            raw += chunk
        if len(raw) > REPLY_MAX_BYTES:
            raise CrmError("reply too long")
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return _BAD_JSON

    def _call(self, kind: str, method: str, path: str, obj: Any, deadline: float) -> _Reply:
        """One request, with the bounded waits on 429. `kind` names the counter. The call cap is checked first."""
        key = self._key()
        waits = 0
        while True:
            with self._lock:
                if self._n["calls"] >= self.s.max_calls:
                    raise CrmCapReached("the hubspot call cap is used up")
                if kind == "patch" and self._n["patch"] >= int(self.s.max_calls * MIRROR_CALL_SHARE):
                    raise CrmCapReached("the CRM copy share is used up")
                self._n["calls"] += 1
                self._n[kind] += 1
                write_stats(self.s.stats_file, self._n)
            reply = self._once(method, path, obj, deadline, key)
            if reply.status != HTTP_TOO_MANY_REQUESTS:
                return reply
            self._count("rate_limited")
            delay = reply.retry_after if reply.retry_after is not None else BACKOFF_FIRST_SECONDS * 2**waits
            if waits >= self.s.max_429_retries or time.monotonic() + delay >= deadline:
                raise CrmBusy(reply.retry_after)
            waits += 1
            time.sleep(delay)

    # -- the Crm contract ---------------------------------------------------------------------------------------

    def put(self, lead_id: str, record: Mapping[str, str]) -> Literal["PUT", "ALREADY_PRESENT"]:
        if not _LEAD_ID.fullmatch(lead_id):
            raise CrmRefused("lead id has characters outside the allowed set")
        email = str(record.get("email", "")).strip().lower()
        if not email or "@" not in email:
            raise CrmRefused("record has no email")
        first, last = _split_name(str(record.get("name", "")))
        props = {"email": email, LEAD_ID_PROPERTY: lead_id}
        for k, v in (("firstname", first), ("lastname", last), ("company", str(record.get("company", "")).strip())):
            if v:
                props[k] = v
        deadline = time.monotonic() + self.s.timeout_seconds
        reply = self._call("creates", "POST", CONTACTS_PATH, {"properties": props}, deadline)
        if reply.status == _HTTP_CREATED and isinstance(reply.body, dict) and reply.body.get("id"):
            self._count("created")
            return "PUT"
        if reply.status == HTTP_CONFLICT:
            self._count("conflicts")
            return self._confirm_existing(lead_id, email, reply.body, deadline)
        self._fail(reply)
        raise CrmError("unexpected reply")  # a 2xx that is not a created contact: unclear

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        email = str(email_key).strip().lower()
        if not email or "@" not in email:
            raise CrmRefused("no email")
        props = {"hs_lead_status": crm_status}
        if crm_owner_id:
            props["hubspot_owner_id"] = crm_owner_id
        path = f"{CONTACTS_PATH}/{quote(email, safe='')}?idProperty=email"
        reply = self._call("patch", "PATCH", path, {"properties": props}, time.monotonic() + self.s.timeout_seconds)
        if reply.status == _HTTP_OK_MIN:
            return  # the body is never read: a 200 already says the change was applied
        self._fail(reply)  # 404 and the other 4xx: CrmRefused; 5xx and 408: CrmError; any other 2xx: unclear
        raise CrmError("unexpected reply")

    def _fail(self, reply: _Reply) -> NoReturn:
        if HTTP_BAD_REQUEST <= reply.status < HTTP_INTERNAL_ERROR and reply.status != HTTP_REQUEST_TIMEOUT:
            raise CrmRefused(f"hubspot answered {reply.status}")
        raise CrmError(f"http {reply.status}" if reply.status >= HTTP_INTERNAL_ERROR else "unexpected reply")

    def _confirm_existing(
        self, lead_id: str, email: str, body: Any, deadline: float
    ) -> Literal["PUT", "ALREADY_PRESENT"]:
        """409: read the contact that holds this email (by the id HubSpot named, else by email) and compare ids."""
        text = json.dumps(body) if isinstance(body, (dict, list)) else ""
        m = _EXISTING_ID.search(text)
        sel = (
            f"/{m.group(1)}?properties=email,{LEAD_ID_PROPERTY}"
            if m
            else f"/{quote(email, safe='')}?idProperty=email&properties=email,{LEAD_ID_PROPERTY}"
        )
        got = self._call("gets", "GET", CONTACTS_PATH + sel, None, deadline)
        if got.status == _HTTP_NOT_FOUND and m:  # the named id was wrong: ask by email once
            got = self._call(
                "gets",
                "GET",
                f"{CONTACTS_PATH}/{quote(email, safe='')}?idProperty=email&properties=email,{LEAD_ID_PROPERTY}",
                None,
                deadline,
            )
        if not (_HTTP_OK_MIN <= got.status <= _HTTP_OK_MAX):
            if got.status == _HTTP_NOT_FOUND:
                raise CrmConflictUnreadable(  # unclear: HubSpot says it exists, nobody can read it (recycle bin)
                    "a contact with this email exists but cannot be read (recycle bin?): restore or delete it"
                )
            self._fail(got)
        props = got.body.get("properties") if isinstance(got.body, dict) else None
        if not isinstance(props, dict):
            raise CrmError("unexpected reply")
        if props.get(LEAD_ID_PROPERTY) == lead_id and str(props.get("email", "")).strip().lower() == email:
            return "ALREADY_PRESENT"  # the same lead id AND the same email: this lead's own contact
        raise CrmEmailTaken("a contact with this email exists under another lead id or another email")

    # -- read-back and clean-up, used by the run script (never by the desk) ------------------------------------------

    def rows_with_prefix(self, prefix: str) -> list[tuple[str, str]]:
        """[(lead id, contact id)] of every contact whose lead id starts with `prefix` (a lead id twice shows twice),
        read through the LIST endpoint (the object store, not the search index). Bounded: LIST_PAGES_MAX pages."""
        out: list[tuple[str, str]] = []
        after = ""
        deadline = time.monotonic() + min(self.s.timeout_seconds * LIST_PAGES_MAX, LIST_TOTAL_SECONDS)
        for _ in range(LIST_PAGES_MAX):
            q = f"?limit={LIST_PAGE}&properties={LEAD_ID_PROPERTY}" + (
                f"&after={quote(after, safe='')}" if after else ""
            )
            r = self._call("gets", "GET", CONTACTS_PATH + q, None, deadline)
            if r.status != _HTTP_OK_MIN or not isinstance(r.body, dict):
                self._fail(r)
            for row in r.body.get("results", []):
                lid = (row.get("properties") or {}).get(LEAD_ID_PROPERTY) or ""
                if lid.startswith(prefix):
                    out.append((lid, str(row["id"])))
            after = (((r.body.get("paging") or {}).get("next")) or {}).get("after") or ""
            if not after:
                return out
        raise CrmError("too many pages")

    def wait_visible(self, lead_id: str, seconds: float) -> float | None:
        """Seconds until the search index shows this lead id (polled with a backoff), None if not within `seconds`.
        For measuring the index lag only; never used to decide a put."""
        if not _LEAD_ID.fullmatch(lead_id):
            raise CrmRefused("lead id has characters outside the allowed set")
        t0 = time.monotonic()
        body = {"filterGroups": [{"filters": [{"propertyName": LEAD_ID_PROPERTY, "operator": "EQ", "value": lead_id}]}]}
        wait = SEARCH_SPACING_SECONDS
        while True:
            r = self._call("searches", "POST", SEARCH_PATH, {**body, "limit": 1}, t0 + seconds + self.s.timeout_seconds)
            if r.status != _HTTP_OK_MIN or not isinstance(r.body, dict):
                self._fail(r)
            if r.body.get("total"):
                return time.monotonic() - t0
            if time.monotonic() - t0 + wait > seconds:
                return None
            time.sleep(wait)
            wait = min(wait * 2, 4.0)

    def archive(self, contact_id: str) -> None:
        """Archive (delete into the recycle bin) one contact by id."""
        if not contact_id.isdigit():
            raise CrmRefused("contact id is not a number")
        r = self._call(
            "archives", "DELETE", f"{CONTACTS_PATH}/{contact_id}", None, time.monotonic() + self.s.timeout_seconds
        )
        if r.status not in (_HTTP_NO_CONTENT, _HTTP_NOT_FOUND):
            self._fail(r)


def make_crm(config: object) -> HubspotCrm:
    """Entry point the loader calls when the CRM is "hubspot": settings come from the DESK_HUBSPOT_* environment.
    The time budget of one put never exceeds the desk's own send timeout (config.send_timeout_seconds) or the call
    deadline."""
    s = HubspotSettings.from_env(os.environ)
    limit = min(float(getattr(config, "send_timeout_seconds", CALL_DEADLINE_MAX_SECONDS)), CALL_DEADLINE_MAX_SECONDS)
    return HubspotCrm(replace(s, timeout_seconds=limit) if s.timeout_seconds > limit else s)
