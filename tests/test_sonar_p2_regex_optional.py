"""Sonar fixes, optional patch: the two patterns that Sonar may keep flagging (S8786) although they are rewritten here:
`run_real_check._ADDRESS` and `scrub_n8n_export.MAIL`. Timing on a long text and equality with the old pattern on every
short text of a small alphabet. Made-up data only."""

from __future__ import annotations

import importlib
import itertools
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from tools import scrub_n8n_export as scrub

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
rrc = importlib.import_module("run_real_check")

LONG = 40_000  # the old pattern took seconds here
BOUND_SECONDS = 0.5
AT = "@"
DOT = "."

# the patterns as they were before the patch (the reference for the equality tests)
OLD_ADDRESS = re.compile(r"[^\s,;:<>\"']+@[^\s,;<>\"']+")
OLD_MAIL = re.compile(
    r"(?<![\w.%+-])[\w.%+-]+[@＠](?!(?:[\w-]+\.)*example\.(?:com|test|invalid)(?![\w-]|\.[\w-]))[\w.-]+"
)


def _seconds(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def _same(old: re.Pattern[str], new: re.Pattern[str], alphabet: list[str], longest: int) -> int:
    """Compare the replaced text AND every match span for every text of up to `longest` symbols."""
    seen = 0
    for n in range(longest + 1):
        for parts in itertools.product(alphabet, repeat=n):
            text = "".join(parts)
            assert old.sub("<>", text) == new.sub("<>", text), text
            assert [m.span() for m in old.finditer(text)] == [m.span() for m in new.finditer(text)], text
            seen += 1
    return seen


def test_run_real_check_address_pattern_is_linear_on_a_long_run_without_an_at_sign() -> None:
    assert _seconds(lambda: rrc._ADDRESS.sub("x", "a" * LONG)) < BOUND_SECONDS
    assert _seconds(lambda: rrc._ADDRESS.sub("x", ("a" + AT) * LONG)) < BOUND_SECONDS


def test_scrub_mail_pattern_stays_linear() -> None:
    """Honest note: this test CANNOT fail on the old pattern. No slow input for the old scrub pattern was found (300
    random searches and the structured shapes below all took under a millisecond), so the rewrite (one mandatory
    character before the repeat, possessive repeats, a guarded label in the look-ahead) was made because the static
    analyser asked for it, with no measured speed gain. This test only pins that the new pattern stays fast; the
    equivalence test below is what guards the rewrite."""
    labels = "a" + DOT
    texts = [
        "a" + AT + labels * (LONG // 2) + " ",
        ("a" + AT + labels * 200 + "example" + DOT + "com" + DOT + " ") * (LONG // 400),
        "a" + AT + ("example" + DOT) * (LONG // 8) + "x",
        ("a" + AT) * LONG,
    ]
    for text in texts:
        assert _seconds(lambda text=text: scrub.MAIL.sub("x", text)) < BOUND_SECONDS


def test_run_real_check_address_pattern_finds_the_same_addresses() -> None:
    alphabet = ["a", AT, DOT, " ", ":", ",", "\n"]
    assert _same(OLD_ADDRESS, rrc._ADDRESS, alphabet, 7) > 100_000


def test_scrub_mail_pattern_replaces_the_same_texts() -> None:
    alphabet = ["a", AT, DOT, "-", "example" + DOT, "com", "test", " ", "＠"]
    assert _same(OLD_MAIL, scrub.MAIL, alphabet, 5) > 50_000
    for text in (
        "x" + AT + "example" + DOT + "com",  # the allowed name: left alone
        "x" + AT + "a" + DOT + "example" + DOT + "invalid",  # a sub-domain of it: left alone
        "x" + AT + "example" + DOT + "com" + DOT + "evil",  # the allowed name as a front part of another: replaced
        "x" + AT + "notexample" + DOT + "com",  # a different name that ends the same: replaced
        "x" + AT + "example" + DOT + "com" + DOT,  # a trailing dot: left alone
        "first" + DOT + "last" + AT + "corp" + DOT + "test, and y" + AT + "example" + DOT + "test",
    ):
        assert OLD_MAIL.sub("<>", text) == scrub.MAIL.sub("<>", text)


def test_ordinary_addresses_still_match() -> None:
    mail = "jane" + DOT + "doe" + AT + "corp" + DOT + "test"
    assert rrc._ADDRESS.sub("<address>", "write to " + mail + ", thanks") == "write to <address>, thanks"
