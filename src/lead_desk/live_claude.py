"""The live AI step: one question to a model over HTTPS, with a spend cap. Standard library only.

What it does: `LiveQualifier.qualify(lead_id, email_key)` asks the model for one word (hot, warm or cold).
The model sees ONLY the lead id and the email address (the desk's contract); this proves the plumbing (valid answer,
failures, cost), not the quality of the answer.

Rules:
* one call = one TOTAL time limit (settings.timeout_seconds, default 8 s, at most the desk's AI call budget of 9 s),
  counted from the start of `qualify`: waiting for the spend ledger counts, and the network request is not started at
  all when the limit is already used up (the reserve is then settled at 0). The request runs in a helper thread and the
  caller waits for it at most the time left. A call that is not finished then raises LiveTransport; its helper thread
  ends on its own (a socket timeout is per operation, so it can outlive the limit, and the thread of `qualify`
  itself can still be waiting on the ledger lock for a few seconds) and its late answer is dropped. The provider
  may still have charged a request that was sent: the worst-case reserve stays;
* NO retry in here: a failure raises a classed error and the desk retries later or hands the lead to a person;
* a 429 raises LiveRateLimited with the Retry-After seconds (the desk does not wait for them; they are only reported);
* the key is read from a file path in the settings. It is never an argument and never printed. Every error of this
  module carries a fixed text only (never part of the reply, never the key, never the key file path);
* spend: before each call the WORST case cost (every byte of the question counted as a token, max_tokens answer
  tokens) is reserved in a locked ledger file; the call is refused when the per-run or the plan total cap would be
  passed. After the call the reserve is replaced by the cost the model reports. A call that fails or crashes keeps its
  worst-case reserve (the provider may have charged it). This includes a 429 or a 5xx answer: the worst-case reserve
  stays, because the provider does not say whether it charged. The ledger survives restarts.
* if the cost cannot be recorded after a paid answer (ledger locked, or token counts that make no sense), the answer
  is still returned and the worst-case reserve stays as the recorded cost.
The base address and every price come from settings; there is no price or model name in this file."""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from decimal import ROUND_CEILING, Context, Decimal, localcontext
from pathlib import Path
from typing import IO, Any, Literal

from lead_desk.config import LiveSettings
from lead_desk.constants import (
    AI_LABELS,
    HTTP_TOO_MANY_REQUESTS,
    LIVE_ERROR_BODY_MAX,
    LIVE_ERROR_TYPE_MAX,
    LIVE_KEY_MIN_CHARS,
    LIVE_LEDGER_FILE_MAX_BYTES,
    LIVE_LEDGER_LOCK_TRIES,
    LIVE_LEDGER_LOCK_WAIT,
    LIVE_LEDGER_TAIL_WINDOW,
    LIVE_LEDGER_USD_MAX,
    LIVE_PER_MILLION,
    LIVE_REPLY_MAX_BYTES,
    LIVE_REPORTED_TOKENS_MAX,
)

SYSTEM_TEXT = (
    "You sort sales leads. Answer with exactly one word: hot, warm or cold. No other text, no punctuation. "
    "The lead is given only by an id and an email address."
)


class LiveError(RuntimeError):
    """Base of every error of the live step. The text is fixed (never the reply, never the key)."""


class LiveCapReached(LiveError):
    """A call would pass the per-run or the total spend cap. Nothing was sent."""


