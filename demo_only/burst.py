"""Burst script (described in contract.md, paragraph "Burst script"). Demo-only code.
Run: python -m demo_only.burst --url http://127.0.0.1:8765/leads --n 10 --email ada@example.com
     --ref-prefix run1 --source form [--expect N] [--wrap-n8n]
Output: SENT n=10 / STATUS 201=1 200=9 / RETRIES busy503=0 refused=0 / RESULT sent=10 expected=10 (or MISMATCH ...)."""

from __future__ import annotations

import argparse
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from typing import Any

from lead_desk.constants import (
    BURST_BACKOFF_MAX_SECONDS,
    BURST_BACKOFF_SECONDS,
    BURST_RETRY_DEADLINE_SECONDS,
    HTTP_ACCEPTED,
    HTTP_BUSY,
    HTTP_CREATED,
    HTTP_OK,
    SOURCES,
)

REQUEST_TIMEOUT_SECONDS = 60.0
NAME = "Ada Example"


def build_body(source: str, ref: str, email: str, wrap_n8n: bool) -> bytes:
    """The desk envelope, or (wrap_n8n) the n8n input shape."""
    if not wrap_n8n:
        payload = {"name": NAME, "email": email, "phone": "555 0100", "message": "Please call me."}
        return json.dumps({"source": source, "source_ref": ref, "payload": payload}).encode()
    if source == "form":
        form = {"submission_id": ref, "name": NAME, "email": email, "phone": "555 0100", "message": "Please call me."}
        return json.dumps(form).encode()
    hook = {"message_id": ref, "from_name": NAME, "from_email": email, "subject": "Quote", "body": "Please call me."}
    return json.dumps(hook).encode()


def retry_after_seconds(value: str | None) -> float | None:
    """The Retry-After header as seconds ("1"); an HTTP date, a negative number or junk gives None."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None


def next_wait(wait: float, retry_after: float | None, left: float) -> float:
    """Seconds to sleep before the next try: the doubling back-off, or the desk's Retry-After if that is longer, never
    more than BURST_BACKOFF_MAX_SECONDS and never past the deadline (`left` seconds remain)."""
    asked = wait if retry_after is None else max(wait, retry_after)
    return max(0.0, min(asked, BURST_BACKOFF_MAX_SECONDS, left))


def post_with_retries(
    post_once: Callable[[], tuple[int | str, float | None, Any]],
    *,
    deadline: float | None = None,
    max_retries: int | None = None,
    stop: threading.Event | None = None,
    on_retryable: Callable[[int | str], None] | None = None,
) -> tuple[int | str, Any]:
    """The retry rule of the burst script and of the portal demo (the same rule as load_tools.load_driver): a 503 or a
    refused connection is tried again by TIME, not by count. The wait starts at BURST_BACKOFF_SECONDS, doubles, never
    exceeds BURST_BACKOFF_MAX_SECONDS and honours Retry-After up to that maximum; no new try starts more than `deadline`
    seconds (default BURST_RETRY_DEADLINE_SECONDS) after the first, measured on the monotonic clock. `max_retries`
    (default None: only the deadline limits) caps the number of tries after the first; 0 means no retry at all.
    `stop` ends the loop early. `post_once` returns (status or word, Retry-After seconds or None, reply).
    Returns the last (status or word, reply). `on_retryable` is called for every 503 or refused answer, the last too."""
    limit = BURST_RETRY_DEADLINE_SECONDS if deadline is None else deadline
    first = time.monotonic()
    wait = BURST_BACKOFF_SECONDS
    retries = 0
    while True:
        res, retry_after, reply = post_once()
        if not (res == HTTP_BUSY or res == "refused"):
            return res, reply
        if on_retryable is not None:
            on_retryable(res)
        left = limit - (time.monotonic() - first)
        if left <= 0 or (max_retries is not None and retries >= max_retries) or (stop is not None and stop.is_set()):
            return res, reply
        retries += 1
        time.sleep(next_wait(wait, retry_after, left))
        wait = min(wait * 2, BURST_BACKOFF_MAX_SECONDS)


def _post_once(url: str, body: bytes) -> tuple[int | str, float | None, None]:
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})  # noqa: S310 (url from the caller)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT_SECONDS) as r:  # noqa: S310
            return int(r.status), None, None
    except urllib.error.HTTPError as e:
        return int(e.code), retry_after_seconds(e.headers.get("Retry-After")), None
    except urllib.error.URLError as e:
        return "refused" if isinstance(e.reason, ConnectionRefusedError) else type(e.reason).__name__, None, None
    except (OSError, http.client.HTTPException) as e:  # includes a reply cut off or a bad status line
        return type(e).__name__, None, None


def fire(
    url: str, n: int, email: str, ref_prefix: str, source: str, wrap_n8n: bool = False, max_retries: int | None = None
) -> dict[str, object]:
    """n requests at the same moment (start barrier). A 503 or a refused connect is retried by time, see
    post_with_retries (`max_retries=0`: no retry at all). Returns final statuses and retry counts."""
    barrier = threading.Barrier(n)
    final: list[int | str] = []
    stats = {"busy503": 0, "refused": 0}
    lock = threading.Lock()

    def count(res: int | str) -> None:
        with lock:
            stats["busy503" if res == HTTP_BUSY else "refused"] += 1

    def one(i: int) -> None:
        body = build_body(source, f"{ref_prefix}-{i}", email, wrap_n8n)
        barrier.wait()
        res, _ = post_with_retries(lambda: _post_once(url, body), max_retries=max_retries, on_retryable=count)
        with lock:
            final.append(res)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(1, n + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {"n": n, "final": final, **stats}


def main(argv: list[str] | None = None) -> int:
    """Post n requests, print SENT / STATUS / RETRIES / RESULT lines, return the exit code."""
    ap = argparse.ArgumentParser(prog="demo_only.burst")
    ap.add_argument("--url", required=True)
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--ref-prefix", required=True)
    ap.add_argument("--source", choices=SOURCES, required=True)
    ap.add_argument("--expect", type=int)
    ap.add_argument("--wrap-n8n", action="store_true")
    a = ap.parse_args(argv)
    res = fire(a.url, a.n, a.email, a.ref_prefix, a.source, a.wrap_n8n)
    final: list[int | str] = list(res["final"])  # type: ignore[call-overload]
    counts = Counter(final)
    ok_count = sum(v for k, v in counts.items() if k in (HTTP_OK, HTTP_CREATED, HTTP_ACCEPTED))
    expected = a.n if a.expect is None else a.expect
    shown = " ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: str(kv[0]), reverse=True))
    print(f"SENT n={a.n}")
    print(f"STATUS {shown}")
    print(f"RETRIES busy503={res['busy503']} refused={res['refused']}")
    if ok_count == expected:
        print(f"RESULT sent={ok_count} expected={expected}")
        return 0
    print(f"MISMATCH sent={ok_count} expected={expected}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
