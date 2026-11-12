"""The demo folder is removable: a desk that still asks for the recorded answers says so in plain words."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from lead_desk.config import Config
from lead_desk.loader import load_demo_adapters


def test_missing_demo_only_gives_a_plain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.import_module

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name == "demo_only.fakes":
            raise ModuleNotFoundError("No module named 'demo_only'", name="demo_only")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    monkeypatch.setattr("lead_desk.loader.HttpOutbox", lambda *a: object())  # no fake server is running here
    with pytest.raises(RuntimeError, match="folder demo_only is missing"):
        load_demo_adapters(Config())
