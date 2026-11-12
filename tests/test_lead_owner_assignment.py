"""Lead owner, lead stage and reminder, first part: the two new tables, the owner given to every new lead,
and the guard around the two new reaper parts. Made-up data only (example.test, the demo rota Asha and Ravi)."""

from __future__ import annotations

import copy
import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from support import DEMO_LEADFLOW, CrashAt, lead
from test_mutants import FAILS, schema_with, weaken

from lead_desk.contract import SimulatedCrash
from lead_desk.desk import Desk


def _state_rows(rig: Any) -> list[tuple[Any, ...]]:
    return rig.query(
        "SELECT l.email_key, s.owner_id, s.owner_name, s.step, s.step_rank, s.assigned_at, s.changed_at"
        " FROM lead_state s JOIN leads l ON l.lead_id = s.lead_id ORDER BY l.created_at, l.lead_id"
    )


def test_owner_assigned_once(env) -> None:
    """Every NEW lead gets the next person of the list, in turn, and step New; a replay, a repeat enquiry and a
    request the desk files for review change nothing; with the feature off nothing is ever written."""
    rig = env.rig(leadflow=copy.deepcopy(DEMO_LEADFLOW))
    names = []
    for i in range(3):
        reply = rig.desk.process("form", f"own-{i}", lead(email=f"own{i}@example.test"))
        assert reply.status == 201, reply.body
        names.append(rig.query("SELECT owner_name FROM lead_state WHERE lead_id=?", (reply.body["lead_id"],))[0][0])
    assert names == ["Asha", "Ravi", "Asha"]  # in turn, the list is Asha, Ravi
    before = _state_rows(rig)
    assert [r[3:5] for r in before] == [("new", 0)] * 3
    assert all(r[5] == r[6] == rig.clock.t for r in before)
    replay = rig.desk.process("form", "own-0", lead(email="own0@example.test"))
    assert replay.status == 201 and replay.body["replayed"] is True  # a replay of the first request
    assert (
        rig.desk.process("webhook", "own-x", lead(email="OWN0@example.test", phone="555 0199")).status == 200
    )  # repeat
    assert rig.desk.process("form", "own-bad", {"name": "No Email"}).status in (200, 202)  # a review verdict
    assert _state_rows(rig) == before  # no row added, none changed
    assert rig.query("SELECT count(*) FROM nudges") == [(0,)]
    off = env.rig()  # the default: the feature is off
    assert off.desk.process("form", "off-0", lead()).status == 201
    assert off.query("SELECT count(*) FROM lead_state") == [(0,)]
    assert off.query("SELECT count(*) FROM nudges") == [(0,)]


def test_failing_owner_insert_never_unstorable(env) -> None:
    """A fault of the lead_state insert (forced by a schema CHECK that refuses the first owner, so every try fails the
    same way) is a desk-side fault: the request gets 500 and stays open, the third failure files review/internal_error,
    never internal_unstorable (the payload is not blamed), and no lead is stored."""
    weak = schema_with(
        re.escape("owner_id       TEXT NOT NULL,"), "owner_id       TEXT NOT NULL CHECK (owner_id <> 'o-1'),"
    )
    rig = env.rig(
        schema_sql=weak, leadflow=copy.deepcopy(DEMO_LEADFLOW)
    )  # no rig.restart(): it would migrate the schema
    assert rig.desk.process("form", "fail-1", lead()).status == 500
    assert rig.query("SELECT outcome, outcome_reason FROM receipts") == [(None, None)]
    for _ in range(2):
        rig.clock.advance(rig.config.stall_seconds + 5)
        rig.desk.reap_once()
        assert rig.query("SELECT outcome_reason FROM receipts")[0][0] != "internal_unstorable"
    assert rig.query("SELECT outcome, outcome_reason FROM receipts") == [("review", "internal_error")]
    body = rig.desk.counts(None).body
    assert body["decide_integrity_errors"] == 0, body  # never counted as an unstorable payload
    assert rig.query("SELECT count(*) FROM leads") == [(0,)]
    assert rig.query("SELECT count(*) FROM lead_state") == [(0,)]


