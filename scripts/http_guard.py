"""The one place where the helper scripts open a web address: only http and https are allowed, never file:, ftp: or
any other scheme (urllib would open those too). Used by feed.py, n8n_api.py and run_real_check.py."""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses to follow a redirect: the 3xx answer is raised as urllib.error.HTTPError instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def open_http(
    req: urllib.request.Request | str, timeout: float, follow_redirects: bool = True, use_proxy: bool = True
) -> Any:
    """urllib.request.urlopen after a scheme check; raises ValueError for any scheme but http and https.
    With follow_redirects=False a redirect is not followed (urllib would send every request header, an API key
    included, to the new address): the 3xx answer is raised as urllib.error.HTTPError.
    With use_proxy=False the proxy settings of the environment (HTTP_PROXY, HTTPS_PROXY, no_proxy) are ignored and the
    request goes straight to the address (for a request to this computer that carries a key in a header)."""
    url = req if isinstance(req, str) else req.full_url
    if urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("only http and https addresses are opened")
    if not follow_redirects or not use_proxy:
        handlers: list[Any] = [] if follow_redirects else [_NoRedirect]
        if not use_proxy:
            handlers.append(urllib.request.ProxyHandler({}))  # an empty map: no proxy for any address
        return urllib.request.build_opener(*handlers).open(req, timeout=timeout)  # noqa: S310  # nosec B310
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  # nosec B310  (scheme checked just above)
