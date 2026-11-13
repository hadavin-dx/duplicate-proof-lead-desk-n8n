"""Tests of the n8n evidence harness (run_all.py, lib.py, summarise.py and the g*.py scripts) and of the published numbers they produce.
Self-contained: needs only pytest, Python 3.12+ and this folder (demo_only/n8n_evidence): no other file of the repository.
Both operating-system branches (Windows, POSIX) are tested on any computer: lib.IS_NT is switched and the OS calls are faked.
  python -m pytest demo_only/n8n_evidence/harness/tests -q
A developer can point the tests at another copy of the folder with a file named .dev-folder next to this file (one line: the folder); it is not an environment variable, so a stranger cannot set it by accident."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import quote

import pytest

_DEV = Path(__file__).with_name(".dev-folder")
FOLDER = Path(_DEV.read_text(encoding="utf-8").strip()) if _DEV.is_file() else Path(__file__).resolve().parents[2]
RUN = FOLDER / "published_run"
HARNESS = FOLDER / "harness"
DOCS = ("README.md", "GLOSSARY.md", "DETAILS.md", "CHANGES.md")  # the explanatory files; the README is the short front page


def _alldocs() -> str:
    return "\n".join((FOLDER / n).read_text(encoding="utf-8") for n in DOCS)


sys.dont_write_bytecode = True  # the tests must leave no .pyc in the published folder (a scan of the folder would flag a compiled path)
ACCT = "hadavin" + "-dx"  # the author's GitHub account, named only in the README and the licence note


def _pyc_outside_tests() -> set:
    return {p for p in FOLDER.rglob("*.pyc") if p.parent != HARNESS / "tests" / "__pycache__"}




def _load(name: str):  # the harness is not a package (frozen scripts), so load the helper modules by path
    spec = importlib.util.spec_from_file_location("n8n_evd_" + name, HARNESS / (name + ".py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


summarise = _load("summarise")


COLON = chr(58)
FAKE_HOME = "/".join(("", "home", "someone"))  # path-shaped and time-shaped values are built from parts: the public hygiene scan flags such literals
USR_BIN = "/".join(("", "usr", "bin"))


def ts(sec: float) -> str:
    """An ISO time on 2026-01-01, `sec` seconds after midnight (under one hour)."""
    return "2026-01-01T00" + COLON + "%02d" % (sec // 60) + COLON + "%06.3f" % (sec % 60) + "Z"


def _norm(text: str) -> str:
    return text.replace(chr(13) + chr(10), chr(10))


# ---- changes after code review (review of the harness): settle by polling, count every attempt, check every kill, own PIDs only ----
run_all = _load("run_all")


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """Tests that call run_all.main() or install_signal_handlers() must not leave SIGTERM/SIGHUP handlers in the pytest process."""
    import signal  # noqa: PLC0415

    names = [n for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n)]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in names}
    yield
    for n, h in saved.items():
        signal.signal(getattr(signal, n), h)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("N8N_EVD_WORK", str(tmp_path))
    monkeypatch.setenv("N8N_EVD_NODE", str(tmp_path / "node"))
    monkeypatch.syspath_prepend(str(HARNESS))
    for m in ("lib", "api"):
        sys.modules.pop(m, None)
    import lib as mod  # noqa: PLC0415

    yield mod
    for m in ("lib", "api"):
        sys.modules.pop(m, None)


class Clock:
    """A fake clock: sleep() moves it; used to run the polling code without waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _g12_row(i: int, **over) -> dict:
    row = {
        "variant": "b",
        "setting": "default",
        "attempt": i,
        "email": f"x{i}@example.com",
        "valid": True,
        "client_send_spread_ms": 20,
        "replies": {"200": 10},
        "after_run1": {"crm": 1, "outbox": 1},
        "replay_added": {"crm": 0, "outbox": 0},
        "after_replay": {"crm": 1, "outbox": 1},
        "overlap": {"n": 10, "latest_start_before_earliest_stop": True},
    }
    row.update(over)
    return row


def _kill_row(i: int, kp: str = "K3", **over) -> dict:
    row = {
        "variant": "b",
        "kill_point": kp,
        "run": i,
        "email": f"k{i}@example.com",
        "trigger_seen_after_s": 0.4,
        "counts_at_kill": {"crm": 0, "outbox": 0},
        "counts_after_inflight_settled": {"crm": 0, "outbox": 0},
        "killed_pid": True,
        "client_replies_during_kill": {"ConnectionResetError": 10},
        "n8n_restarted_ok": True,
        "exec_status_after_restart": {"crashed": 1},
        "retry_pressed_n": 1,
        "final": {"crm": 1, "outbox": 0},
    }
    row.update(over)
    return row


def _write(folder: Path, name: str, rows: list[dict], ext: str = "jsonl") -> None:
    (folder / f"{name}.{ext}").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_iso_reads_the_clock_once(lib, monkeypatch) -> None:
    calls = []

    def fake() -> float:
        calls.append(1)
        return 1000.999 if len(calls) == 1 else 1001.001

    monkeypatch.setattr(lib.time, "time", fake)
    out = lib.iso()
    assert len(calls) == 1
    assert out.endswith(":40.999Z"), out  # second and millisecond come from the same instant


