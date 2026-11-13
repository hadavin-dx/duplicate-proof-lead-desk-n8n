"""The reset scripts delete the lead book, so they refuse a database path that is not inside the repository's data
folder. Made-up data only; each test creates only the files it names."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from support import REPO, wait_until

WINDOWS = sys.platform == "win32"


def _run(
    script: str,
    db: Path,
    *args: str,
    first_on_path: Path | None = None,
    shell: str = "bash",
    code: str | None = None,
    **extra: str,
) -> subprocess.CompletedProcess[str]:
    """first_on_path: a folder (holding a stand-in `python`) searched before this interpreter's folder; the
    desk's own DESK_LEADFLOW_FILE and DESK_NUDGE_TO of the calling shell are then left out of the environment.
    code: shell text run with `shell -c` instead of the script file (not on Windows)."""
    env = {**os.environ, "DESK_DB_PATH": str(db), **extra}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if first_on_path is not None:
        env["PATH"] = str(first_on_path) + os.pathsep + env["PATH"]
        for name in ("DESK_LEADFLOW_FILE", "DESK_NUDGE_TO"):
            if name not in extra:
                env.pop(name, None)
    if WINDOWS:
        powershell = shutil.which("powershell")
        if powershell is None:
            pytest.skip("Windows PowerShell is not installed")
        cmd = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO / "scripts" / script)]
    else:
        cmd = [shell, "-c", code] if code is not None else [shell, str(REPO / "scripts" / script), *args]
    return subprocess.run(  # noqa: S603 (our own script and a path made by the test)
        cmd, cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False
    )


SCRIPT = "reset.ps1" if WINDOWS else "reset.sh"


def test_reset_refuses_a_database_outside_the_data_folder(tmp_path: Path) -> None:
    db = tmp_path / "desk.sqlite3"
    log = tmp_path / "desk.log"
    for f in (db, log):
        f.write_text("keep me", encoding="utf-8")
    done = _run(SCRIPT, db)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "refused" in done.stdout + done.stderr
    assert db.read_text(encoding="utf-8") == "keep me" and log.read_text(encoding="utf-8") == "keep me"


def test_reset_still_deletes_a_database_inside_the_data_folder() -> None:
    (REPO / "data").mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="reset-test-", dir=REPO / "data"))  # a fresh folder: only it is removed
    db = folder / "desk.sqlite3"
    db.write_text("x", encoding="utf-8")
    try:
        done = _run(SCRIPT, db, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port()))
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert not db.exists()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# -- the reset never deletes the lead book while the desk may still be using it
@pytest.fixture
def book() -> Any:
    (REPO / "data").mkdir(exist_ok=True)
    folder = Path(
        tempfile.mkdtemp(prefix="reset-guard-", dir=REPO / "data")
    )  # a fresh folder: nothing else is ever removed
    db = folder / "desk.sqlite3"
    db.write_text("keep me", encoding="utf-8")
    (folder / "desk.log").write_text("keep me", encoding="utf-8")
    yield db
    shutil.rmtree(folder, ignore_errors=True)


def _kept(db: Path) -> bool:
    return db.read_text(encoding="utf-8") == "keep me" and (db.parent / "desk.log").exists()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_reset_refuses_when_something_answers_on_the_desk_port(book: Path) -> None:
    """A desk started by hand has no pid file: the port that still answers stops the reset."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        done = _run(SCRIPT, book, DESK_PORT=str(listener.getsockname()[1]), DESK_FAKES_PORT=str(_free_port()))
    assert done.returncode == 4 and "answers on port" in done.stdout + done.stderr, (done.stdout, done.stderr)
    assert _kept(book)


def test_reset_refuses_when_a_process_holds_the_database(book: Path) -> None:
    from lead_desk import lockfile

    handle = lockfile.acquire(book)
    try:
        done = _run(SCRIPT, book, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port()))
    finally:
        handle.close()
    assert done.returncode == 3 and "holds the database" in done.stdout + done.stderr, (done.stdout, done.stderr)
    assert _kept(book)


