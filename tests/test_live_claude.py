"""Live AI step: the adapter, the spend ledger, the settings, the loader choice, the trace wrappers, the check runner.
Everything runs against the local fake model server (demo_only/fake_model_server.py) with a fake key built from parts.
No real model, no real key."""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from support import REPO, lead, start_serving, stop_serving

from demo_only.fake_model_server import ModelState, make_model_server
from lead_desk import live_claude as lc
from lead_desk.config import Config, LiveSettings
from lead_desk.constants import LIVE_DEFAULT_BASE_URL
from lead_desk.live_claude import (
    LiveBadReply,
    LiveCapReached,
    LiveQualifier,
    LiveRateLimited,
    LiveRefused,
    LiveTransport,
    SpendLedger,
)
from lead_desk.trace import CrashWindows, TracedCrm, TracedOutbox, TracedQualifier, TraceHooks, Tracer

FAKE_KEY = "sk-" + "fake-" + "key-" + "0123456789" + "abcdef"  # built from parts: not a real key
PRICE_IN, PRICE_OUT = Decimal("2"), Decimal("10")  # fake prices for tests only


class Model:
    def __init__(self, state: ModelState, url: str) -> None:
        self.state, self.url = state, url


@pytest.fixture
def model() -> Iterator[Model]:
    srv, state = make_model_server(port=0, mode="ok", key=FAKE_KEY)
    th = start_serving(srv)
    try:
        yield Model(state, f"http://127.0.0.1:{srv.server_address[1]}")
    finally:
        state.stop.set()
        stop_serving(srv, th)


def settings(tmp_path: Path, m: Model, **over: Any) -> LiveSettings:
    key_file = tmp_path / "key.txt"
    if not key_file.exists():
        key_file.write_text(FAKE_KEY + "\n", encoding="utf-8")
    kw: dict[str, Any] = {
        "model": "fake-model-1",
        "key_file": key_file,
        "price_in_per_mtok": PRICE_IN,
        "price_out_per_mtok": PRICE_OUT,
        "price_date": "2026-01-01",
        "cap_run_usd": Decimal("1"),
        "cap_total_usd": Decimal("3"),
        "ledger_path": tmp_path / "ledger.jsonl",
        "run_id": "run-a",
        "base_url": m.url,
        "timeout_seconds": 2.0,
    }
    kw.update(over)
    return LiveSettings(**kw)


def test_ok_label_and_cost_log(tmp_path: Path, model: Model) -> None:
    model.state.label = "hot"
    q = LiveQualifier(settings(tmp_path, model))
    assert q.qualify("L1", "a@example.test") == "hot"
    assert q.mode == "live" and model.state.calls == 1 and model.state.auth_failures == 0
    body = model.state.last_request
    assert body["model"] == "fake-model-1" and body["max_tokens"] == 64
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config"}  # no temperature, tools...
    assert body["output_config"] == {"effort": "low"}
    assert model.state.last_headers["anthropic-version"] == "2026-10-21" and model.state.last_headers["has_key"]
    assert FAKE_KEY not in json.dumps(body)
    ledger = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["op"] for x in ledger] == ["reserve", "settle"]
    assert ledger[0]["price_date"] == "2026-01-01" and ledger[0]["model"] == "fake-model-1"
    assert Decimal(ledger[1]["usd"]) == (PRICE_IN * 40 + PRICE_OUT * 2) / 1_000_000  # the reported tokens
    assert Decimal(ledger[1]["usd"]) < Decimal(ledger[0]["usd"])  # the reserve was the worst case


@pytest.mark.parametrize(
    ("mode", "exc"),
    [
        ("r429", LiveRateLimited),
        ("malformed", LiveBadReply),
        ("refusal", LiveRefused),
        ("r5xx", LiveTransport),
        ("badlabel", LiveBadReply),
    ],
)
def test_failure_modes_raise_classed_errors(tmp_path: Path, model: Model, mode: str, exc: type[Exception]) -> None:
    model.state.mode = mode
    q = LiveQualifier(settings(tmp_path, model))
    with pytest.raises(exc) as info:
        q.qualify("L1", "a@example.test")
    assert model.state.calls == 1  # no retry inside the adapter
    assert FAKE_KEY not in str(info.value) and FAKE_KEY not in repr(info.value)
    if mode == "r429":
        assert info.value.retry_after == 2.0  # type: ignore[attr-defined]