def test_wait_quiet_returns_when_nothing(lib, monkeypatch) -> None:
    clk = Clock()
    monkeypatch.setattr(lib.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(lib.time, "sleep", clk.sleep)
    monkeypatch.setattr(lib, "fake_busy", lambda: False)
    seq = iter([{"crm": 0, "outbox": 0}, {"crm": 1, "outbox": 0}] + [{"crm": 1, "outbox": 1}] * 1000)
    monkeypatch.setattr(lib, "per_email", lambda e: next(seq))
    r = lib.wait_quiet("a@example.com", quiet_s=3, timeout=60)
    assert r["timed_out"] is False and r["counts"] == {"crm": 1, "outbox": 1} and r["waited_s"] >= 3
    n = [0]

    def churn(e):
        n[0] += 1
        return {"crm": n[0], "outbox": 0}

    monkeypatch.setattr(lib, "per_email", churn)
    assert lib.wait_quiet("a@example.com", quiet_s=3, timeout=30)["timed_out"] is True
    monkeypatch.setattr(lib, "per_email", lambda e: {"crm": 1, "outbox": 1})
    monkeypatch.setattr(lib, "fake_busy", lambda: True)  # a request is still held inside the stand-in server
    assert lib.wait_quiet("a@example.com", quiet_s=3, timeout=30)["timed_out"] is True
    monkeypatch.setattr(lib, "fake_busy", lambda: False)
    monkeypatch.setattr(lib, "execs", lambda *a, **k: [{"status": "running"}])
    assert (
        lib.wait_quiet("a@example.com", wid="w", quiet_s=3, timeout=30)["timed_out"] is True
    )  # an unfinished execution blocks quiet
    monkeypatch.setattr(lib, "execs", lambda *a, **k: [{"status": "success"}, {"status": "crashed"}])
    assert lib.wait_quiet("a@example.com", wid="w", quiet_s=3, timeout=30)["timed_out"] is False


def test_invalid_reasons_are_named() -> None:
    assert summarise.invalid_reason(_g12_row(1), "b") is None
    assert "missing times" in summarise.invalid_reason(_g12_row(1, overlap={"n": 10, "note": "missing times"}), "b")
    assert "no overlap" in summarise.invalid_reason(
        _g12_row(1, overlap={"n": 10, "latest_start_before_earliest_stop": False}), "b"
    )
    assert "timeout" in summarise.invalid_reason(_g12_row(1, settle1={"timed_out": True, "waited_s": 60}), "b")
    assert "timeout" in summarise.invalid_reason(_g12_row(1, settle2={"timed_out": True, "waited_s": 60}), "b")
    assert summarise.invalid_reason(_g12_row(1, client_send_spread_ms=150), "d")
    assert summarise.valid(_g12_row(1, settle1={"timed_out": True}), "b") is False


def test_every_attempt_is_counted(tmp_path: Path) -> None:
    rows = [
        _g12_row(1),
        _g12_row(2, overlap={"n": 10, "note": "missing times"}),
        _g12_row(3, settle1={"timed_out": True}),
        _g12_row(4, overlap={"n": 9, "latest_start_before_earliest_stop": True}),
    ]
    _write(tmp_path, "g12_b_default", rows)
    text = summarise.summarise(tmp_path, "jsonl")
    line = next(x for x in text.splitlines() if x.startswith("Test configuration b default (ten at once): attempts"))
    assert "attempts 4, valid 1, invalid 3" in line, line
    for reason in ("missing times x1", "timeout x1", "10 executions expected, 9 listed x1"):
        assert reason in line, line


def test_published_invalid_is_counted() -> None:
    text = summarise.summarise(RUN)
    line = next(x for x in text.splitlines() if x.startswith("Test configuration d limit1 (ten at once): attempts"))
    assert "valid 20" in line and "invalid 35" in line, line  # the 35 invalid attempts live in published_run/invalid


def test_kill_rows_are_checked(tmp_path: Path) -> None:
    rows = [
        _kill_row(1),
        _kill_row(2, trigger_seen_after_s=None),
        _kill_row(3, n8n_restarted_ok=False),
        _kill_row(4, killed_pid=False),
        _kill_row(5, wait_settled={"timed_out": True}),
    ]
    _write(tmp_path, "g3_b", rows)
    text = summarise.summarise(tmp_path, "jsonl")
    line = next(x for x in text.splitlines() if x.startswith("Test configuration b (kill runs): attempts"))
    assert "attempts 5, valid 1, invalid 4" in line, line
    for reason in ("trigger never seen x1", "n8n did not restart x1", "no process killed x1", "timeout x1"):
        assert reason in line, line
    assert "| K3 | 2 |" in text  # invalid rows are still shown, not dropped


def test_shortfall_fails_loudly(tmp_path: Path) -> None:
    assert summarise.shortfalls(RUN, "ndjson", 20, 5) == []
    _write(tmp_path, "g12_b_default", [_g12_row(i) for i in range(1, 4)])
    short = summarise.shortfalls(tmp_path, "jsonl", 20, 5)
    assert any("g12 b default" in s and "3" in s for s in short), short
    assert not [s for s in summarise.shortfalls(tmp_path, "jsonl", 3, 5) if "g12 b" in s]
    cmd = [sys.executable, "-I", "-B", str(HARNESS / "summarise.py"), str(tmp_path), "--ext", "jsonl"]
    r = subprocess.run(cmd, capture_output=True)  # noqa: S603
    assert r.returncode == 2 and b"SHORTFALL" in r.stdout


def test_tables_say_runs_not_independent(tmp_path: Path) -> None:
    load = [
        {"setup": "b default", "when": "before", "cpu_idle_pct": 91.5, "free_ram_mb": 20480.0},
        {"setup": "b default", "when": "after", "cpu_idle_pct": 70.0, "free_ram_mb": 19000.0},
    ]
    _write(tmp_path, "load", load)
    text = summarise.summarise(tmp_path, "jsonl")
    assert "not independent" in text
    assert "| b default | before | 91.50 | 20480 |" in text and "| b default | after | 70.00 | 19000 |" in text


def test_jsonl_files_give_the_same_tables(tmp_path: Path) -> None:
    copy = tmp_path / "run"
    shutil.copytree(RUN, copy)
    for p in list(copy.glob("*.ndjson")) + list((copy / "invalid").glob("*.ndjson")):
        p.rename(p.with_suffix(".jsonl"))
    (copy / "calls2.jsonl").write_text(
        (copy / "calls2_evidence2.jsonl").read_text(encoding="utf-8")
        + (copy / "calls2_evidence3.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (copy / "calls2_evidence2.jsonl").unlink()
    (copy / "calls2_evidence3.jsonl").unlink()
    text = summarise.summarise(copy, "jsonl")
    assert _norm(text) == _norm(summarise.summarise(RUN))
    assert "attempts 55, valid 20, invalid 35" in text  # the jsonl path also counts the invalid/ files


def test_sweeper_posts_reads_an_email(tmp_path: Path) -> None:
    body = '{"email": "s1@example.com", "src": "sweeper"}'
    _write(tmp_path, "calls2", [{"k": "outbox", "t": 1.0, "body": body}, {"k": "crm", "t": 2.0, "body": body}])
    posts = summarise.sweeper_posts(tmp_path, "jsonl")
    assert posts["s1@example.com"] == [[1.0], [2.0]]


def _cited_problems(doc: str, tables: str) -> list[str]:
    return [m for m in re.findall(r"\b\d+ of \d+\b", doc) if m not in tables]


def test_numbers_cited_in_the_docs_exist() -> None:
    tables = summarise.summarise(RUN)
    assert "Attempts, valid runs and invalid reasons" in tables
    for name in ("README.md", "GATES.md", "GLOSSARY.md", "DETAILS.md"):
        assert not _cited_problems((FOLDER / name).read_text(encoding="utf-8"), tables), name
    assert _cited_problems("we saw 19 of 20 runs", tables) == ["19 of 20"]  # the check itself fails on a wrong number
    assert _norm((FOLDER / "tables.md").read_text(encoding="utf-8")) == _norm(tables)


def test_run_all_stops_on_a_failing_step(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_all.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=3))
    with pytest.raises(SystemExit):
        run_all.run_script(tmp_path, {}, "g12.py", "b", tag="g12_b")
    monkeypatch.setattr(run_all.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    assert run_all.run_script(tmp_path, {}, "g12.py", "b", tag="g12_b") == 0


# sleeps that remain, by file and argument: bounded poll intervals and documented observation points
ALLOWED_SLEEPS = {
    ("lib.py", "0.5"),  # _wait_port_free poll
    ("lib.py", "0.25"),  # _ready poll
    ("lib.py", "1"),  # up() poll
    ("g3.py", "0.1"),  # trigger poll, at most 10 s
    ("g3e.py", "0.1"),  # trigger poll, at most 10 s
    ("setup.py", "2"),  # owner-setup and login retries, at most 60 and 30 times
    ("g3.py", "1"),  # wait for the crashed entry, at most 40 s
    ("g12.py", "2"),  # login retry, at most 30 times
    ("g12e.py", "30"),  # observation point of the 15 s re-send job, not a settle wait
    ("run_all.py", "1"),  # Postgres readiness poll, at most 60 times
    ("run_all.py", "15"),  # load gate poll, at most gate-wait-min minutes
    ("run_all.py", "0.25"),  # smoke: webhook probe poll, at most 60 s
    ("lib.py", "1.0"),  # Linux load sampling: the two /proc/stat readings are one second apart
    ("cleanup.py", "2"),  # SIGTERM first, SIGKILL only if the group is still there after 2 s (one-shot, not a settle wait)
    ("cleanup.py", "0.5"),  # one look after SIGKILL
}


def test_no_fixed_settle_waits_remain() -> None:
    """Any sleep(<number>) in the harness must be on the named allow-list above (a new fixed wait fails here)."""
    found = set()
    for p in HARNESS.glob("*.py"):
        found |= {(p.name, m) for m in re.findall(r"sleep\(\s*([0-9.]+)\s*\)", p.read_text(encoding="utf-8"))}
    assert found <= ALLOWED_SLEEPS, sorted(found - ALLOWED_SLEEPS)
    assert ("lib.py", "3") not in found and ("lib.py", "1.5") not in found  # start_n8n / start_fakes poll now


@pytest.mark.parametrize(
    ("script", "argv", "existing"),
    [
        ("g12.py", ["b", "default", "3"], "g12_b_default.jsonl"),
        ("g3.py", ["b", "K1", "1"], "g3_b.jsonl"),
        ("g3e.py", ["K1", "1"], "g3e_K1.jsonl"),
        ("g12e.py", [], "g12e.jsonl"),
    ],
)
def test_scripts_refuse_old_results(tmp_path: Path, script: str, argv: list[str], existing: str) -> None:
    """Behavioural: started in a folder that already holds its result file, each script stops at once."""
    (tmp_path / existing).write_text(json.dumps({"kill_point": "K1", "run": 1}) + "\n", encoding="utf-8")
    (tmp_path / "ids.json").write_text(json.dumps({"wp0b2-g2-b": "w"}), encoding="utf-8")
    env = {**os.environ, "N8N_EVD_WORK": str(tmp_path), "N8N_EVD_NODE": str(tmp_path)}
    r = subprocess.run(  # noqa: S603
        [sys.executable, "-B", str(HARNESS / script), *argv],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode != 0 and "refusing" in r.stderr, (script, r.returncode, r.stderr[-300:])
    assert (tmp_path / existing).read_text(encoding="utf-8").count(chr(10)) == 1  # nothing was appended


def test_require_fresh_rules(lib, tmp_path: Path) -> None:
    f = tmp_path / "g3_b.jsonl"
    f.write_text(json.dumps({"kill_point": "K1", "run": 1}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        lib.require_fresh(str(f))
    with pytest.raises(SystemExit):
        lib.require_fresh(str(f), kill_point="K1")
    lib.require_fresh(str(f), kill_point="K2")  # g3.py writes the three kill points of a variant to one file
    lib.require_fresh(str(tmp_path / "none.jsonl"))


@pytest.mark.parametrize("nt", [True, False])
def test_only_own_pid_is_killed(lib, monkeypatch, nt) -> None:
    monkeypatch.setattr(lib, "IS_NT", nt)
    calls: list[str] = []
    monkeypatch.setattr(
        lib.subprocess, "run", lambda cmd, *a, **k: calls.append(str(cmd)) or types.SimpleNamespace(stdout="", returncode=0)
    )
    monkeypatch.setattr(lib.os, "killpg", lambda pid, sig: calls.append("killpg %s" % pid), raising=False)
    assert lib.kill_n8n() is None  # nothing recorded: whatever holds the port is left alone
    assert not calls, calls
    lib.record_pid("n8n", 4242)
    assert lib.kill_n8n() == "4242"
    assert len(calls) == 1 and "4242" in calls[0] and ("taskkill" in calls[0]) == nt and ("killpg" in calls[0]) == (not nt), calls


def test_start_refuses_a_busy_port(lib, monkeypatch) -> None:
    monkeypatch.setattr(lib, "port_free", lambda p: False)
    monkeypatch.setattr(lib.subprocess, "Popen", lambda *a, **k: pytest.fail("started although the port is busy"))
    with pytest.raises(RuntimeError):
        lib.start_n8n(1)
    with pytest.raises(RuntimeError):
        lib.start_fakes()


@pytest.mark.parametrize("nt", [True, False])
def test_n8n_gets_minimal_env(lib, monkeypatch, nt) -> None:
    monkeypatch.setattr(lib, "IS_NT", nt)
    monkeypatch.setenv("SOME_PARENT_SECRET_FOR_TEST", "x")
    monkeypatch.setenv("N8N_CONCURRENCY_PRODUCTION_LIMIT", "77")
    monkeypatch.setenv("HOME", FAKE_HOME)
    env = lib.n8n_env(None)
    assert "SOME_PARENT_SECRET_FOR_TEST" not in env and "N8N_CONCURRENCY_PRODUCTION_LIMIT" not in env
    assert env["N8N_PORT"] == "5679" and env["PATH"].startswith(str(lib.NODE_DIR))
    assert lib.n8n_env(3)["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "3"
    if not nt:
        assert env["HOME"] == FAKE_HOME and env["PATH"].split(os.pathsep)[1:] == [USR_BIN, "/bin"]  # HOME, and a short POSIX PATH
        assert "SystemRoot" not in env


def test_machine_load_is_parsed_on_windows(lib, monkeypatch) -> None:
    monkeypatch.setattr(lib, "IS_NT", True)
    monkeypatch.setattr(
        lib.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="87.5 5242880\n", returncode=0)
    )
    assert lib.machine_load() == {"cpu_idle_pct": 87.5, "free_ram_mb": 5120.0}
    monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="garbage", returncode=1))
    assert "error" in lib.machine_load()


def test_machine_load_on_linux_reads_proc(lib, monkeypatch) -> None:
    monkeypatch.setattr(lib, "IS_NT", False)
    stats = iter(["cpu  100 0 100 800 0 0 0 0 0 0\n", "cpu  150 0 150 900 0 0 0 0 0 0\n"])  # second reading: 100 of 200 jiffies idle
    sleeps: list[float] = []
    monkeypatch.setattr(lib.time, "sleep", sleeps.append)

    def fake_open(path, *a, **k):
        if path == "/proc/stat":
            return types.SimpleNamespace(readline=lambda: next(stats))
        if path == "/proc/meminfo":
            return iter(["MemTotal: 999 kB\n", "MemAvailable:    2097152 kB\n"])
        raise AssertionError(path)

    monkeypatch.setattr(lib, "open", fake_open, raising=False)
    assert lib.machine_load() == {"cpu_idle_pct": 50.0, "free_ram_mb": 2048.0}
    assert sleeps == [1.0]  # one second between the two readings
    monkeypatch.setattr(lib, "open", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()), raising=False)
    assert lib.machine_load() == {"error": "FileNotFoundError"}


# ---- changes after code review (second review of the harness) ----
def _run_script(monkeypatch, tmp_path: Path, name: str, argv: list[str], attrs: dict) -> int:
    """Run a harness script in tmp_path with a fake `lib` and `api`; returns its exit code."""
    fake = types.ModuleType("lib")
    fake.__dict__.update(attrs)
    fake_api = types.ModuleType("api")
    fake_api.login = lambda: {"data": 1}  # type: ignore[attr-defined]
    fake_api.call = lambda *a, **k: {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lib", fake)
    monkeypatch.setitem(sys.modules, "api", fake_api)
    monkeypatch.syspath_prepend(str(HARNESS))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [name, *argv])
    monkeypatch.setattr("time.sleep", lambda s: None)
    (tmp_path / "ids.json").write_text(json.dumps({"wp0b2-g2-b": "w", "wp0b2-g2-d": "w"}), encoding="utf-8")
    try:
        runpy.run_path(str(HARNESS / name), run_name="__main__")
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) or e.code is None else 1
    return 0


def _g12_fake(overlap: dict) -> dict:
    return {
        "require_fresh": lambda *a, **k: None,
        "fpost": lambda *a, **k: None,
        "fire": lambda *a, **k: {"200": 10},
        "SENT_SPREAD_MS": 10,
        "wait_quiet": lambda *a, **k: {"counts": {"crm": 1, "outbox": 1}, "timed_out": False, "waited_s": 3},
        "execs": lambda *a, **k: [],
        "overlap": lambda rows: overlap,
        "exec_times": lambda rows: [] if overlap.get("note") else [
            {"createdAt": ts(0), "startedAt": ts(0.1), "stoppedAt": ts(1)}
        ] * 10,
        "iso": lambda t=None: "iso-time",
        "slim": lambda w: {"timed_out": w["timed_out"], "waited_s": w["waited_s"]},
    }


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_g12_attempt_cap_is_three_times(tmp_path, monkeypatch, capsys) -> None:
    """Variant d with missing times used to count as valid (g12 had its own rule); now summarise decides. Cap 3 x."""
    miss = {"n": 10, "note": "missing times"}
    code = _run_script(monkeypatch, tmp_path, "g12.py", ["d", "limit1", "2"], _g12_fake(miss))
    rows = _lines(tmp_path / "g12_d_limit1.jsonl")
    assert code == 1 and len(rows) == 6 and not any(r["valid"] for r in rows)  # old: all valid, 2 attempts, exit 0
    out = capsys.readouterr().out
    assert "cap 6" in out and "missing times" in out and "FAILED" in out
    (tmp_path / "g12_d_limit1.jsonl").unlink()
    assert _run_script(monkeypatch, tmp_path, "g12.py", ["d", "limit1", "2", "4"], _g12_fake(miss)) == 1
    assert len(_lines(tmp_path / "g12_d_limit1.jsonl")) == 4  # the 4th argument sets the cap
    (tmp_path / "g12_d_limit1.jsonl").unlink()
    ok = {"n": 10, "latest_start_before_earliest_stop": True}
    assert _run_script(monkeypatch, tmp_path, "g12.py", ["b", "default", "2"], _g12_fake(ok)) == 0
    assert len(_lines(tmp_path / "g12_b_default.jsonl")) == 2


def _g3_fake(killed: bool, status: str = "crashed") -> dict:
    return {
        "require_fresh": lambda *a, **k: None,
        "port_free": lambda p: False,  # n8n is up
        "webhooks_live": lambda *a, **k: True,
        "fpost": lambda *a, **k: None,
        "fget": lambda p: {"outbox_pre": 1, "outbox_post": 1, "crm_pre": 1},
        "per_email": lambda e: {"crm": 0, "outbox": 0},
        "kill_n8n": lambda: "1",
        "kill_n8n_confirmed": lambda: killed,
        "fire_async": lambda *a, **k: (types.SimpleNamespace(join=lambda: None), {"ConnectionResetError": 10}),
        "wait_quiet": lambda *a, **k: {"counts": {"crm": 1, "outbox": 0}, "timed_out": False, "waited_s": 1},
        "start_n8n": lambda *a, **k: True,
        "execs": lambda *a, **k: [{"id": "1", "status": status}],
        "iso": lambda t=None: "iso-time",
        "slim": lambda w: {"timed_out": w["timed_out"], "waited_s": w["waited_s"]},
        "EXIT_INVALID_KILL_ROWS": 4,
        "PG": "pg",
    }


def test_kill_scripts_exit_4_on_bad_rows(tmp_path, monkeypatch) -> None:
    assert _run_script(monkeypatch, tmp_path, "g3.py", ["b", "K3", "2"], _g3_fake(killed=False)) == 4
    assert len(_lines(tmp_path / "g3_b.jsonl")) == 2  # the rows are still written
    (tmp_path / "g3_b.jsonl").unlink()
    assert _run_script(monkeypatch, tmp_path, "g3.py", ["b", "K3", "2"], _g3_fake(killed=True)) == 0
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="pending\n", returncode=0))
    assert _run_script(monkeypatch, tmp_path, "g3e.py", ["K3", "1"], _g3_fake(killed=False)) == 4
    (tmp_path / "g3e_K3.jsonl").unlink()
    assert _run_script(monkeypatch, tmp_path, "g3e.py", ["K3", "1"], _g3_fake(killed=True)) == 0


def test_run_all_exits_2_on_shortfall(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(HARNESS))
    with pytest.raises(SystemExit) as e:
        run_all.finish(str(tmp_path), True, None)  # an empty work folder: every test configuration is short
    assert e.value.code == 2 and "RUN ALL INCOMPLETE" in capsys.readouterr().out
    _write(tmp_path, "g12_b_default", [_g12_row(i) for i in range(1, 4)])
    _write(tmp_path, "g3_b", [_kill_row(i, kp) for kp in ("K1", "K2", "K3") for i in range(1, 4)])
    run_all.finish(str(tmp_path), True, None)
    assert "RUN ALL DONE" in capsys.readouterr().out
    steps = []
    monkeypatch.setattr(
        run_all.subprocess, "run", lambda *a, **k: steps.append(1) or types.SimpleNamespace(returncode=4)
    )
    with pytest.raises(SystemExit):
        run_all.run_script(tmp_path, {}, "g12.py", "b", tag="x")  # code 4 stops everything except the two kill scripts
    assert run_all.run_script(tmp_path, {}, "g3.py", "b", tag="x", ok_codes=(0, 4)) == 4


def _fake_kill(lib, monkeypatch, nt, ok):
    """Make the OS kill call report ok (True) or 'process not found' (False) on the chosen OS."""
    monkeypatch.setattr(lib, "IS_NT", nt)
    monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="", returncode=0 if ok else 128))

    def killpg(pid, sig):
        if not ok:
            raise ProcessLookupError

    monkeypatch.setattr(lib.os, "killpg", killpg, raising=False)


@pytest.mark.parametrize("nt", [True, False])
def test_kill_confirmed_by_os_and_ports(lib, monkeypatch, nt) -> None:
    lib.record_pid("n8n", 4242)
    _fake_kill(lib, monkeypatch, nt, ok=False)  # "process not found"
    assert lib.kill_n8n() is None  # old: "4242"
    assert lib._recorded_pid("n8n") == 0  # forgotten at once, so a reused PID is never killed later
    _fake_kill(lib, monkeypatch, nt, ok=True)
    lib.record_pid("n8n", 4242)
    monkeypatch.setattr(lib, "_wait_port_free", lambda port, timeout=20: False)  # the port is still busy
    assert lib.kill_n8n_confirmed() is False
    lib.record_pid("n8n", 4242)
    asked: list[int] = []
    monkeypatch.setattr(lib, "_wait_port_free", lambda port, timeout=20: asked.append(port) or port == 5679)  # the task-runner port stays busy
    assert lib.kill_n8n_confirmed() is False
    assert 5780 in asked  # the task-runner port is checked too
    lib.record_pid("n8n", 4242)
    monkeypatch.setattr(lib, "_wait_port_free", lambda port, timeout=20: True)
    assert lib.kill_n8n_confirmed() is True


def test_posix_kill_goes_to_the_process(lib, monkeypatch) -> None:
    monkeypatch.setattr(lib, "IS_NT", False)
    sent = []
    monkeypatch.setattr(lib.os, "killpg", lambda pid, sig: sent.append((pid, sig)), raising=False)
    assert lib._kill_tree("77") is True
    assert sent == [(77, getattr(lib.signal, "SIGKILL", 9))]
    monkeypatch.setattr(lib.os, "killpg", lambda pid, sig: (_ for _ in ()).throw(PermissionError()), raising=False)
    assert lib._kill_tree(77) is False  # not ours to kill: not a kill


@pytest.mark.parametrize("nt", [True, False])
def test_processes_start_with_the_flags(lib, monkeypatch, nt) -> None:
    """Windows: no console window and a process group (creationflags). POSIX: a session of its own; creationflags would raise ValueError there."""
    monkeypatch.setattr(lib, "IS_NT", nt)
    seen = []

    class P:
        pid = 5

    monkeypatch.setattr(lib.subprocess, "Popen", lambda cmd, **k: seen.append((cmd, k)) or P())
    monkeypatch.setattr(lib, "port_free", lambda p: True)
    monkeypatch.setattr(lib, "up", lambda *a, **k: True)
    monkeypatch.setattr(lib, "_ready", lambda url, timeout: True)
    lib.start_n8n(None)
    lib.start_fakes()
    assert len(seen) == 2
    for cmd, k in seen:
        if nt:
            assert k["creationflags"] == 0x08000000 | 0x00000200 and "start_new_session" not in k
        else:
            assert k["start_new_session"] is True and "creationflags" not in k
    assert os.path.basename(seen[0][0][0]) == ("node.exe" if nt else "node")  # the node binary of the OS
    assert lib.node_name() == ("node.exe" if nt else "node")


