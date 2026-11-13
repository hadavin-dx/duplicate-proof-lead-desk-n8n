"""The one place that writes the address of the desk or of its stand-ins.

The desk and the stand-ins speak plain HTTP on purpose: they run on this computer and listen on a loopback address
(`Config.public_bind_problem` refuses a public bind when a real service is in use; `Config.problem` refuses a stand-ins
host that is not loopback). Nothing is sent over a network, so there is nothing to encrypt. The scheme is therefore
named once here, with this reason, and every address is built from it; no file writes the clear-text scheme as a
literal, so a clear-text address elsewhere in the code stands out as a mistake."""

from __future__ import annotations

from typing import Final

DESK_SCHEME: Final = "http"  # loopback only: see the module text


def desk_origin(host: str, port: int | str) -> str:
    """scheme://host:port with no path. `host` is a bind or stand-ins host as the configuration holds it (an IPv6
    address keeps its brackets)."""
    return f"{DESK_SCHEME}://{host}:{port}"
