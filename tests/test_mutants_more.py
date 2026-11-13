"""More mutation guards (test side only, nothing in src). Each test asserts what a caller or the lead book can see
and fails when one specific small change is made to the desk. Made-up data only."""

from __future__ import annotations

import re
import sqlite3
import threading
from typing import Any

import pytest
from support import BlockAt, lead, sql_exec

from demo_only.fakes import BlindOutbox
from lead_desk import db, identity, server
from lead_desk.desk import Desk


def _new_lead(rig: Any, ref: str = "r1", email: str | None = None) -> str:
    """Receipt and decision only (no follow-up drive): the lead id of the new lead."""
    reply = rig.desk.process("form", ref, lead() if email is None else lead(email=email))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def _row(rig: Any, lid: str) -> tuple[Any, ...]:
    return rig.query("SELECT state, last_error, sent_at, auto_attempts FROM followups WHERE lead_id=?", (lid,))[0]


# -- the email key drops surrounding blanks
def test_email_key_strips_blanks() -> None:
    assert identity.normalise_email("  Ada@" + "Example" + ".TEST \t") == "ada@example.test"
    assert identity.normalise_email("\n Ada@Example.test\n") == identity.normalise_email("ada@example.test")


# -- a replay drives a pending follow-up exactly when its pause is over
def test_replay_due_exactly_at_pause_end(env) -> None:
    rig = env.rig()
    lid = _new_lead(rig)
    sql_exec(
        rig.db_path,
        "UPDATE followups SET auto_attempts=1, updated_at=? WHERE lead_id=?",
        (rig.clock.t, lid),
    )  # one automatic try was made just now: the pause after it is 1 s
    assert rig.desk.process("form", "r1", lead()).drive_lead_id is None  # 0 s later: not due
    rig.clock.advance(1)
    assert rig.desk.process("form", "r1", lead()).drive_lead_id == lid  # exactly 1 s later: due
    rig.clock.advance(5)
    assert rig.desk.process("form", "r1", lead()).drive_lead_id == lid  # later still: due


# -- sent_at and last_error after each kind of result
def test_sent_sets_time_and_clears_error(env) -> None:
    rig = env.rig()
    lid = _new_lead(rig)
    rig.outbox.fail_next("unreachable")
    rig.desk.run_followup(lid)
    assert _row(rig, lid)[:3] == ("pending", "unreachable", None)
    rig.clock.advance(10)
    rig.desk.run_followup(lid)
    state, err, sent_at, _ = _row(rig, lid)
    assert (state, err, sent_at) == ("sent", None, rig.clock.t)


@pytest.mark.parametrize(
    "mode, expected, expected_error",
    [("refuse", "pending", "refused"), ("unreachable", "pending", "unreachable"), ("drop", "failed", "ambiguous")],
)
def test_unsent_result_has_no_sent_time(env, mode: str, expected: str, expected_error: str) -> None:
    rig = env.rig()
    lid = _new_lead(rig)
    if mode == "drop":  # keyed sender, unclear end, cap reached: failed
        sql_exec(rig.db_path, "UPDATE followups SET auto_attempts=99 WHERE lead_id=?", (lid,))
    rig.outbox.fail_next(mode)  # type: ignore[arg-type]
    rig.desk.run_followup(lid)
    state, err, sent_at, _ = _row(rig, lid)
    assert (state, err) == (expected, expected_error)
    assert sent_at is None


def test_blind_unclear_has_no_sent_time(env) -> None:
    rig = env.rig(outbox=BlindOutbox())
    lid = _new_lead(rig)
    rig.outbox.fail_next("drop")
    rig.desk.run_followup(lid)
    state, err, sent_at, _ = _row(rig, lid)
    assert (state, err, sent_at) == ("unknown", "ambiguous", None)