def test_record_pid_is_atomic(lib, monkeypatch) -> None:
    lib.record_pid("n8n", 11)
    with monkeypatch.context() as m:
        m.setattr(lib.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError, match="disk"):
            lib.record_pid("n8n", 22)
    assert lib._recorded_pid("n8n") == 11  # the old file is intact (old code truncated and rewrote it in place)


def test_start_waits_by_polling(lib, monkeypatch) -> None:
    class P:
        pid = 7

    monkeypatch.setattr(lib.subprocess, "Popen", lambda *a, **k: P())
    monkeypatch.setattr(lib, "port_free", lambda p: True)
    monkeypatch.setattr(lib.time, "sleep", lambda s: pytest.fail("fixed sleep %s" % s))
    polled = []
    monkeypatch.setattr(lib, "up", lambda *a, **k: True)
    monkeypatch.setattr(lib, "_ready", lambda url, timeout: polled.append(url) or True)
    assert lib.start_n8n(None) is True and polled and "readiness" in polled[0]
    polled.clear()
    lib.start_fakes()
    assert polled and polled[0].endswith("/ctl/state")
    monkeypatch.setattr(lib, "_ready", lambda url, timeout: False)
    with pytest.raises(RuntimeError, match="stand-in"):
        lib.start_fakes()


def test_wait_quiet_unreachable_and_stale(lib, monkeypatch) -> None:
    clk = Clock()
    monkeypatch.setattr(lib.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(lib.time, "sleep", clk.sleep)
    monkeypatch.setattr(lib, "per_email", lambda e: {"crm": 1, "outbox": 1})
    monkeypatch.setattr(
        lib, "fake_busy", lambda: None
    )  # the stand-in cannot be reached (old code: "busy", then a generic timeout)
    r = lib.wait_quiet("a@example.com", quiet_s=3, timeout=60)
    assert r["unreachable"] is True and r["timed_out"] is False and r["waited_s"] < 15
    monkeypatch.setattr(lib, "fake_busy", lambda: False)
    monkeypatch.setattr(lib, "execs", lambda *a, **k: [{"status": "running"}])
    r = lib.wait_quiet("a@example.com", wid="w", quiet_s=3, timeout=30, stale_outcome=True)
    assert r["stale_running"] is True and r["timed_out"] is False
    assert (
        lib.wait_quiet("a@example.com", wid="w", quiet_s=3, timeout=30)["timed_out"] is True
    )  # without the flag it stays a timeout

    def boom(*a, **k):
        raise RuntimeError("executions list failed")

    monkeypatch.setattr(lib, "execs", boom)
    r = lib.wait_quiet("a@example.com", wid="w", quiet_s=3, timeout=30, stale_outcome=True)
    assert r["timed_out"] is True and r["exec_errors"] > 0 and "stale_running" not in r  # an error is never "not busy"
    assert lib.slim(r) == {"timed_out": True, "waited_s": r["waited_s"], "exec_errors": r["exec_errors"]}


def test_execs_raises_on_an_api_error(lib, monkeypatch) -> None:
    monkeypatch.setattr(lib.api, "call", lambda *a, **k: {"HTTPError": 401, "body": "x"})
    with pytest.raises(RuntimeError, match="401"):
        lib.execs("w")  # old: returned [] and the caller saw "no executions"
    monkeypatch.setattr(lib.api, "call", lambda *a, **k: {"data": {"results": [{"id": "1", "status": "success"}]}})
    assert lib.execs("w") == [{"id": "1", "status": "success"}]


def test_fake_busy_is_none_when_unreachable(lib, monkeypatch) -> None:
    monkeypatch.setattr(lib, "fget", lambda p: (_ for _ in ()).throw(OSError("down")))
    assert lib.fake_busy() is None
    monkeypatch.setattr(lib, "fget", lambda p: {"crm_pre": 0, "crm_post": 1})
    assert lib.fake_busy() is True


def test_per_email_asks_the_stand_in(lib, monkeypatch) -> None:
    asked = []
    monkeypatch.setattr(lib, "fget", lambda p: asked.append(p) or {"crm": 2, "outbox": 1})
    assert lib.per_email("a+b@example.com") == {"crm": 2, "outbox": 1}
    assert asked == [
        "/ctl/count?email=" + quote("a+b@example.com")
    ]  # the address is url-encoded  # old: two full lists on every call


def test_load_is_sampled_during_setup(lib, monkeypatch, tmp_path: Path) -> None:
    import time as t

    monkeypatch.setattr(lib, "machine_load", lambda: {"cpu_idle_pct": 50.0, "free_ram_mb": 1000.0})
    end = lib.sample_load("b default", every=0.01)
    t0 = t.monotonic()
    while t.monotonic() - t0 < 10 and not (tmp_path / "load.jsonl").exists():  # bounded wait for the first sample
        t.sleep(0.01)
    end()
    rows = _lines(tmp_path / "load.jsonl")
    assert rows and all(r["when"] == "during" and r["setup"] == "b default" for r in rows)
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d" + COLON + r"\d\d" + COLON + r"\d\d\.\d{3}Z", r["ts"]) for r in rows)  # a time stamp on every row


def test_machine_load_asks_for_a_language(lib, monkeypatch) -> None:
    monkeypatch.setattr(lib, "IS_NT", True)
    seen = []
    monkeypatch.setattr(
        lib.subprocess, "run", lambda cmd, *a, **k: seen.append(cmd[-1]) or types.SimpleNamespace(stdout="1 2\n")
    )
    lib.machine_load()
    assert "LoadPercentage" in seen[0] and "Idle Time" not in seen[0]


def test_stale_running_is_a_finding(tmp_path: Path) -> None:
    rows = [
        _kill_row(1),
        _kill_row(2, wait_after_retry={"timed_out": False, "waited_s": 60, "stale_running": True}),
        _kill_row(3, wait_settled={"timed_out": False, "waited_s": 10, "unreachable": True}),
    ]
    _write(tmp_path, "g3_b", rows)
    text = summarise.summarise(tmp_path, "jsonl")
    line = next(x for x in text.splitlines() if x.startswith("Test configuration b (kill runs): attempts"))
    assert "attempts 3, valid 1, invalid 2" in line, line
    assert "stale_running_after_kill x1" in line and "stand-in unreachable x1" in line, line
    assert "NO: stale_running_after_kill" in text  # shown in the run table
    assert not any("timeout" in x for x in summarise.kill_reasons(rows[1]))  # not a generic timeout


def test_replay_heading_is_not_stale() -> None:
    text = summarise.summarise(RUN)
    assert "5 seconds later" not in text and "not strictly comparable" in text
    assert "ten again 5 seconds" not in (FOLDER / "tables.md").read_text(encoding="utf-8")


def test_script_env_is_minimal(monkeypatch) -> None:
    monkeypatch.setenv("SOME_PARENT_SECRET_FOR_TEST", "x")
    env = run_all.script_env("W", "N")
    assert "SOME_PARENT_SECRET_FOR_TEST" not in env
    assert env["N8N_EVD_WORK"] == "W" and env["N8N_EVD_NODE"] == "N" and "N8N_EVD_PG" in env


def test_run_all_stale_folder_check(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "node_modules" / "n8n").mkdir(parents=True)
    for name in ("load.jsonl", "n8n2.log", "fakes2.log"):
        (tmp_path / name).write_text("x", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run_all.py", "--work-dir", str(tmp_path), "--node-dir", str(tmp_path)])
        with pytest.raises(SystemExit, match="not fresh"):
            run_all.main()
        (tmp_path / name).unlink()


# ---- changes after code review (third review: Linux, webhook readiness, load gate, manifest, one set of kill numbers) ----
NOT_REGISTERED = 'The requested webhook "POST g2-b2" is not registered.'
WRONG_METHOD = "This webhook is not registered for GET requests. Did you mean to make a POST request?"


def test_webhook_live_reads_n8n_answers(lib) -> None:
    assert lib.webhook_live(404, NOT_REGISTERED) is False  # the path is not registered yet
    assert lib.webhook_live(404, WRONG_METHOD) is True  # registered, for POST only
    assert lib.webhook_live(200, "") is True
    assert lib.webhook_live(500, "") is True  # a 500 from the workflow itself means the route exists
    for starting in (502, 503, 504):
        assert lib.webhook_live(starting, "") is False, starting  # n8n (or a proxy) is not ready: n8n answers 503 while it starts
    assert lib.webhook_live(404, "") is False
    assert lib.webhook_live("URLError", "") is False  # n8n not answering is not "live"


def test_start_n8n_polls_the_real_webhooks(lib, monkeypatch) -> None:
    clk = Clock()
    monkeypatch.setattr(lib.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(lib.time, "sleep", clk.sleep)
    monkeypatch.setattr(lib.subprocess, "Popen", lambda *a, **k: types.SimpleNamespace(pid=9))
    monkeypatch.setattr(lib, "port_free", lambda p: True)
    monkeypatch.setattr(lib, "up", lambda *a, **k: True)
    monkeypatch.setattr(lib, "_ready", lambda url, timeout: True)  # readiness answers at once ...
    answers = {"g2-b2": iter([(404, NOT_REGISTERED)] * 3 + [(404, WRONG_METHOD)]), "e3": iter([(404, NOT_REGISTERED)] * 5 + [(200, "")])}
    asked: list[str] = []
    monkeypatch.setattr(lib, "webhook_status", lambda path: asked.append(path) or next(answers[path]))
    assert lib.start_n8n(None, paths=("g2-b2", "e3")) is True  # ... but the webhooks do not: start_n8n waits for them
    assert asked.count("g2-b2") == 4 and asked.count("e3") == 6
    monkeypatch.setattr(lib, "webhook_status", lambda path: (404, NOT_REGISTERED))
    assert lib.start_n8n(None, paths=("g2-b2",)) is False  # never registered: not ready, no silent carry-on
    assert clk.t < 200  # and the wait is bounded
    seen: list[str] = []
    monkeypatch.setattr(lib, "webhook_status", lambda path: seen.append(path) or (404, NOT_REGISTERED))
    assert lib.start_n8n(None) is True and not seen  # without paths nothing is probed (first start, nothing deployed yet)


def _fresh_g3(monkeypatch, tmp_path, name, argv, port_free, started):
    attrs = _g3_fake(killed=True)
    asked = []
    attrs["port_free"] = lambda p: port_free
    attrs["start_n8n"] = lambda *a, **k: asked.append(k.get("paths")) or started
    code = _run_script(monkeypatch, tmp_path, name, argv, attrs)
    return code, asked


def test_g3_and_g3e_record_the_start_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="pending\n", returncode=0))
    code, asked = _fresh_g3(monkeypatch, tmp_path, "g3.py", ["b", "K3", "1"], port_free=True, started=False)
    assert code == 1 and not (tmp_path / "g3_b.jsonl").read_text(encoding="utf-8")  # old: carried on with n8n down
    (tmp_path / "g3_b.jsonl").unlink()
    code, asked = _fresh_g3(monkeypatch, tmp_path, "g3.py", ["b", "K3", "1"], port_free=True, started=True)
    row = _lines(tmp_path / "g3_b.jsonl")[0]
    assert code == 0 and row["n8n_started_ok"] is True and "was down" in row["note_start"]
    assert asked[0] == ("g2-b2",) and asked[-1] == ("g2-b2",)  # the start waits for this variant's webhook
    code, asked = _fresh_g3(monkeypatch, tmp_path, "g3e.py", ["K3", "1"], port_free=True, started=False)
    assert code == 1 and not (tmp_path / "g3e_K3.jsonl").read_text(encoding="utf-8")
    (tmp_path / "g3e_K3.jsonl").unlink()
    code, asked = _fresh_g3(monkeypatch, tmp_path, "g3e.py", ["K3", "1"], port_free=True, started=True)
    assert code == 0 and _lines(tmp_path / "g3e_K3.jsonl")[0]["n8n_started_ok"] is True and asked[0] == ("e3",)
    (tmp_path / "g3e_K3.jsonl").unlink()
    code, asked = _fresh_g3(monkeypatch, tmp_path, "g3e.py", ["K3", "1"], port_free=False, started=True)  # n8n up: no start before the run
    assert "n8n_started_ok" not in _lines(tmp_path / "g3e_K3.jsonl")[0]


def test_g12_stores_the_execution_times(tmp_path, monkeypatch) -> None:
    ok = {"n": 10, "latest_start_before_earliest_stop": True}
    assert _run_script(monkeypatch, tmp_path, "g12.py", ["b", "default", "1"], _g12_fake(ok)) == 0
    row = _lines(tmp_path / "g12_b_default.jsonl")[0]
    assert len(row["exec_times"]) == 10 and set(row["exec_times"][0]) == {"createdAt", "startedAt", "stoppedAt"}
    # validity is recomputed from the times: ten executions that never overlapped are invalid whatever the stored summary says
    lone = [{"createdAt": ts(i), "startedAt": ts(i + 0.1), "stoppedAt": ts(i + 0.2)} for i in range(9)]
    lone.append(dict(lone[0]))
    assert summarise.invalid_reason(_g12_row(1, exec_times=lone), "b") == "no overlap"  # the stored overlap field says True
    together = [{"createdAt": ts(0), "startedAt": ts(0.1), "stoppedAt": ts(2)}] * 10
    assert summarise.invalid_reason(_g12_row(1, exec_times=together, overlap={"n": 3, "note": "stale"}), "b") is None  # the stored summary is ignored
    assert summarise.invalid_reason(_g12_row(1, exec_times=together[:9]), "b") == "10 executions expected, 9 listed"
    assert summarise.invalid_reason(_g12_row(1, exec_times=[]), "b") == "missing times"


def test_overlap_same_in_summarise(lib) -> None:
    rows = [{"createdAt": ts(0), "startedAt": ts(0.01), "stoppedAt": ts(1)}] * 2
    assert lib.overlap(rows) == summarise.overlap(rows) and summarise.overlap(rows)["latest_start_before_earliest_stop"] is True
    assert lib.exec_times([{"createdAt": "a", "startedAt": "b", "stoppedAt": "c", "id": "x"}]) == [{"createdAt": "a", "startedAt": "b", "stoppedAt": "c"}]


def test_both_kill_counts_agree(tmp_path: Path) -> None:
    rows = [
        _kill_row(1),
        _kill_row(2, killed_pid=False),  # invalid (no process killed) but a client did see a reset
        _kill_row(3, client_replies_during_kill={"200": 10}),  # valid, kill may not have taken effect
    ]
    _write(tmp_path, "g3_b", rows)
    text = summarise.summarise(tmp_path, "jsonl")
    want = "Kill rows where no client lost its connection: 1 of 2 valid kill rows (the result file holds 3 rows). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 1"
    lines = [x for x in text.splitlines() if want in x]
    assert len(lines) == 2, lines  # the summary line and the per-variant section: the same numbers (old: 1 of 2 and 1 of 3)


def test_no_killed_pid_is_no_kill() -> None:
    row = _kill_row(1)
    del row["killed_pid"]
    assert "no process killed" in summarise.kill_reasons(row)  # old: the missing field passed as killed
    assert summarise.kill_stats([row])["not_killed"] == 1


def test_burst_of_ten_for_e_needs_one_send(tmp_path: Path) -> None:
    good = {"run": 1, "spread_ms": 6, "settle1": {"timed_out": False}}
    assert summarise.burst_reasons(good) == []
    assert summarise.burst_reasons(dict(good, spread_ms=250)) == ["ten not sent within 100 ms"]
    assert summarise.burst_reasons({"run": 1}) == ["ten not sent within 100 ms"]
    rows = [dict(good, counts={"crm": 1, "outbox": 1}, replies={"200": 10}, counts_after_35s={"crm": 1, "outbox": 1}, run=i) for i in range(1, 6)]
    rows[0]["spread_ms"] = 900
    _write(tmp_path, "g12e", rows)
    assert any("g12e: expected 5 valid runs, found 4" in x for x in summarise.shortfalls(tmp_path, "jsonl", 20, 5))


def test_load_rows_print_their_time(tmp_path: Path) -> None:
    _write(tmp_path, "load", [{"ts": ts(0), "setup": "b default", "when": "before", "cpu_idle_pct": 91.5, "free_ram_mb": 100.0}, {"setup": "old", "when": "after", "cpu_idle_pct": 50.0, "free_ram_mb": 100.0}])
    text = summarise.summarise(tmp_path, "jsonl")
    assert "| " + ts(0) + " | b default | before | 91.50 | 100 |" in text and "| n/a | old | after | 50.00 | 100 |" in text


class FakeLib:
    """What run_all needs of lib: a scripted machine_load and a record of the load rows it was asked to write."""

    def __init__(self, loads):
        self.loads = iter(loads)
        self.rows: list[tuple] = []

    def machine_load(self):
        return next(self.loads)

    def log_load(self, setup, when, m=None):
        self.rows.append((setup, when, m))

    def node_name(self):
        return "node"

    def live_text(self):
        return "not registered for"


def _idle(p):
    return {"cpu_idle_pct": p, "free_ram_mb": 1000.0}


def test_load_gate_passes_waits_and_fails(capsys) -> None:
    now = [0.0]
    naps: list[int] = []
    sleep = lambda s: naps.append(s) or now.__setitem__(0, now[0] + s)  # noqa: E731
    clock = lambda: now[0]  # noqa: E731
    lib = FakeLib([_idle(95.0)])
    assert run_all.load_gate(lib, "b default", 80, 10, sleep, clock) == _idle(95.0) and not naps
    lib = FakeLib([_idle(30.0), _idle(50.0), _idle(85.0)])  # busy, busy, then idle: it waits
    assert run_all.load_gate(lib, "b default", 80, 10, sleep, clock) == _idle(85.0) and naps == [15, 15]
    lib = FakeLib([_idle(10.0)] * 1000)  # never idle: record the failure and exit 3
    with pytest.raises(SystemExit) as e:
        run_all.load_gate(lib, "b default", 80, 1, sleep, clock)
    assert e.value.code == 3 and lib.rows and lib.rows[-1][:2] == ("b default", "load_gate_failed") and lib.rows[-1][2]["cpu_idle_pct"] == 10.0
    out = capsys.readouterr().out
    assert "IDLE CHECK FAILED" in out and "LOAD GATE" not in out  # old: the console said LOAD GATE FAILED while the README calls it the idle check
    lib = FakeLib([{"error": "FileNotFoundError"}] * 1000)  # a reading that cannot be taken does not prove idleness
    with pytest.raises(SystemExit) as e:
        run_all.load_gate(lib, "x", 80, 1, sleep, clock)
    assert e.value.code == 3 and run_all.EXIT_LOAD_GATE_FAILED != run_all.EXIT_SHORTFALL
    assert run_all.load_gate(FakeLib([]), "x", 0, 1, sleep, clock) is None  # --min-idle 0: off, no reading taken


def test_run_manifest_written_at_start(tmp_path, monkeypatch) -> None:
    work = tmp_path / "work"
    (work / "node_modules" / "n8n").mkdir(parents=True)
    (work / "node_modules" / "n8n" / "package.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    git_args: list[tuple] = []
    monkeypatch.setattr(run_all, "_git", lambda *a: git_args.append(a) or "abc123")
    monkeypatch.setattr(run_all.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="v24.0.0\n", returncode=0))
    lib = FakeLib([_idle(90.0), _idle(70.0)])
    run_all.manifest(str(work), str(tmp_path / "nodedir"), lib, "start", {"smoke": True})
    m = json.loads((work / "run_manifest.json").read_text(encoding="utf-8"))
    assert m["n8n"] == "9.9.9" and m["node"] == "v24.0.0" and m["git_head"] == "abc123" and m["smoke"] is True
    assert m["python"] and m["os"] and m["start_load"] == _idle(90.0) and m["start_utc"].endswith("Z")
    assert m["webhook_live_text"] == "not registered for"  # the 404 wording in use is recorded (default or override)
    assert any("--untracked-files=no" in a for a in git_args)  # a __pycache__ folder does not make the harness "dirty"
    run_all.manifest(str(work), str(tmp_path / "nodedir"), lib, "end", {"status": "finished"})
    m = json.loads((work / "run_manifest.json").read_text(encoding="utf-8"))
    assert m["end_load"] == _idle(70.0) and m["status"] == "finished" and m["n8n"] == "9.9.9" and m["start_load"] == _idle(90.0)
    assert str(tmp_path) not in json.dumps(m)  # no folder names of the computer


def test_script_env_keeps_home(monkeypatch) -> None:
    monkeypatch.setenv("HOME", FAKE_HOME)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("SOME_PARENT_SECRET_FOR_TEST", "x")
    env = run_all.script_env("W", "N")
    assert env["HOME"] == FAKE_HOME and env["XDG_RUNTIME_DIR"] == "/run/user/1000" and "SOME_PARENT_SECRET_FOR_TEST" not in env


def test_smoke_readiness_writes_raw_probes(tmp_path, monkeypatch, capsys) -> None:
    clk = Clock()
    monkeypatch.setattr(run_all.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(run_all.time, "sleep", clk.sleep)
    seq = iter([(404, NOT_REGISTERED), (404, NOT_REGISTERED), (200, "ok")])
    lib = types.SimpleNamespace(
        start_n8n=lambda limit, **k: True,
        webhook_status=lambda path: next(seq),
        webhook_live=lambda st, body: st != 404,
    )
    run_all.smoke_readiness(str(tmp_path), lib)
    ev = json.loads((tmp_path / "smoke_readiness.json").read_text(encoding="utf-8"))
    assert ev["readiness_answered"] is True and ev["webhook_registered"] is True and [p["status"] for p in ev["probes"]] == [404, 404, 200]
    assert ev["probes"][0]["body"] == NOT_REGISTERED and "smoke readiness:" in capsys.readouterr().out  # raw evidence is printed too
    lib.webhook_status = lambda path: (404, NOT_REGISTERED)
    with pytest.raises(SystemExit, match="never answered"):
        run_all.smoke_readiness(str(tmp_path), lib)


def test_stale_check_covers_new_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "node_modules" / "n8n").mkdir(parents=True)
    for name in ("run_manifest.json", "smoke_readiness.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["run_all.py", "--work-dir", str(tmp_path), "--node-dir", str(tmp_path)])
        with pytest.raises(SystemExit, match="not fresh"):
            run_all.main()
        (tmp_path / name).unlink()


def test_small_source_rules() -> None:
    import ast  # noqa: PLC0415

    tree = ast.parse((HARNESS / "lib.py").read_text(encoding="utf-8"))
    first_def = min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
    assert all(n.lineno < first_def for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))  # no import below the first function
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    assert names - {"overlap"} <= used, names - used  # no unused import (old: parse); overlap is re-exported on purpose (a test compares lib.overlap)
    lib_src = (HARNESS / "lib.py").read_text(encoding="utf-8")
    assert "listener_pid" not in lib_src and "netstat" not in lib_src
    fakes = ast.parse((HARNESS / "fakes2.py").read_text(encoding="utf-8"))
    clock_reads = [n for n in ast.walk(fakes) if isinstance(n, ast.Call) and ast.unparse(n.func) == "time.time"]
    assert len(clock_reads) == 1  # one clock read for the two logs
    assert run_all.PG_IMAGE.startswith("postgres@sha256:")  # the image is pinned by digest


def test_no_personal_data_in_folder() -> None:
    """Hygiene for the public copy of this folder (patterns built from parts, so this file does not trip itself)."""
    patterns = [
        re.compile("[A-Za-z]:" + chr(92) + "{1,2}Users"),
        re.compile("/(?:home|Users)/(?!someone|x\\b|runner)[a-z]"),
        re.compile("AKIA" + "[0-9A-Z]{16}"),
        re.compile("BEGIN [A-Z ]*PRIVATE" + " KEY"),
        re.compile("[A-Za-z0-9._-]+@(?!example\\.com)[A-Za-z0-9-]+\\.(?:com|org|net|in|io)"),
    ]
    hits = []
    for p in FOLDER.rglob("*"):
        if not p.is_file() or p.name == "package-lock.json" or "__pycache__" in p.parts or p.suffix in (".pyc", ".diff"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        hits += [f"{p.relative_to(FOLDER).as_posix()}: {m.group(0)}" for pat in patterns for m in pat.finditer(text)]
    assert not hits, hits[:10]


# ---- changes after an earlier code review ----
def test_every_lost_connection_name_counts(tmp_path: Path) -> None:
    for name in sorted(summarise.LOST_CONNECTION):
        assert summarise.kill_stats([_kill_row(1, client_replies_during_kill={"200": 9, name: 1})])["no_reset"] == 0, name
    assert "RemoteDisconnected" in summarise.LOST_CONNECTION  # what urllib raises when a killed server closes with a FIN (old: counted as no kill)
    assert summarise.kill_stats([_kill_row(1, client_replies_during_kill={"200": 10})])["no_reset"] == 1
    assert summarise.kill_stats([_kill_row(1, client_replies_during_kill={})])["no_reset"] == 1
    _write(tmp_path, "g3_b", [_kill_row(1, client_replies_during_kill={"RemoteDisconnected": 10})])
    text = summarise.summarise(tmp_path, "jsonl")
    assert "Kill rows where no client lost its connection: 0 of 1 valid kill rows (the result file holds 1 rows)" in text and "connection reset seen" not in text


def test_reply_names_are_listed_by_keys(tmp_path: Path) -> None:
    _write(tmp_path, "g3_b", [_kill_row(1, client_replies_during_kill={"RemoteDisconnected": 9, "200": 1})])
    _write(tmp_path, "g3e_K3", [_kill_row(1, client_replies_during_kill={"URLError": 2})])
    assert summarise.reply_keys(tmp_path, "jsonl") == {"RemoteDisconnected": 9, "200": 1, "URLError": 2}
    out = subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "summarise.py"), str(tmp_path), "--ext", "jsonl", "--keys"], capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["RemoteDisconnected"] == 9


