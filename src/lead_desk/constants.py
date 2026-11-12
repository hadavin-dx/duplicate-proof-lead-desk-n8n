"""Named values that are not settings. Ports and defaults live in config.Config, not here."""

from __future__ import annotations

import ipaddress
import math
from typing import Final

# HTTP status codes the desk and the fakes server use
HTTP_OK: Final = 200
HTTP_CREATED: Final = 201
HTTP_ACCEPTED: Final = 202
HTTP_BAD_REQUEST: Final = 400
HTTP_NOT_FOUND: Final = 404
HTTP_METHOD_NOT_ALLOWED: Final = 405
HTTP_REQUEST_TIMEOUT: Final = 408
HTTP_CONFLICT: Final = 409
HTTP_TOO_LARGE: Final = 413
HTTP_UNPROCESSABLE: Final = 422
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_INTERNAL_ERROR: Final = 500
HTTP_BUSY: Final = 503

# Process exit codes
EXIT_OK: Final = 0
EXIT_ANOTHER_DESK: Final = 2  # another desk holds the lock file (the lead book is in use)
EXIT_SETTING_REFUSED: Final = (
    3  # a limit in constants.py does not fit this computer (too many handler threads for the open-file limit)
)
EXIT_DESK_SETTING_REFUSED: Final = 4  # a setting was refused (Config, an adapter, or the Desk)
EXIT_PORT_UNAVAILABLE: Final = 5  # the port could not be bound within BIND_RETRY_SECONDS
EXIT_SIMULATED_KILL: Final = 137  # scripts/crash_desk.py: os._exit(137) at the chosen crash point

# Field limits
SOURCE_REF_MIN: Final = 1
SOURCE_REF_MAX: Final = 128
NAME_MAX: Final = 200
EMAIL_MAX: Final = 254
EMAIL_KEY_MIN: Final = 3
PHONE_MAX: Final = 40
COMPANY_MAX: Final = 200
MESSAGE_MAX: Final = 2000

# SQLite and server rules
BUSY_WAIT_SECONDS: Final = 5.0  # sqlite3.connect(timeout=...): the one busy-wait setting
LISTEN_QUEUE: Final = 1024  # request_queue_size (listen backlog); the operating system may cap it lower
LOCK_RETRIES: Final = 3  # lock-timeout retries for the claim and the result record
BIND_RETRY_SECONDS: Final = 5.0  # at start-up the desk retries a failed bind this long (a restart on Windows can
# find the old port in TIME_WAIT), then exits with EXIT_PORT_UNAVAILABLE
BIND_RETRY_STEP_SECONDS: Final = 0.25  # pause between two bind attempts
SCHEMA_VERSION: Final = 1  # PRAGMA user_version after schema.sql
LEAD_ID_PREFIX: Final = "L"
LEAD_ID_HEX_CHARS: Final = 12
SQLITE_BUSY: Final = 5  # sqlite_errorcode & 0xFF
SQLITE_LOCKED: Final = 6

