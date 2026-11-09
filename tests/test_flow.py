"""Desk tests that drive the Desk class in process. Made-up data only.

Bodies that the mutation guards re-run (cross_source_dupe_body, retry_twice_body) are plain functions here; the guards
import them from this module. Their key assertions carry explicit messages so the guards can match on the text."""

from __future__ import annotations

import sqlite3

import pytest
from support import CountingCrm, CrashAt, Rig, call, lead, post_lead, run_threads, wait_until

from lead_desk.contract import SimulatedCrash
from lead_desk.gate import SEND_MAX_ATTEMPTS

SUBJECT = "Thanks for getting in touch, {name}"
HINT_R = "Send a corrected submission with a NEW request number."
HINT_D = "Informational only, no action needed."
HINT_O = "Send the same request number again, or restart the desk."


# -- test_cross_source_dupe
def cross_source_dupe_body(rig: Rig) -> None:
    """The same person through the form door and the webhook door: one lead, one email, one CRM row."""
    desk = rig.desk
    first = desk.handle("form", "sub-0001", lead())
    assert first.status == 201
    lead_id = first.body["lead_id"]
    second = desk.handle("webhook", "msg-0001", lead(email="  ADA@" + "Example.test ", phone="555 0199"))
    n = rig.leads()
    assert n == 1, f"leads == 1 expected, got {n}"  # the guard matches on this text
    assert second.status == 200
    assert second.body["verdict"] == "duplicate"
    assert second.body["lead_id"] == lead_id
    assert second.body["differing_fields"] == ["phone"]
    assert second.body["replayed"] is False
    assert rig.outbox.count == 1
    assert rig.crm.count == 1
    msg = rig.outbox.messages[0]
    assert msg["key"] == lead_id
    assert msg["to"] == "Ada@Example.test"  # the address as typed in the first submission
    assert msg["subject"] == SUBJECT.format(name="Ada Example")
    rec = rig.crm.rows[lead_id]
    assert rec["lead_id"] == lead_id
    assert rec["source"] == "form"
    assert rec["ai_label"] == "warm"
    assert rig.state(lead_id)[0] == "sent"


def test_cross_source_dupe(env) -> None:
    cross_source_dupe_body(env.rig())


# -- test_dupe_diff_first_untouched
def test_dupe_diff_first_untouched(env) -> None:
    rig = env.rig()
    d = rig.desk
    first = d.handle("form", "sub-1", lead())
    lead_id = first.body["lead_id"]
    second = d.handle("webhook", "msg-1", lead(name="Ada E.", phone="555 0199", message="Call after 3 pm."))
    assert second.status == 200
    assert second.body["differing_fields"] == ["name", "phone", "message"]  # fixed order
    stored = d.lead(lead_id).body["lead"]
    assert stored["name"] == "Ada Example"
    assert stored["phone"] == "555 0100"
    assert stored["company"] == "Example Ltd"
    assert stored["message"] == "Please call me."
    assert stored["source"] == "form"
    assert "555 0199" in rig.query("SELECT payload FROM receipts WHERE source_ref='msg-1'")[0][0]
    # spaces collapsed, empty new value is not a difference, comparison is case-sensitive
    third = d.handle("form", "sub-3", lead(name="  Ada    Example ", company="example ltd", phone=""))
    assert third.body["differing_fields"] == ["company"]
    same = d.handle("form", "sub-4", lead())
    assert same.status == 200
    assert same.body["differing_fields"] == []
    assert rig.leads() == 1
    assert rig.outbox.count == 1