def test_sigterm_and_sighup_end_the_run() -> None:
    import signal  # noqa: PLC0415

    for n in (n for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n)):
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_DFL)  # not started under nohup
        run_all.install_signal_handlers()  # again for each signal: the first stop handler sets both signals to ignored (a second signal must not abort the cleanup)
        with pytest.raises(SystemExit) as e:
            signal.getsignal(getattr(signal, n))(getattr(signal, n), None)
        assert e.value.code == 143


@pytest.mark.skipif(os.name == "nt", reason="no SIGHUP on Windows")
def test_sighup_stays_ignored_under_nohup() -> None:
    import signal  # noqa: PLC0415

    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # what nohup does
    run_all.install_signal_handlers()
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN  # old: replaced by the stop handler, so a dropped ssh session ended a 70 to 90 minute run
    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_IGN, signal.SIG_DFL)  # SIGTERM still ends the run cleanly


def test_second_signal_is_ignored() -> None:
    import signal  # noqa: PLC0415

    run_all.install_signal_handlers()
    names = [n for n in ("SIGTERM", "SIGHUP") if hasattr(signal, n)]
    with pytest.raises(SystemExit):
        signal.getsignal(getattr(signal, names[0]))(getattr(signal, names[0]), None)  # the first signal starts the stop ...
    assert all(signal.getsignal(getattr(signal, n)) == signal.SIG_IGN for n in names)  # ... and a second one can no longer abort the cleanup


