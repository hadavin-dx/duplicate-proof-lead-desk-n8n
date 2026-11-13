"""NEW: turns the raw result files of one run into the tables of tables.md (the original summ12/summ3/summ3e scripts
did this by hand for the two published result sets). Every number in the README comes from this output.
  python -I harness/summarise.py <results folder> [--out tables.md] [--ext ndjson]
The folder holds g12_<v>_<setting>, g3_<v>, g12e, g3e_K<n> and calls2_evidence2/3 files (a fresh run writes .jsonl:
pass --ext jsonl). A run is "valid" when recomputed from its overlap fields; the stored flag is ignored."""

import argparse
import collections
import functools
import json
from datetime import datetime
import math
import pathlib
import re
import sys

VARIANTS = (("a", "default"), ("b", "default"), ("c", "default"), ("d", "limit1"))
POINTS = {
    "K1": "email request in flight (CRM already stored)",
    "K2": "email stored, its reply held",
    "K3": "CRM request in flight (first step, email never sent)",
}
E_POINTS = {  # variant e does the email first and the CRM last, so the raw-file letters name other moments than for a to d
    "K3": "email request in flight (row pending)",
    "K2": "email stored, its reply held (row pending)",
    "K1": "CRM request in flight (last step, email already sent)",
}
K_NOTE = ("The letters K1, K2 and K3 in the raw files name different moments for variants a to d (CRM first, then email) and for variant e (email first, then CRM). "
          "This file names the moment in words and gives the letter only so that the raw file can be found.")
CRASH_WORDS = ("crashed", "error", "failed")
# the ONE list of client-side errors that mean "the connection was lost" (the names fire() in lib.py records: type(e).__name__).
# On Linux a SIGKILLed server usually closes its sockets with a FIN, which urllib reports as RemoteDisconnected (a ConnectionResetError subclass, recorded under its own name).
# fire() records an error raised while the request is being sent as "URLError:" plus the class of its reason: a wrapped reset or broken pipe is a lost connection too; other URLError reasons (refused, timeout, name not found) are not.
LOST_CONNECTION = frozenset({"ConnectionResetError", "RemoteDisconnected", "ConnectionAbortedError", "BrokenPipeError", "IncompleteRead", "URLError:ConnectionResetError", "URLError:BrokenPipeError"})


def refuse_damaged(fn):
    """one choke point for every row access: a raw row with a missing field or a field of the wrong type (a damaged or hand-edited file) is a one-line refusal naming only the error class, never a traceback."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except (KeyError, TypeError, AttributeError, IndexError, ValueError, OverflowError, RecursionError, OSError) as e:  # OverflowError: a number too big (1e999, a huge integer); RecursionError: absurd nesting; OSError: a folder or an unreadable file
            sys.exit("refusing to summarise: a raw row or a result file is damaged or unreadable (a field is missing, has the wrong type or an impossible value: %s)" % type(e).__name__)
    return wrapper


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(k, n):
    lo, hi = wilson(k, n)
    return "%d of %d (95%% interval %.1f to %.1f percent)" % (k, n, lo * 100, hi * 100)


def parse(s):
    """ISO time (as n8n and lib.iso write it) to seconds since the epoch."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def overlap(rows):
    """moved here from lib.py (same arithmetic): do the executions' times show ten alive at the same instant? rows: dicts with createdAt, startedAt, stoppedAt."""
    st = sorted(parse(x["startedAt"]) for x in rows if x.get("startedAt"))
    en = [parse(x["stoppedAt"]) for x in rows if x.get("stoppedAt")]
    if len(st) < 2 or len(en) < len(st):
        return {"n": len(rows), "note": "missing times"}
    cr = sorted(parse(x["createdAt"]) for x in rows if x.get("createdAt"))
    return {"n": len(rows), "created_spread_ms": round((cr[-1] - cr[0]) * 1000) if cr else None, "start_spread_ms": round((st[-1] - st[0]) * 1000), "latest_start_before_earliest_stop": st[-1] < min(en),
            "ms_latest_start_minus_earliest_stop": round((st[-1] - min(en)) * 1000), "longest_ms": round((max(en) - st[0]) * 1000)}