def test_reset_refuses_a_pid_file_of_another_program(book: Path) -> None:
    pidfile = book.parent / "desk.pid"
    pidfile.write_text(str(os.getpid()), encoding="utf-8")  # this test process: not the desk
    done = _run(SCRIPT, book, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port()))
    assert done.returncode == 6 and "not the desk" in done.stdout + done.stderr, (done.stdout, done.stderr)
    assert _kept(book) and pidfile.exists()


@pytest.mark.skipif(WINDOWS, reason="Windows ends a process at once; the slow stop is a POSIX case")
def test_reset_sh_refuses_when_the_desk_does_not_stop(book: Path) -> None:
    ready = book.parent / "ready"
    code = (
        "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[2], 'w').close(); time.sleep(60)"
    )
    slow = subprocess.Popen([sys.executable, "-c", code, "lead_desk.server", str(ready)])  # noqa: S603 (our own interpreter)
    try:
        wait_until(ready.exists, what="the child has installed its signal handler")
        (book.parent / "desk.pid").write_text(str(slow.pid), encoding="utf-8")
        done = _run(
            SCRIPT, book, RESET_STOP_WAIT_SECONDS="1", DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port())
        )
    finally:
        slow.kill()
        slow.wait()
    assert done.returncode == 5 and "did not stop" in done.stdout + done.stderr, (done.stdout, done.stderr)
    assert _kept(book)


@pytest.mark.skipif(WINDOWS, reason="Windows ends a process at once; the slow stop is a POSIX case")
def test_reset_sh_waits_for_a_desk_that_needs_a_few_seconds_to_stop(book: Path) -> None:
    """A desk that takes about three seconds to finish after the stop signal: the reset waits for it, and only then
    deletes. (A reset that checked once and went on would delete under a live desk or give up too early.)"""
    code = (
        "import signal, sys, time\n"
        "def stop(*a):\n"
        "    time.sleep(3)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "open(sys.argv[2], 'w').close()\n"
        "time.sleep(60)\n"
    )
    ready = book.parent / "ready"
    slow = subprocess.Popen([sys.executable, "-c", code, "lead_desk.server", str(ready)])  # noqa: S603 (our own interpreter)
    try:
        wait_until(ready.exists, what="the child has installed its signal handler")
        (book.parent / "desk.pid").write_text(str(slow.pid), encoding="utf-8")
        done = _run(SCRIPT, book, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port()))
        alive_after = slow.poll() is None
    finally:
        slow.kill()
        slow.wait()
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert not alive_after and not book.exists() and not (book.parent / "desk.pid").exists()


@pytest.mark.skipif(WINDOWS, reason="reset.sh is the non-Windows script")
@pytest.mark.parametrize("name", ["desk.pid", "fakes.pid"])
def test_reset_sh_deletes_an_empty_pid_file(book: Path, name: str) -> None:
    """An empty pid file names no process: it is removed and the reset goes on (the check is -e, not -s)."""
    pidfile = book.parent / name
    pidfile.write_text("", encoding="utf-8")
    done = _run(SCRIPT, book, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port()))
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert not pidfile.exists() and not book.exists()


@pytest.mark.skipif(WINDOWS, reason="a process that ended but is not collected yet is a POSIX state")
def test_reset_sh_treats_a_pid_file_of_an_ended_but_uncollected_process_as_gone(book: Path) -> None:
    ended = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 (our own interpreter)

    def uncollected() -> bool:  # not waited for: the process stays a zombie of this test until it is collected
        out = subprocess.run(  # noqa: S603 (ps with a number made here)
            ["ps", "-p", str(ended.pid), "-o", "stat="],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return out.strip().startswith("Z")

    try:
        wait_until(uncollected, what="the child ended and was not collected")
        (book.parent / "desk.pid").write_text(str(ended.pid), encoding="utf-8")
        done = _run(SCRIPT, book, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port()))
    finally:
        ended.wait()
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert not book.exists() and not (book.parent / "desk.pid").exists()


