"""No tracked text file holds a raw Unicode direction control character (SonarCloud rule text:S6389).

These characters can make source code read differently from how it runs ("Trojan Source", CVE-2021-42574):
U+202A..U+202E (embeddings, overrides), U+2066..U+2069 (isolates), U+200E, U+200F and U+061C (direction marks).
A test that needs such a character builds it from an escape (\\u202e), so the file itself holds none.
"""

from __future__ import annotations

import re
from pathlib import Path

from support import REPO

from tools import hygiene

_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")


def _offenders(repo: Path) -> list[str]:
    """`file:line` of each line with a direction control character, in each tracked text file of the repository."""
    listing = hygiene._git_text(repo, "ls-files", "-z")  # -z: a name with a newline or a non-ASCII letter stays a path
    offenders = []
    for rel in listing.split("\0"):
        path = repo / rel
        if not rel or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a binary file (image, font): not text
        offenders += [
            f"{rel}:{n}" for n, line in enumerate(text.split("\n"), 1) if _BIDI.search(line)
        ]  # split on \n only: splitlines() also cuts at U+2028
    return offenders


def test_no_tracked_file_holds_a_raw_direction_control_character() -> None:
    offenders = _offenders(REPO)
    assert not offenders, f"raw direction control characters in: {offenders}"


def test_a_tracked_file_with_a_non_ascii_name_and_a_raw_character_is_reported(tmp_path: Path) -> None:
    files = {
        "caf\u00e9_note.txt": "one" + chr(0x2028) + "x\ntwo " + chr(0x202E) + " three\n",  # U+2028 is no line end here
        "plain_note.txt": "one\ntwo\n",
        "gone_note.txt": "tracked, then deleted from the work tree\n",
    }
    hygiene._git_text(tmp_path, "init", "-q")
    for rel, text in files.items():
        (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
        hygiene._git_text(tmp_path, "add", "--", rel)
    (tmp_path / "gone_note.txt").unlink()
    (tmp_path / "binary_blob.bin").write_bytes(b"\xff\xfe\x00")
    hygiene._git_text(tmp_path, "add", "--", "binary_blob.bin")
    assert _offenders(tmp_path) == ["caf\u00e9_note.txt:2"]


def test_the_direction_control_pattern_sees_each_character() -> None:
    for code in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F, 0x061C):
        assert _BIDI.search("a" + chr(code) + "b"), hex(code)
    assert not _BIDI.search("plain text \u200d joiner and \u200b space are not direction controls")