def calls_file(folder, ext, which):
    """The published run keeps two logs of the stand-in server; a fresh run writes one (calls2.jsonl) that holds both."""
    p = folder / ("calls2_evidence" + which + "." + ext)
    return p if p.exists() else folder / ("calls2." + ext)


def read(folder, name, ext):
    return read_path(folder / (name + "." + ext))


def _bad_number(x):
    raise ValueError("number out of range")


def _int(x):
    v = int(x)
    if abs(v) > 10 ** 15:  # a 400-digit integer is damage, not a count or a time
        _bad_number(x)
    return v


def _float(x):
    v = float(x)
    if not math.isfinite(v) or abs(v) > 1e15:  # 1e999 parses to infinity; either would reach a count or a table cell
        _bad_number(x)
    return v


def read_path(p):
    """the rows of one JSON-lines file ([] when it does not exist); a damaged line is a one-line refusal naming the file and the line."""
    if not p.exists():
        return []
    rows = []
    for n, x in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not x.strip():
            continue
        try:
            rows.append(json.loads(x, parse_constant=_bad_number, parse_int=_int, parse_float=_float))
        except ValueError:  # JSONDecodeError (a truncated line): refuse in one line, naming the file and the line but no folder
            sys.exit("refusing to summarise: %s line %d is not valid JSON or holds a number that is not finite or is out of range (truncated or damaged file)" % (p.name, n))
    return rows


EMAIL_RE = re.compile(r'"email":\s*"([^"]+)"')  # one pattern for the sweeper count and the cross-check (optional space after the colon)


SETTLE_KEYS = ("settle1", "settle2", "wait_settled", "wait_after_retry")


def settle_reasons(r):
    """Why the waits for quiet of a row do not count (empty list: they do). stale_running_after_kill: an execution still showed running
    after the kill and the retry although the counts stood still and the stand-in was idle: a finding about n8n, not a harness timeout."""
    ws = [r.get(k) or {} for k in SETTLE_KEYS]
    out = []
    if any(w.get("timed_out") for w in ws):
        out.append("timeout")
    if any(w.get("unreachable") for w in ws):
        out.append("stand-in unreachable")
    if (r.get("wait_after_retry") or {}).get("stale_running"):
        out.append("stale_running_after_kill")
    return out


def timed_out(r):
    return "timeout" in settle_reasons(r)


def _ms_ok(v):
    """A millisecond value of an overlap summary: absent, or a finite number that is not negative."""
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0)


def invalid_reason(r, variant):
    """None for a valid ten-at-once run, else the reason. A run whose wait for quiet ran into its time limit is never valid."""
    try:
        ov = overlap(r["exec_times"]) if "exec_times" in r else r["overlap"]  # recomputed from the stored times when the row has them (the stored summary is ignored then)
        if not isinstance(ov, dict):
            return "unreadable overlap data"
    except (AttributeError, ValueError, TypeError, KeyError):  # a time that is not a date, NaN, null, a missing field: the row is invalid, not a crash
        return "unreadable overlap data"
    if any(not _ms_ok(ov.get(k)) for k in ("created_spread_ms", "start_spread_ms", "longest_ms")):
        return "impossible overlap values"  # negative, not a number or NaN
    if settle_reasons(r):
        return settle_reasons(r)[0]
    if ov.get("note") == "missing times":
        return "missing times"
    if ov.get("n") != 10:
        return "10 executions expected, %s listed" % ov.get("n")
    if variant == "d":  # the serialised variant can never overlap: valid = ten sent together and ten executions listed
        if r.get("client_send_spread_ms") is None or r["client_send_spread_ms"] >= 100:
            return "ten not sent within 100 ms"
        return None
    return None if ov.get("latest_start_before_earliest_stop") is True else "no overlap"


def valid(r, variant):
    return invalid_reason(r, variant) is None