def test_error_body_and_retry_after_are_read_defensively() -> None:
    assert lc.error_type(b'{"type":"error","error":{"type":"overloaded_error","message":"x"}}') == "overloaded_error"
    for bad in (b"", b"{not json", b"[]", b'{"error":"x"}', b'{"error":{"type":5}}', b'{"error":{"type":"Bad Text!"}}'):
        assert lc.error_type(bad) is None
    assert lc._retry_after("7") == 7.0 and lc._retry_after("0") == 0.0
    for bad in ("-1", "inf", "nan", "Wed, 21 Oct 2026 GMT", "", None):
        assert lc._retry_after(bad) is None


def test_5xx_names_the_error_type_only(tmp_path: Path, model: Model) -> None:
    model.state.mode = "r5xx"
    with pytest.raises(LiveTransport, match=r"500 \(api_error\)") as info:
        LiveQualifier(settings(tmp_path, model)).qualify("L1", "a@example.test")
    assert "boom" not in str(info.value)


def test_worst_case_reserve_uses_max_tokens_as_output(tmp_path: Path, model: Model) -> None:
    s = settings(tmp_path, model, max_tokens=500)
    assert lc.worst_case_usd(s, 0) == PRICE_OUT * 500 / 1_000_000


def test_timeout_is_hard_and_short(tmp_path: Path, model: Model) -> None:
    model.state.mode = "timeout"
    q = LiveQualifier(settings(tmp_path, model, timeout_seconds=1.0))
    t0 = time.monotonic()
    with pytest.raises(LiveTransport):
        q.qualify("L1", "a@example.test")
    assert time.monotonic() - t0 < 4.0  # far below the 25 s reaper window
    assert model.state.calls == 1


def test_slow_drip_hits_the_whole_call_deadline(tmp_path: Path, model: Model) -> None:
    model.state.mode = "drip"  # every read returns quickly, so only the whole-call deadline can stop it
    q = LiveQualifier(settings(tmp_path, model, timeout_seconds=1.0))
    t0 = time.monotonic()
    with pytest.raises(LiveTransport):
        q.qualify("L1", "a@example.test")
    assert time.monotonic() - t0 < 3.0


def test_slow_three_seconds_still_answers_inside_the_limit(tmp_path: Path, model: Model) -> None:
    model.state.mode = "slow"
    q = LiveQualifier(settings(tmp_path, model, timeout_seconds=8.0))
    assert q.qualify("L1", "a@example.test") == "warm"


