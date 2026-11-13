"""A cheap tripwire against the single-bracket test `[ ... ]` in shell scripts (SonarCloud rule shell:S7688).

The real control for that rule is SonarCloud itself. This file only stops the obvious return of `[ ... ]` between two
SonarCloud runs. It is NOT a shell parser and does not try to be one: it reads each line of a tracked *.sh file and of
each workflow `run:` block on its own, in one pass, with no state between lines, and reports a line where `[` followed
by a blank (or the end of the line) stands where a command starts: at the start of the line, after `;` `&` `|` `(` `)`
`{` or a backtick, or after the keywords then, do, else, elif, if, while, until; a `!` word and variable assignments
(up to 8, `FOO=1 [ x ]`) may stand between that place and the `[`.

What it does to a line, in this order (every step is one regular expression or one slice, so the time is linear):
  1. a backslash escape (`\\;`) and text inside '...' or "..." on that line is replaced by `_` (so `grep -E 'a|[ ]'`
     is not a test, and `echo a\\; [ x ]` is an argument); an apostrophe inside a word opens a quote in bash too
     (`echo don't; [ x ]; echo it's` is one quoted string there), and the line is read the way bash reads it;
  2. a `#` that starts the line or follows a blank cuts the rest of the line off (so `$#` and `a#b` are not comments);
  3. the command-start pattern is searched.
A line longer than 4000 characters is refused with a clear message, so the cost is bounded by the file size.

Known false positives (a file that really needs one is listed in _ALLOWED with a reason, or the line is rewritten):
  - a line inside a heredoc body or a multi-line string that looks like a test, and a `case` pattern `[ a]*)` that
    starts a line; a continuation line `\\` + `[ x ]` that is an argument of the line before it;
  - a word `then`/`do`/... in an echo argument followed by `[ `; an unquoted regex alternation `[[ $x =~ a|[ ]b ]]`.
(`echo a ! [ x ]` is not flagged: a `!` counts only at the start of a command.)
Known false negatives (a real single-bracket test the scan does not see): `test -e x`, `sudo [`, `env [`, `xargs [`,
`exec [`, `time [`, `command [`, `env FOO=1 [`; a test inside quotes (`"$( [ x ]; )"`); a `[` at the start of a
continuation line whose previous line ends with a backslash is treated as a start of line (flagged), not read as an
argument.
Also scanned: the `run:` steps of composite actions (.github/actions/*/action.yml); a `shell:` other than bash fails.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from support import REPO

from tools import hygiene

MAX_LINE = 4000

# a file that legitimately needs a flagged line (a heredoc body, say) goes here: path -> reason. Empty today.
_ALLOWED: dict[str, str] = {}

_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"|\\.")
_COMMENT = re.compile(r"(?:^|(?<=\s))#")
# The assignment prefix is at most 8 assignments and possessive (`*+`, `{0,8}+`: no backtracking, Python 3.11+), so each
# start of a command costs a bounded number of steps: `then a=then a=then a=...` would be quadratic with `*`.
_SINGLE = re.compile(
    r"(?:^|[;&|(){`]|\b(?:then|do|else|elif|if|while|until)\b)\s*(?:!\s+)?(?:[A-Za-z_]\w*\+?=[^\s;&|(){`]*+\s+){0,8}+\[(?:\s|$)"
)


def single_bracket_lines(text: str) -> list[int]:
    """The 1-based numbers of the lines that hold a command-position `[ `. Raises ValueError on CR or a long line."""
    if "\r" in text:
        raise ValueError("the text holds a carriage return (CRLF line ends): the tripwire reads LF text only")
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        if len(line) > MAX_LINE:
            raise ValueError(
                f"line {number} has {len(line)} characters (more than {MAX_LINE}): the tripwire refuses it"
            )
        line = _QUOTED.sub("_", line)
        comment = _COMMENT.search(line)
        if comment:
            line = line[: comment.start()]
        if _SINGLE.search(line):
            found.append(number)
    return found


@pytest.mark.parametrize(
    "snippet",
    [
        "[ -e x ]",
        "  [ -e x ]",
        "\t[ -e x ]",
        "if [ -e x ]; then :; fi",
        "if ! [ -e x ]; then :; fi",
        "!   [ -e x ]",
        "then [ -e x ]",
        "do [ -e x ]",
        "else [ -e x ]",
        "elif [ -e x ]; then",
        "while [ -e x ]; do :; done",
        "until [ -e x ]; do :; done",
        "a && [ -e x ]",
        "a || [ -e x ]",
        "a | [ -e x ]",
        "a & [ -e x ]",
        "a; [ -e x ]",
        "a;[ -e x ]",
        "{ [ -e x ]; }",
        "( [ -e x ] )",
        "x=$( [ -e x ] && echo a)",
        "x=`[ -e x ] && echo a`",
        "case $x in a) [ -e y ] ;; esac",
        "[ -e x ] || exit 1",
        "[",
        "a && [",
        "echo $#; [ -e x ]",
        "FOO=1 [ -e x ]",
        "a=1 [ -e x ]",
        "a=1 b=2 [ -e x ]",
        "a+=1 [ -e x ]",
        "a= [ -e x ]",
        "x; a=1 [ -e x ]",
        "if a=1 [ -e x ]; then :; fi",
        "! a=1 [ -e x ]",
        "{ ! [ -e x ]; }",
        "a=$(b) [ -e x ]",
        'echo \\" ; [ -e x ]; echo \\"',  # escaped quotes are literal characters
        'echo "a\\""; [ -e x ]',
        "echo 'a\\'; [ -e x ]",  # a backslash inside single quotes is literal: the quote ends there
        "if [ \"$a\" = 'b c' ]; then",
        "a=1 [ -e x ]; [ -e y ]",
        "[ [ -e x ] ]",
        "[[ -e x ]] && [ -e y ]",
        "echo a#b; [ -e x ]",
        "case $x in [ a]*) ;; esac\n[ -e y ]",
        "cmd && \\\n  [ -e x ]",
        "x=1 # not a comment start? it is: a blank before\n[ -e x ]",
    ],
)
def test_a_command_position_single_bracket_is_flagged(snippet: str) -> None:
    assert single_bracket_lines(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "",
        "\n\n",
        "[[ -e x ]]",
        "if [[ -e x ]]; then :; fi",
        "a && [[ -e x ]]",
        "grep -E 'a|[ ]' f",
        'grep -E "a|[ ]" f',
        "grep '[ ]' f",
        "sed 's/[ ]//' f",
        "sed -e 's/a/[ ]/g' f",
        "awk '{ print $1 \"[ ]\" }' f",
        "echo [x]",
        "echo a [b",
        "echo a [ b ]",  # an argument, not a command (a false positive here would be wrong)
        "echo a ! [ -e x ]",  # a `!` that is an argument
        "echo a=1 [ -e x ]",  # an assignment-looking argument
        "a=$(b [ -e x ])",
        "echo a\\; [ -e x ]",  # an escaped separator
        'echo "a\\" ; [ -e x ]; \\"b"',  # an escaped quote does not end a double-quoted string
        "echo don't; [ -e x ]; echo it's",  # bash reads 't; [ -e x ]; echo it' as one quoted string
        "env FOO=1 [ -e x ]",  # documented: not looked for
        "echo hi # [ -e x ]",
        "# [ -e x ]",
        "  # if [ -e x ]; then",
        "a;# [ -e x ]",
        "echo '[ -e x ]'",
        'echo "[ -e x ]"',
        "m=(['a']=1)",
        "echo ${#a[@]}",
        "echo $((1 << 2))",
        "test -e x",  # documented: not looked for
        "sudo [ -e x ]",  # documented
        "env [ -e x ]",  # documented
        "xargs [ -e x ]",  # documented
        "echo [",
        "cat <<< '[ x ]'",
        "git log -1 --format=[%h]",
    ],
)
def test_other_text_is_not_flagged(snippet: str) -> None:
    assert not single_bracket_lines(snippet), snippet


def test_the_regex_alternation_in_double_brackets_is_a_documented_false_positive() -> None:
    assert single_bracket_lines("[[ $x =~ a|[ ]b ]] || :") == [1]  # `|[ ` looks like a pipe and a test
    assert not single_bracket_lines("[[ $x =~ 'a'|'[ ]' ]] || :")  # quoted, it is not


def test_the_case_pattern_false_positive_is_the_documented_one() -> None:
    assert single_bracket_lines("[ a]*) :;;") == [1]  # a line that starts with `[ `: flagged on purpose
    assert not single_bracket_lines("case $x in [ a]*) :;; esac")  # in the middle of a line it is not


def test_the_line_numbers_are_right() -> None:
    assert single_bracket_lines("a\n[ x ]\nb\n\nif [ y ]; then\n") == [2, 5]


def test_crlf_text_gets_a_message_naming_the_cause() -> None:
    with pytest.raises(ValueError, match="carriage return"):
        single_bracket_lines("echo a\r\n[ x ]\r\n")


def test_a_line_over_the_cap_is_refused_and_one_at_the_cap_is_read() -> None:
    with pytest.raises(ValueError, match=r"line 2 has 4001 characters"):
        single_bracket_lines("a\n" + "x" * (MAX_LINE + 1))
    assert single_bracket_lines("x" * (MAX_LINE - 7) + "; [ x ]") == [1]  # exactly 4000 characters


# the lines of the base commit (db54e13) that held its 10 single-bracket tests: 5 lines of scripts/reset.sh, 2 of
# scripts/start_all.sh (copied as they were; one long tail is cut)
# and the 2 lines of the lint.yml run block that held one each
_BASE_RESET = """\
if [ "$inside" != "True" ]; then
if ! command -v ps >/dev/null 2>&1 && { [ -e "$data_dir/desk.pid" ] || [ -e "$data_dir/fakes.pid" ]; }; then
alive() { kill -0 "$1" 2>/dev/null && [ "$(ps -p "$1" -o stat= 2>/dev/null | cut -c1)" != "Z" ]; }
  if [ -e "$pidfile" ]; then
    if [ "$(ps -p "$pid" -o stat= 2>/dev/null | cut -c1)" = "Z" ]; then what=""; fi
