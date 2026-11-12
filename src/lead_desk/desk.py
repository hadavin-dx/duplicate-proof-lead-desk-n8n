"""The desk API. Standard library only.

One SQLite connection per thread (threading.local); every write is BEGIN IMMEDIATE; no network call is made while a
transaction is open. Logs carry exception class names only (no addresses, no names)."""

from __future__ import annotations

import functools
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from lead_desk import db, identity
from lead_desk.config import Config
from lead_desk.constants import (
    ADAPTER_WORKER_CAPS,
    AI_LABELS,
    APPROVER_ID_PATTERN,
    BUSY_WAIT_SECONDS,
    CALL_DEADLINE_MAX_SECONDS,
    FOLLOWUP_BODY_TEMPLATE,
    FOLLOWUP_BODY_TEMPLATE_PROVIDER,
    FOLLOWUP_SUBJECT_TEMPLATE,
    HTTP_ACCEPTED,
    HTTP_BAD_REQUEST,
    HTTP_BUSY,
    HTTP_CONFLICT,
    HTTP_CREATED,
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_OK,
    LEAD_ID_HEX_CHARS,
    LEAD_ID_PREFIX,
    LOCK_RETRIES,
    MAX_DIGITS,
    MIRROR_MAX_TRIES,
    MIRROR_PER_PASS,
    MIRROR_RETRY_WAIT_SECONDS,
    MODEL_CALL_BUDGET_SECONDS,
    MODEL_GRACE_MARGIN_SECONDS,
    NUDGE_KEY_PREFIX,
    NUDGES_PER_PASS,
    SOURCE_REF_MAX,
    SOURCE_REF_MIN,
    SOURCES,
    is_positive_seconds,
    validate_model_budget,
    validate_worker_caps,
)
from lead_desk.contract import (
    AdapterBusy,
    Crm,
    CrmTimeout,
    CrmUnreachable,
    ModelAnswerLost,
    ModelBusy,
    ModelTimeout,
    Outbox,
    OutboxModeMismatch,
    Qualifier,
    RefreshTimeout,
    SendAmbiguous,
    SendNoDescriptors,
    SendRefused,
    SendUnreachable,
    is_fd_exhaustion,
)
from lead_desk.gate import (
    BUSY_ERRORS,
    CRM_WORKERS_MAX,
    DECIDE_FAIL_LIMIT,
    DRIVE_MAX,
    DRIVE_WAIT_SECONDS,
    GATE_WAIT_SECONDS,
    INFLIGHT_MAX,
    INFLIGHT_WAIT_SECONDS,
    MIRROR_PASS_SECONDS,
    MODEL_HUNG_MAX,
    NO_DESCRIPTORS_BACKOFF_SECONDS,
    NO_DESCRIPTORS_ERROR,
    NUDGE_PASS_SECONDS,
    OUTBOX_REFRESH_SECONDS,
    REAPER_INTERVAL_SECONDS,
    REAPER_PASS_SECONDS,
    REFRESH_WORKERS_MAX,
    REQUEST_DEADLINE_SECONDS,
    SEND_BUSY_ERROR,
    SEND_MAX_ATTEMPTS,
    SEND_WORKERS_MAX,
    SENDING_GRACE_BUSY_WAITS,
    WRITE_DEADLINE_SECONDS,
    FairSemaphore,
    GateTimeout,
    gate_for,
    wait_before_drive,
)
from lead_desk.hooks import Hooks

log = logging.getLogger("lead_desk")
_T = TypeVar("_T")
for _budget in (MODEL_CALL_BUDGET_SECONDS, CALL_DEADLINE_MAX_SECONDS):  # the model step and every other bounded call
    validate_model_budget(
        _budget,
        MODEL_GRACE_MARGIN_SECONDS,
        SENDING_GRACE_BUSY_WAITS * BUSY_WAIT_SECONDS,
        WRITE_DEADLINE_SECONDS,
        BUSY_WAIT_SECONDS,
    )
validate_worker_caps(DRIVE_MAX, ADAPTER_WORKER_CAPS)


class _MirrorWait:
    """The wait before a failed CRM copy is tried again, per lead: (monotonic time, stage rank it was set for). It is
    in memory only (a restart starts the waits afresh; the tries in the database still cap them). A request thread
    and the reaper both touch it, so every touch holds one small lock; the lock is never held across a database
    write or a CRM call. An entry counts only while the lead is still at that stage rank."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: dict[str, tuple[float, int]] = {}

    def set(self, lead_id: str, until: float, rank: int) -> None:
        with self._lock:
            self._d[lead_id] = (until, rank)

    def clear(self, lead_id: str) -> None:
        with self._lock:
            self._d.pop(lead_id, None)

    def waiting(self, now: float) -> list[list[Any]]:
        """The [lead id, rank] pairs still waiting; entries whose time has passed are dropped (no leak)."""
        with self._lock:
            for lid in [k for k, (until, _) in self._d.items() if until <= now]:
                del self._d[lid]
            return [[lid, rank] for lid, (_, rank) in self._d.items()]

    def __contains__(self, lead_id: object) -> bool:
        with self._lock:
            return lead_id in self._d


@dataclass
class _Call:
    """The hand-over between the desk and the worker of one bounded adapter call (guarded by Desk._worker_lock)."""

    done: threading.Event = field(default_factory=threading.Event)
    answer: Any = None
    error: BaseException | None = None
    finished: bool = False  # the worker has stored its result
    abandoned: bool = False  # the desk stopped waiting: the worker drops its result


class _GateBusy(sqlite3.OperationalError):
    """The write gate line did not move within the wait. It is a lock answer like any other (db.is_busy is true), but
    it is NOT tried again at once: a retry would queue at the tail of the line and wait just as long again."""


class DeskSettingRefused(ValueError):
    """The Desk refuses to be built with a setting that would break its once-only guarantee (the AI adapter declares a
    time limit that does not fit the stuck grace). The launcher turns exactly this one into exit code 4."""


class _IncompleteCheck(Exception):
    """A payload that passed identity.check_payload came back without a required field."""


# What the decision step treats as "this payload cannot be stored": it gets verdict review, never an open receipt.
_UNSTORABLE = (
    sqlite3.IntegrityError,
    sqlite3.DataError,
    OverflowError,
    RecursionError,  # a stored payload nested so deeply that json.loads gives up
    json.JSONDecodeError,
    UnicodeError,  # a lone surrogate that slipped past the checks
    _IncompleteCheck,
)
CRM_NOT_ASKED, CRM_STARTED, CRM_DONE = 0, 2, 1  # followups.crm_done values (2 = intent mark, written first)
_RETRYABLE = ("refused", "unreachable", "ambiguous")  # send results that go back to `pending` until the cap
_CLAIM_SQL = {  # the only two claims there are, as whole statements (nothing is built from text)
    ("pending",): (
        "UPDATE followups SET state='sending', attempts=attempts+1, send_started=0,"
        " auto_attempts=CASE WHEN state='failed' THEN 1 ELSE auto_attempts+1 END, updated_at=?"
        " WHERE lead_id=? AND state = 'pending'"
    ),
    ("pending", "failed"): (
        "UPDATE followups SET state='sending', attempts=attempts+1, send_started=0,"
        " auto_attempts=CASE WHEN state='failed' THEN 1 ELSE auto_attempts+1 END, updated_at=?"
        " WHERE lead_id=? AND state IN ('pending','failed')"
    ),
}

HINTS = {
    "R": "Send a corrected submission with a NEW request number.",
    "D": "Informational only, no action needed.",
    "O": "Send the same request number again, or restart the desk.",
    "N": "A reminder is never sent again by the desk. Check your mailbox, then mark it seen.",
}
HINT_INTERNAL_ERROR = (
    "The desk itself could not decide this receipt (a fault on the desk side, not in the request) and the same fault "
    "came up again and again. A person must look at the desk log and fix the cause. The stored request stays as it is; "
    "once the cause is fixed, send the lead again with a new request number."
)
HINT_NOT_UNKNOWN = "Only a follow-up in state unknown can be resolved."
HINT_CRM_UNCONFIRMED = (
    "The CRM may or may not hold this lead. Look in the CRM, then answer crm_present (the row is there: Retry goes "
    "straight to the email) or not_sent (the row is not there: Retry asks the CRM again). sent is refused here: no "
    "email was ever tried."
)
HINT_EMAIL_UNKNOWN = (
    "The CRM row is already done; what is unclear is the email. Look at the mail step, then answer sent (it left) or "
    "not_sent (it did not). crm_present is refused here. If a call for this lead is still running, not_sent is "
    "refused until it has ended."
)
HINT_CALL_RUNNING = (
    "A call for this lead that the desk stopped waiting for is still running and may yet land. Wait until the calls "
    "still running (hung_workers on /counts) are gone, look again, then answer."
)
_RESOLVE_DECISIONS = ("sent", "not_sent", "crm_present")
_RECEIPT_KEYS = (
    "receipt_id",
    "source",
    "source_ref",
    "payload",
    "attempts",
    "outcome",
    "outcome_lead_id",
    "outcome_reason",
    "differing_fields",
)
_RECEIPT_BY_ID = (
    "SELECT receipt_id, source, source_ref, payload, attempts, outcome, outcome_lead_id, outcome_reason,"
    " differing_fields FROM receipts WHERE receipt_id=?"
)
_ITEM_EXISTS = {
    "R": "SELECT 1 FROM receipts WHERE receipt_id=? AND outcome = 'review'",
    "D": "SELECT 1 FROM receipts WHERE receipt_id=?"
    " AND ((outcome = 'duplicate' AND differing_fields <> '[]') OR payload_conflict = 1)",
    "O": "SELECT 1 FROM receipts WHERE receipt_id=? AND outcome IS NULL",
}
# Approval mode (DESK_LATER_MODE=approval): the word is approve:<approver id> or deny:<approver id>
_LATER_WORD = re.compile(rf"(approve|deny):({APPROVER_ID_PATTERN})", re.ASCII)
_STORED_WORD = {"approve": "approved", "deny": "denied"}
DECISION_KEYS = ("decision", "decided_by", "decided_by_name", "channel", "decided_at")  # the winner's row, in replies
_LATER_DECIDABLE = "SELECT 1 FROM receipts WHERE receipt_id=? AND outcome = 'duplicate' AND differing_fields <> '[]'"
_DECIDE = (
    "INSERT INTO later_decisions (receipt_id, decision, approver_id, approver_name, channel, decided_at)"
    " VALUES (?, ?, ?, ?, 'portal', ?) ON CONFLICT(receipt_id) DO NOTHING"
)
_DECIDED = "SELECT decision, approver_id, approver_name, channel, decided_at FROM later_decisions WHERE receipt_id=?"
_REVIEW_SQL = """
SELECT item_id, item_type, kind, reason, lead_id, since, source FROM (
  SELECT item_id, item_type, kind, reason, lead_id, since, source FROM review_items
  UNION ALL
  SELECT 'O' || receipt_id, 'receipt', 'stalled', 'open receipt', NULL, received_at, source
    FROM receipts WHERE outcome IS NULL AND received_at < :before
  UNION ALL
  SELECT 'F' || f.lead_id, 'followup', 'stalled', f.state, f.lead_id, f.updated_at, l.source
    FROM followups f JOIN leads l USING (lead_id) WHERE f.state IN ('pending','sending') AND f.updated_at < :before
) ORDER BY since, item_id
"""
_COUNTS_SQL = """
SELECT (SELECT count(*) FROM receipts),
       (SELECT count(*) FROM receipts WHERE outcome = 'new'),
       (SELECT count(*) FROM receipts WHERE outcome = 'duplicate'),
       (SELECT count(*) FROM receipts WHERE outcome = 'review'),
       (SELECT count(*) FROM receipts WHERE outcome IS NULL),
       (SELECT count(*) FROM followups WHERE state IN ('pending','sending')),
       (SELECT count(*) FROM followups WHERE state = 'failed'),
       (SELECT count(*) FROM followups WHERE state = 'unknown'),
       (SELECT count(*) FROM followups WHERE state = 'sending')
