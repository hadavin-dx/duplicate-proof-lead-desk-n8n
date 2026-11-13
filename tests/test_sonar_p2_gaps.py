"""Sonar fixes, patch P2 (delta): behaviours of the code P2 touched that no test pinned down. A result that is
not `sent` must not move the lead stage; a database error that is not a lock answer must reach the caller; the
warning 'expected one row, changed N' is logged exactly when the row count is not one; the line `desk listening on
...` is the text the scripts read. Made-up data only."""

from __future__ import annotations

import copy
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from support import DEMO_LEADFLOW, HOST, FakeClock, is_write, lead
from test_mirror_gate import _Crm, _flow, _sql

from lead_desk import server
from lead_desk.constants import LOCK_RETRIES
from lead_desk.desk import _GateBusy
from lead_desk.gate import WRITE_DEADLINE_SECONDS

WARN = "expected one row"


def _mid_send(env: Any, monkeypatch: pytest.MonkeyPatch, ref: str) -> tuple[Any, str, int]:
    """A desk with a step file, one lead whose follow-up is 'sending' (token known), stage 'new', copy wait set."""
    clock = FakeClock(monkeypatch)
    rig = env.rig(leadflow=_flow(), crm=_Crm())
    lid = str(rig.desk.handle("form", ref, lead()).body["lead_id"])
    for trigger in ("followups_sent_final", "lead_state_no_regress"):
        _sql(rig, f"DROP TRIGGER IF EXISTS {trigger}")
    _sql(rig, "UPDATE lead_state SET step='new', step_rank=0 WHERE lead_id=?", lid)
    _sql(rig, "UPDATE followups SET state='sending', crm_done=1 WHERE lead_id=?", lid)
    (token,) = rig.query("SELECT attempts FROM followups WHERE lead_id=?", (lid,))[0]
    rig.desk._mirror_not_before.set(lid, clock.now + 400, 0)
    return rig, lid, int(token)


def _step(rig: Any, lid: str) -> str:
    return str(rig.query("SELECT step FROM lead_state WHERE lead_id=?", (lid,))[0][0])


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if WARN in r.getMessage()]


# -- (a) only a recorded `sent` moves the stage
@pytest.mark.parametrize("state", ["unknown", "pending", "failed"])
def test_a_result_that_is_not_sent_leaves_the_stage_and_the_copy_wait(env, monkeypatch, state) -> None:
    rig, lid, token = _mid_send(env, monkeypatch, f"notsent-{state}")
    rig.desk._record_send_result(lid, state, token, None if state == "pending" else "x")  # type: ignore[arg-type]
    assert rig.query("SELECT state FROM followups WHERE lead_id=?", (lid,)) == [(state,)]  # it was recorded
    assert _step(rig, lid) == "new"
    assert lid in rig.desk._mirror_not_before


def test_a_sent_result_moves_the_stage_and_frees_the_copy_wait(env, monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING)
    rig, lid, token = _mid_send(env, monkeypatch, "sent-moves")
    rig.desk._record_send_result(lid, "sent", token)
    assert _step(rig, lid) == rig.desk.config.leadflow.after_followup
    assert lid not in rig.desk._mirror_not_before
    assert not _warnings(caplog)


# -- (b) an error that is not a lock answer is not swallowed
@pytest.mark.parametrize(
    "error", [sqlite3.OperationalError("disk I/O error"), sqlite3.OperationalError("unable to open database file")]
)
def test_a_database_error_that_is_not_a_lock_answer_propagates(env, monkeypatch, caplog, error) -> None:
    caplog.set_level(logging.WARNING)
    rig, lid, token = _mid_send(env, monkeypatch, "nonbusy")

    @contextmanager
    def failing_write(conn: Any) -> Iterator[None]:
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(rig.desk, "_write", failing_write)
    with pytest.raises(sqlite3.OperationalError, match=str(error)):
        rig.desk._record_send_result(lid, "sent", token)
    assert not any("stayed locked" in r.getMessage() for r in caplog.records)
    assert lid in rig.desk._mirror_not_before  # nothing was recorded, so the wait stays


# -- (c) the row-count warning, exactly when the count is not one
def test_c_stale_token_logs_changed_0(env, monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING)
    rig, lid, token = _mid_send(env, monkeypatch, "stale")
    rig.desk._record_send_result(lid, "sent", token + 5)
    assert _warnings(caplog) == ["record_send_result: expected one row, changed 0"]
    assert _step(rig, lid) == "new"


@pytest.mark.parametrize("state", ["sent", "failed", "unknown"])
def test_c_one_row_logs_no_warning_even_when_the_stage_did_not_move(env, monkeypatch, caplog, state) -> None:
    """The row count is the first value of the attempt, not the 'stage moved' flag and not a constant."""
    caplog.set_level(logging.WARNING)
    rig, lid, token = _mid_send(env, monkeypatch, f"one-{state}")
    if state == "sent":
        _sql(rig, "UPDATE lead_state SET step='qualified', step_rank=99 WHERE lead_id=?", lid)  # already further
    rig.desk._record_send_result(lid, state, token, None if state == "sent" else "x")  # type: ignore[arg-type]
    assert _warnings(caplog) == []


