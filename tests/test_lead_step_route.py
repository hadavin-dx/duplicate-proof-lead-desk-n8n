"""Lead owner, lead stage and reminder, second part: the Contacted step, the step route, the lead detail
keys, the launchers and the settings. Made-up data only (example.test, the demo rota Asha and Ravi)."""

from __future__ import annotations

import copy
import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from support import (
    DEMO_LEADFLOW,
    NUDGE_TO_TEST,
    CrashAt,
    build_rig,
    call,
    lead,
    raw_exchange,
    run_threads,
)
from test_desk_http_edge_cases import book_shape, make_book_at_schema_version_1, sql_names
from test_mutants import FAILS, WeakSchemaEnv, drop_trigger, weaken

from demo_only.fakes import FakeCrm
from lead_desk import server
from lead_desk.config import Config, Leadflow, SettingRefused, read_leadflow
from lead_desk.contract import SimulatedCrash
from lead_desk.desk import Desk, DeskSettingRefused, Reply

FOREIGN = "evil.example"
REPO = Path(__file__).resolve().parent.parent
FLOW_TRIGGERS = {
    "lead_state_no_delete",
    "lead_state_owner_immutable",
    "lead_state_no_regress",
    "nudges_no_delete",
    "nudges_sent_final",
}


def _flow() -> dict[str, Any]:
    return copy.deepcopy(DEMO_LEADFLOW)


def _new(rig: Any, i: int, **over: Any) -> str:
    reply = rig.desk.process("form", f"a2-{i}", lead(email=f"a2p{i}@example.test", **over))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


def _state(rig: Any, lead_id: str) -> tuple[Any, ...]:
    return rig.query("SELECT owner_name, step, step_rank, changed_at FROM lead_state WHERE lead_id=?", (lead_id,))[0]


def _blind_unknown(rig: Any, i: int) -> str:
    rig.outbox.fail_next("record_then_drop")  # sent, reply lost: the follow-up is `unknown`
    lid = str(rig.desk.handle("form", f"a2-u{i}", lead(email=f"a2u{i}@example.test")).body["lead_id"])
    assert rig.state(lid) == ("unknown", "ambiguous")
    return lid


# -- owner
def test_duplicate_never_reassigns(env) -> None:
    """A repeat enquiry (other request id, same email), in any source, never gives the lead a new owner, step or
    reminder row; the next NEW lead continues the turn as if the repeat had not happened."""
    rig = env.rig(leadflow=_flow())
    first = _new(rig, 0)
    before = rig.query("SELECT * FROM lead_state")
    for n, source in enumerate(("form", "webhook", "form")):
        r = rig.desk.process(source, f"dup-{n}", lead(email="A2P0@example.test", phone=f"555 02{n}0"))
        assert r.status == 200 and r.body["verdict"] == "duplicate", r.body
    assert rig.query("SELECT * FROM lead_state") == before
    assert rig.query("SELECT count(*) FROM nudges") == [(0,)]
    second = _new(rig, 1)
    assert [_state(rig, x)[0] for x in (first, second)] == ["Asha", "Ravi"]


@pytest.mark.parametrize("point", ["A", "A2"])
def test_replay_after_crash_one_owner(env, point: str) -> None:
    """The desk is killed before or after the lead and its owner are written; after the restart and a replay of the
    same request there is one lead, one lead_state row, and the next new lead is the NEXT person in turn."""
    rig = env.rig(leadflow=_flow(), hooks=CrashAt(point))
    with pytest.raises(SimulatedCrash):
        rig.desk.process("form", "crash-1", lead(email="crash@example.test"))
    rig.restart()
    replay = rig.desk.process("form", "crash-1", lead(email="crash@example.test"))
    assert replay.status == 201 and replay.body["verdict"] == "new", replay.body
    assert rig.leads() == 1
    assert [r[0] for r in rig.query("SELECT owner_name FROM lead_state")] == ["Asha"]
    assert rig.query("SELECT count(*) FROM lead_state") == [(1,)]
    _new(rig, 5)
    assert [r[0] for r in rig.query("SELECT owner_name FROM lead_state ORDER BY assigned_at, rowid")] == [
        "Asha",
        "Ravi",
    ]