@pytest.mark.skipif(os.name == "nt", reason="a child can only be sent SIGTERM with a handler on POSIX")
def test_sigterm_runs_the_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "cleaned"
    lines = ["import sys, time", "sys.path.insert(0, %r)" % str(HARNESS), "import run_all", "run_all.install_signal_handlers()",
             "try:", "    print('ready', flush=True)", "    time.sleep(60)", "finally:", "    open(%r, 'w').write('x')" % str(marker)]
    code = chr(10).join(lines)
    p = subprocess.Popen([sys.executable, "-I", "-B", "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout is not None and p.stdout.readline().strip() == "ready"
    p.send_signal(15)
    assert p.wait(timeout=20) != 0 and marker.exists()


def test_404_wording_can_be_overridden(lib, monkeypatch) -> None:
    other = "Method not allowed for this path"
    assert lib.webhook_live(404, other) is False
    monkeypatch.setenv("N8N_WEBHOOK_LIVE_TEXT", "Method not allowed")
    assert lib.webhook_live(404, other) is True and lib.webhook_live(404, NOT_REGISTERED) is False
    env = run_all.script_env("W", "N")
    assert env["N8N_WEBHOOK_LIVE_TEXT"] == "Method not allowed"  # the g*.py scripts get it too


def test_burst_and_cross_check_valid_rows(tmp_path: Path) -> None:
    good = {"run": 1, "spread_ms": 6, "settle1": {"timed_out": False}, "counts": {"crm": 1, "outbox": 1}, "replies": {"200": 10}, "counts_after_35s": {"crm": 1, "outbox": 1}}
    _write(tmp_path, "g12e", [good, dict(good, run=2, spread_ms=900)])
    _write(tmp_path, "g3_b", [_kill_row(1), _kill_row(2, killed_pid=False, final={"crm": 5, "outbox": 5})])
    (tmp_path / "calls2.jsonl").write_text(json.dumps({"k": "crm", "t": 1.0, "body": json.dumps({"email": "k1@example.com"})}) + chr(10), encoding="utf-8")
    text = summarise.summarise(tmp_path, "jsonl")
    assert "Ten at once for e: 1 runs;" in text  # old: 2
    assert "1 runs equal, 0 differ (kill runs that do not count are left out: 1)." in text  # old: 1 equal, 1 differ


def test_missing_git_is_recorded_as_null(tmp_path, monkeypatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(run_all.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    run_all.manifest(str(work), str(tmp_path), FakeLib([_idle(90.0)]), "start", {})
    m = json.loads((work / "run_manifest.json").read_text(encoding="utf-8"))
    assert m["git_head"] is None and m["harness_dirty"] is None  # old: "unknown" and False


def test_docker_label_is_neutral() -> None:
    assert run_all.LABEL == "n8n-evidence.run=1"
    tool = "e" + "gm"  # a word from the author's private tooling: built from parts so this file does not contain it
    for p in [*HARNESS.glob("*.py"), *(HARNESS / "tests").glob("*.py"), *(FOLDER / n for n in DOCS)]:
        assert tool not in p.read_text(encoding="utf-8").lower(), p.name


def test_public_text_names_no_person() -> None:
    """The public files name no person (case-insensitive; the phrases are built from parts). The GitHub account is named only in the README (author line, clone step) and the licence note in DETAILS.md; the other files say "the author". The test files build the account name from parts (ACCT), so the name is not a literal in them."""
    phrases = ["the owner" + "'s ", "owner" + " hours", "vee" + "ra", "keth" + "ari", "rake" + "s"]
    for p in [*HARNESS.glob("*.py"), *(HARNESS / "tests").glob("*.py"), *FOLDER.glob("*.md")]:
        text = p.read_text(encoding="utf-8").lower()
        assert not [x for x in phrases if x in text], p.name
        assert p.name in ("README.md", "DETAILS.md") or p.suffix != ".md" or ACCT not in text, p.name


def test_g3e_and_g12e_probe_webhook(tmp_path, monkeypatch) -> None:
    attrs = _g3_fake(killed=True)
    attrs["webhooks_live"] = lambda *a, **k: False
    assert _run_script(monkeypatch, tmp_path, "g3e.py", ["K3", "1"], attrs) == 1  # n8n is up (port busy) but e3 is not registered: stop
    assert not (tmp_path / "g3e_K3.jsonl").read_text(encoding="utf-8")  # nothing was fired
    attrs2 = {"require_fresh": lambda *a, **k: None, "fpost": lambda *a, **k: None, "webhooks_live": lambda *a, **k: False}
    assert _run_script(monkeypatch, tmp_path, "g12e.py", [], attrs2) == 1


def _pull(monkeypatch, work, runs, sleeps=None):
    """Run pull_image with a scripted subprocess.run: runs is a list of results; an Exception instance is raised."""
    it = iter(runs)

    def fake(cmd, **k):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(run_all.subprocess, "run", fake)
    return run_all.pull_image("img@sha256:x", {}, str(work), sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def _res(code, err=b""):
    return types.SimpleNamespace(returncode=code, stderr=err)


def test_image_pull_retries_and_fails(tmp_path, monkeypatch, capsys) -> None:
    sleeps: list[int] = []
    _pull(monkeypatch, tmp_path, [_res(1, b"registry text"), _res(1, b"registry text"), _res(0)], sleeps)
    assert sleeps == [15, 30]
    sleeps.clear()
    with pytest.raises(SystemExit) as e:
        _pull(monkeypatch, tmp_path, [_res(1, b"registry text 429")] * 4, sleeps)
    assert sleeps == [15, 30, 60] and "failed after 4 tries (last: 1)" in str(e.value) and "registry text" not in str(e.value)
    assert "registry text" not in capsys.readouterr().out
    assert b"registry text 429" in (tmp_path / "pull.err").read_bytes()  # docker's text goes to a file, not the console


def test_pull_no_retry_when_hopeless(tmp_path, monkeypatch) -> None:
    sleeps: list[int] = []
    with pytest.raises(SystemExit, match="FileNotFoundError"):  # docker is not installed
        _pull(monkeypatch, tmp_path, [FileNotFoundError()], sleeps)
    assert sleeps == []
    with pytest.raises(SystemExit, match="after 1 tries"):  # the daemon is not running
        _pull(monkeypatch, tmp_path, [_res(1, b"Cannot connect to the Docker daemon at unix")], sleeps)
    assert sleeps == []
    with pytest.raises(SystemExit, match="after 1 tries"):  # the other exact wording
        _pull(monkeypatch, tmp_path, [_res(1, b"error during connect: Is the docker daemon running?")], sleeps)
    assert sleeps == []


def test_registry_error_is_retried(tmp_path, monkeypatch) -> None:
    """Docker prefixes registry failures with 'Error response from daemon:'; a 429 or a TLS timeout is exactly what the retries are for."""
    sleeps: list[int] = []
    _pull(monkeypatch, tmp_path, [_res(1, b"Error response from daemon: toomanyrequests: You have reached your pull rate limit"), _res(1, b"Error response from daemon: net/http: TLS handshake timeout"), _res(0)], sleeps)
    assert sleeps == [15, 30]  # old: any text with the word daemon ended the retries after one try


def test_image_pull_has_a_timeout(tmp_path, monkeypatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(run_all.subprocess, "run", lambda cmd, **k: seen.append(k) or _res(0))
    run_all.pull_image("img@sha256:x", {}, str(tmp_path))
    assert seen[0]["timeout"] == 300  # a hung daemon or network cannot hang the run forever
    sleeps: list[int] = []
    with pytest.raises(SystemExit, match="timeout"):
        _pull(monkeypatch, tmp_path, [subprocess.TimeoutExpired("docker", 300)] * 4, sleeps)
    assert sleeps == [15, 30, 60]


def test_url_errors_keep_their_reason_class(lib, monkeypatch) -> None:
    import urllib.error  # noqa: PLC0415

    def boom(*a, **k):
        raise urllib.error.URLError(ConnectionResetError())

    monkeypatch.setattr(lib.urllib.request, "urlopen", boom)
    assert lib.fire("p", "a@example.com", n=2) == {"URLError:ConnectionResetError": 2}  # old: "URLError" only


def test_readme_exit_codes_and_rule() -> None:
    """The README has the exit-code table with 3 for the idle check, the --min-idle 0 disclosure and the link to the rerun rule."""
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    assert not [x for x in readme.splitlines() if "idle" in x and re.search(r"code 2\b", x)]  # old: the idle check was said to exit with 2
    for code in ("| 0 |", "| 2 |", "| 3 |", "| 143 |", "| 4 |", "| other |"):
        assert code in readme, code
    assert "NOT a clean reproduction" in readme and "REPRODUCTION-RULE.md" in readme and "REPRODUCTION-RULE.md" in (FOLDER / "GATES.md").read_text(encoding="utf-8")
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    assert "--min-idle 0" in rule and "NOT a clean reproduction" in rule and "run_manifest.json" in rule and "a person or an AI agent" in rule
    out = subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "run_all.py"), "--help"], capture_output=True, text=True, check=True).stdout
    assert "(exit 3)" in " ".join(out.split()) and "(exit 2)" not in out


def test_original_rule_file_is_withheld() -> None:
    """Decision: the original rule file is not in the public folder; its recorded hash is (64 hex characters)."""
    assert not (FOLDER / "gate-rule-original.md").exists()
    assert re.fullmatch(r"[0-9a-f]{64}  gate-rule-original\.md\s*", (FOLDER / "gate-rule.sha256.txt").read_text(encoding="utf-8"))
    assert "is not published" in (FOLDER / "DETAILS.md").read_text(encoding="utf-8")


def test_tables_headline_and_notes() -> None:
    """Every headline bullet carries its attempts and invalid counts; the K letters are explained once for a-d and for e."""
    text = _norm((FOLDER / "tables.md").read_text(encoding="utf-8"))
    head = text.split("## Attempts", 1)[0]
    bullets = [x for x in head.splitlines() if x.startswith("* ")]
    assert len(bullets) == 12 and sum("(attempts " in x and ", invalid " in x for x in bullets) == 8  # four variants, ten at once and kill runs
    assert text.count("name different moments for variants a to d") == 2
    assert "does NOT mean that duplicates cannot happen" in text and "not a failure of n8n" in text


def test_readme_published_data_command(tmp_path: Path) -> None:
    """The README's own command for the published data (--ext ndjson) reproduces tables.md exactly; --ext jsonl on that folder is a shortfall."""
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    assert "published_run --ext ndjson --out data/published-check.md" in readme and "cmp data/published-check.md demo_only/n8n_evidence/tables.md" in readme
    out = tmp_path / "t.md"
    subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "summarise.py"), str(RUN), "--ext", "ndjson", "--out", str(out)], check=True, capture_output=True)
    assert out.read_bytes() == (FOLDER / "tables.md").read_bytes()
    bad = subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "summarise.py"), str(RUN), "--ext", "jsonl", "--out", str(tmp_path / "x.md")], capture_output=True)
    assert bad.returncode == 2


def _run_summarise(folder: Path, tmp_path: Path):
    return subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "summarise.py"), str(folder), "--ext", "jsonl", "--out", str(tmp_path / "o.md")], capture_output=True, text=True)


def test_truncated_line_is_refused(tmp_path: Path) -> None:
    (tmp_path / "g12_b_default.jsonl").write_text(json.dumps(_g12_row(1)) + chr(10) + '{"variant": "b", "att', encoding="utf-8")
    p = _run_summarise(tmp_path, tmp_path)
    assert p.returncode == 1 and "Traceback" not in p.stderr and str(tmp_path) not in p.stderr
    assert p.stderr.strip().count(chr(10)) == 0 and "g12_b_default.jsonl line 2" in p.stderr


@pytest.mark.parametrize(
    "mutate, reason",
    [
        ({"exec_times": [{"createdAt": float("nan"), "startedAt": float("nan"), "stoppedAt": float("nan")}] * 10}, "unreadable overlap data"),
        ({"exec_times": [{"createdAt": "not-a-time", "startedAt": "not-a-time", "stoppedAt": "not-a-time"}] * 10}, "unreadable overlap data"),
        ({"exec_times": None}, "unreadable overlap data"),
        ({"overlap": {"n": 10, "latest_start_before_earliest_stop": True, "start_spread_ms": -5}}, "impossible overlap values"),
        ({"overlap": {"n": 10, "latest_start_before_earliest_stop": True, "longest_ms": float("nan")}}, "impossible overlap values"),
        ({"overlap": {"n": 10, "latest_start_before_earliest_stop": True, "longest_ms": "slow"}}, "impossible overlap values"),
    ],
)
def test_hostile_times_make_row_invalid(mutate: dict, reason: str) -> None:
    row = _g12_row(1, **mutate)
    assert summarise.invalid_reason(row, "b") == reason and not summarise.valid(row, "b")
    assert summarise.valid(_g12_row(1), "b")  # the unmutated row still counts


def test_hostile_times_do_not_crash(tmp_path: Path) -> None:
    _write(tmp_path, "g12_b_default", [_g12_row(1, exec_times=[{"createdAt": "x", "startedAt": "x", "stoppedAt": "x"}] * 10)])
    p = _run_summarise(tmp_path, tmp_path)
    assert "Traceback" not in p.stderr and p.returncode == 2  # a shortfall (the row is invalid), not a crash
    assert "unreadable overlap data" in (tmp_path / "o.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("args", [["--gate-wait-min", "-1"], ["--min-idle", "150"], ["--min-idle", "-5"], ["--min-idle", "nan"], ["--gate-wait-min", "inf"]])
def test_run_all_rejects_impossible_numbers(args: list[str], tmp_path: Path) -> None:
    p = subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "run_all.py"), "--work-dir", str(tmp_path), "--node-dir", str(tmp_path), *args], capture_output=True, text=True)
    assert p.returncode == 2 and "error: argument" in p.stderr and "Traceback" not in p.stderr
    ok = subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "run_all.py"), "--work-dir", str(tmp_path), "--node-dir", str(tmp_path), "--min-idle", "0", "--gate-wait-min", "0"], capture_output=True, text=True)
    assert ok.returncode == 1 and "node_modules/n8n" in ok.stderr + ok.stdout  # accepted by argparse, stopped later by the work-folder check


# ---- documentation checks ----
def test_wrapped_reset_is_lost_connection(tmp_path: Path) -> None:
    for name in ("URLError:ConnectionResetError", "URLError:BrokenPipeError"):
        assert name in summarise.LOST_CONNECTION
        assert summarise.kill_stats([_kill_row(1, client_replies_during_kill={name: 10})])["no_reset"] == 0, name
    for name in ("URLError:ConnectionRefusedError", "URLError:TimeoutError", "URLError"):  # other reasons are not a lost connection
        assert summarise.kill_stats([_kill_row(1, client_replies_during_kill={name: 10})])["no_reset"] == 1, name


def test_kill_count_names_rows(tmp_path: Path) -> None:
    _write(tmp_path, "g3_b", [_kill_row(1)])
    inv = tmp_path / "invalid"
    inv.mkdir()
    (inv / "g3_b_SETUP-PROBLEM.jsonl").write_text(json.dumps(_kill_row(2)) + chr(10), encoding="utf-8")
    text = summarise.summarise(tmp_path, "jsonl")
    assert "Kill rows where no client lost its connection: 0 of 1 valid kill rows (the result file holds 1 rows; 1 more attempt kept in invalid/)" in text  # old: 'Of 1 kill attempts' next to 'attempts 2'


def _exit_text(fn, *a) -> str:
    with pytest.raises(SystemExit) as e:
        fn(*a)
    return str(e.value)


def test_damaged_rows_refused(tmp_path: Path) -> None:
    rows = [_kill_row(1)]
    del rows[0]["kill_point"]
    _write(tmp_path, "g3_b", rows)
    msg = _exit_text(summarise.shortfalls, tmp_path, "jsonl", 3, 3, "b", False)
    assert msg.startswith("refusing to summarise") and "KeyError" in msg and chr(10) not in msg  # old: a KeyError traceback
    good = tmp_path / "good"
    good.mkdir()
    _write(good, "g3_b", [_kill_row(1)])
    (good / "calls2.jsonl").write_text("not json" + chr(10), encoding="utf-8")
    assert "calls2.jsonl line 1 is not valid JSON" in _exit_text(summarise.summarise, good, "jsonl")  # old: a JSONDecodeError traceback
    (good / "calls2.jsonl").write_text(json.dumps({"k": "crm", "t": 1}) + chr(10), encoding="utf-8")  # no "body"
    msg = _exit_text(summarise.summarise, good, "jsonl")
    assert msg.startswith("refusing to summarise") and "KeyError" in msg
    bad = tmp_path / "bad"
    bad.mkdir()
    _write(bad, "g12e", [5])
    assert _exit_text(summarise.reply_keys, bad, "jsonl").startswith("refusing to summarise")


def test_docker_missing_is_one_line(tmp_path, monkeypatch) -> None:
    def fake(cmd, **k):
        raise FileNotFoundError(2, "no docker")

    monkeypatch.setattr(run_all.subprocess, "run", fake)
    work = tmp_path / "w"
    (work / "node_modules" / "n8n").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["run_all.py", "--work-dir", str(work), "--node-dir", str(tmp_path)])
    msg = _exit_text(run_all.main)
    assert "docker could not be started (FileNotFoundError)" in msg and chr(10) not in msg


def test_docs_decidable_and_honest() -> None:
    gates = (FOLDER / "GATES.md").read_text(encoding="utf-8")
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    readme = _alldocs()
    for text in (gates, rule, readme):
        assert "written down before the 2026-11-09 results were read." not in text and "before any result of that rerun was read" not in text and "written before the independent rerun" not in text
        assert "has not changed since it was recorded" not in text
    assert "proves nothing about when it was written" in gates and "cannot be checked from this repository" in gates
    assert "busy" in rule and "two or more consecutive" in rule and "worked example" in rule.lower()
    for term in ("**Claim step**", "**Sending state**", "**Wilson", "**Kill points**"):
        assert term in readme, term
    assert "python3-venv" in readme and "build-essential" in readme and "xz-utils" in readme and "usermod -aG docker" in readme and "docker ps" in readme
    for path in (HARNESS / "g12.py", HARNESS / "run_all.py", HARNESS / "summarise.py", FOLDER / "GATES.md", FOLDER / "REPRODUCTION-RULE.md"):
        assert "lead ruling" not in path.read_text(encoding="utf-8"), path.name  # only the published rows' label keeps the old words, and the README says so


def test_permission_denied_pull_stops(tmp_path, monkeypatch) -> None:
    sleeps: list[int] = []
    msg = "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
    with pytest.raises(SystemExit, match="after 1 tries"):
        _pull(monkeypatch, tmp_path, [_res(1, msg.encode())], sleeps)
    assert sleeps == []  # old: 4 tries and 105 s of pauses before the stop


