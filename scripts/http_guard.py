"""The one place where the helper scripts open a web address: only http and https are allowed, never file:, ftp: or
any other scheme (urllib would open those too). Used by feed.py, n8n_api.py and run_real_check.py."""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


def open_http(req: urllib.request.Request | str, timeout: float) -> Any:
    """urllib.request.urlopen after a scheme check; raises ValueError for any scheme but http and https."""
    url = req if isinstance(req, str) else req.full_url
    if urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("only http and https addresses are opened")
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  # nosec B310  (scheme checked just above)
