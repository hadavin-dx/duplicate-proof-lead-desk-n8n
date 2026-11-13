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
The key file must hold one printable ASCII word of at most 4096 characters (no space, no second line, no control
character); anything else is refused at start with a fixed message that does not show the file text.
Every failure that can come from the address, the key file, the workflow file, the network or n8n's answer ends with a
short message and exit code 1 or 2, never a Python traceback, and never prints the key, the address, a header or an
answer body: a network or protocol fault prints only the name of its error type. That list is: an address that cannot
be read or is not allowed (also a port outside 1..65535), a key file that is missing, unreadable, a directory or not
one printable ASCII word, a workflow file that is missing, not JSON, nested too deeply (RecursionError) or without a
name, a workflow that cannot be encoded, every network, TLS and protocol fault, an answer that is not JSON or is nested
too deeply, a list with an odd entry. A workflow name that the terminal cannot show is printed with "?" in place of
the characters it cannot encode. NOT covered (a traceback is possible): Ctrl-C (KeyboardInterrupt), running out of
memory (MemoryError), and a closed output pipe (BrokenPipeError).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
# insert(0) puts each entry first, so the entry added LAST ends up ahead: add the repository folder first, so that src
# stays ahead of it (the same order as PYTHONPATH=src:. in the run commands). That order holds only when neither folder
# is on PYTHONPATH already (an entry that is already there is not moved). A `tools` package of another project that
# comes first on the path stops the start with a traceback (ModuleNotFoundError or ImportError): start it from the
# repository folder, without another project's `tools` on PYTHONPATH.
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from http_guard import open_http  # noqa: E402

from lead_desk.config import is_loopback_host  # noqa: E402
from lead_desk.envnum import SettingRefused, split_url  # noqa: E402
from tools.pathconfine import ROOTS_ENV, PathRefused, confine_existing  # noqa: E402

REFUSED = (
    "N8N_URL must be an https address, or http to this computer (127.0.0.1, ::1 or localhost); "
    "for an n8n on another computer use https, or an SSH tunnel to 127.0.0.1"
)


# What urllib dials is the whole text between "//" and the next "/": a host (letters, digits, dots, hyphens) or a
# bracketed IPv6 literal, then at most one ":port" of digits. Any other shape (a backslash, a second colon, text after
# a bracket, an empty port) is read one way by urlsplit and another by urllib, so it is refused.
NETLOC = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?")
MAX_PORT = 65535


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
    shape = NETLOC.fullmatch(parts.netloc)
    if shape is None or (shape.group(2) is not None and not 1 <= int(shape.group(2)[1:]) <= MAX_PORT):
        raise SystemExit(REFUSED)
    scheme, host = parts.scheme.lower(), parts.hostname or ""
    if host and (scheme == "https" or (scheme == "http" and is_loopback_host(host))):
        return url.rstrip("/")
    raise SystemExit(REFUSED)


BASE = checked_base(os.environ.get("N8N_URL", "http://127.0.0.1:5679"))
PROXY_ALLOWED = urlsplit(BASE).scheme.lower() == "https"  # http is only ever to this computer: no proxy for the key


MAX_KEY = 4096  # an n8n API key is a token of a few hundred characters; anything longer is not one


def _read_key() -> str:
    name = os.environ.get("N8N_API_KEY_FILE")
    if not name:
        raise SystemExit("N8N_API_KEY_FILE must be the path of a one-line file with the n8n API key")
    try:
        with Path(name).open("rb") as f:  # a directory, a missing or an unreadable file: OSError
            raw = f.read(MAX_KEY + 1)  # never the whole of a huge file: a key is short
        key = raw.decode("utf-8").strip()  # bytes that are not text: UnicodeDecodeError (a ValueError)
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read the n8n API key file ({type(e).__name__})") from None
    # Printable ASCII without a blank: a second line, a tab or NUL inside, or a non-latin-1 letter would make the header
    # call fail with a message that holds the key. The text of the file is never part of the message.
    if not key or len(key) > MAX_KEY or not all(c.isascii() and c.isprintable() and not c.isspace() for c in key):
        raise SystemExit(
            "the n8n API key file must hold one word of printable ASCII (no space, second line or control character) "
            f"of at most {MAX_KEY} characters"
        )
    return key


KEY = _read_key()
KEEP = ("name", "nodes", "connections", "settings")  # the create call refuses other top-level keys


def say(*parts: object) -> None:
    """print() for names that come from n8n or from a file: a character the terminal cannot encode (a lone surrogate,
    an emoji on a cp1252 pipe) becomes "?" instead of ending the run with a UnicodeEncodeError."""
    encoding = sys.stdout.encoding or "utf-8"
    text = " ".join(str(p) for p in parts)
    sys.stdout.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def call(method: str, path: str, body: object = None) -> tuple[int, object]:
    try:
        data = None if body is None else json.dumps(body).encode()
    except (ValueError, TypeError, RuntimeError) as e:  # nested too deeply (RecursionError), a lone surrogate, ...
        raise SystemExit(f"cannot encode the workflow ({type(e).__name__})") from None
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
            except RuntimeError:  # RecursionError: JSON nested thousands of levels deep (not a ValueError)
                raise SystemExit("n8n answered 200 with a body nested too deeply to read (not shown)") from None
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
            say(w["name"], "active" if w["active"] else "inactive")
    elif cmd == "import":
        try:
            wf = json.loads(confine_existing(a[2], what="FILE").read_text(encoding="utf-8"))
            wf.setdefault("settings", {})
            str(wf["name"])  # a workflow file without a name is refused here, not after the call
        except PathRefused as e:  # the rule is in the message: a workflow file lies in an allowed folder
            hint = "" if ROOTS_ENV in str(e) else f"; more folders can be listed in {ROOTS_ENV}"
            raise SystemExit(
                f"cannot read a workflow ({e}). Rule: the file must exist and lie in the folder you started in, "
                f"the repository folder or the temporary folder{hint}"
            ) from None
        except (OSError, ValueError, KeyError, AttributeError, RuntimeError) as e:  # no file, not JSON (or nested too
            # deeply: RecursionError), not an object, no name
            raise SystemExit(f"cannot read a workflow from {a[2]} ({type(e).__name__})") from None
        code, res = call("POST", "/workflows", {k: wf[k] for k in KEEP if k in wf})
        say(wf["name"], code, "" if code == HTTPStatus.OK else res)
        return 0 if code == HTTPStatus.OK else 1
    else:  # activate or deactivate
        code, res = call("POST", f"/workflows/{find(a[2])}/{cmd}")
        say(a[2], cmd, code, "" if code == HTTPStatus.OK else res)
        return 0 if code == HTTPStatus.OK else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