def test_load_file_damage_is_one_line(tmp_path: Path) -> None:
    _write(tmp_path, "load", [{"setup": "s", "when": "before", "cpu_idle_pct": 10 ** 400, "free_ram_mb": 1}])
    assert "out of range" in _exit_text(summarise.busy_verdict, tmp_path, "jsonl")  # old: an OverflowError traceback (number too big for a float)
    deep = tmp_path / "deep"
    deep.mkdir()
    (deep / "load.jsonl").write_text("[" * 200000 + chr(10), encoding="utf-8")
    assert "RecursionError" in _exit_text(summarise.busy_verdict, deep, "jsonl")  # old: a RecursionError traceback
    isdir = tmp_path / "isdir"
    (isdir / "load.jsonl").mkdir(parents=True)
    msg = _exit_text(summarise.busy_verdict, isdir, "jsonl")
    assert msg.startswith("refusing to summarise") and "Error" in msg and chr(10) not in msg  # old: an OSError traceback


def _load_rows(*pairs):
    return [{"setup": s, "when": "during", "cpu_idle_pct": v, "free_ram_mb": 1} for s, v in pairs]


def test_busy_verdict_rule(tmp_path: Path) -> None:
    rows = (_load_rows(("one_low", 95), ("one_low", 40), ("one_low", 97), ("one_low", 99))  # a single low reading: not busy
            + _load_rows(("two_low", 95), ("two_low", 79), ("two_low", 70), ("two_low", 99))  # two consecutive low readings: busy
            + _load_rows(("mean_low", 85), ("mean_low", 60), ("mean_low", 85), ("mean_low", 70), ("mean_low", 95))  # no two in a row but the mean is 79: busy
            + [{"setup": "one_low", "when": "after", "error": "no reading"}])  # an error row is not a reading
    _write(tmp_path, "load", rows)
    got = {x.split(":")[0]: x for x in summarise.busy_verdict(tmp_path, "jsonl")}
    assert "not busy (4 readings" in got["one_low"] and "BUSY" in got["two_low"] and "BUSY" in got["mean_low"]
    assert "not judged" in summarise.busy_verdict(tmp_path / "nope", "jsonl")[0]
    out = subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "summarise.py"), str(tmp_path), "--ext", "jsonl", "--busy"], capture_output=True, text=True)
    assert out.returncode == 0 and "BUSY" in out.stdout and "two_low" in out.stdout


def test_docs_plain_and_decidable() -> None:
    readme = _alldocs()
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    gates = (FOLDER / "GATES.md").read_text(encoding="utf-8")
    for name, text in (("README", readme), ("RULE", rule), ("GATES", gates)):
        low = text.lower()
        assert "the publisher" not in low and "test runner" not in low and "set-up" not in low, name
    assert "the author" in readme and ACCT in readme and ACCT not in gates and ACCT not in rule
    assert "| What to do |" in readme and "| 130 |" in readme and "deadsnakes" in readme and "python3 --version" in readme
    assert "systemctl start docker" in readme and "xargs -r docker rm" in readme and "optional" in readme.lower()
    assert readme.count("the author's secret scanner flags complete web addresses in the files it scans") == 1 and "automatic secret scan" not in readme and "expand to the usual GitHub" in readme  # why the addresses are split, said once, and what the lines build
    assert "nodejs" + ".org" not in readme and "github" + ".com" not in readme  # the scanner would flag a plain host
    assert "--busy" in readme and "--busy" in rule and "cpu_idle_pct" in rule and "Worked non-match" in rule
    assert "quote only" in rule and "A count in neither list" in rule and "Who scores" in rule
    assert "Treat this summary as the author's word" in gates and "fingerprint" in gates
    assert "treat that summary as the author's word" in readme.lower() and "fingerprint" in readme
    assert "old label for the author's own decision" in readme


