"""Count the demo mails that arrived in a mailbox you own, by the header X-Send-Key. Standard library only
(imaplib over TLS, certificate checked). Prints nothing and returns only counts: no body, no address, no subject is
read (only the one header X-Send-Key is fetched, with PEEK so no message is marked as read).

The password is read from a file at every call (never an argument, never in an error text).
A mail provider's IMAP search on a header is a substring match (and may split a value into words), so the
search only finds the candidates and the exact key is compared here: a count for `L-001` never includes `L-0010`.
`counts_for_keys` searches for each FULL key (never a one-letter prefix) and, if ANY wanted key was found in no
folder that way, also fetches the
headers of each folder's newest messages (at most FALLBACK_SCAN_MAX) and compares them here; per folder and key the
LARGER of the two counts is kept (never fewer than either way saw, so a second delivery is not hidden).
A folder that does not exist on the server is left OUT of the result (not shown as empty), so the caller can say
"could not check this folder" (a provider may name its spam folder in the user's language).
The scan runs only when a key EXPECTED in this mailbox (`expected`, default: every wanted key) was found in no folder:
with two mailboxes, a key that belongs to the other mailbox is never a reason to scan.
One call is bounded as a whole: `total_seconds`, counted from before the connection is opened (checked between the
steps; one single step can still last up to `timeout_seconds` more). After the deadline the connection is closed without
a LOGOUT exchange (that would add two more waits). The connection ALWAYS checks the certificate chain and the host name:
no settings value can switch that off."""

from __future__ import annotations

import imaplib
import re
import ssl
import time
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from lead_desk.constants import MAIL_SPAM_FOLDER
from lead_desk.contract import SendRefused
from lead_desk.smtp_provider import read_secret_file

_PREFIX = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_HEADER = re.compile(rb"X-Send-Key:[\s]*([^\s]+)", re.IGNORECASE)
IMAP_SSL_PORT = 993
FALLBACK_SCAN_MAX = 300  # headers fetched by the fallback scan of one folder (the newest messages)
KEYS_MAX = 500
TOTAL_SECONDS_DEFAULT = 120.0  # one whole call (login, every search and fetch, every folder)


class ImapCountError(RuntimeError):
    """Fixed text only: never the server's words, never the password or its path."""


class ImapConnectionError(ImapCountError):
    """The connection failed while connecting or after a successful login (reset, refused, TLS or socket error,
    server hung up): worth one repeat. NOT this class, so never repeated: a failure during the login itself (a
    wrong password may look like a hang-up, and a repeat would be a second login attempt), a step that timed out, a
    failure after the total time limit, a missing folder, a bad key."""


class ImapTotalTimeout(ImapCountError):
    """The call ran past its total time limit (a caller that already holds a full earlier result may keep it)."""


@dataclass(frozen=True)
class ImapSettings:
    host: str
    user: str = field(repr=False)  # an address: kept out of every printout
    password_file: Path = field(repr=False)
    port: int = IMAP_SSL_PORT
    folders: tuple[str, ...] = ("INBOX", MAIL_SPAM_FOLDER)  # inbox and spam are counted apart
    timeout_seconds: float = 30.0  # one network step
    total_seconds: float = TOTAL_SECONDS_DEFAULT  # the whole call
    ssl_context: ssl.SSLContext | None = None  # None = the system authorities, chain and name checked


def bad_folder_name(folder: str) -> bool:
    """True for a folder name this module cannot send: not plain ASCII, a quote, a backslash or a control character
    (IMAP wants other characters in a special encoding; use the ASCII form of the name your provider shows)."""
    return not folder or not folder.isascii() or any(c in '"\\' or ord(c) < 32 for c in folder)  # noqa: PLR2004


def _password(s: ImapSettings) -> str:
    try:
        return read_secret_file(s.password_file, "mailbox password")
    except SendRefused as e:
        raise ImapCountError(str(e)) from None  # a fixed text that names no path


def _session(
    s: ImapSettings, folders_work: Callable[[imaplib.IMAP4_SSL], dict[str, dict[str, int]]]
) -> dict[str, dict[str, int]]:
    if any(bad_folder_name(f) for f in s.folders):
        raise ImapCountError("a mailbox folder name has characters this program cannot send (use plain ASCII letters)")
    password = _password(s)
    ctx = s.ssl_context or ssl.create_default_context()  # imaplib's own None default is an UNVERIFIED context
    deadline = time.monotonic() + s.total_seconds  # starts BEFORE the connection and the server greeting
    logging_in = False
    try:
        m = imaplib.IMAP4_SSL(s.host, s.port, ssl_context=ctx, timeout=s.timeout_seconds)
        m.deadline = deadline  # type: ignore[attr-defined]  # read by _tick
        try:
            _tick(m)
            logging_in = True
            m.login(s.user, password)
            logging_in = False
            return folders_work(m)
        finally:
            try:
                if time.monotonic() > m.deadline:
                    m.shutdown()  # past the deadline: no LOGOUT round trip, just close the socket
                else:
                    m.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    except UnicodeError:  # a user name or password the login cannot carry (non-ASCII)
        raise ImapCountError("the mailbox user or password has characters the mailbox login cannot carry") from None
    except (imaplib.IMAP4.abort, OSError) as e:
        if logging_in or isinstance(e, TimeoutError) or time.monotonic() > deadline:
            raise ImapCountError("the mailbox could not be read") from None  # not repeated: see ImapConnectionError
        raise ImapConnectionError("the mailbox could not be read") from None
    except imaplib.IMAP4.error:
        raise ImapCountError("the mailbox could not be read") from None


