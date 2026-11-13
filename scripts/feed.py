"""Send the made-up canned leads to the desk, one request each (the two "doors": source form or webhook).

Run from the repository root:  PYTHONPATH=src python scripts/feed.py --source form --index 1
Each lead in demo_only/canned_leads.json becomes the envelope {"source", "source_ref": "canned-<i>", "payload"}, where
<i> is the position in the file starting at 1. Sending the same --index twice sends the same request id twice
(a replay).
The desk address comes from the desk configuration (DESK_BIND, DESK_PORT), not from this file and not from the command
line: there is no option to point the request anywhere else.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from http_guard import open_http

from lead_desk.config import Config
from lead_desk.constants import SOURCES
from lead_desk.urls import desk_origin

CANNED = Path("demo_only") / "canned_leads.json"


def target_host(bind: str) -> str:
    """The name to call for a bind address. A wildcard bind (the all-zeros address, ::) is reached through loopback,
    because the desk refuses a Host header that names a wildcard address."""
    try:
        ip = ipaddress.ip_address(bind.strip("[]"))
    except ValueError:
        return bind
    if ip.is_unspecified:
        return "[::1]" if isinstance(ip, ipaddress.IPv6Address) else "127.0.0.1"
    return f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)


def build_envelope(source: str, index: int, leads: list[dict[str, object]]) -> dict[str, object]:
    if source not in SOURCES:
        raise ValueError("source must be one of " + ", ".join(SOURCES))
    if not 1 <= index <= len(leads):
        raise ValueError(f"index must be 1..{len(leads)}")
    return {"source": source, "source_ref": f"canned-{index}", "payload": leads[index - 1]}


def post(url: str, envelope: dict[str, object], timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(  # noqa: S310 - the url is built from the http-only desk config
        url, data=json.dumps(envelope).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with open_http(req, timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def main(argv: list[str] | None = None) -> int:
    cfg = Config.from_env()
    ap = argparse.ArgumentParser(description="Send canned made-up leads to the desk.")
    ap.add_argument("--source", choices=SOURCES, required=True)
    ap.add_argument("--index", type=int, help="position in canned_leads.json, starting at 1")
    ap.add_argument("--all", action="store_true", help="send every canned lead once")
    args = ap.parse_args(argv)
    if args.all == (args.index is not None):
        ap.error("give exactly one of --index or --all")
    url = f"{desk_origin(target_host(cfg.bind), cfg.port)}/leads"  # from the configuration only
    leads = json.loads(CANNED.read_text(encoding="utf-8"))
    indexes = range(1, len(leads) + 1) if args.all else [args.index]
    for i in indexes:
        status, body = post(url, build_envelope(args.source, i, leads), cfg.send_timeout_seconds)
        print(f"canned-{i} {args.source} -> {status} {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