# -- test_replay_same_source_ref
def test_replay_same_source_ref(env) -> None:
    rig = env.rig()
    d = rig.desk
    first = d.handle("form", "sub-1", lead())
    assert first.status == 201
    assert first.body["replayed"] is False
    for _ in range(20):
        r = d.handle("form", "sub-1", lead())
        assert r.status == 201
        assert r.body["replayed"] is True
        assert r.body["payload_matches"] is True
        assert r.body["verdict"] == "new"
        assert r.body["lead_id"] == first.body["lead_id"]
        assert r.body["receipt_id"] == first.body["receipt_id"]
    other = d.handle("form", "sub-1", lead(name="Someone Else", email="other@example.test"))
    assert other.status == 201
    assert other.body["replayed"] is True
    assert other.body["payload_matches"] is False
    assert other.body["lead_id"] == first.body["lead_id"]
    assert rig.leads() == 1
    assert rig.outbox.count == 1
    assert rig.query("SELECT attempts FROM receipts")[0][0] == 22  # the counter moved; no trigger fired
    # a review verdict and a duplicate verdict replay with their own status and body
    first = d.handle("form", "sub-2", lead(email=None))
    second = d.handle("form", "sub-2", lead(email=None))
    assert (first.status, second.status) == (202, 202)
    assert second.body["replayed"] is True
    assert second.body["reason"] == first.body["reason"] == "incomplete:email"
    dup1 = d.handle("webhook", "msg-1", lead(phone="555 0199"))
    dup2 = d.handle("webhook", "msg-1", lead(phone="555 0199"))
    assert (dup1.status, dup2.status) == (200, 200)
    assert dup2.body["replayed"] is True
    assert dup2.body["differing_fields"] == dup1.body["differing_fields"] == ["phone"]

    # replay at point B drives the follow-up exactly once
    rig = env.rig(hooks=CrashAt("B"))
    d = rig.desk
    with pytest.raises(SimulatedCrash):
        d.handle("form", "sub-9", lead(email="b@example.test"))
    assert rig.state()[0] == "pending"
    assert rig.outbox.count == 0
    r = d.process("form", "sub-9", lead(email="b@example.test"))
    assert r.body["replayed"] is True
    assert r.drive_lead_id == r.body["lead_id"]  # a replay of `new` whose follow-up is still pending drives it
    d.run_followup(r.drive_lead_id)
    assert rig.outbox.count == 1
    assert rig.state()[0] == "sent"
    again = d.process("form", "sub-9", lead(email="b@example.test"))
    assert again.drive_lead_id is None
    d.run_followup(r.drive_lead_id)  # a second drive of a sent follow-up changes nothing
    assert rig.outbox.count == 1
    assert rig.leads() == 1


# -- test_concurrent_same_source_ref
def test_concurrent_same_source_ref(env) -> None:
    rig = env.rig()
    base = env.serve(rig)
    rounds = 5
    for i in range(rounds):
        payload = lead(email=f"c{i}@example.test")
        res = run_threads([lambda p=payload, i=i: post_lead(base, "form", f"sub-{i}", p) for _ in range(2)])
        assert all(isinstance(x, tuple) for x in res), res
        assert [x[0] for x in res] == [201, 201], res  # no 500, no 503
        assert sorted(x[1]["replayed"] for x in res) == [False, True]  # one decides, the other replays
        assert res[0][1]["lead_id"] == res[1][1]["lead_id"]
        assert res[0][1]["receipt_id"] == res[1][1]["receipt_id"]
    wait_until(lambda: call(base, "GET", "/counts")[1]["pending_followups"] == 0, what="follow-ups finished")
    assert rig.leads() == rounds
    assert rig.outbox.count == rounds  # the follow-up ran once per lead although two requests could drive it


# -- test_write_once_triggers
def test_write_once_triggers(env) -> None:
    rig = env.rig()
    d = rig.desk
    first = d.handle("form", "sub-1", lead())
    lead_id = first.body["lead_id"]
    before = rig.query("SELECT attempts FROM receipts")[0][0]
    again = d.handle("form", "sub-1", lead())  # raises attempts; must not trip receipts_immutable
    assert again.status == 201
    assert rig.query("SELECT attempts FROM receipts")[0][0] == before + 1
    con = sqlite3.connect(rig.db_path, isolation_level=None)
    con.execute("PRAGMA recursive_triggers=ON")  # REPLACE only fires the delete trigger with this on
    con.execute("PRAGMA foreign_keys=ON")
    try:

        def refused(sql: str, text: str) -> None:
            with pytest.raises(sqlite3.DatabaseError, match=text):
                con.execute(sql)

        refused("UPDATE receipts SET payload='{}'", "never change")
        refused("UPDATE receipts SET source_ref='x'", "never change")
        refused("UPDATE receipts SET received_at=1", "never change")
        refused("UPDATE receipts SET outcome='review'", "written once")
        refused("UPDATE receipts SET outcome_reason='x', decided_at=1", "written once")
        refused("DELETE FROM receipts", "never deleted")
        refused(
            "INSERT OR REPLACE INTO receipts(source, source_ref, received_at, payload) VALUES('form','sub-1',1,'{}')",
            "never deleted",
        )
        refused("UPDATE leads SET name='x'", "write-once")
        refused("DELETE FROM leads", "write-once")
        refused(
            "INSERT OR REPLACE INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at) "
            f"VALUES('{lead_id}', 1, 'form', 'n', 'e@example.test', 'e@example.test', 1)",
            "write-once",
        )
        refused("UPDATE followups SET state='failed'", "final")  # the follow-up is `sent` by now
        refused("DELETE FROM followups", "never deleted")
        refused(
            f"INSERT OR REPLACE INTO followups(lead_id, state, updated_at) VALUES('{lead_id}', 'pending', 1)",
            "never deleted",
        )
        refused(  # a `new` verdict always names its lead: no partial verdict
            "INSERT INTO receipts(source, source_ref, received_at, payload, outcome, decided_at)"
            " VALUES('form', 'sub-partial', 1, '{}', 'new', 1)",
            "CHECK",
        )
    finally:
        con.close()
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 1
    assert rig.leads() == 1
    assert rig.query("SELECT name FROM leads")[0][0] == "Ada Example"
    assert rig.state(lead_id)[0] == "sent"


