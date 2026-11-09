"""Mutation guards (test side only, nothing in src). Each shows the tests can detect one fault: it
weakens the desk, re-runs a real test body and requires the expected AssertionError. Not a proof of correctness."""

from __future__ import annotations

import importlib.resources
import inspect
import re
import sqlite3
import sys
import textwrap
import threading
from typing import Any

import pytest
import test_flow
import test_http
from test_flow import cross_source_dupe_body, retry_twice_body

from lead_desk import identity, server
from lead_desk.desk import Desk

UNIQUE_INDEX = "leads_email_key_uq"
UNIQUE_STATEMENT = "CREATE UNIQUE INDEX IF NOT EXISTS leads_email_key_uq ON leads(email_key);"


def schema_without_unique_key() -> str:
    sql = importlib.resources.files("lead_desk").joinpath("schema.sql").read_text(encoding="utf-8")
    assert sql.count(UNIQUE_STATEMENT) == 1, "the unique index statement is missing or doubled"
    weak = sql.replace(UNIQUE_STATEMENT, "")
    assert UNIQUE_INDEX in sql and UNIQUE_INDEX not in weak  # the weakened version really lacks the index
    return weak


def test_mutant_no_unique_key(env, monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[bool] = []

    def no_such_lead(self: Desk, conn: Any, email_key: str) -> None:
        applied.append(True)

    monkeypatch.setattr(Desk, "_lookup_email_key", no_such_lead)
    rig = env.rig(schema_sql=schema_without_unique_key())
    with pytest.raises(AssertionError, match=re.escape("leads == 1")) as info:
        cross_source_dupe_body(rig)
    assert applied, "the weakened lookup was never called: the mutation was not applied"
    assert "got 2" in str(info.value)


def test_mutant_no_claim(env, monkeypatch: pytest.MonkeyPatch) -> None:
    applied: list[bool] = []
    lock = threading.Lock()
    both_updated = threading.Barrier(2, timeout=20)  # neither caller leaves the claim before both rows were updated
    original_claim = Desk._claim

    def always_claimed(
        self: Desk, conn_or_none: Any, lead_id: str, from_states: tuple[str, ...] = ("pending",)
    ) -> bool:
        """The weakened claim of a person's Retry (from_states with `failed`): moves the row to `sending` WITHOUT the
        state precondition and always says yes, once both callers have updated. The automatic drivers (reaper,
        start-up) keep the real claim, so the scenario can first bring the follow-up to `failed` through the real
        retries."""
        if from_states == ("pending",):
            return original_claim(self, conn_or_none, lead_id, from_states)
        with lock:
            applied.append(True)
        con = sqlite3.connect(self.config.db_path, isolation_level=None, timeout=10)
        try:
            con.execute("UPDATE followups SET state='sending', attempts=attempts+1 WHERE lead_id=?", (lead_id,))
        finally:
            con.close()
        # Without this wait the stand-in is racy: a caller that reads its ownership token after the other one's update
        # holds a stale token, loses it at the first touch and sends nothing, so the doubled send would not show.
        both_updated.wait()
        return True

    monkeypatch.setattr(Desk, "_claim", always_claimed)
    # A second line of defence: a blind send is not started while another send of the SAME lead is still
    # running (`_bounded(..., exclusive=True)`). That would hide the weakened claim from this test, which exists to show
    # the claim's own necessity, so the stand-in switches that second line off. The claim-free path is covered by its
    # own tests in test_hung_adapter_calls.py (`test_blind_retry_waits_for_old_send`).
    original_bounded = Desk._bounded

    def not_exclusive(self: Desk, *args: Any, **kwargs: Any) -> Any:
        kwargs["exclusive"] = False
        return original_bounded(self, *args, **kwargs)

    monkeypatch.setattr(Desk, "_bounded", not_exclusive)
    rig = env.rig(outbox="blind")
    with pytest.raises(AssertionError, match=re.escape("emails == 1")) as info:
        retry_twice_body(rig)
    assert applied, "the weakened claim was never called: the mutation was not applied"
    assert "got 2" in str(info.value)


# -- more guards. Each plants one fault, re-runs a real test and requires that test to fail with the given text.
FAILS = (AssertionError, KeyError, pytest.fail.Exception)  # how a real test reports a caught fault


def weaken(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, *subs: tuple[str, str]) -> None:
    """Rewrite the source of owner.name (each regex must match exactly once), compile it in the module's own
    namespace and swap it in. Nothing in src changes."""
    fn = getattr(owner, name)
    src = textwrap.dedent(inspect.getsource(fn))
    for pattern, repl in subs:
        src, n = re.subn(pattern, repl, src)
        assert n == 1, f"mutation pattern {pattern!r} matched {n} times in {name}"
    ns = dict(vars(sys.modules[fn.__module__]))
    exec(compile(src, f"<mutant {name}>", "exec"), ns)  # noqa: S102 (test-only; the source is our own)
    monkeypatch.setattr(owner, name, ns[name])


def schema_with(pattern: str, repl: str = "") -> str:
    sql = importlib.resources.files("lead_desk").joinpath("schema.sql").read_text(encoding="utf-8")
    weak, n = re.subn(pattern, repl, sql, flags=re.DOTALL)
    assert n == 1, f"schema pattern {pattern!r} matched {n} times"
    return weak


class WeakSchemaEnv:
    """Hands out rigs built on a weakened schema; everything else is the real fixture."""

    def __init__(self, env: Any, sql: str) -> None:
        self._env, self._sql = env, sql

    def rig(self, **kw: Any) -> Any:
        return self._env.rig(schema_sql=self._sql, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._env, name)


def drop_trigger(name: str) -> str:
    return schema_with(rf"CREATE TRIGGER IF NOT EXISTS {name}\b.*?\bEND;")


def test_mutant_claim_no_rowcount(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim ignores how many rows it changed: a second Retry also thinks it won and sends again."""
    weaken(monkeypatch, Desk, "_claim", (r"\.rowcount != 1:", ".rowcount < 0:"))
    # the second Retry must lose; otherwise there are two emails or two 200 answers
    with pytest.raises(AssertionError, match=r"emails == 1|== \[200, 409\]"):
        retry_twice_body(env.rig(outbox="blind"))


@pytest.mark.parametrize(
    ("point", "kind", "old", "new", "text"),
    [
        # D2: the email may have left (send_started and crm_done are 1), so the row needs the sender's mode
        ("D2", "keyed", "int(keyed),", "0,", "unknown"),  # a keyed row is parked as unknown instead of put back
        # blind put back to pending: the second layer (ambiguous_prior) still parks it, with another last_error
        ("D2", "blind", "int(keyed),", "1,", "crash_while_sending"),
        # D1: killed before the send mark, so nothing left: a keyed row is put back and sent
        ("D1", "keyed", "WHERE state='sending'", "WHERE state='no_such_state'", "D1 keyed: emails"),
        # D1 blind: without the send_started test the row is parked as unknown although no email could have left
        (
            "D1",
            "blind",
            "WHEN crm_done <> 1 OR send_started = 0 THEN 'pending'",
            "WHEN crm_done <> 1 THEN 'pending'",
            "D1 blind: emails",
        ),
    ],
)
def test_mutant_sweep(
    env, monkeypatch: pytest.MonkeyPatch, point: str, kind: str, old: str, new: str, text: str
) -> None:
    weaken(monkeypatch, Desk, "_requeue_sending", (re.escape(old), new.replace("\\", "\\\\")))
    with pytest.raises(AssertionError, match=re.escape(text)):
        test_flow.test_crash_resume_each_point(env, point, kind, "keyed")


def test_mutant_sweep_blind_both_layers(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both guards against a blind re-send are removed (the sweep puts the row back to pending AND the send step no
    longer parks an ambiguous_prior row): the email is sent twice and the test must see it."""
    weaken(monkeypatch, Desk, "_requeue_sending", (r"int\(keyed\),", "1,"))
    weaken(monkeypatch, Desk, "_send_claimed", (r'if self\.outbox\.capability == "blind":', "if False:"))
    with pytest.raises(AssertionError, match=re.escape("D2 blind: emails")):
        test_flow.test_crash_resume_each_point(env, "D2", "blind", "keyed")


@pytest.mark.parametrize(
    ("trigger", "statement", "text"),
    [
        ("followups_sent_final", "UPDATE followups SET state='failed'", "DID NOT RAISE"),
        ("receipts_outcome_once", "UPDATE receipts SET outcome='review'", "DID NOT RAISE"),
        ("receipts_immutable", "UPDATE receipts SET payload='{}'", "DID NOT RAISE"),
        (
            "receipts_no_delete",
            "DELETE FROM receipts",
            "FOREIGN KEY constraint failed",
        ),  # a foreign key still blocks it, with a different message
        ("leads_no_update", "UPDATE leads SET name='x'", "DID NOT RAISE"),
        ("leads_no_delete", "DELETE FROM leads", "FOREIGN KEY constraint failed"),
    ],
)
def test_mutant_trigger_dropped(env, trigger: str, statement: str, text: str) -> None:
    with pytest.raises(FAILS, match=text) as info:
        test_flow.test_write_once_triggers(WeakSchemaEnv(env, drop_trigger(trigger)))
    assert any(statement in str(e.statement) for e in info.traceback), f"the failing line was not {statement!r}"


def test_mutant_identity_case(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity key keeps the typed case: ADA@ and Ada@ become two people."""
    monkeypatch.setattr(identity, "normalise_email", lambda s: s.strip())
    with pytest.raises(AssertionError, match=re.escape("leads == 1")):
        cross_source_dupe_body(env.rig())


def test_mutant_identity_trim(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Field values are not trimmed: an address with spaces around it is refused as invalid."""
    weaken(monkeypatch, identity, "check_payload", (r"t = v\.strip\(\)", "t = v"))
    with pytest.raises(AssertionError, match=re.escape("202 == 200")):
        cross_source_dupe_body(env.rig())


def test_mutant_followup_own_commit(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead is committed before its follow-up row is written; a crash between leaves a lead with no follow-up."""
    commit_first = 'conn.execute("COMMIT"); conn.execute("BEGIN IMMEDIATE"); conn.execute("INSERT INTO followups'
    weaken(monkeypatch, Desk, "_decide_once", (r'conn\.execute\(\s*"INSERT INTO followups', commit_first))
    with pytest.raises(AssertionError, match=re.escape("A2 keyed: emails")):
        test_flow.test_crash_resume_each_point(env, "A2", "keyed", "keyed")


def test_mutant_receipt_not_durable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The receipt transaction stays open while the work runs: a crash at point A loses the receipt."""
    weaken(
        monkeypatch,
        Desk,
        "_process",
        (
            r"([ \t]*)with self\._write\(conn\):  # transaction 1[^\n]*",
            '\\1conn.execute("BEGIN IMMEDIATE")\n\\1while True:',
        ),
    )
    with pytest.raises(AssertionError, match=re.escape("A keyed: emails")):
        test_flow.test_crash_resume_each_point(env, "A", "keyed", "keyed")


def test_mutant_send_before_reply(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server runs the follow-up before it closes the reply: the caller waits for the send."""
    weaken(
        monkeypatch,
        server._Handler,  # noqa: SLF001
        "_dispatch",
        (r"self\._flush_and_half_close\(\)\n(\s*)except Exception as e:", "pass\n\\1except Exception as e:"),
    )
    with pytest.raises(AssertionError, match="the reply took"):
        test_http.test_reply_does_not_wait_for_send(env)


@pytest.mark.parametrize(
    ("name", "pattern", "repl", "text"),
    [
        (  # a replay is reported as a first-time answer
            "_process",
            r'(if rec\["outcome"\] is not None[:]\s*return self\._verdict_reply\(rec, )True(, matches\))',
            r"\1False\2",
            "False is True",
        ),
        (  # a replay of `new` drives the follow-up even when it is already sent
            "_verdict_reply",
            r'elif self\._pending_and_due\(rec\["outcome_lead_id"\]\):',
            "elif True:",
            "is None",
        ),
    ],
)
def test_mutant_replay(env, monkeypatch: pytest.MonkeyPatch, name: str, pattern: str, repl: str, text: str) -> None:
    weaken(monkeypatch, Desk, name, (pattern, repl))
    with pytest.raises(FAILS, match=text):
        test_flow.test_replay_same_source_ref(env)


def test_mutant_incomplete_not_reviewed(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The completeness check is skipped: a lead without a name gets past review and ends in a 500."""
    weaken(monkeypatch, identity, "check_payload", (r"if missing:", "if False:"))
    with pytest.raises(FAILS, match="incomplete:name"):
        test_flow.test_incomplete_to_review(env)


def test_mutant_review_list_hides_review(env) -> None:
    """The review list no longer shows receipts sent to review."""
    sql = schema_with(r"FROM receipts WHERE outcome = 'review'", "FROM receipts WHERE outcome = 'none'")
    with pytest.raises(AssertionError, match=r"assert 0 =="):
        test_flow.test_incomplete_to_review(WeakSchemaEnv(env, sql))