def kill_reasons(r):
    """Why a kill run does not count (empty list: it counts)."""
    out = []
    if r.get("trigger_seen_after_s") is None:
        out.append("trigger never seen")
    if r.get("killed_pid") is not True:  # a row without the field is not a confirmed kill (was: default True)
        out.append("no process killed")
    if r.get("n8n_restarted_ok") is False:
        out.append("n8n did not restart")
    return out + settle_reasons(r)


def burst_reasons(r):
    """why a ten-at-once run of variant e does not count: the settle reasons, or the ten were not sent within 100 ms."""
    out = settle_reasons(r)
    if r.get("spread_ms") is None or r["spread_ms"] >= 100:
        out.append("ten not sent within 100 ms")
    return out


def kill_stats(rs):
    """the ONE place that counts the two kill-failure signs, over the rows it is given (callers pass all rows of the variant's result file, so the headline and the per-variant section cannot differ; attempts kept in invalid/ are not rows of that file and are named in kill_stats_text).
    n: the VALID kill rows (the rows kill_reasons does not reject: this is the basis of the 0-of-15 count of REPRODUCTION-RULE.md M5). no_reset: of those, the ones in which no client saw a lost connection (any name in LOST_CONNECTION), so the kill may not have taken effect.
    rows: every row of the file. not_killed: rows (valid or not) in which the harness itself did not confirm the kill (killed_pid not true); such a row is never valid."""
    ok = [r for r in rs if kill_valid(r)]
    return {"n": len(ok), "rows": len(rs), "no_reset": sum(1 for r in ok if not LOST_CONNECTION & set(r.get("client_replies_during_kill", {}))), "not_killed": sum(1 for r in rs if r.get("killed_pid") is not True)}


def kill_stats_text(rs, extra=0):
    """extra: attempts of this variant kept in published_run/invalid/ (not rows of the result file, so not counted in the numbers that follow)."""
    k = kill_stats(rs)
    more = "; %d more attempt%s kept in invalid/" % (extra, "" if extra == 1 else "s") if extra else ""
    return ("Kill rows where no client lost its connection: %d of %d valid kill rows (the result file holds %d rows%s). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. "
            "Rows with killed_pid not true in the raw row: %d (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found)." % (k["no_reset"], k["n"], k["rows"], more, k["not_killed"]))


@refuse_damaged
def reply_keys(folder, ext):
    """every client reply name (status code or error class name) found in the kill and burst rows, with its count: shows the real names fire() recorded."""
    c = collections.Counter()
    for p in sorted(pathlib.Path(folder).glob("g3*." + ext)) + sorted(pathlib.Path(folder).glob("g12e." + ext)):
        for r in read(p.parent, p.name[: -len(ext) - 1], ext):
            c.update(r.get("client_replies_during_kill") or r.get("replies") or {})
    return dict(c)


def kill_valid(r):
    return not kill_reasons(r)


def read_invalid(folder, ext, prefix):
    """Rows moved to published_run/invalid (a fresh run keeps everything in the main file): label -> count."""
    d = folder / "invalid"
    res = collections.Counter()
    if d.is_dir():
        for p in sorted(d.glob(prefix + "*." + ext)):
            n = sum(1 for x in p.read_text(encoding="utf-8").splitlines() if x.strip())
            res["kept in invalid/ (%s)" % (re.sub(r"[_-]+", " ", p.stem[len(prefix):]).strip() or "no reason given")] += n
    return res


def attempt_counts(rows, reasons_fn, extra):
    """(attempts, valid, invalid, reasons) of one test configuration; extra: attempts kept in invalid/ (label -> count)."""
    reasons = collections.Counter()
    nvalid = 0
    for r in rows:
        rs = reasons_fn(r)
        if rs:
            reasons.update(rs)
        else:
            nvalid += 1
    n_invalid = len(rows) - nvalid + sum(extra.values())
    reasons.update(extra)
    return nvalid + n_invalid, nvalid, n_invalid, reasons


def attempts_text(rows, reasons_fn, extra):
    att, nvalid, n_invalid, reasons = attempt_counts(rows, reasons_fn, extra)
    return "attempts %d, valid %d, invalid %d (%s)." % (att, nvalid, n_invalid, ", ".join("%s x%d" % kv for kv in sorted(reasons.items())) or "none")


