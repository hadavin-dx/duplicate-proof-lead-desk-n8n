"""Guards that the CodeAnt review showed to be blind: an inline workflow trigger, a missing import inside demo_only."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_hygiene import _workflow_problems

NL = chr(
    10
)  # lines are joined, not written with a backslash-n after a colon (the secret scan reads that as a drive path)
READ_ONLY = NL.join(["permissions:", "  contents: read", ""])


@pytest.mark.parametrize(
    "trigger",
    [
        "on: pull_request_target",
        "on: [push, pull_request_target]",
        "on: { pull_request_target: { types: [opened] } }",
        NL.join(["on:", "  workflow_run:", "    workflows: [x]"]),
    ],
)
def test_a_trigger_that_hands_secrets_to_pull_request_code_is_found_in_every_yaml_shape(trigger: str) -> None:
    assert any("forbidden trigger" in p for p in _workflow_problems(trigger + "\n" + READ_ONLY))


def test_a_commented_or_harmless_line_is_not_a_forbidden_trigger() -> None:
    assert _workflow_problems("# pull_request_target is never used here\non: [push]\n" + READ_ONLY) == []


def test_a_missing_import_inside_demo_only_is_an_error_not_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    import test_portal_demo

    def broken(name: str) -> object:
        raise ModuleNotFoundError("No module named 'demo_only.gone'", name="demo_only.gone")

    monkeypatch.setattr(importlib, "import_module", broken)
    try:
        with pytest.raises(ModuleNotFoundError):
            test_portal_demo._demo_module("demo_only.portal_demo")
    except pytest.skip.Exception:  # pytest.raises does not catch a skip: without this a skip would only show as SKIPPED
        pytest.fail("the missing import inside demo_only was skipped instead of raised")


def test_a_desk_setting_in_the_shell_does_not_reach_a_test() -> None:
    """tests/conftest.py clears every DESK_* name before each test, so a test that calls `server.main()` or
    `Config.from_env()` without a dict is not changed by the settings of whoever runs the suite."""
    probe = Path(__file__).with_name("desk_env_probe_check.py")
    shell = {**os.environ, "DESK_PORT": "1", "DESK_DB_PATH": "/nowhere", "DESK_CRM": "hubspot"}
    done = subprocess.run(  # noqa: S603 (our own interpreter, a file of this repository)
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(probe)],
        env=shell,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=probe.parent.parent,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