# Every call to an adapter is bounded by the desk itself, with ONE mechanism (Desk._bounded): the call runs in a daemon
# worker thread and the desk waits for it at most a TOTAL deadline of real time, whatever the adapter does (an adapter
# whose socket timeout is per operation can be kept going by a slow trickle for much longer). The four calls are the AI
# step (budget MODEL_CALL_BUDGET_SECONDS), the CRM put and the outbox send (deadline: the config's send_timeout_seconds,
# at most CALL_DEADLINE_MAX_SECONDS) and the capability / mode reads of the CRM and the sender (the same deadline). A
# call that does not answer in time is abandoned: its worker thread is left to end on its own, its late answer is
# thrown away and never reaches the database, and the outcome is the safe one for that call (a model call or a
# keyed CRM put or send: one used-up try, asked again later with the same key; a blind CRM or sender: `unknown`, a
# person looks; a capability read: the mode counts as unreadable). So a hung or trickling adapter cannot hold the
# thread that drives the row, the reaper included. The number of workers of one kind that exist at once, abandoned
# ones included, is capped (ADAPTER_WORKER_CAPS): a slot is reserved BEFORE the worker is started, so the bound is
# exact. At the cap the desk starts no new call of that kind (nothing is asked or sent: the row waits with
# `model_busy`, `crm_busy` or `send_busy`, the try is given back). The reaper puts back a `sending` row that has not
# been stamped for the stuck grace (send timeout + SENDING_GRACE_BUSY_WAITS busy waits, 30 s with the defaults). The row
# is stamped before each stage and the stages are bounded, so what must fit in the grace is the longest stage plus the
# write of its closing stamp: the budget plus MODEL_GRACE_MARGIN_SECONDS, where the margin is the longest that one write
# can take (WRITE_DEADLINE_SECONDS in gate.py, 10 s, plus one more SQLite busy wait for a lock held by another process,
# 5 s: 15 s). validate_model_budget checks it, strictly (an equal sum is refused, the clocks count whole seconds), for
# the model budget and for CALL_DEADLINE_MAX_SECONDS. The reaper takes a row only when it is strictly older than the
# grace. An adapter that declares a `timeout_seconds` above MODEL_CALL_BUDGET_SECONDS is refused when the desk is built
# (the desk cuts every model call at that budget, so such an adapter could never answer in time); 9 s is accepted, 9.1 s
# is not. `timeout_seconds` is an honest wall-clock bound only if the adapter enforces a TOTAL deadline; the desk's
# bound does not depend on it.
MODEL_CALL_BUDGET_SECONDS: Final = 9.0
MODEL_GRACE_MARGIN_SECONDS: Final = 15.0
CALL_DEADLINE_MAX_SECONDS: Final = 9.0  # the longest total deadline of a CRM put, a send or a capability read
# Workers of one kind that may exist at once (running or abandoned). The model cap is below the drive places on purpose
# (a model call is slow); the CRM and sender caps are not below them (a healthy burst of drives is never turned away);
# the reads happen on the reaper and in rare drive steps. The sum is the most abandoned workers that can hold a socket.
ADAPTER_WORKER_CAPS: Final = (("model", 4), ("crm", 8), ("send", 8), ("refresh", 4))
ADAPTER_WORKERS_TOTAL: Final = sum(n for _, n in ADAPTER_WORKER_CAPS)

# Retry rule of the burst script, the portal demo's burst and the load_tools load driver: all retry by TIME (so a
# desk restart does not fail a burst): waits double from BURST_BACKOFF_SECONDS up to BURST_BACKOFF_MAX_SECONDS
# (a Retry-After is honoured up to that maximum) until BURST_RETRY_DEADLINE_SECONDS after the first try. These are the
# only values.
BURST_BACKOFF_SECONDS: Final = 0.1
BURST_BACKOFF_MAX_SECONDS: Final = 1.0
BURST_RETRY_DEADLINE_SECONDS: Final = 30.0

# Names used in more than one module
SOURCES: Final = ("form", "webhook")
VERDICTS: Final = ("new", "duplicate", "review")
FOLLOWUP_STATES: Final = ("pending", "sending", "sent", "failed", "unknown")
FOLLOWUP_SUBJECT_TEMPLATE: Final = "Thanks for getting in touch, {name}"  # the follow-up mail the desk sends
FOLLOWUP_BODY_TEMPLATE: Final = "Hi {name}, we received your message. (DEMO EMAIL)"  # load_tools checks the same text
# the same line for the mail provider kind, which marks every message as a demo message by itself (subject tag and
# first body lines, smtp_provider.py), so the stand-in marker is not repeated there
FOLLOWUP_BODY_TEMPLATE_PROVIDER: Final = "Hi {name}, we received your message."
AI_LABELS: Final = ("hot", "warm", "cold")

