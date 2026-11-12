"""Tiny n8n REST helper: import, activate, deactivate, list workflows. Needs an n8n API key in a file you name.

Run from the repository root with N8N_URL (default http://127.0.0.1:5679) and N8N_API_KEY_FILE set:
  python scripts/n8n_api.py import file.json | activate NAME | deactivate NAME | list
Workflows are addressed by NAME so no id is printed.
The key is read from the file, sent as a header, never printed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

from http_guard import open_http

BASE = os.environ.get("N8N_URL", "http://127.0.0.1:5679").rstrip("/")
KEY = Path(os.environ["N8N_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
KEEP = ("name", "nodes", "connections", "settings")  # the create call refuses other top-level keys


def call(method: str, path: str, body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310 - local n8n, http by design
        BASE + "/api/v1" + path,
        data=data,
        method=method,
        headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"},
    )
    try:
        with open_http(req, 60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        e.read()  # the body is not kept: an error page could echo a header
        return e.code, "(no body kept)"


def find(name: str) -> str:
    _, res = call("GET", "/workflows?limit=250")
    hits = [w["id"] for w in res["data"] if w["name"] == name]  # type: ignore[index]
    if len(hits) != 1:
        raise SystemExit(f"{len(hits)} workflows named {name!r}")
    return hits[0]


def main(a: list[str]) -> int:
    cmd = a[1]
    if cmd == "list":
        _, res = call("GET", "/workflows?limit=250")
        for w in res["data"]:  # type: ignore[index]
            print(w["name"], "active" if w["active"] else "inactive")
    elif cmd == "import":
        wf = json.loads(Path(a[2]).read_text(encoding="utf-8"))
        wf.setdefault("settings", {})
        code, res = call("POST", "/workflows", {k: wf[k] for k in KEEP if k in wf})
        print(wf["name"], code, "" if code == HTTPStatus.OK else res)
        return 0 if code == HTTPStatus.OK else 1
    elif cmd in ("activate", "deactivate"):
        code, res = call("POST", f"/workflows/{find(a[2])}/{cmd}")
        print(a[2], cmd, code, "" if code == HTTPStatus.OK else res)
        return 0 if code == HTTPStatus.OK else 1
    else:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
