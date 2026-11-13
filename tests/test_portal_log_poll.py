"""Request log page: after "Load more", the next poll must not drop the rows that a new request pushed out of page one.
The real portal.js and pages/log.js run in node (tests/portal_log_poll_probe.js)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PORTAL_DIR = HERE.parent / "src" / "lead_desk" / "portal"


def _probe(scenario: str) -> dict[str, list[str]]:
    done = subprocess.run(  # noqa: S603
        ["node", str(HERE / "portal_log_poll_probe.js"), str(PORTAL_DIR), scenario],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = done.stdout.strip().splitlines()
    assert lines, f"node printed nothing (exit {done.returncode}): {done.stderr}"
    out = json.loads(lines[-1])
    assert "error" not in out, out
    return out  # type: ignore[no-any-return]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_row_pushed_out_of_page_one_stays_on_screen_after_older_rows_were_loaded() -> None:
    out = _probe("after")
    assert len(out["before"]) == 5  # page one (3) and the older rows (2)
    assert len(out["after"]) == 6, out  # the new request (1) and all five that were there: none falls between the pages


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_row_pushed_out_of_page_one_stays_on_screen_when_the_poll_comes_while_older_rows_are_loading() -> None:
    """The window between the click on "Load more" and its answer: no older row is in `extra` yet, but the request that
    pushed a row out of page one has already arrived, so the answer of that request would drop the row."""
    out = _probe("inflight")
    assert len(out["before"]) == 3
    assert len(out["during"]) == 4, out  # the new request and the three that were there
    assert len(out["after"]) == 6, out  # plus the two older rows: the row between the pages is not lost