# Config validation
PORT_MAX: Final = 65535
# Sane ceilings for number settings (a setting above one is refused by name; each is far above any normal use and
# far below a value that could overflow a time or lift a cap in practice).
CAP_COUNT_MAX: Final = 10_000  # most sends / CRM calls / read-back calls a cap setting may allow
REAL_PERSONS_MAX: Final = 1_000  # most invented people one real-services run may send
STALL_SECONDS_MAX: Final = 86_400  # one day
SIZE_CAP_BYTES_MAX: Final = 16_777_216  # 16 MiB
SEND_TIMEOUT_SECONDS_MAX: Final = 600.0  # the desk still cuts every call at CALL_DEADLINE_MAX_SECONDS
REAL_SECONDS_MAX: Final = 86_400.0  # longest wait a REAL_*_SECONDS setting of the real-services script may hold
ADAPTER_CHOICES: Final = ("demo",)
QUALIFIER_CHOICES: Final = ("recorded", "live_claude")  # the AI step: replies from a list, or a live model call
# the sender: the fakes server, an SMTP test server (lead_desk.smtp_outbox) or a real mail provider
OUTBOX_CHOICES: Final = ("http", "smtp", "smtp_provider")
OUTBOX_STAND_INS: Final = ("http", "smtp")  # the sender kinds that stay on this computer; any other kind is real
QUALIFIER_STAND_IN: Final = "recorded"  # the AI step kind that calls nothing; any other kind is real
SMTP_MODULE: Final = "lead_desk.smtp_outbox"  # loaded lazily when outbox == "smtp"
# Real-services kinds (each loaded lazily; the neutral default stays the fake CRM and the loopback sender)
SMTP_PROVIDER_MODULE: Final = "lead_desk.smtp_provider"  # outbox == "smtp_provider": a real mail provider
CRM_STAND_IN: Final = "fake"  # the CRM kind that is a stand-in; every other CRM kind reaches a real service
CRM_CHOICES: Final = (
    CRM_STAND_IN,
    "hubspot",
)  # the CRM: the fakes server, or a HubSpot account (lead_desk.crm_hubspot)
LATER_MODES: Final = ("off", "approval")  # DESK_LATER_MODE: approval mode lets a person decide a later message
APPROVER_ID_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,31}"  # an approver id; at most 32 characters (the table CHECK)
APPROVER_NAME_MAX: Final = 64  # longest approver name; the same number is in the table CHECK of schema.sql
APPROVER_ID_MAX: Final = 32  # longest approver id; the same number is in the table CHECK of schema.sql
APPROVERS_FILE_MAX_BYTES: Final = 16384  # the approvers file is read only up to this size

# -- lead owner, lead stage and reminder (DESK_LEADFLOW_FILE; every limit of the leadflow file lives here)
# WARNING: writing a lead stage changes the CRM's lead status field;
# automations in the CRM that watch it may react.
LEADFLOW_OFF: Final = "off"  # DESK_LEADFLOW_FILE set to this word (lower case, after strip) switches the feature off
LEADFLOW_FILE_MAX_BYTES: Final = 16384  # the leadflow file is read only up to this size
LEADFLOW_ROTA_MAX: Final = 20  # most people on the lead owner list
LEADFLOW_STEPS_MAX: Final = 12  # most steps in the file's steps list (Closed not counted)
LEADFLOW_ID_PATTERN: Final = (
    r"[a-z0-9][a-z0-9-]{0,31}"  # a rota id or a step id; the same grammar as APPROVER_ID_PATTERN
)
LEADFLOW_NAME_MAX: Final = 64  # longest lead owner name; the same number is in the table CHECK of schema.sql
LEADFLOW_LABEL_MAX: Final = 32  # longest step label
LEADFLOW_CRM_VALUE_PATTERN: Final = r"[A-Za-z0-9_-]{1,64}"  # a CRM owner id or a CRM status value
NUDGE_AFTER_MAX_SECONDS: Final = 2678400  # longest wait before a reminder: 31 days
NUDGE_KEY_PREFIX: Final = "nudge:"  # key and tag of a reminder email: this prefix plus the lead id
NUDGES_PER_PASS: Final = 5  # most reminders claimed in one reaper pass
MIRROR_PER_PASS: Final = 5  # most leads copied to the CRM in one reaper pass
MIRROR_MAX_TRIES: Final = 5  # failed copy tries in a row, then the lead is left
# Waits (seconds) before the next copy try after the 1st, 2nd, 3rd and 4th failed try in a row: a CRM that is down or
# rate-limited for a few minutes gets the copy afterwards. Together over 10 minutes (kept in memory; a restart
# tries once at once).
MIRROR_RETRY_WAIT_SECONDS: Final = (10.0, 60.0, 180.0, 420.0)
MIRROR_CALL_SHARE: Final = 0.5  # the CRM copy may use at most this share of DESK_HUBSPOT_MAX_CALLS (rounded down)
HUBSPOT_MODULE: Final = "lead_desk.crm_hubspot"
# The vendor host names live HERE and nowhere else (the repo's address scan allows this one file for them). Every other
# file, every test and every example imports these values or uses a reserved example name.
HUBSPOT_BASE_URL: Final = "https://api.hubapi.com"  # the CRM's address; DESK_HUBSPOT_BASE_URL overrides it
HUBSPOT_HOST_SUFFIXES: Final = ("hubapi.com", "hubspot.com")  # the key is sent only to these (or on purpose elsewhere)
MAIL_IMAP_DEFAULT_HOST: Final = "imap.gmail.com"  # default of REAL_IMAP_n_HOST in scripts/run_real_check.py
MAIL_SPAM_FOLDER: Final = "[Gmail]/Spam"  # the provider's English spam folder name; REAL_IMAP_n_FOLDERS overrides it

