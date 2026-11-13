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


def _run(script: str, db: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DESK_DB_PATH": str(db), **extra}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    if WINDOWS:
        powershell = shutil.which("powershell")
        if powershell is None:
            pytest.skip("Windows PowerShell is not installed")
        cmd = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO / "scripts" / script)]
    else:
        cmd = ["bash", str(REPO / "scripts" / script)]
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
