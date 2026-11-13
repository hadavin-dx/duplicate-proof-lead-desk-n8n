"""Demo extension of the portal: the "Demo controls" panel and its three calls. Demo-only code.

The desk reaches this module only through lead_desk.loader (load_demo_portal -> make). Deleting demo_only/ must
leave the product portal working. Calls, all answered as (status, JSON object):

  GET  state   what the made-up sender and CRM hold, how often the pre-written AI was asked,
                whether a crash can be armed
  POST burst   n parallel requests to the desk's own /leads (1 to BURST_MAX), same person, numbered request numbers
  POST crash   arm or clear the one-shot crash flag (allowlisted point, fixed file path from the launcher)

Made-up data only. No secrets. `state` and `crash` never touch the lead book. `burst` DOES write to the lead book:
it posts made-up leads to the desk's own /leads address, exactly as an outside caller would. Every call except
`state` (which reports simulated:false) is refused with 409 demo_not_wired unless the desk really runs on the
made-up CRM, sender and recorded AI reachable on loopback only."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from demo_only.burst import post_with_retries, retry_after_seconds
from demo_only.fakes import RecordedQualifier
from lead_desk.adapters import HttpCrm, HttpOutbox
from lead_desk.constants import (
    BURST_RETRY_DEADLINE_SECONDS,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_METHOD_NOT_ALLOWED,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_TOO_MANY_REQUESTS,
    REPLY_MAX_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    SOURCES,
)
from lead_desk.desk import Desk
from lead_desk.hooks import HOOK_POINTS, FlagFileHooks
from lead_desk.portal_api import connect_readonly

ASSET_TYPES: Final = {
    "portal_demo.js": "text/javascript; charset=utf-8",
    "portal_demo.css": "text/css; charset=utf-8",
}
ROWS_MAX: Final = 200  # most messages and CRM rows shown in `state`
BURST_MIN: Final = 1
BURST_MAX: Final = 20  # most parallel requests of one burst
PERSON_MIN: Final = 1
CLEAR_POINT: Final = "none"  # the crash call's word for "remove the flag"
REF_AUTO_PREFIX: Final = "ui-"
MS_DIGITS: Final = 13
REF_PREFIX_RE: Final = re.compile(rf"^{re.escape(REF_AUTO_PREFIX)}[0-9]{{{MS_DIGITS}}}$")  # "ui-" + epoch milliseconds
MS_PER_SECOND: Final = 1000
BURST_KEYS: Final = frozenset({"n", "person", "door", "ref_prefix"})
CRASH_KEYS: Final = frozenset({"point"})
GET: Final = "GET"
POST: Final = "POST"
ERROR_UNREACHABLE: Final = "unreachable"
ERR_NOT_WIRED: Final = "demo_not_wired"
ERR_BAD_BODY: Final = "bad_body"
ERR_NOT_FOUND: Final = "not_found"
ERR_METHOD: Final = "method_not_allowed"
ERR_CRASH_UNAVAILABLE: Final = "crash_unavailable"
ERR_IN_PROGRESS: Final = "burst_in_progress"
ERR_TOO_SOON: Final = "burst_too_soon"
ERR_BUDGET: Final = "burst_budget_used"
DESK_SCHEME: Final = "http"
# Most requests (counted by slots) one PortalDemo instance may send through `burst` in its lifetime:
# the lead book grows by at most this many rows.
BURST_BUDGET_REQUESTS: Final = 500
ERR_NO_URL: Final = "desk_url_unknown"
STATUS_ERROR: Final = "error"  # a burst slot whose request failed in a way that gave no HTTP status
STATUS_TIMEOUT: Final = "timeout"  # a burst slot still running when BURST_DEADLINE_SECONDS ran out
STATUS_REFUSED: Final = "refused"
BURST_DEADLINE_SECONDS: Final = 60.0  # the whole burst, all retries included
BURST_COOLDOWN_SECONDS: Final = 1.0  # least time between the end of one burst and the start of the next
STATE_DEADLINE_SECONDS: Final = 5.0  # the three sender reads of one `state` call share this
FIELD_MAX: Final = 200  # longest text taken from a sender or CRM row
RECORD_KEYS_MAX: Final = 10
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "[::1]"})  # as Config.fakes_host spells them
LOOPBACK_NAMES: Final = frozenset({"127.0.0.1", "localhost", "::1"})  # as urllib's .hostname spells them
ERR_URL_NOT_LOCAL: Final = "desk_url_not_local"
ARMED_READ_MAX: Final = 16  # bytes read from the flag file: the longest point name is 2
INFO_PATH: Final = "/fake/info"
OUTBOX_PATH: Final = "/fake/outbox"
CRM_PATH: Final = "/fake/crm"
KEY_MESSAGES: Final = "messages"
KEY_ROWS: Final = "rows"
KEY_COUNT: Final = "count"
KEY_CRM_CAPABILITY: Final = "crm_capability"
FLAG_TEMP_PREFIX: Final = ".crash-"
FLAG_TEMP_SUFFIX: Final = ".tmp"
FLAG_TEMP_MAX_AGE_SECONDS: Final = 3600.0  # a temporary flag file older than this is a leftover of a killed process
CRM_MODES: Final = ("keyed", "blind")
CRM_CAPABILITY_UNKNOWN: Final = "unknown"  # /fake/info of an older fakes server does not name the CRM mode
CANNED_FILE: Final = "canned_leads.json"
WILDCARD_BINDS: Final = {
    ".".join(["0"] * 4): ".".join(["127", "0", "0", "1"]),
    "::": "[::1]",
}  # wildcard bind -> loopback (built from parts so the address scan does not flag the wildcard literal)
LEADS_PATH: Final = "/leads"
JSON_TYPE: Final = {"Content-Type": "application/json"}
TOP_BAR_LINE: Final = (
    "Demo: Every lead is demo data. The demo CRM (customer records) and the demo mail are stand-ins. "
    "AI ratings are pre-written. "
    "Nothing here is sent to real customers or real systems."
)
SHORT_LINE: Final = (
    "Every lead is demo data. Demo CRM, demo mail, pre-written AI ratings. Nothing reaches real customers."
)
TILE_LABELS: Final = {
    "kpi-leads": "Leads",
    "kpi-outbox": "Emails in demo mail",
    "kpi-duplicates": "Repeats caught",
    "kpi-crm": "Leads in the demo CRM",
    "kpi-ai-calls": "AI ratings given",
}
BLIND_CRM_LABEL: Final = "Posts to the demo CRM"  # shown instead of the CRM tile label when the CRM is the blind one
BLIND_MODE: Final = "blind"
HERE: Final = Path(__file__).resolve().parent


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A made-up sender never redirects; following one could leave loopback."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())  # loopback, no proxy


def _bad_body() -> tuple[int, dict[str, Any]]:
    return HTTP_BAD_REQUEST, {"error": ERR_BAD_BODY}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_object(body: bytes, allowed: frozenset[str]) -> dict[str, Any] | None:
    """The body as a JSON object whose keys are all in `allowed`; None otherwise."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or not set(obj) <= allowed:
        return None
    return obj


