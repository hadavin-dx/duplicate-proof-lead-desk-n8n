"""Follow-up ownership and rejected input.

Covers: the ownership token of every follow-up writer (a drive that stalled must write nothing into a later drive's
claim), the 400 for an unknown source and the rejected-envelope count in /counts, and one review item when a duplicate
lead and a replay with other content meet on the same receipt."""

from __future__ import annotations

from typing import Any

import pytest
from support import call, lead, post_lead, sql_exec


def claimed(rig: Any, ref: str) -> tuple[str, int]:
    """A new lead whose follow-up this thread has claimed: returns (lead id, ownership token of that claim)."""
    lid = str(rig.desk.process("form", ref, lead(email=f"{ref}@example.test")).body["lead_id"])
    assert rig.desk._claim(None, lid, ("pending",))
    return lid, rig.desk._take_token(lid)


def go_stale(rig: Any, lid: str) -> None:
    """Another drive took the row after the reaper put it back: the claim counter moved on, the state is `sending`."""
    sql_exec(rig.db_path, "UPDATE followups SET attempts=attempts+1 WHERE lead_id=?", (lid,))


def crm_mark(rig: Any, lid: str) -> int:
    return int(rig.query("SELECT crm_done FROM followups WHERE lead_id=?", (lid,))[0][0])


# ===== ownership token: a stalled drive writes nothing
def test_token_current_mark_is_written(env) -> None:
    rig = env.rig()
    lid, token = claimed(rig, "tk-1")
    assert rig.desk._mark_crm(lid, 2, token) is True
    assert crm_mark(rig, lid) == 2


def test_token_stale_mark_crm_writes_nothing(env) -> None:
    rig = env.rig()
    lid, token = claimed(rig, "tk-2")
    go_stale(rig, lid)
    assert rig.desk._mark_crm(lid, 2, token) is False
    assert rig.desk._mark_crm(lid, 1, token, best_effort=True) is False
    assert crm_mark(rig, lid) == 0, "a stalled drive wrote into the claim of a later drive"


def test_token_stale_touch_writes_nothing(env) -> None:
    rig = env.rig()
    lid, token = claimed(rig, "tk-3")
    go_stale(rig, lid)
    before = rig.query("SELECT updated_at FROM followups")[0][0]
    rig.clock.advance(100)
    assert rig.desk._touch(lid, token) is False
    assert rig.query("SELECT updated_at FROM followups")[0][0] == before


def test_token_stale_crm_step_asks_nothing(env) -> None:
    rig = env.rig()
    lid, token = claimed(rig, "tk-4")
    go_stale(rig, lid)
    assert rig.desk._crm_step(lid, token) is False
    assert rig.crm.count == 0, "the CRM was asked on behalf of a drive that no longer owns the row"
    assert crm_mark(rig, lid) == 0
    assert rig.state(lid)[0] == "sending"


def test_token_stale_send_sends_nothing(env) -> None:
    rig = env.rig()
    lid, token = claimed(rig, "tk-5")
    go_stale(rig, lid)
    rig.desk._send_claimed(lid, True, token)
    assert rig.outbox.count == 0, "an email was sent for a drive that no longer owns the row"
    assert rig.state(lid)[0] == "sending"


def test_token_current_send_sends_once(env) -> None:
    rig = env.rig()
    lid, token = claimed(rig, "tk-6")
    rig.desk._send_claimed(lid, True, token)
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1


# ===== unknown source and rejected envelopes
def test_unknown_source_is_400_and_counted(env) -> None:
    rig = env.rig()
    ok = rig.desk.handle("form", "src-1", lead())
    assert ok.status == 201
    assert rig.desk.counts(1).body["ok"] is True
    r = rig.desk.process("no_such_source", "src-2", lead(email="other@example.test"))
    assert (r.status, r.body) == (400, {"error": "bad_envelope", "receipt_id": None})
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 1, "a rejected envelope made a receipt"
    body = rig.desk.counts(1).body
    assert body["rejected_envelopes"] == 1
    assert body["expected_match"] is True and body["ok"] is False, "a rejected envelope must turn ok off"


@pytest.mark.parametrize("source", ["", "FORM", "form ", None, 7])
def test_odd_sources_are_400(env, source: Any) -> None:
    rig = env.rig()
    assert rig.desk.process(source, "src-3", lead()).status == 400
    assert rig.desk.counts(None).body["rejected_envelopes"] == 1


def test_unknown_source_over_http(env) -> None:
    rig = env.rig()
    base = env.serve(rig)
    status, body = post_lead(base, "no_such_source", "src-4", lead())
    assert status == 400 and body.get("receipt_id") is None
    assert call(base, "GET", "/counts?expected=0")[1]["ok"] is False


# ===== a duplicate lead and a replay with other content on one receipt: one review item
def test_overlap_gives_one_review_item(env) -> None:
    rig = env.rig()
    rig.desk.process("form", "ov-1", lead(email="ov@example.test"))
    dup = rig.desk.process("form", "ov-2", lead(email="ov@example.test", name="Ada New"))
    assert dup.body["verdict"] == "duplicate"
    rid = dup.body["receipt_id"]
    again = rig.desk.process("form", "ov-2", lead(email="ov@example.test", name="Ada Newer"))
    assert again.body["payload_matches"] is False and again.body["receipt_id"] == rid
    assert rig.query("SELECT payload_conflict FROM receipts WHERE receipt_id=?", (rid,))[0][0] == 1
    assert rig.query("SELECT count(*) FROM review_items WHERE item_id=?", (f"D{rid}",))[0][0] == 1
    items = [i for i in rig.desk.review().body if i["item_id"] == f"D{rid}"]
    assert len(items) == 1
    assert items[0]["kind"] == "duplicate_with_new_info"  # the more useful of the two reasons wins
