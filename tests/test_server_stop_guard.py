"""A stop with no time limit can stall a test run; support.py bounds it and prints the stacks of all threads.

The standard library's BaseServer.shutdown() waits, without a time limit, for the serve_forever loop to finish; when
that loop is not running the wait never ends. The helpers in support.py wait until the loop runs and bound every
stop, so a stuck stop becomes a failed test with a message. This is a diagnostic guard: the cause of the one
suspected stall in an earlier full run is UNPROVEN (that run was most probably only slow, or its buffered progress
file looked stale). Honest limit: a stuck stop is given up on, not undone; tests 1 and 4 below leave one daemon
thread blocked until the test process ends. Made-up data only."""

from __future__ import annotations

import io
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from support import HOST, ServerDidNotStop, start_serving, stop_all, stop_serving, stop_within


def _server(cls: type[ThreadingHTTPServer] = ThreadingHTTPServer) -> ThreadingHTTPServer:
    return cls((HOST, 0), BaseHTTPRequestHandler)


def test_a_stop_that_never_returns_fails_fast_with_a_message(capfd: pytest.CaptureFixture[str]) -> None:
    # No loop is running, so the plain srv.shutdown() would wait for ever. The helper thread that makes that call
    # stays blocked (a daemon) until the test process ends: the guard gives up on a stuck stop, it cannot undo it.
    srv = _server()
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    t0 = time.monotonic()
    try:
        with pytest.raises(ServerDidNotStop, match=r"stuck server did not stop within 0\.5 s"):
            stop_serving(srv, dead, "stuck server", seconds=0.5)
    finally:
        srv.server_close()
    assert time.monotonic() - t0 < 5
    assert "socketserver.py" in capfd.readouterr().err  # the stack of every thread goes to stderr


def test_the_stack_of_every_thread_is_printed_when_a_stop_hangs(capfd: pytest.CaptureFixture[str]) -> None:
    release = threading.Event()
    try:
        with pytest.raises(ServerDidNotStop):
            stop_within(release.wait, "blocked stop", seconds=0.3)
    finally:
        release.set()
    err = capfd.readouterr().err
    assert "most recent call first" in err and "in wait" in err  # the stuck stop thread is in the dump


def test_start_serving_returns_only_when_the_loop_runs() -> None:
    class Late(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.5) -> None:
            time.sleep(0.4)  # the thread has started, the loop has not
            super().serve_forever(poll_interval)

    srv = _server(Late)
    t0 = time.monotonic()
    thread = start_serving(srv)
    assert time.monotonic() - t0 >= 0.4 and thread.is_alive()
    stop_serving(srv)
    assert not thread.is_alive()


def test_start_serving_fails_when_the_loop_never_starts() -> None:
    class Never(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.5) -> None:
            threading.Event().wait()

    srv = _server(Never)
    try:
        with pytest.raises(ServerDidNotStop, match="did not start"):
            start_serving(srv, ready_within=0.3)
    finally:
        srv.server_close()


def test_the_stack_dump_does_not_hide_the_error_when_stderr_has_no_file_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under `--capture=sys` or capsys, sys.stderr has no fileno(); faulthandler would raise io.UnsupportedOperation
    and replace ServerDidNotStop. The guard falls back to the process's own stderr."""
    with pytest.raises(io.UnsupportedOperation):
        sys.stderr.fileno()  # the setup is real: this capture has no file number (capsys's CaptureIO)
    release = threading.Event()
    try:
        with pytest.raises(ServerDidNotStop, match="blocked stop did not stop within 0.2 s"):
            stop_within(release.wait, "blocked stop", seconds=0.2)
    finally:
        release.set()


def test_stop_all_stops_every_server_even_when_the_first_stop_fails() -> None:
    """The first server is made stuck (its loop is not running); the second must still be stopped, and the error names
    the stuck one. A single stuck server raises as it is; two raise one error naming both."""

    class Stuck(ThreadingHTTPServer):
        def shutdown(self) -> None:
            threading.Event().wait()  # a daemon helper thread stays blocked here until the process ends

    stuck, good = _server(Stuck), _server()
    stuck.serve_thread = threading.Thread(target=lambda: None)  # type: ignore[attr-defined]
    good_thread = start_serving(good)
    try:
        with pytest.raises(ServerDidNotStop, match=r"alpha did not stop within 0\.3 s"):
            stop_all((stuck, good), "alpha", seconds=0.3)
    finally:
        stuck.server_close()
    assert not good_thread.is_alive()


def test_a_stop_that_works_returns_at_once_and_passes_on_errors() -> None:
    srv = _server()
    thread = start_serving(srv)
    t0 = time.monotonic()
    stop_serving(srv, thread)
    assert time.monotonic() - t0 < 2 and not thread.is_alive()

    def boom() -> None:
        raise ValueError("kept")

    with pytest.raises(ValueError, match="kept"):
        stop_within(boom)


def test_the_env_fixture_reports_every_server_that_did_not_stop_after_closing_the_rest(env: Any) -> None:
    release = threading.Event()
    closed: list[str] = []
    env.stop_on_close(lambda: closed.append("first"))  # stopped last: must still run
    env.stop_on_close(lambda: stop_within(release.wait, "stuck stand-in A", seconds=0.2))
    env.stop_on_close(lambda: stop_within(release.wait, "stuck stand-in B", seconds=0.2))
    try:
        env.close()  # does not raise: the test fails at its end (the autouse fixture in conftest.py)
        assert closed == ["first"] and len(env.stuck) == 2
        assert "stuck stand-in A" in env.stuck_report() and "stuck stand-in B" in env.stuck_report()  # every one named
    finally:
        release.set()
        env.stuck.clear()  # the autouse fixture would otherwise (rightly) fail this test at its end
