"""scripts/http_guard.py opens http and https addresses only; the security scan covers scripts/ too."""

from __future__ import annotations

import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from http_guard import open_http  # noqa: E402


@pytest.mark.parametrize(
    "url", ["file:///etc/hosts", "ftp://host.example.test/x", "data:text/plain,hi", "gopher://h/x"]
)
def test_a_scheme_other_than_http_and_https_is_refused_before_anything_is_opened(url: str) -> None:
    with pytest.raises(ValueError, match="only http and https"):
        open_http(url, 1)
    with pytest.raises(ValueError, match="only http and https"):
        open_http(urllib.request.Request(url), 1)  # noqa: S310  (the guard must refuse it)


def test_an_http_address_is_really_tried() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(urllib.error.URLError):  # nothing listens on the port that was just freed
        open_http(f"http://127.0.0.1:{port}/", 1)


def test_the_security_scan_covers_the_scripts_folder() -> None:
    workflow = (REPO / ".github" / "workflows" / "lint.yml").read_text(encoding="utf-8")
    assert "bandit -ll -r src tools scripts" in workflow
    config = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    exclude = next(ln for ln in config.splitlines() if ln.startswith("exclude_dirs"))
    assert '"scripts"' not in exclude