def _balanced_body(env: Any) -> None:
    rig = env.rig(leadflow=_flow())

    def post(i: int) -> Any:
        return lambda: rig.desk.process("form", f"par-{i}", lead(email=f"par{i}@example.test")).status

    assert run_threads([post(i) for i in range(10)]) == [201] * 10
    counts = {n: c for n, c in rig.query("SELECT owner_name, count(*) FROM lead_state GROUP BY owner_name")}
    assert sum(counts.values()) == 10, counts
    assert max(counts.values()) - min(counts.values()) <= 1, f"owner counts differ by at most 1: {counts}"


def test_ten_parallel_leads_balanced(env) -> None:
    """Ten leads arrive at the same moment (one barrier): the count and the owner are taken inside the lead's own
    write, so owners are handed out in turn and the counts differ by at most 1."""
    _balanced_body(env)


# -- step
def test_step_never_backward_both_orders(env) -> None:
    """The step only moves forward, whichever comes first: the follow-up recorded as sent (Contacted) or a posted
    step. A lower or equal step changes nothing."""
    rig = env.rig(leadflow=_flow())
    a = str(rig.desk.handle("form", "o-a", lead(email="oa@example.test")).body["lead_id"])
    assert _state(rig, a)[1:3] == ("contacted", 1)  # the sent follow-up moved New to Contacted
    r = rig.desk.set_step(a, "new")
    assert (r.status, r.body) == (200, {"step": "contacted", "changed": False})
    assert rig.desk.set_step(a, "qualified").body == {"step": "qualified", "changed": True}
    assert rig.desk.set_step(a, "contacted").body == {"step": "qualified", "changed": False}
    b = _new(rig, 2)  # second order: a person posts Won first, the follow-up is sent afterwards
    assert rig.desk.set_step(b, "won").body == {"step": "won", "changed": True}
    rig.desk.run_followup(b)
    assert rig.state(b) == ("sent", None)
    assert _state(rig, b)[1:3] == ("won", 4)  # Contacted did not move it back
    stamp = _state(rig, b)[3]
    rig.clock.advance(50)
    assert rig.desk.set_step(b, "won").body["changed"] is False
    assert _state(rig, b)[3] == stamp  # a refused move does not even touch the time


