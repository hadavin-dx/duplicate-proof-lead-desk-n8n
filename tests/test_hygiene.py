"""Hygiene tests; the mutation guards are in test_mutants.py. `tools/hygiene.py` also has a word-list scan
(`scan_repo`); the tests here use an invented word list."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tomllib
import unicodedata
import warnings
from pathlib import Path

import pytest
from support import REPO

from tools import hygiene

SRC = REPO / "src"
GITLEAKS_ENV = "GITLEAKS_BIN"
FILL_TOKEN = "<<" + "FILL"
REQUIRE_SCAN_ENV = "DPLD_REQUIRE_SCAN"  # when set (CI), a missing gitleaks program fails instead of skipping


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return p.stdout


def test_no_demo_only_outside_loader() -> None:
    """src/ may name demo_only only in loader.py; tests/, scripts/, conftest.py, README and docs may."""
    offenders = [
        str(p.relative_to(REPO))
        for p in sorted(SRC.rglob("*"))
        if p.is_file()
        and p.suffix in (".py", ".sql", ".md", ".txt", ".toml")
        and p.name != "loader.py"
        and "demo_only" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not offenders, f"src/ files that name demo_only (only loader.py may): {offenders}"
    assert "demo_only" in (SRC / "lead_desk" / "loader.py").read_text(encoding="utf-8")  # the rule is not vacuous


# Words the portal files must not hold: they belong to the demo extension (demo_only/portal_demo.*). The
# portal is judged on what it SAYS about its own data source, so the product files stay free of demo vocabulary,
# of absolute links (the page loads nothing from outside) and of inline data URLs or XML namespaces.
PORTAL_BANNED = ("fake", "simulated", "made-up", "recorded", "canned", "demo_only", "http:", "https:", "data:", "xmlns")
PORTAL_PRODUCT_API = SRC / "lead_desk" / "portal_api.py"
PORTAL_DIR = SRC / "lead_desk" / "portal"


def _portal_offenders(texts: dict[str, str]) -> list[str]:
    return sorted(f"{where}: {word}" for where, text in texts.items() for word in PORTAL_BANNED if word in text.lower())


def test_portal_product_files_hold_no_demo_words() -> None:
    """Deleting demo_only/ must leave an honest portal: its files name no fake system, no recorded reply,
    no outside link. (The page files come from the front-end package; the JSON module is checked here too.)"""
    files = [
        PORTAL_PRODUCT_API,
        *(p for p in sorted(PORTAL_DIR.rglob("*")) if p.is_file() and p.suffix != ".png"),
    ]  # icon.png is binary
    texts = {str(p.relative_to(REPO)): p.read_text(encoding="utf-8") for p in files}
    assert str(PORTAL_PRODUCT_API.relative_to(REPO)) in texts  # the check is not vacuous
    assert not _portal_offenders(texts), f"demo words in product portal files: {_portal_offenders(texts)}"
    planted = {"x.js": " ".join(w.upper() for w in PORTAL_BANNED)}  # each word, in capitals: all must be found
    assert [o.split(": ")[1] for o in _portal_offenders(planted)] == sorted(PORTAL_BANNED)


def _control_chars(text: str) -> list[str]:
    """Control characters (Unicode category Cc) other than tab, line feed and carriage return, as code points."""
    return [f"U+{ord(c):04X}" for c in text if c not in "\t\n\r" and unicodedata.category(c) == "Cc"]


def test_no_control_characters_in_markdown() -> None:
    """A stray backspace or escape in a .md file (for example from a backslash escape that was read as a control
    code) hides text. Tracked Markdown may hold tab, line feed and carriage return only."""
    names = [n for n in _git(REPO, "ls-files", "*.md").splitlines() if n and (REPO / n).is_file()]
    assert names, "no Markdown files found: the test would prove nothing"
    found = {n: _control_chars((REPO / n).read_text(encoding="utf-8")) for n in names}
    bad = {n: c for n, c in found.items() if c}
    assert not bad, f"control characters in Markdown files: {bad}"
    assert _control_chars("a\tb\r\nc") == [] and _control_chars("a\x08b\x1b\x00\x7f") == [
        "U+0008",
        "U+001B",
        "U+0000",
        "U+007F",
    ]


def test_hygiene_scan_public(tmp_path: Path) -> None:
    """Patterns + exceptions pipeline with one canary per rule, on the tree; gitleaks on tree and history (--redact)."""
    patterns, exceptions, toml = REPO / "patterns.txt", REPO / "exceptions.txt", REPO / ".gitleaks.toml"
    for f in (patterns, exceptions, toml):
        assert f.is_file(), f"missing {f.name} (this test needs it)"
    # 1. canaries: a temp repo with one planted value per kind, plus values that must be allowed
    canary = tmp_path / "canary"
    canary.mkdir()
    _git(canary, "init", "-q")
    planted = "\n".join(f"{k}: {v}" for k, v in hygiene.CANARIES.items()) + "\n"
    (canary / "planted.txt").write_text(planted, encoding="utf-8")
    (canary / "allowed.txt").write_text("\n".join(f"x {v} y" for v in hygiene.ALLOWED) + "\n", encoding="utf-8")
    _git(canary, "add", ".")
    reported = hygiene.pipeline(canary, patterns, exceptions)
    matches = [ln.split(":", 2)[2].strip() for ln in reported if ln.startswith("planted.txt:")]
    for kind, value in hygiene.CANARIES.items():
        assert any(m and m in value for m in matches), f"canary not found, rule silently stopped matching: {kind}"
    assert not [ln for ln in reported if ln.startswith("allowed.txt:")], "an allowed value was reported"
    for pat in hygiene.read_lines(patterns):  # every rule in the file must have a canary that it matches
        assert any(re.search(pat, v) for v in hygiene.CANARIES.values()), f"rule without a canary: {pat}"
    # 2. the real tree
    left = hygiene.pipeline(REPO, patterns, exceptions)
    assert not left, "hardcoded literals found (file:line:match):\n" + "\n".join(left[:50])
    # 3. gitleaks (release binary; CI installs it with a checked SHA-256)
    exe = os.environ.get(GITLEAKS_ENV) or shutil.which("gitleaks")
    if not exe and not os.environ.get(REQUIRE_SCAN_ENV):
        pytest.skip(
            f"no gitleaks program on PATH (set {GITLEAKS_ENV}); set {REQUIRE_SCAN_ENV}=1 to make this a failure"
        )
    assert exe, f"gitleaks binary not found: put it on PATH or set {GITLEAKS_ENV}"
    ids = [r["id"] for r in tomllib.loads(toml.read_text(encoding="utf-8")).get("rules", [])]
    report = tmp_path / "canary-report.json"  # reports go outside the repo
    common = ["--no-banner", "--redact", "--ignore-gitleaks-allow", "--config", str(toml)]
    fmt = ["--report-format", "json", "--report-path", str(report)]
    subprocess.run([exe, "dir", *common, *fmt, str(canary)], check=False)  # noqa: S603
    found = {x["RuleID"] for x in json.loads(report.read_text(encoding="utf-8") or "[]")}
    assert set(ids) <= found, f"gitleaks custom rules that did not fire on their canary: {sorted(set(ids) - found)}"
    for rule, key in hygiene.RULE_CANARY.items():  # one canary per rule, alone in its own file
        single = tmp_path / f"single-{rule}"
        single.mkdir()
        (single / "one.txt").write_text(f"x {hygiene.CANARIES[key]} y" + chr(10), encoding="utf-8")
        subprocess.run([exe, "dir", *common, *fmt, str(single)], check=False)  # noqa: S603
        got = {x["RuleID"] for x in json.loads(report.read_text(encoding="utf-8") or "[]")}
        assert rule in got, f"rule {rule} did not find its own canary"
    assert set(hygiene.RULE_CANARY) == set(ids), "every custom rule needs an entry in RULE_CANARY"
    # the vendor host home: allowed for host/url rules in constants.py only, never in another file, never for e-mail
    home = tmp_path / "home"
    (home / "src" / "lead_desk").mkdir(parents=True)
    host = "api." + "vendor-x" + ".com"
    mail = "a" + "@" + "b-x" + ".org"
    (home / "src" / "lead_desk" / "constants.py").write_text(
        f'U = "https://{host}"' + chr(10) + f'M = "{mail}"' + chr(10), encoding="utf-8"
    )
    (home / "other.py").write_text(f'U = "https://{host}"' + chr(10), encoding="utf-8")
    subprocess.run([exe, "dir", *common, *fmt, str(home)], check=False)  # noqa: S603
    hits = {
        (x["File"].replace(chr(92), "/").rsplit("/", 1)[-1], x["RuleID"])
        for x in json.loads(report.read_text(encoding="utf-8"))
    }
    assert hits == {
        ("other.py", "hardcoded-url"),
        ("other.py", "hardcoded-hostname"),
        ("constants.py", "hardcoded-email"),
    }, hits
    tree = tmp_path / "tree"  # the tracked files only: `dir` mode would also scan caches and virtual environments
    for rel in _git(REPO, "ls-files").splitlines():
        if (REPO / rel).is_file():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / rel, tree / rel)
    # The history scan only runs when DPLD_REQUIRE_SCAN=1 (CI). A history scan can report
    # literals that are no longer in the tree, so otherwise only the tree is scanned and a warning says so.
    scans = [("dir", tree), ("git", REPO)] if os.environ.get(REQUIRE_SCAN_ENV) else [("dir", tree)]
    if len(scans) == 1:
        warnings.warn(f"gitleaks history scan skipped: set {REQUIRE_SCAN_ENV}=1 to scan the history", stacklevel=1)
    for sub, target in scans:
        p = subprocess.run([exe, sub, *common, "--exit-code", "1", str(target)], capture_output=True, check=False)  # noqa: S603
        assert p.returncode == 0, f"gitleaks {sub} reported findings (values are redacted)"


def test_sh_blobs_no_cr() -> None:
    """Shell scripts must be stored with LF only (a CR breaks them on Linux). Reads the index, not the work tree."""
    names = [n for n in _git(REPO, "ls-files", "*.sh").splitlines() if n]
    assert names, "no shell scripts found: the test would prove nothing"
    for n in names:
        blob = subprocess.run(["git", "show", ":" + n], cwd=REPO, capture_output=True, check=True).stdout  # noqa: S603, S607
        assert 13 not in blob, f"{n} contains a carriage return in the index"


def test_exceptions_narrow(tmp_path: Path) -> None:
    """Each exception covers only its own file kind and length: the same value elsewhere must still be reported."""
    h40, h64 = "a1b2c3d4e5" * 4, "a1b2c3d4e5f6a7b8" * 4
    long_name = "test_" + "_".join(["aaaaaaaaaaaa"] * 4)  # five parts of at most 12 letters: allowed in tests/ only
    files = {
        "requirements-dev.txt": h40,  # a 40-hex id is not a hash line
        "README.md": h64,
        "src/x.py": long_name,
        "tests/t.py": "test_" + "a" * 45,  # one part over the cap
        ".github/workflows/ok.yml": h40,  # allowed
        "tests/ok.py": long_name,  # allowed
        "docs/h.md": "https://" + "github.com/" + "someone/else",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text + chr(10), encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    left = hygiene.pipeline(tmp_path, REPO / "patterns.txt", REPO / "exceptions.txt")
    reported = {ln.split(":", 1)[0] for ln in left}
    expected = {"requirements-dev.txt", "README.md", "src/x.py", "tests/t.py", "docs/h.md"}
    assert reported == expected, sorted(reported)


def _commit(repo: Path, *args: str) -> None:
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", *args)


def test_term_scan_finds_a_planted_term(tmp_path: Path) -> None:
    """A planted term is found in the tree and in the history; an allowed line is skipped in the tree only."""
    term = "zz-planted-term-9x"
    allowed_line = f"sample line {term} (allowed)"
    (tmp_path / "notes.md").write_text(f"written with {term.upper()}\n", encoding="utf-8")
    (tmp_path / "sample.txt").write_text(allowed_line + "\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _commit(tmp_path, "-m", f"add {term}")
    (tmp_path / "notes.md").write_text("clean\n", encoding="utf-8")
    _commit(tmp_path, "-am", "clean")
    hits = set(hygiene.scan_repo(tmp_path, [term]))
    assert hits == {(0, "sample.txt"), (0, "<git history>")}, hits
    left = hygiene.scan_repo(tmp_path, [term], [allowed_line])
    assert {w for _, w in left} == {"<git history>"}, left


def test_scrub_canary() -> None:
    """tools/scrub_n8n_export.py keeps an allow-list of known-safe keys; canaries in unknown keys must go too."""
    from tools.scrub_n8n_export import scrub

    wf = {
        "id": "CANARY-wf-id",
        "name": "Lead desk",
        "active": False,
        "createdAt": "CANARY-created",
        "updatedAt": "CANARY-updated",
        "versionId": "CANARY-version",
        "zzUnknownTop": "CANARY-unknown-top",
        "meta": {"instanceId": "CANARY-instance", "zzUnknownMeta": "CANARY-meta"},
        "homeProject": {"id": "CANARY-proj", "name": "CANARY-account-name", "email": "canary@example.test"},
        "pinData": {"Webhook": [{"json": {"email": "CANARY-pin"}}]},
        "tags": [{"name": "CANARY-tag"}],
        "nodes": [
            {
                "id": "CANARY-node-id",
                "webhookId": "CANARY-webhook",
                "zzUnknownNodeKey": "CANARY-unknown-node",
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [0, 0],
                "parameters": {"path": "lead"},
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 1000,
            }
        ],
        "connections": {"Webhook": {"main": [[{"node": "Next", "type": "main", "index": 0}]]}},
    }
    before = copy.deepcopy(wf)
    out = scrub(wf)
    assert wf == before, "scrub must not change its input"
    assert "CANARY" not in json.dumps(out), "a planted value survived the scrub"
    assert [n["name"] for n in out["nodes"]] == ["Webhook"]
    assert out["nodes"][0]["type"] == "n8n-nodes-base.webhook"
    assert out["nodes"][0]["parameters"] == {"path": "lead"}
    assert (out["nodes"][0]["retryOnFail"], out["nodes"][0]["maxTries"], out["nodes"][0]["waitBetweenTries"]) == (
        True,
        3,
        1000,
    )
    assert out["connections"] == wf["connections"]


def test_no_document_holds_a_placeholder_marker() -> None:
    """No document holds a placeholder marker."""
    docs = [REPO / p for p in ("README.md", "SECURITY.md", "contract.md", "versions.md")]
    docs += [*(REPO / "docs").glob("*.md"), REPO / "load_tools" / "README.md"]
    assert [d.name for d in docs if FILL_TOKEN in d.read_text(encoding="utf-8")] == []


NL = chr(10)
USES_LINE = re.compile(r"^[ ]*(?:-[ ]*)?uses:[ ]*(\S+)", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
FORBIDDEN_TRIGGERS = ("pull_request_target", "workflow_run")


def _workflow_problems(text: str) -> list[str]:
    """What is wrong with one workflow file: an Action not pinned by a full commit SHA, no top-level `permissions:`,
    a write permission at the top level, or a trigger that hands secrets to code from a pull request."""
    problems = [f"not pinned by full SHA: {u}" for u in USES_LINE.findall(text) if not PINNED.match(u)]
    lines = text.splitlines()
    top = [i for i, ln in enumerate(lines) if ln.startswith("permissions:")]
    if len(top) != 1:
        problems.append("needs exactly one top-level permissions: block")
    else:
        block = [lines[top[0]]]
        for ln in lines[top[0] + 1 :]:
            if ln.strip() and not ln.startswith((" ", "	", "#")):
                break
            block.append(ln)
        if "write" in NL.join(block):
            problems.append("top-level permissions must be read-only (raise per job)")
    problems += [f"forbidden trigger: {t}" for t in FORBIDDEN_TRIGGERS if re.search(rf"^[ ]*{t}\b", text, re.MULTILINE)]
    return problems


def test_workflows_are_pinned_and_limited() -> None:
    """Every workflow pins each Action by full SHA, has a read-only top-level permissions: block and no risky trigger;
    the rule itself is checked on bad samples, so a rule that stops matching fails here."""
    files = sorted((REPO / ".github" / "workflows").glob("*.yml"))
    assert files, "no workflows found: the test would prove nothing"
    for f in files:
        assert _workflow_problems(f.read_text(encoding="utf-8")) == [], f.name
    sha = "a" * 40
    perm = NL.join(["permissions:", "  contents: read", ""])
    good = perm + NL.join(["jobs:", "  j:", "    steps:", f"      - uses: actions/checkout@{sha} # v1", ""])
    assert _workflow_problems(good) == []
    assert _workflow_problems(good.replace(sha, "v4")) != []  # a tag, not a SHA
    assert _workflow_problems(good.replace(sha, sha[:39])) != []  # a short SHA
    assert _workflow_problems(good.replace(perm, "")) != []  # no permissions block
    assert _workflow_problems(good.replace("contents: read", "contents: write")) != []  # write at the top
    assert _workflow_problems(good.replace(perm, "permissions: write-all" + NL)) != []
    assert _workflow_problems(good + NL.join(["on:", "  pull_request_target:", ""])) != []