# Live model step (live_claude.py). No price, cap or model id lives here: they are required settings.
LIVE_DEFAULT_BASE_URL: Final = "https://api.anthropic.com"  # the provider's address; override by setting
# anthropic-version header value (config can override); where the value comes from: versions.md
LIVE_API_VERSION: Final = "2026-10-21"
LIVE_DEFAULT_EFFORT: Final = "low"  # output_config effort: one-word answer, so little thinking (config can override)
LIVE_ERROR_BODY_MAX: Final = 4096  # bytes of an error body read for its error type
LIVE_ERROR_TYPE_MAX: Final = 40
LIVE_DEFAULT_TIMEOUT_SECONDS: Final = 8.0  # the total time limit of one call (a worker thread is joined for this long)
LIVE_DEFAULT_MAX_TOKENS: Final = 64  # one word comes back; thinking is billed as output and counts here, so not tiny
LIVE_REPLY_MAX_BYTES: Final = 65_536
LIVE_PER_MILLION: Final = 1_000_000
LIVE_LEDGER_LOCK_TRIES: Final = 50  # blocking ledger lock: tries x LIVE_LEDGER_LOCK_WAIT seconds
LIVE_LEDGER_LOCK_WAIT: Final = 0.1
LIVE_KEY_MIN_CHARS: Final = 8
LIVE_LEDGER_FILE_MAX_BYTES: Final = 8_000_000  # a bigger ledger file is refused (it is read whole on every reserve)
LIVE_PRICE_MIN: Final = "0.000001"  # US$ per million tokens: below this the worst-case cost could round to zero
LIVE_PRICE_MAX: Final = "1000000"
LIVE_MAX_TOKENS_MAX: Final = 1_000_000
LIVE_REPORTED_TOKENS_MAX: Final = 1_000_000_000  # a token count a provider reports above this is not believed
LIVE_LEDGER_TAIL_WINDOW: Final = 65_536  # bytes read from the end of the ledger to look at an unterminated last line
LIVE_LEDGER_USD_MAX: Final = "1000000"  # sanity bound: a ledger amount above this (US$) is a broken row

