"""Findings of the CodeAnt review about desk.py, as tests: (1) a private in-memory lead book is refused (each request
thread opens its own connection, so every thread but the first would see no tables); (2) while an abandoned CRM copy
call for a lead is still running, the next copy for THAT lead is not started (a late old stage must not overtake a
newer one), and (3) that refusal does not stop the copy of the other leads in the same pass. Made-up data only."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from support import Clock, CountingCrm, KeyedOutbox, RecordedQualifier, lead
from test_lead_crm_copy import _flow, _pass

from demo_only.fakes import FakeCrm
from lead_desk import server
from lead_desk.config import Config
from lead_desk.constants import EXIT_DESK_SETTING_REFUSED
from lead_desk.desk import Desk, DeskSettingRefused

MAIN_WAIT_SECONDS = 30.0  # a mutant run can shorten this


def test_a_private_in_memory_book_is_refused(tmp_path: Path) -> None:
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    config = Config(db_path=Path(":memory:"), port=0, replies_path=replies)
    with pytest.raises(DeskSettingRefused, match="must be a file"):
        Desk(config, FakeCrm(), KeyedOutbox(), RecordedQualifier(replies), now=Clock())


def _main_within(seconds: float = 30.0) -> int | None:
    """Run `server.main()` in a daemon thread and give it `seconds`: a refusal that regressed far enough to reach
    `serve_forever` is a FAILURE here, not a hung test run. Returns the exit code of the SystemExit, or None."""
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            server.main()
        except BaseException as e:  # noqa: BLE001 - SystemExit is what is looked at
            outcome.append(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"server.main() was still running after {seconds} s: it reached the serving loop"
    assert outcome and isinstance(outcome[0], SystemExit), outcome
    code = outcome[0].code
    return code if isinstance(code, int) or code is None else None


def test_main_refuses_a_memory_book_before_the_lock_step_and_makes_no_file(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    """The lock file is named after the book (`<path>.lock`): a refusal that came after the lock step left a stray
    `:memory:.lock` in the working folder at every refused start."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("DESK_DB_PATH", ":memory:")
    monkeypatch.setenv("DESK_PORT", "0")
    assert _main_within(MAIN_WAIT_SECONDS) == EXIT_DESK_SETTING_REFUSED
    assert "must be a file" in capsys.readouterr().out
    assert sorted(p.name for p in work.iterdir()) == [], "the refusal left files behind"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cwd"]


