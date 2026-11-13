"""The published n8n comparison (demo_only/n8n_evidence): the tables and the cited numbers follow from the raw files."""

from __future__ import annotations

import collections
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support import REPO

from tools import hygiene
from tools.scrub_n8n_export import scrub

FOLDER = REPO / "demo_only" / "n8n_evidence"
RUN = FOLDER / "published_run"
if not FOLDER.exists():  # the demo-only folder is removable: without it these tests have nothing to check
    pytest.skip("demo_only/n8n_evidence is not present", allow_module_level=True)

GATE_SHA = "c4418825328627719f1b85f6b4c6e5fea7fea0c3e61f2bbacea1efa7be8178ec"  # recorded on 2026-11-09; the file itself is not published


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


def _load(name: str):  # the harness is not a package (frozen scripts), so load the two helper modules by path
    spec = importlib.util.spec_from_file_location("n8n_evd_" + name, FOLDER / "harness" / (name + ".py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


summarise = _load("summarise")
sums = _load("sums")


def _rows(folder: Path, name: str) -> list[dict]:
    return [json.loads(x) for x in (folder / (name + ".ndjson")).read_text(encoding="utf-8").splitlines() if x.strip()]


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _tables_problems(folder: Path) -> list[str]:
    got = summarise.summarise(folder)
    return (
        []
        if _norm(got) == _norm((FOLDER / "tables.md").read_text(encoding="utf-8"))
        else ["tables.md differs from the tables generated from the raw files"]
    )


def _distribution_problems(folder: Path) -> list[str]:
    """The numbers the post cites, counted straight from the raw rows (not through summarise)."""
    bad: list[str] = []

    def one(name: str, rows: list[dict], want: dict) -> None:
        got = dict(collections.Counter(rows))
        if got != want:
            bad.append(f"{name}: {got} != {want}")

    def g1(v: str, setting: str) -> list[dict]:
        return [r for r in _rows(folder, f"g12_{v}_{setting}") if summarise.valid(r, v)]

    a = g1("a", "default")
    one(
        "a ten at once",
        [(r["after_run1"]["crm"], r["after_run1"]["outbox"]) for r in a],
        {(1, 1): 18, (8, 8): 1, (3, 3): 1},
    )
    for v, setting in (("b", "default"), ("c", "default"), ("d", "limit1")):
        rs = _rows(folder, f"g12_{v}_{setting}")
        one(f"{v} ten at once", [(r["after_run1"]["crm"], r["after_run1"]["outbox"]) for r in rs], {(1, 1): 20})
        one(f"{v} replay added", [(r["replay_added"]["crm"], r["replay_added"]["outbox"]) for r in rs], {(0, 0): 20})
    for v in "abcd":  # the CRM request in flight (first step): the email is never sent
        rs = [r for r in _rows(folder, f"g3_{v}") if r["kill_point"] == "K3"]
        one(f"{v} crm-first kill", [(r["final"]["crm"], r["final"]["outbox"]) for r in rs], {(1, 0): 5})
    last = [r["samples"][-1] for kp in ("K3", "K2") for r in _rows(folder, f"g3e_{kp}")]
    one("e emails at 60 s (2 or more)", [s["outbox"] >= 2 for s in last], {True: 10})
    one("e CRM rows at 60 s (2 or more)", [s["crm"] >= 2 for s in last], {True: 4, False: 6})
    one("e ten at once", [(r["counts"]["crm"], r["counts"]["outbox"]) for r in _rows(folder, "g12e")], {(1, 1): 5})
    lo, hi = summarise.wilson(2, 20)
    if (round(lo * 100, 1), round(hi * 100, 1)) != (2.8, 30.1):
        bad.append(f"wilson 2/20 = {lo}, {hi}")
    if round(summarise.wilson(0, 20)[1] * 100, 1) != 16.1:
        bad.append("wilson 0/20 upper bound")
    return bad


def _sums_problems(root: Path) -> list[str]:
    bad = sums.check((FOLDER / "SHA256SUMS").read_text(encoding="utf-8"), root)
    original = FOLDER / "gate-rule-original.md"  # withheld from the public copy; checked only when present
    if original.exists() and hashlib.sha256(original.read_bytes()).hexdigest() != GATE_SHA:
        bad.append("gate-rule-original.md does not match the recorded SHA-256")
    recorded = (FOLDER / "gate-rule.sha256.txt").read_text(encoding="utf-8")
    if GATE_SHA not in recorded or not re.search(r"[0-9a-f]{64}", recorded):
        bad.append("gate-rule.sha256.txt does not hold the recorded SHA-256 (64 hex characters)")
    return bad


def test_tables_equal_the_generated_tables() -> None:
    assert not _tables_problems(RUN)
    for v, setting in (
        ("a", "default"),
        ("b", "default"),
        ("c", "default"),
        ("d", "limit1"),
    ):  # valid files: every run valid by recomputation
        assert all(summarise.valid(r, v) for r in _rows(RUN, f"g12_{v}_{setting}")), v


def test_cited_numbers_are_hard_coded() -> None:
    assert not _distribution_problems(RUN)


def test_published_files_match_sums() -> None:
    assert not _sums_problems(FOLDER)


def test_workflows_are_scrubbed() -> None:
    files = sorted((FOLDER / "workflows").glob("*.json"))
    assert len(files) == 6
    for p in files:
        wf = json.loads(p.read_text(encoding="utf-8"))
        assert scrub(wf) == wf, p.name


def test_an_edited_raw_file_fails_the_checks(tmp_path: Path) -> None:
    """Negative case: change one count in a raw file of a copy; tables, cited numbers and sums must all notice."""
    copy = tmp_path / "published_run"
    shutil.copytree(RUN, copy)
    target = copy / "g12_b_default.ndjson"
    rows = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines()]
    rows[0]["after_run1"]["outbox"] = 2
    target.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert _tables_problems(copy)
    assert _distribution_problems(copy)
    sums_root = tmp_path / "all"
    shutil.copytree(FOLDER, sums_root, ignore=shutil.ignore_patterns("harness"))
    shutil.copyfile(target, sums_root / "published_run" / "g12_b_default.ndjson")
    assert any("g12_b_default" in p for p in sums.check((FOLDER / "SHA256SUMS").read_text(encoding="utf-8"), sums_root))


def test_hygiene_exceptions_are_narrow(tmp_path: Path) -> None:
    """The exceptions added for this folder cover only its own files; the same values elsewhere are still reported."""
    h64 = "a1b2c3d4e5f6a7b8" * 4
    lock_url = "https://" + "registry" + ".npmjs" + ".org/x/-/x-1.0.0.tgz"
    files = {
        "demo_only/n8n_evidence/SHA256SUMS": h64,  # allowed
        "demo_only/n8n_evidence/package-lock.json": lock_url,  # allowed
        "demo_only/n8n_evidence/harness/other.py": h64,  # reported: hash outside the two sum files
        "demo_only/n8n_evidence/README.md": lock_url,  # reported: a link outside the lock file
        "demo_only/other/SHA256SUMS": h64,  # reported: another folder
        "docs/package-lock.json": lock_url,  # reported: another folder
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text + chr(10), encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    left = hygiene.pipeline(tmp_path, REPO / "patterns.txt", REPO / "exceptions.txt")
    assert {ln.split(":", 1)[0] for ln in left} == {
        "demo_only/n8n_evidence/harness/other.py",
        "demo_only/n8n_evidence/README.md",
        "demo_only/other/SHA256SUMS",
        "docs/package-lock.json",
    }, left


def test_gate_rule_hash_checked_if_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The original rule file is withheld from the public copy: absent is fine, present with another content is a problem."""
    copy = tmp_path / "folder"
    shutil.copytree(FOLDER, copy, ignore=shutil.ignore_patterns("harness"))
    monkeypatch.setattr(sys.modules[__name__], "FOLDER", copy)
    assert not [p for p in _sums_problems(copy) if "gate-rule" in p]
    (copy / "gate-rule-original.md").write_text("changed", encoding="utf-8")
    assert any("gate-rule-original" in p for p in _sums_problems(copy))
