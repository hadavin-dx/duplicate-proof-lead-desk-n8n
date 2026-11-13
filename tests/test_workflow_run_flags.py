"""The command lines of the workflow `run:` steps keep the flags SonarCloud asks for.

A separate file on purpose: it needs nothing from the workflow shape test (tests/test_workflow_files_parse.py), so the
two can change independently. PyYAML is in the development lock (requirements-dev.txt).
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted(p for ext in ("*.yml", "*.yaml") for p in (REPO / ".github" / "workflows").glob(ext))


def test_there_are_workflow_files() -> None:
    assert WORKFLOWS, "no workflow file found: the checks below would pass for nothing"


def _run_commands(path: Path) -> list[str]:
    """Every shell command line of every `run:` step in one workflow file (a line that ends in a backslash is joined
    with the next one, so a command that is split over lines is one command)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    lines: list[str] = []
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                lines += str(step["run"]).replace("\\\n", " ").splitlines()
    return lines


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_download_in_a_workflow_is_https_only_and_modern_tls(path: Path) -> None:
    """curl follows redirects and would speak plain http if told to; --proto (first request) and --proto-redir (every
    redirect) pin https, and --tlsv1.2 sets the lowest TLS version (SonarCloud rule githubactions:S6506)."""
    for cmd in (c for c in _run_commands(path) if re.search(r"\bcurl\b", c)):
        for flag in ("--proto '=https'", "--proto-redir '=https'", "--tlsv1.2"):
            assert flag in cmd, f"{path.name}: curl without {flag}: {cmd}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_a_pip_install_in_a_workflow_takes_wheels_only_with_hashes(path: Path) -> None:
    """A source distribution runs the package's build script at install time; wheels do not (SonarCloud rule
    githubactions:S8541). The `=` spelling is the one sonar.yml uses (no finding there); --require-hashes stays."""
    for cmd in (c for c in _run_commands(path) if re.search(r"\bpip3?\s+install\b", c)):
        assert "--only-binary=:all:" in cmd, f"{path.name}: pip install without --only-binary=:all: -> {cmd}"
        assert "--require-hashes" in cmd, f"{path.name}: pip install without --require-hashes -> {cmd}"
