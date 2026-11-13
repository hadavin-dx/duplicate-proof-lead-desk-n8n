"""Glue for the hygiene tests: git grep and a regex file do the finding;
this file only (1) runs `git grep -n -o -I -E -f patterns.txt | grep -v -E -f exceptions.txt` without needing a `grep`
program on PATH, (2) holds one canary per kind of pattern, (3) scans a repository for caller-supplied terms
(only when a term file is passed)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import quote

# Canaries are built from parts so this file does not itself trip the scan. Each must be reported by the pipeline.
CANARIES: dict[str, str] = {
    "ipv4": ".".join(["10", "20", "30", "40"]),
    "ipv6": ":".join(["2001", "db8", "85a3", "0", "0", "8a2e", "370", "7334"]),
    "url": "https://" + "private-host" + ".corp" + ".invalid/x",
    "hostname": "build01." + "acme-corp" + ".invalid",
    "short hostname": "svc" + "." + "ai",
    "internal hostname": "db" + "." + "internal",
    "new ending hostname": "svc" + "." + "xyz",
    "other github link": "https://" + "github.com/" + "someone/else",
    "windows path": "C:" + "\\" + "Users" + "\\" + "someone",
    "unix path": " /home" + "/someone/x",
    "email": "someone" + "@" + "example" + ".org",
    "hex": "0123456789abcdef" * 2,
    "token": "A" + "bcdefghijklmnopqrstuvwxyz" + "0123456789ABCD",
}
# One canary per gitleaks custom rule (rule id -> key in CANARIES); each is planted alone in its own file.
RULE_CANARY: dict[str, str] = {
    "hardcoded-ipv4": "ipv4",
    "hardcoded-url": "url",
    "hardcoded-hostname": "new ending hostname",
    "hardcoded-absolute-path": "windows path",
    "hardcoded-email": "email",
    "long-hex-token": "hex",
}

# Values that must NOT be reported (the allowed cases).
ALLOWED = [
    "127.0.0.1",
    "192.0.2.7",
    "ada@" + "example.test",
    "http://localhost:8765/leads",
    "https://github.com/gitleaks/gitleaks/releases/download/v1/x.tar.gz",
    "SELECT fu.ai_label FROM t",  # a column name after a dot is not a host name (the underscore ends the word)
    "tmp.co_worker, self.us_state",
]


def read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def git_grep(repo: Path, patterns: Path) -> list[str]:
    """`git grep -n -o -I -E -f patterns.txt` in repo (tracked files). Exit 1 = no match = empty list."""
    p = subprocess.run(  # noqa: S603
        ["git", "grep", "-n", "-o", "-I", "-E", "-f", str(patterns)],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if p.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed ({p.returncode}): {p.stderr[-300:]}")
    return p.stdout.splitlines()


def pipeline(repo: Path, patterns: Path, exceptions: Path) -> list[str]:
    """The reported lines after `| grep -v -E -f exceptions.txt` (a line is dropped if ANY exception matches)."""
    exc = [re.compile(x) for x in read_lines(exceptions)]
    return [ln for ln in git_grep(repo, patterns) if not any(e.search(ln) for e in exc)]


def variants(term: str) -> set[str]:
    """The term and the spellings a path or URL would give: escaped backslash, forward slash, URL-encoded."""
    out = {term, term.replace("\\", "\\\\"), term.replace("\\", "/"), quote(term, safe=""), quote(term, safe="/")}
    return {v.lower() for v in out if v}


ALLOW_PREFIX = "allow:"  # a term-file line "allow:<exact text>": that exact text is removed before the match


def load_term_list(path: Path) -> tuple[list[str], list[str]]:
    """(terms, allowed texts) of a term file: one entry per line, `#` lines and blank lines skipped."""
    lines = [t.strip() for t in path.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]
    allowed = [t[len(ALLOW_PREFIX) :] for t in lines if t.startswith(ALLOW_PREFIX)]
    return [t for t in lines if not t.startswith(ALLOW_PREFIX)], allowed


def find_terms(terms: list[str], texts: dict[str, str]) -> list[tuple[int, str]]:
    """(term number, where) for every term variant found; the term itself is never returned."""
    hits = []
    for i, term in enumerate(terms):
        vs = variants(term)
        for where, text in texts.items():
            low = text.lower()
            if any(v in low for v in vs):
                hits.append((i, where))
    return hits


def masked(texts: dict[str, str], allowed: list[str] | None = None) -> dict[str, str]:
    """Remove the exact allowed strings; everything else stays and is matched."""
    out = dict(texts)
    for ok in allowed or []:
        out = {k: v.replace(ok, "") for k, v in out.items()}
    return out


def _git_text(repo: Path, *args: str) -> str:
    p = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return p.stdout


def scan_repo(repo: Path, terms: list[str], allowed: list[str] | None = None) -> list[tuple[int, str]]:
    """Terms found in the tracked files and in the whole git history (`<git history>`) of repo."""
    files = [f for f in _git_text(repo, "ls-files").splitlines() if (repo / f).is_file()]
    texts = {f: (repo / f).read_text(encoding="utf-8", errors="replace") for f in files}
    texts["<git history>"] = _git_text(repo, "log", "--all", "--format=fuller", "-p")
    return find_terms(terms, masked(texts, allowed))
