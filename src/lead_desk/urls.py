"""The one place that names the scheme of the desk's own address.

The desk and the stand-ins speak plain HTTP on purpose: they run on this computer and listen on a loopback address
(`Config.public_bind_problem` refuses a public bind when a real service is in use; `Config.problem` refuses a stand-ins
host that is not loopback). The scheme is named once here, with this reason, and the addresses built by `desk_origin`
use it. Not every address is built here: a few files (for example the demo code and the scripts) still write a literal
loopback address of their own. No test checks those literal addresses; the one scan in tests (it looks for an
address built from a host written in braces) does not cover them. Whoever writes an address for another host
must use a secure scheme."""

from __future__ import annotations

from typing import Final

DESK_SCHEME: Final = "http"  # loopback only: see the module text


def desk_origin(host: str, port: int | str) -> str:
    """scheme://host:port with no path. `host` is a bind or stand-ins host as the configuration holds it (an IPv6
    address keeps its brackets)."""
    return f"{DESK_SCHEME}://{host}:{port}"
