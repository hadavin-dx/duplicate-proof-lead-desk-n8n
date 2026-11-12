"""Guards that each plant one fault on a hot path (a mutant) and require a test to catch it.

Every test names the function (or schema rule) it pins in its docstring, not a line number, so the guard stays
correct when lines move. Made-up data only; the test clock is explicit and nothing here sleeps."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from support import Rig, lead, rows, run_threads, sql_exec

from demo_only.fakes import BlindOutbox, FakeCrm
from lead_desk import db
from lead_desk.adapters import CrmNothingStored
from lead_desk.constants import BUSY_WAIT_SECONDS, SOURCE_REF_MAX
from lead_desk.desk import Desk
from lead_desk.gate import SEND_MAX_ATTEMPTS, SENDING_GRACE_BUSY_WAITS

SCHEMA = Path(db.__file__).with_name("schema.sql")
OPEN_PAYLOAD = '{"email":"%s","name":"Ada"}'
ROUNDS = 30


# -- helpers
def new_lead(rig: Rig, ref: str, email: str | None = None) -> str:
    """Receipt and decision only (no follow-up drive); returns the lead id."""
    reply = rig.desk.process("form", ref, lead(email=email or f"{ref}@example.test"))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def follow(rig: Rig, lid: str, cols: str) -> tuple[Any, ...]:
    return rig.query(f"SELECT {cols} FROM followups WHERE lead_id=?", (lid,))[0]  # noqa: S608 (fixed column text)


def set_follow(rig: Rig, lid: str, **cols: Any) -> None:
    names = ", ".join(f"{k}=?" for k in cols)
    sql_exec(rig.db_path, f"UPDATE followups SET {names} WHERE lead_id=?", (*cols.values(), lid))  # noqa: S608


def schema_book(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "raw.sqlite3", isolation_level=None)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    return con


def raw_receipt(con: sqlite3.Connection, ref: str, source: str = "form") -> int:
    cur = con.execute(
        "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES(?,?,1,'{}')", (source, ref)
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def raw_lead(con: sqlite3.Connection, lead_id: str, receipt_id: int, key: str) -> None:
    con.execute(
        "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at)"
        " VALUES(?,?,'form','Ada',?,?,1)",
        (lead_id, receipt_id, key, key),
    )


# =====
# the unique key on leads.email_key (schema.sql unique_email_key)
# =====
def test_email_key_unique_in_sql(tmp_path: Path) -> None:
    """schema.sql: CREATE UNIQUE INDEX leads_email_key_uq. Two lead rows with one email_key are refused."""
    con = schema_book(tmp_path)
    try:
        raw_lead(con, "L000000000001", raw_receipt(con, "a"), "ada@example.test")
        raw_lead(con, "L000000000003", raw_receipt(con, "c"), "bob@example.test")  # a different key is fine
        with pytest.raises(sqlite3.IntegrityError, match="email_key"):
            raw_lead(con, "L000000000002", raw_receipt(con, "b", "webhook"), "ada@example.test")
        assert con.execute("SELECT count(*) FROM leads").fetchone()[0] == 2
    finally:
        con.close()


def test_email_key_race_only_index(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The code-level lookup says 'not found' every time, so only the unique index can stop the second lead. The
    desk then gives the loser verdict review / internal_unstorable:integrity (a refused insert is
    'cannot be stored', desk.py _UNSTORABLE), never a second lead and never a 500."""
    monkeypatch.setattr(Desk, "_lookup_email_key", lambda self, conn, key: None)
    rig = env.rig()
    for i in range(ROUNDS):
        email = f"race{i}@example.test"
        replies = run_threads(
            [
                lambda i=i, email=email: rig.desk.process("form", f"f-{i}", lead(email=email)),
                lambda i=i, email=email: rig.desk.process("webhook", f"w-{i}", lead(email=email)),
            ]
        )
        assert not any(isinstance(r, BaseException) for r in replies), replies
        statuses = sorted(r.status for r in replies)
        assert statuses in ([200, 201], [201, 202]), f"round {i}: {[r.body for r in replies]}"
        for r in replies:
            if r.status == 202:
                assert r.body["reason"] == "internal_unstorable:integrity", r.body
            if r.status == 200:
                assert r.body["verdict"] == "duplicate"
        n = rig.query("SELECT count(*) FROM leads WHERE email_key=?", (email,))[0][0]
        assert n == 1, f"round {i}: {n} lead rows for one email_key"
    assert rig.leads() == ROUNDS
    assert rig.query("SELECT count(*) FROM followups")[0][0] == ROUNDS, "a refused lead left a follow-up behind"
    assert rig.query("SELECT count(*) FROM receipts WHERE outcome IS NULL")[0][0] == 0