# -- test_dedupe_before_ai
def test_dedupe_before_ai(env) -> None:
    rig = env.rig(replies={"ada@example.test": "hot"})
    d = rig.desk
    first = d.handle("form", "sub-1", lead())
    lead_id = first.body["lead_id"]
    assert rig.qualifier.calls == 1
    assert d.lead(lead_id).body["followup"]["ai_label"] == "hot"
    assert rig.crm.rows[lead_id]["ai_label"] == "hot"
    d.handle("webhook", "msg-1", lead(phone="555 0199"))  # duplicate
    d.handle("form", "sub-1", lead())  # replay
    d.handle("form", "sub-2", lead(name=None))  # review
    assert rig.qualifier.calls == 1  # the AI step ran once, only for the new lead
    other = d.handle("form", "sub-3", lead(email="grace@example.test"))
    assert rig.qualifier.calls == 2
    assert d.lead(other.body["lead_id"]).body["followup"]["ai_label"] == "warm"  # no entry: the default
    assert rig.outbox.count == 2


# -- test_incomplete_to_review
ZERO_WIDTH = chr(0x200B)
CASES: list[tuple[str, dict[str, object], str]] = [
    ("no name", {"email": "a1@example.test"}, "incomplete:name"),
    ("blank name", {"name": "   ", "email": "a2@example.test"}, "incomplete:name"),
    ("no email", {"name": "Ada"}, "incomplete:email"),
    ("blank email", {"name": "Ada", "email": "  "}, "incomplete:email"),
    ("neither", {"company": "Example Ltd"}, "incomplete:name,email"),
    ("name and phone only", {"name": "Ada", "phone": "555 0100"}, "incomplete:email"),
    ("no at sign", {"name": "Ada", "email": "not-an-email"}, "invalid_email"),
    ("no dot in domain", {"name": "Ada", "email": "a@b"}, "invalid_email"),
    ("space inside", {"name": "Ada", "email": "a b@example.test"}, "invalid_email"),
    ("ends in dot", {"name": "Ada", "email": "a@example.test."}, "invalid_email"),
    ("zero width char", {"name": "Ada", "email": f"ada{ZERO_WIDTH}@example.test"}, "invalid_email"),
    ("email too long", {"name": "Ada", "email": "a" * 250 + "@x.test"}, "invalid_email"),
    ("name wrong type", {"name": 123, "email": "a3@example.test"}, "bad_field:name"),
    ("email wrong type", {"name": "Ada", "email": 5}, "bad_field:email"),
    ("phone wrong type", {"name": "Ada", "email": "a4@example.test", "phone": 5}, "bad_field:phone"),
    ("company wrong type", {"name": "Ada", "email": "a5@example.test", "company": ["x"]}, "bad_field:company"),
    ("message wrong type", {"name": "Ada", "email": "a6@example.test", "message": {}}, "bad_field:message"),
    ("name too long", {"name": "n" * 201, "email": "a7@example.test"}, "bad_field:name"),
    ("phone too long", {"name": "Ada", "email": "a8@example.test", "phone": "5" * 41}, "bad_field:phone"),
    ("message too long", {"name": "Ada", "email": "a9@example.test", "message": "m" * 2001}, "bad_field:message"),
    ("types before completeness", {"name": 123}, "bad_field:name"),
    ("first bad field wins", {"name": "Ada", "email": "b1@example.test", "phone": 5, "company": 5}, "bad_field:phone"),
]


