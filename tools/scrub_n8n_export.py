"""Scrub an exported n8n workflow before it goes into a public repo (made-up-data exports only).

Allow-list by key name: a key that is not listed is dropped, so a field n8n adds later cannot leak.
Never kept: ids, instance id, credentials, webhook ids, pinData, tags, project owner, timestamps.
Inside node parameters (free-form) strings are cleaned: absolute URLs (any scheme; JSON-escaped slashes as in
https:\\/\\/host too), data: and javascript: links, and e-mail addresses (letters of any alphabet) other than
example.test / example.com / example.invalid, are replaced. Node names are cleaned too, and the "connections" part is
rewritten with the cleaned names, so the references stay consistent.
One exception: a plain web address (scheme http, https, ws or wss) to localhost or 127.0.0.1 (a port may follow;
user:password in front of the host is never allowed) is kept, but only up to the end of its path: the query string
(?...) and the fragment (#...) are dropped, because an API key can sit in a query. Every other scheme with a localhost
host (file://localhost/..., postgres://localhost/..., ftp://localhost/...) is replaced whole.
A tab, a carriage return or a line feed right after a localhost host (a browser ignores
it, so `http://localhost<TAB>@host` reads as user localhost at host) makes the address a look-alike: that address, the
break and the next run of non-blank characters are replaced whole. Any other blank right after a localhost host (a
space, a vertical tab, a form feed, a no-break space or another Unicode space; a browser keeps it as part of the user
name) does the same, but only when the next run of non-blank characters holds an at-sign before any / ? or #
(`http://localhost @host`, `http://localhost :80@host`); a plain `http://localhost:5678/x y` or `http://localhost y`
(words after a space) is not touched. A zero-width space is not a blank: `http://localhost<ZWSP>@host` is read as one
word and replaced as a look-alike by the ordinary rule.
Never kept: webhook ids (the id field). The webhook PATH is kept as it is, so a secret written inside a path stays.
What is NOT cleaned (the free text of a name or a parameter is not parsed), stated plainly:
 - an address without a scheme, such as //host/path or host:5432; a bare host name or IP address; a path that is not a
   URL; the scheme's colon and slashes written with percent signs (%3A%2F%2F) or with backslashes (http:\\\\host);
 - a secret inside the path of a kept localhost address (/webhook/<token>); a secret inside a path as a %3F-encoded
   query (only a real ? or # ends the path);
 - a data: link without a media type (data:,text and data:;base64,...), and U+FE6B (small at-sign) or an ideographic
   full stop in an e-mail address (only the ASCII "@" and the full-width "@" are read as the at-sign);
 - the keys of a dictionary inside node parameters (only the values are cleaned; node names and the keys of the
   "connections" part are cleaned);
 - an expression inside a node name that refers to another node by an old name when two names became the same (the
   second gets " (2)").
Over-cleaning, also by design: an address that an expression completes, such as "http://localhost:5678/x?" + $json.q,
is read as a plain address and loses its "?" and what follows on the same run of characters. Likewise a bare local
address, a space and then an e-mail address (`http://localhost jane@example.test`) loses the e-mail address too.
Check the output by eye before it goes into a public repo.

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

try:  # started as a script (python tools/scrub_n8n_export.py): the repository folder is not on the path yet
    from tools.pathconfine import PathRefused, confine, confine_existing
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.pathconfine import PathRefused, confine, confine_existing

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
# No look-behind in front of the scheme: a look-behind made an address right after a digit, "-", "+" or "." (a digit,
# then http, then the rest of the address) survive whole. The scheme is at most 32 characters, so the search stays
# linear. The leftmost match starts at the true start of the scheme, so a character in front of it (a digit, "-")
# stays and the address after it goes. The slashes may be JSON-escaped (:\/\/).
# Any scheme (http, https, ftp, ssh, postgres, file, ...) is matched whole: a host, a path or a user:password part must
# not survive. The one exception is decided in _url_replacement, not in the pattern (a pattern with the exception in
# it would also match from the middle of a scheme).
URL = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]{0,31}:(?:\\?/){2}|(?:https?|ftps?|wss?):/(?!/))[^\s\"'<>]+")
# What may stay: http, https, ws or wss to localhost or 127.0.0.1 (optionally :digits, and the host must END there:
# localhost:x@host and 127.0.0.1.host are other hosts). Only up to the end of the path: no query, no fragment.
_LOCAL_WEB = re.compile(
    r"((?:https?|wss?)://(?:localhost|127\.0\.0\.1)(?::[0-9]+)?)(?=[/?#]|\Z)([^?#]*)[?#]?.*\Z", re.DOTALL
)
# A blank right after a local host. Tab, CR and LF: browsers drop them, so what follows reads as part of the same
# address. Any other blank (space, VT, FF, NBSP, other Unicode spaces; \s): a browser keeps it as part of the user
# name, so it only matters when the next run of non-blank characters holds an at-sign before any / ? or # (then the
# text after it is the real host); a plain "http://localhost:5678/x y" or "http://localhost y" stays out. The whole
# thing (scheme, host, the blanks and the next non-blank run) is replaced. Linear: no nested repetition, a fixed
# prefix, one blank run and one non-blank run per start; the look-ahead reads at most the next non-blank run.
_AT = "@\uff20"
_LOCAL_BROKEN = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]{0,31}:(?:\\?/){2}(?:localhost|127\.0\.0\.1)(?::[0-9]*)?"
    r"(?:\s*[\t\r\n]\s*|\s+(?=[^\s" + _AT + r"/?#\"'<>]*[" + _AT + r"]))[^\s\"'<>]*",
    re.IGNORECASE,
)
# links that carry content or code instead of a host
ACTIVE_LINK = re.compile(
    r"(?:(?:javascript|vbscript):[^\s\"'<>]+|data:[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+[;,][^\s\"'<>]*)", re.IGNORECASE
)
# letters of any alphabet (\w is Unicode for str patterns) in the local part and the domain; @ or its full-width form.
# The allowed domain must END there: the allowed name with more labels or letters after it is another, real domain.
# One character, then the rest of the run taken possessively (Python 3.11 and later: taken once, never given back), so
# the pattern cannot be tried from every character of a run as the plain "+" form is. In the look-ahead a label is taken
# only when it does not itself start the allowed name, so the labels before "example.<ending>" and the allowed name
# cannot be split two ways. It replaces the same text as the plain form (tests compare both on every short text).
MAIL = re.compile(
    r"(?<![\w.%+-])[\w.%+-][\w.%+-]*+[@\uFF20]"
    r"(?!(?:(?!example\.(?:com|test|invalid)(?![\w-]|\.[\w-]))[\w-]++\.)*+example\.(?:com|test|invalid)(?![\w-]|\.[\w-]))"
    r"[\w.-]++"
)
REPLACEMENT_URL = "http://localhost/REPLACE-ME"
REPLACEMENT_MAIL = "someone@example.test"


def _url_replacement(m: re.Match[str]) -> str:
    """A web address to this computer keeps scheme, host, port and path (no query, no fragment); all others go."""
    local = _LOCAL_WEB.fullmatch(m.group(0))
    return local.group(1) + local.group(2) if local else REPLACEMENT_URL


def _clean(v: Any) -> Any:
    if isinstance(v, str):
        broken = _LOCAL_BROKEN.sub(REPLACEMENT_URL, v)
        return MAIL.sub(REPLACEMENT_MAIL, ACTIVE_LINK.sub(REPLACEMENT_URL, URL.sub(_url_replacement, broken)))
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def _rename(value: Any, names: dict[str, str]) -> Any:
    """The connections part: every key and every "node" value is a node name; use the cleaned name."""
    if isinstance(value, list):
        return [_rename(x, names) for x in value]
    if isinstance(value, dict):
        return {
            names.get(k, _clean(k)): (
                names.get(x, _clean(x)) if k == "node" and isinstance(x, str) else _rename(x, names)
            )
            for k, x in value.items()
        }
    return _clean(value)


def _unique(name: str, taken: set[str]) -> str:
    """A cleaned name that no other node has (two different addresses clean to the same text)."""
    out, n = name, 1
    while out in taken:
        n += 1
        out = f"{name} ({n})"
    taken.add(out)
    return out


def scrub(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a new, scrubbed workflow; the input is not changed."""
    wf = copy.deepcopy(workflow)
    out: dict[str, Any] = {k: wf[k] for k in TOP if k in wf and k not in ("nodes", "connections")}
    if isinstance(wf.get("settings"), dict):
        out["settings"] = {k: v for k, v in wf["settings"].items() if k in SETTINGS}
    names: dict[str, str] = {}
    taken: set[str] = set()
    nodes = []
    for i, n in enumerate(wf.get("nodes", []), 1):
        node = {"id": f"node-{i}", **{k: _clean(n[k]) for k in NODE if k in n}}
        if isinstance(n.get("name"), str):
            if n["name"] not in names:
                names[n["name"]] = _unique(node["name"], taken)
            node["name"] = names[n["name"]]
        nodes.append(node)
    out["nodes"] = nodes
    if "connections" in wf:
        out["connections"] = _rename(wf["connections"], names)
    out["name"] = _clean(out.get("name", "workflow"))
    return out


def main(argv: list[str]) -> int:
    try:
        _, src, dst = argv
    except ValueError:
        print("usage: python -m tools.scrub_n8n_export in.json out.json", file=sys.stderr)
        return 2
    try:
        src_path = confine_existing(src, what="in.json")
        dst_path = confine(dst, what="out.json")
    except PathRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    wf = json.loads(src_path.read_text(encoding="utf-8"))
    dst_path.write_text(json.dumps(scrub(wf), indent=2, ensure_ascii=False) + chr(10), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