"""


def review_rows(conn: sqlite3.Connection, before: int) -> list[dict[str, Any]]:
    """The things that need a person, oldest first, with the hint of the rows that carry one. `before` is the stall
    cutoff (now minus stall_seconds). Read-only: the portal runs it on its own read-only connection."""
    rows = conn.execute(_REVIEW_SQL, {"before": before}).fetchall()
    keys = ("item_id", "item_type", "kind", "reason", "lead_id", "since", "source")
    items = [dict(zip(keys, r, strict=True)) for r in rows]
    for item in items:  # a hint only where the person needs one (keeps the item shape of the other rows)
        if item["kind"] == "unknown" and item["reason"] == "crm_unconfirmed":
            item["hint"] = HINT_CRM_UNCONFIRMED + " not_sent: an extra CRM row may need deleting by hand."
        elif item["item_type"] == "receipt" and item["reason"] == "internal_error":
            item["hint"] = HINT_INTERNAL_ERROR
    return items


def default_now() -> int:
    return int(time.time())  # wall-clock: calendar stamp


@dataclass
class Reply:
    status: int
    body: Any  # as described in contract.md (a dict, or a list for GET /review)
    drive_lead_id: str | None = None  # set when the caller must now run the follow-up


class Desk:
    def __init__(
        self,
        config: Config,
        crm: Crm,
        outbox: Outbox,
        qualifier: Qualifier,
        hooks: Hooks | None = None,
        now: Callable[[], int] = default_now,
        schema_sql: str
        | None = None,  # the schema text to apply instead of the shipped schema.sql (default: the shipped file)
    ) -> None:
        self.config = config
        self.crm = crm
        self.outbox = outbox
        self.qualifier = qualifier
        self.hooks = hooks if hooks is not None else Hooks()
        self._now = now
        self._schema_sql = schema_sql
        # An AI adapter may declare how long one call can take. None (not declared) is accepted: the desk bounds every
        # model call itself (_bounded). A declaration that is not a number above 0 is refused (a NaN would pass every
        # comparison; a whole number too large for a float counts as not a number), and so is one above
        # MODEL_CALL_BUDGET_SECONDS: the desk cuts every model call at that budget, so an adapter that honestly
        # declares more could never answer in time (every lead would use up its tries on timeouts). Equal is accepted.
        declared = getattr(qualifier, "timeout_seconds", None)
        if declared is not None:
            if not is_positive_seconds(declared):
                raise DeskSettingRefused(
                    f"the AI adapter's timeout_seconds must be a number of seconds above 0, got {declared!r}"
                )
            if declared > MODEL_CALL_BUDGET_SECONDS:
                raise DeskSettingRefused(
                    f"the AI adapter's timeout_seconds ({declared!r}) is above the {MODEL_CALL_BUDGET_SECONDS} s the "
                    "desk waits for one model call: the desk would cut every call before the adapter's own limit"
                )
        if config.later_mode == "approval" and not config.approvers:
            raise DeskSettingRefused("approval mode needs at least one approver (DESK_APPROVERS_FILE)")
        if (problem := config.reminder_setting_problem()) is not None:  # refused here too, not only in server.main
            raise DeskSettingRefused(problem)
        # approver id -> name; names are looked up here, never taken from a caller
        self._approvers = dict(config.approvers)
        self._local = threading.local()
        self._worker_lock = threading.Lock()  # guards the worker counts below and the hand-over of every bounded call
        self._workers: dict[str, int] = {kind: 0 for kind, _ in ADAPTER_WORKER_CAPS}  # running workers (abandoned too)
        self._abandoned: dict[str, int] = {kind: 0 for kind, _ in ADAPTER_WORKER_CAPS}  # the ones the desk gave up on
        self._tagged: dict[tuple[str, str], int] = {}  # (kind, lead id) -> workers still running for that lead
        self._busy_logged: set[str] = set()  # kinds whose give-back burst has been logged (cleared when a slot frees)
        self._closed_logged: set[str] = set()  # kinds whose "every slot is hung" error has been logged
        self._mirror_not_before = _MirrorWait()  # lead id -> (monotonic time, stage rank) the copy waits until
        self._nudge_pause_until = 0.0  # monotonic time before which the reminder part claims nothing (memory only)
        self._rejected = 0
        self._rejected_lock = threading.Lock()
        self._inflight = FairSemaphore(INFLIGHT_MAX)
        self._drives = FairSemaphore(DRIVE_MAX)
        db_text = str(config.db_path)
        # resolved ONCE here: a later chdir cannot send a new thread's connection to another file
        self._db_path: Path | str = db_text if db_text in ("", ":memory:") else Path(db_text).resolve()
        self._gate = gate_for(self._db_path)  # one write line per lead book (not re-entrant, see gate.py)
        self._decide_failures: dict[int, int] = {}  # receipt id -> consecutive non-busy decide failures (in memory)
        self._decide_lock = threading.Lock()
        self._integrity_errors = 0  # IntegrityErrors met while deciding (a constraint bug, not a bad payload); /counts
        self._crm_refresh_failed = False
        self._seen_open: dict[int, int] = {}  # open receipt id -> desk time the reaper first saw it open
        self._transient_errors = 0  # database faults that are not a lock met while deciding (the receipt stays open)
        self._poked: set[int] = set()  # receipts left open by a busy answer: the reaper decides them at its next pass
        self._refreshed_at: int | None = None  # clock time of the last capability read
        self._used = (
            False  # set by the first call that serves, drives, claims, retries or resolves; startup() refuses after it
        )
        self._started = threading.Event()  # set when startup() has finished its quick repairs
        self._background = False  # start_background() was called: pending follow-ups are driven by the reaper
        self._bg_lock = threading.Lock()  # start_background / stop_background change the reaper state together
        self._thread: threading.Thread | None = None  # the running reaper, or None
        self._thread_stop: threading.Event | None = None  # that reaper's own stop signal (a fresh one per thread)
        self._last_reap_at: int | None = None  # epoch seconds when the last reaper pass ended (shown on /counts)
        self._retiring: set[threading.Thread] = set()  # stopped reapers that have not ended yet (set before the join)

    @property
    def stuck_grace_seconds(self) -> int:
        """How long a `sending` row may go unstamped before the reaper puts it back: the send timeout plus
        SENDING_GRACE_BUSY_WAITS busy waits (30 s with the defaults)."""
        return int(self.config.send_timeout_seconds + SENDING_GRACE_BUSY_WAITS * BUSY_WAIT_SECONDS)

    # -- plumbing
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self._db_path)
            self._local.conn = conn
        return cast(sqlite3.Connection, conn)

    @contextmanager
    def _write(self, conn: sqlite3.Connection) -> Iterator[None]:
        """BEGIN IMMEDIATE ... COMMIT; any exception (a simulated crash too) rolls back first. The writer first
        waits its turn in the process-wide fair gate (gate.py); a wait that runs out is the same "database is locked"
        error as an SQLite busy timeout, so every caller treats it as busy."""
        wait = GATE_WAIT_SECONDS
        for deadline in (getattr(self._local, "call_deadline", None), getattr(self._local, "req_deadline", None)):
            if deadline is not None:
                wait = min(wait, deadline - time.monotonic())
        try:
            self._gate.acquire(wait, low=bool(getattr(self._local, "low", False)))
        except GateTimeout:
            raise _GateBusy("database is locked (write gate wait ran out)") from None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        finally:
            self._gate.release()

    def _locked_retry(self, fn: Callable[[], _T]) -> _T:
        """Run fn up to LOCK_RETRIES times while the database stays locked (each try already waited the busy time).
        All tries of one call share ONE deadline (WRITE_DEADLINE_SECONDS, and the request's own deadline if there is
        one). A gate wait that ran out is not tried again: the line has not moved, and a new ticket would go to its
        tail."""
        self._local.call_deadline = time.monotonic() + WRITE_DEADLINE_SECONDS
        try:
            for attempt in range(LOCK_RETRIES):
                try:
                    return fn()
                except sqlite3.OperationalError as e:
                    if not db.is_busy(e) or isinstance(e, _GateBusy) or attempt == LOCK_RETRIES - 1:
                        raise
                    if time.monotonic() >= self._local.call_deadline:
                        raise
            raise RuntimeError("unreachable")
        finally:
            self._local.call_deadline = None

    def _error_reply(self, exc: Exception, receipt_id: int | None, with_receipt: bool = True) -> Reply:
        busy = db.is_busy(exc)
        log.warning("request failed: %s", type(exc).__name__)
        body: dict[str, Any] = {"error": "busy" if busy else "internal"}
        if with_receipt:
            body["receipt_id"] = receipt_id
        return Reply(HTTP_BUSY if busy else HTTP_INTERNAL_ERROR, body)

    def close(self) -> None:
        """Closes the calling thread's connection (the next use opens a new one)."""
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            conn.close()

    def note_rejected_envelope(self) -> None:
        with self._rejected_lock:
            self._rejected += 1

    def now(self) -> int:
        """The desk's clock (whole seconds); the portal reads time from here so a replaced clock holds for it too."""
        return self._now()

    def stalled_before(self) -> int:
        """Work older than this many seconds ago counts as stalled."""
        return self._now() - self.config.stall_seconds

    def rejected_envelopes(self) -> int:
        """How many requests the front door refused without a receipt (shown by the portal)."""
        with self._rejected_lock:
            return self._rejected

    # -- start-up
    def _refresh_outbox(self) -> bool:
        """Read the sender's capability again when the adapter can (HttpOutbox.refresh). False when it could not."""
        self._refresh_crm()
        refresh = getattr(self.outbox, "refresh", None)
        if refresh is None:
            return True
        self._refreshed_at = self._now()
        self._local.outbox_refresh_no_fd = False
        try:
            self._bounded("refresh", refresh, self._call_deadline(), lambda: RefreshTimeout("no answer in time"))
        except Exception as e:  # the cached capability stays; sends fail visibly if the sender is really down
            log.warning("outbox capability not re-read: %s", type(e).__name__)
            self._local.outbox_refresh_no_fd = is_fd_exhaustion(e)
            return False
        return True

    def _refresh_crm(self) -> None:
        """Read the CRM's mode again when the adapter can (HttpCrm.refresh). A failure keeps the cached value.

        Too many running adapter calls (`AdapterBusy`, for example four hung refreshes) count as a failed read: the CRM
        is then judged not keyed until a read succeeds. That is the safe direction (the lead waits, nothing is sent)."""
        self._local.crm_refresh_no_fd = False
        self._local.crm_refresh_failed = False
        refresh = getattr(self.crm, "refresh", None)
        if refresh is None:
            return
        try:
            self._bounded("refresh", refresh, self._call_deadline(), lambda: RefreshTimeout("no answer in time"))
        except Exception as e:
            self._local.crm_refresh_failed = True
            self._local.crm_refresh_no_fd = is_fd_exhaustion(
                e
            )  # the read could not even be made: says nothing of the CRM
            if not self._crm_refresh_failed:  # said once per change of state, not on every pass
                log.warning("crm capability not re-read: %s", type(e).__name__)
            self._crm_refresh_failed = True
            self._forget_crm_capability()
        else:
            self._crm_refresh_failed = False

    def _forget_crm_capability(self) -> None:
        """A failed re-read must not leave an old "keyed" in force: the adapter drops it (None counts as not keyed)."""
        forget = getattr(self.crm, "forget_capability", None)
        if forget is not None:
            forget()

    def _requeue_sending(self, older_than: int | None, mode_known: bool = True) -> int:
        """The `sending` rows of the transition table: keyed -> pending, blind -> unknown (a blind sender cannot say
        whether the email left). A row whose send never started (crm_done not 1, or no `send_started` mark) never
        reached the send, so it goes back to pending (or failed at the cap) whatever the sender is: the CRM rule decides
        at the next drive. Only a row whose send may have started needs the sender's mode; with mode_known False
        (the mode could not be read) the other rows are left alone.
        older_than=None: every `sending` row (start-up); else only rows not touched since that time (the reaper)."""
        keyed = self.outbox.capability == "keyed"
        conn = self._conn()
        now = self._now()

        def attempt() -> int:
            with self._write(conn):
                # a row that has used up its automatic tries is not put back for ever: keyed -> failed (a person's
                # Retry can still start it again), blind -> unknown
                return conn.execute(
                    "UPDATE followups SET state=CASE"
                    " WHEN (crm_done <> 1 OR send_started = 0) AND auto_attempts >= ? THEN 'failed'"
                    " WHEN crm_done <> 1 OR send_started = 0 THEN 'pending'"
                    " WHEN NOT ? THEN 'unknown' WHEN auto_attempts >= ? THEN 'failed' ELSE 'pending' END,"
                    " last_error='crash_while_sending',"
                    " ambiguous_prior=CASE WHEN crm_done = 1 AND send_started = 1 THEN 1 ELSE ambiguous_prior END,"
                    " updated_at=?"
                    " WHERE state='sending' AND (? IS NULL OR updated_at < ?)"
                    " AND (? OR crm_done <> 1 OR send_started = 0)",
                    (
                        SEND_MAX_ATTEMPTS,
                        int(keyed),
                        SEND_MAX_ATTEMPTS,
                        now,
                        older_than,
                        older_than,
                        int(mode_known),
                    ),
                ).rowcount

        return self._locked_retry(attempt)

    def startup(self) -> None:
        """init_schema, then the start-up sweep; call once, before the desk serves or drives anything (RuntimeError
        otherwise). Without start_background() it also drives every pending follow-up before it returns; with
        start_background() called first, that driving is left to the reaper thread so serving starts at once."""
        if self._used:
            raise RuntimeError("startup() must be called once, before the desk serves or drives anything")
        self._used = True
        conn = self._conn()
        db.init_schema(conn, self._schema_sql)
        flow = self.config.leadflow
        if flow is not None:  # a kept book must agree with the steps file: no step may change meaning
            known = {(s[0], i) for i, s in enumerate(flow.steps)}
            stored = {(str(a), int(b)) for a, b in conn.execute("SELECT DISTINCT step, step_rank FROM lead_state")}
            if not stored <= known:
                raise DeskSettingRefused("the steps file does not match the steps stored in the lead book")
        refreshed = self._refresh_outbox()
        # (1) finish every OPEN receipt from its stored payload
        open_ids = conn.execute("SELECT receipt_id FROM receipts WHERE outcome IS NULL ORDER BY receipt_id").fetchall()
        for (rid,) in open_ids:  # fetched first: _decide commits while this loop runs
            try:
                self._decide(int(rid))
            except Exception as e:  # the receipt stays open and shows as stalled
                log.warning("sweep: receipt left open: %s", type(e).__name__)
                if db.is_busy(e):
                    with self._decide_lock:  # the reaper tries it at its next pass, not after stall_seconds
                        self._poked.add(int(rid))
        # (2) the two `sending` rows of the transition table
        self._requeue_sending(None, refreshed)  # rows that never reached the send need no mode
        if not refreshed:  # the sender's mode is not known: guessing could send twice, so the reaper decides once read
            log.warning("start-up: sending rows left for the reaper (sender capability not read)")
        if flow is not None:  # no live sender exists at start: a reminder still `sending` may have left, never re-sent
            self._locked_retry(lambda: self._sweep_nudges(conn, self._now(), None))
        self._started.set()
        # (3) drive every pending follow-up; run_followup catches its own errors
        if not self._background:
            for (lead_id,) in conn.execute(
                "SELECT lead_id FROM followups WHERE state='pending' ORDER BY lead_id"
            ).fetchall():
                self.run_followup(lead_id)

    # -- background reaper
    def start_background(self, interval: float = REAPER_INTERVAL_SECONDS) -> None:
        """Start the reaper thread (daemon, stoppable with stop_background). Call it BEFORE startup(): the thread
        waits for startup() to finish its quick repairs, then every `interval` seconds runs reap_once(). A second call
        while a reaper runs does nothing. A reaper has its own stop signal; if any reaper stopped before is still
        finishing a pass, the new one waits for all of them to end, so two reapers never run a pass at the same time."""
        with self._bg_lock:
            if self._thread is not None:
                return
            stop = threading.Event()
            after = tuple(t for t in self._retiring if t.is_alive())
            self._retiring = set(after)
            thread = threading.Thread(
                target=self._reaper_loop, args=(interval, stop, after), name="desk-reaper", daemon=True
            )
            self._background = True
            self._thread_stop, self._thread = stop, thread
            thread.start()

    def stop_background(self, timeout: float = BUSY_WAIT_SECONDS * 2) -> None:
        """Ask the reaper to stop and wait for it (bounded). Afterwards the desk is not in background mode."""
        with self._bg_lock:
            stop, thread = self._thread_stop, self._thread
            self._thread_stop = self._thread = None
            self._background = False
            if thread is not None:
                self._retiring.add(thread)  # tracked BEFORE the join: a start during the join waits for this one too
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=timeout)
            if not thread.is_alive():
                with self._bg_lock:
                    self._retiring.discard(thread)

    def reaper_running(self) -> bool:
        """True while any reaper thread (the current one, or one stopped but still finishing a pass) is alive."""
        with self._bg_lock:
            return (self._thread is not None and self._thread.is_alive()) or any(t.is_alive() for t in self._retiring)

    def _stopping(self) -> bool:
        """True on a reaper thread that has been asked to stop (False on any other thread, for example one that
        calls reap_once itself)."""
        stop: threading.Event | None = getattr(self._local, "stop", None)
        return stop is not None and stop.is_set()

    def _reaper_loop(self, interval: float, stop: threading.Event, after: tuple[threading.Thread, ...] = ()) -> None:
        self._local.low = True  # repairs queue behind requests for the write gate
        self._local.stop = stop
        try:
            for older in after:  # every older reaper that is still finishing its last pass
                while older.is_alive():
                    if stop.is_set():
                        return
                    older.join(0.05)
            while not self._started.wait(0.05):
                if stop.is_set():
                    return
            while not stop.is_set():
                try:
                    self.reap_once()
                except BaseException as e:  # noqa: BLE001 - never let the thread die: the next pass tries again
                    log.warning("reaper pass failed: %s", type(e).__name__)
                self._last_reap_at = int(time.time())  # wall-clock: calendar stamp
                if stop.wait(interval):
                    break
        finally:
            self.close()

    def reap_once(self) -> dict[str, int]:
        """One repair pass (the reaper calls it; it can also be called directly). Returns counts of what it did.
        (1) open receipts older than stall_seconds get their verdict from the stored payload;
        (2) `sending` rows older than send timeout + 5 busy waits (30 s with the default 5 s each) get the start-up rule
            (keyed -> pending, blind ->
            unknown), after the sender's capability is read again (not done when it cannot be read). The capability is
            read again at most every OUTBOX_REFRESH_SECONDS, or at once while such a stuck row exists;
        (3) pending follow-ups that are due (fresh ones after a short grace, automatic retries after their backoff)
            are driven, as many as fit in REAPER_PASS_SECONDS of real time per pass."""
        self._used = True
        conn = self._conn()
        done = {"receipts": 0, "requeued": 0, "driven": 0, "unknown": 0, "mirrored": 0, "reminders": 0,
                "reminders_unknown": 0}  # fmt: skip
        now = self._now()
        self._restamp_future(conn, now)
        with self._decide_lock:
            poked = sorted(self._poked)  # a busy answer left these open: they do not wait for stall_seconds
        stalled = [
            int(r[0])
            for r in conn.execute(
                "SELECT receipt_id FROM receipts WHERE outcome IS NULL AND received_at <= ? ORDER BY receipt_id",
                (now - self.config.stall_seconds,),
            ).fetchall()
        ] + self._stalled_by_own_clock(conn, now)
        for rid in sorted(set(poked) | set(stalled)):
            try:
                _, here = self._decide(int(rid))
                done["receipts"] += int(here)
            except Exception as e:
                log.warning("reaper: receipt left open: %s", type(e).__name__)
        # strictly older than the grace: the clocks count whole seconds (truncated), so a difference equal to the grace
        # can be a real age just over grace - 1 s; "older than" is then a real age above the grace
        stuck_before = now - self.stuck_grace_seconds
        n_sending = conn.execute(
            "SELECT count(*) FROM followups WHERE state='sending' AND updated_at < ?",
            (stuck_before,),
        ).fetchone()[0]
        refreshed = True
        if n_sending or self._refreshed_at is None or now - self._refreshed_at >= OUTBOX_REFRESH_SECONDS:
            refreshed = self._refresh_outbox()  # a stuck row needs the current mode; otherwise only now and then
        if n_sending and not refreshed:
            log.warning("reaper: sending rows that may have sent left as they were (sender capability not read)")
        if n_sending:
            try:
                done["requeued"] = self._requeue_sending(stuck_before, refreshed)  # rows really changed
            except Exception as e:
                log.warning("reaper: sending rows left as they were: %s", type(e).__name__)
        due = []
        for lead_id, tries, updated_at, ambiguous, last_error in conn.execute(
            "SELECT lead_id, auto_attempts, updated_at, ambiguous_prior, last_error FROM followups"
            " WHERE state='pending' ORDER BY updated_at, lead_id"
        ).fetchall():
            if updated_at + wait_before_drive(int(tries), last_error) <= now:
                due.append((str(lead_id), bool(ambiguous)))
        started = time.monotonic()  # real time, not the desk clock: the budget bounds how long THIS pass may run
        for lead_id, ambiguous_prior in due:
            if self._stopping() or time.monotonic() - started >= REAPER_PASS_SECONDS:
                break  # the rest is still due and comes first in the next pass
            if ambiguous_prior and self.outbox.capability == "blind":
                self._pending_to_unknown(lead_id)  # the sender became blind: do not send again blindly
                done["unknown"] += 1
                continue
            self.run_followup(lead_id)
            done["driven"] += 1
        try:
            self._mirror_pass(done)
        except Exception as e:  # noqa: BLE001 - one part's fault never stops the next part or the next pass
            log.warning("reaper: copy part stopped: %s", type(e).__name__)  # class name only, never the text
        try:
            self._nudge_pass(done)
        except Exception as e:  # noqa: BLE001 - same for the reminder part
            log.warning("reaper: reminder part stopped: %s", type(e).__name__)
        return done

    def _mirror_pass(self, done: dict[str, int]) -> None:
        """Copy the lead owner and the lead stage to the CRM (own clock, own error guard). Only for a lead whose CRM
        row exists (crm_done = 1). The step and rank read in the SELECT are the ones sent and recorded, so a step
        posted meanwhile stays above `crm_step_done` and is copied by the next pass. An owner the CRM cannot take
        (left the rota, or no crm_owner_id) is not sent and not selected again for the owner alone; the status is."""
        lf = self.config.leadflow
        if lf is None:
            return
        started = time.monotonic()
        status_of = {s[0]: s[2] for s in lf.steps}
        crm_owner_of = {r[0]: r[2] for r in lf.rota}
        copyable = json.dumps([r[0] for r in lf.rota if r[2]])
        now_mono = (
            time.monotonic()
        )  # leads still backing off after a failed try are not selected (they cannot starve others)
        backing_off = json.dumps(self._mirror_not_before.waiting(now_mono))  # [lead id, stage rank] pairs
        rows = (
            self._conn()
            .execute(
                "SELECT s.lead_id, l.email_key, s.owner_id, s.step, s.step_rank, s.crm_tries"
                " FROM lead_state s JOIN followups f ON f.lead_id = s.lead_id JOIN leads l ON l.lead_id = s.lead_id"
                " WHERE f.crm_done = 1 AND s.crm_tries < ? AND NOT EXISTS (SELECT 1 FROM json_each(?) w"
                "   WHERE json_extract(w.value, '$[0]') = s.lead_id AND json_extract(w.value, '$[1]') = s.step_rank)"
                " AND (s.crm_step_done < s.step_rank"
                "      OR (s.crm_owner_done = 0 AND s.owner_id IN (SELECT value FROM json_each(?))))"
                " ORDER BY s.changed_at, s.lead_id LIMIT ?",
                (MIRROR_MAX_TRIES, backing_off, copyable, MIRROR_PER_PASS),
            )
            .fetchall()
        )
        for lead_id, email_key, owner_id, step, rank_sent, _tries_before in rows:
            if self._stopping() or time.monotonic() - started >= MIRROR_PASS_SECONDS:
                return
            owner_sent = crm_owner_of.get(owner_id, "") if owner_id in json.loads(copyable) else ""
            status_sent = status_of[step]
            try:
                self._bounded(
                    "crm",
                    # partial, not a lambda: this row's values are bound here, so a worker that runs late still sends
                    # them, not the next row's; set_fields is only looked up here and is called in the worker
                    functools.partial(self.crm.set_fields, lead_id, email_key, owner_sent, status_sent),
                    self._call_deadline(),
                    lambda: CrmTimeout("no answer in time"),
                    tag=f"mirror:{lead_id}",
                )
            except AdapterBusy:
                return  # nothing was asked and nothing is counted
            except Exception as e:  # noqa: BLE001 - one try used; the part stops for this pass
                error = type(e).__name__

                tries_now: list[
                    int
                ] = []  # the crm_tries the UPDATE wrote (empty: the stage moved, nothing was charged)

                def failed(lead_id: str = lead_id, error: str = error, rank_sent: int = rank_sent) -> int:
                    tries_now.clear()
                    with self._write(self._conn()):
                        n = (
                            self._conn()
                            .execute(  # only for the stage that was sent: a move meanwhile started the copy afresh
                                "UPDATE lead_state SET crm_tries = crm_tries + 1, crm_error = ? WHERE lead_id = ?"
                                " AND step_rank = ?",
                                (error, lead_id, rank_sent),
                            )
                            .rowcount
                        )
                        if n:
                            row = self._conn().execute("SELECT crm_tries FROM lead_state WHERE lead_id=?", (lead_id,))
                            tries_now.append(int(row.fetchone()[0]))
                        return n

                self._finish_update(failed, "mirror_failed")
                # The wait is for the stage that was sent: a stage moved meanwhile is a new stage and the gate ignores
                # the entry. No charge written (moved, or the write stayed locked): the first wait still applies.
                n_tries = tries_now[0] if tries_now else 1
                if n_tries < MIRROR_MAX_TRIES:  # given up = no entry
                    wait = MIRROR_RETRY_WAIT_SECONDS[min(n_tries - 1, len(MIRROR_RETRY_WAIT_SECONDS) - 1)]
                    self._mirror_not_before.set(lead_id, time.monotonic() + wait, rank_sent)
                else:
                    self._mirror_not_before.clear(lead_id)
                return

            def copied(lead_id: str = lead_id, owner_sent: str = owner_sent, rank_sent: int = rank_sent) -> int:
                with self._write(self._conn()):
                    return (
                        self._conn()
                        .execute(
                            "UPDATE lead_state SET crm_owner_done = CASE WHEN ? <> '' THEN 1 ELSE crm_owner_done END,"
                            " crm_step_done = MAX(crm_step_done, ?), crm_tries = 0, crm_error = NULL WHERE lead_id = ?",
                            (owner_sent, rank_sent, lead_id),
                        )
                        .rowcount
                    )

            self._finish_update(copied, "mirror")
            self._mirror_not_before.clear(lead_id)
            done["mirrored"] += 1

    def _nudge_pass(self, done: dict[str, int]) -> None:
        """Send the one reminder of a lead nobody has replied to (own clock, own error guard). First the restamp
        (clock set back) and the stuck sweep, then at most NUDGES_PER_PASS claims inside NUDGE_PASS_SECONDS. The part
        stops after a give-back (the given-back row is still the oldest eligible one) and makes no claim while the
        give-back pause runs."""
        flow = self.config.leadflow
        if flow is None or flow.nudge_after_seconds is None:
            return
        conn = self._conn()
        now = self._now()
        self._housekeep_nudges(conn, now)
        started = time.monotonic()
        for _ in range(NUDGES_PER_PASS):
            if self._stopping() or time.monotonic() - started >= NUDGE_PASS_SECONDS:
                return
            if time.monotonic() < self._nudge_pause_until:
                return
            with self._worker_lock:
                if self._workers["send"] >= self._worker_cap("send"):
                    return
            try:
                claim = self._locked_retry(lambda: self._claim_nudge(conn, self._now()))
            except sqlite3.OperationalError as e:
                if db.is_busy(e):
                    log.warning("reminder: the claim stayed locked; nothing claimed")
                    return
                raise
            if claim is None:
                return
            state = self._send_nudge(*claim)
            if state == "pending":
                return
            if state == "sent":
                done["reminders"] += 1
            elif state == "unknown":
                done["reminders_unknown"] += 1

    def _housekeep_nudges(self, conn: sqlite3.Connection, now: int) -> None:
        """Restamp after a clock set back, then the stuck sweep. A lock that stays is logged by class name only."""

        def restamp() -> None:
            with self._write(conn):
                conn.execute(
                    "UPDATE nudges SET updated_at=? WHERE state IN ('pending','sending') AND updated_at > ?",
                    (now, now),
                )

        try:
            if conn.execute(
                "SELECT 1 FROM nudges WHERE state IN ('pending','sending') AND updated_at > ?", (now,)
            ).fetchone():
                self._locked_retry(restamp)
            self._locked_retry(lambda: self._sweep_nudges(conn, now, now - self.stuck_grace_seconds))
        except sqlite3.OperationalError as e:
            if not db.is_busy(e):
                raise
            log.warning("reminder: housekeeping skipped, database locked: %s", type(e).__name__)

    def _sweep_nudges(self, conn: sqlite3.Connection, now: int, stuck_before: int | None) -> None:
        """A reminder still `sending` (no live sender: start-up; older than the grace: the reaper) may have left: it
        becomes `unknown` and is never sent again."""
        with self._write(conn):
            conn.execute(
                "UPDATE nudges SET state='unknown', last_error='crash_while_sending', updated_at=?"
                " WHERE state='sending' AND (? IS NULL OR updated_at < ?)",
                (now, stuck_before, stuck_before),
            )

    def _claim_nudge(self, conn: sqlite3.Connection, now: int) -> tuple[str, int, str, str] | None:
        """One claim transaction: the oldest eligible lead (a step before reminder_until, older than the wait, no
        reminder row or a given-back one) gets its row `sending`. Returns (lead id, token, owner name, step id) or
        None. These are the only database reads of a reminder before its send."""
        flow = self.config.leadflow
        assert flow is not None and flow.nudge_after_seconds is not None  # noqa: S101 - _nudge_pass checked it
        params = {"until_rank": flow.rank(flow.reminder_until), "after": flow.nudge_after_seconds, "now": now}
        with self._write(conn):
            row = conn.execute(
                "SELECT s.lead_id, n.state, s.owner_name, s.step FROM lead_state s"
                " JOIN leads l ON l.lead_id = s.lead_id LEFT JOIN nudges n ON n.lead_id = s.lead_id"
                " WHERE s.step_rank < :until_rank AND l.created_at + :after <= :now"
                " AND (n.lead_id IS NULL OR n.state = 'pending')"
                " ORDER BY l.created_at, s.lead_id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            lead_id, state, owner_name, step_id = str(row[0]), row[1], str(row[2]), str(row[3])
            if state is None:
                conn.execute(
                    "INSERT INTO nudges (lead_id, state, attempts, updated_at) VALUES (?, 'sending', 1, ?)",
                    (lead_id, now),
                )
            else:
                conn.execute(
                    "UPDATE nudges SET state='sending', attempts=attempts+1, last_error=NULL, updated_at=?"
                    " WHERE lead_id=? AND state='pending'",
                    (now, lead_id),
                )
            token = conn.execute("SELECT attempts FROM nudges WHERE lead_id=?", (lead_id,)).fetchone()[0]
        return lead_id, int(token), owner_name, step_id

    def _send_nudge(self, lead_id: str, token: int, owner_name: str, step_id: str) -> str:
        """Send the reminder (outside any transaction) and record the result by the table of design 4.4. Returns the
        state reached; `pending` means given back (the mail service was provably not asked)."""
        flow = self.config.leadflow
        assert flow is not None  # noqa: S101 - _nudge_pass checked it
        state, err = "sent", None
        try:
            label = next((s[1] for s in flow.steps if s[0] == step_id), step_id)
            key = NUDGE_KEY_PREFIX + lead_id
            subject = f"Lead reminder {lead_id[:8]}"
            text = (
                f"Reference code: {lead_id[:8]}\nLead owner: {owner_name}\nLead stage: {label}\n"
                f"No person has replied for {flow.nudge_after_seconds} seconds.\n"
            )
            to, outbox, deadline = self.config.nudge_to, self.outbox, self._call_deadline()
        except Exception as e:  # before the send: nothing left
            state, err = "failed", f"internal:{type(e).__name__}"
        else:
            try:
                self._bounded(
                    "send",
                    lambda: outbox.send(key, to, subject, text, deadline),
                    deadline,
                    lambda: SendAmbiguous("no answer in time"),
                    tag=key,
                    exclusive=outbox.capability == "blind",
                )
            except AdapterBusy:  # no slot (or no thread): the send was never made
                self._note_busy("send")
                self._give_back_nudge(lead_id, token, SEND_BUSY_ERROR)
                return "pending"
            except SendNoDescriptors:  # no file handle for the connection: nothing left
                self._give_back_nudge(lead_id, token, NO_DESCRIPTORS_ERROR)
                return "pending"
            except OutboxModeMismatch:
                state, err = "unknown", "mode_mismatch"
                self._refresh_outbox()
            except SendRefused:
                state, err = "failed", "refused"
            except SendUnreachable:
                state, err = "failed", "unreachable"
            except Exception as e:  # SendAmbiguous, a timeout or anything else: the email may have left
                log.warning("reminder send ended unclear: %s", type(e).__name__)
                state, err = "unknown", "ambiguous"
        self.hooks.crash_point("N1", None)  # the send returned; the result is not recorded yet
        conn, now = self._conn(), self._now()

        def record() -> int:
            with self._write(conn):
                return conn.execute(
                    "UPDATE nudges SET state=?, last_error=?, updated_at=?, sent_at=?"
                    " WHERE lead_id=? AND state='sending' AND attempts=?",
                    (state, err, now, now if state == "sent" else None, lead_id, token),
                ).rowcount

        self._finish_update(record, "record_nudge_result")
        return state

    def _give_back_nudge(self, lead_id: str, token: int, error: str) -> None:
        """Nothing was sent: the row goes back to `pending` with its age kept, and the reminder part pauses."""
        conn = self._conn()
        self._nudge_pause_until = time.monotonic() + NO_DESCRIPTORS_BACKOFF_SECONDS

        def give_back() -> int:
            with self._write(conn):
                return conn.execute(
                    "UPDATE nudges SET state='pending', last_error=?"
                    " WHERE lead_id=? AND state='sending' AND attempts=?",
                    (error, lead_id, token),
                ).rowcount

        self._finish_update(give_back, "give_back_nudge")

    def _resolve_nudge(self, item_id: str, decision: str) -> Reply:
        """Mark a failed or unknown reminder as seen (only the word `seen` fits). Nothing is ever sent."""
        if decision != "seen":
            return Reply(HTTP_BAD_REQUEST, {"error": "bad_decision"})
        lead_id, conn = item_id[1:], self._conn()

        def attempt() -> int:
            with self._write(conn):
                return conn.execute(
                    "UPDATE nudges SET handled=1 WHERE lead_id=? AND state IN ('failed','unknown')", (lead_id,)
                ).rowcount

        if self._locked_retry(attempt) != 1:
            return Reply(
                HTTP_CONFLICT,
                {"error": "not_to_check", "hint": "Only a reminder that is failed or unknown can be marked seen."},
            )
        state = conn.execute("SELECT state FROM nudges WHERE lead_id=?", (lead_id,)).fetchone()[0]
        return Reply(HTTP_OK, {"item_id": item_id, "state": state, "handled": True})

    def _restamp_future(self, conn: sqlite3.Connection, now: int) -> None:
        """After the clock was set back, a follow-up row stamped with a later time would look "fresh" until the clock
        catches up, and recovery would wait that long. Such a stamp is set to now (it then ages normally)."""

        def attempt() -> None:
            with self._write(conn):
                conn.execute(
                    "UPDATE followups SET updated_at=? WHERE state IN ('pending','sending') AND updated_at > ?",
                    (now, now),
                )

        if conn.execute(
            "SELECT 1 FROM followups WHERE state IN ('pending','sending') AND updated_at > ? LIMIT 1", (now,)
        ).fetchone():
            try:
                self._locked_retry(attempt)
            except sqlite3.OperationalError as e:
                if not db.is_busy(e):
                    raise

    def _stalled_by_own_clock(self, conn: sqlite3.Connection, now: int) -> list[int]:
        """Open receipts stamped in the future (clock set back; the stamp cannot be changed) count their age from the
        first pass of the reaper that saw them open."""
        open_rows = conn.execute("SELECT receipt_id, received_at FROM receipts WHERE outcome IS NULL").fetchall()
        live = {int(r[0]) for r in open_rows}
        for rid in [r for r in self._seen_open if r not in live]:
            del self._seen_open[rid]
        out = []
        for rid, at in open_rows:
            first = self._seen_open.setdefault(int(rid), now)
            if at > now and first <= now - self.config.stall_seconds:
                out.append(int(rid))
        return out

    def _pending_to_unknown(self, lead_id: str) -> None:
        conn = self._conn()

        def attempt() -> None:
            with self._write(conn):
                conn.execute(
                    "UPDATE followups SET state='unknown', last_error='ambiguous_prior', updated_at=?"
                    " WHERE lead_id=? AND state='pending'",
                    (self._now(), lead_id),
                )

        try:
            self._locked_retry(attempt)
        except sqlite3.OperationalError as e:
            if not db.is_busy(e):
                raise

    # -- receipts and decisions
    def _receipt(self, conn: sqlite3.Connection, receipt_id: int) -> dict[str, Any] | None:
        row = conn.execute(_RECEIPT_BY_ID, (receipt_id,)).fetchone()
        return None if row is None else dict(zip(_RECEIPT_KEYS, row, strict=True))

    def _lookup_email_key(self, conn: sqlite3.Connection, email_key: str) -> str | None:
        """The lead id that holds this email key, or None. Always called as self._lookup_email_key(conn, key)."""
        row = conn.execute("SELECT lead_id FROM leads WHERE email_key=?", (email_key,)).fetchone()
        return None if row is None else str(row[0])

    def _decide(self, receipt_id: int) -> tuple[dict[str, Any], bool]:
        """Transaction 2, retried while the database stays locked. Returns (receipt as stored afterwards, True when
        THIS call wrote the verdict). Safety net: a payload that passed the checks but still cannot be stored (the
        database refuses it with an integrity, value or overflow error that is not a lock) gets the verdict
        `review`, reason `internal_unstorable` (`internal_unstorable:integrity` for a broken constraint), so no receipt
        stays open for ever. A database fault that is not a lock (disk I/O error, disk full, a file held by another
        program) is transient: it never counts towards the limit and never makes a verdict; the receipt stays open,
        is counted (decide_transient_errors) and the reaper tries it again at every pass."""
        try:
            result = self._locked_retry(lambda: self._decide_once(receipt_id))
        except _UNSTORABLE as e:
            reason = "internal_unstorable"
            if isinstance(e, sqlite3.IntegrityError):
                reason += ":integrity"
                # A payload that passed every check but broke a constraint is most likely a bug in this code (a
                # schema or check mismatch): the receipt stays visible as review/internal_unstorable, but it is logged
                # loudly and counted on /counts (decide_integrity_errors) so it is not mistaken for a bad payload.
                with self._decide_lock:
                    self._integrity_errors += 1
                log.error(
                    "receipt %d: IntegrityError while deciding (constraint bug?): %s", receipt_id, type(e).__name__
                )
            else:
                log.warning("payload could not be stored: %s", type(e).__name__)
            result = self._locked_retry(lambda: self._decide_unstorable(receipt_id, reason))
        except Exception as e:
            if db.is_busy(e):
                raise  # a lock is not the receipt's fault: it keeps being tried
            if isinstance(e, sqlite3.OperationalError):
                # a disk fault is not the receipt's fault either, and a verdict is write-once: keep it open and retry
                with self._decide_lock:
                    self._transient_errors += 1
                    self._poked.add(receipt_id)
                log.warning(
                    "receipt %d: database fault while deciding, receipt stays open: %s", receipt_id, type(e).__name__
                )
                raise
            with self._decide_lock:
                failures = self._decide_failures.get(receipt_id, 0) + 1
                self._decide_failures[receipt_id] = failures
            if failures < DECIDE_FAIL_LIMIT:
                raise
            # the same plain error again and again: a person must look, and the reaper must stop trying
            log.warning(
                "receipt %d: decision failed %d times (%s): verdict review", receipt_id, failures, type(e).__name__
            )
            result = self._locked_retry(lambda: self._decide_unstorable(receipt_id, "internal_error"))
        with self._decide_lock:
            self._decide_failures.pop(receipt_id, None)
            self._poked.discard(receipt_id)
        return result

    def _decide_unstorable(self, receipt_id: int, reason: str = "internal_unstorable") -> tuple[dict[str, Any], bool]:
        conn = self._conn()
        with self._write(conn):
            rec = self._receipt(conn, receipt_id)
            if rec is None:
                raise LookupError("receipt vanished")
            if rec["outcome"] is not None:
                return rec, False
            conn.execute(
                "UPDATE receipts SET outcome='review', outcome_reason=?, decided_at=? WHERE receipt_id=?",
                (reason, self._now(), receipt_id),
            )
        stored = self._receipt(conn, receipt_id)
        if stored is None:
            raise LookupError("receipt vanished")
        return stored, True

    def _decide_once(self, receipt_id: int) -> tuple[dict[str, Any], bool]:
        conn = self._conn()
        with self._write(conn):
            rec = self._receipt(conn, receipt_id)
            if rec is None:
                raise LookupError("receipt vanished")
            if rec["outcome"] is not None:  # another request or a restart finished it: nothing to write
                return rec, False
            now = self._now()
            chk = identity.check_payload(json.loads(rec["payload"]))
            if chk.kind == "review":
                conn.execute(
                    "UPDATE receipts SET outcome='review', outcome_reason=?, decided_at=? WHERE receipt_id=?",
                    (chk.reason, now, receipt_id),
                )
            else:
                if chk.email_key is None or chk.email is None or chk.name is None:
                    raise _IncompleteCheck("checked payload is incomplete")
                existing = self._lookup_email_key(conn, chk.email_key)
                if existing is not None:
                    row = conn.execute(
                        "SELECT name, phone, company, message FROM leads WHERE lead_id=?", (existing,)
                    ).fetchone()
                    stored = dict(zip(("name", "phone", "company", "message"), row, strict=True))
                    diff = identity.diff_fields(
                        stored, {"name": chk.name, "phone": chk.phone, "company": chk.company, "message": chk.message}
                    )
                    conn.execute(
                        "UPDATE receipts SET outcome='duplicate', outcome_lead_id=?, differing_fields=?, decided_at=?"
                        " WHERE receipt_id=?",
                        (existing, json.dumps(diff), now, receipt_id),
                    )
                else:
                    lead_id = LEAD_ID_PREFIX + secrets.token_hex(LEAD_ID_HEX_CHARS // 2)
                    conn.execute(
                        "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, phone, company,"
                        " message, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (lead_id, receipt_id, rec["source"], chk.name, chk.email, chk.email_key, chk.phone,
                         chk.company, chk.message, now),
                    )  # fmt: skip
                    conn.execute(
                        "INSERT INTO followups(lead_id, state, ai_label, attempts, last_error, updated_at, sent_at)"
                        " VALUES(?, 'pending', NULL, 0, NULL, ?, NULL)",
                        (lead_id, now),
                    )
                    flow = self.config.leadflow
                    if (
                        flow is not None
                    ):  # lead owner and first step, in this transaction (nothing when the feature is off)
                        (n,) = conn.execute("SELECT count(*) FROM lead_state").fetchone()
                        owner_id, owner_name, _ = flow.rota[n % len(flow.rota)]
                        try:
                            conn.execute(
                                "INSERT INTO lead_state(lead_id, owner_id, owner_name, assigned_at, step, step_rank,"
                                " changed_at) VALUES(?,?,?,?,?,?,?)",
                                (lead_id, owner_id, owner_name, now, flow.first, flow.rank(flow.first), now),
                            )
                        except (sqlite3.IntegrityError, sqlite3.DataError) as e:  # only this insert is wrapped
                            raise RuntimeError("lead_state insert failed") from e
                    conn.execute(
                        "UPDATE receipts SET outcome='new', outcome_lead_id=?, decided_at=? WHERE receipt_id=?",
                        (lead_id, now, receipt_id),
                    )
            self.hooks.crash_point("A2", conn)
        stored_rec = self._receipt(conn, receipt_id)
        if stored_rec is None:
            raise LookupError("receipt vanished")
        return stored_rec, True

    def _verdict_reply(self, rec: dict[str, Any], replayed: bool, payload_matches: bool | None) -> Reply:
        body: dict[str, Any] = {"receipt_id": rec["receipt_id"]}
        outcome = rec["outcome"]
        drive: str | None = None
        if outcome == "new":
            status = HTTP_CREATED
            body.update(verdict="new", lead_id=rec["outcome_lead_id"])
            if not replayed:
                drive = rec["outcome_lead_id"]
            elif self._pending_and_due(rec["outcome_lead_id"]):
                drive = rec["outcome_lead_id"]  # a replay of `new` drives a pending follow-up, but only once it is due
        elif outcome == "duplicate":
            status = HTTP_OK
            body.update(
                verdict="duplicate",
                lead_id=rec["outcome_lead_id"],
                differing_fields=json.loads(rec["differing_fields"]),
            )
        else:
            status = HTTP_ACCEPTED
            body.update(verdict="review", reason=rec["outcome_reason"])
        body["replayed"] = replayed
        if replayed:
            body["payload_matches"] = payload_matches
        return Reply(status, body, drive)

    @staticmethod
    def _envelope_ok(source: object, source_ref: object) -> bool:
        """The envelope rules the desk itself keeps (server.py checks the same before it calls process): a known
        source, a source_ref of 1 to 128 characters with no control character (NUL included) and no lone surrogate."""
        return (
            source in SOURCES
            and isinstance(source_ref, str)
            and SOURCE_REF_MIN <= len(source_ref) <= SOURCE_REF_MAX
            and not identity.has_control_char(source_ref)
            and identity.encodable(source_ref)
        )

    def process(self, source: str, source_ref: str, payload: object) -> Reply:
        """Receipt and decision (envelope already checked); decision only; never raises (503/500 are Replies).

        A bad envelope (source_ref with a control character or outside 1..128 characters, unknown source) is
        answered 400 bad_envelope with receipt_id null and no receipt. At most INFLIGHT_MAX calls run at once; one
        that finds no place within INFLIGHT_WAIT_SECONDS is answered 503 busy with receipt_id null BEFORE any receipt
        is written, so the client can safely send it again.

        Crash-point placement (crash_point(name, conn); each hook is one-shot: it fires once, then clears):
        "A"  right after transaction 1 commits (conn = that connection, no transaction open). It fires ONLY when
             THIS call created the receipt row; a replay that finds an existing receipt (open or with a verdict)
             never fires "A".
        "A2" in transaction 2 after all writes and before COMMIT (conn has the open transaction).
        "B"  at the end of process, after transaction 2 commits and before the Reply is returned (conn = that
             connection). It fires ONLY when THIS call committed transaction 2; a replay that finds a stored
             verdict never fires "B".
        process never claims a follow-up (it makes no `_claim` call); it only creates the follow-up in `pending`
        inside transaction 2."""
        self._used = True
        if not self._envelope_ok(source, source_ref):
            self.note_rejected_envelope()
            return Reply(HTTP_BAD_REQUEST, {"error": "bad_envelope", "receipt_id": None})
        self._local.req_deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS  # ONE deadline for this whole request
        try:
            if not self._inflight.acquire(timeout=INFLIGHT_WAIT_SECONDS):
                return Reply(HTTP_BUSY, {"error": "busy", "receipt_id": None})
            try:
                return self._process(source, source_ref, payload)
            finally:
                self._inflight.release()
        finally:
            self._local.req_deadline = None

    def _process(self, source: str, source_ref: str, payload: object) -> Reply:
        rid: int | None = None
        try:
            conn = self._conn()
            try:
                canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            except (RecursionError, TypeError, ValueError, OverflowError):
                # nested so deeply, or holding something JSON cannot write (a set, a date, mixed-type keys, a 4301-digit
                # number): not a request the desk can store, the same answer as the server's
                self.note_rejected_envelope()
                return Reply(HTTP_BAD_REQUEST, {"error": "bad_envelope", "receipt_id": None})

            def receive() -> tuple[int, bool]:
                with self._write(conn):  # transaction 1: the receipt, before anything else happens
                    conn.execute(
                        "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES(?,?,?,?)"
                        " ON CONFLICT(source, source_ref) DO UPDATE SET attempts = attempts + 1",
                        (source, source_ref, self._now(), canon),
                    )
                    found = conn.execute(
                        "SELECT receipt_id, attempts FROM receipts WHERE source=? AND source_ref=?",
                        (source, source_ref),
                    ).fetchone()
                    return int(found[0]), found[1] == 1  # (receipt id, this call created the row)

            rid, created = self._locked_retry(receive)
            rec = self._receipt(conn, rid)
            if rec is None:
                raise LookupError("receipt vanished")
            matches = rec["payload"] == canon
            if created:
                self.hooks.crash_point("A", conn)
            elif not matches:
                self._note_payload_conflict(rid)
            if rec["outcome"] is not None:
                return self._verdict_reply(rec, True, matches)
            rec, decided_here = self._decide(rid)
            if not decided_here:
                return self._verdict_reply(rec, True, matches)
            self.hooks.crash_point("B", conn)
            return self._verdict_reply(rec, False, None)
        except Exception as e:
            if rid is not None and db.is_busy(e):
                with (
                    self._decide_lock
                ):  # the 503 is final for this client, but the receipt must not wait for stall_seconds
                    self._poked.add(rid)
            return self._error_reply(e, rid)

    def _note_payload_conflict(self, receipt_id: int) -> None:
        """A request number came again with a different payload: the first answer stands, but the difference must not
        vanish. Marks the receipt so the review list shows an informational D item that names it."""
        conn = self._conn()

        def attempt() -> None:
            with self._write(conn):
                conn.execute("UPDATE receipts SET payload_conflict=1 WHERE receipt_id=?", (receipt_id,))

        try:
            self._locked_retry(attempt)
        except sqlite3.OperationalError as e:
            if not db.is_busy(e):
                raise
            log.warning("payload conflict not recorded: database stayed locked")

    # -- follow-up
    def _followup_state(self, lead_id: str) -> str | None:
        row = self._conn().execute("SELECT state FROM followups WHERE lead_id=?", (lead_id,)).fetchone()
        return None if row is None else str(row[0])

    def _pending_and_due(self, lead_id: str) -> bool:
        """True for a `pending` follow-up that may be driven now: one with no automatic try yet (its first drive), or
        one whose backoff after the last try is over. A client that sends the same request again and again therefore
        cannot burn the try cap: after the first drive each further replay waits for the backoff."""
        row = (
            self._conn()
            .execute("SELECT state, auto_attempts, updated_at, last_error FROM followups WHERE lead_id=?", (lead_id,))
            .fetchone()
        )
        if row is None or row[0] != "pending":
            return False
        if row[3] in (NO_DESCRIPTORS_ERROR, *BUSY_ERRORS):  # given back (no file handles, too many calls): back off
            return bool(row[2] + wait_before_drive(int(row[1]), row[3]) <= self._now())
        return bool(row[1] == 0 or row[2] + wait_before_drive(int(row[1]), row[3]) <= self._now())

    # -- bounded adapter calls
    def _call_deadline(self) -> float:
        """The total deadline of a CRM put, a send or a capability read: the config's send_timeout_seconds, at most
        CALL_DEADLINE_MAX_SECONDS (read when the call starts). The adapter is still told the config value as
        its own per-operation timeout; this one is the total."""
        return min(float(self.config.send_timeout_seconds), CALL_DEADLINE_MAX_SECONDS)

    @staticmethod
    def _worker_cap(kind: str) -> int:
        """The cap of one kind, read from the module at call time."""
        return {
            "model": MODEL_HUNG_MAX,
            "crm": CRM_WORKERS_MAX,
            "send": SEND_WORKERS_MAX,
            "refresh": REFRESH_WORKERS_MAX,
        }[kind]

    def hung_workers(self) -> dict[str, int]:
        """How many calls of each kind the desk gave up on and that are still running (shown on /counts and in the
        portal summary). A kind that stays at its cap has no free slot: the rows that need it wait."""
        with self._worker_lock:
            return dict(self._abandoned)

    def _busy(self, kind: str) -> AdapterBusy:
        return ModelBusy() if kind == "model" else AdapterBusy(kind)

    def _note_busy(self, kind: str) -> None:
        """One warning per give-back burst of a kind (not one per row per 5 s); a free slot starts a new burst."""
        with self._worker_lock:
            first = kind not in self._busy_logged
            self._busy_logged.add(kind)
        if first:
            log.warning("%s calls are at their cap: the follow-ups that need one wait, nothing is asked", kind)

    def _bounded(
        self,
        kind: str,
        fn: Callable[[], _T],
        budget: float,
        on_timeout: Callable[[], Exception],
        *,
        tag: str | None = None,
        exclusive: bool = False,
    ) -> _T:
        """The ONE place the desk calls an adapter that can block (the AI step, the CRM put, the outbox send, the
        capability reads). `fn` runs in a daemon worker thread and the desk waits for it at most `budget` seconds of
        real time in total. On a timeout the worker is abandoned (it only holds the answer in memory, never the
        database: a late answer is thrown away) and `on_timeout()` is raised; what that means for the row is decided
        by the caller (an AI timeout is a used-up try, an ambiguous send is retried with the same key or ends
        `unknown`, see _send_claimed and _crm_step). An exception of `fn` is raised again here, on the calling thread.

        The workers of one kind that exist at once (running or abandoned) are counted and capped: a slot is reserved
        under the lock BEFORE the worker starts and given back when the worker returns (or when it could not be
        started), so the bound is exactly the cap. At the cap, or when no thread can be started, AdapterBusy is
        raised and `fn` is never called. `tag` (a lead id) and `exclusive` make that stricter for one lead: while a
        call for the same lead is still running, a new one is refused (a blind sender or CRM must never be asked
        twice, even when the first call was abandoned and has not ended yet)."""
        cap = self._worker_cap(kind)
        key = (kind, tag) if tag is not None else None
        with self._worker_lock:
            if self._workers[kind] >= cap or (exclusive and key is not None and self._tagged.get(key, 0) > 0):
                raise self._busy(kind)
            self._workers[kind] += 1  # the slot is taken before the worker exists
            if key is not None:
                self._tagged[key] = self._tagged.get(key, 0) + 1
            self._busy_logged.discard(kind)
        call = _Call()

        def work() -> None:
            answer: Any = None
            error: BaseException | None = None
            try:
                answer = fn()
            except BaseException as e:  # raised again on the calling thread
                error = e
            with self._worker_lock:
                self._workers[kind] -= 1
                if key is not None:
                    left = self._tagged.get(key, 1) - 1
                    if left > 0:
                        self._tagged[key] = left
                    else:
                        self._tagged.pop(key, None)
                if call.abandoned:  # nobody waits any more: drop the answer, free the counts
                    self._abandoned[kind] -= 1
                    self._closed_logged.discard(kind)
                    return
                call.answer, call.error, call.finished = answer, error, True
            call.done.set()

        try:
            threading.Thread(target=work, name=f"lead-desk-{kind}", daemon=True).start()
        except RuntimeError as e:  # no thread could be started: nothing was asked, the slot is given back
            with self._worker_lock:
                self._workers[kind] -= 1
                if key is not None:
                    left = self._tagged.get(key, 1) - 1
                    if left > 0:
                        self._tagged[key] = left
                    else:
                        self._tagged.pop(key, None)
            raise self._busy(kind) from e
        call.done.wait(budget)
        closed = False
        with self._worker_lock:
            if not call.finished:
                call.abandoned = True
                self._abandoned[kind] += 1
                if self._abandoned[kind] >= cap and kind not in self._closed_logged:
                    self._closed_logged.add(kind)
                    closed = True
        if not call.finished:
            if closed:
                log.error(
                    "every %s slot is held by a call that never came back (%d): no follow-up that needs one can go on "
                    "until a call ends or the desk is restarted",
                    kind,
                    cap,
                )
            raise on_timeout()
        if call.error is not None:
            raise call.error
        return cast(_T, call.answer)

    def _ask_model(self, lead_id: str, email_key: str) -> Any:
        """The AI step, through the one bounded-call helper: at most MODEL_CALL_BUDGET_SECONDS of real time (read from
        the module at call time), then ModelTimeout (one used-up automatic try); at the model cap ModelBusy (nothing
        was asked, the try is given back)."""
        budget = MODEL_CALL_BUDGET_SECONDS
        return self._bounded(
            "model",
            lambda: self.qualifier.qualify(lead_id, email_key),
            budget,
            lambda: ModelTimeout(f"no answer within {budget} s"),
        )

    def _closed_kind(self, lead_id: str) -> str | None:
        """The kind of call a `pending` row needs next when `_bounded` would turn it away: every slot of that kind is
        taken (by abandoned or by healthy slow calls), or a blind sender or CRM is still running a call for this very
        lead. A quick look before the claim, so such a row is not claimed, stamped and given back every few seconds:
        it stays as it is and ages into the stalled list of the review desk.
        None when the row can go on. A look, not a promise: the slot is reserved by _bounded, and a row that gets
        there at a bad moment is given back by AdapterBusy without moving its age."""
        row = self._conn().execute("SELECT ai_label, crm_done FROM followups WHERE lead_id=?", (lead_id,)).fetchone()
        if row is None:
            return None
        label, crm_done = row
        need = "send" if crm_done == CRM_DONE else ("model" if label is None else "crm")
        if need == "send":
            exclusive = self.outbox.capability == "blind"
        elif need == "crm":
            exclusive = getattr(self.crm, "capability", None) != "keyed"
        else:
            exclusive = False
        with self._worker_lock:
            # the same refusal rule as _bounded, so a row that _bounded would turn away is not claimed (a claim stamps
            # the row: it would look fresh for ever and never age into the review desk's stalled list)
            if self._workers[need] >= self._worker_cap(need):
                return need
            if exclusive and self._tagged.get((need, lead_id), 0) > 0:
                return need
            return None

    def _label_step(self, lead_id: str, token: int) -> str | None:
        """Follow-up step 1: the AI label, asked for once; the label stored first wins. No CRM call here. Returns None
        when the row is no longer ours once the model has answered (the label is then not stored). The row is stamped
        again right after the model returns and before the label is written: the reaper counts the stuck time from
        that stamp, so a slow model answer plus a slow label write cannot add up past the grace
        (MODEL_GRACE_MARGIN_SECONDS covers the stamp's own write window, validate_model_budget checks it)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT l.email_key, f.ai_label FROM leads l JOIN followups f USING (lead_id) WHERE l.lead_id=?",
            (lead_id,),
        ).fetchone()
        if row is None:
            raise LookupError("no such lead")
        email_key, label = row
        if label is None:
            asked_at = self._now()
            label = self._ask_model(lead_id, email_key)
            if self._now() - asked_at > MODEL_CALL_BUDGET_SECONDS:  # the desk clock jumped, or the clock was replaced
                log.warning("the AI step took longer than its budget: the reaper may have taken the row meanwhile")
            if label not in AI_LABELS:  # None or anything else would reach the CRM as text and leave the label empty
                raise ValueError("the AI step answered no valid label")

            def store() -> None:
                with self._write(conn):
                    conn.execute(  # also stamps the row: the next stage (the CRM mark) counts its grace from here
                        "UPDATE followups SET ai_label=?, updated_at=? WHERE lead_id=? AND ai_label IS NULL"
                        " AND state='sending' AND attempts=?",
                        (label, self._now(), lead_id, token),
                    )

            try:
                if not self._touch(lead_id, token):  # False = not ours (a lock raises, handled below)
                    return None
                self._locked_retry(store)
                stored = conn.execute(
                    "SELECT ai_label, state='sending' AND attempts=? FROM followups WHERE lead_id=?", (token, lead_id)
                ).fetchone()
            except sqlite3.OperationalError as e:
                if db.is_busy(e):
                    # the model WAS asked and its answer cannot be recorded: the answer is thrown away and the try
                    # stays used up (not given back), so the 5-try cap bounds how often the model is asked again
                    raise ModelAnswerLost("the database stayed locked while the AI answer was recorded") from e
                raise
            if not stored[1]:  # the row was put back or claimed again meanwhile: the write above changed nothing
                return None
            label = stored[0]
            if label not in AI_LABELS:
                raise LookupError("label not stored")
            self.hooks.crash_point("L", None)
        return str(label)

    def _crm_step(self, lead_id: str, token: int) -> bool:
        """Follow-up step 2, run ONLY by the thread that won the claim: the CRM row, once per lead. Returns True when
        the follow-up may go on to the send, False when this call already recorded the result (or lost the claim).

        `crm_done` is an intent mark: 0 = not asked, 2 = a put was started (written BEFORE the put), 1 = the put
        came back. A leftover 2 means a put may have reached the CRM without the desk learning it. A CRM that says it
        is keyed (an attribute `capability == "keyed"`) is simply asked again, which is harmless. Any other CRM (no
        such attribute counts as not keyed: the desk cannot know) must not be asked again, so the follow-up goes to
        `unknown` / `crm_unconfirmed` and a person looks. A put that fails in a way that proves nothing was stored
        (the exception has `nothing_stored` true) resets the mark to 0; any other failure of a not-keyed CRM is just
        as unclear as a leftover 2 and also ends `unknown`."""
        conn = self._conn()
        row = conn.execute(
            "SELECT l.name, l.email, l.company, l.source, f.ai_label, f.crm_done"
            " FROM leads l JOIN followups f USING (lead_id) WHERE l.lead_id=?",
            (lead_id,),
        ).fetchone()
        if row is None:
            raise LookupError("no such lead")
        name, email, company, source, label, crm_done = row
        if crm_done == CRM_DONE:
            return True
        if crm_done == CRM_STARTED:
            self._refresh_crm()  # a CRM restarted in another mode must not be judged by an old answer
            if getattr(self._local, "crm_refresh_no_fd", False):
                # no file handle to read the CRM's mode: nothing was asked, nothing can be judged. Not "not keyed" (that
                # would end `unknown` and send a person to look for nothing): the try is given back, the mark stays 2
                raise SendNoDescriptors("crm capability read")
            if getattr(self._local, "crm_refresh_failed", False):
                # the CRM's mode could not be read (it is down): the desk asked nothing in this try, so it must not
                # judge the leftover mark by a forgotten mode and send a person to look. The try is used up with a
                # backoff (bounded by SEND_MAX_ATTEMPTS, then `failed`, which a person's Retry starts again)
                raise CrmUnreachable("crm capability read")
        keyed = getattr(self.crm, "capability", None) == "keyed"
        if crm_done == CRM_STARTED and not keyed:
            self._record_send_result(lead_id, "unknown", token, "crm_unconfirmed")
            return False
        if label is None:
            label = self._label_step(lead_id, token)
            if label is None:
                log.warning("crm step: the follow-up is no longer ours after the AI step, nothing was stored")
                return False
        if not self._mark_crm(
            lead_id, CRM_STARTED, token
        ):  # also moves updated_at, so the reaper counts from this stage
            log.warning("crm step: the follow-up is no longer ours, nothing was asked")
            return False
        record = {
            "lead_id": lead_id,
            "name": name,
            "email": email,
            "company": company or "",
            "source": source,
            "ai_label": label,
        }
        self.hooks.crash_point("C0", None)  # the intent mark 2 is written; crm.put is not called yet
        try:
            # A put that does not answer in time is ambiguous (the row may be in the CRM): a keyed CRM is asked again
            # with the same key, any other CRM ends `unknown` below. A CRM that is not keyed is never asked while an
            # earlier put for this lead is still running (exclusive): the abandoned put may yet land.
            self._bounded(
                "crm",
                lambda: self.crm.put(lead_id, record),
                self._call_deadline(),
                lambda: CrmTimeout("no answer in time"),
                tag=lead_id,
                exclusive=not keyed,
            )
        except Exception as e:
            if getattr(e, "nothing_stored", False):
                self._mark_crm(
                    lead_id, CRM_NOT_ASKED, token, best_effort=True
                )  # a failed write leaves 2: the safe side
                raise
            if keyed:
                raise  # asking again is harmless; the mark stays 2
            log.warning("crm put ended unclear on a CRM that is not keyed: %s", type(e).__name__)
            self._record_send_result(lead_id, "unknown", token, "crm_unconfirmed")
            return False
        self.hooks.crash_point("C1", None)  # the put came back; crm_done=1 is not written yet
        try:
            return self._mark_crm(lead_id, CRM_DONE, token)  # retried; False = no longer ours
        except sqlite3.OperationalError as e:
            if not db.is_busy(e):
                raise
            # the put came back but the mark cannot be written: do NOT go on to the send. The row stays `sending`;
            # the reaper puts it back flagged ambiguous_prior and the leftover 2 is judged on the next drive.
            log.warning("crm_done=1 not recorded: database stayed locked; row left for the reaper")
            return False

    def _mark_crm(self, lead_id: str, value: int, token: int, best_effort: bool = False) -> bool:
        """Write crm_done=value for the row this thread claimed: state `sending` AND attempts equal to the claim's
        `token` (the attempts value the claim wrote), so a drive that stalled and was put back cannot write into a
        later drive's claim. A lock that stays held raises, except for the best-effort reset to 0 after a proven
        failed put (the leftover 2 is then judged on the next drive). Returns False when the row is no longer ours."""
        conn = self._conn()

        def attempt() -> int:
            with self._write(conn):
                return conn.execute(
                    "UPDATE followups SET crm_done=?, updated_at=? WHERE state='sending' AND lead_id=? AND attempts=?",
                    (value, self._now(), lead_id, token),
                ).rowcount

        try:
            return self._locked_retry(attempt) == 1
        except sqlite3.OperationalError as e:
            if best_effort and db.is_busy(e):
                log.warning("crm_done=%d not recorded: database stayed locked", value)
                return True
            raise

    def _touch(self, lead_id: str, token: int, send_started: bool = False) -> bool:
        """Move updated_at of the claimed row (state `sending`, attempts == token) to now, so the reaper's stuck-time
        counts from the last stage. With send_started the same UPDATE also writes the intent mark `send_started=1`
        (BEFORE outbox.send). Two different answers, never mixed up: False means the row is really not ours any more
        (put back, or claimed again: the token or the state changed; nothing was written). A database that stayed
        locked through the retries RAISES (the busy `sqlite3.OperationalError`): the row is still ours, the write just
        could not be made, so the caller must give its try back (`_abort_unsent`) and must not send without the mark."""
        conn = self._conn()

        def attempt() -> int:
            with self._write(conn):
                return conn.execute(
                    "UPDATE followups SET updated_at=?, send_started=CASE WHEN ? THEN 1 ELSE send_started END"
                    " WHERE state='sending' AND lead_id=? AND attempts=?",
                    (self._now(), int(send_started), lead_id, token),
                ).rowcount

        try:
            return self._locked_retry(attempt) == 1
        except sqlite3.OperationalError as e:
            if db.is_busy(e):
                log.warning("sending row not refreshed: database stayed locked")
            raise

    def run_followup(self, lead_id: str) -> None:
        """Follow-up steps 1-5 for one lead, as a fresh drive: the claim (step 3) as
        `self._claim(None, lead_id, ("pending",))` (an automatic driver never takes a `failed` row) and, only if the
        claim returned True, steps 1, 2, 4 and 5 (`_after_claim`: the label, the CRM row once, then `_send_claimed`).
        The AI step is asked only by the thread that won the claim, never before it. A lost claim (False) returns
        quietly. Catches Exception, logs the class name only; an exception in step 1 counts as one automatic try
        (`_abort_unsent`: back to waiting with a backoff, `failed` only at the cap), except a database that stayed
        locked: the try is given back and the reaper drives it later (not after the model has answered: that try stays
        used up, see `_label_step`). A follow-up that is not `pending` is left
        alone (nothing to drive). D1 and D2 live in `_send_claimed`."""
        self._used = True
        # With the reaper running, a drive that finds no place leaves its row to the reaper. Without a reaper nobody
        # else would drive it, so it waits for a place (every place is held by a drive that ends on its own timeouts).
        if not self._drives.acquire(timeout=DRIVE_WAIT_SECONDS if self._background else None):
            log.warning("follow-up not driven now: all drive places are taken; the reaper drives it later")
            return
        was_low = bool(getattr(self._local, "low", False))
        self._local.low = True  # a drive queues behind requests for the write gate, so it cannot starve them
        try:
            self._run_followup(lead_id)
        finally:
            self._local.low = was_low
            self._drives.release()

    def _run_followup(self, lead_id: str) -> None:
        try:
            state0 = self._conn().execute("SELECT state FROM followups WHERE lead_id=?", (lead_id,)).fetchone()
            if state0 is None or state0[0] != "pending":
                return
            # The AI step runs only after the claim (in `_after_claim`): while the row is still `pending` the reaper
            # would treat a slow drive as absent and start a second one, and the model would be asked twice.
            closed = self._closed_kind(lead_id)
            if closed is not None:  # no free slot for the call this row needs: leave it as it is (it keeps ageing)
                self._note_busy(closed)
                return
            if self._claim(None, lead_id, ("pending",)):
                self._after_claim(lead_id, auto=True)
        except Exception as e:
            log.warning("follow-up failed: %s", type(e).__name__)

    def _after_claim(self, lead_id: str, auto: bool) -> None:
        """Steps 1, 2, 4 and 5 for a follow-up this thread has just claimed (the claim is step 3 and always comes
        first, so the AI step is asked only by the claim's winner). An exception in step 1 or 2 (the AI step or the
        CRM was down, or answered something odd) means nothing was sent: `_abort_unsent` counts it as one automatic
        try (`internal:<class>`), the row goes back to `pending` with a backoff and `failed` only at the cap (after a
        person's Retry the count starts at 1, so the row is `pending`, not `failed`, and Retry stays possible). A
        database that stayed locked, on any write here (the first stage stamp included), is the desk's own trouble:
        the try is given back (`_give_back`), except a lock after the model answered (`ModelAnswerLost`, the model
        was asked, so the try stays used up). Too many running adapter calls (`AdapterBusy`, `ModelBusy` for the model)
        also give the try back. A stamp that finds the row no longer ours (token or state changed) ends the drive
        without writing anything."""
        token = self._take_token(lead_id)
        try:
            if not self._touch(lead_id, token):  # BEFORE the AI step, which can be slow: the reaper counts from here
                log.warning("follow-up skipped: it is no longer ours")
                return
            if self._label_step(lead_id, token) is None:
                log.warning("follow-up stopped: it is no longer ours after the AI step")
                return
            if not self._crm_step(lead_id, token):
                return
        except Exception as e:
            log.warning("follow-up steps 1-2 failed after the claim: %s", type(e).__name__)
            self._abort_unsent(lead_id, token, e)
            return
        self._send_claimed(lead_id, auto, token)

    def _abort_unsent(self, lead_id: str, token: int, exc: Exception) -> None:
        """Nothing was sent. A database that stayed locked is not the follow-up's fault: the try is given back and the
        row goes to `pending` if the write can be made now (else it stays `sending` and the reaper puts it back);
        nothing is recorded as an error. Too many running adapter calls (AdapterBusy) give the try back too (nothing was
        asked). Any other error is one used-up automatic try (`internal:<class>`), a lock after the model has answered
        (`ModelAnswerLost`) included: the model was asked. When that error cannot be written either (the database is
        still locked) the row stays `sending` with its try used up; the reaper puts it back after the grace as
        `crash_while_sending` with `ambiguous_prior` 0, which the review desk reads as "nothing was sent"."""
        if isinstance(exc, AdapterBusy):  # nothing was asked: the try is given back and the row waits a little
            self._note_busy(exc.kind)
            self._give_back(lead_id, token, f"{exc.kind}_busy")
            return
        if not (db.is_busy(exc) or is_fd_exhaustion(exc)):
            self._record_send_result(lead_id, self._pending_or_failed(lead_id), token, f"internal:{type(exc).__name__}")
            return
        self._give_back(lead_id, token, NO_DESCRIPTORS_ERROR if is_fd_exhaustion(exc) else None)

    def _give_back(self, lead_id: str, token: int, last_error: str | None) -> None:
        """Nothing was sent and the cause is the desk's own (a locked database, no file handles left, too many adapter
        calls still running), not the sender's: the automatic try is given back and the row goes to `pending`, to be
        tried again later. A give-back for too many running calls (`model_busy`, `crm_busy`, `send_busy`) does not move
        `updated_at` again: it stays at the time of the claim (or stage stamp) being given back, not the time of the
        give-back. Each new claim of the row stamps it afresh, so this keeps the row from looking newer than its last
        claim; it does not by itself age the row to the stalled list across several claims. If the write cannot be
        made now the row stays `sending` and the reaper puts it back."""
        conn = self._conn()
        keep_age = int(last_error in BUSY_ERRORS)

        def attempt() -> None:  # one write: the state, the try count and the send mark change together or not at all
            with self._write(conn):
                conn.execute(
                    "UPDATE followups SET state='pending', auto_attempts=MAX(auto_attempts-1, 0), send_started=0,"
                    " last_error=CASE WHEN ? IS NOT NULL THEN ? WHEN last_error IN (?, ?, ?, ?) THEN NULL"
                    " ELSE last_error END, updated_at=CASE WHEN ? THEN updated_at ELSE ? END"
                    " WHERE state='sending' AND lead_id=? AND attempts=?",
                    (
                        last_error,
                        last_error,
                        NO_DESCRIPTORS_ERROR,
                        *BUSY_ERRORS,
                        keep_age,
                        self._now(),
                        lead_id,
                        token,
                    ),
                )

        try:
            self._locked_retry(attempt)
        except sqlite3.OperationalError as e:
            if not db.is_busy(e):
                raise
            log.warning("claim not given back: database stayed locked; the reaper puts the row back")

    def handle(self, source: str, source_ref: str, payload: object) -> Reply:
        """process, then run_followup if reply.drive_lead_id is set (the simplest way to run one request to the end)."""
        reply = self.process(source, source_ref, payload)
        if reply.drive_lead_id is not None:
            self.run_followup(reply.drive_lead_id)
        return reply

    def _claim(
        self, conn_or_none: sqlite3.Connection | None, lead_id: str, from_states: tuple[str, ...] = ("pending",)
    ) -> bool:
        """Owns its transaction; the UPDATE of step 3; True when exactly one row changed. Never leaves a
        transaction open. A lock that stays held through LOCK_RETRIES tries gives False (the item stays pending and
        shows as stalled). `from_states` is ("pending",) for every automatic driver (run_followup, the reaper, the
        start-up sweep) and ("pending", "failed") only for a person's Retry: an automatic driver can never take a
        `failed` row, so it can never start the automatic-try count again. Claiming a `failed` row starts the count
        again at 1 (this try); claiming a `pending` row counts one more automatic try."""
        self._used = True
        sql = _CLAIM_SQL.get(tuple(from_states))
        if sql is None:
            raise ValueError("a claim takes ('pending',) or ('pending', 'failed')")
        conn = self._conn() if conn_or_none is None else conn_or_none

        def attempt() -> bool:
            self._local.token = None
            with self._write(conn):
                if conn.execute(sql, (self._now(), lead_id)).rowcount != 1:
                    return False
                got = conn.execute("SELECT attempts FROM followups WHERE lead_id=?", (lead_id,)).fetchone()
                self._local.token = (lead_id, int(got[0]))  # the ownership token: this claim's attempts value
                return True

        try:
            return self._locked_retry(attempt)
        except sqlite3.OperationalError as e:
            self._local.token = None  # a COMMIT that failed after the token was set must not leave it behind
            if db.is_busy(e):
                log.warning("claim gave up: database stayed locked")
                return False
            raise
        except BaseException:
            self._local.token = None
            raise

    def _take_token(self, lead_id: str) -> int:
        """The attempts value THIS thread's last claim of `lead_id` wrote (read in the claim's own transaction). If
        none is recorded (no claim was recorded), the current value is read: no protection, but no crash."""
        held = getattr(self._local, "token", None)
        self._local.token = None
        if held is not None and held[0] == lead_id:
            return int(held[1])
        row = self._conn().execute("SELECT attempts FROM followups WHERE lead_id=?", (lead_id,)).fetchone()
        return 0 if row is None else int(row[0])

    def _auto_attempts(self, lead_id: str) -> int:
        row = self._conn().execute("SELECT auto_attempts FROM followups WHERE lead_id=?", (lead_id,)).fetchone()
        return 0 if row is None else int(row[0])

    def _pending_or_failed(self, lead_id: str) -> Literal["pending", "failed"]:
        """`pending` (wait, try again) while fewer than SEND_MAX_ATTEMPTS automatic tries were made, else `failed`."""
        return "pending" if self._auto_attempts(lead_id) < SEND_MAX_ATTEMPTS else "failed"

    def _send_claimed(self, lead_id: str, auto: bool, token: int) -> None:
        """Follow-up steps 4-5 for a follow-up this thread has just claimed (state `sending`). Private; calls no claim.
        Crash points (conn=None, one-shot): "D1" after the claim committed and before the send mark; "D1b" after the
        send mark and before outbox.send; "D2" after outbox.send returns and before the result is recorded; "D3" after
        the result is recorded. ("L" after the AI label is stored and "C1" after the CRM put returned, before
        crm_done=1, are in the earlier steps.) Send uses send_key=lead_id.

        Results: a reply that came with the wrong mode is `unknown`. A refusal, an unreachable sender, or (keyed
        sender only, where sending again is safe) an unclear end puts the row back to `pending` with the error until
        SEND_MAX_ATTEMPTS automatic tries were made, then `failed`; the reaper tries again after a backoff (gate.py).
        An unclear end on a blind sender is `unknown` at once (the email may have left; a person decides). A row
        flagged `ambiguous_prior` (an earlier send may have left) is never sent blind: if the sender is blind now it
        becomes `unknown`. A person's Retry also runs with auto=True (its count starts again at the claim)."""
        self.hooks.crash_point("D1", None)
        state: Literal["sent", "failed", "unknown", "pending"]
        err: str | None = None
        try:
            row = (
                self._conn()
                .execute(
                    "SELECT l.name, l.email, f.ambiguous_prior FROM leads l JOIN followups f USING (lead_id)"
                    " WHERE l.lead_id=?",
                    (lead_id,),
                )
                .fetchone()
            )
            name, email, ambiguous_prior = row
            if ambiguous_prior:
                # the sender may have been restarted in the other mode since the cached read; an earlier send may have
                # left, so a mode that could not be read is not good enough to send again
                if not self._refresh_outbox():
                    if getattr(
                        self._local, "outbox_refresh_no_fd", False
                    ):  # no file handle: nothing asked, no try used
                        self._give_back(lead_id, token, NO_DESCRIPTORS_ERROR)
                        return
                    self._record_send_result(lead_id, self._pending_or_failed(lead_id), token, "unreachable")
                    return
                if self.outbox.capability == "blind":
                    self._record_send_result(lead_id, "unknown", token, "ambiguous_prior")
                    return
            # the intent mark goes in BEFORE the send: a row without it never sent anything
            if not self._touch(lead_id, token, send_started=True):  # not ours (taken back, or the mark was not written)
                log.warning("send skipped: the follow-up is no longer ours")
                return
        except Exception as e:  # before the send: nothing left
            log.warning("follow-up failed before the send: %s", type(e).__name__)
            self._abort_unsent(lead_id, token, e)
            return
        self.hooks.crash_point("D1b", None)  # the send mark is written; outbox.send is not called yet
        subject = FOLLOWUP_SUBJECT_TEMPLATE.format(name=name)
        template = FOLLOWUP_BODY_TEMPLATE_PROVIDER if self.config.outbox == "smtp_provider" else FOLLOWUP_BODY_TEMPLATE
        text = template.format(name=name)
        try:
            # A send that does not answer in time is ambiguous (the email may have left, and the abandoned call may
            # still complete): the handling below is the one of SendAmbiguous. A blind sender is never asked while an
            # earlier send for this lead is still running (exclusive); a keyed one is asked again with the same key.
            self._bounded(
                "send",
                lambda: self.outbox.send(lead_id, email, subject, text, self._call_deadline()),
                self._call_deadline(),
                lambda: SendAmbiguous("no answer in time"),
                tag=lead_id,
                exclusive=self.outbox.capability == "blind",
            )
            state = "sent"
        except AdapterBusy:  # no slot (or no thread): the send was never made
            self._note_busy("send")
            self._give_back(lead_id, token, SEND_BUSY_ERROR)
            return
        except OutboxModeMismatch:
            state, err = "unknown", "mode_mismatch"  # the sender answered 2xx: the email is stored, in the wrong mode
            self._refresh_outbox()  # the sender's mode changed: read it now, not at the next 30 s mark
        except SendRefused:
            state, err = "failed", "refused"
        except SendNoDescriptors:  # no file handle for the connection: nothing left, and not the sender's fault
            log.warning("send not made: no file handle left; the follow-up waits")
            self._give_back(lead_id, token, NO_DESCRIPTORS_ERROR)
            return
        except SendUnreachable:
            state, err = "failed", "unreachable"
        except Exception as e:  # SendAmbiguous or anything unexpected: the email may have left
            log.warning("send ended unclear: %s", type(e).__name__)
            err = "ambiguous"
            state = "failed" if self.outbox.capability == "keyed" else "unknown"
        if auto and state == "failed" and err in _RETRYABLE:
            state = self._pending_or_failed(lead_id)
        self.hooks.crash_point("D2", None)
        self._record_send_result(lead_id, state, token, err, ambiguous=err == "ambiguous")
        self.hooks.crash_point("D3", None)  # the result is recorded

    def _record_send_result(
        self,
        lead_id: str,
        state: Literal["sent", "failed", "unknown", "pending"],
        token: int,
        last_error: str | None = None,
        ambiguous: bool = False,
    ) -> None:
        """Internal (not part of the HTTP or test API). Follow-up step 5 for the claimed row ONLY: UPDATE ...
        WHERE lead_id=? AND attempts=<token> AND state='sending'; a row count of 0 (put back, or claimed again by
        another drive) is logged and ignored. A `sent` result clears
        last_error and sets sent_at. `ambiguous` marks the row `ambiguous_prior` (an email may have left)."""
        conn = self._conn()
        now = self._now()

        def attempt() -> int:
            with self._write(conn):
                rc = conn.execute(
                    "UPDATE followups SET state=?, last_error=?, updated_at=?, sent_at=?,"
                    " ambiguous_prior=CASE WHEN ? THEN 1 ELSE ambiguous_prior END"
                    " WHERE lead_id=? AND attempts=? AND state='sending'",
                    (
                        state,
                        None if state == "sent" else last_error,
                        now,
                        now if state == "sent" else None,
                        int(ambiguous),
                        lead_id,
                        token,
                    ),
                ).rowcount
                moved.clear()
                if rc == 1 and state == "sent" and self._mark_contacted(conn, lead_id, now):
                    moved.append(True)
                return rc

        moved: list[bool] = []
        self._finish_update(attempt, "record_send_result")
        if moved:
            self._mirror_not_before.clear(lead_id)  # (the rank in the entry already ignores it; this frees the slot)

    def _mark_contacted(self, conn: sqlite3.Connection, lead_id: str, now: int) -> bool:
        """Inside the caller's write: the follow-up was recorded as sent, so the lead stage moves forward to the
        step the file names `after_followup`. No lead_state row, or a step already further: silent no-op.
        True when the stage moved (the caller then drops the lead's copy wait, after the commit)."""
        flow = self.config.leadflow
        if flow is None:
            return False
        return (
            conn.execute(
                "UPDATE lead_state SET step=?, step_rank=?, changed_at=?, crm_tries=0, crm_error=NULL"
                " WHERE lead_id=? AND step_rank < ?",  # a new stage starts the copy to the CRM afresh
                (flow.after_followup, flow.rank(flow.after_followup), now, lead_id, flow.rank(flow.after_followup)),
            ).rowcount
            == 1
        )

    def set_step(self, lead_id: str, value: object) -> Reply:
        """POST /leads/<id>/step. Order: (1) 404 not_found (lead id not encodable, control character, or no such
        lead) or no_state (feature off, or the lead has no lead_state row); (2) 400 bad_step (not a string or not a
        step id of the file; Closed is allowed); (3) forward-only write, 200 {"step": current, "changed": bool}."""
        self._used = True
        try:
            if not identity.encodable(lead_id) or identity.has_control_char(lead_id):
                return Reply(HTTP_NOT_FOUND, {"error": "not_found"})
            conn = self._conn()
            exists = conn.execute("SELECT 1 FROM leads WHERE lead_id=?", (lead_id,)).fetchone() is not None
            flow = self.config.leadflow
            if flow is None or not exists:
                return Reply(HTTP_NOT_FOUND, {"error": "no_state" if exists else "not_found"})
            if conn.execute("SELECT 1 FROM lead_state WHERE lead_id=?", (lead_id,)).fetchone() is None:
                return Reply(HTTP_NOT_FOUND, {"error": "no_state"})
            ids = [sid for sid, _, _ in flow.steps]
            if not isinstance(value, str) or value not in ids:
                return Reply(HTTP_BAD_REQUEST, {"error": "bad_step"})
            rank, now = flow.rank(value), self._now()

            def attempt() -> tuple[int, str]:
                with self._write(conn):
                    n = conn.execute(
                        "UPDATE lead_state SET step=?, step_rank=?, changed_at=?, crm_tries=0, crm_error=NULL"
                        " WHERE lead_id=? AND step_rank < ?",  # a new stage starts the copy to the CRM afresh
                        (value, rank, now, lead_id, rank),
                    ).rowcount
                    (cur,) = conn.execute("SELECT step FROM lead_state WHERE lead_id=?", (lead_id,)).fetchone()
                    return n, cur

            n, cur = self._locked_retry(attempt)
            if n:
                self._mirror_not_before.clear(lead_id)
            return Reply(HTTP_OK, {"step": cur, "changed": n == 1})
        except Exception as e:
            return self._error_reply(e, None, with_receipt=False)

    def _finish_update(self, attempt: Callable[[], int], what: str) -> None:
        try:
            changed = self._locked_retry(attempt)
        except sqlite3.OperationalError as e:
            if db.is_busy(e):
                log.warning("%s: database stayed locked; the row stays as it was and shows as stalled", what)
                return
            raise
        if changed != 1:
            log.warning("%s: expected one row, changed %d", what, changed)

    # -- review items, retry, resolve
    def _find_item(self, item_id: str) -> str | None:
        """'F', 'R', 'D', 'O' or 'N' when the item exists, else None (also for a malformed id)."""
        if not item_id[1:] or item_id[0] not in "FRDON":
            return None
        kind, ref = item_id[0], item_id[1:]
        if not identity.encodable(ref) or identity.has_control_char(ref):
            return None
        conn = self._conn()
        if kind == "F":
            found = conn.execute("SELECT 1 FROM followups WHERE lead_id=?", (ref,)).fetchone()
        elif kind == "N":
            found = conn.execute("SELECT 1 FROM nudges WHERE lead_id=?", (ref,)).fetchone()
        elif not (ref.isascii() and ref.isdecimal()) or len(ref) > MAX_DIGITS or ref[0] == "0":
            # not a number, one no receipt id can reach (also keeps int() clear of its digit limit), or written with
            # a leading zero (R0012 would be the same item as R12: every item has exactly one id)
            return None
        else:
            found = conn.execute(_ITEM_EXISTS[kind], (int(ref),)).fetchone()
        return kind if found else None

    def _not_actionable(self, kind: str, item_id: str = "") -> Reply:
        hint = HINTS[kind]
        if kind == "R" and item_id[1:].isdecimal():
            row = self._conn().execute("SELECT outcome_reason FROM receipts WHERE receipt_id=?", (int(item_id[1:]),))
            found = row.fetchone()
            if found is not None and found[0] == "internal_error":
                hint = HINT_INTERNAL_ERROR
        return Reply(HTTP_CONFLICT, {"error": "not_actionable", "hint": hint})

    def retry(self, item_id: str) -> Reply:
        """Check order: item id grammar and existence (404), then the 409 rows (see contract.md). Reads no state
        first. For an F item it makes its OWN claim `self._claim(None, lead_id)` (state IN
        ('pending','failed')), and if that returns True it runs `_after_claim` (steps 1-2, then the send). It answers
        409 in_progress, never 200, when the row is still `sending` afterwards. It does NOT call
        run_followup and run_followup does not call it. If the claim returns False it re-reads the state to
        choose the 409 code. An exception in steps 1-2 after the claim goes through `_abort_unsent`: the claim
        started the automatic count at 1, so the row is `pending` with `internal:<class>` and a backoff (not
        `failed`), and Retry stays possible; a database that stayed locked gives the try back instead, except after the
        model has answered (`ModelAnswerLost`: the model was asked, so that try stays used up). A `D` item is 409
        `not_actionable` in every mode, approval mode included: only `resolve` decides a `D` item."""
        self._used = True
        try:
            kind = self._find_item(item_id)
            if kind is None:
                return Reply(HTTP_NOT_FOUND, {"error": "not_found"})
            if kind != "F":
                return self._not_actionable(kind, item_id)
            lead_id = item_id[1:]
            if self._claim(None, lead_id, ("pending", "failed")):  # a failed row: the automatic count starts again
                self._after_claim(lead_id, auto=True)  # a Retry gets a fresh automatic count
                state_now = self._followup_state(lead_id)
                if state_now == "sending":  # the result could not be recorded: never answer 200 with `sending`
                    return Reply(HTTP_CONFLICT, {"error": "in_progress"})
                return Reply(HTTP_OK, {"item_id": item_id, "state": state_now})
            state = self._followup_state(lead_id)
            code = {"sending": "in_progress", "unknown": "unknown_needs_person", "sent": "not_retryable"}.get(
                str(state)
            )
            if state is None:
                return Reply(HTTP_NOT_FOUND, {"error": "not_found"})
            if code is None:  # pending or failed, yet the claim failed: the database stayed locked
                return Reply(HTTP_BUSY, {"error": "busy"})
            return Reply(HTTP_CONFLICT, {"error": code})
        except Exception as e:
            return self._error_reply(e, None, with_receipt=False)

    def _call_running(self, lead_id: str) -> bool:
        """True while a send or CRM call of this lead (one the desk may have given up waiting for) is still running."""
        with self._worker_lock:
            return any(self._tagged.get((kind, lead_id), 0) > 0 for kind in ("send", "crm"))

    def resolve(self, item_id: str, decision: str) -> Reply:
        """Check order: see contract.md, section Resolve check order (the server does the first check). Approval mode
        (DESK_LATER_MODE=approval): a D item goes to `_decide_later` instead of step (2) below. Here, in this
        order: (1) item id grammar and existence -> 404; (2) N item (a reminder): decision other than
        "seen" -> 400, failed or unknown -> 200 and marked handled (a repeat too), else 409 not_to_check; R, D or
        O item -> 409 not_actionable (decision not looked at); (3) decision not in
        {"sent", "not_sent", "crm_present"} -> 400 bad_decision;
        (4) F item whose state is not `unknown` -> 409 not_unknown; an unknown item that this answer does not fit
        (crm_present unless crm_done is 2, or sent unless crm_done is 1) -> 409 wrong_decision.
        `crm_present` (only for crm_done=2: a CRM put was started and its end is unknown) sets crm_done=1 and
        state `failed`, so Retry goes straight to the email. `not_sent` fits every unknown row; it clears
        `ambiguous_prior` and the leftover `crm_done=2` of a `crm_unconfirmed` row, so a Retry afterwards is not
        stopped by them again."""
        self._used = True
        try:
            kind = self._find_item(item_id)
            if kind is None:
                return Reply(HTTP_NOT_FOUND, {"error": "not_found"})
            if kind == "D" and self.config.later_mode == "approval":
                return self._decide_later(item_id, decision)
            if kind == "N":
                return self._resolve_nudge(item_id, decision)
            if kind in "RDO":
                return self._not_actionable(kind, item_id)
            if decision not in _RESOLVE_DECISIONS:
                return Reply(HTTP_BAD_REQUEST, {"error": "bad_decision"})
            lead_id = item_id[1:]
            if decision == "not_sent" and self._call_running(lead_id):
                # The earlier send or CRM put of this lead is still running (the desk only stopped waiting for it): it
                # may still land, so a check that finds nothing proves nothing yet. The row stays `unknown`.
                return Reply(HTTP_CONFLICT, {"error": "call_still_running", "hint": HINT_CALL_RUNNING})
            new_state, note = {
                "sent": ("sent", "person: sent"),
                "not_sent": ("failed", "person: not sent"),
                "crm_present": ("failed", "person: crm row present"),
            }[decision]
            undo = int(decision == "not_sent")  # the person looked and decided nothing was sent: forget both marks
            present = int(decision == "crm_present")  # the person saw the CRM row: the CRM step is done
            conn = self._conn()
            now = self._now()
            # crm_present fits only a row whose CRM step is open (crm_done 2: a put may have reached the CRM);
            # sent fits only a row whose CRM step is finished (crm_done 1: only then did an email attempt follow)
            as_sent = int(decision == "sent")

            def attempt() -> int:
                with self._write(conn):
                    rc = conn.execute(
                        "UPDATE followups SET state=?, last_error=?, updated_at=?, sent_at=?,"
                        " ambiguous_prior=CASE WHEN ? THEN 0 ELSE ambiguous_prior END,"
                        " crm_done=CASE WHEN ? AND crm_done=2 AND last_error IS 'crm_unconfirmed' THEN 0"
                        " WHEN ? THEN 1 ELSE crm_done END"
                        " WHERE lead_id=? AND state='unknown'"
                        " AND (? = 0 OR crm_done = 2)"
                        " AND (? = 0 OR crm_done = 1)",
                        (
                            new_state,
                            note,
                            now,
                            now if new_state == "sent" else None,
                            undo,
                            undo,
                            present,
                            lead_id,
                            present,
                            as_sent,
                        ),
                    ).rowcount
                    if rc == 1 and new_state == "sent":
                        self._mark_contacted(conn, lead_id, now)
                    return rc

            if self._locked_retry(attempt) != 1:
                row = conn.execute("SELECT state, crm_done FROM followups WHERE lead_id=?", (lead_id,)).fetchone()
                if row is not None and row[0] == "unknown":  # the row waits, but not for this answer
                    hint = HINT_CRM_UNCONFIRMED if row[1] in (CRM_NOT_ASKED, CRM_STARTED) else HINT_EMAIL_UNKNOWN
                    return Reply(HTTP_CONFLICT, {"error": "wrong_decision", "hint": hint})
                return Reply(HTTP_CONFLICT, {"error": "not_unknown", "hint": HINT_NOT_UNKNOWN})
            return Reply(HTTP_OK, {"item_id": item_id, "state": new_state})
        except Exception as e:
            return self._error_reply(e, None, with_receipt=False)

    def _decide_later(self, item_id: str, decision: str) -> Reply:
        """Approval mode, a D item. Order: (1) the receipt is not a later message with new details (a same-request-
        number-only item) -> 409 not_actionable, decision not looked at; (2) the word does not fit approve:<id> or
        deny:<id> -> 400 bad_decision; (3) the id is not in the approvers list -> 403 not_an_approver; (4) ONE write:
        the first decision wins (primary key = the receipt); 200 with the row when this call wrote it, else 409
        already_decided with the winner's row. Records the decision only: no lead, follow-up, CRM or email changes."""
        rid, conn = int(item_id[1:]), self._conn()
        # the verdict is write-once (trigger), so this cannot change between the check and the write
        if conn.execute(_LATER_DECIDABLE, (rid,)).fetchone() is None:
            return self._not_actionable("D", item_id)
        m = _LATER_WORD.fullmatch(decision)
        if m is None:
            return Reply(HTTP_BAD_REQUEST, {"error": "bad_decision"})
        approver_id = m[2]
        name = self._approvers.get(approver_id)
        if name is None:
            return Reply(HTTP_FORBIDDEN, {"error": "not_an_approver"})
        decided = conn.execute(_DECIDED, (rid,)).fetchone()  # cheap read: an item decided already never takes the gate
        if decided is not None:
            return self._already_decided(item_id, decided)
        now = self._now()

        def attempt() -> tuple[int, tuple[Any, ...]]:
            with self._write(conn):
                n = conn.execute(_DECIDE, (rid, _STORED_WORD[m[1]], approver_id, name, now)).rowcount
                return n, conn.execute(_DECIDED, (rid,)).fetchone()

        written, row = self._locked_retry(attempt)
        if written == 1:
            return Reply(HTTP_OK, {"item_id": item_id, **dict(zip(DECISION_KEYS, row, strict=True))})
        return self._already_decided(item_id, row)

    @staticmethod
    def _already_decided(item_id: str, row: tuple[Any, ...]) -> Reply:
        """The 409 of a decision that lost (or came late): names the winner's row."""
        return Reply(
            HTTP_CONFLICT,
            {"error": "already_decided", "item_id": item_id, **dict(zip(DECISION_KEYS, row, strict=True))},
        )

    # -- reads
    def review(self) -> Reply:
        try:
            return self._review()
        except Exception as e:
            return self._error_reply(e, None, with_receipt=False)

    def _review(self) -> Reply:
        return Reply(HTTP_OK, review_rows(self._conn(), self.stalled_before()))

    def lead(self, lead_id: str) -> Reply:
        try:
            return self._lead(lead_id)
        except Exception as e:
            return self._error_reply(e, None, with_receipt=False)

    def _lead(self, lead_id: str) -> Reply:
        conn = self._conn()
        if not identity.encodable(lead_id) or identity.has_control_char(lead_id):
            return Reply(HTTP_NOT_FOUND, {"error": "not_found"})  # no lead id can contain such text
        row = conn.execute(
            "SELECT l.lead_id, l.receipt_id, l.source, l.name, l.email, l.phone, l.company, l.message, l.created_at,"
            " f.state, ai_label, f.attempts, f.last_error, f.sent_at, s.owner_name, s.step, n.state"
            " FROM leads l JOIN followups f ON f.lead_id = l.lead_id"
            " LEFT JOIN lead_state s ON s.lead_id = l.lead_id LEFT JOIN nudges n ON n.lead_id = l.lead_id"
            " WHERE l.lead_id=?",
            (lead_id,),
        ).fetchone()
        if row is None:
            return Reply(HTTP_NOT_FOUND, {"error": "not_found"})
        lead_keys = ("lead_id", "receipt_id", "source", "name", "email", "phone", "company", "message", "created_at")
        fu_keys = ("state", "ai_label", "attempts", "last_error", "sent_at")
        nl = len(lead_keys)
        end = nl + len(fu_keys)
        return Reply(
            HTTP_OK,
            {
                "lead": dict(zip(lead_keys, row[:nl], strict=True)),
                "followup": dict(zip(fu_keys, row[nl:end], strict=True)),
                **dict(zip(("owner", "step", "reminder"), row[end:], strict=True)),
            },
        )

    def counts(self, expected: int | None) -> Reply:
        """expected=None when the query has no `expected`; server.py answers 400 bad_expected for a bad value. The body
        also carries `hung_workers`: per kind of adapter call (model, crm, send, refresh) how many calls the desk gave
        up on and that are still running. It is information only: it never changes `ok`."""
        try:
            return self._counts(expected)
        except Exception as e:
            return self._error_reply(e, None, with_receipt=False)

    def _counts(self, expected: int | None) -> Reply:
        received, new, dup, review, open_, pending, failed, unknown, sending = (
            self._conn().execute(_COUNTS_SQL).fetchone()
        )
        with self._rejected_lock:
            rejected = self._rejected
        match = None if expected is None else received == expected
        ok = (
            None
            if expected is None
            else (
                open_ == 0
                and rejected == 0
                and self._integrity_errors == 0
                and match is True
                and pending == 0
                and failed == 0
                and unknown == 0
            )
        )
        return Reply(
            HTTP_OK,
            {
                "received": received,
                "new": new,
                "duplicate": dup,
                "review": review,
                "open": open_,
                "rejected_envelopes": rejected,
                "pending_followups": pending,
                "failed_followups": failed,
                "unknown_followups": unknown,
                "sending_followups": sending,
                "hung_workers": self.hung_workers(),
                "decide_integrity_errors": self._integrity_errors,
                "decide_transient_errors": self._transient_errors,
                "reaper_running": self.reaper_running(),
                "last_reaper_pass_at": self._last_reap_at,
                "expected_match": match,
                "ok": ok,
            },
        )
