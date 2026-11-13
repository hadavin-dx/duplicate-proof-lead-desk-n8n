"""Run by test_codeant_hygiene on purpose (the name does not match test_*.py, so a normal run does not collect it):
a test must never see a DESK_* name that comes from the shell of the person running the tests."""

import os


def test_no_desk_name_from_the_shell_is_visible() -> None:
    assert [n for n in os.environ if n.startswith("DESK_")] == []