def test_incomplete_to_review(env) -> None:
    rig = env.rig()
    d = rig.desk
    for i, (label, payload, reason) in enumerate(CASES):
        r = d.handle("form", f"sub-{i}", payload)
        assert (r.status, r.body["verdict"], r.body["reason"]) == (202, "review", reason), label
        assert "lead_id" not in r.body, label
    assert rig.leads() == 0
    assert rig.outbox.count == 0
    assert rig.qualifier.calls == 0
    # an invalid submission goes to review even if its email matches an existing lead
    ok = d.handle("form", "sub-ok", lead())
    assert ok.status == 201
    for i, (payload, reason) in enumerate(
        [
            (lead(name=None), "incomplete:name"),
            (lead(phone=123), "bad_field:phone"),
            (lead(company="c" * 201), "bad_field:company"),
        ]
    ):
        r = d.handle("webhook", f"msg-{i}", payload)
        assert (r.status, r.body["verdict"], r.body["reason"]) == (202, "review", reason)
    assert rig.leads() == 1
    assert rig.outbox.count == 1
    items = d.review().body
    assert sum(1 for x in items if x["kind"] == "review") == len(CASES) + 3
    counts = d.counts(None).body
    assert counts["review"] == len(CASES) + 3
    assert counts["new"] == 1
    assert counts["duplicate"] == 0


# -- test_crash_resume_each_point
# (emails after restart, follow-up state, last_error or None = not asserted)
EXPECT: dict[tuple[str, str], tuple[int, str, str | None]] = {
    ("A", "keyed"): (1, "sent", None),
    ("A", "blind"): (1, "sent", None),
    ("A2", "keyed"): (1, "sent", None),
    ("A2", "blind"): (1, "sent", None),
    ("B", "keyed"): (1, "sent", None),
    ("B", "blind"): (1, "sent", None),
    ("D1", "keyed"): (1, "sent", None),
    ("D1", "blind"): (1, "sent", None),  # killed before the send mark: nothing left, the sweep puts it back and sends
    ("D2", "keyed"): (1, "sent", None),
    ("D2", "blind"): (1, "unknown", "crash_while_sending"),
}


@pytest.mark.parametrize("crm", ["keyed", "blind"])
@pytest.mark.parametrize("kind", ["keyed", "blind"])
@pytest.mark.parametrize("point", ["A", "A2", "B", "D1", "D2"])
def test_crash_resume_each_point(env, point: str, kind: str, crm: str) -> None:
    hook = CrashAt(point)
    rig = env.rig(outbox=kind, hooks=hook, crm=CountingCrm(crm))
    with pytest.raises(SimulatedCrash):
        rig.desk.handle("form", "sub-1", lead())
    assert hook.fired == [point]  # the point really was reached
    rig.restart()  # a new Desk on the same file: the start-up sweep
    emails, state, err = EXPECT[(point, kind)]
    assert rig.outbox.count == emails, f"{point} {kind}: emails"
    assert rig.leads() == 1
    assert rig.crm.count == 1
    assert len(rig.crm.calls) <= 1, f"{point} {kind} {crm}: the CRM was asked {len(rig.crm.calls)} times"
    got_state, got_err = rig.state()
    assert got_state == state
    if err:
        assert got_err == err
    # the caller's retry of the same request id: replay, no second lead, no second email
    r = rig.desk.handle("form", "sub-1", lead())
    assert r.body["replayed"] is True
    assert r.body["verdict"] == "new"
    assert rig.leads() == 1
    assert rig.outbox.count == emails
    if state == "unknown":
        kinds = [x["kind"] for x in rig.desk.review().body]
        assert "unknown" in kinds


