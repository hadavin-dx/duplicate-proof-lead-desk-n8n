"""Reminder email: one reminder email per lead nobody has replied to, claimed in the desk, sent at most
once, listed for a person when it cannot be confirmed. Made-up data only (example.test, the demo rota Asha and Ravi)."""

from __future__ import annotations

import copy
import json
import logging
import re
import sqlite3
from typing import Any

import pytest
from support import DEMO_LEADFLOW, NUDGE_TO_TEST, CrashAt, call, lead, run_threads
from test_mutants import FAILS, schema_with, weaken

from lead_desk.config import Config, SettingRefused
from lead_desk.contract import (
    AdapterBusy,
    OutboxModeMismatch,
    SendAmbiguous,
    SendNoDescriptors,
    SendRefused,
    SendUnreachable,
    SimulatedCrash,
)
from lead_desk.desk import Desk
from lead_desk.gate import NO_DESCRIPTORS_ERROR, SEND_BUSY_ERROR

AFTER = DEMO_LEADFLOW["nudge_after_seconds"]  # 90
_NUDGE_ROWS = "SELECT lead_id, state, attempts, last_error, sent_at, handled FROM nudges ORDER BY lead_id"


def _rig(env: Any, **kw: Any) -> Any:
    return env.rig(leadflow=copy.deepcopy(DEMO_LEADFLOW), **kw)


def _lead(rig: Any, i: int = 0, **over: Any) -> str:
    reply = rig.desk.process("form", f"rem-{i}", lead(email=f"rem{i}@example.test", **over))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def _set_step(rig: Any, lead_id: str, step: str, rank: int) -> None:
    con = sqlite3.connect(rig.db_path, timeout=10)
    try:
        with con:
            con.execute("UPDATE lead_state SET step=?, step_rank=? WHERE lead_id=?", (step, rank, lead_id))
    finally:
        con.close()


def _seed_nudge(rig: Any, lead_id: str, state: str, updated_at: int, error: str | None = None) -> None:
    con = sqlite3.connect(rig.db_path, timeout=10)
    try:
        with con:
            con.execute(
                "INSERT INTO nudges (lead_id, state, attempts, updated_at, sent_at, last_error) VALUES (?,?,1,?,?,?)",
                (lead_id, state, updated_at, updated_at if state == "sent" else None, error),
            )
    finally:
        con.close()


def _reminder_keys(rig: Any) -> list[str]:
    return [k for k in rig.outbox.keys() if k.startswith("nudge:")]


def _state(rig: Any, lead_id: str) -> tuple[str, str | None]:
    return rig.query("SELECT state, last_error FROM nudges WHERE lead_id=?", (lead_id,))[0]


def test_reminder_at_n_minus_1_n_n_plus_1(env) -> None:
    rig = _rig(env)
    lid = _lead(rig)
    rig.clock.advance(AFTER - 1)
    assert rig.desk.reap_once()["reminders"] == 0
    assert rig.query("SELECT count(*) FROM nudges") == [(0,)] and _reminder_keys(rig) == []
    rig.clock.advance(1)  # exactly N seconds old
    done = rig.desk.reap_once()
    assert done["reminders"] == 1
    assert _reminder_keys(rig) == [f"nudge:{lid}"]
    assert [m["to"] for m in rig.outbox.messages if m["key"].startswith("nudge:")] == [NUDGE_TO_TEST]
    assert _state(rig, lid) == ("sent", None)
    rig.clock.advance(1)
    assert rig.desk.reap_once()["reminders"] == 0
    assert _reminder_keys(rig) == [f"nudge:{lid}"]


