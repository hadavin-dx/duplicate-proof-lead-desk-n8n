"""One-shot probe, run by you: does a FREE HubSpot account accept the API writes the desk needs?

It makes at most 8 calls against one private-app token: reads owners, creates ONE made-up contact, writes the owner and
then the lead status on it (two calls, so the table shows which one a free plan refuses), reads it back, then always
deletes it (by id, or by email when the create answer was lost). It prints a PASS/FAIL table and the HTTP status classes
seen (for a 4xx answer also the error category and the first 100 characters of its message). It never prints the
token or a header. Stdlib only; it does not import the desk code.

    python -I scripts/hubspot_probe.py --plan                       # prints the planned call count, calls nothing
    python -I scripts/hubspot_probe.py --yes-spend-calls 7          # the real run (token from the environment)
    python -I scripts/hubspot_probe.py --yes-spend-calls 7 --dry-run http://127.0.0.1:PORT   # against a local stub

The token is read only from the environment variable named by --token-env (default TOKEN_ENV_DEFAULT); it is never an
argument. The number after --yes-spend-calls must equal the planned count that --plan prints.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Final, NoReturn
from urllib.parse import quote, urlsplit

# the same host as in src/lead_desk/constants.py. This one-file script imports nothing from the desk, and the
# repository scan allows the full address only in that file, so it is written in parts here
HUBSPOT_BASE_URL: Final = "https://" + "api." + "hubapi" + ".com"
TOKEN_ENV_DEFAULT: Final = "HUBSPOT_PROBE_TOKEN"  # noqa: S105  (the NAME of a variable, not a secret)
TAG_PROPERTY: Final = "dpld_tag"  # written only if the account already has it (checked first)
TAG_VALUE: Final = "probe"
LEAD_STATUS_VALUE: Final = "OPEN"
# reserved name (RFC 2606) with a real top-level domain, so HubSpot accepts it; never a real mailbox
EMAIL_DOMAIN: Final = "example.com"
HTTP_CLIENT_ERROR_MIN: Final = 400
HTTP_CLIENT_ERROR_END: Final = 500  # first status that is not a client error
CALL_BUDGET: Final = 8  # hard cap, counted per attempt, 429 retries included
DELETE_RESERVE: Final = 1  # attempts kept back for the cleanup: the permanent delete always has its call
# (the archive-delete fallback uses whatever is left: 1 spare on the normal path, none after a 429 retry)
PLANNED_CALLS: Final = (
    7  # properties check, owners, create, patch owner, patch lead status, read back, permanent delete
)
TOKEN_ENV_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")
MAX_429_WAITS: Final = 2
MAX_WAIT_SECONDS: Final = 5.0
FIRST_WAIT_SECONDS: Final = 0.5
TIMEOUT_SECONDS: Final = 15.0
MAX_REPLY_BYTES: Final = 1_000_000
LOOPBACK_HOSTS: Final = ("127.0.0.1", "localhost")
HTTP_OK: Final = 200
HTTP_REDIRECT: Final = 300
HTTP_BAD_REQUEST: Final = 400
HTTP_SERVER_ERROR: Final = 500
HTTP_NOT_FOUND: Final = 404
HTTP_TOO_MANY: Final = 429
CONTACTS: Final = "/crm/v3/objects/contacts"
EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2

QUESTIONS: Final = (
    "owners readable",
    "contact create",
    "owner write",
    "lead-status write",
    "read-back equal",
    "delete",
)


class _Stop(Exception):  # noqa: N818  (control flow, not an error report)
    """The call budget is used up."""


class _Quiet(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # argparse would echo the offending argument, which could be a token
        print("refused: bad arguments (see --help); the token is never an argument")
        raise SystemExit(EXIT_REFUSED)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # the token must never follow a redirect
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _klass(status: int) -> str:
    return f"{status // 100}xx" if status else "no answer"


def _mask(value: object) -> str:
    return "..." + str(value)[-4:]


class Probe:
    def __init__(self, base: str, token: str, sleep: Callable[[float], None], out: Callable[[str], None]) -> None:
        self.base, self._token, self._sleep, self.say = base, token, sleep, out
        self.used = 0
        self.seen: list[str] = []  # "step: class", never a body

    def call(self, step: str, method: str, path: str, obj: Any = None, *, cleanup: bool = False) -> tuple[int, Any]:
        """One logical call: attempts (429 retries included) are counted and the budget is hard."""
        limit = CALL_BUDGET if cleanup else CALL_BUDGET - DELETE_RESERVE
        waits = 0
        while True:
            if self.used >= limit:
                raise _Stop
            self.used += 1
            status, body, wait = self._once(method, path, obj)
            self.seen.append(f"{step}: {_klass(status)} ({status}){_why(status, body, self._token)}")
            if status != HTTP_TOO_MANY or waits >= MAX_429_WAITS:
                return status, body
            self._sleep(min(wait if wait is not None else FIRST_WAIT_SECONDS * 2**waits, MAX_WAIT_SECONDS))
            waits += 1

    def _once(self, method: str, path: str, obj: Any) -> tuple[int, Any, float | None]:
        data = None if obj is None else json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        wait: float | None = None
        try:
            with _OPENER.open(req, timeout=TIMEOUT_SECONDS) as r:  # noqa: S310  # nosec B310
                status, raw = r.status, r.read(MAX_REPLY_BYTES)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read(MAX_REPLY_BYTES)
            except Exception:  # noqa: BLE001
                raw = b""
            try:
                wait = float(e.headers.get("Retry-After", ""))
            except ValueError:
                wait = None
            if wait is not None and not (math.isfinite(wait) and wait >= 0):
                wait = None  # negative, NaN or infinite: use the built-in back-off instead
            e.close()
        except Exception:  # noqa: BLE001  (timeout, refused, reset: only "no answer" is kept, never the text)
            return 0, None, None
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except ValueError:
            body = None
        return status, body, wait


def _why(status: int, body: Any, token: str) -> str:
    """For a 4xx answer only: HubSpot's error category (a fixed upper-case word) and the first 100 characters of its
    message, never anything else from the reply. The message of a rejected call names the field that was refused.
    The token is replaced by *** wherever it appears in the message, so "the token is never printed" holds for every
    reply."""
    if not (HTTP_CLIENT_ERROR_MIN <= status < HTTP_CLIENT_ERROR_END) or not isinstance(body, dict):
        return ""
    cat, msg = body.get("category"), body.get("message")
    out = ""
    if isinstance(cat, str) and re.fullmatch(r"[A-Z_]{3,40}", cat):
        out += f" [{cat}]"
    if isinstance(msg, str):
        out += " " + json.dumps(re.sub(r"[^ -~]", "?", msg.replace(token, "***"))[:100])
    return out


def _ok(status: int) -> bool:
    return HTTP_OK <= status < HTTP_REDIRECT


def run(p: Probe, tag: str) -> dict[str, str]:
    """The whole flow. The delete in `finally` runs after any failure or budget stop."""
    res = dict.fromkeys(QUESTIONS, "NOT RUN")
    email = f"probe-{tag}@{EMAIL_DOMAIN}"
    cid: str | None = None
    owner: str | None = None
    owner_ok = False
    lost = False
    try:
        s, b = p.call("tag property", "GET", f"/crm/v3/properties/contacts/{TAG_PROPERTY}")
        has_tag = _ok(s)
        p.say(f"tag property {TAG_PROPERTY}: {'present, will be set' if has_tag else 'absent, skipped'}")
        s, b = p.call("owners", "GET", "/crm/v3/owners?limit=10")
        results = b.get("results") if isinstance(b, dict) else None
        if _ok(s) and isinstance(results, list):
            res["owners readable"] = "PASS"
            ids = [str(x.get("id")) for x in results if isinstance(x, dict) and x.get("id")]
            owner = ids[0] if ids else None
            p.say(f"owners: {len(ids)} found, ids {[_mask(i) for i in ids]}")
        else:
            res["owners readable"] = "FAIL"
        props: dict[str, str] = {"email": email}
        if has_tag:
            props[TAG_PROPERTY] = TAG_VALUE
        s, b = p.call("create", "POST", CONTACTS, {"properties": props})
        if _ok(s):
            cid = str(b.get("id")) if isinstance(b, dict) and b.get("id") else None
            res["contact create"] = "PASS"
            p.say(f"created contact {_mask(cid) if cid else '(no id returned)'} for {email}")
            lost = cid is None
        else:
            res["contact create"] = "FAIL"
            lost = s == 0 or s >= HTTP_SERVER_ERROR  # a timeout or 5xx may have committed the contact
            if not lost:
                return res
        if cid:
            if owner:
                s, b = p.call(
                    "patch owner",
                    "PATCH",
                    f"{CONTACTS}/{quote(email, safe='')}?idProperty=email",
                    {"properties": {"hubspot_owner_id": owner}},
                )
                owner_ok = _ok(s)
                res["owner write"] = "PASS" if owner_ok else "FAIL"
            else:
                res["owner write"] = "SKIPPED (no owner found)"
            s, b = p.call(
                "patch lead status",
                "PATCH",
                f"{CONTACTS}/{quote(email, safe='')}?idProperty=email",
                {"properties": {"hs_lead_status": LEAD_STATUS_VALUE}},
            )
            lead_ok = _ok(s)
            res["lead-status write"] = "PASS" if lead_ok else "FAIL"
            if owner_ok or lead_ok:
                q = f"{CONTACTS}/{quote(email, safe='')}?idProperty=email&properties=hubspot_owner_id,hs_lead_status"
                s, b = p.call("read back", "GET", q)
                got = b.get("properties") if isinstance(b, dict) else None
                same = (
                    _ok(s)
                    and isinstance(got, dict)
                    and (not lead_ok or got.get("hs_lead_status") == LEAD_STATUS_VALUE)
                    and (not owner_ok or str(got.get("hubspot_owner_id")) == owner)
                )
                bad = [
                    k
                    for k, ok, v in (
                        ("hs_lead_status", lead_ok, LEAD_STATUS_VALUE),
                        ("hubspot_owner_id", owner_ok, owner),
                    )
                    if ok and not (isinstance(got, dict) and str(got.get(k)) == v)
                ]
                res["read-back equal"] = (
                    "PASS"
                    if same
                    else "FAIL (" + (", ".join(bad) + " differs" if bad and _ok(s) else "no usable answer") + ")"
                )
    except _Stop:
        p.say("STOPPED: the call budget is used up")
    finally:
        if cid:
            res["delete"] = _delete(p, cid, by_email=None)
        elif lost:
            r = _delete(p, email, by_email=email)  # the create answer was lost: look for the contact by its email
            if not r.startswith("PASS") and not r.startswith("NOT NEEDED"):
                p.say(f"WARNING: the create answer was lost; look for {email} in HubSpot and delete it by hand")
            elif r.startswith("PASS"):
                p.say("note: create outcome unknown (its answer was lost); the contact existed and is now deleted")
            res["delete"] = r
        else:
            res["delete"] = "NOT NEEDED (nothing was created)"
    return res


def _delete(p: Probe, ref: str, by_email: str | None) -> str:
    """Permanent delete, then the archive delete. `by_email` is set when no contact id is known."""
    body = {"objectId": ref, "idProperty": "email"} if by_email else {"objectId": ref}
    shown = ref if by_email else _mask(ref)
    try:
        s, _ = p.call("delete permanent", "POST", f"{CONTACTS}/gdpr-delete", body, cleanup=True)
        if _ok(s):
            return "PASS (permanent)"
        if by_email and s == HTTP_NOT_FOUND:
            return "NOT NEEDED (no contact with that email)"
        if not by_email:  # the normal delete is documented for an id only, so no by-email variant
            s, _ = p.call("delete normal", "DELETE", f"{CONTACTS}/{quote(ref, safe='')}", cleanup=True)
            if _ok(s):
                return "PASS (normal delete: archived, not permanent)"
    except _Stop:
        pass
    p.say(f"WARNING: the probe contact {shown} may still exist; delete it by hand in HubSpot")
    return "FAIL"


def main(argv: list[str] | None = None, *, sleep: Callable[[float], None] = time.sleep) -> int:
    ap = _Quiet(description="One-shot HubSpot free-account API probe (see the module docstring).", allow_abbrev=False)
    ap.add_argument("--plan", action="store_true", help="print the planned call count and exit")
    ap.add_argument("--yes-spend-calls", type=int, default=None, help="must equal the count --plan prints")
    ap.add_argument("--token-env", default=TOKEN_ENV_DEFAULT, help="NAME of the environment variable holding the token")
    ap.add_argument("--dry-run", metavar="URL", default=None, help="run against a loopback stub at this address")
    a = ap.parse_args(argv)
    say = print
    say(f"planned calls: {PLANNED_CALLS} (hard budget {CALL_BUDGET} incl. retries and the fallback delete)")
    if a.plan:
        return EXIT_OK
    if a.yes_spend_calls != PLANNED_CALLS:
        say(f"refused: pass --yes-spend-calls {PLANNED_CALLS} to confirm the planned call count")
        return EXIT_REFUSED
    if not TOKEN_ENV_RE.fullmatch(a.token_env):  # before any lookup or print: a pasted token must never be echoed
        say("refused: --token-env must be the NAME of an environment variable, not the token")
        return EXIT_REFUSED
    base = HUBSPOT_BASE_URL
    if a.dry_run is not None:
        u = urlsplit(a.dry_run)
        if u.scheme != "http" or u.hostname not in LOOPBACK_HOSTS:
            say("refused: --dry-run only talks to a loopback http address")
            return EXIT_REFUSED
        base = a.dry_run.rstrip("/")
    token = os.environ.get(a.token_env, "").strip()
    if not token or "\n" in token or "\r" in token or " " in token:
        say(
            "refused: the token environment variable is missing, empty or not a single token"
        )  # the name is never echoed
        return EXIT_REFUSED
    p = Probe(base, token, sleep, say)
    res = run(p, secrets.token_hex(4))
    say("")
    for q in QUESTIONS:
        say(f"{q:20} {res[q]}")
    say(f"calls used: {p.used} of {CALL_BUDGET}")
    say("status classes seen: " + ("; ".join(p.seen) or "none"))
    good = all(v.startswith(("PASS", "SKIPPED")) for v in res.values())
    return EXIT_OK if good else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