@pytest.mark.parametrize(
    "value", ["1+1", "$(touch pwned)", "-3", "1.5", " ", "a", "abc", "08", "09", "0001", "1234", "99999999999999999999"]
)
def test_reset_refuses_a_stop_wait_that_is_not_a_plain_whole_number(book: Path, value: str) -> None:
    """Both scripts: digits only, no leading zero (a shell reads 08 as a bad octal), at most three digits; the answer
    is exit 2 before a process is stopped or a file is deleted."""
    ready = book.parent / "ready"
    code = "import sys, time; open(sys.argv[2], 'w').close(); time.sleep(60)"
    desk = subprocess.Popen([sys.executable, "-c", code, "lead_desk.server", str(ready)])  # noqa: S603 (our own interpreter)
    try:
        wait_until(ready.exists, what="the child is running")
        (book.parent / "desk.pid").write_text(str(desk.pid), encoding="utf-8")
        ports = {"DESK_PORT": str(_free_port()), "DESK_FAKES_PORT": str(_free_port())}
        done = _run(SCRIPT, book, RESET_STOP_WAIT_SECONDS=value, **ports)
        still_running = desk.poll() is None
    finally:
        desk.kill()
        desk.wait()
    assert done.returncode == 2 and "RESET_STOP_WAIT_SECONDS" in done.stdout + done.stderr, (done.stdout, done.stderr)
    assert still_running and (book.parent / "desk.pid").exists()  # nothing was stopped
    assert _kept(book) and not (REPO / "pwned").exists()


@pytest.mark.parametrize("value", ["0", "1", "10", "999"])
def test_reset_accepts_a_plain_whole_number_of_seconds(book: Path, value: str) -> None:
    done = _run(
        SCRIPT, book, RESET_STOP_WAIT_SECONDS=value, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port())
    )
    assert done.returncode == 0 and not book.exists(), (done.stdout, done.stderr)


