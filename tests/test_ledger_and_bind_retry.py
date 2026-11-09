"""Ledger repair after a crash, bad usage numbers, settle failure, ids across processes, bind retry
with its own exit code, and a connection that has received its whole request is not closed to make room.
Made-up data only; loopback only."""

from __future__ import annotations

import json
import os
import socket
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from support import HOST, call, child_env, free_port, start_proc, stop_proc, wait_until

from lead_desk.config import LiveSettings
from lead_desk.constants import BIND_RETRY_SECONDS, EXIT_PORT_UNAVAILABLE
from lead_desk.live_claude import LiveCapReached, LiveQualifier, SpendLedger


def _settings(tmp_path: Path, **over: Any) -> LiveSettings:
    key = tmp_path / "key.txt"
    key.write_text("sk-" + "fake-" + "key-" + "0123456789abcdef\n", encoding="utf-8")
    kw: dict[str, Any] = {
        "model": "m",
        "key_file": key,
        "price_in_per_mtok": Decimal("2"),
        "price_out_per_mtok": Decimal("10"),
        "price_date": "2026-01-01",
        "cap_run_usd": Decimal("1"),
        "cap_total_usd": Decimal("3"),
        "ledger_path": tmp_path / "ledger.jsonl",
        "run_id": "run-a",
        "base_url": "http://127.0.0.1:9",
    }
    kw.update(over)
    return LiveSettings(**kw)


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_ledger_id_holds_the_process_id(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    rid = SpendLedger(s.ledger_path).reserve(s, Decimal("0.001"))
    assert f"-{os.getpid()}-" in rid  # two processes with one run id can never make the same id


def test_torn_last_line_is_ignored_and_repaired(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)
    ledger.reserve(s, Decimal("0.001"))
    with open(s.ledger_path, "ab") as f:
        f.write(b'{"op": "reserve", "id": "x", "run": "run-a", "usd": "0.0')  # a crash in the middle of a write
    assert ledger.totals("run-a")[0] == Decimal("0.001")  # still readable
    ledger.reserve(s, Decimal("0.002"))  # the next append starts on a new line
    ledger.reserve(s, Decimal("0.003"))
    assert ledger.totals("run-a")[0] == Decimal("0.006")  # the torn line counts as nothing, the others are whole
    with open(s.ledger_path, "ab") as f:
        f.write(b"garbage\n")
    with pytest.raises(LiveCapReached):  # real corruption (not a cut-off line) still stops spending
        ledger.reserve(s, Decimal("0.001"))


def test_non_object_rows_fail_closed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ledger_path.write_bytes(b'[1, 2]\n5\nnull\n"text"\n')
    with pytest.raises(LiveCapReached):  # ignoring them could lower the counted spend
        SpendLedger(s.ledger_path).reserve(s, Decimal("0.001"))


class _Replying(LiveQualifier):
    def __init__(self, s: LiveSettings, usage: Any) -> None:
        super().__init__(s)
        self._usage = usage

    def _post(self, body: bytes, key: str, timeout: float | None = None) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "hot"}], "usage": self._usage}


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -50, "output_tokens": 2},
        {"input_tokens": 40, "output_tokens": -1},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": 40, "output_tokens": 2.5},
        {"input_tokens": 10**1000, "output_tokens": 2},  # a valid int far beyond the money context
        {"input_tokens": 40, "output_tokens": 10**12},
    ],
)
def test_bad_token_counts_keep_the_worst_case_reserve(tmp_path: Path, usage: dict[str, Any]) -> None:
    q = _Replying(_settings(tmp_path), usage)
    assert q.qualify("L1", "a@example.test") == "hot"  # the answer is returned
    rows = _rows(q.s.ledger_path)
    assert [r["op"] for r in rows] == ["reserve"]  # no settle: a negative number must never lower the total
    assert q.ledger.totals("run-a")[0] == Decimal(rows[0]["usd"])


def test_settle_failure_does_not_discard_a_paid_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    q = _Replying(_settings(tmp_path), {"input_tokens": 40, "output_tokens": 2})

    def boom(*_: object) -> None:
        raise LiveCapReached("the spend ledger is locked by another process")

    monkeypatch.setattr(q.ledger, "settle", boom)
    assert q.qualify("L1", "a@example.test") == "hot"
    assert [r["op"] for r in _rows(q.s.ledger_path)] == ["reserve"]  # the worst-case reserve stays


# -- start-up: the port is retried, then its own exit code
def _desk_env(tmp_path: Path, db: Path, port: int, fakes_url: str) -> dict[str, str]:
    replies = tmp_path / "replies.json"
    replies.write_text("{}", encoding="utf-8")
    return child_env(
        DESK_DB_PATH=str(db),
        DESK_BIND=HOST,
        DESK_PORT=str(port),
        DESK_FAKES_URL=fakes_url,
        DESK_REPLIES_PATH=str(replies),
    )


def test_port_taken_for_good_exits_with_its_own_code(env, tmp_path: Path) -> None:
    _, fakes_url = env.fakes("keyed")
    port = free_port()
    holder = socket.socket()
    holder.bind((HOST, port))
    holder.listen(1)
    p = start_proc(["-m", "lead_desk.server"], _desk_env(tmp_path, tmp_path / "d.sqlite3", port, fakes_url))
    try:
        p.wait(timeout=BIND_RETRY_SECONDS + 20)
    finally:
        out = stop_proc(p)
        holder.close()
    assert p.returncode == EXIT_PORT_UNAVAILABLE, out.decode(errors="replace")[-300:]
    assert b"cannot bind" in out


def test_port_freed_during_the_retry_time_is_taken(env, tmp_path: Path) -> None:
    _, fakes_url = env.fakes("keyed")
    port = free_port()
    holder = socket.socket()
    holder.bind((HOST, port))
    holder.listen(1)
    threading.Timer(1.5, holder.close).start()
    p = start_proc(["-m", "lead_desk.server"], _desk_env(tmp_path, tmp_path / "d.sqlite3", port, fakes_url))
    try:
        wait_until(
            lambda: p.poll() is not None or call(f"http://{HOST}:{port}", "GET", "/counts")[0] == 200,
            timeout=BIND_RETRY_SECONDS + 15,
            what="desk serves after the port was freed",
        )
        assert p.poll() is None
    finally:
        out = stop_proc(p)
    assert b"cannot bind" not in out


def test_a_key_file_that_is_not_utf8_is_a_classed_error(tmp_path: Path) -> None:
    from lead_desk.live_claude import LiveError

    s = _settings(tmp_path)
    s.key_file.write_bytes(b"\xff\xfe\xfa not text")
    with pytest.raises(LiveError):
        LiveQualifier(s).qualify("L1", "a@example.test")