def test_step_trigger_blocks_lower_rank(env) -> None:
    """The database itself refuses a lower step, a changed name for the same rank, a changed owner and a delete:
    a direct UPDATE or DELETE that skips the desk's forward-only WHERE is stopped by the triggers."""
    rig = env.rig(leadflow=_flow())
    lid = str(rig.desk.handle("form", "t-1", lead()).body["lead_id"])  # step Contacted, rank 1
    con = sqlite3.connect(rig.db_path, isolation_level=None)
    con.execute("PRAGMA recursive_triggers=ON")  # as the desk's own connections: this is what stops INSERT OR REPLACE
    try:
        for sql in (
            "UPDATE lead_state SET step_rank=0, step='new'",
            "UPDATE lead_state SET step='other' WHERE step_rank=1",
            "UPDATE lead_state SET owner_name='Zed'",
            "UPDATE lead_state SET owner_id='o-9'",
            "UPDATE lead_state SET assigned_at=1",
            "DELETE FROM lead_state",
            "INSERT OR REPLACE INTO lead_state(lead_id, owner_id, owner_name, assigned_at, step, step_rank,"
            f" changed_at) VALUES('{lid}','o-1','Asha',1,'new',0,1)",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(sql)
        con.execute("UPDATE lead_state SET step='qualified', step_rank=3")  # forward passes
        con.execute("UPDATE lead_state SET crm_tries=crm_tries+1")  # the copy columns are in no trigger list
    finally:
        con.close()
    assert _state(rig, lid)[:3] == ("Asha", "qualified", 3)


def test_contacted_and_sent_same_transaction(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both writers of a sent follow-up (the send's own record and a person's "sent" answer) move New to Contacted in
    the same write, only when exactly one follow-up row became sent; a lead with no lead_state row is left alone."""
    rig = env.rig(leadflow=_flow(), outbox="blind")
    ok = str(rig.desk.handle("form", "c-1", lead(email="c1@example.test")).body["lead_id"])
    assert rig.state(ok) == ("sent", None) and _state(rig, ok)[1] == "contacted"  # writer 1: _record_send_result
    assert _state(rig, ok)[3] == rig.clock.t
    u = _blind_unknown(rig, 1)
    assert _state(rig, u)[1] == "new"  # an unknown follow-up is not Contacted
    assert rig.desk.resolve(f"F{u}", "not_sent").status == 200
    assert _state(rig, u)[1] == "new"  # "not sent" is not Contacted either
    again = _blind_unknown(rig, 2)
    # all or nothing: when the step write fails, the follow-up stays as it was
    monkeypatch.setattr(Desk, "_mark_contacted", lambda self, c, lid, now: (_ for _ in ()).throw(RuntimeError("x")))
    assert rig.desk.resolve(f"F{again}", "sent").status == 500
    assert rig.state(again) == ("unknown", "ambiguous") and _state(rig, again)[1] == "new"
    monkeypatch.undo()
    r = rig.desk.resolve(f"F{again}", "sent")  # writer 2: resolve
    assert (r.status, r.body["state"]) == (200, "sent")
    assert _state(rig, again)[1] == "contacted"
    assert rig.desk.resolve(f"F{again}", "sent").status == 409  # no second change
    # no lead_state row (a lead from before the feature): the sent write is silent and complete
    old = env.rig(outbox="blind")
    lid = _blind_unknown(old, 3)
    flow_path = old.config.db_path.parent / "flow.json"
    flow_path.write_text(json.dumps(_flow()), encoding="utf-8")
    old.config = dataclasses.replace(
        old.config, leadflow_path=flow_path, leadflow=read_leadflow(flow_path), nudge_to=NUDGE_TO_TEST
    )
    old.restart()
    assert old.desk.resolve(f"F{lid}", "sent").status == 200
    assert old.state(lid) == ("sent", "person: sent")
    assert old.query("SELECT count(*) FROM lead_state") == [(0,)]


def test_old_book_gets_new_tables_and_old_leads_stay_unassigned(env, tmp_path: Path) -> None:
    """A kept book from before the feature gets both tables and all five triggers at start-up, its old leads have no
    owner or step (the step route answers no_state), and a new lead gets the first person."""
    book = make_book_at_schema_version_1(tmp_path / "old", leads=2)
    before = book_shape(book)
    assert not {"lead_state", "nudges"} & sql_names(book, "table")
    assert not FLOW_TRIGGERS & sql_names(book, "trigger")
    kept = build_rig(tmp_path / "old", leadflow=_flow())
    after = book_shape(book)
    assert after != before
    assert {"lead_state", "nudges"} <= sql_names(book, "table")
    assert FLOW_TRIGGERS <= sql_names(book, "trigger")
    assert kept.query("SELECT count(*) FROM lead_state") == [(0,)]
    old_id = kept.query("SELECT lead_id FROM leads ORDER BY created_at LIMIT 1")[0][0]
    assert kept.desk.set_step(old_id, "won").body == {"error": "no_state"}
    assert kept.desk.lead(old_id).body["owner"] is None
    new = _new(kept, 9)
    assert _state(kept, new)[:2] == ("Asha", "new")  # the first person: old leads do not count
    kept.desk.close()


def test_steps_file_mismatch_refuses_start(env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A kept book whose stored steps no longer fit the steps file is refused at start-up (DeskSettingRefused), and
    server.main turns that into exit 4 with the lock given back."""
    rig = env.rig(leadflow=_flow())
    _new(rig, 0)  # stores step new, rank 0
    renamed = _flow()
    renamed["steps"][0]["id"] = "fresh"
    renamed["first"] = "fresh"
    renamed["steps"][1]["id"] = renamed["after_followup"] = "talked"  # the restart below moves the lead to Contacted
    reordered = _flow()
    reordered["steps"][0], reordered["steps"][1] = reordered["steps"][1], reordered["steps"][0]
    reordered["first"], reordered["after_followup"] = "contacted", "new"
    for bad in (renamed, reordered):
        p = tmp_path / "bad-flow.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        rig.config = dataclasses.replace(rig.config, leadflow_path=p, leadflow=read_leadflow(p))
        with pytest.raises(DeskSettingRefused, match="steps"):
            rig.restart()
    longer = _flow()  # a longer list that keeps every stored pair is accepted
    longer["steps"].insert(4, {"id": "extra", "label": "Extra", "crm_status": "OPEN_DEAL"})
    p = tmp_path / "ok-flow.json"
    p.write_text(json.dumps(longer), encoding="utf-8")
    rig.config = dataclasses.replace(rig.config, leadflow_path=p, leadflow=read_leadflow(p))
    rig.restart()
    # server.main: exit 4 and no traceback
    bad_path = tmp_path / "bad-flow2.json"
    bad_path.write_text(json.dumps(renamed), encoding="utf-8")
    monkeypatch.setenv("DESK_DB_PATH", str(rig.db_path))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setenv("DESK_LEADFLOW_FILE", str(bad_path))
    monkeypatch.setenv("DESK_NUDGE_TO", NUDGE_TO_TEST)
    rig.desk.close()
    other = env.rig()
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (FakeCrm(), other.outbox, other.qualifier))
    with pytest.raises(SystemExit) as e:
        server.main()
    assert e.value.code == 4


def _refused(mutate: Any, tmp_path: Path, name: str = "f") -> str:
    data = _flow()
    mutate(data)
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SettingRefused) as e:
        read_leadflow(p)
    assert str(e.value).startswith("DESK_LEADFLOW_FILE"), str(e.value)
    return str(e.value)


