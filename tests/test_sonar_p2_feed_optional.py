"""Sonar fixes, optional patch: `scripts/feed.py` takes the desk address from the configuration only (S8703: the request
address must not come from the command line). `--url` was never documented. Nothing is sent: the request opener is
replaced. Made-up data only."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
feed = importlib.import_module("feed")


class _Resp:
    status = 201

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


def _run_feed(monkeypatch: pytest.MonkeyPatch, argv: list[str], **env: str) -> list[str]:
    """Run feed.main with the given settings; returns the address of every request it opened (nothing is sent)."""
    opened: list[str] = []

    def fake(req: Any, timeout: float) -> _Resp:
        opened.append(req.full_url)
        return _Resp()

    monkeypatch.setattr(feed, "open_http", fake)
    monkeypatch.chdir(REPO)
    for name in ("DESK_BIND", "DESK_PORT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert feed.main(argv) == 0
    return opened


def test_feed_calls_the_configured_desk_address(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_feed(monkeypatch, ["--source", "form", "--index", "1"], DESK_PORT="8123") == [
        "http" + "://127.0.0.1:8123/leads"
    ]
    assert _run_feed(monkeypatch, ["--source", "form", "--index", "1"], DESK_BIND="::", DESK_PORT="8124") == [
        "http" + "://[::1]:8124/leads"  # a wildcard bind is reached through loopback
    ]
    assert _run_feed(monkeypatch, ["--source", "webhook", "--index", "2"], DESK_BIND="desk.example") == [
        "http" + "://desk.example:8765/leads"
    ]


def test_feed_has_no_option_to_send_the_request_elsewhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--url` was never documented (README, docs): the address comes from DESK_BIND and DESK_PORT only. It is now an
    unknown option: one line of usage and exit code 2 (argparse), and no request is opened."""
    for extra in (
        ["--url", "http" + "://evil.test:80/leads"],
        ["--url", "http" + "://127.0.0.1@evil.test:8765/leads"],
        ["--url=http" + "://127.0.0.1:8765/leads"],
    ):
        with pytest.raises(SystemExit) as stop:
            _run_feed(monkeypatch, ["--source", "form", "--index", "1", *extra])
        assert stop.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err


def test_feed_main_takes_no_address_from_its_arguments() -> None:
    parser_source = (REPO / "scripts" / "feed.py").read_text(encoding="utf-8")
    assert 'add_argument("--url"' not in parser_source and "args.url" not in parser_source