# HTTP layer
REQUEST_TIMEOUT_SECONDS: Final = 30.0  # socket timeout of one request handler; frees a thread held by a stalled client
HEADER_READ_DEADLINE_SECONDS: Final = 10.0  # wall-clock limit: request line plus all headers
BODY_READ_DEADLINE_SECONDS: Final = 10.0  # wall-clock limit: the whole body, from the end of the headers
# Cap on connections that are still being READ (request line, headers, body). A connection takes a place when it is
# accepted and gives it back when the desk starts to write the reply (or on any error), so work after the reply (the
# follow-up) and a slow reader of the reply never hold a place. When the cap is reached, the OLDEST connection that is
# still being read AND has sent no byte for EVICT_MIN_IDLE_SECONDS is closed to make room, so a crowd of stalled
# senders cannot lock real callers out while a sender that is still sending is never the one closed. A connection
# stops being closable at start_work: the moment its whole request has been read, parsed and checked (JSON and
# envelope) and the desk is about to act on it, so a request that has a receipt is never closed. When no connection
# can be closed, the new one gets 503 busy (with Retry-After).
MAX_CONNECTIONS: Final = 256
MAX_CONNECTIONS_MIN: Final = 8  # accepted range of MAX_CONNECTIONS (checked below, at import)
MAX_CONNECTIONS_MAX: Final = 4096
# Cap on handler threads in total: a thread counts from the moment the server hands a connection to it until the thread
# ends, so the reply write, the draining and the follow-up after the reply are counted too (MAX_CONNECTIONS only
# counts the reading part). At the cap a new connection gets the same complete 503 busy and nothing is stored.
MAX_HANDLER_THREADS: Final = 1024
MAX_HANDLER_THREADS_MIN: Final = 8  # accepted range of MAX_HANDLER_THREADS (checked below, at import)
MAX_HANDLER_THREADS_MAX: Final = 65536
EVICT_MIN_IDLE_SECONDS: Final = (
    2.0  # a reading connection may be closed for room only after this long with no byte received
)
REPLY_WRITE_TIMEOUT_SECONDS: Final = (
    5.0  # total wall-clock limit for writing one reply; a client that does not read it loses the thread
)
# Open files one handler thread may hold, counted for the worst case (every thread at once): its connection socket (1),
# its database connection in WAL mode (the database file and the -wal file: 2) and the ONE adapter socket of the call it
# is making (a drive asks the AI step, the CRM and the sender one after the other, never two at once: 1). That is 4. A
# call that the desk gave up on keeps its socket in its abandoned worker until it ends by itself; those are counted
# apart, in the reserve below (ADAPTER_WORKERS_TOTAL). The -shm file is opened once per process and shared by every
# connection, so it is counted in the reserve below too.
FDS_PER_HANDLER_THREAD: Final = 4
ACCEPT_PAUSE_SECONDS: Final = 0.05  # the accept loop waits this long after "no file handle left", so it cannot spin
PORTAL_MAX_INFLIGHT: Final = 8  # portal JSON requests answered at once; more get 503 busy
FDS_PER_THREAD_MINIMUM: Final = 4  # a handler thread holds at least this many (see above); FDS_PER_HANDLER_THREAD >= it
FD_PORTAL_ITEM: Final = "portal read-only database connections"
# Open files for everything that is not a handler thread, item by item (the sum must not exceed FD_RESERVE; checked at
# import by validate_descriptor_budget): the listening socket, the lock file, the standard streams and log handler, the
# shared -shm file, the reaper's database connection (database + -wal), the portal's read-only database connections
# (PORTAL_MAX_INFLIGHT of them, 2 files each), the sockets of the reaper's own adapter calls and the sockets of the
# abandoned workers (at most ADAPTER_WORKERS_TOTAL, one socket each).
# Refused connections kept open for a moment after their 503 (see server._Lingerer): at most this many at once; the
# next refusal is closed at once.
LINGER_MAX_SOCKETS: Final = 16
FD_RESERVE_ITEMS: Final = (
    ("listening socket", 1),
    ("lock file", 1),
    ("standard streams and log handler", 4),
    ("shared -shm file", 1),
    ("reaper database connection", 2),
    (FD_PORTAL_ITEM, PORTAL_MAX_INFLIGHT * 2),
    ("reaper adapter socket", 1),
    ("lingering refused connections", LINGER_MAX_SOCKETS),
    ("abandoned adapter workers", ADAPTER_WORKERS_TOTAL),
)
FD_RESERVE: Final = 96  # open files for everything else: at least the sum of FD_RESERVE_ITEMS, the rest is slack
BUSY_WRITE_TIMEOUT_SECONDS: Final = 1.0  # writing one 503 busy reply (the wait after it: QUIET_DRAIN_SECONDS)
BUSY_RETRY_AFTER_SECONDS: Final = 1  # Retry-After header of that 503, in whole seconds
QUIET_DRAIN_SECONDS: Final = 0.05  # how long a finished connection waits for request bytes still on their way
DRAIN_TOTAL_SECONDS: Final = 1.0  # wall-clock limit of all draining on one connection, however slowly bytes arrive
DRAIN_MAX_BYTES: Final = (
    1_048_576  # unread request bytes read and dropped after an early reply, so the reply is not lost to a reset
)
EXIT_ADAPTERS_UNREACHABLE: Final = 1  # start-up cannot reach the sender (/fake/info)
MAX_DIGITS: Final = 18  # longest Content-Length or item number taken as a number; longer is over every limit
REPLY_MAX_BYTES: Final = (
    1_048_576  # an adapter reply longer than this is refused unread (the fakes answer a few hundred bytes)
)