def test_reminder_once_over_many_passes(env) -> None:
    """A sent, a failed and an unknown row are never claimed or sent again; a lead without a row gets one reminder,
    however many passes run. (The failed and unknown leads are the OLDEST, so a claim rule that picked them would
    show at once.)"""
    rig = _rig(env)
    old = [_lead(rig, i) for i in range(3)]
    rig.clock.advance(1)
    fresh = _lead(rig, 3)
    t = rig.clock.t
    _seed_nudge(rig, old[0], "failed", t, "refused")
    _seed_nudge(rig, old[1], "unknown", t, "ambiguous")
    _seed_nudge(rig, old[2], "sent", t)
    before = rig.query(_NUDGE_ROWS)
    rig.clock.advance(AFTER + 5)
    for _ in range(10):
        rig.desk.reap_once()
        rig.clock.advance(1)
    assert _reminder_keys(rig) == [f"nudge:{fresh}"]
    after = {r[0]: r for r in rig.query(_NUDGE_ROWS)}
    assert [after[r[0]] for r in before] == before  # the three seeded rows are exactly as they were
    assert after[fresh][1] == "sent" and after[fresh][2] == 1


def test_ten_racing_one_row(env) -> None:
    rig = _rig(env)
    lid = _lead(rig)
    rig.clock.advance(AFTER)
    desk = rig.desk

    def claim() -> Any:
        return desk._locked_retry(lambda: desk._claim_nudge(desk._conn(), desk._now()))

    results = run_threads([claim] * 10)
    assert not [r for r in results if isinstance(r, BaseException)], results
    won = [r for r in results if r is not None]
    assert len(won) == 1 and won[0][0] == lid and won[0][1] == 1
    assert rig.query("SELECT state, attempts FROM nudges") == [("sending", 1)]
    con = sqlite3.connect(rig.db_path, timeout=10)  # the primary key itself keeps one row per lead
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO nudges(lead_id, state, updated_at) VALUES(?, 'pending', 0)", (lid,))
    finally:
        con.close()


def test_reply_step_ends_reminder(env) -> None:
    """A lead at or past the reply step (Replied, rank 2) gets no reminder; one that only has the follow-up
    recorded (Contacted, rank 1) still does."""
    rig = _rig(env)
    replied, contacted = _lead(rig, 0), _lead(rig, 1)
    _set_step(rig, replied, "replied", 2)
    _set_step(rig, contacted, "contacted", 1)
    rig.clock.advance(AFTER + 1)
    rig.desk.reap_once()
    assert _reminder_keys(rig) == [f"nudge:{contacted}"]
    assert rig.query("SELECT lead_id FROM nudges") == [(contacted,)]


@pytest.mark.parametrize(("step", "rank"), [("qualified", 3), ("won", 4), ("closed", 5)])
def test_terminal_step_before_claim_no_reminder(env, step: str, rank: int) -> None:
    rig = _rig(env)
    lid, control = _lead(rig), _lead(rig, 1)
    _set_step(rig, lid, step, rank)
    rig.clock.advance(AFTER * 3)
    for _ in range(3):
        rig.desk.reap_once()
    # the lead at a terminal step gets nothing; the control lead (still New) proves reminders are running
    assert _reminder_keys(rig) == [f"nudge:{control}"] and rig.query("SELECT lead_id FROM nudges") == [(control,)]


def test_clock_set_back_no_resend(env) -> None:
    rig = _rig(env)
    lid = _lead(rig)
    rig.clock.advance(AFTER)
    rig.desk.reap_once()
    assert _reminder_keys(rig) == [f"nudge:{lid}"]
    rig.clock.advance(-10_000)  # the clock is set back: the lead looks young, the row exists
    for _ in range(3):
        rig.desk.reap_once()
        rig.clock.advance(AFTER)
    assert _reminder_keys(rig) == [f"nudge:{lid}"]
    # a row stamped in the future is stamped again at the next pass, so it ages normally and is never sent
    rig2 = _rig(env)
    lid2 = _lead(rig2)
    _seed_nudge(rig2, lid2, "sending", rig2.clock.t + 1000)
    rig2.desk.reap_once()
    assert rig2.query("SELECT updated_at, state FROM nudges") == [(rig2.clock.t, "sending")]
    rig2.clock.advance(rig2.desk.stuck_grace_seconds + 1)
    rig2.desk.reap_once()
    assert _state(rig2, lid2) == ("unknown", "crash_while_sending") and _reminder_keys(rig2) == []


