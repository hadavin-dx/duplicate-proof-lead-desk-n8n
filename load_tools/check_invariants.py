"""The seven checks run after a load scenario has drained. Formats: load_tools/README.md.

Run: python -m load_tools.check_invariants --requests req.jsonl --book desk.sqlite3 --catcher mail.jsonl
         --crm crm.json --model-log model.jsonl [--drain drain.json] [--json out.json]
Exit code 0 only when all seven checks hold; 1 when a check fails; 2 when an input file cannot be read."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# ---- config block (the only place for numbers and words) ----
FIRST_N = 10  # offending ids listed per check
DEFAULT_MODEL_CALL_BOUND = 3  # check 5: most model calls allowed for one lead
DEFAULT_MAX_DRAIN_SECONDS = 120.0  # check 7: ceiling from the last intended send to "nothing pending or sending"
DRAIN_STAMP_TOLERANCE_SECONDS = (
    1.0  # check 7: the book cuts stamps to whole seconds: up to 1 s before the last send is normal
)
ACK_CODES = (200, 201, 202)  # reply codes that carry a receipt number (contract.md, HTTP table)
BUSY_CODE = 503
OPEN_STATES = ("pending", "sending")
STATES = ("pending", "sending", "sent", "failed", "unknown")
MESSAGE_FREE_STATES = ("failed", "unknown")  # follow-ups allowed (and required) to have no message
DEFAULT_MAX_REVIEW = 0  # check 1: receipts allowed to end with the verdict `review`
DEFAULT_MAX_FAILED = 0  # check 2: follow-ups allowed to end `failed` (a scenario with a fault window sets its own)
DEFAULT_MAX_UNKNOWN = (
    0  # check 2: follow-ups allowed to end `unknown` (a scenario with a crash or a slow mail system sets its own)
)
# The follow-up text the desk sends (the same words as FOLLOWUP_BODY_TEMPLATE in src/lead_desk/constants.py; a test
# pins the two together). The checker stays free of any import from the desk, so it can run on a copy of the logs.
FOLLOWUP_BODY_TEMPLATE = "Hi {name}, we received your message. (DEMO EMAIL)"
# The follow-up subject the desk sends (FOLLOWUP_SUBJECT_TEMPLATE in src/lead_desk/constants.py; a test pins the two).
FOLLOWUP_SUBJECT_TEMPLATE = "Thanks for getting in touch, {name}"
CRM_INTENT_MARK = 2  # followups.crm_done: a put was started and its end is not known
CRM_DONE = 1
EXIT_OK, EXIT_FAIL, EXIT_INPUT = 0, 1, 2
CHECK_NAMES = {
    1: "every request id has one decided receipt and one lead",
    2: "follow-up states add up and nothing is left pending",
    3: "at most one message per send key; no message only for failed or unknown",
    4: "CRM rows match the leads marked done, plus the listed unknowns",
    5: "model calls per lead are within the bound",
    6: "503 rate reported (fails only above a set ceiling)",
    7: "drain time within the ceiling",
}


class InputError(Exception):
    """An input file is missing or not in the documented format."""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise InputError(f"cannot read {path.name}: {type(e).__name__}") from e
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise InputError(f"{path.name} line {n}: not JSON") from e
        if not isinstance(obj, dict):
            raise InputError(f"{path.name} line {n}: not a JSON object")
        rows.append(obj)
    return rows


def read_crm(path: Path) -> list[dict[str, Any]]:
    """Accepts the fakes server reply {"count", "rows": [...]} or a bare list of rows (each has lead_id)."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InputError(f"cannot read CRM dump {path.name}: {type(e).__name__}") from e
    rows = obj.get("rows") if isinstance(obj, dict) else obj
    if not isinstance(rows, list) or not all(isinstance(r, dict) and "lead_id" in r for r in rows):
        raise InputError(f"{path.name}: expected a list of rows or an object with a rows list; each row needs lead_id")
    return rows


