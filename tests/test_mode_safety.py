"""What the portal says about the CRM, the banner, the demo controls, the public
bind, the follow-up text and the reset script, and that a real service is never reachable from another computer.
Made-up data only; the HubSpot side is the loopback stand-in (demo_only) or no network at all."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from support import DEMO_LEADFLOW, REPO, KeyedOutbox, build_rig, call, lead
from test_portal_api import fresh, get

from demo_only import fake_hubspot as fh
from lead_desk import server
from lead_desk.config import Config, LiveSettings, SettingRefused
from lead_desk.constants import EXIT_DESK_SETTING_REFUSED, FOLLOWUP_BODY_TEMPLATE
from lead_desk.crm_hubspot import HubspotCrm, HubspotSettings
from lead_desk.smtp_outbox import SmtpSettings

LEADS_JS = REPO / "src" / "lead_desk" / "portal" / "pages" / "leads.js"
PORTAL_JS = REPO / "src" / "lead_desk" / "portal" / "portal.js"
PROBE = REPO / "tests" / "portal_probe.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _flow() -> dict[str, Any]:
    flow = copy.deepcopy(DEMO_LEADFLOW)
    flow["nudge_after_seconds"] = None
    return flow


def _crm_rows(rig: Any, base: str) -> dict[str, list[str]]:
    status, out = get(base, "/api/ui/data-map")
    assert status == 200, out
    return {r["field"]: [t for t in r["sent_to"] if t.startswith("CRM")] for r in out["stored"]}


# -- the data page follows the loaded CRM kind
def test_data_map_stand_in_crm_is_called_a_stand_in(env: Any) -> None:
    rig, base = fresh(env, leadflow=_flow())
    rows = _crm_rows(rig, base)
    assert rows["lead_stage"] == ["CRM (stand-in)"] and rows["name"] == ["CRM (stand-in)"]
    assert "status only in the demo" not in json.dumps(get(base, "/api/ui/data-map")[1])


def test_data_map_hubspot_lists_exactly_what_the_adapter_sends(env: Any) -> None:
    rig, base = fresh(env, crm_kind="hubspot", leadflow=_flow())
    got = {f for f, to in _crm_rows(rig, base).items() if to}
    # crm_hubspot.put sends email, lead id, first and last name, company; set_fields sends the lead status
    # (and the owner id when the owner has one): nothing else, so no door and no AI label
    assert got == {"lead_id", "name", "email", "company", "lead_stage", "lead_owner"}
    labels = {t for to in _crm_rows(rig, base).values() for t in to}
    assert labels == {"CRM"}  # not called a stand-in, not "status only in the demo"


def test_data_map_hubspot_without_a_lead_flow_sends_no_status_and_no_owner(env: Any) -> None:
    rig, base = fresh(env, crm_kind="hubspot")
    got = {f for f, to in _crm_rows(rig, base).items() if to}
    assert got == {"lead_id", "name", "email", "company"}


def test_data_map_hubspot_matches_its_fixture(env: Any) -> None:
    """One data page fixture per CRM kind (data-map.json is the stand-in kind)."""
    _, base = fresh(env, crm_kind="hubspot", leadflow=_flow())
    want = json.loads((REPO / "tests" / "portal_fixtures" / "data-map-hubspot.json").read_text(encoding="utf-8"))
    assert get(base, "/api/ui/data-map")[1] == want


def test_data_map_matches_what_hubspot_put_really_sends(tmp_path: Path) -> None:
    """The map's CRM fields are tied to the adapter's properties, not to a second list that can drift."""
    key = tmp_path / "key.txt"
    key.write_text("k" + "e" * 15 + "-for-tests-0001\n", encoding="utf-8")
    state = fh.HubspotState("k" + "e" * 15 + "-for-tests-0001", "ok")
    srv, th, url = fh.serve(state, "127.0.0.1")
    try:
        crm = HubspotCrm(
            HubspotSettings(key_file=key, max_calls=10, base_url=url, timeout_seconds=2.0, allow_any_base_url=True)
        )
        record = {"lead_id": "L-1", "name": "Ada Example", "email": "ada@example.test", "company": "Example Ltd"}
        crm.put("L-1", {**record, "source": "form", "ai_label": "hot"})
        (row,) = state.contacts.values()
    finally:
        fh.stop(srv, th, state)
    sent = set(row["properties"])
    assert sent == {"email", "hadavin_lead_id", "firstname", "lastname", "company"}
    from lead_desk import portal_api

    # one adapter property per lead field the map names (the lead number is hadavin_lead_id, name is two properties)
    assert portal_api.CRM_FIELDS_SENT["hubspot"] == {"lead_id", "name", "email", "company"}


def test_lead_detail_warns_about_the_crm_stage_write_only_for_a_real_crm(env: Any) -> None:
    for kind, want in (("fake", False), ("hubspot", True)):
        rig, base = fresh(env, crm_kind=kind, leadflow=_flow())
        lid = rig.desk.process("form", "w-" + kind, lead(email=f"w{kind}@example.test")).body["lead_id"]
        status, out = get(base, f"/api/ui/leads/{lid}")
        assert status == 200 and out["stage_writes_real_crm"] is want, (kind, out.get("stage_writes_real_crm"))


def _browse(base: str, fn: Any, width: int = 1280, scheme: str = "light") -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 (no browser installed here)
            pytest.skip(f"no browser: {e}")
        try:
            page = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme).new_page()
            fn(page, base)
        finally:
            browser.close()


@pytest.mark.parametrize(("kind", "shown"), [("fake", False), ("hubspot", True)])
def test_the_warning_is_beside_the_move_control_only_for_a_real_crm(env: Any, kind: str, shown: bool) -> None:
    rig, base = fresh(env, crm_kind=kind, leadflow=_flow())
    lid = str(rig.desk.process("form", "b-" + kind, lead(email=f"b{kind}@example.test")).body["lead_id"])

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector(f'[data-lead-id="{lid}"]')
        page.click(f'[data-lead-id="{lid}"]')
        page.wait_for_selector('[data-testid="step-select"]')
        warn = page.locator('[data-testid="step-control"] [data-testid="step-crm-warning"]')
        assert warn.count() == (1 if shown else 0)
        if shown:
            assert warn.is_visible() and "automation" in warn.inner_text().lower()

    _browse(base, body)


# -- the banner and sample_data follow the loaded kinds
@pytest.mark.parametrize(
    ("over", "real"),
    [
        ({}, []),
        ({"crm_kind": "hubspot"}, ["crm"]),
        ({"outbox_kind": "smtp_provider"}, ["mail"]),
        ({"outbox_kind": "smtp"}, []),  # the SMTP test-server kind talks to a test server
        ({"crm_kind": "hubspot", "outbox_kind": "smtp_provider"}, ["crm", "mail"]),
    ],
)
def test_summary_mode_says_which_kinds_are_real(env: Any, over: dict[str, str], real: list[str]) -> None:
    _, base = fresh(env, **over)
    mode = get(base, "/api/ui/summary")[1]["mode"]
    assert mode["real_services"] == real
    assert mode["sample_data"] is (not real)


def test_a_live_ai_step_counts_as_a_real_service(tmp_path: Path) -> None:
    assert Config(qualifier="live_claude", live=_live_settings(tmp_path)).real_services == ("ai",)
    assert Config().real_services == ()


def _banner(mode: dict[str, Any]) -> dict[str, Any]:
    out = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
        ["node", str(PROBE), str(PORTAL_JS), json.dumps(mode)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


@needs_node
def test_the_banner_says_real_services_are_connected_and_not_demo_data() -> None:
    seen = _banner({"demo_tools": False, "sample_data": False, "real_services": ["crm", "mail"]})
    assert seen["hidden"] is False
    assert "Real services are connected" in seen["strip"] and "CRM" in seen["strip"] and "mail" in seen["strip"]
    assert "Every lead is demo data" not in seen["strip"] and "Nothing here is sent" not in seen["strip"]


# -- no demo controls with a real kind
@pytest.mark.parametrize("over", [{"crm_kind": "hubspot"}, {"outbox_kind": "smtp_provider"}])
def test_demo_controls_are_off_with_a_real_kind(env: Any, over: dict[str, str]) -> None:
    rig, base = fresh(env, **over)
    assert get(base, "/api/ui/summary")[1]["mode"]["demo_tools"] is False
    assert call(base, "POST", "/api/demo/burst", {"n": 3})[0] == 404
    assert call(base, "POST", "/api/demo/crash", {})[0] == 404
    assert get(base, "/api/demo/state")[0] == 404
    assert get(base, "/ui/demo/portal_demo.js")[0] == 404  # the extension's page files are not served either
    assert rig.leads() == 0  # nothing was written
    assert "demo_controls" not in get(base, "/api/ui/data-map")[1]["access"]["portal_actions"]


def test_demo_controls_stay_with_the_stand_ins(env: Any) -> None:
    _, base = fresh(env)
    assert get(base, "/api/ui/summary")[1]["mode"]["demo_tools"] is True
    assert get(base, "/api/demo/state")[0] == 200


# -- a real service and a public bind do not start together (the desk has no sign-in)
def _main_env(monkeypatch: Any, tmp_path: Path, bind: str, **settings: str) -> None:
    for name in ("DESK_CRM", "DESK_OUTBOX", "DESK_QUALIFIER"):
        monkeypatch.delenv(name, raising=False)  # one case must not leak its kinds into the next
    for k, v in {"DESK_DB_PATH": str(tmp_path / "m3.sqlite3"), "DESK_PORT": "0", "DESK_BIND": bind, **settings}.items():
        monkeypatch.setenv(k, v)


class _Reached(Exception):
    pass


def _stop_at_adapters(monkeypatch: Any) -> None:
    def adapters(cfg: Any) -> Any:
        raise _Reached

    monkeypatch.setattr(server, "load_demo_adapters", adapters)


@pytest.mark.parametrize(
    ("setting", "kind"), [({"DESK_CRM": "hubspot"}, "crm"), ({"DESK_OUTBOX": "smtp_provider"}, "mail")]
)
def test_main_refuses_a_public_bind_with_any_real_service(
    monkeypatch: Any, tmp_path: Path, capsys: Any, setting: dict[str, str], kind: str
) -> None:
    """No lead flow file is set: a stranger who can reach the port could still create contacts or send mail."""
    _main_env(monkeypatch, tmp_path, "::", **setting)  # the wildcard address: reachable from other computers
    _stop_at_adapters(monkeypatch)
    with pytest.raises(SystemExit) as info:
        server.main()
    assert info.value.code == EXIT_DESK_SETTING_REFUSED
    out = capsys.readouterr().out
    assert "DESK_BIND" in out and kind in out


def test_a_live_ai_step_on_a_public_bind_is_refused(tmp_path: Path) -> None:
    live = _live_settings(tmp_path)
    assert Config(bind="::", qualifier="live_claude", live=live).public_bind_problem() is not None
    assert Config(bind="127.0.0.1", qualifier="live_claude", live=live).public_bind_problem() is None


def test_main_still_starts_on_loopback_and_with_the_stand_ins(monkeypatch: Any, tmp_path: Path) -> None:
    _stop_at_adapters(monkeypatch)
    for bind, setting in (("127.0.0.1", {"DESK_CRM": "hubspot"}), ("::", {}), ("localhost", {"DESK_CRM": "hubspot"})):
        _main_env(monkeypatch, tmp_path, bind, **setting)
        with pytest.raises(_Reached):  # got past the refusal to the adapters
            server.main()


# -- the stand-in kinds are local by construction, so the DEMO banner cannot be false
@pytest.mark.parametrize("host", ["192.0.2.1", "::2", "::", "mail.example.test"])
def test_the_smtp_test_server_must_be_on_this_computer(host: str) -> None:
    with pytest.raises(SettingRefused, match="DESK_SMTP_HOST"):
        SmtpSettings(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_the_smtp_test_server_on_loopback_is_accepted(host: str) -> None:
    assert SmtpSettings(host=host).host == host


@pytest.mark.parametrize("host", ["192.0.2.1", "remote.example.test", "[::2]"])
def test_the_demo_servers_must_be_on_this_computer(host: str) -> None:
    with pytest.raises(SettingRefused, match="DESK_FAKES_HOST"):
        Config(fakes_host=host)
    with pytest.raises(SettingRefused, match="DESK_FAKES_HOST"):
        Config.from_env({"DESK_FAKES_URL": f"http://{host}:8766"})


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_the_demo_servers_on_this_computer_are_accepted(host: str) -> None:
    assert Config(fakes_host=host).fakes_host == host


def _live_settings(tmp_path: Path) -> LiveSettings:
    return LiveSettings(
        model="m",
        key_file=tmp_path / "key.txt",
        price_in_per_mtok=Decimal("2"),
        price_out_per_mtok=Decimal("10"),
        price_date="2026-01-01",
        cap_run_usd=Decimal("1"),
        cap_total_usd=Decimal("3"),
        ledger_path=tmp_path / "ledger.jsonl",
        run_id="run-a",
        base_url="http://127.0.0.1:9",
    )


# -- the demo marker in the follow-up text depends on the sender kind
class _BodyOutbox(KeyedOutbox):
    """The stand-in sender, keeping the body it was given."""

    bodies: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.bodies = []

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Any:
        self.bodies.append(body)
        return super().send(send_key, to, subject, body, timeout)


def _first_body(env: Any, name: str, kind: str | None) -> str:
    out = _BodyOutbox()
    (env.tmp / name).mkdir()
    rig = build_rig(env.tmp / name, outbox=out, outbox_kind=kind)
    rig.desk.handle("form", name, lead(email=f"{name}@example.test"))
    assert len(out.bodies) == 1
    return out.bodies[0]


def test_follow_up_text_keeps_the_demo_marker_for_the_stand_in_sender(env: Any) -> None:
    assert "(DEMO EMAIL)" in _first_body(env, "n4a1", None)


def test_follow_up_text_has_no_stand_in_marker_for_the_mail_provider_kind(env: Any) -> None:
    body = _first_body(env, "n4a2", "smtp_provider")
    assert "(DEMO EMAIL)" not in body and body.startswith("Hi ")  # the provider adds its own demo notice and tag
    assert FOLLOWUP_BODY_TEMPLATE.endswith("(DEMO EMAIL)")  # the stand-in text is unchanged


def test_the_owner_note_names_the_real_reason_not_the_demo_crm() -> None:
    js = LEADS_JS.read_text(encoding="utf-8")
    assert "the demo CRM has no owner field" not in js
    assert "lead flow file" in js[js.index("CRM_COPY_NOTE") : js.index("CRM_COPY_NOTE") + 200]


# -- the reset script never deletes the book when it cannot tell which program a process id belongs to
@pytest.mark.skipif(sys.platform == "win32", reason="reset.sh is the non-Windows script")
def test_reset_sh_refuses_when_ps_is_missing(tmp_path: Path) -> None:
    folder = REPO / "data" / "reset-no-ps"
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "desk.sqlite3"
    db.write_text("keep me", encoding="utf-8")
    (folder / "desk.pid").write_text("1", encoding="utf-8")
    (folder / "desk.log").write_text("keep me", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "dirname", "cat", "rm", "sleep", "kill"):  # everything the script uses, except ps
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    (bin_dir / "python").symlink_to(sys.executable)
    env = {**os.environ, "DESK_DB_PATH": str(db), "PATH": str(bin_dir)}
    try:
        done = subprocess.run(  # noqa: S603 (our own script, a path made by the test)
            [str(bin_dir / "bash"), str(REPO / "scripts" / "reset.sh")],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode != 0, (done.stdout, done.stderr)
        assert "ps" in done.stdout + done.stderr
        assert db.read_text(encoding="utf-8") == "keep me" and (folder / "desk.log").exists()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# -- the data page and the notices say the same thing in every mode
def test_the_data_page_does_not_call_the_desk_a_demo_in_every_mode() -> None:
    data_js = (REPO / "src" / "lead_desk" / "portal" / "pages" / "data.js").read_text(encoding="utf-8")
    api = (REPO / "src" / "lead_desk" / "portal_api.py").read_text(encoding="utf-8")
    assert "Settings of this demo" not in data_js and "describes the demo" not in data_js
    assert "this demo calls nobody" not in api


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_real_services_banner_is_warm_and_the_demo_banner_is_not(env: Any, scheme: str) -> None:
    _, real_base = fresh(env, crm_kind="hubspot")
    _, demo_base = fresh(env)
    js = (
        "() => { const t = document.createElement('span'); t.style.color = 'var(--warn-fg)';"
        " document.body.appendChild(t);"
        " const warn = getComputedStyle(t).color; t.remove();"
        " const tag = document.querySelector('#demo-line .tag');"
        " return { cls: document.querySelector('#demo-line').className, tag: getComputedStyle(tag).color, warn,"
        " text: document.querySelector('#demo-line').innerText }; }"
    )
    seen: dict[str, Any] = {}

    def look(name: str) -> Any:
        def body(page: Any, base: str) -> None:
            page.goto(base + "/ui/")
            page.wait_for_selector("#demo-line .tag", state="attached")  # the demo tools' own notice may be hidden
            seen[name] = page.evaluate(js)

        return body

    _browse(real_base, look("real"), scheme=scheme)
    _browse(demo_base, look("demo"), scheme=scheme)
    assert "is-real" in seen["real"]["cls"] and seen["real"]["tag"] == seen["real"]["warn"], seen["real"]
    assert "Real services are connected" in seen["real"]["text"]
    assert "Lead data this desk handles may be sent to them." in seen["real"]["text"]
    assert "is-real" not in seen["demo"]["cls"] and seen["demo"]["tag"] != seen["demo"]["warn"], seen["demo"]


# -- with a real CRM, a stage move asks a second time and says what it does
def _lead_and_open(env: Any, kind: str) -> tuple[Any, str, str]:
    rig, base = fresh(env, crm_kind=kind, leadflow=_flow())
    lid = str(rig.desk.process("form", "c-" + kind, lead(email=f"c{kind}@example.test")).body["lead_id"])
    return rig, base, lid


def _open_panel(page: Any, base: str, lid: str) -> None:
    page.goto(base + "/ui/#/leads")
    page.wait_for_selector(f'[data-lead-id="{lid}"]')
    page.click(f'[data-lead-id="{lid}"]')
    page.wait_for_selector('[data-testid="step-select"]')


_STEP_IS_REPLIED = "() => document.querySelector('[data-testid=\"step-now\"]')?.textContent === 'Replied'"


def test_a_real_crm_stage_move_needs_a_second_click(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        page.select_option('[data-testid="step-select"]', "replied")
        page.click('[data-testid="step-move"]')
        assert page.inner_text('[data-testid="step-move"]') == "Yes, write to the CRM"
        page.wait_for_timeout(400)
        seen = page.evaluate(
            "() => { const r = document.querySelector('[data-testid=\"step-crm-warning\"]').getBoundingClientRect();"
            " return [r.top, r.bottom, window.innerHeight]; }"
        )
        assert 0 <= seen[0] and seen[1] <= seen[2], seen  # the warning is on screen when the second question is asked
        page.wait_for_timeout(300)
        assert rig.query("SELECT step FROM lead_state WHERE lead_id=?", (lid,))[0][0] == "new"  # nothing written yet
        page.click('[data-testid="step-move"]')
        page.wait_for_function(_STEP_IS_REPLIED, timeout=15000)
        assert "copy to the CRM is pending" in page.inner_text("body")

    _browse(base, body)


def test_a_stand_in_crm_stage_move_is_one_click(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "fake")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        page.select_option('[data-testid="step-select"]', "replied")
        assert page.inner_text('[data-testid="step-move"]') == "Move"
        page.click('[data-testid="step-move"]')
        page.wait_for_function(_STEP_IS_REPLIED, timeout=15000)
        assert "copy to the CRM is pending" not in page.inner_text("body")

    _browse(base, body)


def test_the_rendered_stage_warning_names_the_automatic_writes_and_says_may(env: Any) -> None:
    """Behaviour, not a window of source text: the warning as the person reads it, inside the stage control."""
    _, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        warn = page.locator('[data-testid="step-control"] [data-testid="step-crm-warning"]')
        text = warn.inner_text().lower()
        assert "automation" in text and "automatic first step" in text and "after the follow-up" in text, text
        assert "may run" in text and "will run" not in text, text
        assert "note-warn" in warn.get_attribute("class") and warn.locator("svg").count() == 1

    _browse(base, body)


def test_the_stage_row_says_nothing_about_a_copy_when_no_lead_flow_is_set(env: Any) -> None:
    def purpose(**over: Any) -> str:
        _, base = fresh(env, **over)
        out = get(base, "/api/ui/data-map")[1]
        return str(next(r["purpose"] for r in out["stored"] if r["field"] == "lead_stage"))

    assert "copied" not in purpose(crm_kind="hubspot")
    assert "may run" in purpose(crm_kind="hubspot", leadflow=_flow())
    assert "stand-in" in purpose(leadflow=_flow())


# -- the copy of the stage to the CRM waits and tries again instead of giving up in seconds
# (the waits and the give-up are tested with a fake clock in tests/test_mirror_gate.py)


def test_a_new_stage_starts_the_copy_again_after_a_give_up(env: Any) -> None:
    import sqlite3

    from lead_desk.constants import MIRROR_MAX_TRIES

    rig = env.rig(leadflow=_flow())
    lid = str(rig.desk.handle("form", "g-1", lead(email="giveup@example.test")).body["lead_id"])
    con = sqlite3.connect(rig.db_path)
    con.execute(
        "UPDATE lead_state SET crm_tries=?, crm_error='CrmUnreachable' WHERE lead_id=?", (MIRROR_MAX_TRIES, lid)
    )
    con.commit()
    con.close()
    assert rig.desk.set_step(lid, "replied").status == 200
    assert rig.query("SELECT crm_tries, crm_error FROM lead_state WHERE lead_id=?", (lid,))[0] == (0, None)


# -- a new kind cannot be added without saying what it sends and whether it is real
def test_every_kind_is_listed_in_the_places_that_describe_it() -> None:
    from lead_desk import portal_api
    from lead_desk.constants import (
        CRM_CHOICES,
        CRM_STAND_IN,
        OUTBOX_CHOICES,
        OUTBOX_STAND_INS,
        QUALIFIER_CHOICES,
        QUALIFIER_STAND_IN,
    )

    assert set(CRM_CHOICES) == set(portal_api.CRM_FIELDS_SENT)  # what each CRM kind is sent
    for kind in CRM_CHOICES:
        assert ("crm" in Config(crm=kind).real_services) is (kind != CRM_STAND_IN), kind
    for kind in OUTBOX_CHOICES:
        assert ("mail" in Config(outbox=kind).real_services) is (kind not in OUTBOX_STAND_INS), kind
    assert QUALIFIER_STAND_IN in QUALIFIER_CHOICES


def test_a_copy_the_desk_gave_up_on_is_shown_in_the_warn_tone() -> None:
    js = LEADS_JS.read_text(encoding="utf-8")
    assert "not_copied: 'warn'" in js and "gave up" in js


def test_the_real_services_tone_goes_when_the_desk_comes_back_with_the_demo_tools(env: Any) -> None:
    """A page open on a desk with a real CRM, then the desk restarts with the stand-ins and the demo tools: the amber
    real-services style must not stay on the demo line."""
    _, base = fresh(env, crm_kind="hubspot")

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/")
        page.wait_for_selector("#demo-line.is-real")

        def as_demo(route: Any) -> None:
            reply = route.fetch()
            data = reply.json()
            data["mode"] = {"demo_tools": True, "sample_data": True, "real_services": []}
            route.fulfill(response=reply, body=json.dumps(data))

        page.route("**/api/ui/summary*", as_demo)
        page.wait_for_function(
            "() => !document.querySelector('#demo-line').classList.contains('is-real')", timeout=20000
        )

    _browse(base, body)