# -- a late result for a row that is no longer `sending` changes nothing
def test_late_result_leaves_put_back_row(env) -> None:
    hook = BlockAt("D2")  # after the send returned, before the result is written
    rig = env.rig(hooks=hook)
    lid = _new_lead(rig)
    th = threading.Thread(target=rig.desk.run_followup, args=(lid,), daemon=True)
    th.start()
    assert hook.reached.wait(timeout=15), "the drive never reached the send"
    try:
        # the row is put back to `pending` meanwhile (as the reaper does); the claim count stays the same
        sql_exec(rig.db_path, "UPDATE followups SET state='pending' WHERE lead_id=?", (lid,))
    finally:
        hook.release.set()
        th.join(timeout=15)
    assert not th.is_alive()
    state, err, sent_at, _ = _row(rig, lid)
    assert (state, err, sent_at) == ("pending", None, None)


# -- Retry of a pending or failed row whose claim could not be made answers 503 busy
@pytest.mark.parametrize("start", ["pending", "failed"])
def test_retry_busy_is_503(env, monkeypatch: pytest.MonkeyPatch, start: str) -> None:
    rig = env.rig()
    lid = _new_lead(rig)
    sql_exec(rig.db_path, "UPDATE followups SET state=? WHERE lead_id=?", (start, lid))
    monkeypatch.setattr(Desk, "_claim", lambda *a, **k: False)  # the database stayed locked: no claim
    reply = rig.desk.retry("F" + lid)
    assert (reply.status, reply.body) == (503, {"error": "busy"})
    assert rig.state(lid)[0] == start  # nothing changed


# -- resolve acts only on `unknown` rows
@pytest.mark.parametrize("decision", ["sent", "not_sent", "crm_present"])
def test_resolve_ignores_failed_row(env, decision: str) -> None:
    rig = env.rig()
    lid = _new_lead(rig)
    sql_exec(
        rig.db_path, "UPDATE followups SET state='failed', last_error='refused', crm_done=2 WHERE lead_id=?", (lid,)
    )
    before = rig.query("SELECT * FROM followups WHERE lead_id=?", (lid,))
    reply = rig.desk.resolve("F" + lid, decision)
    assert reply.status == 409 and reply.body["error"] == "not_unknown", reply.body
    assert rig.query("SELECT * FROM followups WHERE lead_id=?", (lid,)) == before


# -- the receipt count matches only when it equals the expected number
def test_counts_match_needs_equality(env) -> None:
    rig = env.rig()
    _new_lead(rig, "req-1")
    _new_lead(rig, "req-2", "other@example.test")
    assert rig.desk.counts(2).body["expected_match"] is True
    for expected in (1, 3):
        body = rig.desk.counts(expected).body
        assert body["received"] == 2
        assert body["expected_match"] is False and body["ok"] is False
    assert rig.desk.counts(None).body["expected_match"] is None


# -- "database table is locked" (code 6) is busy, whatever the message says
@pytest.mark.parametrize("code", [5, 6, 261, 262])
def test_is_busy_takes_both_lock_codes(code: int) -> None:
    exc = sqlite3.OperationalError("some neutral text")
    exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    assert db.is_busy(exc) is True


def test_is_busy_refuses_other_errors() -> None:
    other = sqlite3.OperationalError("some neutral text")
    other.sqlite_errorcode = 1  # type: ignore[attr-defined]
    assert db.is_busy(other) is False
    assert db.is_busy(ValueError("database is locked")) is False


# -- only schema versions 0 and 1 are accepted
@pytest.mark.parametrize("version", [2, 3, 7])
def test_schema_version_refused(version: int) -> None:
    con = sqlite3.connect(":memory:", isolation_level=None)
    try:
        con.execute(f"PRAGMA user_version={version}")
        with pytest.raises(RuntimeError, match="unsupported schema version"):
            db.init_schema(con)
        assert con.execute("PRAGMA user_version").fetchone()[0] == version  # nothing was written
        assert con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        con.close()


