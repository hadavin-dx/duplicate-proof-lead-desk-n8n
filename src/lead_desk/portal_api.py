"""Read-only JSON for the portal pages (GET /api/ui/*) and the fixed list of portal files (GET /ui/*: .html, .css,
.js and .png).

Every endpoint opens its OWN read-only SQLite connection (mode=ro plus PRAGMA query_only), starts one read
transaction, runs fixed SQL with parameters, and closes. So every number of one reply comes from one snapshot, a
write through this module fails, and the desk's write connections are never used for a page (the review list runs the
desk's own SQL, desk.review_rows, on this connection). At most PORTAL_MAX_INFLIGHT replies are built at once; more get
503 busy. Standard library only.

Endpoints (GET /api/ui/...): summary, leads, leads/<lead_id>, review, log (the request record, newest first, one page
per call) and data-map (what the AI step is given, what the desk stores, how access is limited; it reads no database).
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs

from lead_desk import db, identity
from lead_desk.constants import (
    AI_INPUT_FIELDS,
    AI_STEP_NOTE,
    BUSY_WAIT_SECONDS,
    CRM_STAND_IN,
    DEMO_CONTROLS_ACTION,
    DUPLICATES_MAX,
    FEED_ITEMS,
    FIELD_NAME_MAX,
    FOLLOWUP_STATES,
    HTTP_BAD_REQUEST,
    HTTP_BUSY,
    HTTP_NOT_FOUND,
    HTTP_OK,
    LEAD_CONTENT_FIELDS,
    LEAD_ID_PATTERN,
    LEADS_LIMIT_DEFAULT,
    LEADS_LIMIT_MAX,
    LOG_LIMIT_DEFAULT,
    LOG_LIMIT_MAX,
    MAX_DIGITS,
    MIRROR_MAX_TRIES,
    OFFSET_MAX,
    PORTAL_ACTIONS,
    PORTAL_MAX_INFLIGHT,
    QUALIFIER_MODE_FALLBACK,
    QUALIFIER_MODES,
    SEARCH_MAX,
    SERIES_BUCKET_SECONDS,
    SERIES_BUCKETS,
    SOURCES,
    STATIC_FILE_MAX_BYTES,
    STATIC_TYPES,
    VERDICTS,
)
from lead_desk.contract import Qualifier
from lead_desk.desk import CRM_NOT_ASKED, CRM_STARTED, HINT_INTERNAL_ERROR, HINTS, Desk, review_rows

log = logging.getLogger("lead_desk")
PORTAL_DIR: Final = Path(__file__).resolve().parent / "portal"
_LEAD_ID = re.compile(LEAD_ID_PATTERN)
_NOT_FOUND: Final = (HTTP_NOT_FOUND, {"error": "not_found"})
_TAB_ORDER: Final = ("action", "waiting", "info")
_INFLIGHT = threading.BoundedSemaphore(PORTAL_MAX_INFLIGHT)


def build_static(root: Path) -> dict[str, tuple[bytes, str]]:
    """Every file under root of a type in STATIC_TYPES (.html, .css, .js, .png): relative posix path -> (bytes,
    content type). Other files, a missing folder, symbolic links, files that resolve outside root and files over
    STATIC_FILE_MAX_BYTES give nothing. Read once; the server never opens a path taken from a request."""
    out: dict[str, tuple[bytes, str]] = {}
    if not root.is_dir():
        return out
    base = root.resolve()
    for p in sorted(root.rglob("*")):
        kind = STATIC_TYPES.get(p.suffix)
        if kind is None or p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(base):
            continue
        if p.stat().st_size > STATIC_FILE_MAX_BYTES:
            continue
        out[p.relative_to(root).as_posix()] = (p.read_bytes(), kind)
    return out


STATIC: Final[dict[str, tuple[bytes, str]]] = build_static(PORTAL_DIR)


# -- the read-only connection
def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """A connection that cannot write: the file is opened mode=ro and PRAGMA query_only is on."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_WAIT_SECONDS, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only=ON")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _snapshot(desk: Desk) -> Iterator[tuple[sqlite3.Connection, int]]:
    """One read transaction: every SELECT inside sees the same committed state. Yields (connection, now). The clock
    is read after the first read has fixed the snapshot, so no row in the snapshot is newer than `now`."""
    conn = connect_readonly(desk.config.db_path)
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()  # the snapshot starts at the first read
        yield conn, desk.now()
        conn.execute("COMMIT")
    finally:
        conn.close()


# -- query strings
def _digits(raw: str, low: int, high: int) -> int | None:
    if not (raw.isascii() and raw.isdecimal()) or len(raw) > MAX_DIGITS:
        return None
    n = int(raw)
    return n if low <= n <= high else None


def _params(query: str, allowed: tuple[str, ...]) -> tuple[dict[str, str], str | None]:
    """(values, bad field name). An unknown name or a repeated name is a bad field."""
    found = parse_qs(query, keep_blank_values=True)
    for name, values in found.items():
        if name not in allowed or len(values) != 1:
            return {}, name[:FIELD_NAME_MAX]
    return {name: values[0] for name, values in found.items()}, None


def _bad_query(field: str) -> tuple[int, dict[str, Any]]:
    return HTTP_BAD_REQUEST, {"error": "bad_query", "field": field}


_NOT_READY_MESSAGES = ("unable to open database file", "no such table", "disk I/O error")  # last: a folder as path


# -- entry point
def handle(
    desk: Desk, endpoint: str, query: str, started_at: int, demo_controls: bool = False
) -> tuple[int, dict[str, Any]]:
    """(status, JSON object) for GET /api/ui/<endpoint>. A lock that outlasts the wait, or more than
    PORTAL_MAX_INFLIGHT replies at once, is a 503."""
    if not _INFLIGHT.acquire(blocking=False):
        return HTTP_BUSY, {"error": "busy"}
    try:
        return _handle(desk, endpoint, query, started_at, demo_controls)
    except sqlite3.OperationalError as e:
        if db.is_busy(e):
            return HTTP_BUSY, {"error": "busy"}
        # Only the two "not ready" faults get the fixed 503: the read-only open failed or a table is missing (a request
        # before desk.startup(), a db_path that is not a file such as ":memory:", a file that is gone). Any other
        # database error is a bug and stays a 500 (the server logs the class name only).
        if any(m in str(e) for m in _NOT_READY_MESSAGES):
            log.warning("portal read failed: %s", type(e).__name__)
            return HTTP_BUSY, {"error": "unavailable"}
        raise
    finally:
        _INFLIGHT.release()


