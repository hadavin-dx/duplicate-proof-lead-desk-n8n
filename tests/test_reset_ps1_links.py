"""scripts/reset.ps1 must judge the database folder by its REAL path (a symlink or a Windows junction inside data/ that
points elsewhere must be refused), like scripts/reset.sh does with realpath. NOT run here: PowerShell, Windows, a real
Windows junction and the Windows CI job. What runs: the Python check that reset.ps1 hands to `python -c` is cut out of
the file and run on a POSIX symlink."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _strip_comments(text: str) -> str:
    text = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)  # block comments
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))  # whole-line comments


ACTIONS = ("Get-Content", "Stop-Process", "Remove-Item")  # what reads a pid file, stops a program, deletes a file
# The short names PowerShell also accepts for the same three cmdlets (built in aliases, case does not matter).
ALIASES = {
    "Get-Content": ("gc", "cat", "type"),
    "Stop-Process": ("kill", "spps"),
    "Remove-Item": ("rm", "del", "ri", "rmdir", "erase", "rd"),
}


def _first_use(text: str, name: str) -> int:
    """Index of the first place where `name` is used as a command word (not inside a longer word, a variable, a
    parameter such as -Force, a dotted name or $env:name); -1 when there is none."""
    found = re.search(rf"(?<![\w$.:\-]){re.escape(name)}(?![\w\-])", text, flags=re.IGNORECASE)
    return found.start() if found else -1


def _check_code(text: str | None = None) -> str:
    code = _strip_comments((REPO / "scripts" / "reset.ps1").read_text(encoding="utf-8") if text is None else text)
    # the old guard compared text only: [IO.Path]::GetFullPath / IsPathRooted / StartsWith do not follow a link
    assert not re.search(r"GetFullPath|IsPathRooted", code), "reset.ps1 still compares paths that were not resolved"
    found = re.search(r'python\s+-c\s+"(?P<code>[^"]*)"\s+\$dataDir\b', code)
    assert found, "reset.ps1 does not hand the database folder to a python real-path check"
    snippet = found.group("code")
    assert re.search(r"os\.path\.realpath\(\s*sys\.argv\[1\]\s*\)", snippet), "the folder is not resolved with realpath"
    assert "startswith" in snippet, "the check does not compare against the data folder"
    # the result of that check must decide: anything but True is refused with exit code 2 before anything is stopped
    after = code[found.end() :]
    refusal = re.search(r"if\s*\(.*\$inside.*\)\s*\{[^}]*exit\s+2", after, flags=re.DOTALL)
    assert refusal, "no refusal on a failed check"
    for action in ACTIONS:  # the refusal comes before the first read of a pid file, the first stop and the first delete
        assert action in after, f"{action} is not in reset.ps1: the order check would prove nothing"
        assert after.index(action) > refusal.end(), f"{action} comes before the refusal on a failed check"
    # the same for the short names, and from the top of the file (not only after the check): none may come earlier
    before = re.sub(r"'[^']*'|\"[^\"]*\"", '""', code[: found.end() + refusal.end()])  # text in quotes is not a command
    for action in ACTIONS:
        for name in (action, *ALIASES[action]):
            assert _first_use(before, name) == -1, f"{name} comes before the refusal on a failed check ({action})"
    return snippet


def _inside(folder: Path, cwd: Path) -> str:
    done = subprocess.run(  # noqa: S603 (our own snippet and a path made by the test)
        [sys.executable, "-c", _check_code(), str(folder)], cwd=cwd, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def test_the_real_path_check_accepts_a_folder_inside_data_and_refuses_a_link_out_of_it(tmp_path: Path) -> None:
    (tmp_path / "data" / "run").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert _inside(tmp_path / "data" / "run", tmp_path) == "True"
    assert _inside(Path("data") / "run", tmp_path) == "True"
    try:
        os.symlink(outside, tmp_path / "data" / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this computer cannot make a symbolic link")
    assert _inside(tmp_path / "data" / "link", tmp_path) == "False"
    assert _inside(outside, tmp_path) == "False"


def test_a_comment_that_names_the_old_call_does_not_fail_the_text_check() -> None:
    sample = "# GetFullPath is not used any more\n<# GetFullPath\n IsPathRooted #>\nx = 1\n"
    assert not re.search(r"GetFullPath|IsPathRooted", _strip_comments(sample))
    assert re.search(r"GetFullPath", _strip_comments("x = [IO.Path]::GetFullPath($p)  \n"))  # real code is still seen


def test_the_refusal_must_come_before_every_read_stop_and_delete() -> None:
    """The order check sees each of the three actions placed before the refusal (a change that moves one of them up)."""
    text = (REPO / "scripts" / "reset.ps1").read_text(encoding="utf-8")
    _check_code(text)  # the real file is in order
    marker = 'if ("$inside".Trim()'
    assert marker in text
    for action in ACTIONS:
        early = text.replace(marker, f"{action} -Path x{chr(10)}{marker}", 1)
        with pytest.raises(AssertionError, match=f"{action} comes before the refusal"):
            _check_code(early)


@pytest.mark.parametrize("name", [a for names in ALIASES.values() for a in names])
def test_a_short_name_of_a_read_stop_or_delete_before_the_refusal_is_seen_too(name: str) -> None:
    """kill, rm, del, cat, gc, spps, ri ... do the same as the full cmdlet names: the order check must see them.

    This is a LINT-STYLE ORDER GUARD, not a security boundary: it reads text and cannot run PowerShell. Known bypasses
    it does not see: a name built at run time (&('Stop-'+'Process'), &$cmd), Invoke-Expression, a .NET call such as
    [Diagnostics.Process]::Kill() or .Kill(), the program taskkill (or any other program), a command word inside quotes
    (the check blanks quoted text so that a message is not mistaken for a command), and a name this list does not hold.
    What really protects the data folder is the real-path check that runs before the refusal (tested above on a
    symlink)."""
    text = (REPO / "scripts" / "reset.ps1").read_text(encoding="utf-8")
    marker = 'if ("$inside".Trim()'
    for variant in (name, name.upper()):  # PowerShell does not care about case
        early = text.replace(marker, f"{variant} x{chr(10)}{marker}", 1)
        with pytest.raises(AssertionError, match=f"(?i){variant} comes before the refusal"):
            _check_code(early)
    late = text.replace(marker, f"# {name} x{chr(10)}{marker}", 1)  # only in a comment: not code, not a fault
    _check_code(late)


def test_the_short_name_check_does_not_mistake_parts_of_other_words_for_a_command() -> None:
    text = (REPO / "scripts" / "reset.ps1").read_text(encoding="utf-8")
    marker = 'if ("$inside".Trim()'
    harmless = "$rm = 1; $kill = 2; Write-Host 'a cat and a del and a gc'; -rm; x.del; $env:cat"
    _check_code(text.replace(marker, harmless + chr(10) + marker, 1))
