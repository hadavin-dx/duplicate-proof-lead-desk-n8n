"""No tracked text file holds a raw Unicode direction control character (SonarCloud rule text:S6389).

These characters can make source code read differently from how it runs ("Trojan Source", CVE-2021-42574):
U+202A..U+202E (embeddings, overrides), U+2066..U+2069 (isolates), U+200E, U+200F and U+061C (direction marks).
A test that needs such a character builds it from an escape (\\u202e), so the file itself holds none.
"""

from __future__ import annotations

import re

from support import REPO

from tools import hygiene

_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")


def test_no_tracked_file_holds_a_raw_direction_control_character() -> None:
    listing = hygiene._git_text(REPO, "ls-files", "-z")
    offenders = []
    for rel in listing.split("\0"):
        path = REPO / rel
        if not rel or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # a binary file (image, font): not text
        offenders += [
            f"{rel}:{n}" for n, line in enumerate(text.split("\n"), 1) if _BIDI.search(line)
        ]  # split on \n only: splitlines() also cuts at U+2028
    assert not offenders, f"raw direction control characters in: {offenders}"


def test_the_direction_control_pattern_sees_each_character() -> None:
    for code in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F, 0x061C):
        assert _BIDI.search("a" + chr(code) + "b"), hex(code)
    assert not _BIDI.search("plain text \u200d joiner and \u200b space are not direction controls")