# (point, outbox, crm) -> (emails, CRM rows, state, last_error). Whatever the point: never more than one email and one
# CRM row, and a state a person can read.
EXPECT_MORE = {
    ("L", "keyed", "keyed"): (1, 1, "sent", None),
    ("L", "keyed", "blind"): (1, 1, "sent", None),
    ("L", "blind", "keyed"): (1, 1, "sent", None),
    ("L", "blind", "blind"): (1, 1, "sent", None),
    # C0: the intent mark is written, the put was never called. A keyed CRM is simply asked; a blind one is not asked
    # again after a crash (a put may have started), so a person looks and the CRM may hold no row at all.
    ("C0", "keyed", "keyed"): (1, 1, "sent", None),
    ("C0", "keyed", "blind"): (0, 0, "unknown", "crm_unconfirmed"),
    ("C0", "blind", "keyed"): (1, 1, "sent", None),
    ("C0", "blind", "blind"): (0, 0, "unknown", "crm_unconfirmed"),
    ("C1", "keyed", "keyed"): (1, 1, "sent", None),  # a keyed CRM is simply asked again with the same key
    ("C1", "keyed", "blind"): (0, 1, "unknown", "crm_unconfirmed"),  # a blind CRM is never asked twice: a person looks
    ("C1", "blind", "keyed"): (1, 1, "sent", None),
    ("C1", "blind", "blind"): (0, 1, "unknown", "crm_unconfirmed"),
    ("D1b", "keyed", "keyed"): (1, 1, "sent", None),  # the send mark was written, nothing left: a keyed sender goes on
    ("D1b", "keyed", "blind"): (1, 1, "sent", None),
    ("D1b", "blind", "keyed"): (0, 1, "unknown", "crash_while_sending"),  # a blind sender is never asked again
    ("D1b", "blind", "blind"): (0, 1, "unknown", "crash_while_sending"),
    ("D3", "keyed", "keyed"): (1, 1, "sent", None),
    ("D3", "keyed", "blind"): (1, 1, "sent", None),
    ("D3", "blind", "keyed"): (1, 1, "sent", None),
    ("D3", "blind", "blind"): (1, 1, "sent", None),
}


@pytest.mark.parametrize("crm", ["keyed", "blind"])
@pytest.mark.parametrize("kind", ["keyed", "blind"])
@pytest.mark.parametrize("point", ["L", "C0", "C1", "D1b", "D3"])
def test_crash_resume_after_label_crm_send_mark_and_result(env, point: str, kind: str, crm: str) -> None:
    """The moments the first five points leave out: the AI label stored (L), the CRM intent mark written but no put
    yet (C0), the CRM put came back but crm_done=1 is not written (C1), the send mark written but nothing sent (D1b),
    the result recorded (D3). CountingCrm logs every put, so a double put shows: on a blind CRM as a second stored
    row, on a keyed CRM as a third put call (only C1 may legitimately ask a keyed CRM twice)."""
    hook = CrashAt(point)
    rig = env.rig(outbox=kind, hooks=hook, crm=CountingCrm(crm))
    with pytest.raises(SimulatedCrash):
        rig.desk.handle("form", "sub-1", lead())
    assert hook.fired == [point]
    rig.restart()
    emails, crm_rows, state, err = EXPECT_MORE[(point, kind, crm)]
    assert (rig.outbox.count, rig.crm.count) == (emails, crm_rows)
    allowed_puts = 2 if (crm == "keyed" and point == "C1") else 1
    assert len(rig.crm.calls) <= allowed_puts, f"{point} {kind} {crm}: double CRM put {rig.crm.calls}"
    assert len({lid for lid, _ in rig.crm.calls}) <= 1
    got_state, got_err = rig.state()
    assert got_state == state and (err is None or got_err == err)
    assert rig.leads() == 1
    again = rig.desk.handle("form", "sub-1", lead())  # the caller's retry: a replay, nothing doubled
    assert again.body["replayed"] is True and again.body["verdict"] == "new"
    assert (rig.leads(), rig.outbox.count, rig.crm.count) == (1, emails, crm_rows)
    assert len(rig.crm.calls) <= allowed_puts  # the replay asked the CRM nothing more


