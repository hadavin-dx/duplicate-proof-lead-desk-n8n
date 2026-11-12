"""CI must run with the demo_only folder: a missing folder stops the run when DPLD_REQUIRE_DEMO=1 (otherwise most
test files would be skipped and the run would look green). Runs pytest in a throw-away folder that has the real
conftest.py but no demo_only folder."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# not imported from support: that module needs demo_only, and this test must also run in a copy without it
REPO = Path(__file__).resolve().parent.parent

# (no colon before a backslash in this text: the literal scan of CI would read it as a drive path)
NEEDS_NOTHING = "def test_runs_without_the_demo_folder():\n    assert 1 == 1\n"


def _run(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    shutil.copy(REPO / "tests" / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_plain.py").write_text(NEEDS_NOTHING, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DPLD_")}
    env.update(PYTHONPATH=str(REPO / "src"), **extra)
    return subprocess.run(  # noqa: S603 (our own interpreter, our own throw-away folder)
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_a_missing_demo_folder_skips_when_allowed_and_stops_the_run_in_ci(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    allowed = _run(tmp_path / "a")
    assert allowed.returncode == 0 and "1 passed" in allowed.stdout, allowed.stdout + allowed.stderr
    (tmp_path / "b").mkdir()
    required = _run(tmp_path / "b", DPLD_REQUIRE_DEMO="1")
    out = required.stdout + required.stderr
    assert required.returncode == 2 and "demo_only folder is missing" in out and "passed" not in out, out