# =====
# the 1..128 length rule of source_ref, in the schema and in the desk
# =====
def test_source_ref_bounds_in_sql(tmp_path: Path) -> None:
    """schema.sql: CHECK (length(source_ref) BETWEEN 1 AND 128): 128 fits, 129 and 0 are refused."""
    con = schema_book(tmp_path)
    try:
        raw_receipt(con, "x" * SOURCE_REF_MAX)
        raw_receipt(con, "y")
        for bad in ("z" * (SOURCE_REF_MAX + 1), ""):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                raw_receipt(con, bad)
    finally:
        con.close()


def test_source_ref_128_through_desk(env) -> None:
    """The desk accepts a 128-character request id (and the book stores it), and refuses 129."""
    rig = env.rig()
    ok = rig.desk.process("form", "k" * SOURCE_REF_MAX, lead())
    assert ok.status == 201, ok.body
    assert rig.query("SELECT length(source_ref) FROM receipts")[0][0] == SOURCE_REF_MAX
    bad = rig.desk.process("webhook", "k" * (SOURCE_REF_MAX + 1), lead(email="b@example.test"))
    assert (bad.status, bad.body) == (400, {"error": "bad_envelope", "receipt_id": None})
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 1


def test_source_ref_one_char_and_empty(env) -> None:
    """desk.py SOURCE_REF_MIN <= len: one character is accepted, an empty request id is refused."""
    rig = env.rig()
    assert rig.desk.process("form", "q", lead()).status == 201
    empty = rig.desk.process("form", "", lead(email="e@example.test"))
    assert (empty.status, empty.body["error"]) == (400, "bad_envelope")
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 1


# =====
# the claim (desk.py _CLAIM_SQL, _claim)
# =====
@pytest.mark.parametrize(
    ("states", "start"),
    [(("pending",), "pending"), (("pending", "failed"), "pending"), (("pending", "failed"), "failed")],
)
def test_claim_clears_send_started(env, states: tuple[str, ...], start: str) -> None:
    """Every claim writes send_started=0, and one more attempt; a claimed failed row restarts the count at 1."""
    rig = env.rig()
    lid = new_lead(rig, "req-1")
    set_follow(rig, lid, state=start, send_started=1, attempts=3, auto_attempts=4)
    assert rig.desk._claim(None, lid, states) is True
    state, started, attempts, auto = follow(rig, lid, "state, send_started, attempts, auto_attempts")
    assert (state, started) == ("sending", 0), "a claim must clear the send mark"
    assert attempts == 4, "a claim writes attempts + 1 (the ownership token)"
    assert auto == (1 if start == "failed" else 5)


@pytest.mark.parametrize("states", [("pending",), ("pending", "failed")])
@pytest.mark.parametrize("start", ["sending", "unknown", "sent"])
def test_claim_needs_the_right_state(env, states: tuple[str, ...], start: str) -> None:
    """No claim takes a row that is sending, unknown or sent: the row is left exactly as it was."""
    rig = env.rig()
    lid = new_lead(rig, "req-2")
    set_follow(rig, lid, state=start, attempts=2, auto_attempts=2, send_started=1)
    before = follow(rig, lid, "state, attempts, auto_attempts, send_started, updated_at")
    assert rig.desk._claim(None, lid, states) is False
    assert follow(rig, lid, "state, attempts, auto_attempts, send_started, updated_at") == before


# =====
# the reaper's age boundaries
# =====
def stuck_age(rig: Rig) -> int:
    cfg = rig.config
    return int(cfg.send_timeout_seconds + SENDING_GRACE_BUSY_WAITS * BUSY_WAIT_SECONDS)


def test_requeue_age_boundary_direct(env) -> None:
    """desk.py _requeue_sending: updated_at < older_than (strictly older). A row exactly on the line is left,
    one second older is requeued."""
    rig = env.rig()
    on, older = new_lead(rig, "req-3"), new_lead(rig, "req-4")
    line = rig.clock.t - 100
    set_follow(rig, on, state="sending", attempts=1, auto_attempts=1, updated_at=line)
    set_follow(rig, older, state="sending", attempts=1, auto_attempts=1, updated_at=line - 1)
    rig.desk._requeue_sending(line)
    assert follow(rig, older, "state, last_error") == ("pending", "crash_while_sending")
    assert follow(rig, on, "state") == ("sending",)