def _handle(desk: Desk, endpoint: str, query: str, started_at: int, demo_controls: bool) -> tuple[int, dict[str, Any]]:
    parts = endpoint.split("/")
    match parts:
        case ["summary"]:
            _, bad = _params(query, ())
            return _bad_query(bad) if bad is not None else (HTTP_OK, _summary(desk, started_at, demo_controls))
        case ["leads"]:
            return _leads(desk, query)
        case ["leads", lead_id]:
            _, bad = _params(query, ())
            if bad is not None:
                return _bad_query(bad)
            return _lead_detail(desk, lead_id) if _LEAD_ID.fullmatch(lead_id) else _NOT_FOUND
        case ["review"]:
            _, bad = _params(query, ())
            return _bad_query(bad) if bad is not None else _review(desk)
        case ["log"]:
            return _log(desk, query)
        case ["data-map"]:
            _, bad = _params(query, ())
            return _bad_query(bad) if bad is not None else (HTTP_OK, _data_map(desk, demo_controls))
        case _:
            return _NOT_FOUND


# -- summary
_COUNTS = """
SELECT (SELECT count(*) FROM receipts),
       (SELECT count(*) FROM receipts WHERE outcome = 'new'),
       (SELECT count(*) FROM receipts WHERE outcome = 'duplicate'),
       (SELECT count(*) FROM receipts WHERE outcome = 'review'),
       (SELECT count(*) FROM receipts WHERE outcome IS NULL),
       (SELECT count(*) FROM review_items WHERE kind IN ('review','failed','unknown','reminder_to_check')),
       (SELECT count(*) FROM receipts WHERE outcome IS NULL AND received_at < ?)
     + (SELECT count(*) FROM followups WHERE state IN ('pending','sending') AND updated_at < ?)
"""
_FOLLOWUP_COUNTS = (
    "SELECT state, last_error, ambiguous_prior, count(*) FROM followups GROUP BY state, last_error, ambiguous_prior"
)
# the groups a person reads: the five stored states, with failed split in two and unknown split by what is unclear
FOLLOWUP_VIEWS: Final = (*FOLLOWUP_STATES, "did_not_finish", "crm_unconfirmed")
_DOORS = "SELECT source, count(*), max(received_at) FROM receipts GROUP BY source"
_SERIES = """
SELECT outcome, (decided_at / ?) * ? AS bucket, count(*) FROM receipts
WHERE outcome IS NOT NULL AND decided_at >= ? AND decided_at < ? GROUP BY outcome, bucket
"""
_RECENT = """
SELECT e.at, e.kind, e.receipt_id, e.lead_id, n.name, e.source, e.source_ref, e.verdict, e.state,
       e.last_error, e.ambiguous_prior FROM (
  SELECT * FROM (
    SELECT received_at AS at, 'received' AS kind, receipt_id, outcome_lead_id AS lead_id, source, source_ref,
           NULL AS verdict, NULL AS state, NULL AS last_error, NULL AS ambiguous_prior, 1 AS rank
      FROM receipts ORDER BY received_at DESC, receipt_id DESC LIMIT :n)
  UNION ALL
  SELECT * FROM (
    SELECT decided_at, 'decided', receipt_id, outcome_lead_id, source, source_ref, outcome, NULL, NULL, NULL, 2
      FROM receipts WHERE outcome IS NOT NULL ORDER BY decided_at DESC, receipt_id DESC LIMIT :n)
  UNION ALL
  SELECT * FROM (
    SELECT f.updated_at, 'followup', l.receipt_id, f.lead_id, l.source, NULL, NULL, f.state, f.last_error,
           f.ambiguous_prior, 3
      FROM followups f JOIN leads l USING (lead_id) ORDER BY f.updated_at DESC, l.receipt_id DESC LIMIT :n)
) e LEFT JOIN leads n ON n.lead_id = e.lead_id
ORDER BY e.at DESC, e.receipt_id DESC, e.rank DESC LIMIT :n
"""
_FEED_KEYS = ("at", "kind", "receipt_id", "lead_id", "name", "source", "source_ref", "verdict", "state", "state_view")


def _feed_row(r: tuple[Any, ...]) -> dict[str, Any]:
    """One live-feed line; a follow-up line carries the same state_view the other pages show."""
    row = dict(zip(_FEED_KEYS, (*r[:9], None), strict=True))
    if r[1] == "followup":
        row["state_view"] = _state_view(r[8], r[9], r[10])
    return row


def reconciled(received: int, new: int, duplicate: int, review: int, open_: int) -> bool:
    """Every receipt is in exactly one state: decided as new, duplicate or review, or still open."""
    return received == new + duplicate + review + open_


