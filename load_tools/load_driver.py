"""Open-loop load driver for the desk's POST /leads. Standard library only. Formats: load_tools/README.md.

Run: python -m load_tools.load_driver --base-url http://127.0.0.1:8765 --scenario burst:200 --log req.jsonl
     scenarios: burst:N (N at one moment) | rate:R:D (R requests/s for D seconds) |
                dupmix:TOTAL:FRACTION (a share of the requests are repeats, some through both doors at once)
Open loop: each request has an INTENDED send time fixed before the run; a slow reply never moves a later request.
The actual send time and the lag are logged, so a driver that could not keep up shows in the log."""

from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing
import os
import platform
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

FALLBACK_RETRY_VALUES = (
    0.1,
    1.0,
    30.0,
)  # BURST_BACKOFF_SECONDS, BURST_BACKOFF_MAX_SECONDS, BURST_RETRY_DEADLINE_SECONDS
_backoff: float
_backoff_max: float
_deadline: float
try:  # one source of truth for the retry policy of the burst script (contract.md "Burst script")
    from lead_desk import constants as _constants

    _backoff = _constants.BURST_BACKOFF_SECONDS
    _backoff_max = _constants.BURST_BACKOFF_MAX_SECONDS
    _deadline = _constants.BURST_RETRY_DEADLINE_SECONDS
except ImportError:  # run without src on the path: same values (a test pins them to lead_desk.constants)
    _backoff, _backoff_max, _deadline = FALLBACK_RETRY_VALUES

# ---- config block ----
DOORS = ("form", "webhook")
BACKOFF_SECONDS = _backoff  # wait before the second try; doubles after each try
BACKOFF_MAX_SECONDS = _backoff_max  # the wait never grows past this
ATTEMPT_TIMEOUT_SECONDS = 20.0  # one try
# A patient sender (n8n, a mail relay) keeps trying while the desk restarts: tries are bounded by TIME, not by count.
REQUEST_DEADLINE_SECONDS = _deadline  # no new try after this long since the first try
START_LEAD_SECONDS = 1.5  # time to start the workers before the first intended send
DEFAULT_WORKERS = 4
DEFAULT_THREADS = 64
DUP_BOTH_DOORS_SHARE = 0.5  # of the repeats: this share go through the other door at the same moment
DUP_LATER_MAX_SECONDS = 2.0  # the other repeats come this much later at most
DUPMIX_DEFAULT_RATE = 200.0  # requests per second for dupmix
RETRY_CODES = (503,)  # plus every network error and timeout
ACK_CODES = (200, 201, 202)
ID_PREFIX = "ld"
EMAIL_DOMAIN = "example.com"
TCP_STATE_TIME_WAIT = "06"
MIN_TCP_LINE_FIELDS = 4
DUPMIX_RATE_INDEX = 2