# -- test_unknown_blind
def test_unknown_blind(env) -> None:
    rig = env.rig(outbox="blind")
    d, ob = rig.desk, rig.outbox
    ob.fail_next("record_then_drop")  # sent, reply lost
    id1 = d.handle("form", "s1", lead(email="u1@example.test")).body["lead_id"]
    assert rig.state(id1) == ("unknown", "ambiguous")
    assert ob.count == 1
    assert any(x["item_id"] == "F" + id1 and x["kind"] == "unknown" for x in d.review().body)
    r = d.retry("F" + id1)
    assert (r.status, r.body["error"]) == (409, "unknown_needs_person")
    assert "hint" not in r.body
    res = d.resolve("F" + id1, "sent")
    assert (res.status, res.body) == (200, {"item_id": "F" + id1, "state": "sent"})
    assert rig.state(id1) == ("sent", "person: sent")
    assert d.lead(id1).body["followup"]["sent_at"] is not None
    assert ob.count == 1
    assert not any(x["item_id"] == "F" + id1 for x in d.review().body)
    again = d.resolve("F" + id1, "sent")
    assert (again.status, again.body["error"]) == (409, "not_unknown")
    # "not sent", and it really was not sent: Retry then sends once
    ob.fail_next("drop")  # never arrived
    id2 = d.handle("form", "s2", lead(email="u2@example.test")).body["lead_id"]
    assert rig.state(id2)[0] == "unknown"
    assert ob.count == 1
    res = d.resolve("F" + id2, "not_sent")
    assert (res.status, res.body) == (200, {"item_id": "F" + id2, "state": "failed"})
    assert rig.state(id2) == ("failed", "person: not sent")
    assert d.retry("F" + id2).body == {"item_id": "F" + id2, "state": "sent"}
    assert ob.count == 2
    # the one case where a person's answer can cause a second email:
    # a WRONG "not sent" on a blind outbox sends a second email, and that is what we assert
    ob.fail_next("record_then_drop")
    id3 = d.handle("form", "s3", lead(email="u3@example.test")).body["lead_id"]
    assert rig.state(id3)[0] == "unknown"
    assert ob.keys().count(id3) == 1
    d.resolve("F" + id3, "not_sent")  # wrong: the email had gone out
    assert d.retry("F" + id3).body["state"] == "sent"
    assert ob.keys().count(id3) == 2


def fail_until_the_cap(rig: Rig, lid: str, mode: str, err: str) -> None:
    """The first drive of `lid` just failed with `mode`: the follow-up waits as `pending` (a refused, unreachable or
    unclear send is tried again by the reaper after 1, 2, 4, 8 s). Make every further try fail the same way until
    the cap of SEND_MAX_ATTEMPTS tries in all; only then is it `failed`."""
    assert rig.state(lid) == ("pending", err)
    for _ in range(SEND_MAX_ATTEMPTS - 1):
        rig.outbox.fail_next(mode)
        rig.clock.advance(20)  # longer than the longest wait between tries
        rig.desk.reap_once()
    assert rig.state(lid) == ("failed", err)


# -- test_failed_retry
def test_failed_retry(env) -> None:
    rig = env.rig(outbox="keyed")
    d, ob = rig.desk, rig.outbox
    sent = 0
    for i, (mode, err) in enumerate([("refuse", "refused"), ("unreachable", "unreachable"), ("drop", "ambiguous")]):
        ob.fail_next(mode)
        lid = d.handle("form", f"s{i}", lead(email=f"f{i}@example.test")).body["lead_id"]
        fail_until_the_cap(rig, lid, mode, err)
        assert ob.count == sent  # nothing was stored
        assert any(x["item_id"] == "F" + lid and x["kind"] == "failed" for x in d.review().body)
        res = d.retry("F" + lid)
        assert (res.status, res.body) == (200, {"item_id": "F" + lid, "state": "sent"})
        sent += 1
        assert ob.count == sent
    ob.fail_next("record_then_drop")  # keyed: stored, reply lost -> failed/ambiguous; Retry resends the same key
    lid = d.handle("form", "s9", lead(email="f9@example.test")).body["lead_id"]
    assert ob.count == sent + 1  # the message is stored although the reply was lost
    fail_until_the_cap(rig, lid, "drop", "ambiguous")  # every later try also ends unclear (the key makes it safe)
    assert ob.count == sent + 1
    assert d.retry("F" + lid).body["state"] == "sent"
    assert ob.count == sent + 1  # the keyed outbox absorbed the repeat
    assert ob.keys().count(lid) == 1
    nr = d.retry("F" + lid)
    assert (nr.status, nr.body["error"]) == (409, "not_retryable")
    assert d.retry("FLdoesnotexist").status == 404
    assert d.retry("X1").status == 404

    # Retry on a stalled `pending` follow-up
    rig = env.rig(hooks=CrashAt("B"))
    d = rig.desk
    with pytest.raises(SimulatedCrash):
        d.handle("form", "s1", lead())
    lid = rig.query("SELECT lead_id FROM leads")[0][0]
    assert rig.state(lid)[0] == "pending"
    rig.clock.advance(rig.config.stall_seconds + 1)
    items = d.review().body
    assert any(x["item_id"] == "F" + lid and x["kind"] == "stalled" for x in items)
    res = d.retry("F" + lid)
    assert (res.status, res.body) == (200, {"item_id": "F" + lid, "state": "sent"})
    assert rig.outbox.count == 1
    assert not any(x["item_id"] == "F" + lid for x in d.review().body)


