"""The two gitleaks allowances for the Semgrep lock (`.gitleaks.toml`) are narrow: tested with the real gitleaks.

Allowed: the hash lines of `requirements-semgrep.txt` in the repository root (a line of spaces, `--hash=sha256:` and
exactly 64 lowercase hex characters, with or without a trailing ` \\`) and the file name `requirements-semgrep.in`.
Everything else that looks like it is reported by the real program. A second part widens each allowance on purpose
(mutants) and checks that the negative cases then slip through: the test fails if it can no longer see a widening.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from support import REPO
from test_hygiene import (
    _gitleaks_exe,
    _run,
)  # the one place that finds the program

from tools import hygiene

GITLEAKS_ENV = "GITLEAKS_BIN"
REQUIRE_SCAN_ENV = "DPLD_REQUIRE_SCAN"
NL = chr(10)
H32 = "a1b2c3d4" * 4  # hash-like values are built from parts: this file must not hold such a literal itself
H64 = "a1b2c3d4e5f6a7b8" * 4
H40 = "a1b2c3d4e5" * 4
H65 = H64 + "a"
LOCK = "requirements-semgrep.txt"
HASH = f"    --hash=sha256:{H64}"


def _exe() -> str:
    exe = _gitleaks_exe()
    if not exe and not os.environ.get(REQUIRE_SCAN_ENV):
        pytest.skip(
            f"no gitleaks program on PATH (set {GITLEAKS_ENV}); set {REQUIRE_SCAN_ENV}=1 to make this a failure"
        )
    assert exe, f"gitleaks binary not found: put it on PATH or set {GITLEAKS_ENV}"
    return exe


@pytest.mark.skipif(os.name == "nt", reason="a fake program on PATH needs the executable bit")
def test_exe_bare_name_and_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare GITLEAKS_BIN is found on PATH and a symlink is not resolved (the behaviour of _gitleaks_exe)."""
    real = tmp_path / "bin" / "real-gl"
    real.parent.mkdir()
    real.write_text("#!/bin/sh" + NL, encoding="utf-8")
    real.chmod(0o755)
    link = tmp_path / "bin" / "gl-link"
    link.symlink_to(real)
    monkeypatch.setenv("PATH", str(real.parent))
    monkeypatch.setenv(GITLEAKS_ENV, "gl-link")
    assert _exe() == str(link)
    monkeypatch.setenv(GITLEAKS_ENV, str(link))
    assert _exe() == str(link)
    assert _exe() != str(real.resolve())


def _scan(tmp: Path, name: str, files: dict[str, str], toml: Path) -> set[tuple[str, str]]:
    """Scan a folder of planted files from its own root (like lint.yml): the (file, rule) of each finding."""
    tree = tmp / name
    for rel, text in files.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_bytes(text.encode("utf-8"))  # bytes: a CRLF line stays a CRLF line
    report = tmp / f"{name}.json"
    cmd = [_exe(), "dir", "--no-banner", "--redact", "--ignore-gitleaks-allow", "--config", str(toml)]
    _run(tree, *cmd, "--report-format", "json", "--report-path", str(report), ".", check=False)  # exit 1 = leaks found
    rows = json.loads(report.read_text(encoding="utf-8") or "[]")
    return {(x["File"].replace(chr(92), "/"), x["RuleID"]) for x in rows}


OK = {
    LOCK: f"semgrep==1.0.0 \\{NL}{HASH} \\{NL}{HASH}{NL}    --hash=sha256:{H64} \\\r\n    --hash=sha256:{H64}\r\n",
    "requirements-semgrep.in": f"#   uv pip compile requirements-semgrep.in -o {LOCK}{NL}semgrep==1.0.0{NL}",
}
# each value is a requirements-semgrep.txt that must still be reported by the long-hex rule
BAD_LOCK_CONTENT = {
    "hex-before-the-hash": f"{H32} --hash=sha256:{H64} \\{NL}",
    "hex-before-no-space": f"x{H32}--hash=sha256:{H64}{NL}",
    "bare-64-hex": f"    {H64}{NL}",
    "40-hex-after-the-prefix": f"    --hash=sha256:{H40} \\{NL}",
    "65-hex-after-the-prefix": f"    --hash=sha256:{H65}{NL}",
    "upper-case-hex": f"    --hash=sha256:{H64.upper()}{NL}",
    "text-after-the-hash": f"    --hash=sha256:{H64} # more{NL}",
    "40-hex-id-only": f"{H40}{NL}",
}
# the same hash line in a file that is not the lock
BAD_FILES = {
    "old-requirements-semgrep.txt": HASH + NL,
    "requirements-semgrep.txt.bak": HASH + NL,
    "README.md": HASH + NL,
    "src/x.py": HASH + NL,
    "docs/requirements-semgrep.txt": HASH + NL,  # the allowance is for the lock in the repository root only
    "sub/dir/requirements-semgrep.txt": HASH + NL,
}
LOCK_IN = "requirements-semgrep" + ".in"
BAD_HOST = {"lock-copy.txt": "see " + LOCK_IN + "." + "evil-x" + ".io" + NL}  # a longer name is not the allowed one