"""

_BASE_START = """\
if [ "$DESK_LEADFLOW_FILE" != off ] && [ -n "$DESK_LEADFLOW_FILE" ]; then :; fi
if [ -e "$data_dir/fakes.pid" ] || [ -e "$data_dir/desk.pid" ]; then
"""

_BASE_RUN = """\
if [ "$status" -gt 1 ]; then echo "git grep failed (status $status)"; exit 1; fi
if [ -s "${RUNNER_TEMP}/literals-left.txt" ]; then
"""


@pytest.mark.parametrize("text", [_BASE_RESET, _BASE_START, _BASE_RUN])
def test_the_shapes_the_base_commit_had_are_flagged_line_by_line(text: str) -> None:
    assert single_bracket_lines(text) == list(range(1, text.count("\n") + 1))


def _mappings(values: Any) -> list[dict[str, Any]]:
    return [v for v in values if isinstance(v, dict)]


def _step_groups(doc: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """(holder, steps) for every job of a workflow and for the `runs` of a composite action; a null or odd value is no
    steps. The holder of an action's steps is empty (it has no job default)."""
    groups = [(job, job.get("steps")) for job in _mappings((doc.get("jobs") or {}).values())]
    if isinstance(doc.get("runs"), dict):
        groups.append(({}, doc["runs"].get("steps")))
    return [(holder, _mappings(steps) if isinstance(steps, list) else []) for holder, steps in groups]