# -- later messages: each planted fault must make the named test fail (patterns of test_mutants.py)
@pytest.mark.parametrize(
    ("owner_name", "pattern", "repl", "target"),
    [
        ("_trimmed", r"v\.strip\(\) if", "' '.join(v.split()) if", "test_later_values_word_for_word"),  # M1a
        ("_trimmed", r"v\.strip\(\) if", "v.strip().lower() if", "test_later_values_word_for_word"),  # M1b
        ("_trimmed", r"v\.strip\(\) if", "v if", "test_later_values_word_for_word"),  # M1c
        (
            "_lead_detail",
            r"_trimmed\(this_payloads\[n\]\.get\(f\)\)",
            "lead_row.get(f)",
            "test_later_values_word_for_word",
        ),  # M1d
        (
            "_lead_detail",
            r"this_payloads\[n\]\.get",
            "this_payloads[n - 1].get",
            "test_later_burst_ten_each_own_values",
        ),  # M2b
        ("_lead_detail", r"d\[3\] - first\[3\]", "first[3] - d[3]", "test_later_seconds_after_first"),  # M3
    ],
)
def test_mutant_later_messages(env, monkeypatch, owner_name: str, pattern: str, repl: str, target: str) -> None:
    import test_later_messages
    from test_mutants import FAILS, weaken

    from lead_desk import portal_api

    weaken(monkeypatch, portal_api, owner_name, (pattern, repl))
    with pytest.raises(FAILS + (IndexError,)):
        getattr(test_later_messages, target)(env)


def test_mutant_later_order_by_received_time(env, monkeypatch) -> None:  # M2
    import test_later_messages
    from test_mutants import FAILS

    from lead_desk import portal_api

    new = portal_api._DUPLICATES.replace("ORDER BY receipt_id DESC", "ORDER BY received_at DESC, receipt_id DESC")
    assert new != portal_api._DUPLICATES
    monkeypatch.setattr(portal_api, "_DUPLICATES", new)
    with pytest.raises(FAILS):
        test_later_messages.test_later_order_by_receipt_number(env)


@pytest.mark.parametrize(
    ("old", "new", "target"),
    [
        ("(:by_email = 1 AND l.email", "(1 = 1 AND l.email", "test_leads_email_search_needs_reveal"),  # M10
        (
            ":pat ESCAPE '\\' AND :by_email = 1))",
            ":pat ESCAPE '\\' AND 1 = 1))",
            "test_leads_email_search_needs_reveal",
        ),  # M10b
        ("(:by_email = 1 AND l.name", "(1 = 1 AND l.name", "test_leads_email_search_needs_reveal"),  # M10c
    ],
)
def test_mutant_email_search_while_masked(env, monkeypatch, old: str, new: str, target: str) -> None:
    import test_later_messages
    from test_mutants import FAILS

    from lead_desk import portal_api

    for name in ("_LEADS_ROWS", "_LEADS_TOTAL"):
        text = getattr(portal_api, name)
        if old in text:
            monkeypatch.setattr(portal_api, name, text.replace(old, new))
    assert old not in portal_api._LEADS_ROWS and old not in portal_api._LEADS_TOTAL, "the mutation was not applied"
    with pytest.raises(FAILS):
        getattr(test_later_messages, target)(env)


def test_mutant_email_parameter_not_checked(env, monkeypatch) -> None:  # M14
    import test_later_messages
    from test_mutants import FAILS, weaken

    from lead_desk import portal_api

    weaken(monkeypatch, portal_api, "_leads", (r"if by_email is None:", "if False:"))
    with pytest.raises(FAILS):
        test_later_messages.test_leads_email_search_needs_reveal(env)


def test_mutant_exception_object_logged_again(env, monkeypatch, caplog, capfd) -> None:  # M15
    import test_later_messages
    from test_mutants import FAILS, weaken

    from lead_desk import portal_api

    weaken(
        monkeypatch,
        portal_api,
        "handle",
        (r'"portal read failed: %s", type\(e\)\.__name__', '"portal read failed: %s", e'),
    )
    with pytest.raises(FAILS):
        test_later_messages.test_later_messages_log_no_values(env, monkeypatch, caplog, capfd)


def test_mutant_sender_shown_without_mask() -> None:  # drops the mask: the address goes straight to the row
    from pathlib import Path

    import test_later_messages

    text = (Path(__file__).resolve().parents[1] / "src/lead_desk/portal/pages/leads.js").read_text(encoding="utf-8")
    bad = text.replace(
        "P.ui.guarded('email', mailSender, P.reveal.key('sender', mailSender),",
        "(function (a, b, c, d) { return b; })('email', mailSender, P.reveal.key('sender', mailSender),",
    )
    assert bad != text and not test_later_messages._sender_masked(bad)