def counts_by_key(s: ImapSettings, prefix: str) -> dict[str, dict[str, int]]:
    """{folder: {send key: messages}} for every message whose X-Send-Key starts with `prefix`."""
    if not _PREFIX.fullmatch(prefix):
        raise ImapCountError("prefix has characters outside the allowed set")
    return _session(s, lambda m: {f: _count_folder(m, f, prefix) for f in s.folders if _select(m, f)})


def counts_for_keys(
    s: ImapSettings, keys: Collection[str], expected: Collection[str] | None = None, scan: bool = True
) -> dict[str, dict[str, int]]:
    """{folder: {send key: messages}} for exactly the given keys: one search per full key, then (only when some key
    was expected here and not found that way) a bounded scan of the newest headers; every candidate is compared here,
    exactly. `expected` = the keys that should be in THIS mailbox (default: all of `keys`). `scan=False` never runs
    the slow scan (a caller that polls many times asks for it on the first and the last poll only)."""
    wanted = set(keys)
    if not wanted or len(wanted) > KEYS_MAX or not all(_PREFIX.fullmatch(k) for k in wanted):
        raise ImapCountError("keys are empty, too many, or have characters outside the allowed set")
    return _session(s, lambda m: _count_keys(m, s.folders, wanted, wanted if expected is None else set(expected), scan))


def _tick(m: imaplib.IMAP4_SSL) -> None:
    """Called before every network step: past the total deadline the call ends with the fixed error text."""
    if time.monotonic() > getattr(m, "deadline", float("inf")):
        raise ImapTotalTimeout("the mailbox read took longer than its total time limit")


def _select(m: imaplib.IMAP4_SSL, folder: str) -> bool:
    _tick(m)
    typ, _ = m.select(f'"{folder}"', readonly=True)
    return typ == "OK"  # a folder that does not exist is left out of the result


def _key_of(m: imaplib.IMAP4_SSL, number: str) -> str | None:
    _tick(m)
    typ, parts = m.fetch(number, "(BODY.PEEK[HEADER.FIELDS (X-SEND-KEY)])")
    if typ != "OK" or not parts or not isinstance(parts[0], tuple):
        return None
    found = _HEADER.search(parts[0][1])
    return found.group(1).decode("ascii", "replace") if found else None


def _ids(m: imaplib.IMAP4_SSL, *criteria: str) -> list[str]:
    _tick(m)
    typ, data = m.search(None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return [i.decode("ascii") for i in data[0].split()]


def _search_keys(m: imaplib.IMAP4_SSL, wanted: set[str], scan: bool) -> dict[str, int]:
    """Messages per wanted key in the selected folder. scan=False: one search per full key. scan=True: the fallback,
    the headers of the folder's newest FALLBACK_SCAN_MAX messages compared here."""
    found: list[str] = []
    if scan:
        for i in _ids(m, "ALL")[-FALLBACK_SCAN_MAX:]:
            if (got := _key_of(m, i)) in wanted:
                found.append(str(got))
    else:
        seen: set[str] = set()
        for k in sorted(wanted):
            for i in _ids(m, "HEADER", "X-Send-Key", f'"{k}"'):
                if i not in seen:
                    seen.add(i)
                    if (got := _key_of(m, i)) in wanted:
                        found.append(str(got))
    return dict(Counter(found))


def _count_keys(
    m: imaplib.IMAP4_SSL, folders: Sequence[str], wanted: set[str], expected: set[str], scan: bool = True
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for f in folders:
        if _select(m, f):
            out[f] = _search_keys(m, wanted, scan=False)
    unseen = expected - {k for per in out.values() for k in per}  # an expected key seen nowhere: the search may split
    if scan and unseen:  # values
        for f in out:
            _select(m, f)
            for k, n in _search_keys(m, wanted, scan=True).items():
                out[f][k] = max(out[f].get(k, 0), n)
    return out


def _count_folder(m: imaplib.IMAP4_SSL, folder: str, prefix: str) -> dict[str, int]:
    tally: Counter[str] = Counter()
    for i in _ids(m, "HEADER", "X-Send-Key", f'"{prefix}"'):
        key = _key_of(m, i)
        if key is not None and key.startswith(prefix):
            tally[key] += 1
    return dict(tally)


def counts_by_inbox(
    inboxes: Sequence[ImapSettings],
    prefix: str,
    keys: Collection[str] | None = None,
    expected_by_inbox: Sequence[Collection[str]] | None = None,
    scan: bool = True,
) -> list[dict[str, dict[str, int]]]:
    """One result per configured mailbox, in the same order (one login each): `counts_for_keys` when `keys` is given
    (with `expected_by_inbox[n]` as mailbox n's expected keys when given; `scan` as in `counts_for_keys`), else
    `counts_by_key` by prefix. A failure names no mailbox detail: it stays the fixed ImapCountError text."""
    if keys is None:
        return [counts_by_key(s, prefix) for s in inboxes]
    return [
        counts_for_keys(s, keys, None if expected_by_inbox is None else expected_by_inbox[n], scan)
        for n, s in enumerate(inboxes)
    ]