# Portal (read-only pages and JSON under /ui and /api/ui)
HTTP_FORBIDDEN: Final = 403
HTTP_PERMANENT_REDIRECT: Final = 308
SEARCH_MAX: Final = 100  # longest `q` of /api/ui/leads, in characters
OFFSET_MAX: Final = 100_000
LEADS_LIMIT_MAX: Final = 100
LEADS_LIMIT_DEFAULT: Final = 50
DUPLICATES_MAX: Final = 50  # repeats listed for one lead (the total is always given)
FEED_ITEMS: Final = 12  # newest events in the Overview feed
LOG_LIMIT_DEFAULT: Final = 100  # rows of /api/ui/log when `limit` is not given
LOG_LIMIT_MAX: Final = 200  # most rows one /api/ui/log page may ask for
# What the AI step is told, by parameter name of Qualifier.qualify -> the lead field that parameter carries. A new
# parameter that is not listed here makes /api/ui/data-map refuse to answer (it never guesses).
AI_INPUT_FIELDS: Final = {"lead_id": "lead_id", "email_key": "email"}
# Every lead field a person typed or the desk made up for a lead; "never given to the AI" is this set minus what the
# step is given. (Bookkeeping columns of the leads table are not lead content; a test keeps both lists complete.)
LEAD_CONTENT_FIELDS: Final = ("lead_id", "name", "email", "phone", "company", "message")
LEAD_BOOKKEEPING_COLUMNS: Final = ("receipt_id", "source", "email_key", "created_at")
# What the page's action buttons can change, all through the desk (not through the read-only connection). The demo
# controls are listed only when the demo extension is loaded.
PORTAL_ACTIONS: Final = ("retry", "resolve")
DEMO_CONTROLS_ACTION: Final = "demo_controls"
AI_STEP_NOTE: Final = (
    "lists the inputs the AI is given, derived from its signature; it does not prove what a live AI looks up by id"
)
QUALIFIER_MODES: Final = ("recorded", "live")  # what /api/ui/data-map may say about the AI step
QUALIFIER_MODE_FALLBACK: Final = "live"  # a qualifier that does not declare a mode is not called "recorded"
SERIES_BUCKETS: Final = 60
SERIES_BUCKET_SECONDS: Final = 5
DEMO_BODY_MAX: Final = 4096  # longest request body handed to the demo extension
HOST_NAME_MAX: Final = 253
LEAD_ID_PATTERN: Final = rf"^{LEAD_ID_PREFIX}[0-9a-f]{{{LEAD_ID_HEX_CHARS}}}$"  # prefix + lower-case hex characters
FIELD_NAME_MAX: Final = 100  # longest field name echoed back in a 400 reply
STATIC_FILE_MAX_BYTES: Final = 1_000_000  # largest portal file read into the fixed map
DEMO_METHODS: Final = ("GET", "POST")  # methods the demo door accepts
SECURITY_HEADERS: Final = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)
STATIC_TYPES: Final = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
}
SAME_SITE_VALUES: Final = ("same-origin", "none")  # Sec-Fetch-Site values the gate lets through
LOOPBACK_HOSTS: Final = ("127.0.0.1", "localhost", "[::1]")
# bind addresses that mean "every interface"; never a Host name
WILDCARD_BINDS: Final = (str(ipaddress.IPv4Address(0)), str(ipaddress.IPv6Address(0)))


