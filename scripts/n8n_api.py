"""Tiny n8n REST helper: import, activate, deactivate, list workflows. Needs an n8n API key in a file you name.

Run from the repository root with N8N_URL (default http://127.0.0.1:5679) and N8N_API_KEY_FILE set:
  python scripts/n8n_api.py import file.json | activate NAME | deactivate NAME | list
Workflows are addressed by NAME so no id is printed.
The key is read from the file, sent as a header, never printed.

N8N_URL must be an https address, or http to this computer (127.0.0.1, ::1, localhost): the key travels in a header and
plain http to another computer would send it unencrypted. Anything else (and an address that cannot be read) is refused
at start, before the key file is opened. To reach an n8n on another computer use https, or an SSH tunnel to 127.0.0.1.
A redirect answer is never followed (the header with the key would go to the new address); it is reported as an error.
For http to this computer the proxy settings of the environment (HTTP_PROXY, no_proxy) are ignored: the key header goes
straight to this computer, never to a proxy. For https they are used as usual.
The key file must hold one printable ASCII word (no space, no second line, no control character); anything else is
refused at start with a fixed message that does not show the file text.
Every failure ends with a short message and exit code 1 or 2, never a Python traceback, and never prints the key, the
address, a header or an answer body: a network or protocol fault prints only the name of its error type.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from http_guard import open_http  # noqa: E402

from lead_desk.config import is_loopback_host  # noqa: E402
from lead_desk.envnum import SettingRefused, split_url  # noqa: E402

REFUSED = (
    "N8N_URL must be an https address, or http to this computer (127.0.0.1, ::1 or localhost); "
    "for an n8n on another computer use https, or an SSH tunnel to 127.0.0.1"
)


def checked_base(url: str) -> str:
    """The n8n address, only when the API key may travel to it: https, or http to this computer. Uses the repository's
    own readers (split_url, is_loopback_host). Anything else is a SystemExit that says what to do."""
    try:
        parts = split_url("N8N_URL", url)
    except SettingRefused as e:
        raise SystemExit(f"{e}; {REFUSED}") from None
    # urlsplit drops tabs and line breaks and reads the host after the last "@"; urllib dials the whole text between
    # "//" and the next "/". So a user name part or a blank/control character is refused here, by a check.
    if "@" in parts.netloc or any(c.isspace() or not c.isprintable() for c in url):
        raise SystemExit(REFUSED)
    scheme, host = parts.scheme.lower(), parts.hostname or ""
    if host and (scheme == "https" or (scheme == "http" and is_loopback_host(host))):
        return url.rstrip("/")
    raise SystemExit(REFUSED)


BASE = checked_base(os.environ.get("N8N_URL", "http://127.0.0.1:5679"))
PROXY_ALLOWED = urlsplit(BASE).scheme.lower() == "https"  # http is only ever to this computer: no proxy for the key


def _read_key() -> str:
    name = os.environ.get("N8N_API_KEY_FILE")
    if not name:
        raise SystemExit("N8N_API_KEY_FILE must be the path of a one-line file with the n8n API key")
    try:
        key = Path(name).read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as e:  # a missing or unreadable file, or bytes that are not text
        raise SystemExit(f"cannot read the n8n API key file ({type(e).__name__})") from None
    # Printable ASCII without a blank: a second line, a tab or NUL inside, or a non-latin-1 letter would make the header
    # call fail with a message that holds the key. The text of the file is never part of the message.
    if not key or not all(c.isascii() and c.isprintable() and not c.isspace() for c in key):
        raise SystemExit(
            "the n8n API key file must hold one word of printable ASCII (no space, second line or control character)"
        )
    return key


KEY = _read_key()
KEEP = ("name", "nodes", "connections", "settings")  # the create call refuses other top-level keys


def call(method: str, path: str, body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    try:
        req = urllib.request.Request(  # noqa: S310 - local n8n, http by design
            BASE + "/api/v1" + path,
            data=data,
            method=method,
            headers={"X-N8N-API-KEY": KEY, "Content-Type": "application/json"},
        )
        with open_http(req, 60, follow_redirects=False, use_proxy=PROXY_ALLOWED) as r:
            raw = r.read()
            if r.status != HTTPStatus.OK:  # any other success answer (201, 204 ...): same as an error answer
                return r.status, "(no body kept)"
            try:
                return r.status, (json.loads(raw) if raw else {})
            except ValueError:  # JSONDecodeError and UnicodeDecodeError: n8n (or something in front of it) sent no JSON
                raise SystemExit("n8n answered 200 with a body that is not JSON (not shown)") from None
    except urllib.error.HTTPError as e:
        try:
            e.read()  # the body is not kept: an error page could echo a header
        except (OSError, http.client.HTTPException, ValueError):
            pass  # a reset or a cut-off while reading an error page changes nothing: the status is what counts
        return e.code, "(no body kept)"
    except (OSError, http.client.HTTPException, ValueError) as e:
        # refused, no route, timeout, TLS fault, a bad address, a cut-off answer: only the NAME of the error type is
        # printed (the text of such an error can hold the address or, for a bad header, the key).
        reason = getattr(e, "reason", None)
        detail = f": {type(reason).__name__}" if isinstance(e, urllib.error.URLError) and reason is not None else ""
        raise SystemExit(f"cannot reach n8n ({type(e).__name__}{detail})") from None


def workflows() -> list[dict[str, object]]:
    """The workflow list, or a clean SystemExit when n8n did not answer 200 (a redirect, a refused key, an error) or
    sent a list whose entries are not workflows (an entry without a text name, an id or an active flag)."""
    code, res = call("GET", "/workflows?limit=250")
    if code != HTTPStatus.OK:
        raise SystemExit(f"n8n answered {code} to the workflow list ({res})")  # res is "(no body kept)" here
    items = res.get("data") if isinstance(res, dict) else None
    if not isinstance(items, list):
        raise SystemExit(f"n8n answered {code} to the workflow list (unexpected body)")
    for w in items:
        if not (isinstance(w, dict) and isinstance(w.get("name"), str) and "id" in w and "active" in w):
            raise SystemExit(f"n8n answered {code} to the workflow list (an entry without name, id or active flag)")
    return items


def find(name: str) -> str:
    hits = [str(w["id"]) for w in workflows() if w["name"] == name]
    if len(hits) != 1:
        raise SystemExit(f"{len(hits)} workflows named {name!r}")
    return hits[0]


USAGE = "usage: python scripts/n8n_api.py import FILE | activate NAME | deactivate NAME | list"


def main(a: list[str]) -> int:
    cmd = a[1] if len(a) > 1 else ""
    if cmd not in ("list", "import", "activate", "deactivate") or (cmd != "list" and not a[2:]):
        print(USAGE, file=sys.stderr)
        return 2
    if cmd == "list":
        for w in workflows():
            print(w["name"], "active" if w["active"] else "inactive")
    elif cmd == "import":
        try:
            wf = json.loads(Path(a[2]).read_text(encoding="utf-8"))
            wf.setdefault("settings", {})
            str(wf["name"])  # a workflow file without a name is refused here, not after the call
        except (OSError, ValueError, KeyError, AttributeError) as e:  # no file, not JSON, not an object, no name
            raise SystemExit(f"cannot read a workflow from {a[2]} ({type(e).__name__})") from None
        code, res = call("POST", "/workflows", {k: wf[k] for k in KEEP if k in wf})
        print(wf["name"], code, "" if code == HTTPStatus.OK else res)
        return 0 if code == HTTPStatus.OK else 1
    else:  # activate or deactivate
        code, res = call("POST", f"/workflows/{find(a[2])}/{cmd}")
        print(a[2], cmd, code, "" if code == HTTPStatus.OK else res)
        return 0 if code == HTTPStatus.OK else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