def test_main_refuses_a_memory_book_even_when_server_desk_is_replaced(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    """The refusal must not reach for the name `server.Desk` (a test or a wrapper may replace it): it runs from its
    own function, before the desk is built, so the refusal still comes (and the stand-in is never called)."""
    called: list[str] = []

    def stand_in(*a: Any, **k: Any) -> Any:
        called.append("built")
        raise ValueError("some other problem")

    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("DESK_DB_PATH", ":memory:")
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setattr(server, "Desk", stand_in)
    assert _main_within(MAIN_WAIT_SECONDS) == EXIT_DESK_SETTING_REFUSED
    assert "must be a file" in capsys.readouterr().out
    assert called == []
    assert sorted(p.name for p in work.iterdir()) == []


class _HangingFirstCrm(CountingCrm):
    """The first set_fields call (of the lead named in `hang_for`, or of any lead when that is None) blocks until
    released: an answer that comes back after the desk gave up on it."""

    def __init__(self, hang_for: str | None = None) -> None:
        super().__init__()
        self.release = threading.Event()
        self.started: list[tuple[str, str]] = []  # (lead id, status) of every call that started
        self.hang_for = hang_for
        self._guard = threading.Lock()
        self._hung = False

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        with self._guard:
            self.started.append((lead_id, crm_status))
            hang = not self._hung and self.hang_for in (None, lead_id)
            self._hung = self._hung or hang
        if hang:
            self.release.wait(10)
        super().set_fields(lead_id, email_key, crm_owner_id, crm_status)


def _wait_until_no_call_runs(rig: Any) -> None:
    for _ in range(200):
        if not rig.desk._tagged:
            return
        threading.Event().wait(0.05)
    raise AssertionError("an abandoned call never ended")


def _move_on(rig: Any) -> None:
    con = sqlite3.connect(rig.db_path)
    con.execute("UPDATE lead_state SET step='qualified', step_rank=3")
    con.commit()
    con.close()


def test_a_newer_stage_is_not_sent_while_an_abandoned_older_call_still_runs(env: Any) -> None:
    crm = _HangingFirstCrm()
    rig = env.rig(leadflow=_flow(), crm=crm, send_timeout_seconds=0.3)
    assert rig.desk.handle("form", "x-1", lead()).status == 201
    try:
        assert _pass(rig)["mirrored"] == 0  # the first call hangs past its 0.3 s: abandoned, one try used
        _move_on(rig)  # the stage moved on meanwhile
        _pass(rig)  # the next pass: the first call is still running
        assert len(crm.started) == 1, crm.started  # the newer stage waits; it never races the older one
    finally:
        crm.release.set()
    _wait_until_no_call_runs(rig)  # once the old call has ended, the newer stage goes through
    assert _pass(rig)["mirrored"] == 1
    assert crm.started[-1][1] == "OPEN_DEAL"


def test_a_lead_whose_older_call_still_runs_does_not_stop_the_copy_of_the_other_leads(env: Any) -> None:
    """Lead A is the oldest row and its abandoned call is still running; lead B comes after it. The refusal for A is
    that lead's own: B is copied in the SAME pass, A is not asked again, no try of A is charged for the refusal, and A
    waits (it no longer sits at the head of the selection) until its old call has ended."""
    flow = _flow()
    crm = _HangingFirstCrm()
    clock = Clock()
    rig = env.rig(leadflow=flow, crm=crm, send_timeout_seconds=0.3, clock=clock)
    a = rig.desk.handle("form", "x-a", lead(email="ada@example.test")).body["lead_id"]
    clock.advance(5)  # B is a newer row: the copy pass takes the oldest first
    b = rig.desk.handle("form", "x-b", lead(email="bob@example.test")).body["lead_id"]
    crm.hang_for = a
    try:
        _pass(rig)  # A hangs past its 0.3 s and is abandoned: one try used, the pass ends
        assert [x for x, _ in crm.started] == [a]
        tries_a = rig.query("SELECT crm_tries FROM lead_state WHERE lead_id=?", (a,))[0][0]
        assert tries_a == 1
        rig.desk._mirror_not_before._d.clear()  # the failed try's wait is not what this test is about
        done = {"mirrored": 0}
        rig.desk._mirror_pass(done)  # A is first in line again, its old call still runs
        assert [x for x, _ in crm.started] == [a, b], crm.started  # A was not asked again; B was asked
        assert done["mirrored"] == 1  # and B was copied
        assert rig.query("SELECT crm_tries FROM lead_state WHERE lead_id=?", (a,))[0][0] == tries_a  # no try charged
        assert a in rig.desk._mirror_not_before  # A waits, so it leaves the selection
        rig.desk._mirror_pass({"mirrored": 0})  # the wait holds: A is not even looked at
        assert [x for x, _ in crm.started] == [a, b]
    finally:
        crm.release.set()
    _wait_until_no_call_runs(rig)
    assert _pass(rig)["mirrored"] == 1  # the old call has ended: A is copied now
    assert [x for x, _ in crm.started] == [a, b, a]


def test_busy_lead_waits_longer_each_time(env: Any) -> None:
    """Refused again and again because its own old call still hangs: the wait grows (10, 60, 180 s ...) instead of
    staying at the first step, so a call that never ends is looked at ever less often. The list starts higher for a
    lead with earlier failed tries (index max(tries_before, 1) - 1 + refusals - 1); here one try was used, so it
    starts at 10 s. A count kept for another stage is not carried over: the next refusal starts at 10 s again."""
    crm = _HangingFirstCrm()
    clock = Clock()
    rig = env.rig(leadflow=_flow(), crm=crm, send_timeout_seconds=0.3, clock=clock)
    a = rig.desk.handle("form", "x-a", lead()).body["lead_id"]
    waits: list[float] = []
    try:
        _pass(rig)  # the first call hangs and is abandoned: one try used
        for _ in range(4):
            rig.desk._mirror_not_before._d.clear()
            before = time.monotonic()
            rig.desk._mirror_pass({"mirrored": 0})  # refused: its own old call still runs
            waits.append(round(rig.desk._mirror_not_before._d[a][0] - before))
        assert waits[0] == 10 and waits == sorted(waits) and waits[1] > waits[0] and waits[-1] > waits[1], waits
        assert len(crm.started) == 1  # never asked again while the old call runs
    finally:
        crm.release.set()
    _wait_until_no_call_runs(rig)
    rig.desk._mirror_not_before._d.clear()
    assert _pass(rig)["mirrored"] == 1
    assert a not in rig.desk._mirror_busy


def test_busy_count_of_another_stage_is_not_carried_over(env: Any) -> None:
    """A count kept while the lead was at an earlier stage must not push its next refusal to a long wait."""
    crm = _HangingFirstCrm()
    rig = env.rig(leadflow=_flow(), crm=crm, send_timeout_seconds=0.3, clock=Clock())
    a = rig.desk.handle("form", "x-a", lead()).body["lead_id"]
    try:
        _pass(rig)  # the first call hangs and is abandoned: one try used
        rig.desk._mirror_busy[a] = (-1, 3)  # three refusals counted at some other stage rank
        rig.desk._mirror_not_before._d.clear()
        before = time.monotonic()
        rig.desk._mirror_pass({"mirrored": 0})
        assert round(rig.desk._mirror_not_before._d[a][0] - before) == 10
        assert rig.desk._mirror_busy[a][1] == 1
    finally:
        crm.release.set()
    _wait_until_no_call_runs(rig)
