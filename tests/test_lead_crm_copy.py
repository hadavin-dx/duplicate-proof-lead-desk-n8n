"""Lead owner and lead stage, CRM copy part: Crm.set_fields on every CRM class, the stand-in server route,
the fake HubSpot PATCH, and the copy part of the reaper. Made-up data only (example.test, loopback servers)."""

from __future__ import annotations

import copy
import dataclasses
import re
import time
from pathlib import Path
from typing import Any

import pytest
from support import DEMO_LEADFLOW, CountingCrm, call, lead
from test_crm_hubspot import REC
from test_crm_hubspot import Rig as HsRig
from test_mutants import FAILS, weaken

from demo_only import fake_hubspot as fh
from demo_only.fakes import FakeCrm
from lead_desk import crm_hubspot
from lead_desk import desk as desk_module
from lead_desk.adapters import CrmError, CrmNothingStored, HttpCrm
from lead_desk.config import read_leadflow
from lead_desk.crm_hubspot import CrmBusy, CrmCapReached, CrmRefused, HubspotCrm
from lead_desk.desk import Desk
from lead_desk.trace import TracedCrm, Tracer


def _flow(**owner_ids: str) -> dict[str, Any]:
    """The demo leadflow, with the CRM owner id of the named rota ids filled in (by id, for example o_1="H1")."""
    flow = copy.deepcopy(DEMO_LEADFLOW)
    for r in flow["rota"]:
        r["crm_owner_id"] = owner_ids.get(r["id"].replace("-", "_"), r["crm_owner_id"])
    return flow


@pytest.fixture
def hs(tmp_path: Path):
    r = HsRig(tmp_path)
    yield r
    r.stop()


def _put(crm: Any, lead_id: str = "L1", email: str = "ada@example.test") -> None:
    crm.put(lead_id, {**REC, "email": email})


# -- the Crm contract: every class that exists
def test_set_fields_on_every_crm_class(env, hs: HsRig, tmp_path: Path) -> None:
    # FakeCrm: no stored put -> nothing stored; after a put the fields are kept, the last call wins
    fake = FakeCrm()
    with pytest.raises(CrmNothingStored):
        fake.set_fields("L1", "ada@example.test", "H1", "NEW")
    _put(fake)
    fake.set_fields("L1", "ada@example.test", "H1", "NEW")
    fake.set_fields("L1", "ada@example.test", "", "OPEN_DEAL")
    assert fake.fields == {"L1": {"owner_id": "", "status": "OPEN_DEAL"}}
    # CountingCrm: its own log, `calls` (puts) stays as it is, no stored put -> CrmNothingStored
    cc = CountingCrm()
    with pytest.raises(CrmNothingStored):
        cc.set_fields("L1", "ada@example.test", "H1", "NEW")
    _put(cc)
    cc.set_fields("L1", "ada@example.test", "H1", "NEW")
    assert len(cc.calls) == 1 and len(cc.set_fields_calls) == 2
    # TracedCrm: a timed pass-through (stage "crm"), errors pass through unchanged
    tracer = Tracer(tmp_path / "trace.jsonl")
    traced = TracedCrm(fake, tracer)
    traced.set_fields("L1", "ada@example.test", "H2", "NEW")
    assert fake.fields["L1"]["owner_id"] == "H2"
    with pytest.raises(CrmNothingStored):
        traced.set_fields("L-none", "x@example.test", "", "NEW")
    events = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 2 and all('"stage": "crm"' in e for e in events)
    assert '"outcome": "CrmNothingStored"' in events[1]
    # HttpCrm against the stand-in server: no stored put -> nothing stored (404), then stored
    srv, url = env.fakes()
    http = HttpCrm(url, 2.0)
    with pytest.raises(CrmNothingStored):
        http.set_fields("L1", "ada@example.test", "H1", "NEW")
    _put(http)
    http.set_fields("L1", "ada@example.test", "H1", "NEW")
    assert srv.state.crm_fields == {"L1": {"owner_id": "H1", "status": "NEW"}}
    # HubspotCrm against the fake HubSpot
    crm = hs.crm()
    _put(crm)
    crm.set_fields("L1", "Ada@Example.test", "77", "ATTEMPTED_TO_CONTACT")
    props = hs.state.contacts[next(iter(hs.state.contacts))]["properties"]
    assert props["hs_lead_status"] == "ATTEMPTED_TO_CONTACT" and props["hubspot_owner_id"] == "77"


