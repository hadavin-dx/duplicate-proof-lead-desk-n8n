"""Fixtures for the desk tests. `env.rig(...)` builds a started Desk on fakes; everything is stopped at teardown."""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from lead_desk.constants import REQUEST_TIMEOUT_SECONDS
from lead_desk.server import serve_in_thread

# The demo folder is removable (deleting it leaves the desk and its tests intact). Without it a test FILE that needs the
# demo's fakes is reported as one skipped item (the whole file, not test by test), and the files that never touch it
# still run. In CI the folder must be there: with DPLD_REQUIRE_DEMO=1 a missing folder stops the run instead of skipping
# nearly everything green.
DEMO_PRESENT = importlib.util.find_spec("demo_only") is not None
REQUIRE_DEMO_ENV = "DPLD_REQUIRE_DEMO"


def pytest_sessionstart(session: pytest.Session) -> None:
    if not DEMO_PRESENT and os.environ.get(REQUIRE_DEMO_ENV) == "1":
        pytest.exit(
            f"the demo_only folder is missing but {REQUIRE_DEMO_ENV}=1: refusing to skip most tests", returncode=2
        )


if DEMO_PRESENT:
    from support import Rig, ServerDidNotStop, build_rig, stop_within

    from demo_only.fakes_server import FakesServer, serve_fakes_in_thread, stop_fakes

_NEEDS_DEMO = re.compile(
    r"^\s*(?:from|import)\s+(?:demo_only|support|edge_case_helpers|test_\w+)\b|^(?:async )?def test_\w+\([^)]*\benv\b",
    re.MULTILINE,
)


class _NeedsDemoItem(pytest.Item):
    def runtest(self) -> None:
        pytest.skip("needs the demo_only folder, which is not here")

    def reportinfo(self) -> tuple[Path, int, str]:
        return self.path, 0, "needs demo_only"


class _NeedsDemoModule(pytest.Module):
    def collect(self) -> list[pytest.Item]:  # type: ignore[override]
        return [_NeedsDemoItem.from_parent(self, name="needs-demo-only")]


if not DEMO_PRESENT:

    @pytest.hookimpl(tryfirst=True)
    def pytest_pycollect_makemodule(module_path: Path, parent: Any) -> Any:
        if _NEEDS_DEMO.search(module_path.read_text(encoding="utf-8")):
            return _NeedsDemoModule.from_parent(parent, path=module_path)
        return None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
        """A test that reaches the missing folder at run time (for example the dry run of the real-services check) is
        skipped, not failed; any other error stays a failure."""
        outcome = yield
        err = call.excinfo.value if call.excinfo is not None else None
        if isinstance(err, ModuleNotFoundError) and str(err.name or "").split(".")[0] == "demo_only":
            rep = outcome.get_result()
            rep.outcome = "skipped"
            rep.longrepr = (str(item.path), 0, "Skipped: needs the demo_only folder, which is not here")


# serve_in_thread's stop ends with thread.join(timeout=REQUEST_TIMEOUT_SECONDS), so a legitimate stop can last that long
# (measured, a scratch test with stalled clients: the handler threads are daemons and are not joined, the stop took
# 0.00 s; the product bound is still the worst case). The guard must catch a wait with no end, not a slow stop:
# twice that.
DESK_STOP_SECONDS = 2 * REQUEST_TIMEOUT_SECONDS
_ENVS: list[Env] = []  # the Env objects of the running test; _stuck_servers_fail_the_test reads them at the very end


@pytest.fixture(autouse=True)
def _stuck_servers_fail_the_test() -> Iterator[None]:
    """After the test and after every `env` teardown (this fixture is set up first, so it is torn down last): a server
    that did not stop in time fails the test, and the message names every one. Every other item was closed first."""
    _ENVS.clear()
    yield
    report = "; ".join(r for r in (e.stuck_report() for e in _ENVS) if r)
    _ENVS.clear()
    if report:
        raise ServerDidNotStop(report)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self._n = 0
        self.rigs: list[Rig] = []
        self._stops: list[Callable[[], None]] = []
        self.stuck: list[ServerDidNotStop] = []  # every server that did not stop in time; the test fails at its end
        _ENVS.append(self)

    def stuck_report(self) -> str:
        """One message naming every server that did not stop (empty when all stopped)."""
        return "; ".join(str(e) for e in self.stuck)

    def rig(self, **kw: Any) -> Rig:
        self._n += 1
        sub = self.tmp / f"rig{self._n}"
        sub.mkdir()
        r = build_rig(sub, **kw)
        self.rigs.append(r)
        return r

    def serve(self, r: Rig) -> str:
        """Start the real HTTP server (port 0) around r.desk; returns the base URL."""
        base, stop = serve_in_thread(r.desk, port=0)
        self._stops.append(lambda: stop_within(stop, "desk server", seconds=DESK_STOP_SECONDS))
        return base

    def fakes(self, mode: str = "keyed", crm_mode: str = "keyed") -> tuple[FakesServer, str]:
        srv, url, th = serve_fakes_in_thread(mode, 0, crm_mode)
        self._stops.append(lambda: stop_within(lambda: stop_fakes(srv, th), "fakes server", seconds=DESK_STOP_SECONDS))
        return srv, url

    def stop_on_close(self, stop: Callable[[], None]) -> None:
        self._stops.append(stop)

    def close(self) -> None:
        for stop in reversed(self._stops):
            try:
                stop()
            except ServerDidNotStop as e:  # reported, all of them, after every other item is closed
                self.stuck.append(e)
            except Exception:  # noqa: BLE001, S110 (teardown must reach every item)
                pass
        for r in self.rigs:
            for d in r.desks:
                try:
                    d.close()
                except Exception:  # noqa: BLE001, S110
                    pass


@pytest.fixture(autouse=True)
def _no_desk_settings_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DESK_* setting of the person running the tests must never leak into a test that calls `server.main()` or
    `Config.from_env()` without a dict (a test that wants one sets it itself with monkeypatch.setenv)."""
    for name in [n for n in os.environ if n.startswith("DESK_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()