def _summary(desk: Desk, started_at: int, demo_controls: bool = False) -> dict[str, Any]:
    step = SERIES_BUCKET_SECONDS
    with _snapshot(desk) as (conn, now):
        before = now - desk.config.stall_seconds
        last_bucket = (now // step) * step
        first_bucket = last_bucket - (SERIES_BUCKETS - 1) * step
        received, new, dup, review, open_, needs, waiting = conn.execute(_COUNTS, (before, before)).fetchone()
        by_view: dict[str, int] = {}
        for st, err, amb, n in conn.execute(_FOLLOWUP_COUNTS).fetchall():
            view = _state_view(st, err, amb)
            by_view[view] = by_view.get(view, 0) + int(n)
        door_rows = {r[0]: r for r in conn.execute(_DOORS)}
        cells = {(r[0], r[1]): r[2] for r in conn.execute(_SERIES, (step, step, first_bucket, last_bucket + step))}
        feed = conn.execute(_RECENT, {"n": FEED_ITEMS}).fetchall()
    return {
        "now": now,
        "desk_started_at": started_at,
        # what the portal may ask for next: the extension routes exist only when the extension is loaded, and the desk
        # holds sample data only when every kind is a stand-in (the portal never guesses this from a failed request)
        "mode": {
            "demo_tools": demo_controls,
            "sample_data": not desk.config.real_services,
            "real_services": list(desk.config.real_services),
        },
        "counts": {"received": received, "new": new, "duplicate": dup, "review": review, "open": open_},
        "reconciled": reconciled(received, new, dup, review, open_),
        "refused_at_door": desk.rejected_envelopes(),
        "hung_workers": desk.hung_workers(),
        "needs_action": needs,
        "waiting": waiting,
        "followups": {s: by_view.get(s, 0) for s in FOLLOWUP_VIEWS},
        "doors": [
            {
                "source": s,
                "received": door_rows[s][1] if s in door_rows else 0,
                "last_seen": door_rows[s][2] if s in door_rows else None,
            }
            for s in SOURCES
        ],
        "series": {
            "bucket_seconds": step,
            "time_field": "decided_at",
            "buckets": [
                {"t": t, **{v: int(cells.get((v, t), 0)) for v in VERDICTS}}
                for t in range(first_bucket, last_bucket + step, step)
            ],
        },
        "recent": [_feed_row(r) for r in feed],
    }


# -- leads
_LEADS_ROWS = """
SELECT l.lead_id, l.name, l.email, l.company, l.source, r.source_ref, l.created_at, f.state, f.ai_label,
       f.attempts, r.attempts,
       f.updated_at, f.last_error, f.ambiguous_prior, s.owner_name, s.step
  FROM leads l JOIN followups f USING (lead_id) JOIN receipts r ON r.receipt_id = l.receipt_id
  LEFT JOIN lead_state s ON s.lead_id = l.lead_id
 WHERE (:state = '' OR f.state = :state)
   AND (:q = '' OR (:by_email = 1 AND l.name LIKE :pat ESCAPE '\\')
        OR (:by_email = 1 AND l.email LIKE :pat ESCAPE '\\') OR l.company LIKE :pat ESCAPE '\\'
        OR l.lead_id LIKE :pat ESCAPE '\\'
        OR (r.source_ref LIKE :pat ESCAPE '\\' AND :by_email = 1))
 ORDER BY l.created_at DESC, l.lead_id DESC LIMIT :limit OFFSET :offset
"""
_REPEATS = """
SELECT outcome_lead_id, count(*) FROM receipts
 WHERE outcome = 'duplicate' AND outcome_lead_id IN (SELECT value FROM json_each(?)) GROUP BY outcome_lead_id
"""
_LEADS_TOTAL = """
SELECT count(*)
  FROM leads l JOIN followups f USING (lead_id) JOIN receipts r ON r.receipt_id = l.receipt_id
 WHERE (:state = '' OR f.state = :state)
   AND (:q = '' OR (:by_email = 1 AND l.name LIKE :pat ESCAPE '\\')
        OR (:by_email = 1 AND l.email LIKE :pat ESCAPE '\\') OR l.company LIKE :pat ESCAPE '\\'
        OR l.lead_id LIKE :pat ESCAPE '\\'
        OR (r.source_ref LIKE :pat ESCAPE '\\' AND :by_email = 1))
"""
_LEAD_KEYS = (
    "lead_id",
    "name",
    "email",
    "company",
    "source",
    "first_request_id",
    "created_at",
    "state",
    "ai_label",
    "attempts",
    "arrivals",
    "updated_at",
    "last_error",
    "ambiguous_prior",
    "owner",
    "step",
)


def _step_labels(desk: Desk) -> dict[str, str]:
    """step id -> the label of the lead flow file ({} while the feature is off)."""
    flow = desk.config.leadflow
    return {} if flow is None else {sid: label for sid, label, _ in flow.steps}


def _like(text: str) -> str:
    """A LIKE pattern that matches `text` anywhere, with the three special characters taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _leads(desk: Desk, query: str) -> tuple[int, dict[str, Any]]:
    values, bad = _params(query, ("q", "state", "offset", "limit", "by_email"))
    if bad is not None:
        return _bad_query(bad)
    q, state = values.get("q", ""), values.get("state", "")
    if len(q) > SEARCH_MAX or identity.has_control_char(q):  # a NUL would cut the LIKE pattern short
        return _bad_query("q")
    if state and state not in FOLLOWUP_STATES:
        return _bad_query("state")
    offset = 0 if "offset" not in values else _digits(values["offset"], 0, OFFSET_MAX)
    limit = LEADS_LIMIT_DEFAULT if "limit" not in values else _digits(values["limit"], 1, LEADS_LIMIT_MAX)
    if offset is None:
        return _bad_query("offset")
    if limit is None:
        return _bad_query("limit")
    by_email = 1 if "by_email" not in values else _digits(values["by_email"], 0, 1)
    if by_email is None:
        return _bad_query("by_email")
    args = {"q": q, "pat": _like(q), "state": state, "limit": limit, "offset": offset, "by_email": by_email}
    with _snapshot(desk) as (conn, now):
        rows = conn.execute(_LEADS_ROWS, args).fetchall()
        total = conn.execute(_LEADS_TOTAL, {k: args[k] for k in ("q", "pat", "state", "by_email")}).fetchone()[0]
        ids = json.dumps([r[0] for r in rows])
        repeats = dict(conn.execute(_REPEATS, (ids,)).fetchall()) if rows else {}  # one pass, page rows only
    out = []
    labels = _step_labels(desk)
    for r in rows:
        row = dict(zip(_LEAD_KEYS, r, strict=True))
        row["step_label"] = labels.get(row["step"]) if row["step"] else None
        row["state_view"] = _state_view(row["state"], row.pop("last_error"), row.pop("ambiguous_prior"))
        row["repeats"] = int(repeats.get(row["lead_id"], 0))
        out.append(row)
    return HTTP_OK, {
        "now": now,
        "rows": out,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


# -- one lead
_LEAD_ROW = """
SELECT lead_id, receipt_id, source, name, email, phone, company, message, created_at FROM leads WHERE lead_id = ?
"""
_FOLLOWUP_ROW = """
SELECT state, ai_label, attempts, auto_attempts, last_error, updated_at, sent_at, crm_done, ambiguous_prior
  FROM followups WHERE lead_id = ?
"""
_RECEIPT_ROW = """
SELECT receipt_id, source, source_ref, received_at, decided_at, attempts, payload_conflict, payload
  FROM receipts WHERE receipt_id = ?
"""
_DUPLICATES = """
SELECT receipt_id, source, source_ref, received_at, decided_at, differing_fields, payload FROM receipts
 WHERE outcome = 'duplicate' AND outcome_lead_id = ? ORDER BY receipt_id DESC LIMIT ?
"""
_DUPLICATES_TOTAL = "SELECT count(*) FROM receipts WHERE outcome = 'duplicate' AND outcome_lead_id = ?"
_LEAD_ITEMS = """
SELECT item_id FROM (
  SELECT item_id, since, lead_id FROM review_items
  UNION ALL
  SELECT 'F' || f.lead_id, f.updated_at, f.lead_id FROM followups f
   WHERE f.state IN ('pending','sending') AND f.updated_at < ?
) WHERE lead_id = ? ORDER BY since, item_id
"""
_LEAD_STATE_ROW = (
    "SELECT s.owner_id, s.owner_name, s.step, s.step_rank, s.crm_owner_done, s.crm_step_done, s.crm_tries, n.state"
    " FROM leads l LEFT JOIN lead_state s ON s.lead_id = l.lead_id LEFT JOIN nudges n ON n.lead_id = l.lead_id"
    " WHERE l.lead_id = ?"
)
_DECISIONS = (
    "SELECT receipt_id, decision, approver_id, approver_name, channel, decided_at FROM later_decisions"
    " WHERE receipt_id IN (SELECT value FROM json_each(?))"
)
_DECISION_KEYS = ("decision", "decided_by", "decided_by_name", "channel", "decided_at")
_FOLLOWUP_KEYS = (
    "state",
    "ai_label",
    "attempts",
    "auto_attempts",
    "last_error",
    "updated_at",
    "sent_at",
    "crm_done",
    "ambiguous_prior",
)
_LEAD_FIELDS = ("lead_id", "receipt_id", "source", "name", "email", "phone", "company", "message", "created_at")


def _object(text: str) -> dict[str, Any]:
    """The stored payload as a dict ({} when it is not one; the desk only stores objects)."""
    try:
        obj = json.loads(text, parse_constant=lambda _name: None)  # NaN and Infinity become null (not valid JSON)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _trimmed(v: Any) -> Any:
    """A later value as the first record keeps its own: white space at both ends removed (str.strip, the rule of
    identity.check_payload). A listed field is a non-empty string (identity.diff_fields), so this never returns ''."""
    return v.strip() if isinstance(v, str) else v


def _names(text: str | None) -> list[str]:
    try:
        obj = json.loads(text or "[]")
    except ValueError:
        return []
    return [x for x in obj if isinstance(x, str)] if isinstance(obj, list) else []


def _lead_detail(desk: Desk, lead_id: str) -> tuple[int, dict[str, Any]]:
    with _snapshot(desk) as (conn, now):
        before = now - desk.config.stall_seconds
        lead = conn.execute(_LEAD_ROW, (lead_id,)).fetchone()
        followup = conn.execute(_FOLLOWUP_ROW, (lead_id,)).fetchone()
        if lead is None or followup is None:
            return _NOT_FOUND
        first = conn.execute(_RECEIPT_ROW, (lead[1],)).fetchone()
        dups = conn.execute(_DUPLICATES, (lead_id, DUPLICATES_MAX)).fetchall()
        total = conn.execute(_DUPLICATES_TOTAL, (lead_id,)).fetchone()[0]
        items = [r[0] for r in conn.execute(_LEAD_ITEMS, (before, lead_id))]
        state = conn.execute(_LEAD_STATE_ROW, (lead_id,)).fetchone()
        decided = {}
        if desk.config.later_mode == "approval" and dups:
            ids = json.dumps([d[0] for d in dups])
            decided = {r[0]: dict(zip(_DECISION_KEYS, r[1:], strict=True)) for r in conn.execute(_DECISIONS, (ids,))}
    lead_row = dict(zip(_LEAD_FIELDS, lead, strict=True))
    followup_row = dict(zip(_FOLLOWUP_KEYS, followup, strict=True))
    labels = _step_labels(desk)
    owner, step, reminder = (None,) * 3
    owner_id = None
    if state is not None:
        owner_id, owner, step, _step_rank, _owner_done, _step_done, _tries, reminder = state
    approval = None
    if desk.config.later_mode == "approval":  # the names come from the list the desk was started with
        approval = {
            "label": desk.config.approvers_label,
            "approvers": [{"id": i, "name": n} for i, n in desk.config.approvers],
        }
    first_payload = _object(first[7])
    this_payloads = [_object(d[6]) for d in dups]  # parsed once per duplicate, not once per differing field
    return HTTP_OK, {
        "now": now,
        "lead": lead_row,
        "followup": followup_row,
        "state_view": _state_view(followup_row["state"], followup_row["last_error"], followup_row["ambiguous_prior"]),
        "next_step": _next_step(followup_row["state"], followup_row["last_error"], followup_row["ambiguous_prior"]),
        "first_receipt": {
            "receipt_id": first[0],
            "source": first[1],
            "source_ref": first[2],
            "received_at": first[3],
            "decided_at": first[4],
            "arrivals": first[5],
            "payload_conflict": first[6],
        },
        "duplicates": [
            {
                "receipt_id": d[0],
                "source": d[1],
                "source_ref": d[2],
                "received_at": d[3],
                "decided_at": d[4],
                "seconds_after_first": d[3] - first[3],
                "decision": decided.get(d[0]),
                "differing": [
                    {
                        "field": f,
                        "first": lead_row[f] if f in lead_row else first_payload.get(f),
                        "this": _trimmed(this_payloads[n].get(f)),
                    }
                    for f in _names(d[5])
                ],
            }
            for n, d in enumerate(dups)
        ],
        "duplicates_total": total,
        "review_items": items,
        "owner": owner,
        "step": step,
        "step_label": labels.get(step) if step else None,
        # a lead stage move is written to the CRM's lead status field, which can start the customer's own automations
        "stage_writes_real_crm": desk.config.crm != CRM_STAND_IN,
        "steps": [{"id": sid, "label": label} for sid, label in labels.items()],
        "reminder": reminder,
        "crm_copy": _crm_copy(desk, state, owner_id),
        "approval": approval,
    }


def _crm_copy(desk: Desk, state: Any, owner_id: Any) -> str | None:
    """Where the copy of the owner and step into the CRM stands: None (no state row), not_copied, copied, step_only
    (the owner has no CRM identity in the lead flow file) or waiting."""
    flow = desk.config.leadflow
    if state is None or flow is None:
        return None
    _, _, _, step_rank, owner_done, step_done, tries, _ = state
    if tries >= MIRROR_MAX_TRIES:
        return "not_copied"
    if step_done == step_rank and owner_done == 1:
        return "copied"
    copyable = {i for i, _, crm in flow.rota if crm}
    if step_done == step_rank and owner_done == 0 and owner_id not in copyable:
        return "step_only"
    return "waiting"


# -- the list of things that need a person
_NAMES = "SELECT lead_id, name FROM leads WHERE lead_id IN (SELECT value FROM json_each(?))"
# Follow-ups for which an earlier email may already have been sent (set once and kept until a person clears it).
# ambiguous_prior = 1: an earlier email may have been sent; crm_done = 0: the CRM step is open again (one query).
_MARKS = "SELECT lead_id, ambiguous_prior, crm_done FROM followups WHERE lead_id IN (SELECT value FROM json_each(?))"

_RETRY = {"id": "retry", "label": "Retry", "body": None, "confirm": None}
_SENT_CONFIRM = {
    "title": "Mark the email as sent?",
    "body": "This cannot be undone. The desk will never send this follow-up again.",
    "verb": "Mark as sent",
}
_SEEN_CONFIRM = {
    "title": "Mark the reminder as seen?",
    "body": "The desk never sends this reminder again either way.",
    "verb": "Mark as seen",
}
_NOT_SENT_CONFIRM = {
    "title": "Mark the email as not sent?",
    "body": ("If the email was already sent, a retry will send a second one. Check the mail step first."),
    "verb": "Mark as not sent",
}
_CRM_PRESENT_CONFIRM = {
    "title": "Confirm the CRM entry is there?",
    "body": "Retry will then go straight to the email.",
    "verb": "Confirm",
}
_CRM_MISSING_CONFIRM = {
    "title": "Mark the CRM entry as missing?",
    "body": "Retry will ask the CRM again; if a row was there, delete the extra one by hand.",
    "verb": "Mark as missing",
}
_NOTHING_SENT_TEXT = {
    "refused": "The mail step (the part that sends the email) refused the email.",
    "unreachable": "The mail step could not be reached.",
    # a row the reaper put back that never reached the send (no earlier attempt sent the email: ambiguous_prior is 0)
    "crash_while_sending": "The desk stopped or was held up before it tried to send.",
}
# Failed rows a person made by choosing in the review list (Desk.resolve writes these words as last_error).
_PERSON_TEXT = {
    "person: not sent": (  # the answer to "was the email sent?": the CRM step was finished (crm_done 1)
        "Follow-up failed; a person marked the email as not sent",
        "A person checked and marked that the email was not sent. Retry sends it now.",
    ),
    "person: crm row present": (
        "Follow-up not finished; the CRM entry is confirmed",
        "A person confirmed that the CRM entry is there. Retry goes straight to the email.",
    ),
}
_PERSON_TEXT_CRM = {  # the answer to "is the CRM entry there?" (Desk.resolve resets crm_done to 0)
    "person: not sent": (
        "Follow-up failed; a person marked the CRM entry as missing",
        "A person checked and marked that the CRM entry is not there. Retry asks the CRM again and then sends the "
        "email. If a row was there after all, delete the extra one by hand.",
    ),
}
_NOTHING_SENT_INTERNAL = "The desk hit a fault before it tried to send."
_CRM_MAYBE_HINT = (  # crm_done = 2: an earlier try started the CRM write and the desk never learned the result
    " An earlier try may already have written this lead into the CRM. Retry asks the CRM again: a CRM that keeps a key "
    "for repeats does not add a second row; for any other CRM the desk asks a person to look first."
)
# what happened, then what Retry would do; the instruction to check the mail step first is said once, beside the button
# on the card and in the next-step line of the lead panel (which has no button)
_DID_NOT_FINISH_TITLE = "Follow-up did not finish"
_DID_NOT_FINISH_TEXT = (
    "The desk tried this follow-up and stopped before it finished, so the email may already have been sent. "
    "If it did, Retry sends a second one."
)
_UNKNOWN_TEXT = {
    "ambiguous_prior": (
        "An earlier attempt may already have sent the email before a stop. "
        "The desk will not send again until a person checks."
    ),
    "ambiguous": (
        "The send ended unclearly, so the email may have been sent. The desk will not send again until a person checks."
    ),
    "mode_mismatch": "The mail step stored the email in a way the desk did not expect. A person needs to look.",
    "crm_unconfirmed": (
        "The CRM may or may not hold this lead. The follow-up is waiting for the CRM check, so no email was sent."
    ),
}
_UNKNOWN_DEFAULT = "The desk cannot tell whether the email was sent. A person needs to look."
_UNSTORABLE_TEXT = "The desk could not store this request. Send the form again with a new request number."
_UNSTORABLE_INTEGRITY_TEXT = (
    "The desk could not store this request because a value broke a storage rule; nothing was sent. "
    "Send the form again with a new request number."
)


def _failed_email_may_have_left(reason: str, ambiguous_prior: Any) -> bool:
    """A failed follow-up whose email may still have left (the card and the panel share this one rule)."""
    certain = reason in _PERSON_TEXT or (
        not ambiguous_prior and (reason in _NOTHING_SENT_TEXT or reason.startswith("internal"))
    )
    return not certain


def _state_view(state: Any, last_error: Any, ambiguous_prior: Any) -> Any:
    """The state a person reads on a chip: a certain refusal stays failed, a failure whose email may have been sent is
    did_not_finish, and an unknown that is about the CRM is crm_unconfirmed (outcome unknown stays for the email)."""
    reason = str(last_error or "")
    if state == "failed" and _failed_email_may_have_left(reason, ambiguous_prior):
        return "did_not_finish"
    if state == "unknown" and reason == "crm_unconfirmed":
        return "crm_unconfirmed"
    return state


def _next_step(state: Any, last_error: Any, ambiguous_prior: Any) -> str | None:
    """The one line the lead panel shows for a failed or unknown follow-up. The failed cases follow the card's own
    choice (_describe): a send that may have been sent says to check the mail step first (the panel has no button)."""
    reason = str(last_error or "")
    if state == "failed":
        if not _failed_email_may_have_left(reason, ambiguous_prior):
            return (
                "Next step: open Needs a person and press Retry. "
                "If you are not sure it was not sent, check the mail step (the part that sends the email) first."
            )
        return (
            "Did not finish, and the email may already have been sent. Next step: check the mail step "
            "(the part that sends the email) first. If the email was not sent, open Needs a person and press Retry."
        )
    if state == "unknown" and reason == "crm_unconfirmed":
        return (
            "Unclear whether the CRM holds this lead. Next step: check the CRM, then open "
            "Needs a person and choose “The CRM entry is there” or “The CRM entry is not there”. "
            "The desk will not send again until you do."
        )
    if state == "unknown":
        return (
            "Unclear whether the email was sent. Next step: check the mail step (the part that sends the email), "
            "then open Needs a person and choose “I checked: it was sent” or “I checked: it was not sent”. "
            "The desk will not send again until you do."
        )
    return None


def _list_words(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _action(spec: dict[str, Any], item_id: str, verb: str, **over: Any) -> dict[str, Any]:
    return {**spec, "endpoint": f"/review/{item_id}/{verb}", **over}


def _describe(
    item: dict[str, Any],
    stall_seconds: int,
    maybe_sent: bool = False,
    crm_open: bool = False,
    crm_maybe: bool = False,
) -> dict[str, Any]:
    """tab, title, reason_text and actions of one item, from a fixed table."""
    kind, reason, item_id = str(item["kind"]), str(item["reason"] or ""), str(item["item_id"])
    tab, actions = "action", []
    if kind == "failed":
        if reason in _PERSON_TEXT:  # the words of the person's own decision come first
            title, text = (_PERSON_TEXT_CRM if crm_open and reason in _PERSON_TEXT_CRM else _PERSON_TEXT)[reason]
        # the "was not sent" card only when the reason says so AND no earlier attempt sent the email (ambiguous_prior)
        elif not maybe_sent and (reason in _NOTHING_SENT_TEXT or reason.startswith("internal")):
            title = "The follow-up email was not sent"
            text = _NOTHING_SENT_TEXT.get(reason, _NOTHING_SENT_INTERNAL)
            if crm_maybe:  # the CRM entry may exist although the follow-up failed
                text += _CRM_MAYBE_HINT
        else:  # ambiguous, crash_while_sending, anything else: the email may have been sent
            title, text = _DID_NOT_FINISH_TITLE, _DID_NOT_FINISH_TEXT
        actions = [_action(_RETRY, item_id, "retry")]
    elif kind == "unknown" and reason == "crm_unconfirmed":
        title, text = "The CRM may or may not have this lead", _UNKNOWN_TEXT[reason]
        actions = [
            _action(_RETRY, item_id, "retry"),  # the desk answers 409 unknown_needs_person; the page shows its words
            _action(
                {"id": "resolve_crm_present", "label": "The CRM entry is there", "confirm": _CRM_PRESENT_CONFIRM},
                item_id,
                "resolve",
                body={"decision": "crm_present"},
            ),
            _action(
                {"id": "resolve_not_sent", "label": "The CRM entry is not there", "confirm": _CRM_MISSING_CONFIRM},
                item_id,
                "resolve",
                body={"decision": "not_sent"},
            ),
        ]
    elif kind == "unknown":
        title, text = "The email may or may not have been sent", _UNKNOWN_TEXT.get(reason, _UNKNOWN_DEFAULT)
        actions = [
            _action(_RETRY, item_id, "retry"),  # the desk answers 409 unknown_needs_person; the page shows its words
            _action(
                {"id": "resolve_sent", "label": "I checked: it was sent", "confirm": _SENT_CONFIRM},
                item_id,
                "resolve",
                body={"decision": "sent"},
            ),
            _action(
                {"id": "resolve_not_sent", "label": "I checked: it was not sent", "confirm": _NOT_SENT_CONFIRM},
                item_id,
                "resolve",
                body={"decision": "not_sent"},
            ),
        ]
    elif kind == "reminder_to_check":
        if reason == "unknown":
            title, text = "A reminder email could not be confirmed", "Check your mailbox, then mark it seen."
        else:
            title = "A reminder email was not sent"
            text = (
                "The mail step refused it or could not be reached. The desk does not send it again by itself; "
                "mark it seen."
            )
        actions = [
            _action(
                {"id": "resolve_seen", "label": "I checked the mailbox", "confirm": _SEEN_CONFIRM},
                item_id,
                "resolve",
                body={"decision": "seen"},
            )
        ]
    elif kind == "review":
        text = HINTS["R"]
        if reason.startswith("incomplete:"):
            fields = [f for f in reason.partition(":")[2].split(",") if f]
            title = f"Incomplete request: {_list_words(fields)} {'is' if len(fields) == 1 else 'are'} missing"
        elif reason == "invalid_email":
            title = "Incomplete request: the email address is not valid"
        elif reason.startswith("bad_field:"):
            title = f"Incomplete request: {reason.partition(':')[2]} has a value the desk cannot accept"
        elif reason == "internal_unstorable" or reason.startswith("internal_unstorable:"):
            title = "The desk could not store this request"
            text = _UNSTORABLE_INTEGRITY_TEXT if reason == "internal_unstorable:integrity" else _UNSTORABLE_TEXT
        else:
            title, text = "The desk could not decide this request", HINT_INTERNAL_ERROR
    elif kind == "stalled":
        tab = "waiting"
        if reason == "pending":
            title, text = f"Queued longer than {stall_seconds} s", "The desk retries by itself; Retry starts it now."
            actions = [_action(_RETRY, item_id, "retry")]
        elif reason == "sending":
            title, text = (
                f"Sending longer than {stall_seconds} s",
                "The desk puts it back by itself after the send timeout.",
            )
        else:
            title = f"Request received, no verdict for longer than {stall_seconds} s"
            text = HINTS["O"]
    elif kind == "duplicate_with_new_info":
        tab, text = "info", HINTS["D"]
        title = "Repeat with new details: " + ", ".join(_names(reason))
    else:
        tab, text = "info", HINTS["D"]
        title = "The same request came again with different details. The first one stands."
    view = (
        "did_not_finish"
        if title == _DID_NOT_FINISH_TITLE
        else "crm_unconfirmed"
        if kind == "unknown" and reason == "crm_unconfirmed"
        else kind
    )
    return {"tab": tab, "title": title, "reason_text": text, "actions": actions, "state_view": view}


def _review(desk: Desk) -> tuple[int, dict[str, Any]]:
    with _snapshot(desk) as (conn, now):  # the desk's own list, hints and stall rule, on the read-only connection
        items = review_rows(conn, now - desk.config.stall_seconds)
        ids = sorted({str(i["lead_id"]) for i in items if i["lead_id"]})
        names = dict(conn.execute(_NAMES, (json.dumps(ids),)).fetchall())
        failed_ids = sorted({str(i["lead_id"]) for i in items if i["kind"] == "failed" and i["lead_id"]})
        marks = conn.execute(_MARKS, (json.dumps(failed_ids),)).fetchall() if failed_ids else []
        prior = {r[0] for r in marks if r[1] == 1}
        crm_open = {r[0] for r in marks if r[2] == CRM_NOT_ASKED}
        crm_maybe = {r[0] for r in marks if r[2] == CRM_STARTED}
    out = []
    for item in items:
        sent = item["kind"] == "failed" and item["lead_id"] in prior
        full = {
            **item,
            "lead_name": names.get(item["lead_id"]),
            **_describe(
                item, desk.config.stall_seconds, sent, item["lead_id"] in crm_open, item["lead_id"] in crm_maybe
            ),
        }
        out.append(full)
    out.sort(key=lambda i: _TAB_ORDER.index(i["tab"]))  # stable: the desk's order (oldest first) holds inside a tab
    return HTTP_OK, {
        "now": now,
        "items": out,
        "tabs": {t: sum(1 for i in out if i["tab"] == t) for t in _TAB_ORDER},
    }


# -- the request log (one row per receipt, newest first)
_LOG_ROWS = """
SELECT r.received_at, r.receipt_id, r.source, r.source_ref, r.outcome, l.lead_id, l.name, f.state, r.differing_fields,
       f.last_error, f.ambiguous_prior
  FROM receipts r
  LEFT JOIN leads l ON l.lead_id = r.outcome_lead_id
  LEFT JOIN followups f ON f.lead_id = l.lead_id
 WHERE (:first = 1 OR r.received_at < :at OR (r.received_at = :at AND r.receipt_id < :id))
 ORDER BY r.received_at DESC, r.receipt_id DESC LIMIT :n
"""
_LOG_TOTAL = "SELECT count(*) FROM receipts"
_LOG_RESULT: Final = {"new": "new", "duplicate": "duplicate", "review": "review", None: "open"}
_LOG_KEYS = (
    "request_id",
    "received_at",
    "door",
    "result",
    "lead_id",
    "lead_name",
    "followup_state",
    "new_details",
    "state_view",
)
_CURSOR_MAX: Final = 10**MAX_DIGITS - 1


def _cursor(raw: str) -> tuple[int, int] | None:
    """(received_at, receipt_id) from "<received_at>:<receipt_id>"; None for anything else."""
    at, sep, rid = raw.partition(":")
    a, b = _digits(at, 0, _CURSOR_MAX), _digits(rid, 0, _CURSOR_MAX)
    return (a, b) if sep and a is not None and b is not None else None


def _log_row(r: tuple[Any, ...]) -> dict[str, Any]:
    """One receipt as a log line. Only the request number, source, result, lead id, lead display name, the follow-up
    state and whether a repeat carried new details (a yes or no) leave: never email, phone, company or message.
    A value the table's own checks should have refused is shown as "review" (a person must look) or "unknown",
    never as the raw stored text."""
    result = _LOG_RESULT.get(r[4], "review")
    state = r[7] if r[7] in FOLLOWUP_STATES else (None if r[7] is None else "unknown")
    door = r[2] if r[2] in SOURCES else "unknown"
    new_details = result == "duplicate" and r[8] not in (None, "", "[]")  # a repeat that differs from the first request
    view = None if state is None else _state_view(state, r[9], r[10])
    return dict(zip(_LOG_KEYS, (r[3], r[0], door, result, r[5], r[6], state, new_details, view), strict=True))


def _log(desk: Desk, query: str) -> tuple[int, dict[str, Any]]:
    values, bad = _params(query, ("limit", "before"))
    if bad is not None:
        return _bad_query(bad)
    limit = LOG_LIMIT_DEFAULT if "limit" not in values else _digits(values["limit"], 1, LOG_LIMIT_MAX)
    if limit is None:
        return _bad_query("limit")
    after = (0, 0) if "before" not in values else _cursor(values["before"])
    if after is None:
        return _bad_query("before")
    args = {"first": 0 if "before" in values else 1, "at": after[0], "id": after[1], "n": limit + 1}
    with _snapshot(desk) as (conn, _now):
        rows = conn.execute(_LOG_ROWS, args).fetchall()
        total = conn.execute(_LOG_TOTAL).fetchone()[0]
    page = rows[:limit]
    more = f"{page[-1][0]}:{page[-1][1]}" if len(rows) > limit else None
    return HTTP_OK, {"items": [_log_row(r) for r in page], "next": more, "total": total}


# -- what the AI step can read, what the desk stores, how access is limited
class DataMapRefused(RuntimeError):
    """The AI step's inputs are not the documented ones; the page must not describe them from a stale list."""


_MAIL: Final = "Mail step"
_CRM_REAL: Final = "CRM"
_CRM_STAND_IN: Final = "CRM (stand-in)"
# The lead fields each CRM kind is given on the first copy. The stand-in gets the whole record; the HubSpot kind sends
# only what crm_hubspot.put sends: email, the lead number (hadavin_lead_id), first and last name, company. The lead
# stage and the lead owner are copied later by set_fields, and only when a lead flow file is set (CRM_FLOW_FIELDS).
CRM_FIELDS_SENT: Final = {
    CRM_STAND_IN: frozenset({"lead_id", "name", "email", "company", "door", "ai_label"}),
    "hubspot": frozenset({"lead_id", "name", "email", "company"}),
}
CRM_FLOW_FIELDS: Final = frozenset({"lead_stage", "lead_owner"})
_STAGE_PURPOSE: Final = "show how far a new lead has come; named in the reminder email"
_STAGE_PURPOSE_STAND_IN: Final = _STAGE_PURPOSE + "; copied to the stand-in CRM, where nothing reacts to it"
_STAGE_PURPOSE_REAL: Final = (
    _STAGE_PURPOSE
    + "; copied to the CRM's lead status field: any automation in that CRM that watches the status may run"
)
# (field, what it is for, where the desk sends it besides its own records); the CRM and the AI step are added from
# the loaded kinds and the AI step's real inputs
_STORED: Final = (
    ("lead_id", "number each lead, on every page; the AI is given it", (_MAIL,)),
    ("name", "show who the lead is", (_MAIL,)),
    ("email", "identify the lead and send the follow-up", (_MAIL,)),
    ("phone", "a number a person could call; the desk makes no phone calls (kept when given)", ()),
    ("company", "show who the lead works for (kept when given)", ()),
    ("message", "keep what the lead wrote (kept when given)", ()),
    ("request_payload", "keep the whole request as it arrived, to show what a repeat changed", ()),
    ("request_id", "recognise a request that arrives again", ()),
    ("door", "show which source the request came through", ()),
    ("times", "show when each request arrived and when each step happened", ()),
    ("follow_up_state", "show whether the follow-up was sent", ()),
    ("ai_label", "keep the AI's answer: hot, warm or cold", ()),
    (
        "later_decision",
        "keep who approved or denied a later message, through which channel and when (approval mode only)",
        (),
    ),
    ("lead_owner", "show which person on the list owns a new lead; named in the reminder email", (_MAIL,)),
    ("lead_stage", _STAGE_PURPOSE, (_MAIL,)),
    ("reminder_state", "show whether a reminder email was sent for a lead nobody replied to", ()),
)


def _declared(func: Any, skip_self: bool) -> list[str]:
    names = []
    for i, p in enumerate(inspect.signature(func).parameters.values()):
        if i == 0 and skip_self:
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise DataMapRefused("the AI step takes a variable argument list")
        names.append(p.name)
    return names


def ai_inputs(qualifier: Qualifier) -> list[str]:
    """Parameter names of Qualifier.qualify, read now. Refuses (DataMapRefused) when a name is not in
    AI_INPUT_FIELDS, or when the loaded qualifier takes other parameters than the contract says."""
    names = _declared(Qualifier.qualify, True)
    unknown = [n for n in names if n not in AI_INPUT_FIELDS]
    if unknown:
        raise DataMapRefused("the AI step takes an input that is not documented: " + ", ".join(unknown))
    if _declared(qualifier.qualify, False) != names:  # a bound method: no self
        raise DataMapRefused("the loaded AI step does not take the inputs of the contract")
    return names


def _data_map(desk: Desk, demo_controls: bool = False) -> dict[str, Any]:
    receives = ai_inputs(desk.qualifier)
    given = {AI_INPUT_FIELDS[n] for n in receives}
    mode = getattr(type(desk.qualifier), "mode", None)
    stand_in = desk.config.crm == CRM_STAND_IN
    crm_fields = CRM_FIELDS_SENT[desk.config.crm] | (
        CRM_FLOW_FIELDS if desk.config.leadflow is not None else frozenset()
    )
    crm_label = _CRM_STAND_IN if stand_in else _CRM_REAL
    stage_purpose = (
        _STAGE_PURPOSE
        if "lead_stage" not in crm_fields  # no lead flow file: nothing is copied, so nothing is said about a copy
        else _STAGE_PURPOSE_STAND_IN
        if stand_in
        else _STAGE_PURPOSE_REAL
    )
    sender = getattr(
        desk.outbox, "sender_address", None
    )  # the current setting of a mail step that has one; None otherwise
    return {
        "mail_sender": sender if isinstance(sender, str) and sender else None,
        "ai_step": {
            "receives": receives,
            "never_receives": [f for f in LEAD_CONTENT_FIELDS if f not in given],
            "mode": mode if mode in QUALIFIER_MODES else QUALIFIER_MODE_FALLBACK,
            "note": AI_STEP_NOTE,
        },
        "stored": [
            {
                "field": f,
                "where": "desk records",
                "purpose": stage_purpose if f == "lead_stage" else p,
                "sent_to": [*to, *((crm_label,) if f in crm_fields else ()), *(("AI",) if f in given else ())],
            }
            for f, p, to in _STORED
        ],
        "access": {
            "bind": desk.config.bind,
            "portal_pages_read_only": True,
            "portal_actions": [*PORTAL_ACTIONS, *((DEMO_CONTROLS_ACTION,) if demo_controls else ())],
            "host_check": True,
            "origin_check": True,
            "login": False,
            "encryption_at_rest": False,
            "masking": False,
        },
    }