def read_book(path: Path) -> dict[str, Any]:
    """Read-only look at the desk's SQLite book."""
    if not path.is_file():
        raise InputError(f"no such book: {path.name}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            receipts = {
                (s, r): int(i) for i, s, r in conn.execute("SELECT receipt_id, source, source_ref FROM receipts")
            }
            outcomes = {(s, r): o for s, r, o in conn.execute("SELECT source, source_ref, outcome FROM receipts")}
            lead_emails = [r[0] for r in conn.execute("SELECT email_key FROM leads")]
            leads = [r[0] for r in conn.execute("SELECT lead_id FROM leads")]
            lead_to = {lid: (email or "") for lid, email in conn.execute("SELECT lead_id, email FROM leads")}
            lead_names = {lid: (name or "") for lid, name in conn.execute("SELECT lead_id, name FROM leads")}
            followups = {
                lid: (state, int(done), int(upd))
                for lid, state, done, upd in conn.execute("SELECT lead_id, state, crm_done, updated_at FROM followups")
            }
            reasons = {lid: (err or "") for lid, err in conn.execute("SELECT lead_id, last_error FROM followups")}
            new_receipts = int(conn.execute("SELECT count(*) FROM receipts WHERE outcome='new'").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise InputError(f"cannot read book {path.name}: {type(e).__name__}") from e
    return {
        "receipts": receipts,
        "outcomes": outcomes,
        "lead_emails": lead_emails,
        "leads": leads,
        "lead_to": lead_to,
        "lead_names": lead_names,
        "followups": followups,
        "last_errors": reasons,
        "new_receipts": new_receipts,
    }


def _result(n: int, ok: bool, detail: str, offenders: Iterable[str], values: Mapping[str, Any]) -> dict[str, Any]:
    bad = sorted(set(offenders))
    return {
        "id": n,
        "name": CHECK_NAMES[n],
        "ok": ok,
        "detail": detail,
        "offender_count": len(bad),
        "offenders": bad[:FIRST_N],
        "values": dict(values),
    }


def _rid(line: Mapping[str, Any]) -> str:
    return f"{line.get('door')}:{line.get('request_id')}"


def check_receipts(
    reqs: list[dict[str, Any]],
    book: Mapping[str, Any],
    intended: int | None = None,
    max_review: int = DEFAULT_MAX_REVIEW,
) -> dict[str, Any]:
    """Every request id has a receipt, every receipt has a verdict (never NULL), `review` verdicts stay within
    `max_review`, new + duplicate + review == receipts, each request id (one person) has exactly ONE `new` verdict and
    one lead (a doubled lead is a doubled follow-up), and the request log has as many lines as the driver intended to
    send (`intended`, from its facts file; None = not compared)."""
    receipts: Mapping[tuple[str, str], int] = book["receipts"]
    outcomes: Mapping[tuple[str, str], str | None] = book.get("outcomes", {})
    lost: list[str] = []
    mismatch: list[str] = []
    unacked_stored = 0
    unique = {(str(r.get("door")), str(r.get("request_id"))) for r in reqs}
    for door, ref in unique:
        if (door, ref) not in receipts:
            lost.append(f"{door}:{ref}")
    for r in reqs:
        key = (str(r.get("door")), str(r.get("request_id")))
        acked = r.get("final_status") in ACK_CODES
        if key in receipts and not acked:
            unacked_stored += 1
        if acked and key in receipts and r.get("receipt_id") is not None and r["receipt_id"] != receipts[key]:
            mismatch.append(_rid(r))
    undecided = [f"{d}:{r}" for (d, r), o in outcomes.items() if o is None]
    reviews = [f"{d}:{r}" for (d, r), o in outcomes.items() if o == "review"]
    n_new = sum(1 for o in outcomes.values() if o == "new")
    n_dup = sum(1 for o in outcomes.values() if o == "duplicate")
    sum_gap = bool(outcomes) and (n_new + n_dup + len(reviews)) != len(receipts)
    news_per_ref = Counter(r for (_, r), o in outcomes.items() if o == "new")
    doubled_refs = sorted(r for r, n in news_per_ref.items() if n > 1)
    refs_without_new = sorted(
        {r for _, r in outcomes} - set(news_per_ref) - {r for (_, r), o in outcomes.items() if o in ("review", None)}
    )
    emails = Counter(book.get("lead_emails", []))
    doubled_people = sorted(e for e, n in emails.items() if n > 1)
    lead_gap = bool(outcomes) and len(book["leads"]) != n_new
    too_many_review = len(reviews) > max_review
    intended_gap = intended is not None and intended != len(reqs)
    bad = (
        lost
        + mismatch
        + [f"undecided:{u}" for u in undecided]
        + (reviews if too_many_review else [])
        + [f"two-new:{r}" for r in doubled_refs]
        + [f"no-new:{r}" for r in refs_without_new]
        + [f"two-leads:{e}" for e in doubled_people]
    )
    empty = not reqs  # an empty request log proves nothing: a run that sent nothing must not pass
    detail = (
        f"{len(unique)} distinct request ids, {len(lost)} without a receipt, "
        f"{len(mismatch)} with a wrong receipt number; {len(receipts)} receipts = {n_new} new + {n_dup} duplicate"
        f" + {len(reviews)} review, {len(undecided)} undecided; {len(doubled_refs)} ids with two new verdicts, "
        f"{len(doubled_people)} people with two leads; "
        + (f"{intended} requests intended, {len(reqs)} logged" if intended is not None else "intended count not given")
        + ("; the request log is EMPTY" if empty else "")
    )
    if sum_gap or lead_gap:
        detail += f"; verdicts do not add up (leads {len(book['leads'])} vs new {n_new})"
    return _result(
        1,
        not bad and not empty and not sum_gap and not lead_gap and not intended_gap,
        detail,
        bad,
        {
            "distinct_requests": len(unique),
            "lines": len(reqs),
            "intended": intended,
            "lost": len(lost),
            "stored_but_client_unacked": unacked_stored,
            "receipts": len(receipts),
            "new": n_new,
            "duplicate": n_dup,
            "review": len(reviews),
            "undecided": len(undecided),
            "max_review": max_review,
        },
    )


def check_partition(
    book: Mapping[str, Any], max_failed: int = DEFAULT_MAX_FAILED, max_unknown: int = DEFAULT_MAX_UNKNOWN
) -> dict[str, Any]:
    """States add up, nothing is open, every `failed` or `unknown` row names a reason, no more than `max_failed`
    rows ended `failed`, no more than `max_unknown` rows ended `unknown`, and the number of leads
    equals the number of `new` verdicts (a book that lost its leads must not pass)."""
    followups: Mapping[str, tuple[str, int, int]] = book["followups"]
    reasons: Mapping[str, str] = book.get("last_errors", {})
    leads: list[str] = book["leads"]
    counts = Counter(state for state, _, _ in followups.values())
    total = sum(counts[s] for s in STATES)
    no_row = [lid for lid in leads if lid not in followups]
    open_ids = [lid for lid, (state, _, _) in followups.items() if state in OPEN_STATES]
    no_reason = [
        lid for lid, (state, _, _) in followups.items() if state in MESSAGE_FREE_STATES and not reasons.get(lid)
    ]
    too_many_failed = counts["failed"] > max_failed
    too_many_unknown = counts["unknown"] > max_unknown
    all_unknown = bool(leads) and counts["unknown"] >= len(leads)  # proves nothing, whatever the allowance says
    none_sent = bool(leads) and counts["sent"] == 0  # no email at all proves nothing either, whatever the allowances
    new_receipts = int(book.get("new_receipts", len(leads)))
    lead_gap = new_receipts != len(leads)
    ok = (
        total == len(leads)
        and len(followups) == len(leads)
        and not open_ids
        and not no_reason
        and not too_many_failed
        and not too_many_unknown
        and not all_unknown
        and not none_sent
        and not lead_gap
    )
    values = {s: counts[s] for s in STATES} | {
        "leads": len(leads),
        "sum": total,
        "followup_rows": len(followups),
        "max_failed": max_failed,
        "max_unknown": max_unknown,
        "new_receipts": new_receipts,
    }
    detail = " + ".join(f"{s} {counts[s]}" for s in STATES) + f" = {total}; leads {len(leads)}"
    if too_many_failed:
        detail += f"; {counts['failed']} failed, more than the allowed {max_failed}"
    if too_many_unknown:
        detail += f"; {counts['unknown']} unknown, more than the allowed {max_unknown}"
    if all_unknown:
        detail += "; every follow-up ended unknown, which proves nothing (no allowance lets this pass)"
    if none_sent:
        detail += "; no follow-up ended sent, which proves nothing (no allowance lets this pass)"
    if lead_gap:
        detail += f"; {new_receipts} receipts decided new but {len(leads)} leads"
    offenders = no_row + open_ids + [f"no-reason:{lid}" for lid in no_reason]
    if too_many_failed:
        offenders += [lid for lid, (state, _, _) in followups.items() if state == "failed"]
    if too_many_unknown or all_unknown:
        offenders += [lid for lid, (state, _, _) in followups.items() if state == "unknown"]
    if none_sent:
        offenders += ["none-sent"]
    return _result(2, ok, detail, offenders, values)


def _body_hash(name: str) -> str:
    return hashlib.sha256(FOLLOWUP_BODY_TEMPLATE.format(name=name).encode("utf-8")).hexdigest()


def check_messages(
    book: Mapping[str, Any], mail: list[dict[str, Any]], require_comparable: bool = False
) -> dict[str, Any]:
    """A message with no recorded recipient, subject or body hash cannot be compared with the lead (a mailbox count of
    the real run records none). They are COUNTED (`not_comparable`); with `require_comparable` (the default for fake
    and dry runs, where the catcher records all three) a count above 0 fails the check, so a catcher that stopped
    recording cannot turn the recipient and text checks into silent passes."""
    followups: Mapping[str, tuple[str, int, int]] = book["followups"]
    unparseable = [m for m in mail if m.get("unparseable")]
    mail = [m for m in mail if not m.get("unparseable")]
    per_key = Counter(str(m.get("send_key")) for m in mail)
    lead_to: Mapping[str, str] = book.get("lead_to", {})
    wrong_to = sorted(
        {
            str(m.get("send_key"))
            for m in mail
            if str(m.get("send_key")) in lead_to
            and m.get("to")  # no recorded recipient (a mailbox count of the real run): nothing to compare
            and str(m.get("to", "")).strip().lower() != lead_to[str(m.get("send_key"))].strip().lower()
        }
    )
    doubled = [k for k, n in per_key.items() if n > 1]
    # one lead address gets at most ONE message in total, whatever key the messages carry (the key is named,
    # never the address)
    by_address: dict[str, list[str]] = {}
    for m in mail:
        if m.get("to"):
            by_address.setdefault(str(m["to"]).strip().lower(), []).append(str(m.get("send_key")))
    shared_address = sorted({k for keys in by_address.values() if len(keys) > 1 for k in keys} - set(doubled))
    names: Mapping[str, str] = book.get("lead_names", {})
    wrong_body = sorted(  # only when the catcher recorded a body hash and the book holds the lead's name
        {
            str(m.get("send_key"))
            for m in mail
            if m.get("body_sha256")
            and str(m.get("send_key")) in names
            and m["body_sha256"] != _body_hash(names[str(m["send_key"])])
        }
    )
    wrong_subject = sorted(
        {
            str(m.get("send_key"))
            for m in mail
            if m.get("subject")
            and str(m.get("send_key")) in names
            and m["subject"] != FOLLOWUP_SUBJECT_TEMPLATE.format(name=names[str(m["send_key"])])
        }
    )
    not_comparable = sum(1 for m in mail if not (m.get("to") and m.get("body_sha256") and m.get("subject")))
    stray = [k for k in per_key if k not in followups]
    settled = {lid for lid, (state, _, _) in followups.items() if state not in OPEN_STATES}
    silent = {lid for lid in settled if per_key.get(lid, 0) == 0}
    must_be_silent = {lid for lid in settled if followups[lid][0] in MESSAGE_FREE_STATES}
    lost_mail = sorted(silent - must_be_silent)  # sent, but no message at the catcher
    with_message = must_be_silent - silent  # failed or unknown, yet a message arrived
    # an `unknown` row may really have sent (that is what unknown means): reported, not a failure; `failed` never sends
    # a keyed `failed` row whose last try ended unclear (`ambiguous`, the reply was lost) may also have left a message
    # (test_failed_retry: the stored message is there although the reply was lost): the same, reported only
    reasons: Mapping[str, str] = book.get("last_errors", {})
    unknown_sent = sorted(
        lid for lid in with_message if followups[lid][0] == "unknown" or reasons.get(lid) == "ambiguous"
    )
    unexpected = sorted(with_message - set(unknown_sent))
    bad = doubled + stray + lost_mail + unexpected + [f"wrong-recipient:{k}" for k in wrong_to]
    bad += [f"address-shared:{k}" for k in shared_address] + [f"wrong-body:{k}" for k in wrong_body]
    bad += [f"wrong-subject:{k}" for k in wrong_subject]
    bad += ["unparseable-message"] * min(len(unparseable), 1)
    bad += [f"not-comparable:{not_comparable}"] * (1 if require_comparable and not_comparable else 0)
    detail = (
        f"{len(mail)} messages; {len(unparseable)} unreadable, {len(wrong_to)} to the wrong address; "
        f"{len(doubled)} keys more than once, {len(shared_address)} keys sharing an address, "
        f"{len(wrong_body)} with a wrong text, {len(wrong_subject)} with a wrong subject, "
        f"{not_comparable} not comparable (no recipient, subject or text hash recorded), "
        f"{len(lost_mail)} sent without a message, "
        f"{len(unexpected)} failed with a message, {len(unknown_sent)} unknown that did send (reported only), "
        f"{len(stray)} keys of no lead"
    )
    values = {
        "messages": len(mail),
        "unparseable": len(unparseable),
        "wrong_recipient": len(wrong_to),
        "shared_address": len(shared_address),
        "wrong_body": len(wrong_body),
        "wrong_subject": len(wrong_subject),
        "not_comparable": not_comparable,
        "keys": len(per_key),
        "max_per_key": max(per_key.values(), default=0),
        "silent_followups": len(silent),
        "failed_or_unknown": len(must_be_silent),
        "unknown_that_sent": len(unknown_sent),
    }
    return _result(3, not bad, detail, bad, values)


def check_crm(
    book: Mapping[str, Any], crm_rows: list[dict[str, Any]], require_comparable: bool = False
) -> dict[str, Any]:
    """A row whose record carries a lead_id or an email is compared with the lead (the desk writes both). A row with
    an empty record cannot be compared: counted (`not_comparable`) and, with `require_comparable`, a failure.
    Expected = leads with crm_done=1 (must be present) plus unknowns (may be present): leads with crm_done=2
    (a put was started, its end unknown) or a follow-up in state unknown. A row for any other lead, a lead_id twice,
    or a missing crm_done=1 row fails."""
    followups: Mapping[str, tuple[str, int, int]] = book["followups"]
    leads = set(book["leads"])
    done = {lid for lid, (_, d, _) in followups.items() if d == CRM_DONE}
    unknown = {lid for lid, (s, d, _) in followups.items() if d == CRM_INTENT_MARK or s == "unknown"} - done
    ids = Counter(str(r["lead_id"]) for r in crm_rows)
    present = set(ids)
    missing = sorted(done - present)
    extra = sorted(present - done - unknown)
    twice = sorted(k for k, n in ids.items() if n > 1)
    not_leads = sorted(present - leads)
    no_crm = [f"sent-without-crm:{lid}" for lid, (s, d, _) in followups.items() if s == "sent" and d != CRM_DONE]
    lead_to: Mapping[str, str] = book.get("lead_to", {})
    wrong_content: list[str] = []
    not_comparable = 0
    for r in crm_rows:
        rec = r.get("record")
        rid = str(r["lead_id"])
        if not isinstance(rec, Mapping) or not (rec.get("lead_id") or rec.get("email")):
            not_comparable += 1
            continue
        if rec.get("lead_id") and str(rec["lead_id"]) != rid:
            wrong_content.append(f"wrong-row-id:{rid}")
        if rec.get("email") and rid in lead_to and str(rec["email"]).strip().lower() != lead_to[rid].strip().lower():
            wrong_content.append(f"wrong-row-email:{rid}")
    bad = missing + extra + twice + not_leads + no_crm + wrong_content
    bad += [f"not-comparable:{not_comparable}"] * (1 if require_comparable and not_comparable else 0)
    lo, hi = len(done), len(done) + len(unknown)
    detail = (
        f"{len(crm_rows)} CRM rows; allowed {lo} to {hi} (done {lo} + up to {len(unknown)} unknown); "
        f"{len(missing)} missing, {len(extra)} not allowed, {len(twice)} twice, {len(no_crm)} sent without a CRM mark, "
        f"{len(wrong_content)} with a lead id or email that is not the lead's, {not_comparable} not comparable"
    )
    # The keyed-CRM end state (rule): a lead whose follow-up ENDED `failed` can hold crm_done 0 (the put proved
    # nothing was stored: no row allowed), 2 (a put was started and its end is unknown: a row may be there, listed as
    # an unknown) or 1
    # (done: a row must be there). Every other combination of row and mark is a failure.
    values = {
        "crm_rows": len(crm_rows),
        "leads_crm_done": lo,
        "listed_unknowns": len(unknown),
        "unknowns_present": len(present & unknown),
        "missing": len(missing),
        "extra": len(extra),
        "twice": len(twice),
        "sent_without_crm": len(no_crm),
        "wrong_content": len(wrong_content),
        "not_comparable": not_comparable,
    }
    return _result(4, not bad, detail, bad, values)


def check_model_calls(
    model_rows: list[dict[str, Any]], bound: int, book: Mapping[str, Any] | None = None, require_call: bool = False
) -> dict[str, Any]:
    """Upper bound per lead; with `require_call` (a run whose AI step is live) every lead that ended `sent` must have
    at least one model call logged: a sent lead with no model call means the AI step was skipped."""
    per_lead = Counter(str(r.get("lead_id")) for r in model_rows)
    over = [lid for lid, n in per_lead.items() if n > bound]
    if require_call and book is not None:
        over += [
            f"no-model-call:{lid}"
            for lid, (s, _, _) in book["followups"].items()
            if s == "sent" and lid not in per_lead
        ]
    by_status = Counter(str(r.get("status")) for r in model_rows)
    values = {
        "bound": bound,
        "calls": len(model_rows),
        "leads_called": len(per_lead),
        "max_calls_one_lead": max(per_lead.values(), default=0),
        "calls_by_status": dict(by_status),
    }
    most = values["max_calls_one_lead"]
    detail = f"{len(model_rows)} calls for {len(per_lead)} leads; most for one lead {most}; bound {bound}"
    return _result(5, not over, detail, over, values)


def check_rate_503(reqs: list[dict[str, Any]], ceiling: float | None) -> dict[str, Any]:
    attempts = [a for r in reqs for a in (r.get("attempts") or [])]
    n503 = sum(1 for a in attempts if a.get("code") == BUSY_CODE)
    rate = n503 / len(attempts) if attempts else 0.0
    saw = [_rid(r) for r in reqs if any(a.get("code") == BUSY_CODE for a in (r.get("attempts") or []))]
    final = [_rid(r) for r in reqs if r.get("final_status") == BUSY_CODE]
    ok = ceiling is None or rate <= ceiling
    values = {
        "attempts": len(attempts),
        "responses_503": n503,
        "rate": round(rate, 6),
        "requests": len(reqs),
        "requests_that_saw_503": len(saw),
        "requests_ending_on_503": len(final),
        "ceiling": ceiling,
    }
    detail = (
        f"{n503} of {len(attempts)} attempts got 503 (rate {rate:.4f}); {len(saw)} of {len(reqs)} requests saw one; "
        f"{len(final)} ended on 503; ceiling {'none (report only)' if ceiling is None else ceiling}"
    )
    return _result(6, ok, detail, [] if ok else final or saw, values)


def check_drain(
    reqs: list[dict[str, Any]], book: Mapping[str, Any], drained_ts: float | None, ceiling: float
) -> dict[str, Any]:
    intended = [float(r["intended_ts"]) for r in reqs if r.get("intended_ts") is not None]
    last_send = max(intended, default=None)
    first_send = min(intended, default=None)
    # The checker computes the drain moment itself: the last time any follow-up row changed (whole seconds). A marker
    # file can only make it LATER, so a marker that claims "drained" early cannot make the check pass.
    updates = [u for _, _, u in book["followups"].values()]
    from_book = float(max(updates)) if updates else None
    source = "book updated_at (whole seconds)"
    if drained_ts is not None and (from_book is None or drained_ts > from_book):
        source = "marker (later than the book)"
    else:
        drained_ts = from_book
    if last_send is None or drained_ts is None:
        return _result(7, False, "no intended send time or no drain moment", [], {"source": source})
    # Two clocks meet here: the book's stamps are the desk's calendar clock in whole seconds (cut down), the send
    # times are the driver's calendar clock. Both run on this computer. Cutting down can put the last stamp up to one
    # second before the last send. The last row change can also be earlier than the last send, because a send that
    # makes no follow-up row (a repeat, a review item, a give-up on 503) changes no row: that is a normal drain, clamped
    # to 0 s. What cannot happen is a last row change before the FIRST send: that means a clock was stepped back or the
    # files do not belong together: its own failure, never a silent pass. Only the FIRST send is compared, so a backward
    # step smaller than the run length (the drain moment still after the first send) goes undetected: accepted.
    raw = drained_ts - last_send
    if drained_ts - first_send < -DRAIN_STAMP_TOLERANCE_SECONDS:
        return _result(
            7,
            False,
            f"the drain moment is {first_send - drained_ts:.2f} s BEFORE the first intended send: a clock was stepped "
            "back, or the book and the request log are from different runs; the drain time cannot be judged",
            [],
            {
                "source": source,
                "drain_seconds": round(raw, 3),
                "last_intended_send_ts": last_send,
                "drained_ts": drained_ts,
            },
        )
    seconds = max(0.0, raw)
    ok = seconds <= ceiling
    values = {
        "drain_seconds": round(seconds, 3),
        "ceiling_seconds": ceiling,
        "last_intended_send_ts": last_send,
        "drained_ts": drained_ts,
        "source": source,
    }
    return _result(
        7, ok, f"{seconds:.2f} s from the last intended send to drained (ceiling {ceiling} s; {source})", [], values
    )


def run_checks(
    reqs: list[dict[str, Any]],
    book: Mapping[str, Any],
    mail: list[dict[str, Any]],
    crm_rows: list[dict[str, Any]],
    model_rows: list[dict[str, Any]],
    drained_ts: float | None = None,
    model_bound: int = DEFAULT_MODEL_CALL_BOUND,
    max_503_rate: float | None = None,
    max_drain_seconds: float = DEFAULT_MAX_DRAIN_SECONDS,
    max_failed: int = DEFAULT_MAX_FAILED,
    intended: int | None = None,
    max_review: int = DEFAULT_MAX_REVIEW,
    require_model_call: bool = False,
    max_unknown: int = DEFAULT_MAX_UNKNOWN,
    require_comparable: bool = False,
) -> dict[str, Any]:
    """Run all seven checks. `require_comparable` defaults to False here (a library caller with a real mailbox count
    records no recipient or text); the command line passes its own flag (`--require-comparable`, used for fake and dry
    runs) and scripts/run_real_check.py does not pass it, so on the real run the recipient, subject and text checks
    see nothing to compare and pass without looking: only the send-key and count checks bite there."""
    checks = [
        check_receipts(reqs, book, intended, max_review),
        check_partition(book, max_failed, max_unknown),
        check_messages(book, mail, require_comparable),
        check_crm(book, crm_rows, require_comparable),
        check_model_calls(model_rows, model_bound, book, require_model_call),
        check_rate_503(reqs, max_503_rate),
        check_drain(reqs, book, drained_ts, max_drain_seconds),
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def render_table(result: Mapping[str, Any]) -> str:
    lines = ["#  result  check", "-  ------  -----"]
    for c in result["checks"]:
        lines.append(f"{c['id']}  {'PASS' if c['ok'] else 'FAIL':<6}  {c['name']}")
        lines.append(f"            {c['detail']}")
        if c["offender_count"]:
            lines.append(
                f"            first {len(c['offenders'])} of {c['offender_count']}: {', '.join(c['offenders'])}"
            )
    lines.append("ALL 7 CHECKS HOLD" if result["ok"] else "AT LEAST ONE CHECK FAILED")
    return "\n".join(lines)


def _intended(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["intended_requests"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise InputError(f"cannot read the driver facts {path.name} (intended_requests): {type(e).__name__}") from e


def _facts_say_live(path: Path) -> bool:
    """True when the driver's facts file records that the run's AI step was the live model (see load_driver.run)."""
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("qualifier") == "live_claude")
    except (OSError, ValueError, AttributeError):
        return False


def _drain_marker(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        value = obj.get("drained_ts")
    except (OSError, ValueError, AttributeError) as e:
        raise InputError(f"cannot read drain marker {path.name}: {type(e).__name__}") from e
    return None if value is None else float(value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="load_tools.check_invariants")
    ap.add_argument("--requests", type=Path, required=True, help="client request log (JSONL)")
    ap.add_argument("--book", type=Path, required=True, help="desk SQLite file (opened read-only)")
    ap.add_argument("--catcher", type=Path, required=True, help="SMTP catcher log (JSONL)")
    ap.add_argument("--crm", type=Path, required=True, help="CRM dump (JSON)")
    ap.add_argument("--model-log", type=Path, required=True, help="fake model call log (JSONL)")
    ap.add_argument(
        "--drain",
        type=Path,
        help='optional drain marker JSON {"drained_ts": epoch seconds}; it can only make the drain later',
    )
    ap.add_argument("--model-bound", type=int, default=DEFAULT_MODEL_CALL_BOUND)
    ap.add_argument("--max-failed", type=int, default=DEFAULT_MAX_FAILED, help="follow-ups allowed to end failed")
    ap.add_argument("--max-unknown", type=int, default=DEFAULT_MAX_UNKNOWN, help="follow-ups allowed to end unknown")
    ap.add_argument("--facts", type=Path, help="driver facts JSON (default: <requests>.facts.json; must exist)")
    ap.add_argument("--max-review", type=int, default=DEFAULT_MAX_REVIEW, help="receipts allowed to end in `review`")
    ap.add_argument(
        "--model-required",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="a sent lead without a model call fails (default: on if the driver's facts file says the AI step was "
        "live, or DESK_QUALIFIER=live_claude is in the checker's environment)",
    )
    ap.add_argument(
        "--require-comparable",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="fail when a message or CRM row cannot be compared with its lead (no recipient, subject, text hash, or "
        "empty CRM record). Default ON: this command reads fake and dry runs, where the catcher and the fake CRM "
        "record all of them. Pass --no-require-comparable only for a log from a real mailbox.",
    )
    ap.add_argument("--max-503-rate", type=float, default=None)
    ap.add_argument("--max-drain-seconds", type=float, default=DEFAULT_MAX_DRAIN_SECONDS)
    ap.add_argument("--json", type=Path, help="write the JSON result here (default: print it after the table)")
    a = ap.parse_args(argv)
    facts_path = a.facts or a.requests.with_name(a.requests.name + ".facts.json")
    if a.model_required is None:
        a.model_required = os.environ.get("DESK_QUALIFIER") == "live_claude" or _facts_say_live(facts_path)
    try:
        intended = _intended(facts_path)
        result = run_checks(
            read_jsonl(a.requests),
            read_book(a.book),
            read_jsonl(a.catcher),
            read_crm(a.crm),
            read_jsonl(a.model_log),
            _drain_marker(a.drain),
            a.model_bound,
            a.max_503_rate,
            a.max_drain_seconds,
            a.max_failed,
            intended,
            a.max_review,
            a.model_required,
            a.max_unknown,
            a.require_comparable,
        )
    except InputError as e:
        print(f"INPUT ERROR: {e}", file=sys.stderr)
        return EXIT_INPUT
    print(render_table(result))
    text = json.dumps(result, indent=2, sort_keys=True)
    if a.json:
        a.json.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