def _accepted(mutate: Any, tmp_path: Path) -> Leadflow:
    data = _flow()
    mutate(data)
    p = tmp_path / "ok.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return read_leadflow(p)


def _steps(n: int) -> Any:
    def m(d: dict[str, Any]) -> None:
        d["steps"] = [{"id": f"s{i}", "label": f"S{i}", "crm_status": "NEW"} for i in range(n)]
        d["first"], d["after_followup"], d["reminder_until"] = "s0", "s1", "s2"

    return m


def test_leadflow_config_refusals(tmp_path: Path) -> None:
    """Every refusal of the lead flow settings at its named home; every limit at its edge; no text holds the value."""
    # rota limits
    _accepted(
        lambda d: d.__setitem__("rota", [{"id": f"o{i}", "name": f"N{i}", "crm_owner_id": ""} for i in range(20)]),
        tmp_path,
    )
    _refused(
        lambda d: d.__setitem__("rota", [{"id": f"o{i}", "name": f"N{i}", "crm_owner_id": ""} for i in range(21)]),
        tmp_path,
    )
    _refused(lambda d: d.__setitem__("rota", []), tmp_path)
    _refused(lambda d: d["rota"].append(dict(d["rota"][0], name="Other")), tmp_path)  # duplicate id
    _refused(lambda d: d["rota"].append(dict(d["rota"][0], id="o-3")), tmp_path)  # duplicate name
    # steps limits (three open steps are the least, twelve the most)
    _accepted(_steps(3), tmp_path)
    _accepted(_steps(12), tmp_path)
    _refused(_steps(2), tmp_path)
    _refused(_steps(13), tmp_path)
    # names, labels, ids, crm values
    _accepted(lambda d: d["rota"][0].__setitem__("name", "N" * 64), tmp_path)
    msg = _refused(lambda d: d["rota"][0].__setitem__("name", "ZQXN" * 17), tmp_path)  # 68 characters
    assert "ZQXN" not in msg
    _accepted(lambda d: d["steps"][0].__setitem__("label", "L" * 32), tmp_path)
    _refused(lambda d: d["steps"][0].__setitem__("label", "L" * 33), tmp_path)
    _refused(lambda d: d["rota"][0].__setitem__("name", ""), tmp_path)
    _refused(lambda d: d["rota"][0].__setitem__("name", "Two\nLines"), tmp_path)
    _accepted(lambda d: d["rota"][0].__setitem__("id", "a" * 32), tmp_path)
    _refused(lambda d: d["rota"][0].__setitem__("id", "a" * 33), tmp_path)
    _refused(lambda d: d["rota"][0].__setitem__("id", "O-1"), tmp_path)
    _refused(lambda d: d["steps"][0].__setitem__("id", "-bad"), tmp_path)
    _refused(lambda d: d["closed"].__setitem__("id", "Closed"), tmp_path)
    _refused(lambda d: d["rota"][0].__setitem__("crm_owner_id", "has space"), tmp_path)
    _refused(lambda d: d["steps"][0].__setitem__("crm_status", ""), tmp_path)
    _refused(lambda d: d["closed"].__setitem__("crm_status", "has space"), tmp_path)
    # shape
    _refused(lambda d: d.__setitem__("extra", 1), tmp_path)
    _refused(lambda d: d.pop("closed"), tmp_path)
    _refused(lambda d: d["rota"][0].__setitem__("extra", "x"), tmp_path)
    _refused(lambda d: d["rota"][0].pop("name"), tmp_path)
    _refused(lambda d: d["rota"][0].__setitem__("name", 5), tmp_path)
    _refused(lambda d: d.__setitem__("steps", {}), tmp_path)
    _refused(lambda d: d["steps"].append(dict(d["steps"][0])), tmp_path)  # duplicate step id
    _refused(lambda d: d["steps"].append(dict(d["closed"])), tmp_path)  # a file step named like Closed
    # first / after_followup / reminder_until
    _refused(lambda d: d.__setitem__("first", "closed"), tmp_path)
    _refused(lambda d: d.__setitem__("after_followup", "nowhere"), tmp_path)
    _refused(lambda d: d.__setitem__("after_followup", "new"), tmp_path)  # not after first
    _refused(lambda d: d.__setitem__("reminder_until", "contacted"), tmp_path)  # not after after_followup
    _refused(lambda d: d.__setitem__("reminder_until", "closed"), tmp_path)
    # the reminder wait
    for bad in (True, 0, -1, 2_678_401, "90", 1.5):
        _refused(lambda d, v=bad: d.__setitem__("nudge_after_seconds", v), tmp_path)
    assert _accepted(lambda d: d.__setitem__("nudge_after_seconds", 1), tmp_path).nudge_after_seconds == 1
    assert (
        _accepted(lambda d: d.__setitem__("nudge_after_seconds", 2_678_400), tmp_path).nudge_after_seconds == 2_678_400
    )
    assert _accepted(lambda d: d.pop("nudge_after_seconds"), tmp_path).nudge_after_seconds is None
    assert _accepted(lambda d: d.__setitem__("nudge_after_seconds", None), tmp_path).nudge_after_seconds is None
    # file level
    with pytest.raises(SettingRefused, match="cannot be read"):
        read_leadflow(tmp_path / "missing.json")
    (tmp_path / "big.json").write_bytes(b" " * 16385)
    with pytest.raises(SettingRefused, match="too big"):
        read_leadflow(tmp_path / "big.json")
    (tmp_path / "junk.json").write_bytes(b"\xff\xfe{")
    with pytest.raises(SettingRefused, match="JSON"):
        read_leadflow(tmp_path / "junk.json")
    # Leadflow itself is checked when built by hand
    with pytest.raises(SettingRefused):
        Leadflow(rota=(), steps=(("a", "A", "X"),) * 4, first="a", after_followup="a", reminder_until="a")
    # Config.__post_init__: only what is known when from_env builds the Config
    lf = _accepted(lambda d: None, tmp_path)
    for kw in (
        {"nudge_to": NUDGE_TO_TEST},  # an address but no file
        {"leadflow": lf},  # a Leadflow but no file
        {"leadflow_path": Path("x.json"), "nudge_to": "a b@example.test"},
        {"leadflow_path": Path("x.json"), "nudge_to": "a@example.test, b@example.test"},
        {"leadflow_path": Path("x.json"), "nudge_to": "a@example.test\r\nBcc: z@example.test"},
    ):
        with pytest.raises(SettingRefused):
            Config(**kw)
    Config(leadflow_path=Path("x.json"), nudge_to=NUDGE_TO_TEST)
    # server.main, after the file is read: reminders on without an address, an address with reminders off
    # (exit 4 for both, checked in test_main_leadflow_exits below)


