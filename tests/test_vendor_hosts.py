"""A vendor's host name lives in ONE place, src/lead_desk/constants.py. Every other file imports the named value or uses
a reserved example name. The names are taken from constants.py itself, so this file holds no vendor name either."""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from lead_desk import constants

REPO = Path(__file__).resolve().parents[1]
FOLDERS = ("src", "scripts", "tests", "tools", "load_tools", "demo_only", "docs", "n8n", ".github", ".codeant")
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".example",
    ".json",
    ".js",
    ".css",
    ".html",
    ".sql",
    ".sh",
    ".ps1",
    ".txt",
    ".toml",
    ".yml",
    ".in",
    ".yaml",
    ".properties",
}


def root_files(repo: Path) -> tuple[str, ...]:
    """Every file at the top of the repository, dotfiles too: the ones git tracks (an untracked scratch note of whoever
    runs the tests is not part of the repository); every file of the folder when git is not there or says nothing."""
    try:
        done = subprocess.run(  # noqa: S603
            ["git", "ls-files", "-z"],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        tracked = [n for n in done.stdout.split("\0") if n and "/" not in n]
        if done.returncode == 0 and tracked:
            return tuple(sorted(tracked))
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):  # no git, a broken git, a non-UTF-8 name
        pass
    return tuple(sorted(p.name for p in repo.iterdir() if p.is_file()))


ROOT_FILES = root_files(REPO)


def vendor_names() -> set[str]:
    names = set(constants.HUBSPOT_HOST_SUFFIXES)
    names.add(constants.MAIL_IMAP_DEFAULT_HOST.split(".", 1)[1])  # the mail provider's domain
    names.add(constants.MAIL_SPAM_FOLDER)  # the provider's spam folder name
    names.add(str(urlsplit(constants.LIVE_DEFAULT_BASE_URL).hostname).split(".", 1)[1])  # the model provider's domain
    return names


def test_the_vendor_names_are_found_in_constants_so_the_scan_is_not_empty() -> None:
    text = (REPO / "src" / "lead_desk" / "constants.py").read_text(encoding="utf-8")
    assert len(vendor_names()) >= 4 and all(n in text for n in vendor_names())


def test_no_vendor_host_name_outside_constants_py() -> None:
    names = vendor_names()
    offenders: list[str] = []
    paths = [p for folder in FOLDERS for p in sorted((REPO / folder).rglob("*"))] + [REPO / f for f in ROOT_FILES]
    for path in paths:
        if (
            not path.is_file()
            or path.suffix not in TEXT_SUFFIXES
            or "__pycache__" in path.parts
            or path == REPO / "src" / "lead_desk" / "constants.py"
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        offenders += [f"{path.relative_to(REPO).as_posix()}: {n}" for n in sorted(names) if n.lower() in text]
    assert offenders == []


def test_root_files_are_the_tracked_ones_not_an_untracked_scratch_file(tmp_path: Path) -> None:
    git = ["git", "-c", "user.email=a@example.test", "-c", "user.name=a"]
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)  # noqa: S603
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / ".codeant.yaml").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.md").write_text("x", encoding="utf-8")
    subprocess.run([*git, "add", "."], cwd=tmp_path, check=True)  # noqa: S603
    (tmp_path / "scratch-notes.md").write_text("untracked", encoding="utf-8")
    assert root_files(tmp_path) == (".codeant.yaml", "README.md")


def test_root_files_fall_back_to_every_file_when_git_is_not_there(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "scratch-notes.md").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))  # no git on the path
    assert root_files(tmp_path) == ("README.md", "scratch-notes.md")


def test_root_files_fall_back_to_every_file_when_git_is_there_but_the_folder_is_not_a_repository(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "scratch-notes.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # never find a repository above the folder
    assert subprocess.run(["git", "rev-parse"], cwd=tmp_path, capture_output=True, check=False).returncode != 0  # noqa: S603, S607
    assert root_files(tmp_path) == ("README.md", "scratch-notes.md")


def test_root_files_fall_back_when_git_prints_a_file_name_that_is_not_utf8(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")

    def not_utf8(*args: object, **kw: object) -> object:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(subprocess, "run", not_utf8)
    assert root_files(tmp_path) == ("README.md",)