def att_inv(folder, ext, key, rows, reasons_fn):
    """The short form for the headline: '(attempts A, invalid I)'."""
    att, _, inv, _ = attempt_counts(rows, reasons_fn, read_invalid(folder, ext, key))
    return "(attempts %d, invalid %d)" % (att, inv)


def one(x):
    return [x] if x else []


def attempts_section(folder, ext, out):
    out.append("## Attempts, valid runs and invalid reasons (nothing is dropped without being counted)\n")
    out.append("An invalid attempt is not a failure of n8n and is not hidden: it is a run that did not meet the rule for a valid run, so it is not counted in any result above. It is listed here with its reason. For the 2026-11-09 run, \"kept in invalid/\" means that the original run moved those attempts to the folder `published_run/invalid/` and the text in brackets is the label of the file they were moved to; a new run keeps every attempt in its normal result file and names the reason.\n")
    out.append("An attempt is invalid when: no ten executions listed or no overlap between them (variant d: ten not sent within 100 ms), a wait for quiet ran into its time limit (timeout) or the stand-in could not be reached, or for a kill run the trigger was never seen, no process was killed (taskkill failed or the port was still busy), n8n did not restart, or an execution still showed running after the kill and the retry although nothing else moved (stale_running_after_kill: counted and listed, never part of the valid headline).\n")
    for v, setting in VARIANTS:
        key = "g12_%s_%s" % (v, setting)
        out.append("Test configuration %s %s (ten at once): %s" % (v, setting, attempts_text(read(folder, key, ext), lambda r, v=v: one(invalid_reason(r, v)), read_invalid(folder, ext, key))))
    inv = [r for p in sorted((folder / "invalid").glob("g12_d_limit1*." + ext)) for r in read_path(p)] if (folder / "invalid").is_dir() else []
    if inv:
        n404 = sum(1 for r in inv if set(r.get("replies", {})) == {"404"})
        out.append("Of the %d attempts of d kept in invalid/ (counted here from their raw rows): %d got HTTP 404 on all ten requests (the webhook was not registered yet: a harness fault) and %d have no overlap data (n8n listed no executions, so the overlap rule could not be applied)." % (len(inv), n404, len(inv) - n404))
    for v, _ in VARIANTS:
        key = "g3_%s" % v
        out.append("Test configuration %s (kill runs): %s" % (v, attempts_text(read(folder, key, ext), kill_reasons, read_invalid(folder, ext, key))))
    for kp in E_POINTS:
        key = "g3e_%s" % kp
        out.append("Test configuration e %s (kill runs): %s" % (kp, attempts_text(read(folder, key, ext), kill_reasons, read_invalid(folder, ext, key))))
    out.append("Test configuration e (ten at once): %s" % attempts_text(read(folder, "g12e", ext), burst_reasons, read_invalid(folder, ext, "g12e")))
    out.append("")
    out.append("The runs of one test configuration are not independent: one n8n process, one database and one laptop serve all of them, one after another. The 95% intervals (Wilson) treat them as independent trials, so they are optimistic (too narrow); read them as a floor on the uncertainty, not as a promise. 20 of 20 clean runs does NOT mean that duplicates cannot happen: it means that none was seen in 20 tries on one machine.\n")


def load_section(folder, ext, out):
    rows = read(folder, "load", ext)
    if not rows:
        return
    out.append("## Machine load before and after each test configuration\n")
    out.append("| time (UTC) | test configuration | when | CPU idle percent | free RAM MB |\n|---|---|---|---|---|")
    for r in rows:
        ts = r.get("ts", "n/a")  # rows of an older run have no time stamp
        if "error" in r:
            out.append("| %s | %s | %s | n/a (%s) | n/a |" % (ts, r["setup"], r["when"], r["error"]))
        else:
            out.append("| %s | %s | %s | %.2f | %d |" % (ts, r["setup"], r["when"], r["cpu_idle_pct"], r["free_ram_mb"]))
    out.append("")


