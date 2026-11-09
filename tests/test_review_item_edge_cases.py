"""Tests for requeue, the CRM mark, backfills, poison payloads and the review list hints. Made-up data only."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import pytest
from support import lead

from lead_desk import db
from lead_desk.adapters import HttpCrm
from lead_desk.desk import Desk


def new_lead(rig: Any, ref: str = "req-1") -> str:
    reply = rig.desk.process("form", ref, lead())
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def set_row(rig: Any, lid: str, **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    con = sqlite3.connect(rig.db_path, timeout=10)
    try:
        con.execute(f"UPDATE followups SET {sets} WHERE lead_id=?", (*cols.values(), lid))  # noqa: S608
        con.commit()
    finally:
        con.close()


def crm_done(rig: Any) -> int:
    return int(rig.query("SELECT crm_done FROM followups")[0][0])


# =====
# requeue of a sending row whose CRM step is not finished
# =====
def test_blind_sending_no_crm_mark(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig)
    set_row(rig, lid, state="sending", crm_done=0, attempts=1, auto_attempts=1)
    rig.desk._requeue_sending(None)
    assert rig.state(lid)[0] == "pending", "a row that never reached the send must not be offered as `sent`"
    rig.desk.run_followup(lid)
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1


def test_blind_sending_unfinished_put(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-2")
    set_row(rig, lid, state="sending", crm_done=2, attempts=1, auto_attempts=1)
    rig.desk._requeue_sending(None)
    assert rig.state(lid)[0] == "pending"
    rig.desk.run_followup(lid)
    assert rig.state(lid) == ("unknown", "crm_unconfirmed") and rig.outbox.count == 0


# =====
# not_sent clears the CRM mark only for a CRM question
# =====
def test_not_sent_keeps_crm_mark(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-3")
    set_row(rig, lid, state="unknown", last_error="ambiguous", crm_done=2, attempts=1, auto_attempts=1)
    assert rig.desk.resolve(f"F{lid}", "not_sent").status == 200
    assert crm_done(rig) == 2, "the person answered about the email; the CRM question stays open"


def test_not_sent_clears_crm_mark(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-4")
    set_row(rig, lid, state="unknown", last_error="crm_unconfirmed", crm_done=2, attempts=1, auto_attempts=1)
    assert rig.desk.resolve(f"F{lid}", "not_sent").status == 200
    assert crm_done(rig) == 0


# =====
# backfill
# =====
@pytest.mark.parametrize(
    ("state", "err", "label", "want"),
    [
        ("pending", "internal:CrmError", None, 2),
        ("failed", "internal:CrmError", "hot", 2),
        ("pending", "internal:CrmNothingStored", None, 0),
        ("pending", "internal:KeyError", None, 0),
        ("pending", "refused", "hot", 1),
        ("unknown", "crash_while_sending", "hot", 1),
        ("unknown", "ambiguous", None, 1),
        ("pending", "unreachable", None, 1),
        ("sending", None, None, 2),
    ],
)
def test_backfill_classes(env, state: str, err: str | None, label: str | None, want: int) -> None:
    rig = env.rig()
    lid = new_lead(rig, "req-5")
    set_row(rig, lid, state=state, last_error=err, ai_label=label, attempts=1, crm_done=0)
    con = sqlite3.connect(rig.db_path, timeout=10)
    try:
        con.execute(db._BACKFILL["crm_done"])
        con.commit()
    finally:
        con.close()
    assert crm_done(rig) == want


# =====
# poison payload
# =====
def test_internal_error_hint_text(env) -> None:
    rig = env.rig()
    rig.desk.process("form", "req-6", lead())
    con = sqlite3.connect(rig.db_path, timeout=10)
    try:
        con.execute("INSERT INTO receipts(source, source_ref, received_at, payload) VALUES('form','c7b',1,'{}')")
        con.execute(
            "UPDATE receipts SET outcome='review', outcome_reason='internal_error', decided_at=2 WHERE source_ref='c7b'"
        )
        con.commit()
        rid = con.execute("SELECT receipt_id FROM receipts WHERE source_ref='c7b'").fetchone()[0]
    finally:
        con.close()
    body = rig.desk.retry(f"R{rid}").body
    assert "person must look" in body["hint"] and "NEW request number" not in body["hint"]
    item = next(i for i in rig.desk.review().body if i["item_id"] == f"R{rid}")
    assert "person must look" in item["hint"]


# =====
# failure count is cleared when the receipt ends as unstorable
# =====
def test_failure_count_cleared_after_unstorable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    real = Desk._decide_once
    calls = [0]

    def flaky(self: Desk, rid: int) -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("plain")
        raise sqlite3.IntegrityError("cannot store")

    monkeypatch.setattr(Desk, "_decide_once", flaky)
    assert rig.desk.process("form", "req-7", lead()).status == 500
    assert rig.desk.process("form", "req-7", lead()).status == 202
    monkeypatch.setattr(Desk, "_decide_once", real)
    assert rig.desk._decide_failures == {}


# =====
# a failed re-read drops an old "keyed"; the warning is said once
# =====
def test_failed_refresh_forgets_keyed() -> None:
    crm = HttpCrm("http://127.0.0.1:1", 0.2)
    crm._capability = "keyed"
    with pytest.raises(Exception):  # noqa: B017, PT011
        crm.refresh()
    assert crm.capability is None


def test_refresh_failure_logged_once(env, caplog: pytest.LogCaptureFixture) -> None:
    rig = env.rig()
    rig.desk.crm = HttpCrm("http://127.0.0.1:1", 0.2)
    rig.desk.crm._capability = "keyed"
    with caplog.at_level(logging.WARNING, logger="lead_desk"):
        for _ in range(3):
            rig.desk._refresh_crm()
    assert len([r for r in caplog.records if "crm capability not re-read" in r.getMessage()]) == 1
    assert rig.desk.crm.capability is None


# =====
# review list hint
# =====
def test_review_list_hint_for_crm_unconfirmed(env) -> None:
    rig = env.rig(outbox="blind")
    lid = new_lead(rig, "req-8")
    set_row(rig, lid, state="unknown", last_error="crm_unconfirmed", crm_done=2, attempts=1)
    item = next(i for i in rig.desk.review().body if i["item_id"] == f"F{lid}")
    assert "Look in the CRM" in item["hint"] and "deleting by hand" in item["hint"]


# =====
# a second drive that failed step 1 cannot add a second automatic try
# =====
def test_stale_defer_changes_nothing(env) -> None:
    """A second drive that failed step 1 cannot add a second automatic try (the writer is `_abort_unsent`)."""
    rig = env.rig()
    lid = new_lead(rig, "req-9")
    set_row(rig, lid, state="sending", attempts=2, auto_attempts=1)
    rig.desk._abort_unsent(lid, 1, ValueError("x"))  # a drive that lost its claim to a newer one
    assert rig.query("SELECT state, auto_attempts FROM followups")[0] == ("sending", 1)
    rig.desk._abort_unsent(lid, 2, ValueError("x"))
    assert rig.query("SELECT state, auto_attempts FROM followups")[0] == ("pending", 1)  # the claim counted the try
    rig.desk._abort_unsent(lid, 2, ValueError("x"))  # the same drive twice: the row is no longer `sending`
    assert rig.query("SELECT state, auto_attempts FROM followups")[0] == ("pending", 1)


# =====
# very long item numbers are a clean 404
# =====
@pytest.mark.parametrize("ref", ["9" * 19, "1" * 5000, "0" * 5000 + "1", "9" * 4301])
@pytest.mark.parametrize("kind", ["R", "D", "O"])
def test_a_long_item_refs_are_not_found(env, kind: str, ref: str) -> None:
    rig = env.rig()
    for call in (rig.desk.retry, lambda i: rig.desk.resolve(i, "sent")):
        r = call(kind + ref)
        if len(ref.lstrip("0")) > 18:
            assert (r.status, r.body) == (404, {"error": "not_found"})
        else:
            assert r.status != 500, r.body  # a short enough number is a normal lookup, never a crash


def test_a_lone_surrogate_ref_not_found(env) -> None:
    rig = env.rig()
    assert rig.desk.retry("F\ud800x").status == 404
    assert rig.desk.resolve("F\x00x", "sent").status == 404


# =====
# a busy answer leaves no receipt waiting for stall_seconds
# =====
def test_busy_receipt_decided_by_reaper(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = env.rig()
    real = Desk._decide_once
    left = [3]  # LOCK_RETRIES tries of one decision

    def locked(self: Desk, rid: int) -> Any:
        if left[0] > 0:
            left[0] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(self, rid)

    monkeypatch.setattr(Desk, "_decide_once", locked)
    r = rig.desk.process("form", "req-10", lead())
    assert r.status == 503
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 1  # open at this instant
    rig.desk.reap_once()  # no clock advance: stall_seconds has NOT passed
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0
    assert rig.leads() == 1