def _run_blocks(doc: dict[str, Any]) -> list[str]:
    steps = [step for _, group in _step_groups(doc) for step in group]
    return [step["run"] for step in steps if isinstance(step.get("run"), str)]


def _default_shell(holder: dict[str, Any]) -> Any:
    defaults = holder.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return run.get("shell", "bash") if isinstance(run, dict) else "bash"


def _workflow_shell_problems(name: str, doc: dict[str, Any]) -> list[str]:
    """A `run:` block is read as bash: a `shell:` key (workflow default, job default or step) naming another shell is a
    problem. A step is named by its `name` and its place in its job or action (1-based), so an unnamed step is found."""
    problems = [f"{name}: workflow default shell"] if _default_shell(doc) != "bash" else []
    for job, steps in _step_groups(doc):
        if _default_shell(job) != "bash":
            problems.append(f"{name}: job default shell")
        problems += [
            f"{name}: step {number} ({step.get('name') or 'unnamed'})"
            for number, step in enumerate(steps, 1)
            if step.get("shell", "bash") != "bash"
        ]
    return problems


def _flagged(sources: dict[str, str], allowed: dict[str, str]) -> list[str]:
    """`file:line` for each flagged line of each source text; a file in `allowed` is skipped."""
    return [f"{name}:{n}" for name, text in sources.items() if name not in allowed for n in single_bracket_lines(text)]