def test_reset_check_answers_one_line_when_the_lock_file_cannot_be_looked_at(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], book: Path
) -> None:
    import importlib.util

    from lead_desk import lockfile

    spec = importlib.util.spec_from_file_location("reset_check_under_test", REPO / "scripts" / "reset_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Unreadable:
        def exists(self) -> bool:
            raise PermissionError("no access")

    monkeypatch.setenv("DESK_DB_PATH", str(book))
    monkeypatch.setattr(lockfile, "lock_path", lambda _db: Unreadable())
    assert module.main() == 3
    out = capsys.readouterr().out.strip()
    assert out.startswith("refused:") and len(out.splitlines()) == 1 and "no access" not in out and _kept(book)


def test_reset_refuses_when_the_lock_file_cannot_be_tested_and_creates_none_on_a_refusal(book: Path) -> None:
    lock = Path(f"{book.resolve()}.lock")
    lock.mkdir()  # a lock file that cannot be opened: nothing says that no desk holds the book
    try:
        done = _run(SCRIPT, book, DESK_PORT=str(_free_port()), DESK_FAKES_PORT=str(_free_port()))
    finally:
        lock.rmdir()
    assert done.returncode == 3 and "cannot be tested" in done.stdout + done.stderr, (done.stdout, done.stderr)
    assert _kept(book)
    with socket.socket() as listener:  # a refusal for another reason leaves no lock file behind either
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        again = _run(SCRIPT, book, DESK_PORT=str(listener.getsockname()[1]), DESK_FAKES_PORT=str(_free_port()))
    assert again.returncode == 4 and not lock.exists(), (again.stdout, again.stderr)


# A stand-in `python` for scripts/start_all.sh: it answers the three `python -c` helper calls (data folder, demo-server
# port, desk port) with fixed values, writes one line per `python -m <module>` launch with the DESK_LEADFLOW_FILE and
# DESK_NUDGE_TO it saw, and records every call, so the test sees what the shell [[ ]] tests decided without
# starting a server.
_STUB_PYTHON = """#!/usr/bin/env bash
echo "$*" >> "${STUB_DATA}/calls.log"
if [[ "$1" == "-c" ]]; then
  case "$2" in
    *parent*) echo "${STUB_DATA}";;
    *fakes_port*) echo 1234;;
    *.port*) echo 4321;;
  esac
elif [[ "$1" == "-m" ]]; then
  echo "LEADFLOW=[${DESK_LEADFLOW_FILE-<unset>}] NUDGE=[${DESK_NUDGE_TO-<unset>}]" > "${STUB_DATA}/launch-$2.rec"
fi
"""


def _start_all(
    tmp_path: Path, *args: str, pid_file: str = "", **env: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run scripts/start_all.sh with the stub python first on PATH; returns the result and the stub's data folder."""
    data = tmp_path / "data"
    data.mkdir()
    if pid_file:
        (data / pid_file).write_text("1\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python"
    stub.write_text(_STUB_PYTHON, encoding="utf-8")
    stub.chmod(0o755)
    done = _run("start_all.sh", tmp_path / "unused.sqlite3", *args, first_on_path=bin_dir, STUB_DATA=str(data), **env)
    return done, data


def _launch(data: Path) -> str:
    """The env line the stub saw for the desk launch (a background process: wait, bounded, until it has written)."""
    rec = data / "launch-lead_desk.server.rec"
    wait_until(rec.exists, timeout=10, what="the stub python launched as the desk")
    return rec.read_text(encoding="utf-8").strip()


@pytest.mark.skipif(WINDOWS, reason="start_all.sh is the non-Windows script")
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "LEADFLOW=[demo_only/leadflow.json] NUDGE=[reminder@example.test]"),  # default: on
        ({"DESK_LEADFLOW_FILE": "off"}, "LEADFLOW=[off] NUDGE=[<unset>]"),  # script line 12: != off
        ({"DESK_LEADFLOW_FILE": ""}, "LEADFLOW=[] NUDGE=[<unset>]"),  # script line 12: -n
        ({"DESK_LEADFLOW_FILE": "x.json"}, "LEADFLOW=[x.json] NUDGE=[reminder@example.test]"),
        ({"DESK_LEADFLOW_FILE": "of"}, "LEADFLOW=[of] NUDGE=[reminder@example.test]"),  # a near miss of "off" is a file
        ({"DESK_LEADFLOW_FILE": "*"}, "LEADFLOW=[*] NUDGE=[reminder@example.test]"),  # no pattern matching in [[ ]] rhs
        (
            {"DESK_LEADFLOW_FILE": "x.json", "DESK_NUDGE_TO": "me@example.test"},
            "LEADFLOW=[x.json] NUDGE=[me@example.test]",
        ),
        (
            {"DESK_LEADFLOW_FILE": "x.json", "DESK_NUDGE_TO": ""},
            "LEADFLOW=[x.json] NUDGE=[]",
        ),  # a set empty value is kept
    ],
)
def test_start_all_sh_turns_the_reminder_on_only_for_a_real_leadflow_file(
    tmp_path: Path, env: dict[str, str], expected: str
) -> None:
    """Runs the real scripts/start_all.sh (the [[ ]] test of its leadflow line, which SonarCloud shell:S7688 made
    us rewrite) with a stub `python`, and reads the environment the launched desk process would get."""
    done, data = _start_all(tmp_path, **env)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert _launch(data) == expected


@pytest.mark.skipif(WINDOWS, reason="start_all.sh is the non-Windows script")
@pytest.mark.parametrize("pid_file", ["fakes.pid", "desk.pid"])
def test_start_all_sh_refuses_when_either_pid_file_exists_and_starts_nothing(tmp_path: Path, pid_file: str) -> None:
    """The pid-file check is the other rewritten [[ ]] test (two tests joined by ||): either file alone refuses."""
    done, data = _start_all(tmp_path, pid_file=pid_file)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "pid files exist" in done.stderr
    assert not list(data.glob("launch-*.rec")), "a server was launched although a pid file existed"
    assert sorted(p.name for p in data.glob("*.pid")) == [pid_file]  # and the script wrote no pid file of its own