def test_c_nudge_result_with_no_row_logs_changed_0(env, monkeypatch, caplog) -> None:
    """The three `_finish_update` callers share one count; the nudge record is the one reached without a crash."""
    caplog.set_level(logging.WARNING)
    rig = env.rig(leadflow=copy.deepcopy(DEMO_LEADFLOW))
    lid = str(rig.desk.process("form", "nud", lead(email="nud@example.test")).body["lead_id"])
    rig.clock.advance(DEMO_LEADFLOW["nudge_after_seconds"])
    real = rig.desk._locked_retry

    def retry(fn: Any) -> Any:
        if is_write(fn, "record"):
            _sql(rig, "UPDATE nudges SET attempts=attempts+7 WHERE lead_id=?", lid)  # the token no longer matches
        return real(fn)

    monkeypatch.setattr(rig.desk, "_locked_retry", retry)
    rig.desk.reap_once()
    assert _warnings(caplog) == ["record_nudge_result: expected one row, changed 0"]


# -- (e) a locked database: swallowed with one log line, for the gate's own timeout as for an SQLite busy answer
@pytest.mark.parametrize(
    "error", [_GateBusy("database is locked (write gate wait ran out)"), sqlite3.OperationalError("database is locked")]
)
def test_e_a_locked_database_is_swallowed_and_logged(env, monkeypatch, caplog, error) -> None:
    """`_GateBusy` is a busy answer too: `_finish_with` returns None, it must not escape `_record_send_result`."""
    caplog.set_level(logging.WARNING)
    rig, lid, token = _mid_send(env, monkeypatch, "locked")
    tries: list[int] = []

    @contextmanager
    def locked_write(conn: Any) -> Iterator[None]:
        tries.append(1)
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(rig.desk, "_write", locked_write)
    rig.desk._record_send_result(lid, "sent", token)  # no exception
    expected = "record_send_result: database stayed locked; the row stays as it was and shows as stalled"
    assert [r.getMessage() for r in caplog.records if "stayed locked" in r.getMessage()] == [expected]
    assert not _warnings(caplog)  # a locked database is not a row-count warning
    assert len(tries) == (1 if isinstance(error, _GateBusy) else LOCK_RETRIES)  # a gate timeout is not tried again
    assert rig.query("SELECT state FROM followups WHERE lead_id=?", (lid,)) == [("sending",)]
    assert _step(rig, lid) == "new"
    assert lid in rig.desk._mirror_not_before


# -- (f) the shared deadline of one write call: no new try once it has passed (fake clock, nothing waits)
def test_f_no_new_try_after_the_call_deadline(env, monkeypatch) -> None:
    clock = FakeClock(monkeypatch)
    rig = env.rig()
    tries: list[int] = []

    def busy() -> None:
        tries.append(1)
        clock.now += WRITE_DEADLINE_SECONDS + 1  # this try used up the whole deadline
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        rig.desk._locked_retry(busy)
    assert len(tries) == 1


def test_f_busy_tries_within_the_deadline_are_all_used(env, monkeypatch) -> None:
    FakeClock(monkeypatch)
    rig = env.rig()
    tries: list[int] = []

    def busy() -> None:
        tries.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        rig.desk._locked_retry(busy)
    assert len(tries) == LOCK_RETRIES


# -- (g) base behaviour of the result record, pinned
def test_g_a_stage_at_the_same_rank_does_not_move(env, monkeypatch) -> None:
    """`step_rank < ?`: a lead already AT the follow-up's stage is left alone (not moved again, wait kept)."""
    rig, lid, token = _mid_send(env, monkeypatch, "same-rank")
    after = rig.desk.config.leadflow.after_followup
    _sql(
        rig,
        "UPDATE lead_state SET step=?, step_rank=?, changed_at=5 WHERE lead_id=?",
        after,
        rig.desk.config.leadflow.rank(after),
        lid,
    )
    rig.desk._record_send_result(lid, "sent", token)
    assert rig.query("SELECT state FROM followups WHERE lead_id=?", (lid,)) == [("sent",)]
    assert rig.query("SELECT step, changed_at FROM lead_state WHERE lead_id=?", (lid,)) == [(after, 5)]
    assert lid in rig.desk._mirror_not_before


def test_g_a_sent_result_clears_the_last_error(env, monkeypatch) -> None:
    rig, lid, token = _mid_send(env, monkeypatch, "clears-error")
    _sql(rig, "UPDATE followups SET last_error='old' WHERE lead_id=?", lid)
    rig.desk._record_send_result(lid, "sent", token, "ignored")
    assert rig.query("SELECT state, last_error FROM followups WHERE lead_id=?", (lid,)) == [("sent", None)]


# -- (d) the line the scripts read
class _Stub:
    server_address = (HOST, 4321)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt  # main() treats it as Ctrl-C and shuts down

    def server_close(self) -> None:
        pass


def test_d_the_listening_line_is_exactly_http_host_port(env, monkeypatch, tmp_path, capsys) -> None:
    rig = env.rig()
    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "line.sqlite3"))
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (rig.desk.crm, rig.desk.outbox, rig.desk.qualifier))
    monkeypatch.setattr(server, "make_server", lambda desk, cfg: _Stub())
    monkeypatch.setattr(server.signal, "signal", lambda sig, fn: None)
    server.main()
    lines = capsys.readouterr().out.splitlines()
    assert f"desk listening on http://{HOST}:4321" in lines  # written out, not built with desk_origin