def test_semgrep_lock_allowances_are_narrow(tmp_path: Path) -> None:
    toml = REPO / ".gitleaks.toml"
    assert _scan(tmp_path, "ok", OK, toml) == set(), "the real hash lines or the file name were reported"
    for case, text in BAD_LOCK_CONTENT.items():
        assert _scan(tmp_path, f"lock-{case}", {LOCK: text}, toml) == {(LOCK, "long-hex-token")}, case
    got = _scan(tmp_path, "files", BAD_FILES, toml)
    assert got == {(name, "long-hex-token") for name in BAD_FILES}, sorted(got)
    host = _scan(tmp_path, "host", BAD_HOST, toml)
    assert {rule for _, rule in host} == {"hardcoded-hostname"}, host


def _widened(tmp_path: Path, name: str, old: str, new: str) -> Path:
    text = (REPO / ".gitleaks.toml").read_text(encoding="utf-8")
    assert text.count(old) == 1, f"the allowance text this mutant changes is not in .gitleaks.toml once: {old!r}"
    out = tmp_path / f"{name}.toml"
    out.write_text(text.replace(old, new), encoding="utf-8")
    return out


def test_widening_an_allowance_makes_the_negative_cases_pass(tmp_path: Path) -> None:
    """Mutants: if the left anchor, the file-name rule or the hostname rule is loosened, a negative case above must
    stop being reported. This proves the test above would fail on such a change."""
    anchor, path_rule = "'''^\\n?[ \\t]*--hash=sha256:", "paths = ['''^requirements-semgrep\\.txt$''']"
    no_anchor = _widened(tmp_path, "no-anchor", anchor, "'''--hash=sha256:")
    still = _scan(tmp_path, "m1", {LOCK: BAD_LOCK_CONTENT["hex-before-the-hash"]}, no_anchor)
    assert still == set(), f"left anchor removed, the line is still reported: the test could not see this: {still}"
    any_file = _widened(tmp_path, "any-path", path_rule, "paths = ['''.*''']")
    still = _scan(tmp_path, "m2", BAD_FILES, any_file)
    assert still == set(), f"path rule widened, hash lines elsewhere are still reported: {still}"
    any_depth = _widened(tmp_path, "any-depth", path_rule, "paths = ['''(^|[/\\\\])requirements-semgrep\\.txt$''']")
    still = _scan(tmp_path, "m2b", {"docs/requirements-semgrep.txt": HASH + NL}, any_depth)
    assert still == set(), f"the path rule lost its left anchor, a nested lock is still reported: {still}"
    any_host = _widened(tmp_path, "any-host", "'''^requirements-semgrep\\.in[^A-Za-z0-9_-]?$'''", "'''.*'''")
    still = _scan(tmp_path, "m3", BAD_HOST, any_host)
    assert still == set(), f"hostname rule widened, a longer host name is still reported: {still}"


# exceptions.txt, the line for the names of the OpenTelemetry packages in the Semgrep lock: the literal-text scan of
# lint.yml (patterns.txt, any run of 40 or more letters, digits or dashes) reports these long package names.
OTEL_LINE = "^requirements-semgrep\\.txt:[0-9]+:(opentelemetry-[a-z-]+)(==[0-9]+)?$"
OTEL_NAME = "opentelemetry" + "-exporter-otlp-proto-common"  # built from parts: no 40-character literal in this file
OTEL_BAD = {
    "requirements-semgrep.txt": "x" + OTEL_NAME[1:],  # another long name in the lock itself is still reported
    "README.md": OTEL_NAME,
    "docs/requirements-semgrep.txt": OTEL_NAME,  # the exception is for the lock in the repository root only
    "old-requirements-semgrep.txt": OTEL_NAME,
    "requirements-semgrep.txt.bak": OTEL_NAME,
    "src/x.py": OTEL_NAME + "==1",
}


def _literal_scan(tmp: Path, name: str, files: dict[str, str], exceptions: Path) -> set[str]:
    """The files the literal-text scan still reports after `grep -v -E -f exceptions.txt`."""
    tree = tmp / name
    for rel, text in files.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text + NL, encoding="utf-8")
    _run(tree, "git", "init", "-q")
    _run(tree, "git", "add", "-A")
    return {ln.split(":", 1)[0] for ln in hygiene.pipeline(tree, REPO / "patterns.txt", exceptions)}


def test_otel_name_exception_is_narrow(tmp_path: Path) -> None:
    """Only a name of the form `opentelemetry-<letters and dashes>`, with an optional `==<digits>`, in the lock file in
    the repository root is let through; the same text elsewhere, and another long name in the lock, is reported."""
    exceptions = REPO / "exceptions.txt"
    assert OTEL_LINE in exceptions.read_text(encoding="utf-8").splitlines()
    assert _literal_scan(tmp_path, "ok", {LOCK: OTEL_NAME + "==1" + NL + OTEL_NAME}, exceptions) == set()
    assert _literal_scan(tmp_path, "bad", OTEL_BAD, exceptions) == set(OTEL_BAD)
    # the same cases against a widened exception (the mutants): each must let a bad case through, or this test is blind
    text = exceptions.read_text(encoding="utf-8")
    for name, widened in {
        "no-file-anchor": OTEL_LINE.replace("^requirements-semgrep", "requirements-semgrep"),
        "any-file": OTEL_LINE.replace("requirements-semgrep\\.txt", "[^:]+"),
        "any-name": OTEL_LINE.replace("opentelemetry-", ""),
    }.items():
        assert widened != OTEL_LINE
        out = tmp_path / f"{name}.txt"
        out.write_text(text.replace(OTEL_LINE, widened), encoding="utf-8")
        assert _literal_scan(tmp_path, f"m-{name}", OTEL_BAD, out) != set(OTEL_BAD), name
