"""A Semgrep suppression comment must name the rule: `# nosemgrep: <rule-id> <reason>`, never a bare `# nosemgrep`.

Every mention of `nosem` (the short form `nosem` is read by Semgrep 1.185.0 exactly like `nosemgrep`; any letters after
it, such as `nosemx`, and any case, such as `NOSEM`, are read as a suppression too, so the search is for the prefix, in
any case) in a tracked file (any comment form: `#`, `//`, `/* */`) must be `nosem` or `nosemgrep`, then a colon and a
dotted rule id such as `python.lang.x.y`. Semgrep treats a bare mention, and a colon with nothing after it, as
"hide every finding on this line"; a reason without an id is read as an id that matches no rule, so it hides nothing and
only confuses. docs/, this file and the workflow message (which describe the form) are skipped.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from support import REPO
from test_hygiene import _run

NL = chr(10)
WORD = "nosem"  # the prefix of both spellings; Semgrep also accepts letters after it and any case, so refuse those
# `noſem` (U+017F, long s): Semgrep 1.185.0 reads it as `nosem`; git grep -i folds it in some locales only
LONG_S = "no" + chr(0x17F) + "em"
WORD_RE = "no[s" + chr(0x17F) + "]em"  # the two spellings as one regular expression, for any case
TAB = chr(9)
SKIP = (":(exclude)docs", ":(exclude)tests/test_nosemgrep_form.py", ":(exclude).github/workflows/semgrep.yml")
# after `nosem`: `grep` or nothing, a colon, at most one space or tab (Semgrep 1.185.0 reads a tab after the colon
# like a space: the id after it is used), a dotted rule id, then a blank, `*/` or line end
GOOD_TAIL = re.compile(r"(grep)?:[ \t]?[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+(?=[ \t]|\*/|\r?$)")


def _mentions(root: Path) -> list[str]:
    """Every tracked line that holds the word, as `path:line:text` (git grep over the index, no absolute path)."""
    p = _run(root, "git", "grep", "-n", "-I", "-i", "-F", "-e", WORD, "-e", LONG_S, "--", ".", *SKIP, check=False)
    assert p.returncode in (0, 1), f"git grep failed (exit {p.returncode}): {p.stderr}"  # 1 = no match, >1 = an error
    return p.stdout.splitlines()


def _bad(root: Path) -> list[str]:
    """The `path:line` of every mention that does not name a rule id."""
    out = []
    for hit in _mentions(root):
        path, number, text = hit.split(":", 2)
        if not all(GOOD_TAIL.match(tail) for tail in re.split(WORD_RE, text, flags=re.IGNORECASE)[1:]):
            out.append(f"{path}:{number}")
    return out


def test_no_bare_nosemgrep_comment() -> None:
    assert _bad(REPO) == []


def _scratch_repo(root: Path, name: str, text: str) -> Path:
    _run(root, "git", "init", "-q")
    (root / name).write_bytes(text.encode("utf-8"))  # bytes: a CRLF line stays a CRLF line
    _run(root, "git", "add", "-A")
    return root


GOOD = {
    "hash-comment": ("a.py", "x = 1  # nosemgrep: python.lang.x.y because a fixed string\n"),
    "hash-no-space": ("a.py", "x = 1  # nosemgrep:python.lang.x.y reason\n"),
    "line-comment": ("a.js", "f(1);  // nosemgrep: javascript.lang.x.y reason\n"),
    "block-comment": ("a.js", "f(1);  /* nosemgrep: javascript.lang.x.y */\n"),
    "short-hash": ("a.py", "x = 1  # nosem: python.lang.x.y reason\n"),
    "short-hash-no-space": ("a.py", "x = 1  # nosem:python.lang.x.y reason\n"),
    "short-line": ("a.js", "f(1);  // nosem: javascript.lang.x.y reason\n"),
    "short-block": ("a.js", "f(1);  /* nosem: javascript.lang.x.y */\n"),
    "short-crlf": ("a.py", "x = 1  # nosem: python.lang.x.y reason\r\n"),
    "id-at-end-of-line": ("a.py", "x = 1  # nosemgrep: python.lang.x.y\n"),
    "tab-after-colon": ("a.py", "x = 1  # nosemgrep:" + TAB + "python.lang.x.y reason\n"),
    "tab-after-colon-short": ("a.py", "x = 1  # nosem:" + TAB + "python.lang.x.y reason\n"),
    "long-s-with-id": ("a.py", "x = 1  # " + LONG_S + ": python.lang.x.y reason\n"),
    "crlf-with-id": ("a.py", "x = 1  # nosemgrep: python.lang.x.y reason\r\n"),
}
LONG = "nosemgrep"
# an f-string for the shapes that end in a colon: a letter, a colon and a backslash would read as a drive path
BAD = {
    "hash-bare": ("a.py", "x = 1  # nosemgrep\n"),
    "hash-no-space-bare": ("a.py", "x = 1  #nosemgrep\n"),
    "hash-colon-empty": ("a.py", f"x = 1  # {LONG}:\n"),
    "hash-colon-space-empty": ("a.py", "x = 1  # nosemgrep: \n"),
    "hash-reason-no-id": ("a.py", "x = 1  # nosemgrep: because a fixed string\n"),
    "hash-undotted-id": ("a.py", "x = 1  # nosemgrep: justone reason\n"),
    "line-bare": ("a.js", "f(1);  // nosemgrep\n"),
    "line-no-space-bare": ("a.js", "f(1);  //nosemgrep\n"),
    "line-colon-empty": ("a.js", f"f(1);  // {LONG}:\n"),
    "block-bare": ("a.js", "f(1);  /* nosemgrep */\n"),
    "block-colon-empty": ("a.js", "f(1);  /* nosemgrep: */\n"),
    "tab-separated-bare": ("a.py", "x = 1  #\tnosemgrep\n"),
    "tab-then-text": ("a.py", "x = 1  #\tnosemgrep\treason\n"),
    "crlf-bare": ("a.py", "x = 1  # nosemgrep\r\n"),
    "crlf-colon-empty": ("a.py", f"x = 1  # {LONG}:\r\n"),
    "line-above-bare": ("a.js", "// nosemgrep\nf(1);\n"),
    "second-mention-bare": ("a.py", "x = 1  # nosemgrep: python.lang.x.y reason # nosemgrep\n"),
    # the short form `nosem` and the other spellings Semgrep 1.185.0 reads as a suppression (evidence-sg-13)
    "short-hash-bare": ("a.py", "x = 1  # nosem\n"),
    "short-hash-no-space-bare": ("a.py", "x = 1  #nosem\n"),
    "short-hash-colon-empty": ("a.py", f"x = 1  # {WORD}:\n"),
    "short-hash-colon-space-empty": ("a.py", "x = 1  # nosem: \n"),
    "short-hash-reason-no-id": ("a.py", "x = 1  # nosem: because a fixed string\n"),
    "short-hash-text-after": ("a.py", "x = 1  # nosem because a fixed string\n"),
    "short-hash-undotted-id": ("a.py", "x = 1  # nosem: justone reason\n"),
    "short-line-bare": ("a.js", "f(1);  // nosem\n"),
    "short-block-bare": ("a.js", "f(1);  /* nosem */\n"),
    "short-block-colon-empty": ("a.js", "f(1);  /* nosem: */\n"),
    "short-line-above-bare": ("a.js", "// nosem\nf(1);\n"),
    "short-tab-bare": ("a.py", "x = 1  #\tnosem\n"),
    "short-crlf-bare": ("a.py", "x = 1  # nosem\r\n"),
    "short-crlf-colon-empty": ("a.py", f"x = 1  # {WORD}:\r\n"),
    "short-equals-id": ("a.py", "x = 1  # nosem=python.lang.x.y\n"),
    "short-dash-text": ("a.py", "x = 1  # nosem-x\n"),
    "extra-letters": ("a.py", "x = 1  # nosemx\n"),
    "extra-letters-after-grep": ("a.py", "x = 1  # nosemgrepx\n"),
    "half-word": ("a.py", "x = 1  # nosemgre\n"),
    "upper-case-short": ("a.py", "x = 1  # NOSEM\n"),
    "upper-case-long": ("a.py", "x = 1  # NOSEMGREP\n"),
    "mixed-case-short": ("a.py", "x = 1  # NoSem\n"),
    "words-before-bare": ("a.py", "x = 1  # see nosem docs\n"),
    # Unicode: Semgrep 1.185.0 reads U+017F (long s) as `s`; a zero-width or no-break space did not suppress
    "long-s-bare": ("a.py", "x = 1  # " + LONG_S + "\n"),
    "long-s-grep-bare": ("a.py", "x = 1  # " + LONG_S + "grep\n"),
    "long-s-upper-bare": ("a.py", "x = 1  # NO" + chr(0x17F) + "EM\n"),
    "long-s-colon-empty": ("a.py", "x = 1  # " + LONG_S + ":\n"),
    "long-s-second-mention-bare": ("a.py", "x = 1  # nosem: python.lang.x.y reason # " + LONG_S + "\n"),
    "short-second-mention-bare": ("a.py", "x = 1  # nosem: python.lang.x.y reason # nosem\n"),
}


@pytest.mark.parametrize("name", sorted(GOOD))
def test_form_with_rule_id_is_accepted(tmp_path: Path, name: str) -> None:
    file, text = GOOD[name]
    assert _bad(_scratch_repo(tmp_path, file, text)) == []


@pytest.mark.parametrize("name", sorted(BAD))
def test_form_without_rule_id_is_found(tmp_path: Path, name: str) -> None:
    """Mutants: each shape of a suppression without a usable rule id must be reported."""
    file, text = BAD[name]
    assert _bad(_scratch_repo(tmp_path, file, text)) == [f"{file}:1"]


def test_git_error_is_not_read_as_clean(tmp_path: Path) -> None:
    """A folder that is not a git repository makes `git grep` fail (exit above 1): an error, not 'no match'."""
    with pytest.raises(AssertionError, match="git grep failed"):
        _mentions(tmp_path)


CANCEL_LINE = re.compile(r"^[ ]*cancel-in-progress:[ ]*(.+?)[ ]*$", re.MULTILINE)
GROUP_LINE = re.compile(r"^[ ]*group:[ ]*(.+?)[ ]*$", re.MULTILINE)
CONCURRENCY_LINE = re.compile(r"^[ ]*concurrency:", re.MULTILINE)
CANCEL_ONLY_PR = "${{ github.event_name == 'pull_request' }}"
GROUP_PER_REF = "semgrep-${{ github.ref }}"


def _concurrency_unsafe(text: str) -> bool:
    """True unless the workflow has exactly one top-level `concurrency:` block (no job-level one) whose group is exactly
    the per-ref name and whose `cancel-in-progress:` is exactly the pull-request-only expression. A constant group would
    let a pull-request run cancel a run on main in the same group; `true` or an extra clause would cancel pushes."""

    def values(rx: re.Pattern[str]) -> list[str]:
        return [v.split(" #")[0].strip() for v in rx.findall(text)]

    top = re.findall(r"^concurrency:", text, re.MULTILINE)
    return not (
        len(CONCURRENCY_LINE.findall(text)) == 1
        and len(top) == 1
        and values(GROUP_LINE) == [GROUP_PER_REF]
        and values(CANCEL_LINE) == [CANCEL_ONLY_PR]
    )


def test_semgrep_workflow_does_not_cancel_a_running_push_to_main() -> None:
    """Lead decision: per-ref group, cancel only for pull-request runs; checked on the file and on bad samples.
    (GitHub may still replace a PENDING run of the same group; not measured, so not tested.)"""
    text = (REPO / ".github" / "workflows" / "semgrep.yml").read_text(encoding="utf-8")
    assert not _concurrency_unsafe(text)
    nl = chr(10)
    good = nl.join(["concurrency:", f"  group: {GROUP_PER_REF}", f"  cancel-in-progress: {CANCEL_ONLY_PR}", ""])
    assert not _concurrency_unsafe(good)
    assert _concurrency_unsafe(good.replace(CANCEL_ONLY_PR, "true"))  # unconditional cancel: the mutant
    assert _concurrency_unsafe(good.replace(CANCEL_ONLY_PR, CANCEL_ONLY_PR[:-3] + " || true }}"))  # extra clause
    assert _concurrency_unsafe(good.replace("pull_request", "push"))
    assert _concurrency_unsafe(good.replace(GROUP_PER_REF, "semgrep"))  # constant group: a PR run cancels main
    assert _concurrency_unsafe(good.replace(GROUP_PER_REF, "semgrep-${{ github.sha }}"))  # no github.ref
    assert _concurrency_unsafe(good + good)  # a second concurrency block
    assert _concurrency_unsafe(good + nl.join(["jobs:", "  j:", "    concurrency:", "      group: x", ""]))  # job level
    assert _concurrency_unsafe(nl.join(["concurrency:", f"  group: {GROUP_PER_REF}", ""]))  # no cancel line


# --- what the workflow must never do (each rule below was a surviving mutant of an earlier version of this file) ---
WORKFLOW = REPO / ".github" / "workflows" / "semgrep.yml"
SCAN_FLAGS = ("--strict", "--error", "--severity ERROR", "--metrics=off")
FOLDERS = ["src", "scripts", "load_tools", "tools", "n8n"]
MAX_MINUTES = 20
FORBIDDEN_VALUE = re.compile(
    r"secrets\.|secrets\[|github\.token|GITHUB_TOKEN"
)  # a secret or the token in any value or name


def _strings(node: Any) -> list[str]:
    """Every key and every string value of a parsed YAML document."""
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in [str(k), *_strings(v)]]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return [node] if isinstance(node, str) else []


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in doc["jobs"].values() for step in job.get("steps") or []]


def _scan_run_block(text: str) -> str:
    blocks = [str(s["run"]) for s in _steps(yaml.safe_load(text)) if "semgrep scan" in str(s.get("run", ""))]
    assert len(blocks) == 1, "expected exactly one step that runs `semgrep scan`"
    return blocks[0]


def _scan_words(text: str) -> list[str]:
    """The words of the `semgrep scan` command line, up to the `&&` that follows it."""
    line = next(ln for ln in _scan_run_block(text).splitlines() if ln.strip().startswith("semgrep scan"))
    words = shlex.split(line)
    return words[: words.index("&&")] if "&&" in words else words


# the whole scan command, word for word: a flag that is not here (also in the glued `--x=y` form) is refused
SCAN_WORDS = [
    *("semgrep", "scan", "--metrics=off", "--disable-version-check", "--strict", "--error", "--severity", "ERROR"),
    *("--config", "p/python", "--config", "p/javascript", *FOLDERS),
]
INSTALL = "python -m pip install --require-hashes --only-binary=:all: --no-deps -r requirements-semgrep.txt"
CHECK_INSTALL = NL.join(["python -m pip check", "semgrep --version --disable-version-check", ""])
TRIGGERS = {"pull_request": {"branches": ["main"]}, "push": {"branches": ["main"]}}
CONCURRENCY = {"group": GROUP_PER_REF, "cancel-in-progress": CANCEL_ONLY_PR}


def _pinned_uses(name: str) -> str:
    """The `uses:` value of an action as lint.yml pins it (the same full commit id)."""
    lint = (REPO / ".github" / "workflows" / "lint.yml").read_text(encoding="utf-8")
    found = re.findall(rf"uses: ({re.escape(name)}@[0-9a-f]{{40}})\b", lint)
    assert len(set(found)) == 1, f"lint.yml does not pin {name} to one commit id"
    return found[0]


def _whitelist_problems(text: str) -> list[str]:
    """Everything in the workflow that is not exactly the expected shape (a whitelist: an unknown key, step, trigger or
    flag is a problem, so is a missing one)."""
    doc = yaml.safe_load(text)
    out: list[str] = []

    def exact(what: str, got: Any, want: Any) -> None:
        if got != want:
            out.append(f"{what}: got {got!r}, want {want!r}")

    exact(
        "top-level keys", sorted(map(str, doc)), ["True", "concurrency", "jobs", "name", "permissions"]
    )  # True is `on`
    exact("on", doc.get(True, doc.get("on")), TRIGGERS)  # PyYAML reads the key `on` as True
    exact("permissions", doc.get("permissions"), {"contents": "read"})
    exact("concurrency", doc.get("concurrency"), CONCURRENCY)
    jobs = doc.get("jobs") or {}
    exact("jobs", sorted(jobs), ["semgrep"])
    job = jobs.get("semgrep") or {}
    exact("job keys", sorted(job), ["runs-on", "steps", "timeout-minutes"])
    exact("runs-on", job.get("runs-on"), "ubuntu-24.04")
    minutes = job.get("timeout-minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 < minutes <= MAX_MINUTES:
        out.append(f"timeout-minutes: {minutes!r}")
    steps = job.get("steps") or []
    exact("number of steps", len(steps), 5)
    if len(steps) == 5:
        checkout, setup, install, check, scan = steps
        exact(
            "checkout step",
            checkout,
            {"uses": _pinned_uses("actions/checkout"), "with": {"persist-credentials": False}},
        )
        exact(
            "setup-python step",
            setup,
            {"uses": _pinned_uses("actions/setup-python"), "with": {"python-version": "3.12"}},
        )
        exact("install step keys", sorted(install), ["name", "run"])
        exact("install command", install.get("run"), INSTALL)
        exact("check step keys", sorted(check), ["name", "run"])
        exact("check command", check.get("run"), CHECK_INSTALL)
        exact("scan step keys", sorted(scan), ["env", "name", "run"])
        exact("scan env", scan.get("env"), {"RETRY_PAUSE_SECONDS": "20"})
        scan_lines = [ln for ln in str(scan.get("run")).splitlines() if "semgrep scan" in ln]
        exact("lines with `semgrep scan`", len(scan_lines), 1)
        if scan_lines:
            words = shlex.split(scan_lines[0])
            exact("scan command", words[: words.index("&&")] if "&&" in words else words, SCAN_WORDS)
            exact("after the scan command", words[len(SCAN_WORDS) :], ["&&", "status=0", "||", "status=$?"])
    return out


def _workflow_problems(text: str) -> list[str]:
    """What is wrong with the workflow text: a checkout that keeps credentials, text a pull request controls inside a
    `run:` step, a secret or the token anywhere, `continue-on-error`, permissions beyond `contents: read` at any level,
    a runner or time limit other than the allowed ones, an install without `--no-deps`, or a scan command that lost a
    flag, names another pack or does not name exactly the five folders."""
    doc = yaml.safe_load(text)
    out = _whitelist_problems(text)
    for step in _steps(doc):
        if (
            str(step.get("uses", "")).startswith("actions/checkout@")
            and (step.get("with") or {}).get("persist-credentials") is not False
        ):
            out.append("a checkout step without persist-credentials: false")
        if re.search(r"\$\{\{[^}]*\bgithub\.(event\.|head_ref|base_ref|ref_name|actor)", str(step.get("run", ""))):
            out.append("a run: step holds text that a pull request controls (github.event.*, head_ref, ref_name ...)")
        if "continue-on-error" in step:
            out.append("a step has continue-on-error: the job could go green on findings")
        if "pip install" in str(step.get("run", "")) and "--no-deps" not in str(step["run"]):
            out.append("the install lost --no-deps")
    out += [f"forbidden reference: {s}" for s in _strings(doc) if FORBIDDEN_VALUE.search(s)]
    if doc.get("permissions") != {"contents": "read"}:
        out.append("top-level permissions are not exactly contents: read")
    for name, job in doc["jobs"].items():
        if "permissions" in job:
            out.append(f"job {name} has its own permissions block")
        if "continue-on-error" in job:
            out.append(f"job {name} has continue-on-error")
        if job.get("runs-on") != "ubuntu-24.04":
            out.append(f"job {name} runs on {job.get('runs-on')!r}, not ubuntu-24.04")
        if not isinstance(job.get("timeout-minutes"), int) or job["timeout-minutes"] > MAX_MINUTES:
            out.append(f"job {name} has no timeout of at most {MAX_MINUTES} minutes")
    words = _scan_words(text)
    scan = " ".join(words)
    out += [f"the scan command has lost {flag}" for flag in SCAN_FLAGS if f" {flag}" not in f" {scan} "]
    packs = [w for prev, w in zip(words, words[1:], strict=False) if prev == "--config"]
    if packs != ["p/python", "p/javascript"]:
        out.append(f"the scan names these packs: {packs}")
    folders = [
        w
        for prev, w in zip(["", *words], words, strict=False)
        if not w.startswith("-") and prev not in ("--config", "--severity")
    ]
    if folders[2:] != FOLDERS:  # the first two words are `semgrep scan`
        out.append(f"the scan names these folders: {folders[2:]}")
    return out


def test_semgrep_workflow_has_no_known_hazard() -> None:
    assert _workflow_problems(WORKFLOW.read_text(encoding="utf-8")) == []


def _mutants() -> dict[str, tuple[str, str]]:
    """name -> (text in the real workflow, replacement): the surviving mutants of the earlier review, one by one."""
    job = "    runs-on: ubuntu-24.04"
    step_env = NL.join(["        env:", "          RETRY"])
    scan_tail = "--config p/javascript src"
    setup = 'python-version: "3.12"'
    install = "Install Semgrep (pinned version, hashes are checked)"
    last = 'exit "$status"' + NL
    extra = {  # the changes a reviewer tried that the first, blacklist version of this test did not see
        "step if skips the scan": (step_env, "        if: github.event_name != 'pull_request'" + NL + step_env),
        "job if false": (job, job + NL + "    if: false"),
        "extra action step": (
            "      - name: " + install,
            "      - uses: actions/cache@" + "0" * 40 + NL + NL + "      - name: " + install,
        ),
        "glued --config=": (scan_tail, "--config p/javascript --config=./other.yml src"),
        "glued --exclude=": (scan_tail, "--config p/javascript --exclude=src src"),
        "short -c": (scan_tail, "--config p/javascript -c ./other.yml src"),
        "--exclude-rule": (scan_tail, "--config p/javascript --exclude-rule x src"),
        "default shell sh": (
            NL.join(["permissions:", "  contents: read"]),
            NL.join(["defaults:", "  run:", "    shell: sh", "", "permissions:", "  contents: read"]),
        ),
        "step shell sh": (step_env, "        shell: sh" + NL + step_env),
        "step working-directory": (step_env, "        working-directory: tools" + NL + step_env),
        "step timeout 1": (step_env, "        timeout-minutes: 1" + NL + step_env),
        "matrix": (job, job + NL + NL.join(["    strategy:", "      matrix:", "        x: [1]"])),
        "pip cache": (setup, setup + NL + "          cache: pip"),
        "python 3.9": (setup, 'python-version: "3.9"'),
        "job env": (job, job + NL + NL.join(["    env:", "      SEMGREP_RULES: p/ci"])),
        "container": (job, job + NL + "    container: example/img:latest"),
        "push to another branch": (
            NL.join(["  push:", "    branches: [main]"]),
            NL.join(["  push:", "    branches: [dev]"]),
        ),
        "pull request to no branch": (
            NL.join(["  pull_request:", "    branches: [main]"]),
            NL.join(["  pull_request:", "    branches: [nothing]"]),
        ),
        "manual trigger": (
            NL.join(["on:", "  pull_request:"]),
            NL.join(["on:", "  workflow_dispatch:", "  pull_request:"]),
        ),
        "install hidden by || true": ('-r requirements-semgrep.txt"', '-r requirements-semgrep.txt || true"'),
        "second job": (
            last,
            last
            + NL.join(
                [
                    "",
                    "  second:",
                    "    needs: semgrep",
                    "    runs-on: ubuntu-24.04",
                    "    steps:",
                    "      - run: echo hi",
                    "",
                ]
            ),
        ),
    }
    return {
        **extra,
        "persist-credentials true": ("persist-credentials: false", "persist-credentials: true"),
        "persist-credentials removed": ("          persist-credentials: false\n", ""),
        "pull request title in a run step": ('echo "semgrep $(', 'echo "${{ github.event.pull_request.title }} $('),
        "head_ref in a run step": ('echo "semgrep $(', 'echo "${{ github.head_ref }} $('),
        "ref_name in a run step": ('echo "semgrep $(', 'echo "${{ github.ref_name }} $('),
        "base_ref in a run step": ('echo "semgrep $(', 'echo "${{ github.base_ref }} $('),
        "--no-deps removed": ("--only-binary=:all: --no-deps -r", "--only-binary=:all: -r"),
        "continue-on-error on the scan step": (
            "        env:" + NL + "          RETRY",
            "        continue-on-error: true" + NL + "        env:" + NL + "          RETRY",
        ),
        "continue-on-error on the job": (
            "    runs-on: ubuntu-24.04",
            "    runs-on: ubuntu-24.04\n    continue-on-error: true",
        ),
        "extra pack": ("--config p/javascript src", "--config p/javascript --config p/ci src"),
        "self-hosted runner": ("runs-on: ubuntu-24.04", "runs-on: self-hosted"),
        "--metrics=off removed": ("--metrics=off --disable-version-check --strict", "--disable-version-check --strict"),
        "folder tools dropped": ("load_tools tools n8n &&", "load_tools n8n &&"),
        "job-level id-token write": (
            "    runs-on: ubuntu-24.04",
            "    runs-on: ubuntu-24.04" + NL + "    permissions:" + NL + "      id-token: write",
        ),
        "top-level write": ("permissions:" + NL + "  contents: read", "permissions:" + NL + "  contents: write"),
        "timeout 360": ("timeout-minutes: 20 ", "timeout-minutes: 360 "),
        "--strict removed": ("--disable-version-check --strict --error", "--disable-version-check --error"),
        "--error removed": ("--strict --error --severity", "--strict --severity"),
        "severity WARNING": ("--severity ERROR --config", "--severity WARNING --config"),
        "secret in an env block": (
            'RETRY_PAUSE_SECONDS: "20"',
            'RETRY_PAUSE_SECONDS: "20"\n          T: ${{ secrets.X }}',
        ),
        "token in an env block": (
            'RETRY_PAUSE_SECONDS: "20"',
            'RETRY_PAUSE_SECONDS: "20"\n          GITHUB_TOKEN: ${{ github.token }}',
        ),
    }


@pytest.mark.parametrize("name", sorted(_mutants()))
def test_a_workflow_hazard_is_found(name: str) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    old, new = _mutants()[name]
    assert text.count(old) >= 1, f"the mutant {name!r} no longer matches the workflow text"
    assert _workflow_problems(text.replace(old, new, 1)) != [], name


STUB = """#!/bin/bash
if [[ "$1" == "--version" ]]; then echo 1.185.0; exit 0; fi
n=$(cat "$COUNT_FILE" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$COUNT_FILE"
code=$(sed -n "${n}p" "$CODES_FILE"); exit "${code:-0}"
"""
# the exit codes the stand-in `semgrep scan` gives, one per try -> (exit status of the step, number of scan tries)
RETRY_CASES = {
    "clean": ([0], 0, 1),
    "findings are never retried": ([1], 1, 1),
    "exit 2 then clean": ([2, 0], 0, 2),
    "exit 2 twice then clean": ([2, 2, 0], 0, 3),
    "exit 2 three times": ([2, 2, 2], 2, 3),
    "exit 2 then findings": ([2, 1], 1, 2),
    "unreadable file": ([3], 3, 1),
    "unreadable rule file": ([7], 7, 1),
}


@pytest.mark.skipif(
    sys.platform != "linux", reason="the step runs under bash on a Linux runner; the stand-in is a bash script"
)
@pytest.mark.parametrize("name", sorted(RETRY_CASES))
def test_scan_step_retries_only_exit_2(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs the real `run:` block of the scan step with a stand-in `semgrep` that answers with the listed exit codes."""
    codes, status, tries = RETRY_CASES[name]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "semgrep").write_text(STUB, encoding="utf-8")
    (bindir / "semgrep").chmod(0o755)
    (tmp_path / "codes").write_text("".join(f"{c}{NL}" for c in codes), encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("COUNT_FILE", str(tmp_path / "count"))
    monkeypatch.setenv("CODES_FILE", str(tmp_path / "codes"))
    monkeypatch.setenv("RETRY_PAUSE_SECONDS", "0")
    # bash runs the text of the workflow's own `run:` block, read from the file in this repository (not from input)
    result = _run(tmp_path, "bash", "-e", "-c", _scan_run_block(WORKFLOW.read_text(encoding="utf-8")), check=False)
    count = (tmp_path / "count").read_text(encoding="utf-8").strip()
    assert (result.returncode, count) == (status, str(tries)), (result.stdout, result.stderr)
    assert ("NOT findings" in result.stdout) == (status not in (0, 1)), result.stdout