def _shell_sources(workflows: dict[str, dict[str, Any]], scripts: dict[str, str]) -> dict[str, str]:
    """The texts to scan: every script, and every `run:` block of every workflow (named `file#block-N`)."""
    sources = dict(scripts)
    for name, doc in workflows.items():
        for n, block in enumerate(_run_blocks(doc), 1):
            sources[f"{name}#block-{n}"] = block
    return sources


def test_a_run_block_of_a_workflow_is_scanned() -> None:
    doc = yaml.safe_load(
        chr(10).join(
            [
                "jobs:",
                "  a:",
                "    steps:",
                "      - run: |",
                "          echo hi",
                "          if [ -e x ]; then :; fi",
                "",
            ]
        )
    )
    assert _flagged(_shell_sources({"w.yml": doc}, {}), {}) == ["w.yml#block-1:2"]
    assert _flagged(_shell_sources({"w.yml": doc}, {"script": "[ x ]"}), {}) == ["script:1", "w.yml#block-1:2"]


def test_a_file_in_the_allowlist_is_skipped_and_nothing_else() -> None:
    sources = {"one": "[ x ]", "two": "[ y ]"}
    assert _flagged(sources, {}) == ["one:1", "two:1"]
    assert _flagged(sources, {"one": "a heredoc body"}) == ["two:1"]


@pytest.mark.parametrize(
    ("lines", "problems"),
    [
        (["jobs:", "  a:", "    steps:", "      - name: x", "        run: echo hi"], 0),
        (["jobs:", "  a:", "    steps:", "      - name: x", "        run: echo hi", "        shell: bash"], 0),
        (["jobs:", "  a:", "    steps:", "      - name: x", "        run: echo hi", "        shell: sh"], 1),
        (["jobs:", "  a:", "    steps:", "      - name: x", "        run: echo hi", "        shell: pwsh"], 1),
        (["jobs:", "  a:", "    defaults:", "      run:", "        shell: sh", "    steps:", "      - run: echo"], 1),
        (["defaults:", "  run:", "    shell: sh", "jobs:", "  a:", "    steps:", "      - run: echo"], 1),
        (["defaults:", "  run:", "    shell: bash", "jobs:", "  a:", "    steps:", "      - run: echo"], 0),
        (["runs:", "  using: composite", "  steps:", "    - run: echo", "      shell: sh"], 1),
        (["runs:", "  using: composite", "  steps:", "    - run: echo", "      shell: bash"], 0),
    ],
)
def test_a_workflow_shell_key_other_than_bash_is_found(lines: list[str], problems: int) -> None:
    doc = yaml.safe_load(chr(10).join(lines) + chr(10))
    assert len(_workflow_shell_problems("w.yml", doc)) == problems, lines