# -- approval mode (tests/test_later_approval.py): each mutant makes the named test fail
_FAILS = (AssertionError, pytest.fail.Exception)  # a failed assert, or a pytest.raises that did not raise


def _later_mutants():  # imported late: the module builds a rig helper and needs the fixtures of this folder
    import test_later_approval as la
    from test_mutants import WeakSchemaEnv, schema_with, weaken

    return la, WeakSchemaEnv, schema_with, weaken


def test_mutant_decide_updates_instead_of_nothing(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """MU1a: ON CONFLICT DO UPDATE makes the second writer win (the update trigger would stop it, so MU1b below
    takes the trigger away too)."""
    la, _, _, weaken = _later_mutants()
    from lead_desk import desk as desk_mod

    assert "DO NOTHING" in desk_mod._DECIDE
    monkeypatch.setattr(
        desk_mod,
        "_DECIDE",
        desk_mod._DECIDE.replace(
            "DO NOTHING", "DO UPDATE SET decision = excluded.decision, approver_id = excluded.approver_id"
        ),
    )
    weaken(monkeypatch, Desk, "_decide_later", (re.escape("if decided is not None:"), "if False:"))
    with pytest.raises(_FAILS):
        la.test_first_writer_wins_and_409_names_who_where_when(env)


def test_mutant_decide_updates_and_no_update_trigger(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """MU1b: MU1a plus a schema without later_decisions_no_update: the second writer really overwrites."""
    la, WeakSchemaEnv, schema_with, weaken = _later_mutants()
    from lead_desk import desk as desk_mod

    monkeypatch.setattr(
        desk_mod,
        "_DECIDE",
        desk_mod._DECIDE.replace(
            "DO NOTHING", "DO UPDATE SET decision = excluded.decision, approver_id = excluded.approver_id"
        ),
    )
    weak = WeakSchemaEnv(env, schema_with(r"CREATE TRIGGER IF NOT EXISTS later_decisions_no_update.*?END;"))
    weaken(monkeypatch, Desk, "_decide_later", (re.escape("if decided is not None:"), "if False:"))
    with pytest.raises(_FAILS):
        la.test_first_writer_wins_and_409_names_who_where_when(weak)
    with pytest.raises(_FAILS):
        la.test_decision_row_append_only(weak)


def test_mutant_resolve_branch_ignores_the_mode(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """MU2: the D branch of resolve runs in mode off too."""
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        Desk,
        "resolve",
        (re.escape('if kind == "D" and self.config.later_mode == "approval":'), 'if kind == "D":'),
    )
    with pytest.raises(_FAILS):
        la.test_mode_off_d_item_unchanged(env)


def test_mutant_replay_only_item_is_decidable(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """MU3: the check that the receipt is a later message with new details is gone."""
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        Desk,
        "_decide_later",
        (re.escape("if conn.execute(_LATER_DECIDABLE, (rid,)).fetchone() is None:"), "if False:"),
    )
    with pytest.raises(_FAILS):
        la.test_check_order_404_409_400_403(env)


def test_mutant_unknown_approver_passes(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """MU4: the approvers-list check is gone."""
    la, _, _, weaken = _later_mutants()
    weaken(monkeypatch, Desk, "_decide_later", (re.escape("if name is None:"), "if False:"))
    with pytest.raises(_FAILS):
        la.test_check_order_404_409_400_403(env)
    with pytest.raises(_FAILS):
        la.test_unknown_approver_is_403(env)


def test_mutant_409_does_not_name_the_winner(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """MU6: the 409 body carries no decision fields."""
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        Desk,
        "_already_decided",
        (re.escape(", **dict(zip(DECISION_KEYS, row, strict=True))"), ""),
    )
    with pytest.raises(_FAILS):
        la.test_first_writer_wins_and_409_names_who_where_when(env)


def test_mutant_resolve_size_check_removed(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """The resolve route reads the body without the size check first."""
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        server._Handler,
        "_resolve",
        (re.escape("if n > self._desk.config.size_cap_bytes:"), "if False:"),
    )
    with pytest.raises((*_FAILS, TimeoutError)):  # the read waits for a body that never comes
        la.test_oversize_decision_body_is_413_before_any_body_byte_is_read(env)


def test_mutant_resolve_size_check_off_by_one(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """The resolve cap refuses a body of exactly the cap (> became >=)."""
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        server._Handler,
        "_resolve",
        (re.escape("if n > self._desk.config.size_cap_bytes:"), "if n >= self._desk.config.size_cap_bytes:"),
    )
    with pytest.raises(_FAILS):
        la.test_oversize_decision_body_is_413_before_any_body_byte_is_read(env)


def test_mutant_resolve_413_no_drain(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """The resolve 413 stops draining the unread body."""
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        server._Handler,
        "_resolve",
        (re.escape("self._drain(n)"), "pass"),
    )
    with pytest.raises(_FAILS):
        la.test_resolve_413_drains_the_unread_body_like_post_leads(env, monkeypatch)


def test_mutant_foreign_origin_gate_off(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """The Origin check of the Host and Origin gate (it covers the decision route like every route) is gone."""
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        server._Handler,
        "_gate_error",
        (r"if len\(origins\) != 1 or origins\[0\] not in \{[^\n]*\}:", "if False:"),
    )
    with pytest.raises(_FAILS):
        la.test_foreign_origin_decision_is_403(env)


def test_mutant_decision_logs_a_value_to_stderr(
    monkeypatch: pytest.MonkeyPatch, env: Any, caplog: Any, capfd: Any
) -> None:
    """The log test also scans stderr: a desk that prints a stored value there is caught."""
    import sys

    la, _, _, _ = _later_mutants()
    real = Desk.resolve

    def noisy(self: Any, item_id: str, decision: str) -> Any:
        print("phone 555 0143", file=sys.stderr)
        return real(self, item_id, decision)

    monkeypatch.setattr(Desk, "resolve", noisy)
    with pytest.raises(_FAILS):
        la.test_decision_logs_no_values(env, caplog, capfd)


def test_mutant_sec_fetch_site_check_off(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        server._Handler,
        "_gate_error",
        (r"if fetch_site and \(.*SAME_SITE_VALUES\):", "if False:"),
    )
    with pytest.raises(_FAILS):
        la.test_cross_site_fetch_decision_is_403(env)


def test_mutant_host_check_off(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        server._Handler,
        "_gate_error",
        (r'if match is None or match.group\("host"\).lower\(\) not in server.allowed_hosts:', "if match is None:"),
    )
    with pytest.raises(_FAILS):
        la.test_wrong_host_decision_is_403(env)


def test_mutant_decision_outside_the_write_gate(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    la, _, _, weaken = _later_mutants()
    weaken(monkeypatch, Desk, "_decide_later", (re.escape("with self._write(conn):"), "if True:"))
    with pytest.raises(_FAILS):
        la.test_decision_waits_in_the_write_gate(env, monkeypatch)


def test_mutant_desk_accepts_no_approvers(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    la, _, _, weaken = _later_mutants()
    weaken(
        monkeypatch,
        Desk,
        "__init__",
        (re.escape('if config.later_mode == "approval" and not config.approvers:'), "if False:"),
    )
    with pytest.raises(_FAILS):
        la.test_approver_settings_are_checked(tmp_path)


def test_mutant_repair_list_lacks_the_new_triggers(monkeypatch: pytest.MonkeyPatch, env: Any) -> None:
    """If db._migrate no longer repairs the two new triggers, the repair test fails."""
    la, _, _, _ = _later_mutants()
    cut = tuple(x for x in db._VIEW_AND_INDEXES if not x.startswith("later_decisions"))
    monkeypatch.setattr(db, "_VIEW_AND_INDEXES", cut)
    with pytest.raises(_FAILS):
        la.test_old_book_trigger_repair_both_new_triggers(env)