@refuse_damaged
def shortfalls(folder, ext, runs=20, kills=5, variants="abcd", with_e=True):
    """Every test configuration must hold the expected number of valid runs; a missing or short file is a shortfall, never silent."""
    folder = pathlib.Path(folder)
    bad = []
    for v, setting in VARIANTS:
        if v in variants:
            n = sum(1 for r in read(folder, "g12_%s_%s" % (v, setting), ext) if valid(r, v))
            if n != runs:
                bad.append("g12 %s %s: expected %d valid runs, found %d" % (v, setting, runs, n))
            for kp in POINTS:
                m = sum(1 for r in read(folder, "g3_%s" % v, ext) if r["kill_point"] == kp and kill_valid(r))
                if m != kills:
                    bad.append("g3 %s %s: expected %d valid kill runs, found %d" % (v, kp, kills, m))
    if with_e:
        for kp in E_POINTS:
            m = sum(1 for r in read(folder, "g3e_%s" % kp, ext) if kill_valid(r))
            if m != kills:
                bad.append("g3e %s: expected %d valid kill runs, found %d" % (kp, kills, m))
        m = sum(1 for r in read(folder, "g12e", ext) if not burst_reasons(r))
        if m != kills:
            bad.append("g12e: expected %d valid runs, found %d" % (kills, m))
    return bad


def dist(items):
    c = collections.Counter(items)
    return ", ".join("%s x%d" % (k, n) for k, n in sorted(c.items(), key=lambda kv: str(kv[0])))


def replies(d):
    return ", ".join("%sx%d" % kv for kv in sorted(d.items()))


def g12_section(folder, ext, out):
    out.append("## Ten at once, then the same ten again as a replay (variants a to d)\n")
    out.append("The replay is sent as soon as the first ten have been quiet for at least 3 seconds (the published run waited about 5 seconds), so the replay timing differs a little and replay numbers are not strictly comparable to the published run.\n")
    for v, setting in VARIANTS:
        rs = [r for r in read(folder, "g12_%s_%s" % (v, setting), ext) if valid(r, v)]
        n = len(rs)
        ok = sum(1 for r in rs if (r["after_run1"]["crm"], r["after_run1"]["outbox"]) == (1, 1))
        label = "n8n default concurrency" if setting == "default" else "concurrency limit 1"
        out.append("### Variant %s (%s)\n" % (v, label))
        out.append("Valid runs %d. Runs ending with exactly 1 CRM row and 1 email: %s. Runs with more: %s." % (n, pct(ok, n), pct(n - ok, n)))
        out.append("Final (CRM rows, emails) after the ten: %s." % dist((r["after_run1"]["crm"], r["after_run1"]["outbox"]) for r in rs))
        out.append("Posts added by the replay (CRM, emails): %s.\n" % dist((r["replay_added"]["crm"], r["replay_added"]["outbox"]) for r in rs))
        out.append("| run | replies (10 hits) | CRM / email after hits | replay added CRM / email |\n|---|---|---|---|")
        for i, r in enumerate(rs, 1):
            out.append("| %d | %s | %d / %d | +%d / +%d |" % (i, replies(r["replies"]), r["after_run1"]["crm"], r["after_run1"]["outbox"], r["replay_added"]["crm"], r["replay_added"]["outbox"]))
        out.append("")


def visible(r):
    return any(k in r["exec_status_after_restart"] for k in CRASH_WORDS)


