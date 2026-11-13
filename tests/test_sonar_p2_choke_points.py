"""Sonar fixes, patch P2: two patterns that cost n squared steps on a long text (S8786), clear-text addresses written
as literals (S5332), and a flag filled by an inner function (S2583). Timing tests use a long text that took seconds on
the old pattern and take milliseconds on the new one, with a bound of half a second; equivalence tests compare the new
pattern with the old one on every short text of a small alphabet. Every address value is built from parts. Made-up
data only."""

from __future__ import annotations

import errno
import itertools
import re
import socket
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from support import FakeClock, lead
from test_mirror_gate import _Crm, _flow, _sql, _tries

from lead_desk import identity, server, urls
from lead_desk.config import Config

REPO = Path(__file__).resolve().parents[1]

LONG = 40_000  # the old patterns took 3 to 30 seconds here
BOUND_SECONDS = 0.5
AT = "@"
DOT = "."

# the patterns as they were before the patch (the reference for the equivalence tests)
OLD_PLAIN = re.compile(r'^[^\s<>\r\n\x00,;"()]+@[^\s<>\r\n\x00,;"()]+$')
OLD_EMAIL = re.compile(r"[^@\s<>\",;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+")


def _seconds(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


# -- S8786: time ---------------------------------------------------------------------------------------------


def test_plain_address_pattern_is_linear_when_the_text_ends_in_a_blank() -> None:
    assert _seconds(lambda: identity.PLAIN_ADDRESS.fullmatch(("a" + AT) * (LONG // 2) + " ")) < BOUND_SECONDS
    assert _seconds(lambda: identity.PLAIN_ADDRESS.fullmatch(AT * LONG + " ")) < BOUND_SECONDS


def test_email_pattern_is_linear_on_a_domain_with_many_dots_and_a_bad_end() -> None:
    domain = "b" + DOT
    assert _seconds(lambda: identity._EMAIL_RE.fullmatch("a" + AT + domain * (LONG // 2) + " ")) < BOUND_SECONDS
    assert _seconds(lambda: identity._EMAIL_RE.fullmatch("a" * LONG + AT + "b" * LONG)) < BOUND_SECONDS


# -- S8786: same results -------------------------------------------------------------------------------------


def _same(old: re.Pattern[str], new: re.Pattern[str], how: str, alphabet: list[str], longest: int) -> int:
    seen = 0
    for n in range(longest + 1):
        for parts in itertools.product(alphabet, repeat=n):
            text = "".join(parts)
            if how == "fullmatch":
                a, b = bool(old.fullmatch(text)), bool(new.fullmatch(text))
            else:
                a, b = old.sub("<>", text), new.sub("<>", text)
            assert a == b, (text, a, b)
            seen += 1
    return seen


def test_plain_address_pattern_accepts_the_same_texts() -> None:
    alphabet = ["a", AT, DOT, " ", "(", "\n"]
    assert _same(OLD_PLAIN, identity.PLAIN_ADDRESS, "fullmatch", alphabet, 7) > 50_000


def test_email_pattern_accepts_the_same_texts() -> None:
    alphabet = ["a", AT, DOT, " ", "'", ","]
    assert _same(OLD_EMAIL, identity._EMAIL_RE, "fullmatch", alphabet, 7) > 50_000


def test_ordinary_addresses_still_match() -> None:
    mail = "jane" + DOT + "doe" + AT + "corp" + DOT + "test"
    assert identity.PLAIN_ADDRESS.fullmatch(mail) and identity._EMAIL_RE.fullmatch(mail)
    assert not identity._EMAIL_RE.fullmatch("jane" + AT + "corp") and not identity.PLAIN_ADDRESS.fullmatch("jane")


# -- S5332: one place writes the clear-text scheme -----------------------------------------------------------


def test_no_source_file_writes_a_clear_text_address_with_a_host_in_braces() -> None:
    """An f-string address that starts with the clear-text scheme and names its host in braces is how a public host
    could slip in unseen; lead_desk.urls is the one place that writes the scheme."""
    pattern = re.compile("f[\"']" + "http" + "://" + r"\{")
    hits = [
        f"{path.relative_to(REPO)}:{n}"
        for folder in ("src", "scripts", "tools")
        for path in sorted((REPO / folder).rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []


def test_desk_origin_and_the_addresses_built_from_it() -> None:
    assert urls.desk_origin("127.0.0.1", 8765) == "http" + "://127.0.0.1:8765"
    assert urls.desk_origin("[::1]", "80") == "http" + "://[::1]:80"
    cfg = Config(fakes_host="127.0.0.1", fakes_port=5001)
    assert cfg.fakes_url == urls.desk_origin("127.0.0.1", 5001)


# -- S5332 on the HTTP server: the bind rule lives in its own class ------------------------------------------


class _SysWithPlatform:
    """The ``sys`` module as the server module sees it, with only ``platform`` changed: patching ``sys.platform``
    itself would change it for the whole process (every thread and every other module)."""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def __getattr__(self, name: str) -> Any:
        return getattr(sys, name)


def test_the_windows_exclusive_bind_is_set_before_the_http_server_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert server._Server.__mro__.index(server._ExclusiveBind) < server._Server.__mro__.index(
        server.ThreadingHTTPServer
    )
    calls: list[tuple[int, int, int]] = []
    option = 0x7FFF0001  # not a real option on this computer: the recorder takes it and does not pass it on
    real: Any = socket.socket.setsockopt  # typed Any: the overloads of setsockopt take several argument shapes

    def record(self: socket.socket, level: int, name: int, value: Any, *rest: Any) -> None:
        if name == option:
            calls.append((level, name, value))
            return
        real(self, level, name, value, *rest)

    monkeypatch.setattr(socket.socket, "setsockopt", record)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", option, raising=False)
    monkeypatch.setattr(server, "sys", _SysWithPlatform("win32"))
    srv = server._Server(("127.0.0.1", 0), server._Handler)
    try:
        assert calls == [(socket.SOL_SOCKET, option, 1)]
        assert srv.server_address[1] > 0 and srv.server_name  # the HTTP server's own bind step still ran
    finally:
        srv.server_close()


def test_a_platform_stand_in_leaves_the_global_sys_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    real = sys.platform
    monkeypatch.setattr(server, "sys", _SysWithPlatform("win32"))
    assert server.sys.platform == "win32" and sys.platform == real  # only the server module sees the stand-in
    assert server.sys.argv is sys.argv  # everything else is the real module


def test_the_exclusive_bind_is_not_set_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    option = 0x7FFF0001
    real: Any = socket.socket.setsockopt  # typed Any: the overloads of setsockopt take several argument shapes

    def record(self: socket.socket, level: int, name: int, value: Any, *rest: Any) -> None:
        if name == option:
            calls.append(name)
            return
        real(self, level, name, value, *rest)

    monkeypatch.setattr(socket.socket, "setsockopt", record)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", option, raising=False)
    monkeypatch.setattr(server, "sys", _SysWithPlatform("linux"))
    srv = server._Server(("127.0.0.1", 0), server._Handler)
    try:
        assert calls == []  # the option exists on this "computer" and was still not set
        assert srv.server_address[1] > 0
    finally:
        srv.server_close()


# -- S2583: the "stage moved" flag of a sent follow-up -------------------------------------------------------


def _mid_send(env: Any, monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[Any, str, int]:
    clock = FakeClock(monkeypatch)
    rig = env.rig(leadflow=_flow(), crm=_Crm())
    lid = str(rig.desk.handle("form", name, lead()).body["lead_id"])
    for trigger in ("followups_sent_final", "lead_state_no_regress"):  # (test only) put the lead back to mid-send
        _sql(rig, f"DROP TRIGGER IF EXISTS {trigger}")
    _sql(rig, "UPDATE lead_state SET step='new', step_rank=0 WHERE lead_id=?", lid)
    _sql(rig, "UPDATE followups SET state='sending', crm_done=1 WHERE lead_id=?", lid)
    (token,) = rig.query("SELECT attempts FROM followups WHERE lead_id=?", (lid,))[0]
    rig.desk._mirror_not_before.set(lid, clock.now + 400, 0)
    return rig, lid, int(token)


def test_a_sent_result_that_moves_the_stage_drops_the_copy_wait(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, lid, token = _mid_send(env, monkeypatch, "s-1")
    rig.desk._record_send_result(lid, "sent", token)
    assert _tries(rig, lid)[2] == "contacted"
    assert lid not in rig.desk._mirror_not_before


def test_a_failed_result_leaves_the_stage_and_the_copy_wait(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, lid, token = _mid_send(env, monkeypatch, "s-2")
    rig.desk._record_send_result(lid, "failed", token, "x")
    assert _tries(rig, lid)[2] == "new"
    assert lid in rig.desk._mirror_not_before


def test_a_repeated_try_reports_no_move_when_the_row_was_already_written(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first try commits and moves the stage; a second try finds no `sending` row (0 rows) and must not claim a move
    of its own: only the try that ran last reports."""
    rig, lid, token = _mid_send(env, monkeypatch, "s-3")
    real = rig.desk._locked_retry

    def twice(fn: Callable[[], Any]) -> Any:
        real(fn)
        return real(fn)

    monkeypatch.setattr(rig.desk, "_locked_retry", twice)
    rig.desk._record_send_result(lid, "sent", token)
    assert _tries(rig, lid)[2] == "contacted"
    assert lid in rig.desk._mirror_not_before  # the second try wrote nothing and moved nothing, as before the patch


def test_a_move_seen_by_a_try_that_did_not_commit_is_not_reported(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first try moves the stage inside its write and then fails to commit (database locked, rolled back); every
    later try is locked at once. Nothing was recorded, so the copy wait must stay (the old code, which kept a flag
    across tries, freed it)."""
    rig, lid, token = _mid_send(env, monkeypatch, "s-4")
    real_write = rig.desk._write
    count = [0]

    @contextmanager
    def locked_after_the_move(conn: sqlite3.Connection) -> Iterator[None]:
        count[0] += 1
        if count[0] > 1:
            raise sqlite3.OperationalError("database is locked")
        with real_write(conn):
            yield
            raise sqlite3.OperationalError("database is locked")  # in place of the COMMIT: the write is rolled back

    monkeypatch.setattr(rig.desk, "_write", locked_after_the_move)
    rig.desk._record_send_result(lid, "sent", token)
    assert count[0] > 1  # it did try again
    assert _tries(rig, lid)[2] == "new"  # rolled back
    assert lid in rig.desk._mirror_not_before


def test_a_result_for_a_row_that_is_not_in_flight_does_not_move_the_stage(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `sent` result with a stale token changes 0 rows (the follow-up was put back or claimed again): the lead stage
    must stay and so must the copy wait, even though the lead is at a stage that a `sent` result could move."""
    rig, lid, token = _mid_send(env, monkeypatch, "s-5")
    rig.desk._record_send_result(lid, "sent", token + 1)
    assert _tries(rig, lid)[2] == "new"
    assert lid in rig.desk._mirror_not_before
    rig.desk._record_send_result(lid, "sent", token)  # the real token still works afterwards
    assert _tries(rig, lid)[2] == "contacted"
    assert lid not in rig.desk._mirror_not_before


def test_a_sent_result_when_the_stage_is_already_further_keeps_the_copy_wait(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is recorded as sent but `_mark_contacted` moves nothing (the lead is already at a later stage): the
    wait must stay. Kills "flag is true whenever rc == 1 and the state is sent, whatever `_mark_contacted` returned"."""
    rig, lid, token = _mid_send(env, monkeypatch, "s-8")
    _sql(rig, "UPDATE lead_state SET step='qualified', step_rank=? WHERE lead_id=?", 99, lid)
    rig.desk._mirror_not_before.set(lid, time.monotonic() + 400, 9)
    rig.desk._record_send_result(lid, "sent", token)
    assert rig.query("SELECT state FROM followups WHERE lead_id=?", (lid,))[0][0] == "sent"
    assert _tries(rig, lid)[2] == "qualified"
    assert lid in rig.desk._mirror_not_before


# -- the fault selector of tests/support.py sees the caller's own function ------------------------------------


def test_finish_update_hands_the_callers_own_function_to_the_retry_loop(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`support.is_write(fn, name)` picks a fault by the qualified name of the function given to `_locked_retry`. The
    mirror and reminder callers go through `_finish_update`; a wrapper (a lambda) there would hide their names and turn
    such a test into a silent no-op. The retry loop must see the callers' own functions (`copied`, `record`, ...)."""
    from support import is_write

    rig = env.rig()
    seen: list[Any] = []
    real = rig.desk._locked_retry

    def spy(fn: Callable[[], Any]) -> Any:
        seen.append(fn)
        return real(fn)

    monkeypatch.setattr(rig.desk, "_locked_retry", spy)

    def copied() -> int:
        return 1

    def record() -> int:
        return 1

    def moved() -> tuple[int, bool]:
        return 1, True

    rig.desk._finish_update(copied, "t")
    rig.desk._finish_update(record, "t")
    assert rig.desk._finish_update_moved(moved, "t") is True
    assert seen == [copied, record, moved]
    assert is_write(seen[0], "copied") and is_write(seen[1], "record") and is_write(seen[2], "moved")


def test_every_finish_update_caller_in_desk_py_passes_a_named_function() -> None:
    """The callers' attempts are inner functions with a stable name, not lambdas (so `is_write` can select them)."""
    text = (REPO / "src" / "lead_desk" / "desk.py").read_text(encoding="utf-8")
    calls = re.findall(r"self\._finish_update(?:_moved)?\(([^,]*),", text)
    assert len(calls) >= 5
    assert all(re.fullmatch(r"[a-z_]+", c.strip()) for c in calls), calls


# -- the HTTP server class: bind rule in its own class, the server class is reached through one base only ------


def test_the_server_class_order_and_a_real_double_bind_rebind_cycle() -> None:
    names = [c.__name__ for c in server._Server.__mro__[:5]]
    assert names == ["_Server", "_ExclusiveHTTPServer", "_ExclusiveBind", "ThreadingHTTPServer", "ThreadingMixIn"]
    first = server._Server(("127.0.0.1", 0), server._Handler)
    port = first.server_address[1]
    try:
        with pytest.raises(OSError) as refused:  # a second listener on a port that is listening
            server._Server(("127.0.0.1", port), server._Handler)
        assert refused.value.errno == errno.EADDRINUSE
    finally:
        first.server_close()
    again = server._Server(("127.0.0.1", port), server._Handler)  # the port is free at once after the close
    again.server_close()


# -- follow-up A: the scheme name is re-exported on purpose; the docstring says only what is true -----------


def test_the_portal_demo_re_exports_the_scheme_name_explicitly() -> None:
    """``from X import N`` alone is a private import under mypy strict (no implicit re-export); the demo module's
    ``DESK_SCHEME`` must be written ``import ... as DESK_SCHEME`` (or listed in ``__all__``)."""
    import ast

    from demo_only import portal_demo

    assert portal_demo.DESK_SCHEME is urls.DESK_SCHEME
    tree = ast.parse(Path(portal_demo.__file__).read_text(encoding="utf-8"))
    imports = [
        a
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "lead_desk.urls"
        for a in n.names
        if a.name == "DESK_SCHEME"
    ]
    assert len(imports) == 1 and imports[0].asname == "DESK_SCHEME"


def test_the_urls_docstring_makes_no_claim_about_every_file() -> None:
    """The text once said that no file writes the clear-text scheme as a literal. Files do (demo_only/, scripts/);
    the smaller honest fix was to say so, not to scan them."""
    doc = urls.__doc__ or ""
    assert "no file writes" not in doc and "nothing is sent over a network" not in doc.lower()
    assert "still write a literal" in doc
    root = Path(__file__).resolve().parent.parent
    literal = "http" + "://127.0.0.1"
    assert any(literal in p.read_text(encoding="utf-8") for p in (root / "demo_only").glob("*.py")) or any(
        literal in p.read_text(encoding="utf-8") for p in (root / "scripts").glob("*.py")
    )
