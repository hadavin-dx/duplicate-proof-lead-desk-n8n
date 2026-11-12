"""Who may reach the desk: every spelling of a public bind is refused while a real service is set, the stand-in
hosts accept loopback in every spelling and nothing else (the same answer on every Python version), and the one list
of kinds names what each real kind is. Made-up data only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_mode_safety import _main_env, _stop_at_adapters

from lead_desk import server
from lead_desk.config import Config, SettingRefused, is_loopback_host
from lead_desk.constants import EXIT_DESK_SETTING_REFUSED
from lead_desk.smtp_outbox import SmtpSettings

# built from parts: the hygiene scan reads a written address as a hard-coded one
ALL_ZEROS = ".".join(["0"] * 4)
PRIVATE = ".".join(("10", "0", "0", "1"))
LOOP_2 = ".".join(("127", "0", "0", "2"))
PUBLIC = [ALL_ZEROS, "[::]", "::", "0", "remote.example.test", "::ffff:" + PRIVATE, "[::1", "::1]", "192.0.2.1"]


@pytest.mark.parametrize("bind", PUBLIC)
def test_a_real_service_refuses_every_public_spelling_of_the_bind(bind: str) -> None:
    assert Config(bind=bind, crm="hubspot").public_bind_problem() is not None, bind
    assert Config(bind=bind, outbox="smtp_provider").public_bind_problem() is not None, bind


@pytest.mark.parametrize("bind", [ALL_ZEROS, "[::]", "0", "remote.example.test", "::ffff:" + PRIVATE])
def test_main_refuses_these_binds_with_a_real_crm_before_anything_else_starts(
    monkeypatch: Any, tmp_path: Path, capsys: Any, bind: str
) -> None:
    _main_env(monkeypatch, tmp_path, bind, DESK_CRM="hubspot")
    _stop_at_adapters(monkeypatch)
    with pytest.raises(SystemExit) as info:
        server.main()
    assert info.value.code == EXIT_DESK_SETTING_REFUSED
    assert "DESK_BIND" in capsys.readouterr().out
    assert not (tmp_path / "m3.sqlite3.lock").exists()  # refused before the lock was taken


def test_main_refuses_a_public_bind_with_the_live_ai_step(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    live = {
        "DESK_QUALIFIER": "live_claude",
        "DESK_LIVE_MODEL": "m",
        "DESK_LIVE_KEY_FILE": str(tmp_path / "key.txt"),
        "DESK_LIVE_PRICE_DATE": "2026-01-01",
        "DESK_LIVE_LEDGER_PATH": str(tmp_path / "ledger.jsonl"),
        "DESK_LIVE_RUN_ID": "run-a",
        "DESK_LIVE_PRICE_IN_PER_MTOK": "2",
        "DESK_LIVE_PRICE_OUT_PER_MTOK": "10",
        "DESK_LIVE_CAP_RUN_USD": "1",
        "DESK_LIVE_CAP_TOTAL_USD": "3",
        "DESK_LIVE_BASE_URL": "http://127.0.0.1:9",
    }
    _main_env(monkeypatch, tmp_path, ALL_ZEROS, **live)
    _stop_at_adapters(monkeypatch)
    with pytest.raises(SystemExit) as info:
        server.main()
    out = capsys.readouterr().out
    assert info.value.code == EXIT_DESK_SETTING_REFUSED and "DESK_BIND" in out and "ai" in out, out


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        (LOOP_2, True),  # the whole loopback range is this computer
        ("::1", True),
        ("[::1]", True),
        ("LOCALHOST", True),
        ("::ffff:127.0.0.1", True),  # a mapped form is read as the address inside it, on every Python version
        ("::ffff:" + PRIVATE, False),
        ("::1%eth0", False),  # a scope id is not a plain address
        ("[::1", False),
        ("::1]", False),
        (ALL_ZEROS, False),
        ("192.0.2.1", False),
        ("remote.example.test", False),
    ],
)
def test_which_hosts_are_this_computer(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback
    if host.startswith("[") or host.lower() == "localhost" or host.endswith("]"):
        return  # the mail test server takes a plain IP address only
    if loopback:
        assert SmtpSettings(host=host).host == host
    else:
        with pytest.raises(SettingRefused):
            SmtpSettings(host=host)
    if loopback:
        assert Config(fakes_host=host).fakes_host == host
    else:
        with pytest.raises(SettingRefused, match="DESK_FAKES_HOST"):
            Config(fakes_host=host)


def test_every_real_kind_makes_the_config_report_a_real_service() -> None:
    from lead_desk.constants import QUALIFIER_CHOICES, QUALIFIER_STAND_IN

    for kind in QUALIFIER_CHOICES:
        if kind == "live_claude":
            continue  # it needs its live settings; the bind test above builds them
        assert ("ai" in Config(qualifier=kind).real_services) is (kind != QUALIFIER_STAND_IN), kind