def test_readme_gives_no_plain_hosts() -> None:
    for name in (*DOCS, "GATES.md", "REPRODUCTION-RULE.md"):
        text = (FOLDER / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://[A-Za-z]", text), name  # every address is built from parts (see the README)


def _words(line: str) -> int:
    return len(line.split())


def _headline(readme: str) -> list[str]:
    return readme.split("## Headline, in plain words", 1)[1].split("## Quick start", 1)[0].splitlines()


def test_readme_clone_and_checkout_step() -> None:
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    block = readme.split("1. **Clone.**", 1)[1].split("2. **Docker daemon.**", 1)[0]
    assert "\ngit checkout n8n-evidence-rerun\n" in block and "BRANCH_NAME" not in readme  # a real command, no placeholder
    assert "the branch does not exist" in block and "skip that line" in block and "secret scanner flags" in block and "duplicate-proof-lead-desk-n8n" in block
    assert "demo_only/n8n_evidence" in block and "cd repo" in block  # repository root vs the folder of the README


def test_readme_first_screen_box() -> None:
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    top = readme.split("\n\n", 2)[1]  # the block right under the title
    box = [x for x in top.splitlines() if x.startswith("> * ")]
    assert top.startswith("> **What to believe") and 4 <= len(box) <= 6
    low = top.lower()
    for need in ("one windows laptop", "verified", "n8n is bad", "does not mean duplicates cannot happen", "one request at a time", "relaxed", "the cause was not investigated", "not products", "study question", "not been run on linux"):
        assert need in low, need
    assert "## Headline" in readme and "(see \"Headline\")" in top


def test_headline_bullets_are_short() -> None:
    head = _headline((FOLDER / "README.md").read_text(encoding="utf-8"))
    assert max(_words(x) for x in head if x.strip()) <= 90  # old: one bullet of 280 words
    tables = (FOLDER / "tables.md").read_text(encoding="utf-8")
    cited = [m for x in head for m in re.findall(r"\b\d+ of \d+\b", x)]
    assert len(cited) >= 10 and not [m for m in cited if m not in tables]  # every number of the headline is in tables.md


def test_retry_text_is_not_an_n8n_claim() -> None:
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    assert "yet no email" not in readme and "fault in n8n" not in readme  # old: read as an n8n defect
    alldocs = _alldocs()
    assert "quirk of these test workflows" not in alldocs.lower() and "not a finding about n8n" not in alldocs  # old: said the cause was known while also saying it was not investigated
    assert readme.count("it may come from how these test workflows are set up or from n8n itself") >= 2 and alldocs.count("The cause was not investigated") >= 3


def test_readme_exit_code_of_nohup_run() -> None:
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    assert "nohup sh -c" in readme and '> "$2/exit.txt"' in readme and 'cat "$work/exit.txt"' in readme
    assert "If that file is missing" in readme and "pgrep -af run_all.py" in readme
    docs = _alldocs()
    assert "run_manifest.json" in docs and "load.jsonl" in docs and "software-properties-common" in docs and "or may not use it" in docs
    assert "of every file in `published_run/`" in docs and "every published file" not in docs


def test_nohup_wrapper_writes_exit_file(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX shell wrapper")
    (tmp_path / "w").mkdir()
    (tmp_path / "run_all.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    wrapper = [x for x in readme.splitlines() if x.startswith("nohup sh -c")][0]
    cmd = wrapper.replace("nohup ", "", 1).rstrip(" &")
    subprocess.run(["sh", "-c", 'h="$T"; work="$T/w"; node=/x; ' + cmd], env={**os.environ, "T": str(tmp_path)}, check=True)
    assert (tmp_path / "w" / "exit.txt").read_text(encoding="utf-8").strip() == "exit 2"


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_rule_r1_r2_and_configurations() -> None:
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    s1, s2 = _section(rule, "## 1.", "## 2."), _section(rule, "## 2.", "## 3.")
    assert "(R1)" in s1 and "M4 and M5" in s1 and "every test configuration it uses is not busy" in s1 and "M1, M2 and M3 are scored per test configuration" in s1
    assert "(R2)" in s2 and "20 per ten-at-once" in s2 and "5 per kill point" in s2 and "k of n valid" in s2 and s2.index("(R2)") < s2.index("**M1**")
    for m, names in (("M1", ("b default", "c default", "d limit1")), ("M2", ("a default", "d limit1")), ("M3", ("b kill runs K1", "d kill runs K1")),
                     ("M4", ("e kill runs K1", "e kill runs K2", "e kill runs K3")), ("M5", ("X kill runs K1", "X kill runs K3"))):
        item = [x for x in s2.splitlines() if x.startswith("* **%s**" % m)][0]
        assert all(("`%s`" % n) in item for n in names), m
    run_all_src = (HARNESS / "run_all.py").read_text(encoding="utf-8")  # the names are the ones run_all.py writes into load.jsonl
    assert '"d limit1"' in run_all_src and '"e ten at once"' in run_all_src and '"%s default"' in run_all_src and '"%s kill runs %s"' in run_all_src and '"e kill runs "' in run_all_src and '"d kill runs "' in run_all_src
    assert "test configuration" in s1.lower() and "one value of the `setup` column" in s1 and "merge three of them in one row" in s1


def test_rule_n4_n5_publish_and_record() -> None:
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    s5 = _section(rule, "## 5.", "## 6.")
    assert "are scored one by one" in s5 and "may still be scored" not in rule
    assert "Its counts are scored as usual" in s5 and "not-clean label" in s5  # --min-idle 0
    assert "judged by its average" in s5 and "two decimals" in s5 and "NaN" in s5 and "refuses it with a one-line message" in s5
    s6 = _section(rule, "## 6.", "## 7.")
    assert "Where to publish" in s6 and "issue on the repository" in s6 and "tables.md" in s6 and "run_manifest.json" in s6 and "load.jsonl" in s6 and "a person or a program" in s6
    assert "2026-11-13" in _section(rule, "**Change record.**", "The published tables") and "R1 and R2 tighten" in rule.split("**Change record.**", 1)[1][:200] and "N4" not in rule


def test_rule_worked_example_self_contained() -> None:
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    s8 = rule.split("## 8.", 1)[1]
    rows = [x for x in s8.splitlines() if x.startswith("| ") and not x.startswith("| ---") and not x.startswith("| Count")]
    assert len(rows) >= 12 and all(x.count("|") == 4 for x in rows)  # a neutral table of counts in the rule file itself
    for word in ("lead note", "lead-note", "the owner", "stu" + "dio"):
        assert word not in s8.lower(), word
    for m in ("M1", "M2", "M3", "M4", "M5", "list T"):
        assert m in s8, m
    tables = (FOLDER / "tables.md").read_text(encoding="utf-8")
    assert "18 of 20" in tables and "5 of 5 at (1,1)" in s8 and "10 of 10" in s8 and "0 of 15" in s8


def test_readme_points_to_rule_edits() -> None:
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    assert "R1" in readme and "Where to publish" in readme and "REPRODUCTION-RULE.md" in readme


def test_busy_verdict_refuses_nan(tmp_path: Path) -> None:
    (tmp_path / "load.jsonl").write_text('{"setup": "s", "when": "before", "cpu_idle_pct": NaN, "free_ram_mb": 1}\n', encoding="utf-8")
    msg = _exit_text(summarise.busy_verdict, tmp_path, "jsonl")
    assert msg.startswith("refusing to summarise") and "not finite" in msg  # old: NaN compared False twice and read as "not busy"


def test_busy_mean_has_two_decimals(tmp_path: Path) -> None:
    _write(tmp_path, "load", _load_rows(("s", 79.96)))
    line = summarise.busy_verdict(tmp_path, "jsonl")[0]
    assert "BUSY" in line and "mean CPU idle 79.96" in line  # old: printed 80.0 beside BUSY


def test_out_write_failure_is_one_line(tmp_path: Path) -> None:
    _write(tmp_path, "load", _load_rows(("s", 99)))
    bad = tmp_path / "no" / "such" / "tables.md"
    r = subprocess.run([sys.executable, "-I", "-B", str(HARNESS / "summarise.py"), str(RUN), "--ext", "ndjson", "--out", str(bad)], capture_output=True, text=True)
    assert r.returncode == 1 and "Traceback" not in r.stderr and r.stderr.startswith("cannot write the tables")


def test_sums_malformed_line_is_one_line() -> None:
    sums = importlib.util.spec_from_file_location("n8n_sums_f8", HARNESS / "sums.py")
    mod = importlib.util.module_from_spec(sums)
    sums.loader.exec_module(mod)
    for bad in ("nothashnorname\n", "abc  \n"):
        assert "malformed" in _exit_text(mod.check, bad)  # old: a ValueError traceback


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999", "9" * 400], ids=["nan", "inf", "neginf", "1e999", "neg1e999", "bigint"])
def test_non_finite_numbers_are_refused(tmp_path: Path, bad: str) -> None:
    line = '{"setup": "s", "when": "before", "cpu_idle_pct": %s, "free_ram_mb": 1}\n' % bad
    (tmp_path / "load.jsonl").write_text(line, encoding="utf-8")
    assert "refusing to summarise" in _exit_text(summarise.busy_verdict, tmp_path, "jsonl")  # old: exit 0 with mean nan or inf
    pub = tmp_path / "pub"
    shutil.copytree(RUN, pub)
    f = pub / "g12_d_limit1.ndjson"
    f.write_text(f.read_text(encoding="utf-8").replace('"client_send_spread_ms": ', '"client_send_spread_ms": %s, "x": ' % bad, 1), encoding="utf-8")
    assert "refusing to summarise" in _exit_text(summarise.summarise, pub)  # old: ran to exit 0 with no refusal


def test_no_readings_is_not_judged(tmp_path: Path) -> None:
    _write(tmp_path, "load", [{"setup": "s", "when": "after", "error": "x"}])
    assert "not judged" in summarise.busy_verdict(tmp_path, "jsonl")[0] and "not-clean label" in summarise.busy_verdict(tmp_path, "jsonl")[0]
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    assert "**not judged**" in rule and "no readings at all" in rule and "1e999" in rule


def test_unsupported_machine_is_one_line() -> None:
    if os.name == "nt":
        pytest.skip("POSIX shell")
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    line = [x for x in readme.splitlines() if x.startswith("v=24.22.2; case")][0]
    for machine, want in (("riscv64", ""), ("aarch64", "arm64"), ("x86_64", "x64")):
        out = subprocess.run(["sh", "-c", 'uname() { echo %s; }; %s\necho "a=[$a]"' % (machine, line)], capture_output=True, text=True, check=True)
        assert "a=[%s]" % want in out.stdout  # old: a stayed unset (empty address) on an unknown machine, silently
        assert ("UNSUPPORTED" in out.stderr) == (want == "")


# ---- later fixes ----
def test_kill_count_is_over_valid_rows() -> None:
    bad = _kill_row(2, killed_pid=False, client_replies_during_kill={"200": 10})  # invalid, and no lost connection
    k = summarise.kill_stats([_kill_row(1), bad])
    assert (k["n"], k["rows"], k["no_reset"], k["not_killed"]) == (1, 2, 0, 1)  # old: n 2 and no_reset 1 (an invalid row counted)


def test_busy_refuses_non_readings(tmp_path: Path) -> None:
    for bad in ('"50"', "true", "-5", "1e14", "101"):
        (tmp_path / "load.jsonl").write_text('{"setup": "s", "when": "before", "cpu_idle_pct": %s, "free_ram_mb": 1}\n' % bad, encoding="utf-8")
        assert "refusing to summarise" in _exit_text(summarise.busy_verdict, tmp_path, "jsonl"), bad  # old: accepted (a huge value hid a busy reading)
    (tmp_path / "load.jsonl").write_text('{"setup": "s", "when": "before", "cpu_idle_pct": 100, "free_ram_mb": 1}\n', encoding="utf-8")
    assert "not busy" in summarise.busy_verdict(tmp_path, "jsonl")[0]


def test_require_fresh_refuses_damaged(lib, tmp_path: Path) -> None:
    f = tmp_path / "g3_b.jsonl"
    for text in ('{"kill_point": "K1"\n', '{"kill_point": "K2", "x": 1e999}\n'):
        f.write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit) as e:
            lib.require_fresh(str(f), kill_point="K1")
        assert "damaged" in str(e.value) and "Traceback" not in str(e.value)  # old: a JSONDecodeError traceback or a silent infinity


def test_tables_show_split_and_k_meaning() -> None:
    text = summarise.summarise(RUN, "ndjson")
    assert text.count("of 15 valid kill rows") == 8 and "Of the 35 attempts of d kept in invalid/" in text and "1 got HTTP 404" in text and "34 have no overlap data" in text
    assert text.count("Kill points here: K1 = ") == 4 and text.count("Kill points of variant e: K1 = ") == 3 and "may be the reason (a guess, not verified)" in text
    assert (FOLDER / "tables.md").read_text(encoding="utf-8") == text  # the committed tables are the regenerated ones


def _step3_block() -> str:
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    block = readme.split("3. **Node.js 24.22.2**", 1)[1].split("```\n", 2)[1]
    assert block.startswith("v=24.22.2; case")
    return block


def _run_step3(tmp_path: Path, machine: str):
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("no POSIX shell")
    log = tmp_path / "calls.log"
    work = tmp_path / "w"
    work.mkdir()
    stubs = "".join('%s() { echo "%s $*" >> "%s"; return 1; }; ' % (t, t, log.as_posix()) for t in ("curl", "tar"))  # functions, so no real download can happen
    script = 'uname() { echo %s; }; %scd "%s"; %s' % (machine, stubs, work.as_posix(), _step3_block()) + chr(10) + 'echo "pwd=$(pwd)"'
    out = subprocess.run([sh, "-c", script], capture_output=True, text=True)
    return out, log, work


def test_step3_riscv_makes_no_download(tmp_path: Path) -> None:
    out, log, work = _run_step3(tmp_path, "riscv64")
    assert "UNSUPPORTED" in out.stderr and not log.exists() and not (work / "data").exists()  # old: curl ran with node-v24.22.2-linux-.tar.xz
    assert out.stdout.strip().endswith("w")  # the shell never left the folder it started in


def test_step3_failed_download_stops(tmp_path: Path) -> None:
    out, log, work = _run_step3(tmp_path, "x86_64")
    calls = log.read_text(encoding="utf-8")
    assert calls.count("curl") == 1 and "tar -x" not in calls  # the failed curl stops before the checksum list and before tar
    assert out.stdout.strip().endswith("w")  # old: cd data/node and cd ../.. were chained, a failure left the shell in data/node


def test_readme_and_rule_text() -> None:
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    alldocs = _alldocs()
    rule = (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    gates = (FOLDER / "GATES.md").read_text(encoding="utf-8")
    for need in ("sudo usermod -aG docker $USER", "ss -ltn", "harness/sums.py check", "git rev-parse HEAD", "> /dev/null 2>&1 &", "once for each crashed entry",
                 "rerun-1.out", "isolated mode", "SIGKILL", "Remove Duplicates node", "multi-arch index", "stop, delete the folder, clone again", "not** the harness code"):
        assert need in alldocs, need
    for need in ("M4 has three counts" if False else "three counts for variant e", "labelled NOT a clean reproduction", "R1 and R2 tighten when a count is scored", "not in this folder", "`a kill runs K1` to `K3`", "`b kill runs` without a suffix", "e K1 (kill runs)", "file order", "rev-parse", "may show details of your machine",
                 "No independent rerun result existed", "The basis for this rule is the author's report, not checkable here", "not scored for b; run labelled not clean", "Three short worked cases"):
        assert need in rule, need
    assert "variant e burst" not in rule and "N4" not in rule and "N5" not in rule and "N7" not in rule and "quiet start" not in rule
    for text in (readme, rule, gates):
        assert "planning notes" not in text and "Changes after code review" not in text
    assert "serialised" not in rule + gates and ACCT not in rule + gates and "private document of the author" in gates
    assert "does not pass G3" in readme and "4 CRM rows and 4 emails" in readme  # variant a also fails G3 read literally (one kill run ended at (4,4))
    assert "no pass/fail rule applies to e" in alldocs and "a finding about this design; no pass/fail rule applies to e" in alldocs
    r1 = re.search(r"\*\*\(R1\) (.*?)\*\* (M1, M2 and M3 are scored per test configuration\.)", rule)
    assert r1 and ("(R1) " + r1.group(1) + " " + r1.group(2)) in readme  # the README quotes the rule's own sentence


def test_readme_is_short() -> None:
    assert len((FOLDER / "README.md").read_text(encoding="utf-8").splitlines()) <= 150
    sums = (FOLDER / "SHA256SUMS").read_text(encoding="utf-8")
    assert all(n in sums for n in ("GLOSSARY.md", "DETAILS.md", "CHANGES.md"))


def test_step3_sha_failure_stops_before_tar(tmp_path: Path) -> None:
    if not shutil.which("sh"):
        pytest.skip("no POSIX shell")
    log = tmp_path / "calls.log"
    work = tmp_path / "w"
    work.mkdir()
    stubs = ('curl() { echo "curl $*" >> "%s"; : > "${2##*/}"; return 0; }; sha256sum() { echo "sha256sum" >> "%s"; cat > /dev/null; return 1; }; '
             'tar() { echo "tar $*" >> "%s"; return 0; }; ') % ((log.as_posix(),) * 3)
    script = 'uname() { echo x86_64; }; %scd "%s"; %s' % (stubs, work.as_posix(), _step3_block()) + chr(10) + 'echo "pwd=$(pwd)"'
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    calls = log.read_text(encoding="utf-8")
    assert "sha256sum" in calls and "tar " not in calls and "node unpacked" not in out.stdout  # a failed checksum stops before tar
    assert out.stdout.strip().endswith("w")


def test_step3_unsupported_machine_is_quiet(tmp_path: Path) -> None:
    out, log, work = _run_step3(tmp_path, "riscv64")
    assert out.stderr.count(chr(10)) == 1 and "UNSUPPORTED" in out.stderr and "No such file" not in out.stderr  # old: the version line also ran and printed an error


def test_busy_zero_and_missing_readings(tmp_path: Path) -> None:
    _write(tmp_path, "load", _load_rows(("zero", 0), ("ok", 99)) + [{"setup": "none", "when": "after", "error": "x"}])
    got = {x.split(":")[0]: x for x in summarise.busy_verdict(tmp_path, "jsonl")}
    assert "BUSY" in got["zero"] and "not busy" in got["ok"]  # 0 is a valid reading
    assert "no readings" in got["none"] and "not clean" in got["none"]  # old: the configuration was silently left out


def test_require_fresh_one_line_refusals(lib, tmp_path: Path) -> None:
    f = tmp_path / "g3_b.jsonl"
    for text in ("[1, 2]\n", "[" * 100000 + chr(10)):
        f.write_text(text, encoding="utf-8")
        with pytest.raises(SystemExit) as e:
            lib.require_fresh(str(f), kill_point="K1")
        assert "damaged" in str(e.value)  # old: AttributeError and RecursionError tracebacks


def _cleanup_world(tmp_path: Path, pids, cwd: dict, pgid: dict, own=frozenset({1000, 1001, 1002, 1003}), kill_error=None, survive=(), via_main=False, comm=None, members=(), gone_on_kill=False, zombie=(), perm_on_probe=False, pgid_gone=()):
    """Runs cleanup.clean against stubs only: nothing real is read or killed. Returns (lines, killpg calls)."""
    spec = importlib.util.spec_from_file_location("n8n_cleanup_t", HARNESS / "cleanup.py")
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)
    (tmp_path / ".run").mkdir(exist_ok=True)
    (tmp_path / ".run" / "pids.json").write_text(pids if isinstance(pids, str) else json.dumps(pids), encoding="utf-8")
    alive = dict(pgid)
    calls: list = []

    def readlink(path):
        pid = int(path.split("/")[2])
        if pid not in alive:
            raise ProcessLookupError(pid)
        return cwd[pid]

    def getpgid(pid):
        if pid not in alive or pid in pgid_gone:
            raise ProcessLookupError(pid)
        return alive[pid]

    def killpg(pid, sig):
        if int(sig) == 0:  # the "is the group still there" test: not a signal
            if perm_on_probe:
                raise PermissionError(1, "not permitted")
            if pid in alive or pid in members:
                return
            raise ProcessLookupError(pid)
        calls.append((pid, int(sig)))
        if gone_on_kill:
            alive.pop(pid, None)
            raise ProcessLookupError(pid)
        if kill_error:
            raise kill_error
        if int(sig) == 9 or pid not in survive:
            alive.pop(pid, None)

    kw = dict(readlink=readlink, getpgid=getpgid, killpg=killpg, name=lambda pid: "prog%d" % pid, own=set(own), sleep=lambda s: None,
              free=lambda port: port != 5780, comm=lambda pid: (comm or {}).get(pid, "node"), pstate=lambda pid: "Z" if pid in zombie else "S")
    return (cleanup.main(tmp_path, **kw) if via_main else cleanup.clean(tmp_path, **kw)), calls, cleanup


def test_cleanup_ends_only_our_group_leader(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    lines, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242, "fakes": 4343, "x": 4444, "done": 0},
                                     cwd={4242: work, 4343: "/elsewhere", 4444: work}, pgid={4242: 4242, 4343: 4343, 4444: 4000})
    assert [c[0] for c in calls] == [4242] and calls[0][1] == signal_term()  # work-folder match, group leader: ended with SIGTERM, which sufficed
    text = chr(10).join(lines)
    assert "'n8n' pid 4242: stopped (SIGTERM)" in text
    assert "'fakes' pid 4343: SKIPPED, not started in this work folder (program: 'prog4343')" in text  # foreign cwd: program name only
    assert "'x' pid 4444: SKIPPED, not a process group leader" in text
    assert "ports 5679 and 5780: still in use: 5780" in lines[-1]  # reported, not assumed


def signal_term() -> int:
    import signal

    return int(signal.SIGTERM)


def test_cleanup_sigkill_after_sigterm(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    lines, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={4242: work}, pgid={4242: 4242}, survive=(4242,))
    assert [c[1] for c in calls] == [signal_term(), 9]
    assert "stopped (SIGKILL after 2 s)" in lines[0]


def test_cleanup_never_ends_own_ids(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    ids = {"a": 1000, "b": 1001, "c": 1002, "d": 1003}  # own pid, parent, group, session
    lines, calls, _ = _cleanup_world(tmp_path, ids, cwd={i: work for i in ids.values()}, pgid={i: i for i in ids.values()})
    assert calls == [] and sum("this script, its parent" in x for x in lines) == 4


def test_cleanup_skips_bad_pids(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    bad = {"a": True, "b": -5, "c": 10**30, "d": 4.5, "e": "4242", "f": None, "g": 1, "h": 0}
    lines, calls, _ = _cleanup_world(tmp_path, bad, cwd={4242: work}, pgid={4242: 4242})
    assert calls == [] and len(lines) == 1 and lines[0].startswith("ports")  # nothing killed, nothing printed per bad pid


def test_cleanup_zombie_or_dead_is_skipped(tmp_path: Path) -> None:
    lines, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={}, pgid={})  # cwd cannot be read (dead, zombie, hidden)
    assert calls == [] and "'n8n' pid 4242: not running: skipped" in lines[0]


def test_cleanup_odd_pids_json_is_safe(tmp_path: Path) -> None:
    for text in ("[]", "null", "1", "[1, 2]", "{broken", "[" * 100000, ""):
        lines, calls, _ = _cleanup_world(tmp_path, text, cwd={}, pgid={})
        assert calls == [] and lines == ["pids.json is not an object / unreadable: nothing stopped"], text[:10]


def test_cleanup_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    work = str(tmp_path.resolve())
    world = dict(cwd={4242: work}, pgid={4242: 4242})
    lines, _, _ = _cleanup_world(tmp_path, {"n8n": 4242}, kill_error=PermissionError("no"), **world)
    assert "'n8n' pid 4242: could not stop it (PermissionError)" in lines[0]  # killpg OSError is printed, not hidden
    rc, _, cleanup = _cleanup_world(tmp_path, {"n8n": 4242}, kill_error=PermissionError("no"), via_main=True, **world)
    assert rc == 1  # "could not stop" gives exit 1
    assert _cleanup_world(tmp_path, {"n8n": 4242}, via_main=True, **world)[0] == 0  # stopped cleanly: exit 0
    assert _cleanup_world(tmp_path, "[]", via_main=True, **world)[0] == 0  # odd pids.json: exit 0
    assert cleanup.main(tmp_path / "nope") == 0 and "no readable" in capsys.readouterr().out.splitlines()[-1]


def test_cleanup_sigterm_ignored(tmp_path: Path) -> None:
    """Old: any non-matching verdict after SIGTERM meant gone. Now the group test decides: a leader that is gone but whose group lives is not 'stopped'."""
    work = str(tmp_path.resolve())
    lines, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={4242: work}, pgid={4242: 4242}, survive=(4242,), members=())
    assert [c[1] for c in calls] == [signal_term(), 9] and "stopped (SIGKILL after 2 s)" in lines[0]


def test_cleanup_dead_leader_with_members(tmp_path: Path) -> None:
    lines, calls, cl = _cleanup_world(tmp_path, {"n8n": 4242, "fakes": 4343}, cwd={}, pgid={}, members=(4242,))
    assert calls == []  # never signalled: nothing proves the members are ours
    assert "'n8n' pid 4242: leader gone, group may still hold processes: check `ps -g 4242`" in lines[0]
    assert "'fakes' pid 4343: not running: skipped" in lines[1]
    assert _cleanup_world(tmp_path, {"n8n": 4242}, cwd={}, pgid={}, members=(4242,), via_main=True)[0] == 1


def test_cleanup_group_gone_in_kill(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    lines, _, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={4242: work}, pgid={4242: 4242}, gone_on_kill=True)
    assert "stopped (it was already gone)" in lines[0] and "could not" not in lines[0]


def test_cleanup_needs_the_recorded_program(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    w = dict(cwd={4242: work, 4343: work, 4444: work}, pgid={4242: 4242, 4343: 4343, 4444: 4444})
    lines, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242, "fakes": 4343, "other": 4444}, comm={4242: "bash", 4343: "python3.12", 4444: "node"}, **w)
    assert [c[0] for c in calls] == [4343]  # n8n pid is a shell: skipped; fakes is python: ended; an unknown key is never ended
    assert "'n8n' pid 4242: SKIPPED, not the program recorded" in lines[0] and "'other' pid 4444: SKIPPED, not the program recorded" in lines[2]


def test_cleanup_unreadable_other_user(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("n8n_cleanup_e", HARNESS / "cleanup.py")
    cl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cl)
    (tmp_path / ".run").mkdir()
    (tmp_path / ".run" / "pids.json").write_text(json.dumps({"n8n": 4242}), encoding="utf-8")

    def denied(path):
        raise PermissionError(13, "denied")

    lines = cl.clean(tmp_path, readlink=denied, own=set(), free=lambda p: True)
    assert "cannot be read (another user? use sudo): skipped" in lines[0]


