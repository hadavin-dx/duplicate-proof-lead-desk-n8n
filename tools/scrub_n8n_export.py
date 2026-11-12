"""Scrub an exported n8n workflow before it goes into a public repo (made-up-data exports only).

Allow-list by key name: a key that is not listed is dropped, so a field n8n adds later cannot leak.
Never kept: ids, instance id, credentials, webhook ids, pinData, tags, project owner, timestamps.
Inside node parameters (free-form) strings are cleaned: absolute URLs other than localhost, and e-mail
addresses other than example.test / example.com / example.invalid, are replaced.

Usage: python -m tools.scrub_n8n_export in.json out.json
The webhook path is kept (a plain word); n8n creates its own ids on import.
Meant for the licence holder's own use (see LICENSE).
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

TOP = ("name", "active", "nodes", "connections")
SETTINGS = ("executionOrder",)  # add a key here only when the demo needs that setting
NODE = (
    "name",
    "type",
    "typeVersion",
    "position",
    "parameters",
    "disabled",
    "notes",
    "onError",
    "retryOnFail",
    "maxTries",
    "waitBetweenTries",
    "executeOnce",
    "alwaysOutputData",
    "continueOnFail",  # node settings that change what a run does
)
URL = re.compile(r"https?://(?!(?:localhost|127\.0\.0\.1)(?:[:/]|$))[^\s\"'<>]+")
MAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9-]+\.)*example\.(?:com|test|invalid)\b)[A-Za-z0-9.-]+")


def _clean(v: Any) -> Any:
    if isinstance(v, str):
        return MAIL.sub("someone@example.test", URL.sub("http://localhost/REPLACE-ME", v))
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def scrub(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a new, scrubbed workflow; the input is not changed."""
    wf = copy.deepcopy(workflow)
    out: dict[str, Any] = {k: wf[k] for k in TOP if k in wf and k != "nodes"}
    if isinstance(wf.get("settings"), dict):
        out["settings"] = {k: v for k, v in wf["settings"].items() if k in SETTINGS}
    out["nodes"] = [
        {"id": f"node-{i}", **{k: _clean(n[k]) for k in NODE if k in n}} for i, n in enumerate(wf.get("nodes", []), 1)
    ]
    out["name"] = _clean(out.get("name", "workflow"))
    return out


def main(argv: list[str]) -> int:
    try:
        _, src, dst = argv
    except ValueError:
        print("usage: python -m tools.scrub_n8n_export in.json out.json")
        return 2
    wf = json.loads(Path(src).read_text(encoding="utf-8"))
    Path(dst).write_text(json.dumps(scrub(wf), indent=2, ensure_ascii=False) + chr(10), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