def test_key_never_leaks(tmp_path: Path, model: Model, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("sk-" + "wrong-" + "key-" + "9876543210", encoding="utf-8")
    q = LiveQualifier(settings(tmp_path, model, key_file=wrong))
    with pytest.raises(LiveTransport) as info:  # the fake answers 401
        q.qualify("L1", "a@example.test")
    assert model.state.auth_failures == 1
    assert "9876543210" not in str(info.value) and "9876543210" not in caplog.text
    # a missing key file is a classed error that names no path (and chains no OSError that holds one)
    q2 = LiveQualifier(settings(tmp_path, model, key_file=tmp_path / "nokey.txt"))
    with pytest.raises(LiveTransport) as info2:
        q2.qualify("L1", "a@example.test")
    assert "nokey" not in str(info2.value) and info2.value.__cause__ is None and info2.value.__suppress_context__
    assert model.state.calls == 1  # no key, no call


def test_settings_need_money_values() -> None:
    base: dict[str, Any] = {
        "model": "m",
        "key_file": Path("k"),
        "price_in_per_mtok": Decimal("1"),
        "price_out_per_mtok": Decimal("1"),
        "price_date": "d",
        "cap_run_usd": Decimal("1"),
        "cap_total_usd": Decimal("2"),
        "ledger_path": Path("l"),
        "run_id": "r",
    }
    LiveSettings(**base)
    with pytest.raises(TypeError):
        LiveSettings(**{k: v for k, v in base.items() if k != "cap_total_usd"})  # no default for a cap
    for bad in (
        {"price_in_per_mtok": Decimal("0")},
        {"cap_run_usd": Decimal("5")},  # above the total
        {"timeout_seconds": 30.0},  # above the desk's AI call budget
        {"timeout_seconds": 9.1},  # the desk cuts every AI call at 9 s
        {"base_url": "http://example.test"},  # plain http only for loopback
        {"base_url": "ftp://x"},
        {"price_date": " "},
    ):
        with pytest.raises(ValueError):
            LiveSettings(**{**base, **bad})


def _live_kw(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": "m",
        "key_file": Path("k"),
        "price_in_per_mtok": Decimal("1"),
        "price_out_per_mtok": Decimal("1"),
        "price_date": "d",
        "cap_run_usd": Decimal("1"),
        "cap_total_usd": Decimal("2"),
        "ledger_path": Path("l"),
        "run_id": "r",
    }
    return base | over


def test_the_key_goes_only_to_the_providers_host_or_this_computer() -> None:
    """The check is in the settings themselves (not in one script): a stranger's https host is refused when the
    settings are made, so the key can never be sent there by any caller. The opt-out is explicit."""
    stranger = "https://" + "model-host" + ".example.test"
    LiveSettings(**_live_kw())  # the provider's own default
    LiveSettings(**_live_kw(base_url="http://127.0.0.1:9"))
    LiveSettings(**_live_kw(base_url="https://localhost"))
    with pytest.raises(ValueError, match="not the provider's own address"):
        LiveSettings(**_live_kw(base_url=stranger))
    with pytest.raises(ValueError, match="not the provider's own address"):
        LiveSettings(**_live_kw(base_url=LIVE_DEFAULT_BASE_URL.replace("api.", "api.evil.")))  # a look-alike
    assert LiveSettings(**_live_kw(base_url=stranger, allow_any_base_url=True)).base_url == stranger


def test_the_environment_opt_out_is_the_word_yes_only() -> None:
    stranger = "https://" + "model-host" + ".example.test"
    env = {
        "DESK_QUALIFIER": "live_claude",
        "DESK_LIVE_MODEL": "m",
        "DESK_LIVE_KEY_FILE": "k",
        "DESK_LIVE_PRICE_DATE": "d",
        "DESK_LIVE_LEDGER_PATH": "l",
        "DESK_LIVE_RUN_ID": "r",
        "DESK_LIVE_PRICE_IN_PER_MTOK": "1",
        "DESK_LIVE_PRICE_OUT_PER_MTOK": "1",
        "DESK_LIVE_CAP_RUN_USD": "1",
        "DESK_LIVE_CAP_TOTAL_USD": "2",
        "DESK_LIVE_BASE_URL": stranger,
    }
    for word in (None, "no", "true", "1"):
        with pytest.raises(ValueError, match="not the provider's own address"):
            Config.from_env(env | ({} if word is None else {"DESK_LIVE_ALLOW_ANY_BASE_URL": word}))
    live = Config.from_env(env | {"DESK_LIVE_ALLOW_ANY_BASE_URL": "yes"}).live
    assert live is not None and live.base_url == stranger


def test_the_key_file_path_is_not_printed_with_the_settings() -> None:
    assert "secret-key-path" not in repr(LiveSettings(**_live_kw(key_file=Path("secret-key-path"))))


def test_config_from_env_live(tmp_path: Path) -> None:
    env = {"DESK_QUALIFIER": "live_claude", "DESK_OUTBOX": "smtp"}
    with pytest.raises(ValueError, match="DESK_LIVE_MODEL"):
        Config.from_env(env)
    env.update(
        DESK_LIVE_MODEL="fake-model-1",
        DESK_LIVE_KEY_FILE=str(tmp_path / "k"),
        DESK_LIVE_PRICE_DATE="2026-01-01",
        DESK_LIVE_LEDGER_PATH=str(tmp_path / "l"),
        DESK_LIVE_RUN_ID="r1",
    )
    with pytest.raises(ValueError, match="PRICE_IN"):
        Config.from_env(env)
    env.update(
        DESK_LIVE_PRICE_IN_PER_MTOK="2",
        DESK_LIVE_PRICE_OUT_PER_MTOK="10",
        DESK_LIVE_CAP_RUN_USD="1",
        DESK_LIVE_CAP_TOTAL_USD="3",
    )
    c = Config.from_env(env)
    assert c.qualifier == "live_claude" and c.outbox == "smtp" and c.live is not None
    assert c.live.timeout_seconds == 8.0 and c.live.base_url.startswith("https://")
    assert Config().qualifier == "recorded" and Config().outbox == "http" and Config().adapters == "demo"  # default
    with pytest.raises(ValueError):
        Config(qualifier="live_claude")  # no settings
    with pytest.raises(ValueError):
        Config(outbox="carrier-pigeon")


def test_loader_picks_live_and_smtp(env, tmp_path: Path, model: Model) -> None:
    from lead_desk.loader import load_demo_adapters

    _, fakes_url = env.fakes()
    port = int(fakes_url.rsplit(":", 1)[1])
    live = settings(tmp_path, model)
    _, _, q = load_demo_adapters(Config(qualifier="live_claude", live=live, fakes_port=port))
    assert isinstance(q, LiveQualifier)
    _, _, q2 = load_demo_adapters(Config(fakes_port=port))
    assert getattr(q2, "mode", "") == "recorded"
    _, smtp_outbox, _ = load_demo_adapters(Config(fakes_port=port, outbox="smtp"))
    assert smtp_outbox.capability == "blind"  # the real mail sender, not the fakes server's outbox


# -- spend cap
def test_cap_survives_restart_and_counts_worst_case(tmp_path: Path, model: Model) -> None:
    s = settings(tmp_path, model)
    one = lc.worst_case_usd(s, 400)
    cap = one * 2 + one / 2  # room for two worst-case calls, not three
    s = settings(tmp_path, model, cap_run_usd=cap, cap_total_usd=cap * 2)
    q = LiveQualifier(s)
    q.qualify("L1", "a@example.test")
    q.qualify("L2", "b@example.test")
    q2 = LiveQualifier(s)  # a restart: a new object, the same ledger file
    run_total, total = q2.ledger.totals("run-a")
    assert 0 < run_total < cap and total == run_total
    # actual costs are tiny, so the settled total leaves room: a real worst-case reserve is what is checked
    big = settings(tmp_path, model, cap_run_usd=cap, cap_total_usd=cap * 2, max_tokens=4000)
    with pytest.raises(LiveCapReached):
        LiveQualifier(big).qualify("L3", "c@example.test")
    assert model.state.calls == 2  # the refused call never reached the network


def test_total_cap_spans_runs_and_crash_keeps_reserve(tmp_path: Path, model: Model) -> None:
    s = settings(tmp_path, model)
    ledger = SpendLedger(s.ledger_path)
    one = lc.worst_case_usd(s, 400)
    s2 = settings(tmp_path, model, cap_run_usd=one * 2, cap_total_usd=one * 2)
    ledger.reserve(s2, one)  # a reserve with no settle: the process died during a call
    ledger.reserve(s2, one)
    other = settings(tmp_path, model, cap_run_usd=one * 2, cap_total_usd=one * 2, run_id="run-b")
    with pytest.raises(LiveCapReached):
        ledger.reserve(other, one)  # a new run, but the plan total is used up by the two unsettled reserves
    assert ledger.totals("run-a") == (one * 2, one * 2)


def test_broken_ledger_stops_spending(tmp_path: Path, model: Model) -> None:
    s = settings(tmp_path, model)
    s.ledger_path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(LiveCapReached):
        LiveQualifier(s).qualify("L1", "a@example.test")
    assert model.state.calls == 0


def test_ledger_concurrent_reserves_never_pass_the_cap(tmp_path: Path, model: Model) -> None:
    s = settings(tmp_path, model)
    one = lc.worst_case_usd(s, 400)
    s = settings(tmp_path, model, cap_run_usd=one * 5, cap_total_usd=one * 5)
    ledger = SpendLedger(s.ledger_path)
    results: list[str] = []

    def go() -> None:
        try:
            ledger.reserve(s, one)
            results.append("ok")
        except LiveCapReached:
            results.append("cap")

    threads = [threading.Thread(target=go) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert results.count("ok") == 5 and results.count("cap") == 7


# -- the real desk with the live step
def _drive_to_end(rig: Any, lid: str) -> tuple[str, str | None]:
    from lead_desk.gate import SEND_MAX_ATTEMPTS as MAXTRY

    for _ in range(MAXTRY + 1):
        if rig.state(lid)[0] in ("failed", "sent", "unknown"):
            break
        rig.clock.advance(60)
        rig.desk.reap_once()
    return rig.state(lid)


@pytest.mark.parametrize(
    ("mode", "err"),
    [
        ("r429", "internal:LiveRateLimited"),
        ("malformed", "internal:LiveBadReply"),
        ("refusal", "internal:LiveRefused"),
        ("r5xx", "internal:LiveTransport"),
        ("timeout", "internal:LiveTransport"),
    ],
)
def test_desk_failure_ends_in_failed_never_lost(env, tmp_path: Path, model: Model, mode: str, err: str) -> None:
    from lead_desk.gate import SEND_MAX_ATTEMPTS as MAXTRY

    model.state.hang_seconds = 5.0
    model.state.mode = mode
    rig = env.rig()
    rig.desk.qualifier = LiveQualifier(settings(tmp_path, model, timeout_seconds=0.5))
    lid = rig.desk.handle("form", "s1", lead()).body["lead_id"]
    assert rig.state(lid) == ("pending", err)  # first try failed: waiting for the retry, nothing lost
    assert _drive_to_end(rig, lid) == ("failed", err)  # a person sees it in the review list
    assert any(x["item_id"] == "F" + lid for x in rig.desk.review().body)
    assert rig.leads() == 1 and rig.outbox.count == 0 and rig.crm.count == 0
    assert model.state.calls == MAXTRY
    # the person retries after the model is back: exactly one label, one CRM row, one email
    model.state.mode = "ok"
    assert rig.desk.retry("F" + lid).body["state"] == "sent"
    assert rig.crm.count == 1 and rig.outbox.count == 1 and model.state.calls == MAXTRY + 1


def test_desk_slow_model_works(env, tmp_path: Path, model: Model) -> None:
    model.state.mode = "slow"
    model.state.slow_seconds = 0.5
    rig = env.rig()
    rig.desk.qualifier = LiveQualifier(settings(tmp_path, model))
    lid = rig.desk.handle("form", "s1", lead()).body["lead_id"]
    assert rig.state(lid)[0] == "sent" and rig.outbox.count == 1


def test_duplicates_never_call_the_model(env, tmp_path: Path, model: Model) -> None:
    rig = env.rig()
    rig.desk.qualifier = LiveQualifier(settings(tmp_path, model))
    rig.desk.handle("form", "s1", lead())
    assert model.state.calls == 1
    rig.desk.handle("webhook", "m1", lead(phone="555 0199"))  # the same person
    rig.desk.handle("form", "s1", lead())  # a replay
    rig.desk.handle("form", "s2", lead(name=None))  # incomplete: review
    assert model.state.calls == 1 and rig.leads() == 1 and rig.outbox.count == 1


def test_cap_holds_with_five_refusals(env, tmp_path: Path, model: Model) -> None:
    from lead_desk.gate import SEND_MAX_ATTEMPTS as MAXTRY

    model.state.mode = "refusal"
    s0 = settings(tmp_path, model)
    one = lc.worst_case_usd(s0, 400)
    s = settings(tmp_path, model, cap_run_usd=one * 3, cap_total_usd=one * 3)
    rig = env.rig()
    rig.desk.qualifier = LiveQualifier(s)
    lid = rig.desk.handle("form", "s1", lead()).body["lead_id"]
    end = _drive_to_end(rig, lid)
    assert MAXTRY == 5
    # refusals are settled at the tiny real cost, so all five tries run; the cap bound is the ledger's own total
    run_total, _ = SpendLedger(s.ledger_path).totals("run-a")
    assert run_total <= s.cap_run_usd and end[0] == "failed" and model.state.calls == 5
    # now a tight cap: only two worst-case reserves fit, so the later tries end with LiveCapReached without a call
    tight = settings(tmp_path, model, cap_run_usd=one * 2, cap_total_usd=one * 2, run_id="run-t", max_tokens=2000)
    rig2 = env.rig()
    rig2.desk.qualifier = LiveQualifier(tight)
    before = model.state.calls
    lid2 = rig2.desk.handle("form", "s2", lead(email="z@example.test")).body["lead_id"]
    end2 = _drive_to_end(rig2, lid2)
    assert end2 == ("failed", "internal:LiveCapReached") and model.state.calls == before


# -- trace and crash windows
def test_trace_wrappers_and_hooks(env, tmp_path: Path) -> None:
    tr = Tracer(tmp_path / "trace.jsonl")
    rig = env.rig(outbox="keyed")
    d = rig.desk
    d.qualifier = TracedQualifier(d.qualifier, tr)
    d.crm = TracedCrm(d.crm, tr)
    d.outbox = TracedOutbox(d.outbox, tr)
    d.hooks = TraceHooks(tr)
    lid = d.handle("form", "s1", lead()).body["lead_id"]
    rows = [json.loads(x) for x in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    stages = [r["stage"] for r in rows]
    assert {"ai", "crm", "send", "hook:D1", "hook:D2"} <= set(stages)
    for r in rows:
        assert r["t_end_ms"] >= r["t_start_ms"] > 0 and r["outcome"] == "ok"
    assert {r["id"] for r in rows if r["stage"] in ("ai", "crm", "send")} == {lid}
    assert d.outbox.capability == "keyed"  # attributes pass through


def test_trace_records_a_failure_and_reraises(tmp_path: Path) -> None:
    class Boom:
        def qualify(self, lead_id: str, email_key: str) -> str:
            raise KeyError("x")

    tr = Tracer(tmp_path / "t.jsonl")
    with pytest.raises(KeyError):
        TracedQualifier(Boom(), tr).qualify("L1", "a@example.test")
    assert json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8"))["outcome"] == "KeyError"


@pytest.mark.parametrize("window", ["ai_after", "crm_before_put", "crm_after_put", "send_before", "send_after"])
def test_crash_window_kills_process_and_a_restart_loses_nothing(tmp_path: Path, window: str) -> None:
    """A subprocess desk core (no HTTP) dies at the window; a second process on the same lead book finishes the lead:
    one CRM row, at most one email (a keyed fake sender, so exactly one)."""
    script = tmp_path / "child.py"
    script.write_text(
        "import sys, json\n"
        "from pathlib import Path\n"
        "from demo_only.fakes import FakeCrm, KeyedOutbox, RecordedQualifier\n"
        "from lead_desk.config import Config\n"
        "from lead_desk.desk import Desk\n"
        "from lead_desk.trace import CrashWindows\n"
        "tmp = Path(sys.argv[1]); flag = tmp / 'flag'\n"
        "(tmp / 'r.json').write_text('{}')\n"
        "cfg = Config(db_path=tmp / 'd.sqlite3', port=0, replies_path=tmp / 'r.json')\n"
        "state = tmp / 'state.json'\n"
        "crm, ob = FakeCrm(), KeyedOutbox()\n"
        "if state.exists():\n"
        "    s = json.loads(state.read_text()); crm.rows = s['crm']; ob.messages = s['ob']\n"
        "w = CrashWindows(flag)\n"
        "d = Desk(cfg, w.crm(crm), w.outbox(ob), w.qualifier(RecordedQualifier(tmp / 'r.json')))\n"
        "d.startup()\n"
        "def save():\n"
        "    state.write_text(json.dumps({'crm': crm.rows, 'ob': ob.messages}))\n"
        "import lead_desk.contract\n"
        "orig_put, orig_send = crm.put, ob.send\n"
        "def put(*a):\n"
        "    r = orig_put(*a); save(); return r\n"
        "def send(*a, **k):\n"
        "    r = orig_send(*a, **k); save(); return r\n"
        "crm.put, ob.send = put, send\n"
        "if sys.argv[2] == 'first':\n"
        "    d.handle('form', 's1', {'name': 'Ada', 'email': 'ada@example.test'})\n"
        "if sys.argv[2] != 'first':\n"
        "    d.reap_once(); d.reap_once()\n"
        "rows = d._conn().execute('SELECT state, crm_done FROM followups').fetchall()\n"
        "print(json.dumps({'rows': rows, 'crm': len(crm.rows), 'ob': len(ob.messages)}))\n",
        encoding="utf-8",
    )
    from support import child_env

    env_vars = child_env()
    (tmp_path / "flag").write_text(window, encoding="utf-8")
    first = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path), "first"],
        capture_output=True,
        text=True,
        env=env_vars,
        timeout=60,
        check=False,
    )
    assert first.returncode == 137 and "simulate crash" in first.stdout, first.stderr[-500:]
    assert not (tmp_path / "flag").exists()  # one-shot
    second = subprocess.run(  # noqa: S603
        [sys.executable, str(script), str(tmp_path), "second"],
        capture_output=True,
        text=True,
        env=env_vars,
        timeout=60,
        check=False,
    )
    assert second.returncode == 0, second.stderr[-500:]
    out = json.loads(second.stdout.strip().splitlines()[-1])
    assert out["rows"] in ([["sent", 1]], [["unknown", 2]], [["unknown", 1]]), out
    assert out["crm"] <= 1 and out["ob"] <= 1