def test_fake_crm_fields_route_mapping(env) -> None:
    srv, url = env.fakes()
    http = HttpCrm(url, 2.0)
    _put(http)

    def control(mode: str, delay_ms: int = 0) -> None:
        assert call(url, "POST", "/fake/control", {"fail_next": mode, "delay_ms": delay_ms, "target": "crm"})[0] == 200

    http.set_fields("L1", "ada@example.test", "H1", "NEW")  # 200 {"status": "set"}
    assert call(url, "GET", "/fake/crm")[1]["fields"]["L1"] == {"owner_id": "H1", "status": "NEW"}
    for body in ({}, {"lead_id": "L1"}, {"lead_id": "L1", "email_key": "a", "owner_id": 1, "status": "NEW"}):
        assert call(url, "POST", "/fake/crm/fields", body)[0] == 400  # not four strings
    with pytest.raises(CrmNothingStored):  # 404: no stored put for that lead
        http.set_fields("L-none", "x@example.test", "", "NEW")
    control("refuse")
    with pytest.raises(CrmNothingStored):  # 422
        http.set_fields("L1", "ada@example.test", "H9", "WON")
    assert srv.state.crm_fields["L1"]["owner_id"] == "H1"
    control("drop")
    with pytest.raises(CrmError) as e:
        http.set_fields("L1", "ada@example.test", "H9", "WON")
    assert not e.value.nothing_stored and srv.state.crm_fields["L1"]["owner_id"] == "H1"  # nothing stored
    control("record_then_drop")
    with pytest.raises(CrmError):
        http.set_fields("L1", "ada@example.test", "H9", "WON")
    assert srv.state.crm_fields["L1"] == {"owner_id": "H9", "status": "WON"}  # stored, the adapter saw no answer
    control("store_then_delay", 300)
    t0 = time.monotonic()
    http.set_fields("L1", "ada@example.test", "H5", "NEW")
    assert time.monotonic() - t0 >= 0.25 and srv.state.crm_fields["L1"]["owner_id"] == "H5"
    http.set_fields("L1", "ada@example.test", "", "NEW")  # nothing is left over from the one-shot controls
    srv.state.reset()
    assert srv.state.crm_fields == {}


def test_hubspot_patch_mapping(hs: HsRig, monkeypatch: pytest.MonkeyPatch) -> None:
    crm = hs.crm()
    _put(crm)
    crm.set_fields("L1", "ada@example.test", "", "NEW")
    sent = hs.state.contacts[next(iter(hs.state.contacts))]["properties"]
    assert sent["hs_lead_status"] == "NEW" and "hubspot_owner_id" not in sent  # blank owner: not sent
    assert ("PATCH", fh._BASE + "/ada%40example.test?idProperty=email") in hs.state.calls
    with pytest.raises(CrmRefused):  # 404: the contact is absent, nothing changed
        crm.set_fields("L2", "nobody@example.test", "1", "NEW")
    h = hs.state
    h.mode = "patch_400"
    with pytest.raises(CrmRefused):
        crm.set_fields("L1", "ada@example.test", "1", "NEW")
    assert h.failed == 0
    h.mode, h.times = "patch_r429", 1
    crm.set_fields("L1", "ada@example.test", "1", "NEW")  # 429 once, then ok
    assert h.patch_failed == 1
    h.mode, h.patch_failed, h.times, h.retry_after = "patch_r429", 0, 99, "0"
    with pytest.raises(CrmBusy):  # 429 until the bounded waits are used up
        crm.set_fields("L1", "ada@example.test", "1", "NEW")
    h.mode, h.patch_failed, h.times = "patch_r5xx", 0, 1
    with pytest.raises(CrmError) as e:
        crm.set_fields("L1", "ada@example.test", "1", "NEW")
    assert not e.value.nothing_stored
    assert h.failed == 0  # PATCH faults never touch the create counter
    # a 200 whose body is not JSON is still success; the body is never read
    h.mode = "ok"
    monkeypatch.setattr(HubspotCrm, "_read", staticmethod(lambda r, deadline: crm_hubspot._BAD_JSON))
    crm.set_fields("L1", "ada@example.test", "2", "OPEN_DEAL")
    monkeypatch.undo()
    # the body holds only the two properties and nothing else
    seen: list[Any] = []
    orig = HubspotCrm._once
    monkeypatch.setattr(HubspotCrm, "_once", lambda s, m, p, o, d, k: (seen.append((m, o)), orig(s, m, p, o, d, k))[1])
    crm.set_fields("L1", "ada@example.test", "3", "WON")
    assert seen == [("PATCH", {"properties": {"hs_lead_status": "WON", "hubspot_owner_id": "3"}})]
    # a direct bad request to the fake: a wrong property name and a missing idProperty are refused (400)
    assert crm.stats()["patch"] >= 5