def test_reaper_part_failure_does_not_starve_the_next(env, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The copy part raising (a database error, then any error with an address in its text) never stops the reminder
    part in the same pass; the pass still returns its counts; only the class name is logged; a simulated crash is not
    swallowed."""
    rig = env.rig(leadflow=copy.deepcopy(DEMO_LEADFLOW))
    ran: list[str] = []
    failures: list[BaseException] = [sqlite3.OperationalError("disk I/O error"), RuntimeError("secret@example.test")]

    def mirror(self: Desk, done: dict[str, int]) -> None:
        ran.append("copy")
        raise failures.pop(0)

    def nudge(self: Desk, done: dict[str, int]) -> None:
        ran.append("reminder")

    monkeypatch.setattr(Desk, "_mirror_pass", mirror)
    monkeypatch.setattr(Desk, "_nudge_pass", nudge)
    with caplog.at_level(logging.WARNING, logger="lead_desk"):
        for _ in range(2):
            done = rig.desk.reap_once()
            assert {"receipts", "driven", "mirrored", "reminders", "reminders_unknown"} <= set(done)
    assert ran == ["copy", "reminder", "copy", "reminder"]
    assert "OperationalError" in caplog.text and "RuntimeError" in caplog.text
    assert "secret@example.test" not in caplog.text and "disk I/O error" not in caplog.text

    def crash(self: Desk, done: dict[str, int]) -> None:
        raise SimulatedCrash("N1")

    monkeypatch.setattr(Desk, "_mirror_pass", crash)
    with pytest.raises(SimulatedCrash):  # a BaseException is not caught by the part guard
        rig.desk.reap_once()


# -- mutants: each plants one fault in the desk and requires the real test above to fail
def test_mutant_owner_not_in_turn(env, monkeypatch: pytest.MonkeyPatch) -> None:
    weaken(monkeypatch, Desk, "_decide_once", (re.escape("flow.rota[n % len(flow.rota)]"), "flow.rota[0]"))
    with pytest.raises(FAILS):
        test_owner_assigned_once(env)


def test_mutant_owner_insert_fault_blames_the_payload(env, monkeypatch: pytest.MonkeyPatch) -> None:
    weaken(
        monkeypatch,
        Desk,
        "_decide_once",
        (re.escape("except (sqlite3.IntegrityError, sqlite3.DataError) as e:"), "except ZeroDivisionError as e:"),
    )
    with pytest.raises(FAILS):
        test_failing_owner_insert_never_unstorable(env)


def test_mutant_part_guard_catches_the_wrong_class(env, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    weaken(
        monkeypatch,
        Desk,
        "reap_once",
        (re.escape("except Exception as e:  # noqa: BLE001 - one part"), "except ZeroDivisionError as e:  # noqa"),
    )
    with pytest.raises(FAILS + (sqlite3.OperationalError,)):
        test_reaper_part_failure_does_not_starve_the_next(env, monkeypatch, caplog)


def _owner_of_each(rig: Any) -> list[str]:
    return [r[0] for r in rig.query("SELECT owner_id FROM lead_state ORDER BY assigned_at, rowid")]


def test_parallel_new_leads_alternate_owners_exactly(env) -> None:
    """16 new leads arrive at the same moment: every one gets a row, and the two owners alternate with no gap and no
    repeat (the count and the insert sit in ONE write, so two leads can never read the same count)."""
    rig = env.rig(leadflow=copy.deepcopy(DEMO_LEADFLOW))
    barrier = threading.Barrier(16)

    def post(i: int) -> int:
        barrier.wait(timeout=30)
        return rig.desk.process("form", f"par-{i}", lead(email=f"par{i}@example.test")).status

    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(post, range(16)))
    assert statuses == [201] * 16
    owners = _owner_of_each(rig)
    assert len(owners) == 16 and rig.query("SELECT count(*) FROM lead_state WHERE owner_id='o-1'") == [(8,)], owners
    assert owners[::2] == [owners[0]] * 8 and owners[1::2] == [owners[1]] * 8 and owners[0] != owners[1], owners


def test_a_kill_at_a2_then_replay_does_not_skip_an_owner(env) -> None:
    """The owner count is taken inside the write that a kill at A2 rolls back: the replay gives the same lead the owner
    the killed try would have given, and the next new lead gets the next person (no owner is skipped or used twice)."""
    hooks = CrashAt()  # armed only after the first lead: A2 is passed by every new lead, one-shot per point
    rig = env.rig(leadflow=copy.deepcopy(DEMO_LEADFLOW), hooks=hooks)
    first = rig.desk.process("form", "k-0", lead(email="k0@example.test"))
    assert first.status == 201
    assert hooks.fired == []  # the first lead passed A2 unharmed: the kill below is the one that is tested
    hooks.points.add("A2")
    with pytest.raises(SimulatedCrash):
        rig.desk.process("form", "k-1", lead(email="k1@example.test"))
    assert rig.query("SELECT count(*) FROM lead_state") == [(1,)]  # the killed try left no row
    rig.restart()
    assert rig.desk.process("form", "k-1", lead(email="k1@example.test")).status == 201  # the replay
    assert rig.desk.process("form", "k-2", lead(email="k2@example.test")).status == 201
    assert hooks.fired == ["A2"]  # the kill really happened, once
    assert _owner_of_each(rig) == ["o-1", "o-2", "o-1"]


def test_mutant_owner_taken_from_a_counter_a_kill_does_not_roll_back(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the owner came from a counter that survives the killed try, the replay would skip a person: the kill test
    must fail."""
    monkeypatch.setattr(Desk, "_mut_n", -1, raising=False)
    weaken(
        monkeypatch,
        Desk,
        "_decide_once",
        (
            re.escape('(n,) = conn.execute("SELECT count(*) FROM lead_state").fetchone()'),
            "n = Desk._mut_n = Desk._mut_n + 1",
        ),
    )
    with pytest.raises(FAILS):
        test_a_kill_at_a2_then_replay_does_not_skip_an_owner(env)


def test_mutant_owner_count_read_before_the_write_starts(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading the count before BEGIN IMMEDIATE lets two leads read the same number: the parallel test must fail."""
    weaken(
        monkeypatch,
        Desk,
        "_decide_once",
        (
            re.escape("    with self._write(conn):"),
            "    n0 = conn.execute('SELECT count(*) FROM lead_state').fetchone()[0]\n"
            "    __import__('time').sleep(0.05)\n"
            "    with self._write(conn):",
        ),
        (re.escape('(n,) = conn.execute("SELECT count(*) FROM lead_state").fetchone()'), "n = n0"),
    )
    with pytest.raises(FAILS):
        test_parallel_new_leads_alternate_owners_exactly(env)