def _lines(*lines: str) -> str:
    """Text of the given lines, each ended by a line feed (built from parts: the hygiene scan reads a colon followed by
    a backslash-n in this file as a drive path)."""
    return chr(10).join(lines) + chr(10)


def test_an_unnamed_step_is_reported_by_its_place() -> None:
    doc = yaml.safe_load(
        _lines("jobs:", "  a:", "    steps:", "      - name: x", "      - run: echo", "        shell: sh")
    )
    assert _workflow_shell_problems("w.yml", doc) == ["w.yml: step 2 (unnamed)"]


def _tracked(repo: Path, pattern: str) -> list[str]:
    """Tracked names that match the pattern, read with -z so that a name with a newline or a non-ASCII letter (which git
    prints quoted otherwise) is a real path."""
    return [n for n in hygiene._git_text(repo, "ls-files", "-z", pattern).split("\0") if n]


def _load_tree(repo: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """The tracked *.sh texts, and the parsed workflow files (*.yml, *.yaml) and composite action files (action.yml,
    action.yaml under .github/actions), keyed by their path under .github."""
    scripts = {n: (repo / n).read_text(encoding="utf-8") for n in _tracked(repo, "*.sh") if (repo / n).is_file()}
    github = repo / ".github"
    paths = [p for ext in ("*.yml", "*.yaml") for p in (github / "workflows").glob(ext)]
    paths += [p for ext in ("action.yml", "action.yaml") for p in (github / "actions").glob("*/" + ext)]
    docs = {p.relative_to(github).as_posix(): yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(paths)}
    return scripts, {n: d for n, d in docs.items() if isinstance(d, dict)}


def _problems(scripts: dict[str, str], workflows: dict[str, dict[str, Any]], allowed: dict[str, str]) -> list[str]:
    """Every problem of a tree: a script without the bash shebang, a shell other than bash, a flagged line."""
    problems = [
        f"{n}: not a bash script" for n, text in scripts.items() if not text.startswith("#!/usr/bin/env bash\n")
    ]
    for name, doc in workflows.items():
        problems += _workflow_shell_problems(name, doc)
    flagged = _flagged(_shell_sources(workflows, scripts), allowed)
    return problems + [f"single-bracket test (use [[ ]]) in {where}" for where in flagged]


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    """A throw-away git repository holding the files (staged, not committed); the paths must be plain relative names."""
    hygiene._git_text(path, "init", "-q")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text, encoding="utf-8", newline="\n")
        hygiene._git_text(path, "add", "--", rel)
    return path


_BASH = "#!/usr/bin/env bash"


def test_a_tracked_name_with_a_non_ascii_letter_is_found_and_read(tmp_path: Path) -> None:
    ext = "." + "sh"
    name = "caf\u00e9_run" + ext  # git prints this name quoted (octal escapes) unless it is asked for -z
    repo = _git_repo(tmp_path, {name: _lines(_BASH, "[ x ]"), "plain_run" + ext: _lines(_BASH, "true")})
    assert _tracked(repo, "*" + ext) == [name, "plain_run" + ext]
    scripts, _ = _load_tree(repo)
    assert _problems(scripts, {}, {}) == [f"single-bracket test (use [[ ]]) in {name}:2"]


def test_the_tree_loader_reads_yml_yaml_and_composite_action_files(tmp_path: Path) -> None:
    run = "      - run: if [ -e x ]; then true; fi"
    flow = _lines("jobs:", "  a:", "    steps:", run)
    action = _lines("runs:", "  using: composite", "  steps:", run, "        shell: bash")
    repo = _git_repo(tmp_path, {})
    for rel, text in {
        ".github/workflows/a.yaml": flow,
        ".github/workflows/b.yml": flow,
        ".github/actions/one/action.yml": action,
        ".github/actions/two/action.yaml": action,
        ".github/workflows/notes.txt": flow,
    }.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    _, workflows = _load_tree(repo)
    assert sorted(workflows) == [
        "actions/one/action.yml",
        "actions/two/action.yaml",
        "workflows/a.yaml",
        "workflows/b.yml",
    ]
    assert len(_problems({}, workflows, {})) == 4