def _clip(v: Any) -> Any:
    """Text cut to FIELD_MAX; numbers, booleans and None unchanged; anything else (nested) dropped."""
    if isinstance(v, str):
        return v[:FIELD_MAX]
    if isinstance(v, float):
        return v if math.isfinite(v) else None  # NaN and Infinity are not valid JSON for the page
    return v if v is None or isinstance(v, (bool, int)) else None


def _clip_record(rec: Any) -> dict[str, Any]:
    if not isinstance(rec, dict):
        return {}
    return {str(k)[:FIELD_MAX]: _clip(v) for k, v in list(rec.items())[:RECORD_KEYS_MAX]}


def _fetch_json(url: str, timeout: float) -> dict[str, Any] | None:
    """GET one JSON object from the made-up sender. None on any failure (refused, timeout, redirect, odd answer)."""
    try:
        with _OPENER.open(url, timeout=timeout) as r:  # noqa: S310 (url built from Config host and port, http only)
            raw = r.read(REPLY_MAX_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException):  # URLError and HTTPError are OSError
        return None
    if len(raw) > REPLY_MAX_BYTES:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


class PortalDemo:
    """The object the loader hands to the server."""

    def __init__(self, desk: Desk, desk_url: str | None = None) -> None:
        self._desk = desk
        self.desk_started_at = int(time.time())  # wall-clock: calendar stamp
        self.assets: dict[str, tuple[bytes, str]] = {}
        for name, ctype in ASSET_TYPES.items():
            f = HERE / name
            if f.is_file():  # a missing file is left out, not an error
                self.assets[name] = (f.read_bytes(), ctype)
        people = json.loads((HERE / CANNED_FILE).read_text(encoding="utf-8"))
        if not (isinstance(people, list) and people and all(self._person_ok(p) for p in people)):
            raise ValueError("canned_leads.json must hold a non-empty list of objects with a name and an email")
        self._people: list[dict[str, Any]] = people
        self.desk_url = desk_url if desk_url is not None else self._url_from_config()
        self._burst_lock = threading.Lock()
        self._sent = 0  # requests sent by `burst` so far (against BURST_BUDGET_REQUESTS; per instance)
        self._next_burst_at = 0.0  # time.monotonic() before which no new burst starts

    @staticmethod
    def _person_ok(p: Any) -> bool:
        return (
            isinstance(p, dict)
            and isinstance(p.get("name"), str)
            and bool(p["name"])
            and isinstance(p.get("email"), str)
            and bool(p["email"])
        )

    @property
    def wired(self) -> bool:
        """True only when the desk really runs on the made-up CRM, sender and recorded AI, all on loopback."""
        d = self._desk
        return (
            isinstance(d.crm, HttpCrm)
            and isinstance(d.outbox, HttpOutbox)
            and isinstance(d.qualifier, RecordedQualifier)
            and d.config.fakes_host in LOOPBACK_HOSTS
            and self._adapter_points_at(d.crm, d.config.fakes_url)
            and self._adapter_points_at(d.outbox, d.config.fakes_url)
        )

    @staticmethod
    def _adapter_points_at(adapter: Any, fakes_url: str) -> bool:
        """The adapter's own base address (public `base_url` if the adapter has one, else its private `_base`) is the
        configured made-up sender, plain http, and a loopback name: an adapter aimed at a real service (or at file:,
        ftp:, data:) is not wired."""
        base = getattr(adapter, "base_url", None) or getattr(adapter, "_base", None)
        if not isinstance(base, str) or base.rstrip("/") != fakes_url.rstrip("/"):
            return False
        try:
            parts = urlsplit(base)
            return parts.scheme == DESK_SCHEME and parts.hostname in LOOPBACK_NAMES
        except ValueError:
            return False

    def _desk_url_is_local(self, url: str) -> bool:
        """Only the desk itself may be the burst target: a loopback name or the host the desk is bound to."""
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            return False
        if parts.scheme != DESK_SCHEME:  # the opener also speaks file:, ftp: and data:
            return False
        cfg = self._desk.config
        try:
            port = parts.port
        except ValueError:
            return False
        if cfg.port != 0 and port != cfg.port:  # port 0 = chosen by the system: the server sets desk_url itself
            return False
        bound = cfg.bind.strip("[]")
        return host in LOOPBACK_NAMES or (host is not None and host == bound)

    def _url_from_config(self) -> str | None:
        """The desk's own address from Config; None when the port is 0 (ephemeral): then the server (or a test) must
        set `desk_url` once it knows the real port."""
        cfg = self._desk.config
        if cfg.port == 0:
            return None
        return self._url_for(cfg.bind, cfg.port)

    @staticmethod
    def _url_for(bind: str, port: int) -> str:
        host = WILDCARD_BINDS.get(bind, bind)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def set_bound_address(self, bind: str, port: int) -> None:
        """Called by the server once it is bound: with a configured port 0 the real port is only known now."""
        if self.desk_url is None and port:
            self.desk_url = self._url_for(bind, port)

    # -- entry point
    def handle(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        if path == "state":
            return self._state() if method == GET else (HTTP_METHOD_NOT_ALLOWED, {"error": ERR_METHOD})
        if path in ("burst", "crash"):
            if method != POST:
                return HTTP_METHOD_NOT_ALLOWED, {"error": ERR_METHOD}
            if not self.wired:
                return HTTP_CONFLICT, {"error": ERR_NOT_WIRED}
            return self._burst(body) if path == "burst" else self._crash(body)
        return HTTP_NOT_FOUND, {"error": ERR_NOT_FOUND}

    # -- state
    def _flag_hooks(self) -> FlagFileHooks | None:
        hooks = self._desk.hooks
        return hooks if isinstance(hooks, FlagFileHooks) else None

    def _armed(self, hooks: FlagFileHooks | None) -> str | None:
        """The armed point, only if the flag file holds one of HOOK_POINTS (never echoes other file content)."""
        if hooks is None:
            return None
        try:
            with open(hooks.flag_path, "rb") as f:  # noqa: PTH123
                raw = f.read(ARMED_READ_MAX + 1)
            if len(raw) > ARMED_READ_MAX:  # longer than any point: not a flag this module wrote
                return None
            text = raw.decode("utf-8").strip()
        except (OSError, ValueError):
            return None
        return text if text in HOOK_POINTS else None

    def _crm_mode(self, info: dict[str, Any]) -> str:
        """The sender's own answer if it names the CRM mode, else what the desk's CRM adapter has learned."""
        named = info.get(KEY_CRM_CAPABILITY)
        if named in CRM_MODES:
            return str(named)
        learned = getattr(self._desk.crm, "capability", None)
        return learned if learned in CRM_MODES else CRM_CAPABILITY_UNKNOWN

    def _fakes(self) -> dict[str, Any]:
        cfg = self._desk.config
        end = time.monotonic() + STATE_DEADLINE_SECONDS
        got: list[dict[str, Any] | None] = []
        for path in (INFO_PATH, OUTBOX_PATH, CRM_PATH):
            wait = min(cfg.send_timeout_seconds, end - time.monotonic())
            got.append(_fetch_json(cfg.fakes_url + path, wait) if wait > 0 else None)
        info, outbox, crm = got[0], got[1], got[2]  # the loop above always adds exactly three answers
        if info is None or outbox is None or crm is None:
            return {"reachable": False, "error": ERROR_UNREACHABLE}
        messages, rows = outbox.get(KEY_MESSAGES), crm.get(KEY_ROWS)
        if not isinstance(messages, list) or not isinstance(rows, list):
            return {"reachable": False, "error": ERROR_UNREACHABLE}
        count = outbox[KEY_COUNT] if _is_int(outbox.get(KEY_COUNT)) else len(messages)
        reminders = outbox["reminders"] if _is_int(outbox.get("reminders")) else 0  # an older fakes server has no key
        return {
            "reachable": True,
            "outbox_mode": str(info.get("mode", outbox.get("mode", CRM_CAPABILITY_UNKNOWN)))[:FIELD_MAX],
            "crm_mode": self._crm_mode(info),
            "outbox": {
                "count": count,
                "reminders": reminders,
                "followups": count - reminders,
                "messages": [
                    {"to": str(_clip(m.get("to")) or ""), "subject": str(_clip(m.get("subject")) or "")}
                    for m in messages[:ROWS_MAX]
                    if isinstance(m, dict)
                ],
            },
            "crm": {
                "count": crm[KEY_COUNT] if _is_int(crm.get(KEY_COUNT)) else len(rows),
                "rows": [
                    {"lead_id": _clip(r.get("lead_id")), "record": _clip_record(r.get("record"))}
                    for r in rows[:ROWS_MAX]
                    if isinstance(r, dict)
                ],
            },
        }

    def _ai_replies(self, asked: Any) -> Any:
        """AI replies given = the larger of the in-memory counter (resets when the desk restarts) and the ratings
        the desk holds in its own database (the Leads page reads those), so a restart never shows 0 beside ratings."""
        if not _is_int(asked):
            return asked
        try:
            conn = connect_readonly(self._desk.config.db_path)
            try:
                kept = conn.execute("SELECT COUNT(*) FROM followups WHERE ai_label IS NOT NULL").fetchone()[0]
            finally:
                conn.close()
        except Exception:  # noqa: BLE001  # the counter alone is still true: it is only ever a lower bound
            return asked
        return max(asked, kept) if _is_int(kept) else asked

    def _state(self) -> tuple[int, dict[str, Any]]:
        if not self.wired:  # the page must not claim "made-up" for real systems: simulated is false, nothing is read
            return HTTP_OK, {
                "simulated": False,
                "desk_started_at": self.desk_started_at,
                "fakes": {"reachable": False, "error": ERR_NOT_WIRED},
                "ai_calls": None,
                "crash": {"available": False, "armed": None, "points": list(HOOK_POINTS)},
                "people": [],
            }
        hooks = self._flag_hooks()
        calls = self._ai_replies(getattr(self._desk.qualifier, "calls", None))
        fakes = self._fakes()
        tiles = dict(TILE_LABELS)
        if fakes.get("crm_mode") == BLIND_MODE:
            tiles["kpi-crm"] = BLIND_CRM_LABEL
        return HTTP_OK, {
            "simulated": True,
            "labels": {"top_line": TOP_BAR_LINE, "short_line": SHORT_LINE, "tiles": tiles},
            "desk_started_at": self.desk_started_at,
            "fakes": fakes,
            "ai_calls": calls if _is_int(calls) else None,
            "crash": {"available": hooks is not None, "armed": self._armed(hooks), "points": list(HOOK_POINTS)},
            "people": [
                {"person": i, "name": p["name"], "company": p.get("company")} for i, p in enumerate(self._people, 1)
            ],
        }

    # -- crash
    def _crash(self, body: bytes) -> tuple[int, dict[str, Any]]:
        obj = _parse_object(body, CRASH_KEYS)
        point = obj.get("point") if obj is not None else None
        if not isinstance(point, str) or point not in (*HOOK_POINTS, CLEAR_POINT):
            return _bad_body()
        hooks = self._flag_hooks()
        if hooks is None:
            return HTTP_CONFLICT, {"error": ERR_CRASH_UNAVAILABLE}
        try:
            if point == CLEAR_POINT:
                hooks.flag_path.unlink(missing_ok=True)
            else:  # the path is the launcher's, never the request's; replace() keeps a reader from seeing half a file
                self._write_flag(hooks.flag_path, point)
        except OSError:
            return HTTP_CONFLICT, {"error": ERR_CRASH_UNAVAILABLE}
        return HTTP_OK, {"armed": None if point == CLEAR_POINT else point}

    @staticmethod
    def _write_flag(path: Path, point: str) -> None:
        """A private temporary file in the same folder (a fresh name, never a guessable one, never a symlink), then an
        atomic replace; the temporary file is removed if anything fails. Leftovers of a process killed between the two
        steps are swept when a flag is next armed. On Windows os.replace can fail with PermissionError while the hook
        has the flag open: that is reported as crash_unavailable and the caller may try again."""
        PortalDemo._sweep_stale_temps(path.parent)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=FLAG_TEMP_PREFIX, suffix=FLAG_TEMP_SUFFIX)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(point)
            os.replace(name, path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _sweep_stale_temps(folder: Path) -> None:
        cutoff = time.time() - FLAG_TEMP_MAX_AGE_SECONDS  # wall-clock: calendar stamp
        try:
            for f in folder.glob(f"{FLAG_TEMP_PREFIX}*{FLAG_TEMP_SUFFIX}"):
                try:
                    if f.is_file() and not f.is_symlink() and f.stat().st_mtime < cutoff:  # wall-clock: file age
                        f.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            return

    # -- burst
    def _burst(self, body: bytes) -> tuple[int, dict[str, Any]]:
        obj = _parse_object(body, BURST_KEYS)
        if obj is None:
            return _bad_body()
        n, person = obj.get("n"), obj.get("person", PERSON_MIN)
        door, prefix = obj.get("door", SOURCES[0]), obj.get("ref_prefix")
        if not (_is_int(n) and BURST_MIN <= n <= BURST_MAX):
            return _bad_body()
        if not (_is_int(person) and PERSON_MIN <= person <= len(self._people)):
            return _bad_body()
        if door not in SOURCES:
            return _bad_body()
        if prefix is not None and not (isinstance(prefix, str) and REF_PREFIX_RE.fullmatch(prefix)):
            return _bad_body()
        if self.desk_url is None:
            return HTTP_CONFLICT, {"error": ERR_NO_URL}
        if not self._desk_url_is_local(self.desk_url):
            return HTTP_CONFLICT, {"error": ERR_URL_NOT_LOCAL}
        if not self._burst_lock.acquire(blocking=False):  # one burst at a time: the load stays bounded
            return HTTP_TOO_MANY_REQUESTS, {"error": ERR_IN_PROGRESS}
        try:
            if time.monotonic() < self._next_burst_at:
                return HTTP_TOO_MANY_REQUESTS, {"error": ERR_TOO_SOON}
            if self._sent + n > BURST_BUDGET_REQUESTS:
                return HTTP_TOO_MANY_REQUESTS, {"error": ERR_BUDGET}
            self._sent += n
            stamp = int(time.time() * MS_PER_SECOND)  # wall-clock: calendar stamp
            ref_prefix = prefix if prefix is not None else f"{REF_AUTO_PREFIX}{stamp}"
            out = self._fire(self.desk_url + LEADS_PATH, n, self._people[person - 1], door, ref_prefix)
            # A request still in flight when the deadline ran out (status "timeout") cannot be stopped, only waited
            # for: it ends by itself within REQUEST_TIMEOUT_SECONDS and does not retry. No new burst starts before.
            stragglers = any(a["status"] == STATUS_TIMEOUT for a in out["answers"])
            self._next_burst_at = (
                time.monotonic() + BURST_COOLDOWN_SECONDS + (REQUEST_TIMEOUT_SECONDS if stragglers else 0)
            )
            return HTTP_OK, out
        finally:
            self._burst_lock.release()

    @staticmethod
    def _json_or_none(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return None

    def _post_once(self, url: str, data: bytes) -> tuple[int | str, float | None, Any]:
        """(status, Retry-After seconds or None, parsed reply body or None). A failure that gives no HTTP status gives
        a word instead of the status."""
        req = urllib.request.Request(url, data, JSON_TYPE, method=POST)  # noqa: S310 (http, loopback, from Config)
        try:
            with _OPENER.open(req, timeout=REQUEST_TIMEOUT_SECONDS) as r:  # noqa: S310
                return int(r.status), None, self._json_or_none(r.read(REPLY_MAX_BYTES))
        except urllib.error.HTTPError as e:
            after = retry_after_seconds(e.headers.get("Retry-After"))
            try:
                return int(e.code), after, self._json_or_none(e.read(REPLY_MAX_BYTES))
            except (OSError, http.client.HTTPException):
                return int(e.code), after, None
            finally:
                e.close()
        except urllib.error.URLError as e:
            word = STATUS_REFUSED if isinstance(e.reason, ConnectionRefusedError) else type(e.reason).__name__
            return word, None, None
        except (OSError, http.client.HTTPException) as e:  # includes a reply cut off in the middle of its body
            return type(e).__name__, None, None

    def _fire(self, url: str, n: int, payload: dict[str, Any], door: str, ref_prefix: str) -> dict[str, Any]:
        """n requests at the same moment (start barrier), all finished or timed out within BURST_DEADLINE_SECONDS.
        A 503 or a refused connection is tried again by time, not by count (BURST_RETRY_DEADLINE_SECONDS after the
        first try, waits of 0.1 s doubling to 1 s, Retry-After honoured: demo_only.burst.post_with_retries, the same
        rule as demo_only.burst.fire). The reply always holds exactly n answers.
        `busy_retries` counts every 503 or refused answer seen, including a last one that was not tried again."""
        barrier = threading.Barrier(n)
        stop = threading.Event()
        refs = [f"{ref_prefix}-{i + 1}" for i in range(n)]
        answers: list[dict[str, Any]] = [
            {"ref": r, "status": STATUS_ERROR, "verdict": None, "replayed": False} for r in refs
        ]
        busy = [0] * n  # per slot, summed at the end (no shared counter to lock)

        def one(i: int) -> None:
            data = json.dumps({"source": door, "source_ref": refs[i], "payload": payload}).encode("utf-8")
            try:
                barrier.wait(timeout=REQUEST_TIMEOUT_SECONDS)
            except threading.BrokenBarrierError:
                pass

            def count(_res: int | str) -> None:
                busy[i] += 1

            status, reply = post_with_retries(
                lambda: self._post_once(url, data),
                deadline=BURST_RETRY_DEADLINE_SECONDS,
                stop=stop,
                on_retryable=count,
            )
            rep = reply if isinstance(reply, dict) else {}
            answers[i] = {
                "ref": refs[i],
                "status": status,
                "verdict": rep.get("verdict") if isinstance(rep.get("verdict"), str) else None,
                "replayed": rep.get("replayed") is True,
            }

        threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(n)]
        started: list[threading.Thread] = []
        try:
            for t in threads:
                t.start()
                started.append(t)
        except RuntimeError:  # no thread could be made: let the ones already waiting go on without the barrier
            barrier.abort()
        end = time.monotonic() + BURST_DEADLINE_SECONDS
        for t in started:
            t.join(max(0.0, end - time.monotonic()))
        stop.set()
        final = [
            {"ref": refs[i], "status": STATUS_TIMEOUT, "verdict": None, "replayed": False}
            if (t in started and t.is_alive())
            else answers[i]
            for i, t in enumerate(threads)
        ]
        return {"n": n, "ref_prefix": ref_prefix, "answers": final, "busy_retries": sum(busy)}


def make(desk: Desk) -> PortalDemo:
    """Called by lead_desk.loader.load_demo_portal inside make_server."""
    return PortalDemo(desk)