@pytest.mark.parametrize("max_calls", [1, 2, 3, 150])
def test_mirror_subcap_never_starves_put(hs: HsRig, max_calls: int) -> None:
    crm = hs.crm(max_calls=max_calls)
    share = max_calls // 2
    _put(crm)  # one create (one call)
    done = refused = 0
    for _ in range(max_calls + 2):
        try:
            crm.set_fields("L1", "ada@example.test", "", "NEW")
            done += 1
        except CrmCapReached:
            refused += 1
            break
    st = crm.stats()
    assert done == min(share, max_calls - 1) and refused == 1
    assert st["patch"] == done and st["calls"] == 1 + done  # a refused PATCH is never counted
    # the creates keep every remaining call: at least ceil(max_calls / 2) in all
    made = 1
    for i in range(max_calls):
        try:
            _put(crm, f"L{i + 2}", f"p{i}@example.test")
            made += 1
        except CrmCapReached:
            break
    assert made >= (max_calls + 1) // 2
    assert crm.stats()["calls"] == min(max_calls, 1 + done + (made - 1))
    if max_calls == 1:
        assert done == 0


# -- the copy part of the reaper
def _done(rig: Any, lead_id: str) -> tuple[Any, ...]:
    return rig.query(
        "SELECT crm_owner_done, crm_step_done, crm_tries, crm_error, step, step_rank FROM lead_state WHERE lead_id=?",
        (lead_id,),
    )[0]


def _pass(rig: Any) -> dict[str, int]:
    done = {"mirrored": 0}
    rig.desk._mirror_not_before._d.clear()  # these tests count tries, not waits: the waiting has its own tests
    rig.desk._mirror_pass(done)
    return done


def test_mirror_waits_for_crm_done(env) -> None:
    crm = CountingCrm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    reply = rig.desk.process("form", "w-1", lead())  # decided, but the follow-up (and the CRM put) has not run
    lead_id = reply.body["lead_id"]
    assert rig.query("SELECT crm_done FROM followups WHERE lead_id=?", (lead_id,)) == [(0,)]
    assert _pass(rig)["mirrored"] == 0 and crm.set_fields_calls == []
    rig.desk.run_followup(lead_id)
    assert rig.query("SELECT crm_done FROM followups WHERE lead_id=?", (lead_id,)) == [(1,)]
    assert _pass(rig)["mirrored"] == 1
    assert crm.set_fields_calls == [
        (lead_id, "ada@example.test", "", "ATTEMPTED_TO_CONTACT")
    ]  # the follow-up moved the lead to Contacted
    assert _done(rig, lead_id)[:4] == (0, 1, 0, None)  # step copied (rank 1); the blank owner id: not copied
    assert _pass(rig)["mirrored"] == 0 and len(crm.set_fields_calls) == 1  # nothing left to copy
    off = env.rig(crm=CountingCrm())  # feature off: the part does nothing
    off.desk.handle("form", "o-1", lead())
    assert _pass(off)["mirrored"] == 0 and off.crm.set_fields_calls == []