def g3_section(folder, ext, out):
    out.append("## Kill runs (variants a to d): n8n killed, restarted, retry pressed once per crashed entry\n")
    out.append(K_NOTE + " For variants a to d: K1 = email request in flight (CRM already stored), K2 = email stored with its reply held for 3 seconds, K3 = CRM request in flight (first step, the email is never sent).\n")
    for v, _ in VARIANTS:
        rs = read(folder, "g3_%s" % v, ext)
        out.append("### Variant %s\n" % v)
        out.append("Kill points here: K1 = %s; K2 = %s; K3 = %s." % (POINTS["K1"], POINTS["K2"], POINTS["K3"]))
        if v == "d":
            out.append("The \"list after restart\" of variant d holds fewer executions than for a to c (it lists 1 to 3, not ten). The author did not investigate why; with the concurrency limit of 1, n8n runs one execution at a time, which may be the reason (a guess, not verified).")
        out.append("")
        out.append("| point | run | CRM / email at kill | after in-flight requests ended | list after restart | retry presses | final CRM / email | crashed entry visible | valid |\n|---|---|---|---|---|---|---|---|---|")
        for r in rs:
            a, b, c = r["counts_at_kill"], r["counts_after_inflight_settled"], r["final"]
            lst = ", ".join("%s x%d" % kv for kv in sorted(r["exec_status_after_restart"].items())) or "empty"
            out.append("| %s | %d | %d / %d | %d / %d | %s | %d | %d / %d | %s | %s |" % (
                r["kill_point"], r["run"], a["crm"], a["outbox"], b["crm"], b["outbox"], lst, r["retry_pressed_n"], c["crm"], c["outbox"], "yes" if visible(r) else "no",
                "yes" if kill_valid(r) else "NO: " + ", ".join(kill_reasons(r))))
        out.append(chr(10) + kill_stats_text(rs, sum(read_invalid(folder, ext, "g3_%s" % v).values())))
        for kp, label in POINTS.items():
            s = [r for r in rs if r["kill_point"] == kp and kill_valid(r)]
            if s:
                out.append("\n%s (raw file point %s): %d valid runs, final (CRM, email) %s; crashed entry visible %d of %d." % (
                    label[0].upper() + label[1:], kp, len(s), dist((r["final"]["crm"], r["final"]["outbox"]) for r in s), sum(1 for r in s if visible(r)), len(s)))
        out.append("")


def sweeper_posts(folder, ext):
    posts = collections.defaultdict(lambda: [[], []])  # email -> [email post times, CRM post times]
    for r in read_path(calls_file(folder, ext, "3")):
        m = EMAIL_RE.search(r["body"])
        if "sweeper" in r["body"] and m:
            posts[m.group(1)][0 if r["k"] == "outbox" else 1].append(r["t"])
    return posts


def e_section(folder, ext, out):
    out.append("## Variant e: status column and a re-send job every 15 seconds (blind mail stand-in)\n")
    out.append(K_NOTE + " For variant e: K3 = email request in flight (row still pending), K2 = email stored with its reply held (row pending), K1 = CRM request in flight (last step, email already sent). \"Blind mail stand-in\" means the fake mail server stores every email it is sent and cannot recognise a repeat.\n")
    sw = sweeper_posts(folder, ext)

    def cell(d):
        return "%s/%s/%s" % (d["crm"], d["outbox"], d["db"])

    two_mail = two_crm = lost = total = runs = 0
    for kp, label in E_POINTS.items():
        rs = read(folder, "g3e_%s" % kp, ext)
        out.append("### %s (raw file point %s)\n" % (label[0].upper() + label[1:], kp))
        out.append("Kill points of variant e: K1 = %s; K2 = %s; K3 = %s.\n" % (E_POINTS["K1"], E_POINTS["K2"], E_POINTS["K3"]))
        out.append("Cells are CRM posts / email posts / database status.\n")
        out.append("| run | at kill | after in-flight settled | +5s | +15s | +25s | +40s | +60s | re-send job posts (email/CRM) | client replies | valid |\n|---|---|---|---|---|---|---|---|---|---|---|")
        for r in rs:
            s = r["samples"]
            sp = sw[r["email"]]
            out.append("| %d | %s | %s | %s | %d/%d | %s | %s |" % (r["run"], cell(r["at_kill"]), cell(r["after_inflight_settled"]), " | ".join(cell(x) for x in s),
                                                              len(sp[0]), len(sp[1]), replies(r["client_replies_during_kill"]), "yes" if kill_valid(r) else "NO: " + ", ".join(kill_reasons(r))))
            if not kill_valid(r):
                continue
            runs += 1
            lost += s[-1]["db"] != "sent"
            if kp != "K1":
                total += 1
                two_mail += s[-1]["outbox"] >= 2
                two_crm += s[-1]["crm"] >= 2
        out.append("")
    out.append("At the two email points (K3 and K2 together): %s ended with 2 or more emails; %s ended with 2 CRM rows. Status not `sent` at 60 s in %d of %d kill runs.\n" % (
        pct(two_mail, total), pct(two_crm, total), lost, runs))
    gaps = []
    for times in sw.values():
        ts = sorted(times[0])
        gaps += [round((b - a) * 1000) for a, b in zip(ts, ts[1:]) if b - a < 1]
    out.append("Gaps (ms) between two re-send emails for the same address when less than 1 s apart: %s.\n" % (sorted(gaps) or "none"))
    br = [r for r in read(folder, "g12e", ext) if not burst_reasons(r)]  # valid runs only (invalid ones are in the attempts section)
    out.append("Ten at once for e: %d runs; final (CRM, email) %s; 35 s later %s; replies %s.\n" % (
        len(br), dist((r["counts"]["crm"], r["counts"]["outbox"]) for r in br),
        dist((r["counts_after_35s"]["crm"], r["counts_after_35s"]["outbox"]) for r in br), dist(replies(r["replies"]) for r in br)))