def test_requeue_age_boundary_in_reaper(env) -> None:
    """The reaper pass: a sending row exactly the stuck grace old (send timeout + 5 busy waits) is left; the clocks
    count whole seconds, so a difference equal to the grace can be a real age of just over grace - 1 s, and only a
    row strictly older than the grace is put back (one second older is)."""
    rig = env.rig()
    age = stuck_age(rig)
    assert age == 30, "default send timeout 5 s plus 5 busy waits of 5 s"
    on, older = new_lead(rig, "req-5"), new_lead(rig, "req-6")
    set_follow(rig, on, state="sending", attempts=1, auto_attempts=1, updated_at=rig.clock.t - age)
    set_follow(rig, older, state="sending", attempts=1, auto_attempts=1, updated_at=rig.clock.t - age - 1)
    rig.desk.reap_once()
    assert follow(rig, older, "state, last_error") == ("pending", "crash_while_sending")
    assert follow(rig, on, "state") == ("sending",)


def open_receipt(rig: Rig, ref: str, at: int) -> None:
    sql_exec(
        rig.db_path,
        "INSERT INTO receipts(source, source_ref, received_at, payload) VALUES('form', ?, ?, ?)",
        (ref, at, OPEN_PAYLOAD % f"{ref}@example.test"),
    )


def outcome(rig: Rig, ref: str) -> Any:
    return rig.query("SELECT outcome FROM receipts WHERE source_ref=?", (ref,))[0][0]


def test_stalled_receipt_age_boundary(env) -> None:
    """desk.py reap_once: received_at <= now - stall_seconds. Exactly stall_seconds old is decided; one second
    younger is left open."""
    rig = env.rig()
    stall = rig.config.stall_seconds
    open_receipt(rig, "s-on", rig.clock.t - stall)
    open_receipt(rig, "s-young", rig.clock.t - stall + 1)
    rig.desk.reap_once()
    assert outcome(rig, "s-on") == "new"
    assert outcome(rig, "s-young") is None


def test_future_receipt_by_own_clock(env) -> None:
    """desk.py _stalled_by_own_clock: an open receipt stamped in the future is decided once the reaper has seen it
    open for stall_seconds by the desk's own clock (not one second sooner)."""
    rig = env.rig()
    stall = rig.config.stall_seconds
    open_receipt(rig, "fut-1", rig.clock.t + 1_000_000)
    rig.desk.reap_once()  # first sighting
    assert outcome(rig, "fut-1") is None
    rig.clock.advance(stall - 1)
    rig.desk.reap_once()
    assert outcome(rig, "fut-1") is None, "decided one second too early"
    rig.clock.advance(1)
    rig.desk.reap_once()
    assert outcome(rig, "fut-1") == "new"
    assert rig.leads() == 1


# =====
# more mutants: ambiguous_prior, the CRM intent mark, requeue rules, ownership tokens
# =====
def test_ambiguous_send_marks_the_row(env) -> None:
    """desk.py _record_send_result: an unclear end writes ambiguous_prior=1 (the next try then reads the mode)."""
    rig = env.rig()
    lid = new_lead(rig, "req-7")
    rig.outbox.fail_next("drop")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "state, last_error, ambiguous_prior") == ("pending", "ambiguous", 1)
    rig.clock.advance(60)
    rig.desk.reap_once()
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1


def test_crm_done_row_is_not_asked_again(env) -> None:
    """desk.py _crm_step: crm_done=1 goes straight on to the send, and the CRM is not asked."""
    rig = env.rig()
    lid = new_lead(rig, "req-8")
    set_follow(rig, lid, crm_done=1)
    rig.desk.run_followup(lid)
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1
    assert rig.crm.count == 0


class SpyCrm(FakeCrm):
    """Records crm_done as the book holds it at the moment of the put."""

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path, self.seen = db_path, []

    def put(self, lead_id: str, record: Any) -> Any:
        self.seen.append(rows(self.db_path, "SELECT crm_done FROM followups WHERE lead_id=?", (lead_id,))[0][0])
        return super().put(lead_id, record)


def test_crm_intent_mark_before_the_put(env) -> None:
    """desk.py _crm_step: crm_done=2 is written BEFORE the put and crm_done=1 after it came back."""
    rig = env.rig()
    rig.desk.crm = spy = SpyCrm(rig.db_path)
    lid = new_lead(rig, "req-9")
    rig.desk.run_followup(lid)
    assert spy.seen == [2], "the intent mark must be in the book when the put starts"
    assert follow(rig, lid, "crm_done, state") == (1, "sent")


class NothingStoredCrm(FakeCrm):
    def put(self, lead_id: str, record: Any) -> Any:
        raise CrmNothingStored("scripted refusal")