@pytest.fixture
def _main_env(env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    other = env.rig()
    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "main.sqlite3"))
    monkeypatch.setenv("DESK_PORT", "0")
    monkeypatch.setattr(server, "load_demo_adapters", lambda cfg: (FakeCrm(), other.outbox, other.qualifier))
    return other


def test_main_leadflow_exits(_main_env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    on, off = _flow(), _flow()
    off["nudge_after_seconds"] = None
    pon, poff = tmp_path / "on.json", tmp_path / "off.json"
    pon.write_text(json.dumps(on), encoding="utf-8")
    poff.write_text(json.dumps(off), encoding="utf-8")
    monkeypatch.setenv("DESK_LEADFLOW_FILE", str(pon))
    monkeypatch.delenv("DESK_NUDGE_TO", raising=False)
    with pytest.raises(SystemExit) as e:
        server.main()
    assert e.value.code == 4  # reminders on, no address
    monkeypatch.setenv("DESK_DB_PATH", str(tmp_path / "main2.sqlite3"))  # the first exit left its lock behind
    monkeypatch.setenv("DESK_LEADFLOW_FILE", str(poff))
    monkeypatch.setenv("DESK_NUDGE_TO", NUDGE_TO_TEST)
    with pytest.raises(SystemExit) as e2:
        server.main()
    assert e2.value.code == 4  # an address while the file turns reminders off


# -- feature off
@pytest.mark.parametrize("value", [None, "", "off", "  off  "])
def test_mode_off_golden(env, monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """DESK_LEADFLOW_FILE unset, empty or `off`: Config has no path, no row is ever written, the lead detail
    carries the three new keys as null and nothing else differs, and the demo file on disk is not read."""
    e = {} if value is None else {"DESK_LEADFLOW_FILE": value}
    cfg = Config.from_env(e)
    assert cfg.leadflow_path is None and cfg.leadflow is None
    monkeypatch.setattr(server, "read_leadflow", lambda p: pytest.fail("the file was read"))
    rig = env.rig()
    lid = str(rig.desk.handle("form", "g-1", lead()).body["lead_id"])
    assert rig.query("SELECT count(*) FROM lead_state") == [(0,)]
    assert rig.query("SELECT count(*) FROM nudges") == [(0,)]
    body = rig.desk.lead(lid).body
    assert body["owner"] is None and body["step"] is None and body["reminder"] is None
    assert {k: v for k, v in body.items() if k not in ("owner", "step", "reminder")}.keys() == {"lead", "followup"}
    assert rig.desk.set_step(lid, "won").body == {"error": "no_state"}
    # `OFF` in capitals is a file name, so it is a path that main would refuse as unreadable
    assert Config.from_env({"DESK_LEADFLOW_FILE": "OFF"}).leadflow_path == Path("OFF")


def test_demo_launchers_name_the_demo_file_and_the_off_switch() -> None:
    ps = (REPO / "scripts" / "start_all.ps1").read_text(encoding="utf-8")
    sh = (REPO / "scripts" / "start_all.sh").read_text(encoding="utf-8")
    assert ps.index("DESK_LEADFLOW_FILE") < ps.index("python -c")  # before the first helper call
    assert sh.index("DESK_LEADFLOW_FILE") < sh.index("python -c")
    assert "-cne 'off'" in ps and "demo_only/leadflow.json" in ps and "reminder@example.test" in ps
    assert '!= off ]] && [[ -n "$DESK_LEADFLOW_FILE" ]]' in sh and "export DESK_NUDGE_TO" in sh
    assert (REPO / "demo_only" / "leadflow.json").is_file()
    for text in (ps, sh):
        assert "set DESK_LEADFLOW_FILE=off" in text


# -- the step route
def _serve(env: Any, rig: Any) -> str:
    return env.serve(rig)


def test_step_route_check_order_and_gate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """404 not_found and no_state, then 400 bad_step (bad JSON, non-string, unknown step, no value echoed), a foreign
    Host or Origin is 403 before anything, busy is 503, 200 changed true/false, and the Closed rules."""
    rig = env.rig(leadflow=_flow(), outbox="blind")
    base = _serve(env, rig)
    lid = _new(rig, 1)  # no follow-up yet: step New
    st = f"/leads/{lid}/step"
    assert call(base, "POST", "/leads/Lnone/step", {"step": "won"}) == (404, {"error": "not_found"})
    assert call(base, "POST", "/leads/%00x/step", {"step": "won"}) == (404, {"error": "not_found"})
    secret = "zqx-secret-step"
    for raw in (b"not json", b"[]", b'{"step": 5}', b"{}", b'{"step": null}', json.dumps({"step": secret}).encode()):
        status, body = call(base, "POST", st, raw=raw)
        assert (status, body) == (400, {"error": "bad_step"}), raw
    assert secret not in json.dumps(call(base, "POST", st, {"step": secret}))
    assert call(base, "POST", st, {"step": "qualified"}) == (200, {"step": "qualified", "changed": True})
    assert call(base, "POST", st, {"step": "contacted"}) == (200, {"step": "qualified", "changed": False})
    assert call(base, "POST", st, {"step": "qualified"}) == (200, {"step": "qualified", "changed": False})
    assert call(base, "POST", st, {"step": "closed"}) == (200, {"step": "closed", "changed": True})  # from any step
    assert call(base, "POST", st, {"step": "closed"}) == (200, {"step": "closed", "changed": False})
    assert call(base, "POST", st, {"step": "won"}) == (200, {"step": "closed", "changed": False})
    detail = call(base, "GET", f"/leads/{lid}")[1]
    assert (detail["owner"], detail["step"], detail["reminder"]) == ("Asha", "closed", None)
    # routing: other methods and longer paths
    assert call(base, "GET", st)[0] == 405 and call(base, "DELETE", st)[0] == 405
    assert call(base, "POST", st + "/x", {})[0] == 404
    # the gate runs first
    body = b"{}"
    for extra, error in ((f"Host: {FOREIGN}", "forbidden_host"), (f"Origin: http://{FOREIGN}", "forbidden_origin")):
        req = f"POST {st} HTTP/1.1\r\nHost: 127.0.0.1\r\n{extra}\r\nContent-Length: 2\r\nConnection: close\r\n\r\n"
        if extra.startswith("Host"):
            req = req.replace("Host: 127.0.0.1\r\n", "")
        assert raw_exchange(base, req.encode() + body) == (403, {"error": error}), extra
    assert call(base, "GET", f"/leads/{lid}")[1]["step"] == "closed"
    # too large
    big = call(base, "POST", st, raw=b" " * (rig.config.size_cap_bytes + 10))
    assert big[0] == 413
    # a lock that outlasts the retries is 503 busy and changes nothing
    other = _new(rig, 2)

    def locked(self: Desk, fn: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Desk, "_locked_retry", locked)
    assert call(base, "POST", f"/leads/{other}/step", {"step": "won"}) == (503, {"error": "busy"})
    monkeypatch.undo()
    assert _state(rig, other)[1] == "new"


def test_step_route_never_reaches_resolve(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A step request never runs the resolve code, and a route name nobody handles is 404, never a resolve."""
    rig = env.rig(leadflow=_flow())
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(Desk, "resolve", lambda self, item, decision: calls.append((item, decision)) or Reply(200, {}))
    base = _serve(env, rig)
    lid = _new(rig, 1)
    assert call(base, "POST", f"/leads/{lid}/step", {"step": "won"})[0] == 200
    assert calls == []
    real_route = server._route
    monkeypatch.setattr(server, "_route", lambda m, p: ("mystery", "x") if p == "/mystery" else real_route(m, p))
    assert call(base, "POST", "/mystery", {"decision": "sent"}) == (404, {"error": "not_found"})
    assert calls == []
    assert call(base, "POST", "/review/Fx/resolve", {"decision": "sent"})[0] in (200, 404, 409)
    assert len(calls) == 1  # the real resolve route still reaches resolve


# -- mutants: each plants one fault and requires the real test to fail
def test_mutant_no_regress_trigger(env) -> None:
    """Without the no-regress trigger a direct lower step goes through: test_step_trigger_blocks_lower_rank fails."""
    weak = WeakSchemaEnv(env, drop_trigger("lead_state_no_regress"))  # a fresh book only: no rig.restart()
    with pytest.raises(FAILS + (pytest.fail.Exception,)):
        test_step_trigger_blocks_lower_rank(weak)


def test_mutant_owner_count_read_before_the_write_lock(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The count taken above the write lock lets two leads read the same number: owners are not in turn. The kill is
    probabilistic (two threads must read before either writes): up to 20 rounds, each on a fresh rig."""
    weaken(
        monkeypatch,
        Desk,
        "_decide_once",
        (r'\n\s*\(n,\) = conn\.execute\("SELECT count\(\*\) FROM lead_state"\)\.fetchone\(\)', ""),
        (
            r"(\n    conn = self\._conn\(\)\n)(    with self\._write\(conn\):)",
            r'\1    (n,) = conn.execute("SELECT count(*) FROM lead_state").fetchone()\n\2',
        ),
    )
    killed = 0
    for _ in range(20):
        try:
            _balanced_body(env)
        except FAILS as e:
            assert "differ by at most 1" in str(e), str(e)
            killed += 1
            break
    assert killed == 1, "the weakened count was never caught in 20 rounds"
