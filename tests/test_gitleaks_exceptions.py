"""The SonarQube project-key exception is exact: only the one real line in sonar-project.properties is let through;
every near-miss planted in a temporary copy is still reported, by the scan for hardcoded literals (tools/hygiene.py)
and by gitleaks. Values are built from the repository's own key and from a fixed random seed at run time: no
key-shaped literal in this file. Files are written as bytes, so the line endings are the same on every system.

Which scan is asserted for which variant is a measurement (gitleaks 8.30.1, 2026-11-13), see docs/tests.md,
"Quality checks": gitleaks' generic-api-key rule reported a value after `sonar.projectKey=` and `x_api_key=` only
if it held a digit and was not one repeated character (the cause looks like that; the rule inside gitleaks was not
read); a
letters-only second project key is reported by the literal scan alone (ONLY_LITERAL_SCAN).

The two sets below are one-directional on purpose: a variant listed in ONLY_GITLEAKS is not asserted to pass the
literal scan, and one in ONLY_LITERAL_SCAN is not asserted to pass gitleaks, because a stricter scan is a harmless
improvement that must not fail this file."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from support import REPO
from test_hygiene import GITLEAKS_ENV, REQUIRE_SCAN_ENV, _gitleaks_exe

from tools import hygiene

FILE = "sonar-project.properties"
NAME = "sonar.project" + "Key="
SEED = 20261113  # fixed: the same planted values on every run


def _real_key() -> str:
    for line in (REPO / FILE).read_text(encoding="utf-8").splitlines():
        if line.startswith(NAME):
            return line[len(NAME) :]
    raise AssertionError(f"{FILE} has no {NAME} line")


def _random_text(n: int, alphabet: str) -> str:
    rnd = random.Random(SEED)  # noqa: S311  (made-up test value, not a secret)
    return "".join(rnd.choice(alphabet) for _ in range(n))


def _doc(line: str, eol: str = "\n") -> bytes:
    """The planted file: a harmless first line, then the line, each ended by eol (bytes, no newline translation)."""
    return ("a=b" + eol + line + eol).encode("utf-8")


def _variants() -> dict[str, tuple[str, bytes]]:
    """name -> (file name, file content) for a near-miss of the allowed line."""
    key = _real_key()
    prefix = key.split("_")[0] + "_"  # the owner prefix of the real key
    ok = NAME + key
    letters, digits = "abcdefghijklmnopqrstuvwxyz", "0123456789"
    return {
        "other project, letters and digits": (
            FILE,
            _doc(NAME + prefix + "other-" + _random_text(24, letters + digits)),
        ),
        "other project, letters only": (FILE, _doc(NAME + prefix + "o-" + _random_text(24, letters))),
        "last character changed": (FILE, _doc(ok[:-1] + "x")),
        "dash suffix": (FILE, _doc(ok + "-x")),
        "equals suffix": (FILE, _doc(ok + "=x")),
        "space and text suffix": (FILE, _doc(ok + " x")),
        "trailing blank": (FILE, _doc(ok + " ")),
        "dot suffix": (FILE, _doc(ok + ".x")),
        "upper-case value": (FILE, _doc(NAME + key.upper())),
        "capital first letter": (FILE, _doc(NAME + key.capitalize())),
        "lower-case name": (FILE, _doc(ok.replace("Key", "key"))),
        "upper-case name": (FILE, _doc(ok.replace("projectKey", "PROJECTKEY"))),
        "other character at the dot": (FILE, _doc(ok.replace("sonar.project", "sonar" + "X" + "project"))),
        "spaces around the equals sign": (FILE, _doc(NAME[:-1] + " = " + key)),
        "text before the name": (FILE, _doc("x" + ok)),
        "key twice, space between": (FILE, _doc(ok + " " + ok)),
        "key twice, joined": (FILE, _doc(ok + ok)),
        # CRLF pins the CURRENT behaviour on purpose: gitleaks reports it (a future `\r?$` in the allowlist would fail
        # this on purpose). .gitattributes (`* text=auto eol=lf`) makes every checkout LF, so neither CI nor a Windows
        # developer sees this; only a hand-saved CRLF working-tree file would.
        "CRLF line ending": (FILE, _doc(ok, "\r\n")),
        "look-alike file name": ("old-" + FILE, _doc(ok)),
        "other character at the dot of the file name": (FILE.replace(".", "X"), _doc(ok)),
        "text after the file name": (FILE + ".bak", _doc(ok)),
        "other folder and name": ("docs/x.properties", _doc(ok)),
        "other toml file": ("docs/x.toml", _doc(ok)),
        "other character at the dot of .gitleaks.toml": ("Xgitleaks.toml", _doc(ok)),
        "readme": ("README.md", _doc(ok)),
    }


# Allowed by both scans on purpose: the same public text with a blank in front (the key's own line is unchanged).
ALLOWED_VARIANTS = {"leading blank": (FILE, _doc(" " + NAME + _real_key()))}
# The scan for hardcoded literals looks at the matched run of characters: a space or a dot after the key ends the run
# (the text equals the allowed key again), text before the name and a CR before the line end are not part of the run;
# gitleaks sees those.
ONLY_GITLEAKS = {
    "trailing blank",
    "space and text suffix",
    "dot suffix",
    "text before the name",
    "key twice, space between",
    "CRLF line ending",
}
# gitleaks 8.30.1 reported a value after this key name only if it held a digit and was not one repeated character
# (docs/tests.md, "Quality checks"): a letters-only second project key is guarded by the literal scan alone. A joined
# double line was not reported by gitleaks either. Both are measured, not understood.
# Measured: gitleaks 8.30.1 does not report a file named Xgitleaks.toml (it does report Xgitleaks2.toml), so that
# variant is asserted on the literal scan only.
ONLY_LITERAL_SCAN = {
    "other project, letters only",
    "key twice, joined",
    "other character at the dot of .gitleaks.toml",
}


def _plant(root: Path, files: dict[str, bytes]) -> Path:
    if shutil.which("git") is None:
        if os.environ.get(REQUIRE_SCAN_ENV):
            pytest.fail("git program not found on PATH (the scans read tracked files)")
        pytest.skip(
            f"no git program on PATH (the scans read tracked files); set {REQUIRE_SCAN_ENV}=1 to make this a failure"
        )
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S603, S607
    subprocess.run(["git", "add", "."], cwd=root, check=True)  # noqa: S603, S607
    return root


def _literal_hits(root: Path) -> set[str]:
    left = hygiene.pipeline(root, REPO / "patterns.txt", REPO / "exceptions.txt")
    return {ln.split(":", 1)[0] for ln in left}


def _gitleaks_program() -> str:
    exe = _gitleaks_exe()  # the helper of tests/test_hygiene.py: bare name looked up on PATH, absolute path
    if not exe:
        if os.environ.get(REQUIRE_SCAN_ENV):
            pytest.fail(f"gitleaks binary not found: put it on PATH or set {GITLEAKS_ENV}")
        pytest.skip(f"no gitleaks program (set {GITLEAKS_ENV}); set {REQUIRE_SCAN_ENV}=1 to make this a failure")
    return exe


def _gitleaks_hits(root: Path) -> set[str]:
    report = root.parent / (root.name + ".json")  # the report goes outside the planted folder
    cmd = [_gitleaks_program(), "dir", "--no-banner", "--redact", "--ignore-gitleaks-allow"]
    cmd += ["--config", str(REPO / ".gitleaks.toml"), "--report-format", "json", "--report-path", str(report), "."]
    subprocess.run(cmd, cwd=root, capture_output=True, check=False)  # noqa: S603
    rows = json.loads(report.read_text(encoding="utf-8") or "[]")
    return {x["File"].replace(chr(92), "/") for x in rows}


VARIANTS = _variants()


def test_the_exact_line_is_allowed(tmp_path: Path) -> None:
    root = _plant(tmp_path / "t", {FILE: _doc(NAME + _real_key())})
    assert _literal_hits(root) == set()  # without this control, "reported" below would prove nothing
    assert _gitleaks_hits(root) == set()


@pytest.mark.parametrize("name", sorted(ALLOWED_VARIANTS))
def test_a_blank_in_front_of_the_exact_line_is_allowed(tmp_path: Path, name: str) -> None:
    rel, data = ALLOWED_VARIANTS[name]
    root = _plant(tmp_path / "t", {rel: data})
    assert _literal_hits(root) == set()
    assert _gitleaks_hits(root) == set()


@pytest.mark.parametrize("name", [n for n in VARIANTS if n not in ONLY_GITLEAKS])
def test_a_near_miss_is_reported_by_the_literal_scan(tmp_path: Path, name: str) -> None:
    rel, data = VARIANTS[name]
    assert _literal_hits(_plant(tmp_path / "t", {rel: data})) == {rel}


@pytest.mark.parametrize("name", [n for n in VARIANTS if n not in ONLY_LITERAL_SCAN])
def test_a_near_miss_is_reported_by_gitleaks(tmp_path: Path, name: str) -> None:
    rel, data = VARIANTS[name]
    assert _gitleaks_hits(_plant(tmp_path / "t", {rel: data})) == {rel}


def test_the_exact_line_in_a_sub_folder_is_reported_by_the_literal_scan(tmp_path: Path) -> None:
    # gitleaks lets it through (its path rule has a folder part; harmless: the text is the public key)
    rel = "sub/" + FILE
    assert _literal_hits(_plant(tmp_path / "t", {rel: _doc(NAME + _real_key())})) == {rel}