class _FlakyCrm(CountingCrm):
    """set_fields raises while `fail` holds an exception; an optional hook runs inside the call."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: Exception | None = None
        self.inside: Any = None

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        if self.inside:
            self.inside()
        if self.fail is not None:
            self.set_fields_calls.append((lead_id, email_key, crm_owner_id, crm_status))
            raise self.fail
        super().set_fields(lead_id, email_key, crm_owner_id, crm_status)


def test_mirror_idempotent_retry_and_failure_keeps_decision(env) -> None:
    crm = _FlakyCrm()
    rig = env.rig(leadflow=_flow(o_1="H1"), crm=crm)
    reply = rig.desk.handle("form", "m-1", lead())
    assert reply.status == 201  # the decision never waits for the CRM copy
    lead_id = reply.body["lead_id"]
    crm.fail = CrmError("http 503")
    assert _pass(rig)["mirrored"] == 0
    assert _done(rig, lead_id)[2:4] == (1, "CrmError")  # one try used, the class name kept
    assert _pass(rig)["mirrored"] == 0 and _done(rig, lead_id)[2] == 2
    assert rig.leads() == 1 and rig.state()[0] == "sent"  # decision and follow-up untouched
    crm.fail = None
    assert _pass(rig)["mirrored"] == 1  # the same call again is harmless
    assert _done(rig, lead_id) == (1, 1, 0, None, "contacted", 1)  # Contacted after the follow-up
    assert [c[2:] for c in crm.set_fields_calls] == [("H1", "ATTEMPTED_TO_CONTACT")] * 3
    # give up after MIRROR_MAX_TRIES failures in a row, then the row is left
    import sqlite3

    con = sqlite3.connect(rig.db_path)
    con.execute("UPDATE lead_state SET step='replied', step_rank=2")  # a later step is waiting to be copied
    con.commit()
    con.close()
    crm.fail = CrmError("http 503")
    for _ in range(8):
        _pass(rig)
    assert _done(rig, lead_id)[2] == 5 and len(crm.set_fields_calls) == 3 + 5


def test_mirror_posted_step_during_call_is_copied_next_pass(env) -> None:
    crm = _FlakyCrm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    lead_id = rig.desk.handle("form", "p-1", lead()).body["lead_id"]
    import sqlite3

    def post_step() -> None:
        con = sqlite3.connect(rig.db_path)
        con.execute("UPDATE lead_state SET step='qualified', step_rank=3 WHERE step_rank < 3")
        con.commit()
        con.close()

    crm.inside = post_step
    assert _pass(rig)["mirrored"] == 1
    crm.inside = None
    assert crm.set_fields_calls[0][3] == "ATTEMPTED_TO_CONTACT"  # the step read in the SELECT (Contacted) was sent
    assert _done(rig, lead_id)[1] == 1 and _done(rig, lead_id)[5] == 3  # recorded rank 1, the row is at rank 3
    assert _pass(rig)["mirrored"] == 1 and crm.set_fields_calls[1][3] == "OPEN_DEAL"
    assert _done(rig, lead_id)[1] == 3


def test_mirror_owner_missing_or_blank_copies_step_only(env) -> None:
    crm = CountingCrm()
    rig = env.rig(leadflow=_flow(o_2="H2"), crm=crm)  # o-1 has no CRM owner id, o-2 has
    a = rig.desk.handle("form", "a-1", lead(email="a@example.test")).body["lead_id"]  # o-1
    b = rig.desk.handle("form", "b-1", lead(email="b@example.test")).body["lead_id"]  # o-2
    assert [rig.query("SELECT owner_id FROM lead_state WHERE lead_id=?", (x,))[0][0] for x in (a, b)] == ["o-1", "o-2"]
    # the rota is edited before the first copy: o-2 left the list
    flow = _flow()
    flow["rota"] = [flow["rota"][0]]
    path = rig.db_path.parent / "leadflow-2.json"
    path.write_text(__import__("json").dumps(flow), encoding="utf-8")
    rig.config = dataclasses.replace(rig.config, leadflow=read_leadflow(path), leadflow_path=path)
    rig.restart()
    assert _pass(rig)["mirrored"] == 2
    assert sorted(c[2:] for c in crm.set_fields_calls) == [
        ("", "ATTEMPTED_TO_CONTACT"),
        ("", "ATTEMPTED_TO_CONTACT"),
    ]  # no owner sent for either
    assert _done(rig, a)[0] == 0 and _done(rig, b)[0] == 0  # owners not copied
    for _ in range(10):
        assert _pass(rig)["mirrored"] == 0
    assert len(crm.set_fields_calls) == 2  # no call per pass for an owner that cannot be copied
    # a rota that gives both ids a CRM owner id, after a restart: each owner is copied once
    flow = _flow(o_1="H1", o_2="H2")
    path.write_text(__import__("json").dumps(flow), encoding="utf-8")
    rig.config = dataclasses.replace(rig.config, leadflow=read_leadflow(path), leadflow_path=path)
    rig.restart()
    assert _pass(rig)["mirrored"] == 2
    assert sorted(c[2:] for c in crm.set_fields_calls[2:]) == [
        ("H1", "ATTEMPTED_TO_CONTACT"),
        ("H2", "ATTEMPTED_TO_CONTACT"),
    ]
    assert _done(rig, a)[0] == 1 and _done(rig, b)[0] == 1
    assert _pass(rig)["mirrored"] == 0 and len(crm.set_fields_calls) == 4


def test_mirror_call_keeps_the_values_of_its_own_row(env, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CRM call is handed to a worker thread that may run late (an abandoned worker). Here every call is held and
    # only run after the pass has moved on to the next row: each must still send the values of its own row.
    crm = CountingCrm()
    rig = env.rig(leadflow=_flow(o_1="H1", o_2="H2"), crm=crm)
    a = rig.desk.handle("form", "k-1", lead(email="a@example.test")).body["lead_id"]  # o-1
    b = rig.desk.handle("form", "k-2", lead(email="b@example.test")).body["lead_id"]  # o-2
    held: list[Any] = []

    def hold(kind: str, fn: Any, budget: float, on_timeout: Any, **kw: Any) -> None:
        held.append(fn)  # not run now: the loop goes on to the next row first

    monkeypatch.setattr(rig.desk, "_bounded", hold)
    _pass(rig)
    assert len(held) == 2 and crm.set_fields_calls == []
    for fn in held:
        fn()
    assert sorted(crm.set_fields_calls) == sorted(
        [(a, "a@example.test", "H1", "ATTEMPTED_TO_CONTACT"), (b, "b@example.test", "H2", "ATTEMPTED_TO_CONTACT")]
    )


def test_reaper_stage_budgets(env, monkeypatch: pytest.MonkeyPatch) -> None:
    crm = _FlakyCrm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    for i in range(7):
        rig.desk.handle("form", f"s-{i}", lead(email=f"s{i}@example.test"))
    assert _pass(rig)["mirrored"] == desk_module.MIRROR_PER_PASS == 5  # at most five rows a pass
    assert _pass(rig)["mirrored"] == 2
    # a slow CRM stops the part at its own clock; the pass does not wait for every row
    monkeypatch.setattr(desk_module, "MIRROR_PASS_SECONDS", 0.25)
    import sqlite3

    con = sqlite3.connect(rig.db_path)
    con.execute("UPDATE lead_state SET step='replied', step_rank=2")
    con.commit()
    con.close()
    crm.inside = lambda: time.sleep(0.15)
    t0 = time.monotonic()
    n = _pass(rig)["mirrored"]
    assert 1 <= n <= 3 and time.monotonic() - t0 < 1.5, n
    # the follow-up budget is its own constant and is not the copy budget
    assert desk_module.REAPER_PASS_SECONDS == 10 and desk_module.MIRROR_PASS_SECONDS == 0.25


def test_reaper_copies_inside_reap_once(env) -> None:
    """The part is wired into the pass: reap_once copies and counts it."""
    crm = CountingCrm()
    rig = env.rig(leadflow=_flow(), crm=crm)
    rig.desk.handle("form", "r-1", lead())
    assert rig.desk.reap_once()["mirrored"] == 1 and len(crm.set_fields_calls) == 1


# -- the stand-in HTTP copy through the real desk
def test_copy_through_the_stand_in_server(env) -> None:
    srv, url = env.fakes()
    rig = env.rig(leadflow=_flow(), crm=HttpCrm(url, 2.0))
    lead_id = rig.desk.handle("form", "h-1", lead()).body["lead_id"]
    assert _pass(rig)["mirrored"] == 1
    assert srv.state.crm_fields[lead_id] == {"owner_id": "", "status": "ATTEMPTED_TO_CONTACT"}


# -- mutants: each plants one fault and requires a real test above to fail
def test_mutant_set_fields_in_the_decision(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """rev5 mutant 5: a CRM call inside the decision transaction (new-lead branch) blocks the decision when the CRM
    fails; the copy must stay in the reaper."""
    weaken(
        monkeypatch,
        Desk,
        "_decide_once",
        (
            r"\(lead_id, now, receipt_id\),\n(\s*)\)",
            r'(lead_id, now, receipt_id),\n\1)\n\1self.crm.set_fields(lead_id, chk.email_key, "", "NEW")',
        ),
    )
    with pytest.raises(FAILS + (CrmNothingStored,)):
        test_mirror_idempotent_retry_and_failure_keeps_decision(env)


def test_mutant_copy_without_waiting_for_the_crm_row(env, monkeypatch: pytest.MonkeyPatch) -> None:
    weaken(monkeypatch, Desk, "_mirror_pass", (re.escape("WHERE f.crm_done = 1 AND"), "WHERE"))
    with pytest.raises(FAILS):
        test_mirror_waits_for_crm_done(env)


def test_mutant_copy_share_not_checked(hs: HsRig, monkeypatch: pytest.MonkeyPatch) -> None:
    weaken(
        monkeypatch,
        HubspotCrm,
        "_call",
        (
            re.escape('if kind == "patch" and self._n["patch"] >= int(self.s.max_calls * MIRROR_CALL_SHARE):'),
            "if False:",
        ),
    )
    with pytest.raises(FAILS):
        test_mirror_subcap_never_starves_put(hs, 3)


def test_mutant_owner_not_in_copyable_is_selected_again(env, monkeypatch: pytest.MonkeyPatch) -> None:
    weaken(
        monkeypatch,
        Desk,
        "_mirror_pass",
        (
            re.escape("AND s.owner_id IN (SELECT value FROM json_each(?))"),
            "AND s.owner_id IN (SELECT value FROM json_each(?)) OR 1",
        ),
    )
    with pytest.raises(FAILS):
        test_mirror_owner_missing_or_blank_copies_step_only(env)