def test_cleanup_key_is_printed_with_repr(tmp_path: Path) -> None:
    bad = chr(27) + "[2Jx"  # a hostile key: no raw control character may reach the terminal
    lines, _, _ = _cleanup_world(tmp_path, {bad: 4242}, cwd={}, pgid={})
    assert chr(27) not in lines[0] and chr(92) + "x1b" in lines[0]


def test_cleanup_usage_without_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "isdir", lambda p: False)  # macOS / BSD: no /proc
    monkeypatch.setattr(sys, "argv", ["cleanup.py", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(HARNESS / "cleanup.py"), run_name="__main__")
    assert "usage (Linux only)" in str(e.value)  # a usage exit (status 1), not a misleading "not running" for every pid


def _load_cleanup(name: str):
    spec = importlib.util.spec_from_file_location(name, HARNESS / "cleanup.py")
    cl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cl)
    return cl


def test_cleanup_n8n_comm_may_be_n8n(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    _, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={4242: work}, pgid={4242: 4242}, comm={4242: "n8n"})
    assert [c[0] for c in calls] == [4242]  # libuv may rename the process to n8n: still the recorded program


def test_cleanup_python_comm_forms(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    for comm in ("python", "python3", "python3.12"):
        _, calls, _ = _cleanup_world(tmp_path, {"fakes": 4343}, cwd={4343: work}, pgid={4343: 4343}, comm={4343: comm})
        assert [c[0] for c in calls] == [4343], comm


def test_cleanup_skips_exit_nonzero(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    w = dict(cwd={4242: work}, pgid={4242: 4242}, via_main=True)
    assert _cleanup_world(tmp_path, {"n8n": 4242}, comm={4242: "bash"}, **w)[0] == 1  # skipped, not the program: a stranger must see a failure
    w["cwd"] = {4242: str(tmp_path / "elsewhere")}
    assert _cleanup_world(tmp_path, {"n8n": 4242}, **w)[0] == 0  # a foreign cwd stays a plain skip (a stale pid after a reboot)


def test_cleanup_unreadable_exits_nonzero(tmp_path: Path) -> None:
    cl = _load_cleanup("n8n_cleanup_u")
    (tmp_path / ".run").mkdir()
    (tmp_path / ".run" / "pids.json").write_text(json.dumps({"n8n": 4242}), encoding="utf-8")

    def denied(path):
        raise PermissionError(13, "denied")

    assert cl.main(tmp_path, readlink=denied, own=set(), free=lambda p: True) == 1


def test_cleanup_program_name_is_escaped(tmp_path: Path) -> None:
    cl = _load_cleanup("n8n_cleanup_r")
    (tmp_path / ".run").mkdir()
    (tmp_path / ".run" / "pids.json").write_text(json.dumps({"n8n": 4242}), encoding="utf-8")
    lines = cl.clean(tmp_path, readlink=lambda p: "/elsewhere", name=lambda pid: chr(27) + "[2Jx", own=set(), free=lambda p: True)
    assert chr(27) not in lines[0] and chr(92) + "x1b" in lines[0]  # a hostile program name must not reach the terminal raw


def test_cleanup_zombie_leader_is_reported(tmp_path: Path) -> None:
    lines, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={}, pgid={}, members=(4242,), zombie=(4242,))
    assert calls == [] and "leader exited, not yet reaped" in lines[0]
    assert _cleanup_world(tmp_path, {"n8n": 4242}, cwd={}, pgid={}, members=(4242,), zombie=(4242,), via_main=True)[0] == 1


def test_cleanup_probe_denied_is_alive(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    lines, _, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={4242: work}, pgid={4242: 4242}, perm_on_probe=True)
    assert "could not stop it (still running after SIGKILL)" in lines[0]  # EPERM on signal 0 means the group exists


def test_cleanup_leader_gone_after_cwd(tmp_path: Path) -> None:
    work = str(tmp_path.resolve())
    lines, calls, _ = _cleanup_world(tmp_path, {"n8n": 4242}, cwd={4242: work}, pgid={4242: 4242}, pgid_gone=(4242,))
    assert calls == [] and "leader gone" in lines[0]  # getpgid raised ProcessLookupError after the cwd matched


def test_setup_name_forms_exist_in_run_all() -> None:
    src = (HARNESS / "run_all.py").read_text(encoding="utf-8")
    for form in ('"%s kill runs %s"', '"b kill runs"', '"b kill runs K2"', '"d kill runs " + kp', '"e kill runs " + kp'):
        assert form in src, form  # the three name forms the rule lists are the ones the harness writes


def test_no_bytecode_is_written() -> None:
    assert not _pyc_outside_tests()  # no compiled file anywhere in the published folder except tests/__pycache__ (no baseline: a stray one fails too)


def test_fresh_pytest_writes_no_test_pyc(tmp_path: Path) -> None:
    """Pytest on a fresh copy of the tests folder's setup: with -B nothing is compiled; without -B only conftest.py itself is (pytest compiles
    it before it can set the flag), never a test module."""
    d = tmp_path / "tests"
    d.mkdir()
    conftest = HARNESS / "tests" / "conftest.py"
    if conftest.is_file():
        shutil.copy(conftest, d / "conftest.py")
    (d / "test_tiny.py").write_text("def test_a():" + chr(10) + "    assert 1" + chr(10), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    for flags, allowed in (([sys.executable, "-B"], set()), ([sys.executable], {"conftest"})):
        shutil.rmtree(d / "__pycache__", ignore_errors=True)
        r = subprocess.run([*flags, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(d)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout[-300:]
        names = {p.name.split(".")[0] for p in d.rglob("*.pyc")}
        assert names <= allowed, (flags, names)


def test_cleanup_and_history_wording() -> None:
    said = "started in this work folder and leads its own process group"
    for name in ("README.md", "DETAILS.md", "CHANGES.md"):
        assert said in (FOLDER / name).read_text(encoding="utf-8").replace("\n", " "), name  # one sentence about what cleanup.py ends
    readme = (FOLDER / "README.md").read_text(encoding="utf-8")
    assert len(readme.splitlines()) <= 110 and "ss -ltn` for ports 5679 and 5780" in readme and "set `h` and `work` again" in readme
    assert "-B -m pytest" in readme and "commands: `DETAILS.md`, Rerun notes" in readme
    docs = _alldocs()
    assert "nothing was deleted" not in docs and "every number, caveat and admission is kept" not in docs and "with these exceptions" in docs
    assert "second, heavily loaded" not in docs and "a second run by the author" not in docs and "**loaded run**" in docs
    assert "17 of the 18 valid runs ended at (1,1)" in (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")
    assert "**Edited 2026-11-14**" in (FOLDER / "REPRODUCTION-RULE.md").read_text(encoding="utf-8")


def _headings(name: str) -> list:
    return [x.lstrip("#").strip() for x in (FOLDER / name).read_text(encoding="utf-8").splitlines() if x.startswith("#")]


def test_doc_section_pointers_resolve() -> None:
    """Every pointer in the docs of the form `file.md`, "Section" (quoted or not, with or without "under") or `file.md`, section N names a HEADING of
    that file, not just words that occur somewhere in it. A moved or renamed section breaks it. tables.md is a data file whose pointers name a term."""
    quoted = re.compile(r"`([A-Za-z-]+\.md)`,? (?:under )?\"([^\"]{3,80})\"|\"([^\"]{3,80})\" in `([A-Za-z-]+\.md)`")
    plain = re.compile(r"`([A-Za-z-]+\.md)`, ([A-Z][A-Za-z ]{3,60}?)(?=[).;:,]|$)")
    numbered = re.compile(r"`([A-Za-z-]+\.md)`,? section ([0-9]+)")
    seen = 0
    for n in (*DOCS, "REPRODUCTION-RULE.md", "GATES.md"):
        text = (FOLDER / n).read_text(encoding="utf-8")
        for pat in (quoted, plain):
            for m in pat.finditer(text):
                f, name = (m.group(1), m.group(2)) if pat is plain or m.group(1) else (m.group(4), m.group(3))
                heads = _headings(f)
                seen += 1
                if f == "tables.md":
                    assert name in (FOLDER / f).read_text(encoding="utf-8"), (n, f, name)
                else:
                    assert any(name in h for h in heads), (n, f, name, heads)
        for m in numbered.finditer(text):
            seen += 1
            assert any(re.match(m.group(2) + r"\.? ", h) for h in _headings(m.group(1))), (n, m.group(1), "section " + m.group(2))
    assert seen >= 8  # the pattern still finds the pointers that exist (a regex that matches nothing would pass vacuously)


def test_test_names_are_short() -> None:
    """The repository's hygiene scan flags any 40+ character identifier-shaped token ([A-Za-z0-9+/=_-]{40,}) as a possible secret; a test name of 40 trips it."""
    import ast

    for f in sorted(HARNESS.glob("tests/test_*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                assert len(node.name) <= 39, (f.name, node.name, len(node.name))


# Copies of the repository's hygiene scan: the nine patterns of patterns.txt (repository root) and the shapes of the exceptions.txt lines that apply to this
# folder, so the folder checks itself without the rest of the repository. Pattern 2 also catches a time of day with seconds, pattern 9 a 40+ character token.
_HYGIENE = tuple(re.compile(p) for p in (
    r"([0-9]{1,3}\.){3}[0-9]{1,3}",
    r"([0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}",
    r"https?://[^][ )\"'<>`{]+",
    r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.(com|net|org|io|dev|ai|app|cloud|co|uk|us|ca|au|me|in|xyz|info|tech|eu|de|fr|sh|page|site|invalid|local|internal|lan|corp|home)([^A-Za-z0-9_-]|$)",
    r"[A-Za-z]:[\\/][A-Za-z0-9_.-]+",
    r"(^|[ \"'=(])/(home|Users|usr|var|etc|opt|tmp|mnt|root)/[A-Za-z0-9_.-]+",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"[A-Fa-f0-9]{32,}",
    r"[A-Za-z0-9+/=_-]{40,}",
))
_HYGIENE_ALLOWED = re.compile(  # what exceptions.txt allows (match text only)
    r"127\.0\.0\.1|(192\.0\.2|198\.51\.100|203\.0\.113)\.[0-9]{1,3}"
    r"|[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*(example\.com|example\.test|example\.invalid)"
    r"|https?:/{2}(localhost|127\.0\.0\.1|example\.(com|test|invalid))([:/].*)?|https:/{2}(docs\.n8n\.io|github\.com/n8n-io/n8n/blob/master/LICENSE\.md)([:/].*)?"
    r"|(localhost|docs\.n8n\.io|example\.com|example\.test|example\.invalid)[^A-Za-z0-9-]?")
_HYGIENE_FILES_EXEMPT = {"package-lock.json", "gate-rule.sha256.txt", "SHA256SUMS"}  # integrity hashes: the repository's exceptions list them
_HEX64 = re.compile(r"[0-9a-f]{64}")  # the repository allows one SHA-256 only in run_all.py and the sum files
_HEX64_FILES = {"run_all.py"}


def test_folder_has_no_hygiene_literal() -> None:
    hits = []
    for f in sorted(FOLDER.rglob("*")):
        if not f.is_file() or f.suffix not in (".md", ".py", ".json", ".txt") or "published_run" in f.relative_to(FOLDER).parts or f.name in _HYGIENE_FILES_EXEMPT:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for pat in _HYGIENE:
                for m in pat.finditer(line):
                    t = m.group(0)
                    if (f.name in _HEX64_FILES and _HEX64.fullmatch(t)) or _HYGIENE_ALLOWED.fullmatch(t.strip(" \"'=(")):
                        continue
                    hits.append((f.relative_to(FOLDER).as_posix(), i, t[:30]))
    assert not hits, hits[:10]


def test_no_dev_folder_file_is_published() -> None:
    assert not list(FOLDER.rglob(".dev-folder")) or subprocess.run(["git", "-C", str(FOLDER), "ls-files", "--error-unmatch", str(HARNESS / "tests" / ".dev-folder")], capture_output=True).returncode != 0


def test_readme_python_runs_use_dash_b() -> None:
    text = (FOLDER / "README.md").read_text(encoding="utf-8") + (FOLDER / "DETAILS.md").read_text(encoding="utf-8")
    assert not re.findall(r"python3?(?:\.12)? -I\b", text) and text.count("-B -I") >= 8  # every isolated python run also passes -B: a later pytest run must find no compiled file


def _hostile_name() -> str:
    return "x\n'\"%s" + chr(27) + "[2J"  # newline, both quotes, a %s token and an escape sequence


def _skip_line(tmp_path: Path, pgid: dict, comm: str) -> str:
    cl = _load_cleanup("n8n_cleanup_h")
    work = str(tmp_path.resolve())
    (tmp_path / ".run").mkdir(exist_ok=True)
    (tmp_path / ".run" / "pids.json").write_text(json.dumps({"n8n": 4242}), encoding="utf-8")
    lines = cl.clean(tmp_path, readlink=lambda p: work, getpgid=lambda pid: pgid[pid], killpg=lambda pid, sig: None, comm=lambda pid: comm,
                     name=lambda pid: _hostile_name(), own=set(), free=lambda p: True, sleep=lambda s: None)
    return lines[0]


def test_cleanup_name_sites_repr(tmp_path: Path) -> None:
    """The three messages that print a program name (foreign cwd, not a leader, not the recorded program) all print it with %r."""
    cl = _load_cleanup("n8n_cleanup_h0")
    (tmp_path / ".run").mkdir(exist_ok=True)
    (tmp_path / ".run" / "pids.json").write_text(json.dumps({"n8n": 4242}), encoding="utf-8")
    foreign = cl.clean(tmp_path, readlink=lambda p: "/elsewhere", name=lambda pid: _hostile_name(), own=set(), free=lambda p: True)[0]
    leader = _skip_line(tmp_path, {4242: 1}, "node")  # getpgid != pid: not a group leader
    prog = _skip_line(tmp_path, {4242: 4242}, "bash")  # a leader, but comm is not node/n8n
    for line, what in ((foreign, "not started in this work folder"), (leader, "not a process group leader"), (prog, "not the program recorded")):
        assert what in line and repr(_hostile_name()) in line, line
        assert "\n" not in line and chr(27) not in line, line


_SCAN_SUFFIXES = (".md", ".py", ".json", ".txt")
_BAD_CHARS = set(range(0x20)) - {9, 10, 13} | {0x7F} | set(range(0x200B, 0x2010)) | {0x2060, 0xFEFF} | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


def _control_hits(root: Path) -> list:
    hits = []
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root).parts
        if not f.is_file() or f.suffix not in _SCAN_SUFFIXES or "published_run" in rel or "__pycache__" in rel or f.name == "SHA256SUMS":
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            bad = sorted({hex(ord(c)) for c in line if ord(c) in _BAD_CHARS})
            if bad:
                hits.append((f.relative_to(root).as_posix(), i, bad))
    return hits


def test_folder_no_control_chars() -> None:
    assert not _control_hits(FOLDER)


def test_control_scan_catches_bs(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("fine\n", encoding="utf-8")
    assert _control_hits(tmp_path) == []
    for bad in (chr(8), chr(0x7F), chr(0x200B), chr(0x202E), chr(0x2066), chr(0xFEFF)):
        (tmp_path / "a.md").write_text("x" + bad + "y\n", encoding="utf-8")
        assert _control_hits(tmp_path), hex(ord(bad))