def test_a_script_without_the_bash_shebang_is_a_problem() -> None:
    assert _problems({"a": _lines(_BASH, "true")}, {}, {}) == []
    for first in ("#!/bin/sh", "#!/usr/bin/env sh", "# comment"):
        assert _problems({"a": _lines(first, "true")}, {}, {}) == ["a: not a bash script"], first
    assert _problems({"a": "true" + chr(10)}, {}, {}) == ["a: not a bash script"]


def test_a_workflow_with_no_jobs_value_is_read_without_an_error() -> None:
    for lines in (
        ["jobs:"],
        ["jobs:", "  a:"],
        ["jobs:", "  a:", "    steps:"],
        ["jobs: {}"],
        ["name: x"],
        ["defaults:", "jobs:"],
    ):
        doc = yaml.safe_load(_lines(*lines)) or {}
        assert _run_blocks(doc) == [], lines
        assert _workflow_shell_problems("w", doc) == [], lines


def test_no_tracked_script_or_workflow_run_block_holds_a_single_bracket_test() -> None:
    """bash's [[ ]] does not split words or expand patterns inside the test (SonarCloud rule shell:S7688). Every
    tracked .sh file starts with a bash shebang and a workflow `run:` block runs under bash (the runner default on
    Linux; a `shell:` key other than bash fails this test)."""
    scripts, workflows = _load_tree(REPO)
    assert {"scripts/reset.sh", "scripts/start_all.sh"} <= set(scripts), (
        f"the two shipped scripts are not listed: {sorted(scripts)}"
    )
    assert any(n.startswith("workflows/") for n in workflows), "no workflow files found: the test would prove nothing"
    assert any("#block-" in n for n in _shell_sources(workflows, scripts)), "no run blocks found: nothing was proved"
    assert not _problems(scripts, workflows, _ALLOWED)


def _lines_of(shape: str, total: int = 1_000_000, width: int = 3000) -> str:
    return "\n".join([shape * (width // len(shape))] * (total // width))


@pytest.mark.parametrize(
    "shape",
    [
        "<<x",
        "<<x<<x",
        "<<'",
        '<<"',
        "<<EOF ",
        "[ ",
        ";[ ",
        "[[ ",
        "((",
        "$(",
        "`",
        "'",
        '"',
        "'\"",
        " ",
        "\t",
        ";",
        "! ",
        "{ ",
        "if ",
        "then ",
        "# ",
        "\\",
        "a|[ ]",
        "a=1 ",
        "a=1 b=2 ",
        "then a=",  # quadratic with an unbounded assignment prefix: every `then` starts a run to the end of the line
        "do a=do a=",
        ";a=",
        ";a=;a= ",
        "! ",
        "if a=1 ",
        '\\"',
        '"\\"',
        "\\ ",
    ],
)
def test_one_megabyte_of_a_shape_is_scanned_in_linear_time(shape: str) -> None:
    text = _lines_of(shape)
    started = time.monotonic()
    single_bracket_lines(text)
    assert time.monotonic() - started < 2, shape


def test_many_short_lines_and_over_long_lines_are_fast() -> None:
    started = time.monotonic()
    single_bracket_lines("[[ x ]]\n" * 130_000)
    single_bracket_lines("\n" * 1_000_000)
    for shape in ("<<x", "<<'", "[ ", " "):
        with pytest.raises(ValueError, match="characters"):
            single_bracket_lines(shape * 300_000)  # one line of about a megabyte: refused at once, not scanned
    assert time.monotonic() - started < 2