# -- test_retry_twice_concurrent
def retry_twice_body(rig: Rig) -> None:
    """Blind outbox; a failed follow-up; two Retry calls released together; the send is slow so they overlap."""
    d, ob = rig.desk, rig.outbox
    ob.fail_next("refuse")
    lid = d.handle("form", "s1", lead()).body["lead_id"]
    fail_until_the_cap(rig, lid, "refuse", "refused")  # pending after the first refusal, failed after the cap
    ob.delay = 0.5
    res = run_threads([lambda: d.retry("F" + lid), lambda: d.retry("F" + lid)])
    emails = ob.count
    assert emails == 1, f"emails == 1 expected, got {emails}"  # the guard matches on this text
    for r in res:
        if isinstance(r, BaseException):
            raise r
    assert sorted(r.status for r in res) == [200, 409]
    loser = next(r for r in res if r.status == 409)
    assert loser.body["error"] in ("in_progress", "not_retryable")
    assert rig.state(lid)[0] == "sent"


def test_retry_twice_concurrent(env) -> None:
    retry_twice_body(env.rig(outbox="blind"))


# -- test_stalled_and_dup_with_new_info_listed
def test_stalled_and_dup_with_new_info_listed(env) -> None:
    rig = env.rig(hooks=CrashAt("A", "B"))
    d = rig.desk
    with pytest.raises(SimulatedCrash):
        d.process("form", "sub-1", lead(email="a@example.test"))  # crash at A: receipt stays open
    with pytest.raises(SimulatedCrash):
        d.process("form", "sub-2", lead(email="b@example.test"))  # crash at B: follow-up stays pending
    dup = d.process("webhook", "msg-1", lead(email="b@example.test", phone="555 0199"))
    assert dup.body["differing_fields"] == ["phone"]
    d.process("webhook", "msg-2", lead(email="b@example.test"))  # same data: duplicate, not listed
    items = d.review().body
    assert [x["kind"] for x in items] == ["duplicate_with_new_info"]
    item = items[0]
    assert set(item) == {"item_id", "item_type", "kind", "reason", "lead_id", "since", "source"}
    assert item["item_id"] == "D" + str(dup.body["receipt_id"])
    assert item["item_type"] == "receipt"
    assert "phone" in item["reason"]
    assert item["lead_id"] == dup.body["lead_id"]
    assert item["since"] == rig.clock()
    assert item["source"] == "webhook"
    rig.clock.advance(rig.config.stall_seconds)  # age == stall_seconds: not stalled yet
    assert [x["kind"] for x in d.review().body] == ["duplicate_with_new_info"]
    rig.clock.advance(1)
    assert len(d.review().body) == 3
    stalled = [x for x in d.review().body if x["kind"] == "stalled"]
    assert sorted(x["item_type"] for x in stalled) == ["followup", "receipt"]
    o = next(x for x in stalled if x["item_type"] == "receipt")
    f = next(x for x in stalled if x["item_type"] == "followup")
    assert o["item_id"].startswith("O")
    assert o["reason"] == "open receipt"
    assert o["lead_id"] is None
    assert f["item_id"] == "F" + dup.body["lead_id"]
    assert f["reason"] == "pending"
    assert f["lead_id"] == dup.body["lead_id"]
    counts = d.counts(None).body
    assert counts["open"] == 1
    assert counts["pending_followups"] == 1
    # driving the follow-up clears its stalled item
    assert d.retry(f["item_id"]).body["state"] == "sent"
    assert [x["item_type"] for x in d.review().body if x["kind"] == "stalled"] == ["receipt"]