def crosscheck(folder, ext, out):
    """Compare the harness's final counts with the stand-in's own log (every post it stored for that address)."""
    p = calls_file(folder, ext, "2")
    if not p.exists():
        return
    per = collections.defaultdict(lambda: [0, 0])
    for r in read_path(p):
        m = EMAIL_RE.search(r["body"])
        if m:
            per[m.group(1)][0 if r["k"] == "crm" else 1] += 1
    same = diff = skipped = 0
    for v, setting in VARIANTS:
        for r in read(folder, "g12_%s_%s" % (v, setting), ext):
            if "after_replay" in r:
                ok = per[r["email"]] == [r["after_replay"]["crm"], r["after_replay"]["outbox"]]
                same += ok
                diff += not ok
        for r in read(folder, "g3_%s" % v, ext):
            if not kill_valid(r):  # valid kill rows only
                skipped += 1
                continue
            ok = per[r["email"]] == [r["final"]["crm"], r["final"]["outbox"]]
            same += ok
            diff += not ok
    out.append("## Cross-check against the stand-in's own log\n")
    out.append("The final counts of each run (ten-at-once after the replay; kill runs after the retry) compared with the posts the fake server logged for that address: %d runs equal, %d differ%s.\n" % (same, diff, " (kill runs that do not count are left out: %d)" % skipped if skipped else ""))


def headline(folder, ext, out):
    """The numbers the post cites, in one place, counted from the raw rows."""
    out.append("## Summary (counted from the raw files)" + chr(10))
    for v, setting in VARIANTS:
        rs = [r for r in read(folder, "g12_%s_%s" % (v, setting), ext) if valid(r, v)]
        ok = sum(1 for r in rs if (r["after_run1"]["crm"], r["after_run1"]["outbox"]) == (1, 1))
        added = sum(1 for r in rs if (r["replay_added"]["crm"], r["replay_added"]["outbox"]) != (0, 0))
        key = "g12_%s_%s" % (v, setting)
        out.append("* Variant %s, ten at once %s: %s ended with exactly 1 CRM row and 1 email; runs where the replay added posts: %d of %d." % (
            v, att_inv(folder, ext, key, read(folder, key, ext), lambda r, v=v: one(invalid_reason(r, v))), pct(ok, len(rs)), added, len(rs)))
    for v, _ in VARIANTS:
        allrs = read(folder, "g3_%s" % v, ext)
        rs = [r for r in allrs if kill_valid(r)]  # an invalid kill run (no trigger, no restart, timeout) is reported in the attempts section, not counted in the outcome counts below
        crm_first = [r for r in rs if r["kill_point"] == "K3"]
        other = [r for r in rs if r["kill_point"] != "K3"]
        out.append("* Variant %s kill runs: %s" % (v, kill_stats_text(allrs, sum(read_invalid(folder, ext, "g3_%s" % v).values()))))
        out.append("* Variant %s kill runs %s: CRM request in flight (first step): %d of %d ended with 1 CRM row and 0 emails after one retry press; the two email points: %d of %d ended with 1 CRM row and 1 email." % (
            v, att_inv(folder, ext, "g3_%s" % v, allrs, kill_reasons), sum(1 for r in crm_first if (r["final"]["crm"], r["final"]["outbox"]) == (1, 0)), len(crm_first),
            sum(1 for r in other if (r["final"]["crm"], r["final"]["outbox"]) == (1, 1)), len(other)))
    out.append("")