def test_crash_windows_ignore_other_names(tmp_path: Path) -> None:
    flag = tmp_path / "f"
    flag.write_text("send_after", encoding="utf-8")
    w = CrashWindows(flag)
    w.hit("ai_after")  # a different window: nothing happens
    assert flag.exists()
    with pytest.raises(ValueError):
        w.hit("nonsense")


# -- check runner against the fake
def _run_runner(tmp_path: Path, model: Model, extra: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    key = tmp_path / "key.txt"
    key.write_text(FAKE_KEY, encoding="utf-8")
    cmd = [
        sys.executable,
        str(REPO / "scripts" / "run_live_check.py"),
        "--key-file",
        str(key),
        "--base-url",
        model.url,
        "--ledger",
        str(tmp_path / "runner-ledger.jsonl"),
        "--price-date",
        "2026-01-01",
        *extra,
    ]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603


def test_runner_prints_counts_only_and_asks_first(tmp_path: Path, model: Model) -> None:
    model.state.mode = "mixed"
    args = ["--cap-run", "1", "--cap-total", "3", "--model", "fake-a=2,10", "--model", "fake-b=1,5"]
    no = _run_runner(tmp_path, model, args, stdin="n\n")
    assert no.returncode == 1 and "MAXIMUM cost" in no.stdout and "nothing was called" in no.stdout
    assert model.state.calls == 0  # the question came before the first call
    yes = _run_runner(tmp_path, model, args, stdin="y\n")
    assert yes.returncode == 0, yes.stderr[-500:]
    assert FAKE_KEY not in yes.stdout + yes.stderr
    assert model.state.calls == 72  # 2 models x 12 leads x 3 runs
    assert "model fake-a: calls 36, valid labels 36 (100%)" in yes.stdout
    assert "flip rate" in yes.stdout and "p95" in yes.stdout and "per 100 calls" in yes.stdout
    assert "errors by class: none" in yes.stdout
    assert "warm" not in yes.stdout and "hot" not in yes.stdout  # no reply text is printed


def test_runner_refuses_without_caps_or_prices(tmp_path: Path, model: Model) -> None:
    r = _run_runner(tmp_path, model, ["--model", "fake-a=2,10"])  # no caps
    assert r.returncode == 2 and model.state.calls == 0  # argparse: caps are required
    r = _run_runner(tmp_path, model, ["--cap-run", "1", "--cap-total", "3", "--model", "fake-a"])  # no prices
    assert r.returncode == 2 and model.state.calls == 0
    r = _run_runner(tmp_path, model, ["--cap-run", "5", "--cap-total", "3", "--model", "fake-a=2,10"])
    assert r.returncode == 2 and "refused" in r.stdout and model.state.calls == 0  # run cap above total


def test_runner_counts_failures_by_class(tmp_path: Path, model: Model) -> None:
    model.state.mode = "refusal"
    args = ["--cap-run", "1", "--cap-total", "3", "--model", "fake-a=2,10", "--leads", "2", "--runs", "2"]
    r = _run_runner(tmp_path, model, args, stdin="y\n")
    assert r.returncode == 0 and "valid labels 0 (0%)" in r.stdout and "LiveRefused" in r.stdout
    assert "flip rate n/a" in r.stdout


def test_live_adapter_declares_its_limit_for_the_desk_check(tmp_path: Path, model: Any) -> None:
    """The desk's budget check reads `timeout_seconds` from the adapter. The live adapter must declare it, and the
    limit it accepts must be one the desk accepts (9 s yes, 9.1 s no)."""
    from lead_desk import constants

    budget = constants.MODEL_CALL_BUDGET_SECONDS
    q = LiveQualifier(settings(tmp_path, model, timeout_seconds=budget))
    assert q.timeout_seconds == budget
    with pytest.raises(ValueError, match="DESK_LIVE_TIMEOUT_SECONDS"):
        settings(tmp_path, model, timeout_seconds=budget + 0.1)


def test_header_trickle_hits_the_total_deadline(tmp_path: Path, model: Any) -> None:
    """A server that sends its status line one byte at a time never trips a per-operation socket timeout; only a total
    deadline stops it."""
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve() -> None:
        conn, _ = srv.accept()
        conn.recv(65536)
        for b in b"HTTP/1.1 200 OK" + bytes([13, 10]):
            if stop.is_set():
                break
            try:
                conn.sendall(bytes([b]))
            except OSError:
                break
            time.sleep(0.4)
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    st = settings(tmp_path, model, timeout_seconds=1.0)
    st = dataclasses.replace(st, base_url=f"http://127.0.0.1:{srv.getsockname()[1]}")
    t0 = time.monotonic()
    with pytest.raises(LiveTransport):
        LiveQualifier(st).qualify("L1", "a@example.test")
    elapsed = time.monotonic() - t0
    stop.set()
    srv.close()
    assert elapsed < 2.5, elapsed


def test_ledger_append_is_flushed_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = lc.os.fsync
    monkeypatch.setattr(lc.os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    ledger = lc.SpendLedger(tmp_path / "ledger.jsonl")
    ledger._append({"op": "reserve", "id": "x"})
    assert len(calls) == 1


def test_trace_write_failure_does_not_change_the_call(tmp_path: Path) -> None:
    class Inner:
        def put(self, lead_id: str, record: Any) -> str:
            return "PUT"

    tracer = Tracer(tmp_path / "no-such-dir" / "trace.jsonl")  # the file cannot be opened
    assert TracedCrm(Inner(), tracer).put("L1", {}) == "PUT"
    assert tracer.write_failures == 1


def test_busy_ledger_counts_against_the_call_limit(tmp_path: Path, model: Model) -> None:
    q = LiveQualifier(settings(tmp_path, model, timeout_seconds=0.5))
    q.ledger._mutex.acquire()  # another call in this process holds the ledger
    try:
        t0 = time.monotonic()
        with pytest.raises(LiveCapReached):
            q.qualify("L1", "a@example.test")
        assert time.monotonic() - t0 < 2.0  # waited about the limit, not for ever
    finally:
        q.ledger._mutex.release()
    assert model.state.calls == 0


def test_no_request_after_ledger_used_the_limit(tmp_path: Path, model: Model, monkeypatch: pytest.MonkeyPatch) -> None:
    q = LiveQualifier(settings(tmp_path, model, timeout_seconds=0.3))
    real = q.ledger.reserve

    def slow_reserve(*a: Any, **k: Any) -> str:
        rid = real(*a, **k)
        time.sleep(0.5)  # the lock was slow: the limit is gone before the request could start
        return rid

    monkeypatch.setattr(q.ledger, "reserve", slow_reserve)
    with pytest.raises(LiveTransport):
        q.qualify("L1", "a@example.test")
    assert model.state.calls == 0
    ops = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [o["op"] for o in ops] == ["reserve", "settle"] and Decimal(ops[1]["usd"]) == 0