def test_proven_failed_put_resets_the_mark(env) -> None:
    """desk.py _crm_step: a put that proves nothing was stored puts crm_done back to 0 (not left at 2)."""
    rig = env.rig()
    rig.desk.crm = NothingStoredCrm()
    lid = new_lead(rig, "req-10")
    rig.desk.run_followup(lid)
    assert follow(rig, lid, "crm_done, state") == (0, "pending")
    assert rig.outbox.count == 0


def test_requeue_marks_only_started_sends(env) -> None:
    """desk.py _requeue_sending: ambiguous_prior is set only when the CRM row is done AND send_started=1."""
    rig = env.rig()
    unsent, started = new_lead(rig, "m1"), new_lead(rig, "m2")
    set_follow(rig, unsent, state="sending", attempts=1, auto_attempts=1, crm_done=1, send_started=0)
    set_follow(rig, started, state="sending", attempts=1, auto_attempts=1, crm_done=1, send_started=1)
    rig.desk._requeue_sending(None)
    assert follow(rig, unsent, "state, ambiguous_prior") == ("pending", 0)
    assert follow(rig, started, "state, ambiguous_prior") == ("pending", 1)


def test_blind_requeue_unsent_goes_pending(env) -> None:
    """desk.py _requeue_sending: on a blind sender a row that never started its send is pending, one that may have
    sent is unknown."""
    rig = env.rig(outbox=BlindOutbox())
    unsent, started = new_lead(rig, "m3"), new_lead(rig, "m4")
    set_follow(rig, unsent, state="sending", attempts=1, auto_attempts=1, crm_done=1, send_started=0)
    set_follow(rig, started, state="sending", attempts=1, auto_attempts=1, crm_done=1, send_started=1)
    rig.desk._requeue_sending(None)
    assert follow(rig, unsent, "state") == ("pending",)
    assert follow(rig, started, "state") == ("unknown",)


def test_requeue_mode_unknown_rule(env) -> None:
    """desk.py _requeue_sending(mode_known=False): only rows that never reached the send move; a row that may have
    sent is left alone."""
    rig = env.rig()
    cases = {
        "never_crm": {"crm_done": 0, "send_started": 0},
        "never_send": {"crm_done": 1, "send_started": 0},
        "maybe_sent": {"crm_done": 1, "send_started": 1},
    }
    ids = {}
    for name, cols in cases.items():
        ids[name] = new_lead(rig, name)
        set_follow(rig, ids[name], state="sending", attempts=1, auto_attempts=1, **cols)
    rig.desk._requeue_sending(None, mode_known=False)
    assert follow(rig, ids["never_crm"], "state") == ("pending",)
    assert follow(rig, ids["never_send"], "state") == ("pending",)
    assert follow(rig, ids["maybe_sent"], "state") == ("sending",)


def test_touch_only_for_the_owner(env) -> None:
    """desk.py _touch: a stale token (an older claim) changes nothing; the owner moves updated_at and sets the mark."""
    rig = env.rig()
    lid = new_lead(rig, "t1")
    set_follow(rig, lid, state="sending", attempts=2, auto_attempts=2, send_started=0, updated_at=1)
    assert rig.desk._touch(lid, 1, send_started=True) is False
    assert follow(rig, lid, "send_started, updated_at") == (0, 1)
    assert rig.desk._touch(lid, 3, send_started=True) is False, "a token from the future is not the owner either"
    assert rig.desk._touch(lid, 2, send_started=True) is True
    assert follow(rig, lid, "send_started, updated_at") == (1, rig.clock.t)


def test_abort_unsent_counts_one_try(env) -> None:
    """desk.py _abort_unsent: only the drive that owns the claim writes; the claim has
    already counted the try, so the count is not raised again; the row is `pending`, and `failed` at the cap."""
    rig = env.rig()
    lid = new_lead(rig, "req-11")
    set_follow(rig, lid, state="sending", attempts=2, auto_attempts=1)
    rig.desk._abort_unsent(lid, 1, ValueError("x"))  # a stale drive
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("sending", 1, None)
    rig.desk._abort_unsent(lid, 2, ValueError("x"))
    assert follow(rig, lid, "state, auto_attempts, last_error") == ("pending", 1, "internal:ValueError")
    set_follow(rig, lid, state="sending", attempts=3, auto_attempts=SEND_MAX_ATTEMPTS - 1)
    rig.desk._abort_unsent(lid, 3, ValueError("x"))
    assert follow(rig, lid, "state, auto_attempts") == ("pending", SEND_MAX_ATTEMPTS - 1)
    set_follow(rig, lid, state="sending", attempts=4, auto_attempts=SEND_MAX_ATTEMPTS)
    rig.desk._abort_unsent(lid, 4, ValueError("x"))
    assert follow(rig, lid, "state, auto_attempts") == ("failed", SEND_MAX_ATTEMPTS)
