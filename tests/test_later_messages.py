"""Later messages in the lead panel (read only; claims P7 to P9). Made-up data only.
A later message is a request with a new request number and an email address the desk already has (verdict duplicate)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from support import CrashAt, lead, lead_ids_containing, run_threads
from test_portal_api import BURST, fixture, fresh, get, same_shape

from lead_desk.constants import COMPANY_MAX, DUPLICATES_MAX, MESSAGE_MAX, NAME_MAX, PHONE_MAX, SOURCE_REF_MAX
from lead_desk.contract import SimulatedCrash


def _detail(base: str, lead_id: str) -> dict[str, Any]:
    status, out = get(base, f"/api/ui/leads/{lead_id}")
    assert status == 200
    return out  # type: ignore[no-any-return]


def _this(item: dict[str, Any]) -> dict[str, str]:
    return {x["field"]: x["this"] for x in item["differing"]}


def test_later_values_word_for_word(env) -> None:
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    raw = {
        "name": "  Ada  Lovelace ",
        "phone": " +44 20 7946 0958 ",
        "company": "ÉXAMPLE gmbh",
        "message": "\t\n Hello  there,\nline two \U0001f469‍\U0001f4bb Ünï \r\n",
    }
    assert rig.desk.handle("webhook", "w-1", lead(**raw)).status == 200
    got = _this(_detail(base, first)["duplicates"][0])
    assert got == {
        "name": "Ada  Lovelace",
        "phone": "+44 20 7946 0958",
        "company": "ÉXAMPLE gmbh",
        "message": "Hello  there,\nline two \U0001f469‍\U0001f4bb Ünï",
    }
    assert all(got[k] == raw[k].strip() for k in raw)


def test_later_seconds_after_first(env) -> None:
    rig, base = fresh(env)
    t = rig.clock.t
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    rig.clock.t = t + 14
    rig.desk.handle("webhook", "w-1", lead(phone="555 0101"))
    rig.clock.t = t + 7380
    rig.desk.handle("webhook", "w-2", lead(phone="555 0102"))
    assert [d["seconds_after_first"] for d in _detail(base, first)["duplicates"]] == [7380, 14]


def test_later_lower_receipt_number(env) -> None:
    """Receipt and verdict are two steps, so a later message can hold a LOWER receipt number than the first record."""
    rig, base = fresh(env, hooks=CrashAt("A"))
    with pytest.raises(SimulatedCrash):
        rig.desk.handle("webhook", "w-1", lead(phone="555 0111"))  # receipt 1 written, no verdict
    rig.clock.advance(5)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]  # receipt 2 creates the lead
    assert rig.desk.handle("webhook", "w-1", lead(phone="555 0111")).status == 200  # the replay decides receipt 1
    out = _detail(base, first)
    item = out["duplicates"][-1]
    assert item["receipt_id"] < out["first_receipt"]["receipt_id"] and item["seconds_after_first"] == -5


def test_later_order_by_receipt_number(env) -> None:
    rig, base = fresh(env)
    t = rig.clock.t
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    for ref, at in (("w-1", t + 100), ("w-2", t + 200), ("w-3", t - 10)):
        rig.clock.t = at
        rig.desk.handle("webhook", ref, lead(phone="555 01" + ref[-1] + "0"))
    out = _detail(base, first)
    assert [d["source_ref"] for d in out["duplicates"]] == ["w-3", "w-2", "w-1"]
    assert out["duplicates"][0]["seconds_after_first"] == -10


def test_later_burst_ten_each_own_values(env) -> None:
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    res = run_threads(
        [
            (lambda i=i: rig.desk.handle("webhook", f"b-{i}", lead(phone=f"555 05{i:02d}", message=f"msg {i}")))
            for i in range(BURST)
        ]
    )
    assert all(getattr(r, "status", None) == 200 for r in res), res
    out = _detail(base, first)
    ids = [d["receipt_id"] for d in out["duplicates"]]
    assert len(ids) == BURST == out["duplicates_total"] and ids == sorted(ids, reverse=True)
    for d in out["duplicates"]:
        i = int(d["source_ref"].split("-")[1])
        assert _this(d) == {"phone": f"555 05{i:02d}", "message": f"msg {i}"}


def test_later_first_record_untouched(env) -> None:
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    lead_sql, rec_sql = "SELECT * FROM leads WHERE lead_id=?", "SELECT * FROM receipts WHERE receipt_id=1"
    before = rig.query(lead_sql, (first,)), rig.query(rec_sql)

    def other(i: int) -> dict[str, Any]:
        return lead(name=f"N{i}", phone=f"555 02{i:02d}", company=f"C{i}", message=f"M{i}")

    for i in range(5):
        rig.desk.handle("webhook", f"s-{i}", other(i))
    run_threads([(lambda i=i: rig.desk.handle("webhook", f"t-{i}", other(i + 5))) for i in range(5)])
    assert (rig.query(lead_sql, (first,)), rig.query(rec_sql)) == before
    out = _detail(base, first)
    assert out["duplicates_total"] == 10 and out["lead"]["name"] == "Ada Example" and out["lead"]["phone"] == "555 0100"


def test_later_messages_never_call_the_ai(env, monkeypatch) -> None:
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    rig.desk.run_followup(first)
    assert rig.qualifier.calls == 1
    seen: list[str] = []

    def boom(*_a: Any, **_k: Any) -> Any:
        seen.append("called")
        raise AssertionError("the AI step ran for a later message")

    monkeypatch.setattr(rig.qualifier, "qualify", boom)
    for i in range(BURST):
        rig.desk.handle("webhook", f"w-{i}", lead(phone=f"555 03{i:02d}", message=f"hi {i}"))
    assert _detail(base, first)["duplicates_total"] == BURST
    assert seen == [] and rig.qualifier.calls == 1


def test_later_reply_keys_are_fixed(env) -> None:
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    rig.desk.handle("webhook", "w-1", lead(phone="555 0141", message="a\nb"))
    rig.desk.handle("webhook", "w-2", lead())
    out = _detail(base, first)
    fix = fixture("lead-detail-later")
    assert set(out) == set(fix)
    for item in out["duplicates"]:
        assert set(item) == set(fix["duplicates"][0])
        assert all(set(e) == {"field", "first", "this"} for e in item["differing"])
    assert same_shape(out, fix)


def test_later_reply_size_is_bounded(env) -> None:
    rig, base = fresh(env)
    one, two = "\U0001f600", "\U0001f601"

    def full(ch: str) -> dict[str, Any]:
        return lead(name=ch * NAME_MAX, phone=ch * PHONE_MAX, company=ch * COMPANY_MAX, message=ch * MESSAGE_MAX)

    first = rig.desk.handle("form", "f-1", full(one)).body["lead_id"]
    for i in range(DUPLICATES_MAX):
        # a request number of SOURCE_REF_MAX emoji, each one its own request (the last two characters count up)
        reply = rig.desk.handle("webhook", two * (SOURCE_REF_MAX - 2) + f"{i:02d}", full(two))
        assert reply.status == 200, reply.body
    out = _detail(base, first)
    assert len(out["duplicates"]) == DUPLICATES_MAX
    size = len(json.dumps(out["duplicates"], allow_nan=False).encode())  # ensure_ascii on, as the server writes
    bound = DUPLICATES_MAX * (2 * (NAME_MAX + PHONE_MAX + COMPANY_MAX + MESSAGE_MAX) * 12 + 4096)
    assert bound == 3_132_800 and 2_800_000 <= size <= bound, size
    hostile = '<img src=x onerror=alert(1)></script>javascript:"\\ \U0001f600'
    rig.desk.handle("webhook", "h-1", lead(message=hostile))
    item = next(d for d in _detail(base, first)["duplicates"] if d["source_ref"] == "h-1")
    assert _this(item)["message"] == hostile


def test_later_review_verdict_not_listed(env) -> None:
    """Documents the 'not claimed' line: a later request that fails a check has no lead and is not listed."""
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    rig.desk.handle("webhook", "w-1", lead(phone=5))  # not text: verdict review
    assert _detail(base, first)["duplicates_total"] == 0


def test_later_repeat_still_listed(env) -> None:
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead()).body["lead_id"]
    rig.desk.handle("webhook", "w-1", lead(phone="555 0150"))
    rig.desk.handle("webhook", "w-2", lead(phone="555 0150"))
    assert [_this(d) for d in _detail(base, first)["duplicates"]] == [{"phone": "555 0150"}] * 2


# -- masking and logs (claim P10)
def test_leads_email_search_needs_reveal(env) -> None:
    with pytest.MonkeyPatch.context() as mp:  # (called by name from the mutant tests, so no fixture argument)
        lead_ids_containing(mp, "ada0007")  # every lead number spells ada: the probes must not depend on chance
        rig, base = fresh(env)
        rig.desk.handle("form", "ref-ada@example.test", lead(name="Zed Other", email="zed@example.test"))
        rig.desk.handle("form", "ui-1", lead())
        both = "/api/ui/leads?q="

        def count(q: str, extra: str = "") -> tuple[int, int]:
            out = get(base, f"{both}{q}{extra}")[1]
            return out["total"], len(out["rows"])

        assert (
            count("ada%40example")[0] == 2 and count("ada%40example", "&by_email=1")[0] == 2
        )  # the desk answers (default)
        assert count("ada%40example", "&by_email=0") == (0, 0)  # masked search: like a term that matches nothing
        # a name is not searched while masked (no probing for a name); the probe has a space: no lead number holds one
        assert count("Ada%20Ex", "&by_email=0") == (0, 0)
        assert count("Ada%20Ex", "&by_email=1")[0] == 1  # with names shown it matches (the lead named Ada Example)
        assert count("ref-ada", "&by_email=0") == (0, 0)  # a request number with '@' is not searched either
        assert count("ref-ada", "&by_email=1")[0] == 1
        assert count("ui-1", "&by_email=0") == (0, 0)  # no request number is searched while masked (it may hold a name)
        assert count("ui-1", "&by_email=1")[0] == 1
        assert count("Zed", "&by_email=0") == (0, 0)
        for bad in ("&by_email=2", "&by_email=", "&by_email=1&by_email=1"):
            status, out = get(base, f"{both}Ada{bad}")
            assert status == 400 and out["field"] == "by_email", bad


SAFE_LOG_NAMES = {  # ids and counts only, never a request value
    "receipt_id": "the desk's own receipt number",
    "failures": "a count",
    "cap": "a count",
    "kind": "the name of a call kind (ai, crm, mail)",
    "what": "a fixed label of the caller",
    "changed": "a count",
    "value": "a count of rows (0 or 1)",
    "phase": "a fixed phase name",
    "soft": "a number",
    "need": "a number",
    "max_threads": "a number",
}
REVIEWED_TRACEBACKS: dict[str, str] = {}  # file:line -> reason; empty: no log call may print a traceback


def _log_calls() -> list[tuple[str, int, Any]]:
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "lead_desk"
    calls = []
    for path in sorted(src.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            f = getattr(node, "func", None)
            if (
                isinstance(node, ast.Call)
                and isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "log"
                and f.attr in ("debug", "info", "warning", "error", "critical", "exception")
            ):
                calls.append((path.name, node.lineno, node))
    return calls


def test_log_calls_carry_no_values() -> None:
    import ast

    calls = _log_calls()
    assert len(calls) > 40  # the walk really found the desk's log calls
    bad = []
    for name, line, node in calls:
        where = f"{name}:{line}"
        if node.func.attr == "exception" or any(k.arg == "exc_info" for k in node.keywords):
            if where not in REVIEWED_TRACEBACKS:
                bad.append(f"{where}: traceback")
        if not (isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            bad.append(f"{where}: the message is not a literal")
        for a in node.args[1:]:
            ok = (
                isinstance(a, ast.Constant)
                or (isinstance(a, ast.Name) and a.id in SAFE_LOG_NAMES)
                or (isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "len")
                or (
                    isinstance(a, ast.Attribute)
                    and a.attr == "__name__"
                    and isinstance(a.value, ast.Call)
                    and isinstance(a.value.func, ast.Name)
                    and a.value.func.id == "type"
                )
            )
            if not ok:
                bad.append(f"{where}: {ast.unparse(a)}")
    assert not bad, bad
    used = {a.id for _, _, n in calls for a in n.args[1:] if isinstance(a, ast.Name)}
    assert used <= set(SAFE_LOG_NAMES) and set(SAFE_LOG_NAMES) <= used, (
        set(SAFE_LOG_NAMES) ^ used
    )  # the list stays exact


def test_later_messages_log_no_values(env, monkeypatch, caplog, capfd) -> None:
    import logging
    import sqlite3

    from lead_desk import portal_api

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="lead_desk")
    email, phone, message = "canary-" + "7731" + "@example.test", "+1 555 " + "0177 31", "canary message " + "7731"
    rig, base = fresh(env)
    first = rig.desk.handle("form", "f-1", lead(email=email)).body["lead_id"]
    for i in range(BURST):
        rig.desk.handle("webhook", f"w-{i}", lead(email=email, phone=phone, message=f"{message} {i}"))
    assert _detail(base, first)["duplicates_total"] == BURST

    # a failing read whose error text carries the address: the log may name the error class, not its text
    def broken(_desk: Any) -> Any:
        raise sqlite3.OperationalError(portal_api._NOT_READY_MESSAGES[0] + " " + email)

    monkeypatch.setattr(portal_api, "_snapshot", broken)
    assert get(base, f"/api/ui/leads?q={email.replace('@', '%40')}&by_email=1")[0] == 503
    out, err = capfd.readouterr()
    seen = caplog.text + out + err
    assert "portal read failed" in seen  # the failing read really logged
    for value in (email, email.split("@")[0], phone, message, "7731"):
        assert value not in seen, value


# -- the sender address (current setting) in the data map
class _SenderOutbox:
    """Delegates to the rig's outbox and adds the configured From address of a mail step that has one."""

    sender_address = "sales@example.test"

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_data_map_mail_sender(env, monkeypatch) -> None:
    from lead_desk.smtp_outbox import SmtpOutbox, SmtpSettings

    rig, base = fresh(env)
    assert get(base, "/api/ui/data-map")[1]["mail_sender"] is None  # the HTTP stand-in has none
    monkeypatch.setattr(rig.desk, "outbox", _SenderOutbox(rig.desk.outbox))
    assert get(base, "/api/ui/data-map")[1]["mail_sender"] == "sales@example.test"
    assert SmtpOutbox(SmtpSettings(mail_from="sales@example.test")).sender_address == "sales@example.test"


def test_sender_is_masked_by_default_in_the_source() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "src/lead_desk/portal/pages/leads.js").read_text(encoding="utf-8")
    assert _sender_masked(text)


def _sender_masked(text: str) -> bool:
    return (
        "P.ui.guarded('email', mailSender, P.reveal.key('sender', mailSender)," in text
        and "Sent from (current setting)" in text
    )
