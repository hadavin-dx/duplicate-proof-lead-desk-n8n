"""Follow-up B: sys.path order of scripts/n8n_api.py and the comment of scripts/start_all.sh. Made-up data only."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.support import start_proc, stop_proc

REPO = Path(__file__).resolve().parents[1]

# What starting the file does to sys.path, in a clean interpreter (-I: no PYTHONPATH, no user site, no current folder):
# its own folder is first (as when a file is started), then the file runs its module code up to the imports.
PROBE = """
import json, runpy, sys
sys.path.insert(0, sys.argv[1])
runpy.run_path(sys.argv[2], run_name="n8n_api_path_probe")
print(json.dumps(sys.path))
"""


def test_n8n_api_puts_src_ahead_of_the_repository_folder(tmp_path: Path) -> None:
    """Like PYTHONPATH=src:. in the run commands: ``src`` first, so a module that exists in both folders is taken from
    ``src``. (Each ``insert(0)`` puts its entry first, so the loop must add the repository folder first.)"""
    (tmp_path / "key.txt").write_text("made-up-key-for-the-test\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(N8N_API_KEY_FILE=str(tmp_path / "key.txt"), N8N_URL="http://127.0.0.1:5679")
    proc = start_proc(["-I", "-c", PROBE, str(REPO / "scripts"), str(REPO / "scripts" / "n8n_api.py")], env)
    proc.wait(timeout=60)
    out = stop_proc(proc)
    assert proc.returncode == 0, out[-500:]
    path = json.loads(out)
    src, repo = str(REPO / "src"), str(REPO)
    assert src in path and repo in path
    assert path.index(src) < path.index(repo)


def test_start_all_sh_comment_says_what_the_script_does() -> None:
    """The comment must not say the demo settings are dropped when DESK_LEADFLOW_FILE is set to any value: a value you
    set (even an empty one) is kept, and DESK_NUDGE_TO has its own default that follows the leadflow file."""
    text = (REPO / "scripts" / "start_all.sh").read_text(encoding="utf-8")
    comments = " ".join(ln.lstrip("# ") for ln in text.splitlines() if ln.startswith("#"))
    assert "unless you set DESK_LEADFLOW_FILE yourself" not in comments
    assert "a value you set, even an empty one, is kept" in comments
    assert "unless DESK_LEADFLOW_FILE is off or empty, DESK_NUDGE_TO is reminder@example.test" in comments


def test_catcher_start_survives_a_foreign_tools_package(tmp_path: Path) -> None:
    """Class: the fallback import assumed that any failure means "the repository folder is not on the path yet". A
    ``tools`` package of another project (an empty one, first on PYTHONPATH) is found first and cached, so adding the
    repository folder afterwards changed nothing and the start failed."""
    foreign = tmp_path / "foreign" / "tools"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tmp_path / "foreign") + os.pathsep + str(REPO / "src")
    proc = start_proc([str(REPO / "load_tools" / "catcher.py"), "--help"], env)
    proc.wait(timeout=60)
    out = stop_proc(proc).decode("utf-8", "replace")
    assert proc.returncode == 0, out[-500:]
    assert "usage" in out.lower()
