"""Spend ledger amount checks: a bad usd value must stop spending (fail closed), never lift a cap, never raise an
unclassed error, and the read path must not create files. Made-up data only."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lead_desk.config import LiveSettings
from lead_desk.constants import LIVE_LEDGER_FILE_MAX_BYTES
from lead_desk.live_claude import LiveCapReached, SpendLedger


def _settings(tmp_path: Path) -> LiveSettings:
    return LiveSettings(
        model="m",
        key_file=tmp_path / "key.txt",
        price_in_per_mtok=Decimal("2"),
        price_out_per_mtok=Decimal("10"),
        price_date="2026-01-01",
        cap_run_usd=Decimal("1"),
        cap_total_usd=Decimal("3"),
        ledger_path=tmp_path / "ledger.jsonl",
        run_id="run-a",
        base_url="http://127.0.0.1:9",
    )


def _write(path: Path, rows: list[str]) -> None:
    path.write_bytes(("\n".join(rows) + "\n").encode("utf-8"))  # binary: the newline bytes are explicit


def _res(usd: str, rid: str = "r1") -> str:
    return '{"op":"reserve","id":"%s","run":"run-a","usd":%s}' % (rid, usd)


def _set(usd: str, rid: str = "r1") -> str:
    return '{"op":"settle","id":"%s","usd":%s}' % (rid, usd)


def test_good_ledger_totals_unchanged(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _write(s.ledger_path, [_res('"0.5"', "a"), _set('"0.25"', "a"), _res('"0.125"', "b")])
    ledger = SpendLedger(s.ledger_path)
    assert ledger.totals("run-a") == (Decimal("0.375"), Decimal("0.375"))
    assert ledger.read_totals("run-a") == (Decimal("0.375"), Decimal("0.375"))


HOSTILE = [
    ("neg_reserve", [_res('"-100"')]),
    ("neg_settle", [_res('"0.5"'), _set('"-100"')]),
    ("neg_infinity", [_res('"-Infinity"')]),
    ("infinity", [_res('"Infinity"')]),
    ("nan", [_res('"NaN"')]),
    ("snan", [_res('"sNaN"')]),
    ("nan_settle", [_res('"0.5"'), _set('"NaN"')]),
    ("overflow_exp", [_res('"1E+999999999"')]),
    ("big_1e27", [_res('"1E+27"')]),
    ("json_float", [_res("0.5")]),
    ("json_int", [_res("1")]),
    ("bool", [_res("true")]),
    ("bare_nan", [_res("NaN")]),
    ("bare_neg_inf", [_res("-Infinity")]),
    ("null", [_res("null")]),
    ("empty", [_res('""')]),
    ("list", [_res("[]")]),
    ("junk", [_res('"abc"')]),
]


@pytest.mark.parametrize("rows", [r for _, r in HOSTILE], ids=[n for n, _ in HOSTILE])
def test_bad_amount_refuses_and_never_lifts_cap(tmp_path: Path, rows: list[str]) -> None:
    s = _settings(tmp_path)
    _write(s.ledger_path, rows)
    ledger = SpendLedger(s.ledger_path)
    with pytest.raises(LiveCapReached):
        ledger.reserve(s, Decimal("0.1"))
    with pytest.raises(LiveCapReached):
        ledger.totals("run-a")
    with pytest.raises(LiveCapReached):
        ledger.read_totals("run-a")


@pytest.mark.parametrize(
    "bad", [Decimal("-1"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("1E+27")]
)
def test_writer_refuses_bad_amounts(tmp_path: Path, bad: Decimal) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)
    with pytest.raises(LiveCapReached):
        ledger.reserve(s, bad)
    rid = ledger.reserve(s, Decimal("0.5"))
    with pytest.raises(LiveCapReached):
        ledger.settle(rid, bad)
    rows: list[Any] = [json.loads(x) for x in s.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert [r["op"] for r in rows] == ["reserve"]  # nothing bad was written


def test_read_path_creates_no_lock_file_or_folder(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(tmp_path / "sub" / "dir" / "ledger.jsonl")
    assert ledger.read_totals("run-a") == (Decimal(0), Decimal(0))
    assert not (tmp_path / "sub").exists()
    ledger2 = SpendLedger(s.ledger_path)
    _write(s.ledger_path, [_res('"0.5"')])
    assert ledger2.read_totals("run-a")[1] == Decimal("0.5")
    assert not Path(f"{s.ledger_path}.lock").exists()


def _raw(s: LiveSettings, data: bytes) -> SpendLedger:
    s.ledger_path.write_bytes(data)
    return SpendLedger(s.ledger_path)


def _refuses(ledger: SpendLedger, s: LiveSettings) -> None:
    calls = (
        lambda: ledger.reserve(s, Decimal("0.1")),
        lambda: ledger.totals("run-a"),
        lambda: ledger.read_totals("run-a"),
    )
    for call in calls:
        with pytest.raises(LiveCapReached):
            call()


def test_invalid_utf8_is_classed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _refuses(_raw(s, b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n\xff\xfe\n'), s)


def test_directory_as_ledger_path_is_classed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.ledger_path.mkdir()
    _refuses(SpendLedger(s.ledger_path), s)


def test_oversize_file_refused_with_its_own_message(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = _raw(s, b" " * (LIVE_LEDGER_FILE_MAX_BYTES + 1))
    with pytest.raises(LiveCapReached, match="too large"):
        ledger.totals("run-a")
    _refuses(ledger, s)


def test_unreadable_message_says_what_to_do(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(LiveCapReached, match="check the ledger file or move it"):
        _raw(s, b"garbage\n").totals("run-a")


def test_memory_error_is_classed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    ledger = _raw(s, b"")

    def boom(*a: Any, **k: Any) -> Any:
        raise MemoryError

    monkeypatch.setattr(SpendLedger, "_sum", staticmethod(boom))
    with pytest.raises(LiveCapReached):
        ledger.totals("run-a")


def test_lock_open_failure_is_classed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    (tmp_path / "blocker").write_bytes(b"x")  # a file where the ledger folder should be
    ledger = SpendLedger(tmp_path / "blocker" / "ledger.jsonl")
    with pytest.raises(LiveCapReached):
        ledger.reserve(s, Decimal("0.1"))


def test_write_failure_is_classed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)

    def full(fd: int) -> None:
        raise OSError(28, "no space")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(LiveCapReached):
        ledger.reserve(s, Decimal("0.1"))


def test_torn_line_in_the_middle_fails_closed_last_line_is_skipped(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    torn = b'{"op":"reserve","id":"b","run":"run-a","usd":"0.'
    assert _raw(s, good + torn).totals("run-a")[1] == Decimal("0.1")
    _refuses(_raw(s, torn + b"\n" + good), s)


def test_torn_tail_is_dropped_on_the_next_append(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = _raw(
        s, b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n{"op":"reserve","id":"b","run":"run-a","usd":"0.'
    )
    ledger.reserve(s, Decimal("0.2"))
    assert ledger.totals("run-a")[1] == Decimal("0.3")  # still readable: the stump is gone, not left mid-file


def test_duplicate_reserve_id_fails_closed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _refuses(_raw(s, (_res('"0.9"') + "\n" + _res('"0.0001"') + "\n").encode()), s)


def test_settle_rules(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    twice = (_res('"0.5"') + "\n" + _set('"0.1"') + "\n" + _set('"0.3"') + "\n").encode()
    assert _raw(s, twice).totals("run-a")[1] == Decimal("0.3")  # double settle: the larger counts
    more = (_res('"0.5"') + "\n" + _set('"0.7"') + "\n").encode()
    assert _raw(s, more).totals("run-a")[1] == Decimal("0.7")  # settle above the reserve counts as settled
    orphan = (_res('"0.5"') + "\n" + _set('"9"', "nope") + "\n").encode()
    assert _raw(s, orphan).totals("run-a")[1] == Decimal("0.5")  # settle without a reserve counts nothing


def test_reserve_exactly_at_the_cap_is_allowed_and_one_more_is_not(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)
    ledger.reserve(s, Decimal("1"))  # cap_run is 1
    with pytest.raises(LiveCapReached):
        ledger.reserve(s, Decimal("0.000001"))


@pytest.mark.parametrize("bad", [0.5, 1, "0.5", None, True])
def test_writer_refuses_non_decimal(tmp_path: Path, bad: Any) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)
    with pytest.raises(LiveCapReached):
        ledger.reserve(s, bad)
    rid = ledger.reserve(s, Decimal("0.5"))
    with pytest.raises(LiveCapReached):
        ledger.settle(rid, bad)


def test_zero_reserve_refused_zero_settle_allowed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)
    with pytest.raises(LiveCapReached):
        ledger.reserve(s, Decimal(0))
    rid = ledger.reserve(s, Decimal("0.5"))
    ledger.settle(rid, Decimal(0))
    assert ledger.totals("run-a")[1] == Decimal(0)


def test_too_large_writer_message_is_separate(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)
    with pytest.raises(LiveCapReached, match="too large"):
        ledger.reserve(s, Decimal("1E+27"))
    with pytest.raises(LiveCapReached, match="not a valid"):
        ledger.reserve(s, Decimal("NaN"))


def test_missing_key_rows_fail_closed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _refuses(_raw(s, b'{"op":"reserve","run":"run-a","usd":"0.1"}\n'), s)  # no id
    _refuses(_raw(s, b'{"id":"a"}\n'), s)  # no op


def test_crlf_and_many_lines_total_the_same(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    rows = [_res('"0.001"', f"r{i}") for i in range(3000)]
    lf = _raw(s, ("\n".join(rows) + "\n").encode()).totals("run-a")
    crlf = _raw(s, ("\r\n".join(rows) + "\r\n").encode()).totals("run-a")
    assert lf == crlf == (Decimal("3.000"), Decimal("3.000"))


def test_huge_single_line_is_refused_not_slow(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _refuses(_raw(s, b'{"op":"reserve","id":"' + b"a" * 100000 + b'","run":"run-a","usd":"x"}\n'), s)


@pytest.mark.parametrize(
    "over",
    [
        {"price_in_per_mtok": Decimal("1E-999999999")},
        {"price_out_per_mtok": Decimal("1E+999999")},
        {"cap_run_usd": Decimal("1E+999999"), "cap_total_usd": Decimal("1E+999999")},
        {"max_tokens": 10**9},
    ],
)
def test_settings_bounds_keep_worst_case_finite_and_nonzero(tmp_path: Path, over: dict[str, Any]) -> None:
    base = _settings(tmp_path)
    kw = {f: getattr(base, f) for f in base.__dataclass_fields__}
    kw.update(over)
    with pytest.raises(ValueError):
        LiveSettings(**kw)


def test_check_script_bad_cap_is_a_clean_error(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "run_live_check.py"
    args = ["--key-file", "k", "--ledger", "l", "--price-date", "2026-01-01", "--model", "m=1,2", "--cap-total", "3"]
    r = subprocess.run(  # noqa: S603  # nosec B603 - our own script, fixed arguments
        [sys.executable, str(script), "--cap-run", "abc", *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert r.returncode == 2 and "Traceback" not in r.stderr and "not a number" in r.stderr


# -- ledger ids and amounts -------------------------------------------------------------------------------------------
def test_two_instances_in_one_clock_tick_never_make_the_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    s = _settings(tmp_path)
    monkeypatch.setattr(time, "time_ns", lambda: 1_000_000)  # a coarse clock: the same reading twice
    a, b = SpendLedger(s.ledger_path), SpendLedger(s.ledger_path)  # two instances: each counts from 1
    ids = {a.reserve(s, Decimal("0.1")), b.reserve(s, Decimal("0.1"))}
    assert len(ids) == 2
    assert a.totals("run-a")[1] == Decimal("0.2")  # still readable: no duplicate id


@pytest.mark.parametrize(
    "tail",
    [
        b"\x00" * 64,
        b"   ",
        b"\x00\x00 \x00",
        b'{"op":"reserve","id":"x","ru\x00\x00\x00',
        b'{"op":"reserve","id":"x","run":"run-a","usd":"0.1"}\x00',  # a whole row, then NUL instead of its newline
    ],
)
def test_nul_or_blank_tail_is_a_cut_write_not_a_broken_ledger(tmp_path: Path, tail: bytes) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    ledger = _raw(s, good + tail)
    assert ledger.totals("run-a")[1] == Decimal("0.1")
    ledger.reserve(s, Decimal("0.2"))
    assert b"\x00" not in s.ledger_path.read_bytes()  # the next append dropped the stump
    assert ledger.totals("run-a")[1] == Decimal("0.3")


def test_exactly_the_size_cap_is_allowed_and_the_message_names_the_carry_row(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert _raw(s, b" " * (LIVE_LEDGER_FILE_MAX_BYTES - 1) + b"\n").totals("run-a") == (Decimal(0), Decimal(0))
    with pytest.raises(LiveCapReached, match="carry row"):
        _raw(s, b" " * (LIVE_LEDGER_FILE_MAX_BYTES + 1)).totals("run-a")


def test_unknown_op_and_non_object_rows_fail_closed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    for data in (b'{"op":"other"}\n', b"[1]\n", b"5\n", b'"text"\n', b"null\n"):
        _refuses(_raw(s, data), s)


def test_a_newline_terminated_corrupt_last_line_fails_closed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    _refuses(
        _raw(s, good + b'{"op":"reserve","id":"b","run":"run-a","usd":"0.1"\n'), s
    )  # no closing brace, but complete line


def test_prepend_newline_branch_for_a_whole_last_line_without_its_newline(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    whole = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}'  # complete, only its newline is missing
    ledger = _raw(s, whole)
    ledger.reserve(s, Decimal("0.2"))
    assert s.ledger_path.read_bytes().startswith(whole + b"\n{")
    assert ledger.totals("run-a")[1] == Decimal("0.3")


def test_decimal_context_of_the_caller_cannot_round_the_totals(tmp_path: Path) -> None:
    import decimal

    s = _settings(tmp_path)
    ledger = _raw(s, (_res('"0.123456"', "a") + "\n" + _res('"0.123456"', "b") + "\n").encode())
    with decimal.localcontext() as ctx:
        ctx.prec = 3
        assert ledger.totals("run-a")[1] == Decimal("0.246912")


def test_unlock_failure_does_not_escape_the_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    s = _settings(tmp_path)
    if sys.platform == "win32":
        import msvcrt

        real = msvcrt.locking

        def locking(fd: int, mode: int, nbytes: int) -> None:
            if mode == msvcrt.LK_UNLCK:
                raise OSError(5, "denied")
            real(fd, mode, nbytes)

        monkeypatch.setattr(msvcrt, "locking", locking)
    else:
        import fcntl

        real_flock = fcntl.flock

        def flock(fd: int, op: int) -> None:
            if op == fcntl.LOCK_UN:
                raise OSError(5, "denied")
            real_flock(fd, op)

        monkeypatch.setattr(fcntl, "flock", flock)
    ledger = SpendLedger(s.ledger_path)
    rid = ledger.reserve(s, Decimal("0.1"))
    ledger.settle(rid, Decimal("0.05"))
    assert ledger.totals("run-a")[1] == Decimal("0.05")


@pytest.mark.parametrize(
    "over",
    [
        {"max_tokens": True},
        {"max_tokens": 64.0},
        {"model": 5},
        {"run_id": None},
        {"price_date": 3},
        {"timeout_seconds": True},
        {"allow_any_base_url": "yes"},
    ],
)
def test_settings_types_are_checked(tmp_path: Path, over: dict[str, Any]) -> None:
    base = _settings(tmp_path)
    kw = {f: getattr(base, f) for f in base.__dataclass_fields__}
    kw.update(over)
    with pytest.raises(ValueError):
        LiveSettings(**kw)


def test_check_script_refuses_zero_leads_or_runs_and_a_broken_ledger_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import run_live_check as rlc

    key = tmp_path / "k.txt"
    key.write_text("sk-" + "fake-" + "key-" + "0123456789abcdef\n", encoding="utf-8")
    ledger = tmp_path / "l.jsonl"
    ledger.write_bytes(b"garbage\n")
    base = ["--key-file", str(key), "--ledger", str(ledger), "--price-date", "2026-01-01", "--model", "m=1,2"]
    base += ["--cap-run", "1", "--cap-total", "3", "--base-url", "http://127.0.0.1:9"]
    for bad in (["--leads", "0"], ["--runs", "0"]):
        with pytest.raises(SystemExit) as e:
            rlc.main(base + bad)
        assert e.value.code == 2
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert rlc.main(base + ["--leads", "1", "--runs", "1"]) == rlc.EXIT_REFUSED
    out = capsys.readouterr()
    assert "Traceback" not in out.out + out.err and "spend ledger" in out.out


def test_a_huge_unterminated_torn_tail_is_dropped_without_reading_the_whole_file_blindly(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    ledger = _raw(s, good + b"{" + b"a" * 200_000)  # longer than the window read from the end
    ledger.reserve(s, Decimal("0.2"))
    assert len(s.ledger_path.read_bytes()) < 1000  # the stump is gone
    assert ledger.totals("run-a")[1] == Decimal("0.3")


def test_settle_has_the_same_size_gate_as_reserve(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    ledger = SpendLedger(s.ledger_path)
    rid = ledger.reserve(s, Decimal("0.1"))
    with open(s.ledger_path, "ab") as f:
        f.write(b" " * LIVE_LEDGER_FILE_MAX_BYTES + b"\n")
    with pytest.raises(LiveCapReached, match="too large"):
        ledger.settle(rid, Decimal("0.05"))


def test_memory_error_while_writing_is_classed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)

    def boom(fd: int) -> None:
        raise MemoryError

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(LiveCapReached):
        SpendLedger(s.ledger_path).reserve(s, Decimal("0.1"))


def test_reader_and_writer_agree_on_where_the_last_line_starts(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    ledger = _raw(s, good + b'{"p"\r{"q"')  # a carriage return inside the cut-off tail is not a line end
    assert ledger.totals("run-a")[1] == Decimal("0.1")
    ledger.reserve(s, Decimal("0.2"))
    assert ledger.totals("run-a")[1] == Decimal("0.3")


def test_no_random_source_is_a_classed_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)

    def broken(n: int) -> bytes:
        raise OSError("no entropy")

    monkeypatch.setattr(os, "urandom", broken)
    with pytest.raises(LiveCapReached):
        SpendLedger(s.ledger_path).reserve(s, Decimal("0.1"))


def test_rows_with_a_non_text_run_or_id_fail_closed(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _refuses(_raw(s, b'{"op":"reserve","id":"a","run":5,"usd":"0.1"}\n'), s)  # would count in the total, not the run
    _refuses(_raw(s, b'{"op":"reserve","id":7,"run":"run-a","usd":"0.1"}\n'), s)
    _refuses(_raw(s, b'{"op":"settle","id":7,"usd":"0.1"}\n'), s)


def test_check_script_worst_case_figure_never_reads_too_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import run_live_check as rlc

    key = tmp_path / "k.txt"
    key.write_text("sk-" + "fake-" + "key-" + "0123456789abcdef\n", encoding="utf-8")
    argv = ["--key-file", str(key), "--ledger", str(tmp_path / "l.jsonl"), "--price-date", "2026-01-01"]
    argv += ["--model", "m=0.01,0.01", "--cap-run", "1", "--cap-total", "3", "--leads", "1", "--runs", "1"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert rlc.main(argv) == 1
    assert "US$0.0001 (caps" in capsys.readouterr().out  # a tiny cost is rounded UP, not down to 0.0000


def test_a_cut_that_ends_in_a_closing_brace_inside_text_is_still_a_cut_write(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    stump = b'{"op":"reserve","id":"x","run":"run-a","usd":"0.1","model":"m}'  # ends in "}", but only inside a string
    ledger = _raw(s, good + stump)
    assert ledger.totals("run-a")[1] == Decimal("0.1")
    ledger.reserve(s, Decimal("0.2"))
    assert b'"model":"m}' not in s.ledger_path.read_bytes()  # dropped, not kept in the middle
    assert ledger.totals("run-a")[1] == Decimal("0.3")


def test_a_cut_inside_a_multibyte_character_is_judged_the_same_by_reader_and_writer(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    ledger = _raw(
        s, good + b'{"op":"reserve","id":"x","run":"r\xc3'
    )  # the cut falls inside the two bytes of one letter
    assert ledger.totals("run-a")[1] == Decimal("0.1")
    ledger.reserve(s, Decimal("0.2"))
    assert ledger.totals("run-a")[1] == Decimal("0.3")
    _refuses(_raw(s, good + b"\xc3\n" + good.replace(b'"a"', b'"b"')), s)  # a bad byte in the middle still fails closed


@pytest.mark.parametrize("bad", ["8", Decimal("8"), True, None])
def test_timeout_seconds_must_be_an_int_or_a_float(tmp_path: Path, bad: Any) -> None:
    base = _settings(tmp_path)
    kw = {f: getattr(base, f) for f in base.__dataclass_fields__}
    kw["timeout_seconds"] = bad
    with pytest.raises(ValueError):
        LiveSettings(**kw)


def test_a_deeply_nested_last_line_without_newline_fails_closed_not_ignored(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    _refuses(_raw(s, good + b'{"a":' * 100_000), s)  # too deep to read: it must not count as a harmless cut-off line


@pytest.mark.parametrize("tail", [b"\x0c", b"\x0b", b" \x0b\x0c "])
def test_vertical_tab_and_form_feed_tails_are_blank_for_reader_and_writer_alike(tmp_path: Path, tail: bytes) -> None:
    s = _settings(tmp_path)
    good = b'{"op":"reserve","id":"a","run":"run-a","usd":"0.1"}\n'
    ledger = _raw(s, good + tail)
    assert ledger.totals("run-a")[1] == Decimal("0.1")
    ledger.reserve(s, Decimal("0.2"))
    assert tail not in s.ledger_path.read_bytes()  # the writer dropped it, as the reader skips it
    assert ledger.totals("run-a")[1] == Decimal("0.3")


@pytest.mark.parametrize("name", ["DESK_LIVE_TIMEOUT_SECONDS", "DESK_LIVE_MAX_TOKENS"])
@pytest.mark.parametrize("junk", ["abc", "", "1e", "1,5"])
def test_junk_number_settings_are_a_clean_refusal_that_names_the_setting(name: str, junk: str) -> None:
    from lead_desk.config import Config

    env = {
        "DESK_QUALIFIER": "live_claude",
        "DESK_LIVE_MODEL": "m",
        "DESK_LIVE_KEY_FILE": "k.txt",
        "DESK_LIVE_PRICE_DATE": "2026-01-01",
        "DESK_LIVE_LEDGER_PATH": "l.jsonl",
        "DESK_LIVE_RUN_ID": "r",
        "DESK_LIVE_PRICE_IN_PER_MTOK": "1",
        "DESK_LIVE_PRICE_OUT_PER_MTOK": "2",
        "DESK_LIVE_CAP_RUN_USD": "1",
        "DESK_LIVE_CAP_TOTAL_USD": "3",
        name: junk,
    }
    with pytest.raises(ValueError, match=f"{name} is not a number"):
        Config.from_env(env)


@pytest.mark.parametrize("junk", ["nan", "inf", "1e999", "-5", "0"])
def test_non_finite_or_out_of_range_timeouts_are_refused(junk: str) -> None:
    from lead_desk.config import Config

    env = {
        "DESK_QUALIFIER": "live_claude",
        "DESK_LIVE_MODEL": "m",
        "DESK_LIVE_KEY_FILE": "k.txt",
        "DESK_LIVE_PRICE_DATE": "2026-01-01",
        "DESK_LIVE_LEDGER_PATH": "l.jsonl",
        "DESK_LIVE_RUN_ID": "r",
        "DESK_LIVE_PRICE_IN_PER_MTOK": "1",
        "DESK_LIVE_PRICE_OUT_PER_MTOK": "2",
        "DESK_LIVE_CAP_RUN_USD": "1",
        "DESK_LIVE_CAP_TOTAL_USD": "3",
        "DESK_LIVE_TIMEOUT_SECONDS": junk,
    }
    with pytest.raises(ValueError):
        Config.from_env(env)


def test_a_bad_setting_value_stops_the_desk_with_a_plain_message_not_a_traceback(tmp_path: Path) -> None:
    import subprocess
    import sys

    repo = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": str(repo / "src"),
        "DESK_PORT": "abc",
        "DESK_DB_PATH": str(tmp_path / "d.sqlite3"),
    }
    r = subprocess.run(  # noqa: S603  # nosec B603 - our own module, fixed arguments
        [sys.executable, "-m", "lead_desk.server"], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env
    )
    assert r.returncode == 4 and "refuses a setting" in r.stdout and "Traceback" not in r.stdout + r.stderr