class LiveRateLimited(LiveError):
    """The provider answered 429. `retry_after` is the Retry-After seconds it named (None if it named none)."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__("the model service said: too many requests")
        self.retry_after = retry_after


class LiveTransport(LiveError):
    """No usable answer: time limit, connection error, 5xx, or another 4xx."""


class LiveRefused(LiveError):
    """The model declined to answer (a refusal stop reason)."""


class LiveBadReply(LiveError):
    """The reply was not JSON of the expected shape, or its text was not hot, warm or cold."""


MONEY_CONTEXT = Context(
    prec=60, rounding=ROUND_CEILING, Emax=999, Emin=-999
)  # money sums: own precision, rounds UP, no global state


def worst_case_usd(s: LiveSettings, prompt_bytes: int) -> Decimal:
    """Upper bound of one call: every prompt byte counted as one input token (a token is at least one byte) plus
    max_tokens output tokens."""
    with localcontext(MONEY_CONTEXT):
        return (s.price_in_per_mtok * prompt_bytes + s.price_out_per_mtok * s.max_tokens) / LIVE_PER_MILLION


def actual_usd(s: LiveSettings, in_tokens: int, out_tokens: int) -> Decimal:
    """Cost from the token counts the provider reports. They are trusted by design: a low count lowers the settled
    cost, and until then the worst-case reserve is what the caps count."""
    with localcontext(MONEY_CONTEXT):
        return (s.price_in_per_mtok * in_tokens + s.price_out_per_mtok * out_tokens) / LIVE_PER_MILLION


_TOO_LARGE = "amount is too large"
_UNREADABLE = "the spend ledger is unreadable: check the ledger file or move it away"
_TOO_BIG_FILE = (
    "the spend ledger file is too large: move it away and start a new one whose first line is a carry row "
    "(a reserve line holding the total spent so far); without it the total cap starts again from zero"
)
_NUL_WS = b"\x00 \t\r\n\x0b\x0c"  # the same set bytes.strip() removes (plus NUL), so reader and writer agree


def _is_torn_tail(tail: bytes) -> bool:
    """The bytes after the last newline are what a crash leaves: nothing but NUL or white space (a file extended
    before its data reached the disk), or something that starts like a JSON object but is not a whole one. A cut can
    end in "}" inside free text, so "ends in }" proves nothing: the tail must parse. Judged on bytes (a cut may fall
    inside a multi-byte character); the reader and the writer both call this."""
    if not tail.strip(_NUL_WS) or b"\x00" in tail:  # a NUL inside is never part of a row the desk wrote
        return True
    if not tail.startswith(b"{"):
        return False  # not the shape of a cut-off row: kept, the reader fails closed on it
    try:
        json.loads(tail.decode("utf-8"))
    except RecursionError:
        return False  # too deeply nested to be a plain cut: keep it, the reader then fails closed
    except ValueError:  # includes a UTF-8 cut inside a character
        return True
    return False


def _checked_usd(v: object) -> Decimal:
    """A ledger amount: a string that is a finite, non-negative number within the sanity bound; else ValueError."""
    if not isinstance(v, str):
        raise ValueError("amount is not a string")
    d = Decimal(v)  # InvalidOperation is an ArithmeticError: the callers treat it like ValueError
    if not d.is_finite() or d < 0:
        raise ValueError("amount is not a finite non-negative number")
    if d > Decimal(LIVE_LEDGER_USD_MAX):
        raise ValueError(_TOO_LARGE)
    return d


class _FileLock:
    """Blocking exclusive lock on one byte of a lock file (other processes too). Windows: msvcrt, Linux: flock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._f: IO[bytes] | None = None

    def __enter__(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            f = open(self.path, "a+b")  # noqa: SIM115
        except OSError as e:
            raise LiveCapReached("the spend ledger lock file cannot be opened: check the ledger folder") from e
        for _ in range(LIVE_LEDGER_LOCK_TRIES):
            try:
                if sys.platform == "win32":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._f = f
                return
            except OSError:
                time.sleep(LIVE_LEDGER_LOCK_WAIT)
        f.close()
        raise LiveCapReached("the spend ledger is locked by another process")  # refuse to spend without the ledger

    def __exit__(self, *_: object) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # closing the file below releases the lock; the work inside the lock is already done
        finally:
            try:
                f.close()
            except OSError:
                pass


def _writable(usd: object, *, allow_zero: bool) -> None:
    """The writer refuses what the reader would refuse, so a bad amount never reaches the file."""
    if not isinstance(usd, Decimal):
        raise LiveCapReached("the spend amount is not a Decimal number")
    try:
        d = _checked_usd(str(usd))
    except (ValueError, ArithmeticError) as e:
        raise LiveCapReached(
            "the spend amount is too large" if str(e) == _TOO_LARGE else "the spend amount is not a valid number"
        ) from e
    if d == 0 and not allow_zero:
        raise LiveCapReached("a spend reserve of zero is refused")


class SpendLedger:
    """Append-only JSON lines in `path` (kept across restarts), guarded by `<path>.lock`.
    Lines: {"op":"reserve","id","run","t","model","usd","price_date"} and {"op":"settle","id","usd"}.
    A reserve without a settle counts at its reserved (worst-case) amount. A last line cut off by a crash (no
    newline after it, and it is white space or NULs only, or starts with "{" and has no closing "}") is ignored and
    the next append drops it. Anything else unreadable, including a row that is not a reserve or settle object,
    stops spending."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock_path = Path(f"{self.path}.lock")
        self._mutex = threading.Lock()
        self._n = 0

    def _totals(self, run: str) -> tuple[Decimal, Decimal]:
        reserved: dict[str, tuple[str, Decimal]] = {}
        settled: dict[str, Decimal] = {}
        try:
            try:
                with open(self.path, "rb") as f:
                    raw = f.read(LIVE_LEDGER_FILE_MAX_BYTES + 1)  # one read; never more than the cap plus one byte
            except FileNotFoundError:
                raw = b""
            if len(raw) > LIVE_LEDGER_FILE_MAX_BYTES:
                raise LiveCapReached(_TOO_BIG_FILE)
            return self._sum(raw, run, reserved, settled)
        except LiveCapReached:
            raise
        except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError, OSError, MemoryError) as e:
            raise LiveCapReached(_UNREADABLE) from e  # a broken ledger must stop spending, not reset it

    @staticmethod
    def _sum(
        raw: bytes, run: str, reserved: dict[str, tuple[str, Decimal]], settled: dict[str, Decimal]
    ) -> tuple[Decimal, Decimal]:
        lines = raw.split(b"\n")  # bytes, split like the writer does (not splitlines, not decoded first)
        ends_clean = raw.endswith(b"\n")
        if ends_clean:
            lines.pop()
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            if i == len(lines) - 1 and not ends_clean and _is_torn_tail(ln):
                continue  # the LAST line cut off by a crash (nothing was sent for it); a damaged middle line fails
            row = json.loads(ln.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("a ledger row is not an object")  # ignoring it could lower the counted spend
            if not isinstance(row.get("id"), str):
                raise ValueError("a ledger row has no text id")
            if row["op"] == "reserve":
                if not isinstance(row["run"], str):
                    raise ValueError("a reserve row has no text run id")  # it would count in the total only
                if row["id"] in reserved:
                    raise ValueError("duplicate reserve id")  # an overwrite could lower the total
                reserved[row["id"]] = (row["run"], _checked_usd(row.get("usd")))
            elif row["op"] == "settle":
                amount = _checked_usd(row.get("usd"))
                settled[row["id"]] = max(amount, settled.get(row["id"], amount))  # a double settle counts the larger
            else:
                raise ValueError("unknown ledger op")
        total = run_total = Decimal(0)
        with localcontext(MONEY_CONTEXT):
            for rid, (r, usd) in reserved.items():
                amount = settled.get(rid, usd)
                total += amount
                if r == run:
                    run_total += amount
        return run_total, total

    def _append(self, row: dict[str, str]) -> None:
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        try:
            with open(self.path, "a+b") as f:
                size = f.seek(0, 2)
                if size > LIVE_LEDGER_FILE_MAX_BYTES:
                    raise LiveCapReached(_TOO_BIG_FILE)  # reserve and settle both stop at the same size
                if size:
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":  # rare: the last write was cut off; only the end of the file is read
                        win = LIVE_LEDGER_TAIL_WINDOW
                        while True:  # read back from the end until a newline is in view (the size gate bounds this)
                            start = max(0, size - win)
                            f.seek(start)
                            window = f.read()
                            cut_in = window.rfind(b"\n") + 1
                            if cut_in or start == 0:
                                break
                            win *= 2
                        if _is_torn_tail(window[cut_in:]):
                            f.truncate(start + cut_in)  # a write cut off by a crash: nothing was sent for it
                        else:
                            line = b"\n" + line  # not a plain cut-off line: keep it, the reader will fail closed
                f.seek(0, 2)
                f.write(line)
                f.flush()
                os.fsync(f.fileno())  # a reserve must survive a power cut: it is what the cap counts
        except (OSError, MemoryError) as e:
            raise LiveCapReached("the spend ledger cannot be written: check the disk and the ledger file") from e

    def reserve(self, s: LiveSettings, usd: Decimal, wait_seconds: float | None = None) -> str:
        """Check both caps against `usd` and record the reserve; LiveCapReached (nothing recorded) when passed or when
        the in-process lock is not free within `wait_seconds` (None = wait without limit)."""
        if not self._mutex.acquire(timeout=-1 if wait_seconds is None else max(0.0, wait_seconds)):
            raise LiveCapReached("the spend ledger is busy")
        try:
            return self._reserve_locked(s, usd)
        finally:
            self._mutex.release()

    def _reserve_locked(self, s: LiveSettings, usd: Decimal) -> str:
        _writable(usd, allow_zero=False)
        with _FileLock(self._lock_path):
            run_total, total = self._totals(s.run_id)
            with localcontext(MONEY_CONTEXT):
                over = run_total + usd > s.cap_run_usd or total + usd > s.cap_total_usd
            if over:
                raise LiveCapReached("the spend cap would be passed")
            self._n += 1
            try:
                rid = f"{s.run_id}-{os.getpid()}-{os.urandom(8).hex()}-{self._n}"  # random: unique across instances
            except OSError as e:
                raise LiveCapReached("no random source for a unique spend id") from e
            self._append(
                {
                    "op": "reserve",
                    "id": rid,
                    "run": s.run_id,
                    "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),  # wall-clock: calendar stamp
                    "model": s.model,
                    "usd": str(usd),
                    "price_date": s.price_date,
                }
            )
            return rid

    def settle(self, rid: str, usd: Decimal) -> None:
        """Record the real cost. Zero is allowed (nothing was sent). A second settle of one id counts the larger."""
        _writable(usd, allow_zero=True)
        with self._mutex, _FileLock(self._lock_path):
            self._append({"op": "settle", "id": rid, "usd": str(usd)})

    def read_totals(self, run: str) -> tuple[Decimal, Decimal]:
        """Like totals() but takes no lock and creates no file or folder (for read-only views). Not for the cap path.
        A view must catch LiveCapReached (unreadable, too large, or a line caught mid-write) and OSError. A writer that
        truncates a cut-off tail at the same moment can make this read fail once: read again."""
        return self._totals(run)

    def totals(self, run: str) -> tuple[Decimal, Decimal]:
        """(this run, all runs) in US$."""
        with self._mutex, _FileLock(self._lock_path):
            return self._totals(run)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect could move the key to another host


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _retry_after(raw: str | None) -> float | None:
    """Seconds from a retry-after header; None for a missing, negative, non-number (a date) or non-finite value."""
    try:
        v = float(raw) if raw is not None else None
    except ValueError:
        return None
    return v if v is not None and 0 <= v < float("inf") else None


_TYPE_NAME = re.compile(rf"[a-z_]{{1,{LIVE_ERROR_TYPE_MAX}}}")


def error_type(raw: bytes) -> str | None:
    """The `error.type` of an error body ({"type":"error","error":{"type":...,"message":...}}), or None for anything
    else (not JSON, other shape, odd text). Only a short lower-case name is taken, never the message."""
    try:
        obj = json.loads(raw[:LIVE_ERROR_BODY_MAX].decode("utf-8"))
        t = obj["error"]["type"]
    except (ValueError, KeyError, TypeError):
        return None
    return t if isinstance(t, str) and _TYPE_NAME.fullmatch(t) else None


class LiveQualifier:
    """Implements contract.Qualifier on a live model. `mode` = "live" (the portal reads it). `calls` counts every
    call that reached the network step (a counter for callers)."""

    mode = "live"

    def __init__(self, settings: LiveSettings, ledger: SpendLedger | None = None) -> None:
        self.s = settings
        self.ledger = ledger or SpendLedger(settings.ledger_path)
        self.calls = 0
        self._key = ""  # read at the first call, kept in memory only

    @property
    def timeout_seconds(self) -> float:
        """The total time limit of one call, public so the desk can check it against its AI call budget when it is
        built (a limit above MODEL_CALL_BUDGET_SECONDS is refused: the desk would cut the call first)."""
        return self.s.timeout_seconds

    def _read_key(self) -> str:
        if not self._key:
            try:
                key = self.s.key_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise LiveTransport("the key file cannot be read") from None  # the error holds the path
            if len(key) < LIVE_KEY_MIN_CHARS:
                raise LiveTransport("the key file holds no key")
            self._key = key
        return self._key

    def _post(self, body: bytes, key: str, timeout: float | None = None) -> dict[str, Any]:
        """The request in a helper thread, joined for at most `timeout` (default `timeout_seconds`) in total (DNS,
        connect, TLS, headers and body together)."""
        timeout = self.s.timeout_seconds if timeout is None else timeout
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["reply"] = self._post_once(body, key, timeout)
            except BaseException as e:  # noqa: BLE001 (handed to the caller)
                box["error"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise LiveTransport("the model did not answer in time")
        if "error" in box:
            raise box["error"]
        reply: dict[str, Any] = box["reply"]
        return reply

    def _post_once(self, body: bytes, key: str, timeout: float) -> dict[str, Any]:
        s = self.s
        req = urllib.request.Request(  # noqa: S310  # nosec B310  (scheme checked in LiveSettings: https, or loopback http)
            s.base_url.rstrip("/") + "/v1/messages",
            data=body,
            method="POST",
            headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": s.api_version},
        )
        deadline = time.monotonic() + timeout
        try:
            with _OPENER.open(req, timeout=timeout) as r:  # noqa: S310  # nosec B310
                raw = b""
                while len(raw) <= LIVE_REPLY_MAX_BYTES:
                    if time.monotonic() > deadline:
                        raise LiveTransport("the model did not answer in time")
                    chunk = r.read1(4096)  # returns what has arrived; read(n) would wait for n bytes past the deadline
                    if not chunk:
                        break
                    raw += chunk
        except urllib.error.HTTPError as e:
            if e.code == HTTP_TOO_MANY_REQUESTS:
                raise LiveRateLimited(_retry_after(e.headers.get("Retry-After"))) from None
            try:
                kind = error_type(e.read(LIVE_ERROR_BODY_MAX))
            except (OSError, http.client.HTTPException):
                kind = None
            raise LiveTransport(f"the model service answered {e.code}" + (f" ({kind})" if kind else "")) from None
        except (urllib.error.URLError, OSError, socket.timeout, ValueError, http.client.HTTPException):
            raise LiveTransport("the model service could not be reached in time") from None
        if len(raw) > LIVE_REPLY_MAX_BYTES:
            raise LiveBadReply("the reply is too long")
        try:
            reply = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise LiveBadReply("the reply is not JSON") from None
        if not isinstance(reply, dict):
            raise LiveBadReply("the reply is not a JSON object")
        return reply

    def qualify(self, lead_id: str, email_key: str) -> Literal["hot", "warm", "cold"]:
        s = self.s
        key = self._read_key()
        question = f"lead id: {lead_id}\nemail: {email_key}\nAnswer: hot, warm or cold?"
        body = json.dumps(
            {
                "model": s.model,
                "max_tokens": s.max_tokens,
                "system": SYSTEM_TEXT,
                "output_config": {"effort": s.effort},  # no temperature, top_p, top_k, tools or tool_choice
                "messages": [{"role": "user", "content": question}],
            }
        ).encode("utf-8")
        started = time.monotonic()
        rid = self.ledger.reserve(s, worst_case_usd(s, len(body)), s.timeout_seconds)  # worst case, BEFORE the network
        left = s.timeout_seconds - (time.monotonic() - started)
        if left <= 0:  # the ledger wait used up the limit: nothing is sent, so nothing can be charged
            try:
                self.ledger.settle(rid, Decimal(0))
            except (LiveCapReached, OSError):
                pass  # the reserve then stays at the worst case: safe direction
            raise LiveTransport("the model did not answer in time")
        self.calls += 1
        reply = self._post(body, key, left)  # LiveError subclasses carry fixed text only; the worst-case reserve stays
        usage = reply.get("usage")
        if isinstance(usage, dict):
            tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
            if (
                type(tin) is int
                and type(tout) is int
                and 0 <= tin <= LIVE_REPORTED_TOKENS_MAX
                and 0 <= tout <= LIVE_REPORTED_TOKENS_MAX
            ):  # else the reserve stays
                try:
                    self.ledger.settle(rid, actual_usd(s, tin, tout))  # what the provider reports
                except (LiveCapReached, OSError, ArithmeticError):
                    pass  # best effort: the answer was paid for and is returned; the worst-case reserve stays
        if reply.get("stop_reason") == "refusal":  # first: a refusal has no usable content
            raise LiveRefused("the model declined to answer")
        content = reply.get("content")
        blocks = content if isinstance(content, list) else []
        texts = [
            b["text"]
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if not texts:  # thinking blocks and every other block type are ignored
            raise LiveBadReply("the reply has no text")
        label = "".join(texts).strip().strip(".").lower()
        if label not in AI_LABELS:
            raise LiveBadReply("the reply is not hot, warm or cold")
        return label  # type: ignore[return-value]
