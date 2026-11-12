"""A vendor's host name lives in ONE place, src/lead_desk/constants.py. Every other file imports the named value or uses
a reserved example name. The names are taken from constants.py itself, so this file holds no vendor name either."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from lead_desk import constants

REPO = Path(__file__).resolve().parents[1]
FOLDERS = ("src", "scripts", "tests", "load_tools", "demo_only", "docs", "n8n", ".github")
ROOT_FILES = (
    "README.md",
    "contract.md",
    "versions.md",
    "SECURITY.md",
    "pyproject.toml",
    "patterns.txt",
    "exceptions.txt",
)
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
}


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