@pytest.mark.skipif(WINDOWS, reason="start_all.sh is the non-Windows script")
def test_start_all_sh_refuses_an_unknown_mode_before_it_calls_python(tmp_path: Path) -> None:
    """The mode check is a `case`, not a changed [[ ]] line; this test only guards that the early exit stays first."""
    done, data = _start_all(tmp_path, "sideways")
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "mode must be keyed or blind" in done.stderr
    assert not (data / "calls.log").exists(), "python was called before the mode was checked"


@pytest.mark.skipif(WINDOWS or shutil.which("dash") is None, reason="needs dash (the sh of Debian and Ubuntu)")
@pytest.mark.parametrize("bash_version", [None, "5.2.0"], ids=["BASH_VERSION-unset", "BASH_VERSION-exported"])
@pytest.mark.parametrize("script", ["reset.sh", "start_all.sh"])
def test_the_shell_scripts_refuse_to_run_under_dash_and_delete_or_start_nothing(
    tmp_path: Path, script: str, bash_version: str | None
) -> None:
    """[[ ]] is not in dash: its `[[: not found` counts as a false test, which would skip reset.sh's refusal of a
    database outside ./data. The scripts therefore stop at once when the shell cannot run [[ ]]. The test is of the
    feature, not of the variable BASH_VERSION: dash takes that from the environment, so a name test is bypassed
    (the second case exports it with a made-up value)."""
    db = tmp_path / "desk.sqlite3"
    db.write_text("made-up lead book", encoding="utf-8")
    bin_dir = (
        tmp_path / "bin"
    )  # a stand-in python first on PATH, so that without the guard start_all.sh starts nothing real
    bin_dir.mkdir()
    (bin_dir / "python").write_text(_STUB_PYTHON, encoding="utf-8")
    (bin_dir / "python").chmod(0o755)
    stub = bin_dir if script == "start_all.sh" else None
    extra = {} if bash_version is None else {"BASH_VERSION": bash_version}
    done = _run(script, db, shell=shutil.which("dash") or "dash", first_on_path=stub, STUB_DATA=str(tmp_path), **extra)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "run this script with bash" in done.stderr and "not bash" in done.stderr
    assert "not found" not in done.stderr, "a [[ line ran under dash"
    assert db.read_text(encoding="utf-8") == "made-up lead book"
    assert not (tmp_path / "calls.log").exists(), "the stand-in python was called"


@pytest.mark.skipif(WINDOWS or shutil.which("dash") is None, reason="needs dash (the sh of Debian and Ubuntu)")
@pytest.mark.parametrize("script", ["reset.sh", "start_all.sh"])
def test_the_guard_line_refuses_under_dash_even_without_set_e(tmp_path: Path, script: str) -> None:
    """The guard joins its probe and `echo bash` with `&&`. With `;` it would still print `bash` after the failed
    probe, and only the `set -e` of the script (which comes before the guard) would hide that. This runs the guard
    line alone, with no `set -e`, so the `&&` is pinned by itself."""
    lines = (REPO / "scripts" / script).read_text(encoding="utf-8").splitlines()
    guard = [line for line in lines if line.startswith('case "$( (eval ')]
    assert len(guard) == 1, "the guard line was not found exactly once"
    db = tmp_path / "unused.sqlite3"
    after = guard[0] + "\necho PAST-THE-GUARD"
    refused = _run(script, db, shell=shutil.which("dash") or "dash", code=after)
    assert refused.returncode == 2 and "PAST-THE-GUARD" not in refused.stdout, (refused.stdout, refused.stderr)
    passed = _run(script, db, code=after)
    assert passed.returncode == 0 and "PAST-THE-GUARD" in passed.stdout, (passed.stdout, passed.stderr)
