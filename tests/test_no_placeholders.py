"""Nothing unfinished ships: no template placeholder or open-work marker in any file of the repository (this file
builds the words from parts, so it does not match itself)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from support import REPO

MARKERS = ("DATE-TO-" + "FILL", "TO" + "DO", "FIX" + "ME", "X" * 3)
FOUND = re.compile(r"\b(" + "|".join(MARKERS) + r")\b")
BINARY_SUFFIXES = {".png"}  # the only file that is not text; any other unreadable file is reported


def _files() -> list[Path]:
    try:
        out = subprocess.run(  # noqa: S603 (git, fixed arguments)
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
            cwd=REPO,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git work tree (a source archive): the file list is not available")
    return [REPO / n for n in out.split("\0") if n and (REPO / n).is_file()]


def test_no_placeholder_text_ships() -> None:
    hits, unreadable = [], []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            if path.suffix.lower() not in BINARY_SUFFIXES:
                unreadable.append(path.relative_to(REPO).as_posix())  # not a picture: it was not checked
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if FOUND.search(line):
                hits.append(f"{path.relative_to(REPO).as_posix()}:{n}")
    assert not hits, hits
    assert not unreadable, f"files that could not be read as text, so not checked: {unreadable}"


def test_the_placeholder_pattern_matches_what_it_is_for() -> None:
    assert FOUND.search("checked on " + MARKERS[0] + " (not")
    assert FOUND.search("# " + MARKERS[1] + ": later") and not FOUND.search("a todolist")