def _retry_after(value: str | None) -> float | None:
    """The Retry-After header as seconds; an HTTP date, a negative number or junk gives None."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None


def next_wait(wait: float, retry_after: float | None, left: float) -> float:
    """Seconds to sleep before the next try: the doubling back-off, or the desk's Retry-After if longer, never more than
    BACKOFF_MAX_SECONDS and never past the deadline. (Same rule as demo_only.burst.next_wait; a test pins both.)"""
    asked = wait if retry_after is None else max(wait, retry_after)
    return max(0.0, min(asked, BACKOFF_MAX_SECONDS, left))


def _post(url: str, body: bytes) -> tuple[int | None, str | None, dict[str, Any], float | None]:
    """One try. Returns (code, error class name, parsed reply, Retry-After seconds). A 4xx/5xx reply is a code, not an
    error."""
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})  # noqa: S310 (loopback, from args)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def parse(raw: bytes) -> dict[str, Any]:
        try:
            obj = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    try:
        with opener.open(req, timeout=ATTEMPT_TIMEOUT_SECONDS) as r:  # noqa: S310
            return int(r.status), None, parse(r.read()), None
    except urllib.error.HTTPError as e:
        after = _retry_after(e.headers.get("Retry-After"))
        try:
            return int(e.code), None, parse(e.read()), after
        except (OSError, ValueError, http.client.HTTPException):  # the reply body broke off: the code still counts
            return int(e.code), None, {}, after
    except urllib.error.URLError as e:
        return None, type(e.reason).__name__, {}, None
    except (OSError, ValueError, http.client.HTTPException) as e:  # reset, timeout, half-read reply, bad status line
        return None, type(e).__name__, {}, None


def send_with_retries(url: str, item: dict[str, Any]) -> dict[str, Any]:
    """Send one intended request; retry 503 and network errors with doubling waits (BACKOFF_SECONDS up to
    BACKOFF_MAX_SECONDS; a Retry-After is honoured up to that maximum) until the reply is final or
    REQUEST_DEADLINE_SECONDS have passed since the first try.
    An answer other than 503 stops the retries (a 200/201/202 is never sent twice). A request still not answered when
    the deadline ends is logged `accepted: false`, `gave_up: "deadline"` with the last code or error as final_status.
    Returns the log line (format in README.md)."""
    body = json.dumps(item["envelope"]).encode()
    attempts: list[dict[str, Any]] = []
    first = time.monotonic()  # the deadline and the latencies use this one
    first_ts = time.time()  # wall-clock: stored as sent_ts, compared with the schedule's epoch seconds
    reply: dict[str, Any] = {}
    wait = BACKOFF_SECONDS
    after: float | None = None
    while True:
        t0 = time.time()  # wall-clock: stored in the log line
        t0_mono = time.monotonic()
        code, error, reply, after = _post(url, body)
        attempts.append({"ts": t0, "code": code, "error": error, "latency_s": round(time.monotonic() - t0_mono, 4)})
        if not (code in RETRY_CODES or code is None):
            break
        left = REQUEST_DEADLINE_SECONDS - (time.monotonic() - first)
        if left <= 0:
            break
        time.sleep(next_wait(wait, after, left))
        wait = min(wait * 2, BACKOFF_MAX_SECONDS)
    last = attempts[-1]
    final = last["code"] if last["code"] is not None else last["error"]
    accepted = last["code"] in ACK_CODES
    return {
        "request_id": item["request_id"],
        "door": item["door"],
        "intended_ts": item["intended_ts"],
        "sent_ts": first_ts,
        "lag_s": round(first_ts - item["intended_ts"], 4),  # replaced by the worker with the monotonic lag
        "done_ts": time.time(),  # wall-clock: calendar stamp
        "scenario": item["scenario"],
        "kind": item["kind"],
        "attempts": attempts,
        "attempt_count": len(attempts),
        "first_attempt_ts": attempts[0]["ts"],
        "last_attempt_ts": last["ts"],
        "codes": [a["code"] if a["code"] is not None else a["error"] for a in attempts],
        "final_status": final,
        "accepted": accepted,
        "gave_up": "deadline" if (last["code"] is None or last["code"] in RETRY_CODES) else None,
        "receipt_id": reply.get("receipt_id"),
        "verdict": reply.get("verdict"),
    }


def _driver_error_line(item: dict[str, Any], error: BaseException) -> dict[str, Any]:
    """A request the driver itself could not finish still gets its log line (check 1 then reports it lost)."""
    now = time.time()  # wall-clock: calendar stamp
    return {
        "request_id": item["request_id"],
        "door": item["door"],
        "intended_ts": item["intended_ts"],
        "sent_ts": now,
        "lag_s": round(now - item["intended_ts"], 4),
        "done_ts": now,
        "scenario": item["scenario"],
        "kind": item["kind"],
        "attempts": [],
        "attempt_count": 0,
        "first_attempt_ts": None,
        "last_attempt_ts": None,
        "codes": [],
        "final_status": f"driver:{type(error).__name__}",
        "accepted": False,
        "gave_up": "driver_error",
        "receipt_id": None,
        "verdict": None,
    }


def _envelope(door: str, ref: str, email: str) -> dict[str, Any]:
    payload = {"name": f"Load {ref}", "email": email, "phone": "555 0100", "message": "Please call me."}
    return {"source": door, "source_ref": ref, "payload": payload}


def _item(door: str, ref: str, ts: float, scenario: str, kind: str) -> dict[str, Any]:
    email = f"{ref}@{EMAIL_DOMAIN}"
    return {
        "request_id": ref,
        "door": door,
        "intended_ts": ts,
        "scenario": scenario,
        "kind": kind,
        "envelope": _envelope(door, ref, email),
    }


def parse_scenario(text: str) -> tuple[str, list[float]]:
    """burst:N | rate:R:D | dupmix:TOTAL:FRACTION[:RATE]. Returns (name, numbers); raises ValueError."""
    parts = text.split(":")
    name, nums = parts[0], [float(p) for p in parts[1:]]
    want = {"burst": (1, 1), "rate": (2, 2), "dupmix": (2, 3)}
    if name not in want or not want[name][0] <= len(nums) <= want[name][1]:
        raise ValueError(f"bad scenario {text!r}: use burst:N, rate:R:D or dupmix:TOTAL:FRACTION[:RATE]")
    positive = nums[:2] if name != "dupmix" else [nums[0], *nums[2:]]  # a dupmix fraction may be 0
    if any(x <= 0 for x in positive) or (name == "dupmix" and not 0 <= nums[1] < 1):
        raise ValueError(f"bad scenario {text!r}: numbers must be positive (a fraction is 0 to 1)")
    return name, nums


def build_schedule(scenario: str, t0: float, prefix: str, seed: int) -> list[dict[str, Any]]:
    """The whole open-loop schedule, fixed before anything is sent. Sorted by intended time."""
    name, nums = parse_scenario(scenario)
    rng = random.Random(seed)  # noqa: S311 (not for security: spreads made-up requests)
    items: list[dict[str, Any]] = []
    if name == "burst":
        for i in range(int(nums[0])):
            items.append(_item(DOORS[i % len(DOORS)], f"{prefix}-{i}", t0, scenario, "first"))
    elif name == "rate":
        rate, seconds = nums
        for i in range(int(rate * seconds)):
            items.append(_item(DOORS[i % len(DOORS)], f"{prefix}-{i}", t0 + i / rate, scenario, "first"))
    else:
        total = int(nums[0])
        repeats = min(round(total * nums[1]), total - 1)  # at least one first request for a repeat to repeat
        rate = nums[DUPMIX_RATE_INDEX] if len(nums) > DUPMIX_RATE_INDEX else DUPMIX_DEFAULT_RATE
        firsts = total - repeats
        if firsts < 1:
            raise ValueError("dupmix needs at least one first request")
        for i in range(firsts):
            items.append(_item(DOORS[i % len(DOORS)], f"{prefix}-{i}", t0 + i / rate, scenario, "first"))
        for j in range(repeats):
            base = items[rng.randrange(firsts)] if firsts else items[0]
            if rng.random() < DUP_BOTH_DOORS_SHARE:
                other = DOORS[1 - DOORS.index(base["door"])]
                items.append(_item(other, base["request_id"], base["intended_ts"], scenario, "other_door_same_moment"))
            else:
                later = base["intended_ts"] + rng.uniform(0, DUP_LATER_MAX_SECONDS)
                items.append(_item(base["door"], base["request_id"], later, scenario, "repeat_later"))
    items.sort(key=lambda it: (it["intended_ts"], it["door"], it["request_id"]))
    return items


def seconds_until(intended_ts: float, base_wall: float, base_mono: float) -> float:
    """Seconds from now to the intended calendar moment, counted on the monotonic clock from one wall-clock reading
    (`base_wall`, taken together with `base_mono`): a later step of the wall clock does not move the schedule."""
    return intended_ts - base_wall - (time.monotonic() - base_mono)


def _worker(args: tuple[int, str, list[dict[str, Any]], int, Path]) -> None:
    """One worker process: threads take its items in intended-time order, wait until the intended moment, send."""
    index, url, items, threads, part = args
    base_wall = time.time()  # wall-clock: the one reading that turns a schedule's calendar time into a monotonic wait
    base_mono = time.monotonic()
    work: queue.Queue[dict[str, Any]] = queue.Queue()
    for it in items:
        work.put(it)
    lock = threading.Lock()
    out = part.open("w", encoding="utf-8")

    def run() -> None:
        while True:
            try:
                it = work.get_nowait()
            except queue.Empty:
                return
            wait = seconds_until(it["intended_ts"], base_wall, base_mono)
            if wait > 0:
                time.sleep(wait)
            # lag from the monotonic schedule (a wall-clock step cannot change it); negative = woke a little early
            lag = -seconds_until(it["intended_ts"], base_wall, base_mono)
            try:
                line = send_with_retries(url, it)
            except Exception as e:  # noqa: BLE001 (a request is never dropped: it is logged as a driver error)
                line = _driver_error_line(it, e)
            line["lag_s"] = round(lag, 4)
            line["worker"] = index
            with lock:
                out.write(json.dumps(line) + "\n")
                out.flush()

    pool = [threading.Thread(target=run) for _ in range(max(1, min(threads, len(items))))]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    out.close()


def _count_time_wait() -> int | None:
    total = 0
    found = False
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        found = True
        total += sum(
            1 for ln in lines if len(ln.split()) >= MIN_TCP_LINE_FIELDS and ln.split()[3] == TCP_STATE_TIME_WAIT
        )
    return total if found else None


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def machine_facts() -> dict[str, Any]:
    """Facts that explain a result: recorded once at the start of a run."""
    mem: dict[str, int] = {}
    try:
        for ln in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = ln.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                mem[key + "_kb"] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    soft = hard = nproc_soft = nproc_hard = None  # the `resource` module does not exist on Windows
    if sys.platform != "win32":
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        nproc_soft, nproc_hard = resource.getrlimit(resource.RLIMIT_NPROC)
    return {
        "ts": time.time(),  # wall-clock: calendar stamp
        "time_wait_count": _count_time_wait(),
        "ulimit_n": {"soft": soft, "hard": hard},
        "somaxconn": _read_int("/proc/sys/net/core/somaxconn"),
        "nproc": {"cpus": os.cpu_count(), "ulimit_u_soft": nproc_soft, "ulimit_u_hard": nproc_hard},
        "memory": mem,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


def run(
    base_url: str, scenario: str, log: Path, facts_path: Path, workers: int, threads: int, seed: int, prefix: str
) -> dict[str, Any]:
    facts = machine_facts()
    t0 = time.time() + START_LEAD_SECONDS  # wall-clock: calendar stamp
    schedule = build_schedule(scenario, t0, prefix, seed)
    url = base_url.rstrip("/") + "/leads"
    workers = max(1, min(workers, len(schedule) or 1))
    parts = [log.with_name(f"{log.name}.part{k}") for k in range(workers)]
    jobs = [(k, url, schedule[k::workers], threads, parts[k]) for k in range(workers)]
    facts.update(
        scenario=scenario,
        workers=workers,
        threads_per_worker=threads,
        intended_requests=len(schedule),
        max_in_flight=workers * threads,
        first_intended_ts=t0,
        seed=seed,
        qualifier=os.environ.get("DESK_QUALIFIER", ""),  # lets the checker know the AI step was live
    )
    facts_path.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(workers) as pool:
        pool.map(_worker, jobs, chunksize=1)
    lines: list[dict[str, Any]] = []
    for p in parts:
        lines += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        p.unlink()
    if len(lines) != len(schedule):  # intended_requests in the facts file is what check 1 compares the log with
        print(f"WARNING: {len(schedule)} requests intended, {len(lines)} logged", file=sys.stderr)
    lines.sort(key=lambda d: (d["intended_ts"], d["door"], d["request_id"]))
    log.write_text("".join(json.dumps(d) + "\n" for d in lines), encoding="utf-8")
    finals = Counter(str(d["final_status"]) for d in lines)
    lag = max((d["lag_s"] for d in lines), default=0.0)
    return {
        "intended": len(schedule),
        "logged": len(lines),
        "final_status": dict(finals),
        "max_lag_s": lag,
        "last_intended_ts": max((d["intended_ts"] for d in lines), default=None),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="load_tools.load_driver")
    ap.add_argument("--base-url", required=True, help="desk base URL, e.g. http://127.0.0.1:8765")
    ap.add_argument("--scenario", required=True, help="burst:N | rate:R:D | dupmix:TOTAL:FRACTION[:RATE]")
    ap.add_argument("--log", type=Path, required=True, help="request log (JSONL), one line per intended request")
    ap.add_argument("--facts", type=Path, help="machine facts JSON (default: <log>.facts.json)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS, help="threads in each worker process")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--id-prefix", default=ID_PREFIX, help="request ids are <prefix>-<n>; change it for every run")
    a = ap.parse_args(argv)
    try:
        parse_scenario(a.scenario)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    facts = a.facts or a.log.with_name(a.log.name + ".facts.json")
    summary = run(a.base_url, a.scenario, a.log, facts, a.workers, a.threads, a.seed, a.id_prefix)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