class _Script:
    """What the outbox does on its next call(s): raise `exc`, or fall through to the real fake."""

    def __init__(self, rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.exc: BaseException | None = None
        self.calls: list[str] = []
        real = rig.outbox.send

        def send(key: str, to: str, subject: str, body: str, timeout: float) -> Any:
            if not key.startswith("nudge:"):  # follow-ups go through untouched
                return real(key, to, subject, body, timeout)
            self.calls.append(key)
            if self.exc is not None:
                raise self.exc
            return real(key, to, subject, body, timeout)

        monkeypatch.setattr(rig.outbox, "send", send)


_GIVE_BACKS = {
    "no_descriptors": (SendNoDescriptors("no file handle"), NO_DESCRIPTORS_ERROR),
    "slot_full": (AdapterBusy("send"), SEND_BUSY_ERROR),
}
_RESULTS = {
    "mode_mismatch": (OutboxModeMismatch("stored in the wrong mode"), "unknown", "mode_mismatch"),
    "refused": (SendRefused("no"), "failed", "refused"),
    "unreachable": (SendUnreachable("down"), "failed", "unreachable"),
    "ambiguous": (SendAmbiguous("no answer"), "unknown", "ambiguous"),
    "other": (RuntimeError("secret@example.test"), "unknown", "ambiguous"),
}


@pytest.mark.parametrize("case", sorted(_GIVE_BACKS))
def test_nudge_result_table_give_back(env, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """Nothing was sent: the row goes back to pending (age kept), the part stops for this pass (the second lead is not
    claimed), makes no claim while the 5 s pause runs, and sends once the pause is over."""
    exc, error = _GIVE_BACKS[case]
    rig = _rig(env)
    ids = [_lead(rig, 0), _lead(rig, 1)]
    script = _Script(rig, monkeypatch)
    if case == "slot_full":  # the slot check of _bounded refuses before the outbox is touched
        real_bounded = rig.desk._bounded

        def bounded(kind: str, fn: Any, *a: Any, **k: Any) -> Any:
            if script.exc is not None and str(k.get("tag", "")).startswith("nudge:"):
                raise script.exc
            return real_bounded(kind, fn, *a, **k)

        monkeypatch.setattr(rig.desk, "_bounded", bounded)
    script.exc = exc
    rig.clock.advance(AFTER)
    stamp = rig.clock.t
    done = rig.desk.reap_once()
    assert done["reminders"] == 0
    rows = rig.query("SELECT lead_id, state, attempts, last_error, updated_at FROM nudges")
    assert len(rows) == 1  # one claim in the pass: the part stopped after the give-back
    first = rows[0]
    assert first[0] in ids  # one of the two leads, never another row
    assert first[1:4] == ("pending", 1, error) and first[4] == stamp
    assert _reminder_keys(rig) == []
    script.exc = None
    rig.clock.advance(1)
    rig.desk.reap_once()  # inside the pause: no claim
    assert rig.query("SELECT state, attempts FROM nudges WHERE lead_id=?", (first[0],)) == [("pending", 1)]
    rig.desk._nudge_pause_until = 0  # the pause is over (the test does not sleep 5 s)
    rig.desk.reap_once()
    assert _state(rig, first[0]) == ("sent", None)
    assert rig.query("SELECT attempts FROM nudges WHERE lead_id=?", (first[0],)) == [(2,)]
    assert [k for k in _reminder_keys(rig) if k == f"nudge:{first[0]}"] == [f"nudge:{first[0]}"]  # stored once


@pytest.mark.parametrize("case", sorted(_RESULTS))
def test_nudge_result_table_records(env, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    exc, state, error = _RESULTS[case]
    rig = _rig(env)
    lid = _lead(rig)
    script = _Script(rig, monkeypatch)
    script.exc = exc
    rig.clock.advance(AFTER)
    for _ in range(3):
        rig.desk.reap_once()
        rig.clock.advance(1)
    assert _state(rig, lid) == (state, error)
    assert script.calls == [f"nudge:{lid}"]  # asked once, never again: no automatic retry
    row = rig.query("SELECT sent_at, handled FROM nudges")[0]
    assert row == (None, 0)


def test_nudge_result_table_sent_and_internal_and_locked(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(env)
    lid = _lead(rig)
    rig.clock.advance(AFTER)
    # the claim transaction stays locked: nothing claimed, nothing written, the pass still ends normally
    real_claim = Desk._claim_nudge

    def locked(self: Desk, conn: Any, now: int) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Desk, "_claim_nudge", locked)
    assert rig.desk.reap_once()["reminders"] == 0
    assert rig.query("SELECT count(*) FROM nudges") == [(0,)]
    monkeypatch.setattr(Desk, "_claim_nudge", real_claim)
    # any other fault before the send (here: while reading the deadline): failed, internal:<class>, nothing sent
    monkeypatch.setattr(rig.desk, "_call_deadline", lambda: 1 / 0)
    rig.desk.reap_once()
    assert _state(rig, lid) == ("failed", "internal:ZeroDivisionError") and _reminder_keys(rig) == []
    rig2 = _rig(env)
    lid2 = _lead(rig2)
    rig2.clock.advance(AFTER)
    rig2.desk.reap_once()
    row = rig2.query("SELECT state, last_error, sent_at FROM nudges")[0]
    assert row == ("sent", None, rig2.clock.t)
    assert rig2.query("SELECT step FROM lead_state WHERE lead_id=?", (lid2,)) == [
        ("contacted",)
    ]  # the nudge leaves the step as the follow-up set it (Contacted)


def test_reminder_crash_gives_unknown(env) -> None:
    """Killed at N1 (after the send, before the record): the row stays `sending`, the start-up sweep makes it unknown
    before the reaper's first pass, nothing is sent again. A `sending` row older than the grace is made unknown by
    the reaper."""
    hooks = CrashAt("N1")
    rig = _rig(env, hooks=hooks)
    lid = _lead(rig)
    rig.clock.advance(AFTER)
    with pytest.raises(SimulatedCrash):
        rig.desk.reap_once()
    assert _state(rig, lid) == ("sending", None) and _reminder_keys(rig) == [f"nudge:{lid}"]
    rig.restart()  # start-up sweep: no reaper has run yet
    assert _state(rig, lid) == ("unknown", "crash_while_sending")
    for _ in range(3):
        rig.clock.advance(AFTER)
        rig.desk.reap_once()
    assert _reminder_keys(rig) == [f"nudge:{lid}"]
    # the reaper's own sweep: a seeded row older than the grace
    rig2 = _rig(env)
    lid2 = _lead(rig2)
    _seed_nudge(rig2, lid2, "sending", rig2.clock.t - rig2.desk.stuck_grace_seconds - 5)
    rig2.clock.advance(AFTER)
    rig2.desk.reap_once()
    assert _state(rig2, lid2) == ("unknown", "crash_while_sending") and _reminder_keys(rig2) == []
    rig3 = _rig(env)  # a seeded `sending` row of the same age is NOT touched while it is still inside the grace
    lid3 = _lead(rig3)
    _seed_nudge(rig3, lid3, "sending", rig3.clock.t)
    rig3.clock.advance(1)
    rig3.desk.reap_once()
    assert _state(rig3, lid3) == ("sending", None)


def test_unknown_reminder_listed_and_seen_clears(env) -> None:
    rig = _rig(env)
    lid, lid2, lid3 = _lead(rig, 0), _lead(rig, 1), _lead(rig, 2)
    now = rig.clock.t
    _seed_nudge(rig, lid, "unknown", now, "ambiguous")
    _seed_nudge(rig, lid2, "pending", now, "send_busy")
    _seed_nudge(rig, lid3, "failed", now, "refused")
    items = [i for i in rig.desk.review().body if i["item_type"] == "reminder"]
    assert sorted((i["item_id"], i["kind"], i["reason"]) for i in items) == sorted(
        [(f"N{lid}", "reminder_to_check", "unknown"), (f"N{lid3}", "reminder_to_check", "failed")]
    )
    d = rig.desk
    assert d.resolve("Nnope", "seen").status == 404 and d.resolve("N", "seen").status == 404
    assert d.resolve(f"N{lid}", "sent").body == {"error": "bad_decision"}  # 400
    assert d.resolve(f"N{lid}", "sent").status == 400
    assert d.resolve(f"N{lid2}", "seen").status == 409  # pending: nothing to check
    assert d.resolve(f"N{lid2}", "seen").body["error"] == "not_to_check"
    ok = d.resolve(f"N{lid}", "seen")
    assert (ok.status, ok.body) == (200, {"item_id": f"N{lid}", "state": "unknown", "handled": True})
    assert d.resolve(f"N{lid}", "seen").status == 200  # a repeat is 200 too
    assert [i["item_id"] for i in d.review().body if i["item_type"] == "reminder"] == [f"N{lid3}"]
    assert rig.query("SELECT state FROM nudges WHERE lead_id=?", (lid,)) == [("unknown",)]  # state kept, never re-sent
    # a follow-up item does not take `seen`, and retry on a reminder is refused with its hint
    assert d.resolve(f"F{lid}", "seen").body == {"error": "bad_decision"}
    again = d.retry(f"N{lid3}")
    assert again.status == 409 and again.body["error"] == "not_actionable"
    assert "never sent again" in again.body["hint"]
    assert _reminder_keys(rig) == []


def test_unknown_reminder_card_and_counts_over_http(env) -> None:
    rig = _rig(env)
    lid = _lead(rig)
    _seed_nudge(rig, lid, "unknown", rig.clock.t, "ambiguous")
    base = env.serve(rig)
    status, review = call(base, "GET", "/api/ui/review")
    assert status == 200
    card = [i for i in review["items"] if i["item_id"] == f"N{lid}"]
    assert len(card) == 1 and card[0]["kind"] == "reminder_to_check" and card[0]["state_view"] == "reminder_to_check"
    assert [a["id"] for a in card[0]["actions"]] == ["resolve_seen"]
    assert card[0]["actions"][0]["body"] == {"decision": "seen"}
    assert card[0]["actions"][0]["endpoint"] == f"/review/N{lid}/resolve"
    assert call(base, "GET", "/api/ui/summary")[1]["needs_action"] == 1
    status, out = call(base, "POST", f"/review/N{lid}/resolve", {"decision": "seen"})
    assert (status, out["handled"]) == (200, True)
    assert call(base, "GET", "/api/ui/summary")[1]["needs_action"] == 0


def test_reminder_body_has_no_lead_value_and_no_header_injection(env, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(env)
    lid = _lead(rig, name="Zed Zimmerman", phone="555 0123", message="hello private words")
    seen: list[tuple[str, str, str, str]] = []
    real = rig.outbox.send

    def send(key: str, to: str, subject: str, body: str, timeout: float) -> Any:
        seen.append((key, to, subject, body))
        return real(key, to, subject, body, timeout)

    monkeypatch.setattr(rig.outbox, "send", send)
    rig.clock.advance(AFTER)
    rig.desk.reap_once()
    (key, to, subject, body) = next(m for m in seen if m[0].startswith("nudge:"))
    assert key == f"nudge:{lid}" and len(key) == 19 and to == NUDGE_TO_TEST
    assert subject == f"Lead reminder {lid[:8]}"
    assert "Asha" in body and "Contacted" in body and f"{AFTER} seconds" in body and lid[:8] in body
    for secret in ("Zed", "Zimmerman", "555 0123", "private words", "rem0@example.test", "example.test"):
        assert secret not in body and secret not in subject
    for part in (key, to, subject):
        assert "\r" not in part and "\n" not in part
    # DESK_NUDGE_TO must be one plain address: a header injection through it is refused when the Config is built
    for bad in ("a@example.test\r\nBcc: x@example.test", "a@example.test, b@example.test", "a b@example.test"):
        with pytest.raises(SettingRefused):
            Config(db_path=rig.db_path, port=0, replies_path=rig.replies_path, leadflow_path=rig.config.leadflow_path,
                   nudge_to=bad)  # fmt: skip


def test_nudge_to_never_logged(env, caplog) -> None:
    rig = _rig(env)
    lid = _lead(rig)
    base = env.serve(rig)
    rig.clock.advance(AFTER)
    with caplog.at_level(logging.DEBUG):
        rig.desk.reap_once()
    seen = [json.dumps(call(base, "GET", p)[1]) for p in ("/counts", "/api/ui/summary", "/api/ui/leads",
            f"/api/ui/leads/{lid}", "/api/ui/review", "/api/ui/log", "/api/ui/data-map")]  # fmt: skip
    assert _reminder_keys(rig) == [f"nudge:{lid}"]  # the reminder really went out while everything was watched
    for text in (caplog.text, repr(rig.config), repr(rig.desk.config), json.dumps(rig.desk.counts(None).body), *seen):
        assert NUDGE_TO_TEST not in text and "reminder@" not in text


# -- mutants: each plants one fault and requires the real test above to fail
def test_mutant_nudge_pk_removed(env) -> None:
    weak = schema_with(
        re.escape("lead_id    TEXT NOT NULL PRIMARY KEY REFERENCES leads(lead_id),\n  state "),
        "lead_id    TEXT NOT NULL REFERENCES leads(lead_id),\n  state ",
    )

    class Weak:
        def rig(self, **kw: Any) -> Any:
            return env.rig(schema_sql=weak, **kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(env, name)

    with pytest.raises(FAILS + (sqlite3.OperationalError,)):
        test_ten_racing_one_row(Weak())


def test_mutant_reminder_ignores_reply_step(env, monkeypatch: pytest.MonkeyPatch) -> None:
    weaken(monkeypatch, Desk, "_claim_nudge", (re.escape("s.step_rank < :until_rank AND "), ""))
    with pytest.raises(FAILS + (sqlite3.OperationalError,)):
        test_reply_step_ends_reminder(env)


def test_mutant_reminder_claims_every_state(env, monkeypatch: pytest.MonkeyPatch) -> None:
    weaken(
        monkeypatch,
        Desk,
        "_claim_nudge",
        (re.escape("(n.lead_id IS NULL OR n.state = 'pending')"), "1"),
        (re.escape(" AND state='pending'"), ""),
    )
    with pytest.raises(FAILS + (sqlite3.OperationalError,)):
        test_reminder_once_over_many_passes(env)


def test_a_desk_refuses_reminders_without_an_address_and_an_address_without_reminders(tmp_path) -> None:
    """Both directions are refused when the Desk is built (not only by the start script): reminders on and no
    address would send to nobody; an address and reminders off would never be used."""
    import copy

    from support import DEMO_LEADFLOW, NUDGE_TO_TEST, build_rig

    from lead_desk.desk import DeskSettingRefused

    on = copy.deepcopy(DEMO_LEADFLOW)
    assert on["nudge_after_seconds"] is not None
    off = copy.deepcopy(DEMO_LEADFLOW)
    off["nudge_after_seconds"] = None
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(DeskSettingRefused, match="DESK_NUDGE_TO is not set"):
        build_rig(tmp_path / "a", leadflow=on, nudge_to="")
    with pytest.raises(DeskSettingRefused, match="turns reminders off"):
        build_rig(tmp_path / "b", leadflow=off, nudge_to=NUDGE_TO_TEST)