def is_positive_seconds(value: object) -> bool:
    """True for a real number above 0 that is not NaN or infinite (a bool, a string, NaN, inf, zero, a negative number
    and a whole number too large for a float, such as 10**400, give False; math.isfinite alone raises OverflowError
    on that last one)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def validate_http_limits(
    *,
    max_connections: object,
    header: object,
    body: object,
    busy_write: object,
    retry_after: object,
    max_threads: object = MAX_HANDLER_THREADS,
    quiet_drain: object = QUIET_DRAIN_SECONDS,
    drain_total: object = DRAIN_TOTAL_SECONDS,
    request_timeout: object = REQUEST_TIMEOUT_SECONDS,
    evict_min_idle: object = EVICT_MIN_IDLE_SECONDS,
    reply_write: object = REPLY_WRITE_TIMEOUT_SECONDS,
) -> None:
    """Refuse a nonsense HTTP limit with a clear message (a wrong value would otherwise show up as a hung or
    always-busy server). Runs at import on the real values; tests call it with bad ones."""
    if type(max_connections) is not int or not MAX_CONNECTIONS_MIN <= max_connections <= MAX_CONNECTIONS_MAX:
        raise ValueError(
            f"MAX_CONNECTIONS must be a whole number from {MAX_CONNECTIONS_MIN} to {MAX_CONNECTIONS_MAX}, "
            f"got {max_connections!r}"
        )
    if (
        type(max_threads) is not int
        or not MAX_HANDLER_THREADS_MIN <= max_threads <= MAX_HANDLER_THREADS_MAX
        or max_threads < max_connections
    ):
        raise ValueError(
            f"MAX_HANDLER_THREADS must be a whole number from {MAX_HANDLER_THREADS_MIN} to {MAX_HANDLER_THREADS_MAX} "
            f"and not below MAX_CONNECTIONS, got {max_threads!r}"
        )
    for name, value in (
        ("HEADER_READ_DEADLINE_SECONDS", header),
        ("BODY_READ_DEADLINE_SECONDS", body),
        ("BUSY_WRITE_TIMEOUT_SECONDS", busy_write),
        ("QUIET_DRAIN_SECONDS", quiet_drain),
        ("DRAIN_TOTAL_SECONDS", drain_total),
        ("REQUEST_TIMEOUT_SECONDS", request_timeout),
        ("EVICT_MIN_IDLE_SECONDS", evict_min_idle),
        ("REPLY_WRITE_TIMEOUT_SECONDS", reply_write),
    ):
        if not is_positive_seconds(value):
            raise ValueError(f"{name} must be a number of seconds above 0, got {value!r}")
    # (positive finite numbers, checked above, so these comparisons are safe)
    if header > request_timeout or body > request_timeout:  # type: ignore[operator]
        raise ValueError(
            "HEADER_READ_DEADLINE_SECONDS and BODY_READ_DEADLINE_SECONDS must not exceed REQUEST_TIMEOUT_SECONDS"
        )
    if evict_min_idle > header or evict_min_idle > body:  # type: ignore[operator]
        raise ValueError(
            "EVICT_MIN_IDLE_SECONDS must not exceed HEADER_READ_DEADLINE_SECONDS or BODY_READ_DEADLINE_SECONDS "
            "(a stalled connection would end by its own deadline before it could ever be closed for room)"
        )
    if reply_write > request_timeout:  # type: ignore[operator]
        raise ValueError("REPLY_WRITE_TIMEOUT_SECONDS must not exceed REQUEST_TIMEOUT_SECONDS")
    if quiet_drain > drain_total:  # type: ignore[operator]
        raise ValueError("QUIET_DRAIN_SECONDS must not exceed DRAIN_TOTAL_SECONDS")
    if type(retry_after) is not int or retry_after < 1:
        raise ValueError(f"BUSY_RETRY_AFTER_SECONDS must be a whole number, 1 or more, got {retry_after!r}")


def validate_model_budget(
    budget: object, margin: object, grace_floor: object, write_deadline: object, busy_wait: object
) -> None:
    """The model step is stamped again after the model answers and before its label is written (Desk._label_step).
    So two things must hold, or a call within budget could still be overtaken by the reaper:
    (1) budget + margin < the smallest stuck grace there can be (the busy waits alone; the send timeout only makes
        the real grace longer); an equal sum is refused too, because the clocks count whole seconds, and
    (2) the margin covers the longest write of that stamp: `write_deadline` plus one more busy wait (a lock held by
        another process makes SQLite wait once more after the desk's own deadline). The label write that follows the
        stamp starts a new grace and takes the same span, which then also fits (it is below the margin, and the
        margin is below the grace by (1))."""
    for name, value in (
        ("MODEL_CALL_BUDGET_SECONDS", budget),
        ("MODEL_GRACE_MARGIN_SECONDS", margin),
        ("the smallest stuck grace", grace_floor),
        ("the write deadline", write_deadline),
        ("the busy wait", busy_wait),
    ):
        if not is_positive_seconds(value):
            raise ValueError(f"{name} must be a number of seconds above 0, got {value!r}")
    if budget + margin >= grace_floor:  # type: ignore[operator]
        raise ValueError(
            "MODEL_CALL_BUDGET_SECONDS plus MODEL_GRACE_MARGIN_SECONDS must be below the stuck grace "
            f"({grace_floor!r} s at least), got {budget!r} + {margin!r}"
        )
    write_span = write_deadline + busy_wait  # type: ignore[operator]
    if margin < write_span:
        raise ValueError(
            "MODEL_GRACE_MARGIN_SECONDS must cover the write of the stamp after the model answer "
            f"(the write deadline plus one busy wait, {write_span!r} s), got {margin!r}"
        )


def validate_worker_caps(drive_places: object, caps: object) -> None:
    """The caps of the abandoned-worker count (ADAPTER_WORKER_CAPS): each is a whole number of 1 or more, and the CRM
    and sender caps are not below the drive places, so a healthy burst of drives (every place asking the CRM or the
    sender at once) is never turned away by a cap that only abandoned workers should reach."""
    if type(drive_places) is not int or not isinstance(caps, tuple):
        raise ValueError("drive places must be a whole number and the caps a tuple")
    table = dict(caps)
    for kind in ("model", "crm", "send", "refresh"):
        n = table.get(kind)
        if type(n) is not int or n < 1:
            raise ValueError(f"the worker cap of {kind} must be a whole number, 1 or more, got {n!r}")
    for kind in ("crm", "send"):
        if table[kind] < drive_places:
            raise ValueError(f"the worker cap of {kind} must not be below the drive places ({drive_places})")


def validate_descriptor_budget(per_thread: object, reserve: object, items: object, portal_inflight: object) -> None:
    """The open-file arithmetic of ensure_descriptor_budget: a handler thread holds at least its connection socket,
    its database file and -wal file and one adapter socket (4); the reserve covers every item listed in
    FD_RESERVE_ITEMS; and the portal item matches PORTAL_MAX_INFLIGHT (2 files each)."""
    if type(per_thread) is not int or per_thread < FDS_PER_THREAD_MINIMUM:
        raise ValueError(
            f"FDS_PER_HANDLER_THREAD must be a whole number of at least {FDS_PER_THREAD_MINIMUM} (connection socket, "
            f"database file, -wal file, one adapter socket), got {per_thread!r}"
        )
    if type(reserve) is not int or not isinstance(items, tuple):
        raise ValueError("FD_RESERVE must be a whole number and FD_RESERVE_ITEMS a tuple")
    needed = sum(int(n) for _, n in items)
    if reserve < needed:
        raise ValueError(f"FD_RESERVE is {reserve}, but the items it must cover add up to {needed}")
    if type(portal_inflight) is not int or portal_inflight * 2 > dict(items).get(FD_PORTAL_ITEM, 0):
        raise ValueError("FD_RESERVE_ITEMS must cover 2 open files for each of PORTAL_MAX_INFLIGHT portal requests")


validate_descriptor_budget(FDS_PER_HANDLER_THREAD, FD_RESERVE, FD_RESERVE_ITEMS, PORTAL_MAX_INFLIGHT)
validate_http_limits(
    max_connections=MAX_CONNECTIONS,
    header=HEADER_READ_DEADLINE_SECONDS,
    body=BODY_READ_DEADLINE_SECONDS,
    busy_write=BUSY_WRITE_TIMEOUT_SECONDS,
    retry_after=BUSY_RETRY_AFTER_SECONDS,
    max_threads=MAX_HANDLER_THREADS,
    quiet_drain=QUIET_DRAIN_SECONDS,
    drain_total=DRAIN_TOTAL_SECONDS,
    request_timeout=REQUEST_TIMEOUT_SECONDS,
    evict_min_idle=EVICT_MIN_IDLE_SECONDS,
    reply_write=REPLY_WRITE_TIMEOUT_SECONDS,
)