@refuse_damaged
def busy_verdict(folder, ext="ndjson", threshold=80.0):
    """one line per test configuration of load.jsonl, in file order: 'busy' = two or more consecutive readings with cpu_idle_pct below `threshold`, or a mean below it (REPRODUCTION-RULE.md section 5).
    Only rows with a cpu_idle_pct count (a row with an `error` key has none and is not a reading)."""
    by = {}
    for r in read(pathlib.Path(folder), "load", ext):
        if "cpu_idle_pct" not in r and "setup" in r:
            by.setdefault(r["setup"], [])  # a configuration with only error rows is listed below as not judged
        if "cpu_idle_pct" in r:
            x = r["cpu_idle_pct"]
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 100:  # a string, a bool, NaN (fails every comparison), or a value outside 0..100 would read as quiet or hide a busy reading: it is an unreadable row
                raise ValueError("cpu_idle_pct is not a number from 0 to 100")
            x = float(x)
            by.setdefault(r["setup"], []).append(x)
    lines = []
    for setup, v in by.items():
        if not v:
            lines.append("%s: no readings (not judged; count it as not clean)" % setup)
            continue
        run = any(a < threshold and b < threshold for a, b in zip(v, v[1:]))
        mean = sum(v) / len(v)
        lines.append("%s: %s (%d readings, mean CPU idle %.2f, lowest %.2f)" % (setup, "BUSY" if run or mean < threshold else "not busy", len(v), mean, min(v)))
    return lines if any(v for v in by.values()) else ["no CPU readings in load.%s: the load is not judged; quote this and publish the run with the not-clean label" % ext]


@refuse_damaged
def summarise(folder, ext="ndjson"):
    folder = pathlib.Path(folder)
    out = ["# Tables generated from the raw files (harness/summarise.py). Do not edit by hand.\n"]
    headline(folder, ext, out)
    attempts_section(folder, ext, out)
    load_section(folder, ext, out)
    g12_section(folder, ext, out)
    g3_section(folder, ext, out)
    e_section(folder, ext, out)
    crosscheck(folder, ext, out)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--keys", action="store_true", help="print the client reply names seen in the kill and burst rows and stop")
    ap.add_argument("--busy", action="store_true", help="print one busy or not busy line per test configuration from load.jsonl and stop")
    ap.add_argument("--out")
    ap.add_argument("--ext", default="ndjson")
    ap.add_argument("--runs", type=int, default=20, help="valid ten-at-once runs expected per test configuration (3 for a --smoke run)")
    ap.add_argument("--kills", type=int, default=5, help="valid kill runs expected per kill point (3 for a --smoke run)")
    ap.add_argument("--variants", default="abcd", help="variants that were run (b for a --smoke run)")
    ap.add_argument("--no-e", action="store_true", help="variant e was not run (--smoke)")
    a = ap.parse_args()
    if a.keys:
        sys.exit(print(json.dumps(reply_keys(a.folder, a.ext), indent=1)))
    if a.busy:
        sys.exit(print(chr(10).join(busy_verdict(a.folder, a.ext))))
    text = summarise(a.folder, a.ext)
    short = shortfalls(a.folder, a.ext, a.runs, a.kills, a.variants, not a.no_e)
    if short:
        text += "## SHORTFALL (fewer valid runs than expected)\n\n" + "\n".join("* " + x for x in short) + "\n"
    if a.out:
        try:
            pathlib.Path(a.out).write_text(text, encoding="utf-8", newline="\n")
        except OSError as e:
            sys.exit("cannot write the tables to %r (%s)" % (a.out, type(e).__name__))
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    if short:
        sys.stderr.write("SHORTFALL: %d test configurations have fewer valid runs than expected (see the end of the tables)\n" % len(short))
        sys.exit(2)
