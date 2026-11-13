"""Portal wording, layout and documentation pins, and the hide-email check in a real browser.
Made-up data only. Source-text checks prove the rule or word is there, not how a browser draws it; the browser
test (skipped when playwright or its browser is missing) proves the hide switch in the lead panel."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from support import call, lead, lead_ids_containing
from test_portal_api import fresh, get, seed

from lead_desk import portal_api

ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "src/lead_desk/portal"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _portal_js() -> str:
    return "\n".join(_read(str(p.relative_to(ROOT))) for p in sorted(PORTAL.rglob("*.js")))


def _keys(text: str) -> set[str]:
    return {k.strip() for k in text.split(",") if k.strip()}


# -- contract.md lists the fields the code returns
def test_contract_field_lists_match_the_code(env) -> None:
    contract = _read("contract.md")
    m = re.search(r"followups\{([^}]*)\}", contract)
    assert m and _keys(m.group(1)) == set(portal_api.FOLLOWUP_VIEWS)
    m = re.search(r"recent\[\{([^}]*)\}\]", contract)
    assert m and _keys(m.group(1)) == set(portal_api._FEED_KEYS)
    m = re.search(r"rows\[\{lead_id,([^}]*)\}\]", contract)
    assert m
    leads_keys = {"lead_id"} | _keys(m.group(1))
    assert leads_keys == (set(portal_api._LEAD_KEYS) - {"last_error", "ambiguous_prior"}) | {
        "state_view",
        "repeats",
        "step_label",
    }
    m = re.search(r"items\[\{request_id,([^}]*)\}\]", contract)
    assert m and {"request_id"} | _keys(m.group(1)) == set(portal_api._LOG_KEYS)
    rig, base = fresh(env)
    ids = seed(rig)
    detail = get(base, f"/api/ui/leads/{ids['ada']}")[1]
    m = re.search(
        r"GET /api/ui/leads/\{lead_id\}` \| 200 \| `\{now, lead\{\.\.\.\}, followup\{\.\.\.\}, ([^`]*)\}`", contract
    )
    assert m
    documented = {"now", "lead", "followup"} | {re.sub(r"[\[{(].*", "", k).strip() for k in _keys(m.group(1))}
    assert documented == set(detail)


def test_contract_and_readme_say_what_the_pause_covers_and_what_a_crash_row_shows() -> None:
    contract, limits = _read("contract.md"), _read("docs/limits.md")
    assert "any request that fails to reach the desk" in contract and "summary poll or the review poll" in contract
    assert (
        "does not pause them" in _read("docs/portal.md") and "cannot tell a stopped desk from a lost network" in limits
    )
    assert "`crash_while_sending` with `ambiguous_prior` 0" in contract
    assert "any other reason (`ambiguous`, `crash_while_sending`, others)" not in contract


def test_a_crash_row_that_never_reached_the_send_says_nothing_was_sent() -> None:
    item = {"kind": "failed", "reason": "crash_while_sending", "item_id": "F1"}
    out = portal_api._describe(item, 30, maybe_sent=False)
    assert out["title"] == "The follow-up email was not sent" and out["state_view"] == "failed"
    assert portal_api._describe(item, 30, maybe_sent=True)["state_view"] == "did_not_finish"


# -- words
def test_did_not_finish_says_what_happened_then_what_retry_does_and_checks_once() -> None:
    text = portal_api._DID_NOT_FINISH_TEXT
    assert "Retry is safe only" not in text and text.count(". ") == 1 and "remembers which emails" not in text
    for t in (*portal_api._NOTHING_SENT_TEXT.values(), portal_api._NOTHING_SENT_INTERNAL, text):
        assert "check the mail step" not in t.lower(), t  # the cue beside the Retry button says it
    step = portal_api._next_step("failed", "ambiguous", 1)
    assert step is not None and step.lower().count("check the mail step") == 1 and "remembers" not in step
    needs = _read("src/lead_desk/portal/pages/needs.js")
    assert "it.kind === 'failed' && acts.some" in needs and "P.icon('info'), 'Check the mail step first'" in needs


def test_one_verb_for_the_email_going_out_and_one_phrase_for_the_crm_wait() -> None:
    texts = _portal_js() + _read("src/lead_desk/portal_api.py") + _read("demo_only/portal_demo.js")
    assert not re.search(r"email (may have )?left\b", texts) and "whether the email left" not in texts
    assert "Not sent yet" not in texts and "Tried ' + fu.attempts" not in texts
    assert "Waiting for the CRM check" in _read("src/lead_desk/portal/portal.js")
    assert "waiting for the CRM check" in portal_api._UNKNOWN_TEXT["crm_unconfirmed"]
    assert "waiting for the CRM check" in _read("src/lead_desk/portal/pages/needs.js")
    assert "CRM not confirmed: the follow-up is waiting for the CRM check" in _read(
        "src/lead_desk/portal/pages/overview.js"
    )


def test_screen_and_doc_words() -> None:
    screens = _portal_js() + _read("src/lead_desk/portal_api.py") + _read("README.md") + _read("contract.md")
    screens += "".join(_read(f"docs/{n}") for n in ("limits.md", "settings.md", "why-this-code.md", "n8n-recipe.md"))
    assert "lead book" not in screens.lower().replace("lead-book", "")
    needs = _read("src/lead_desk/portal/pages/needs.js")
    assert "a replay" not in needs.split("var TABS", 1)[1].split("];", 1)[0]
    assert "shown in this table" in _read("src/lead_desk/portal/pages/log.js")
    assert "form door" not in _read("docs/n8n-recipe.md")
    assert "Every number below comes from the demo programs" in _read("README.md")
    leads = _read("src/lead_desk/portal/pages/leads.js")
    assert "CRM entry written" in leads and "'Entry written'" not in leads


def test_no_first_person_plural_in_comments_of_the_listed_files() -> None:
    for rel in (
        "src/lead_desk/server.py",
        "src/lead_desk/lockfile.py",
        "tests/support.py",
        "scripts/run_real_check.py",
    ):
        assert not re.search(r"\b(we|our|us)\b", _read(rel)), rel


# -- layout rules
def test_layout_rules_for_the_strip_the_drawer_the_cards_and_the_tiles() -> None:
    css = _read("src/lead_desk/portal/app.css")
    needs_css = _read("src/lead_desk/portal/pages/needs.css")
    needs = _read("src/lead_desk/portal/pages/needs.js")
    ov = _read("src/lead_desk/portal/pages/overview.js")
    assert re.search(r"\.sumstat-asof \{[^}]*grid-column: 1 / -1;[^}]*padding: var\(--s2\) var\(--s5\)", css)
    assert ".drawer.has-more .drawer-body" in css and "mask-image" in css
    assert "dlg.classList.toggle('has-more'" in _read("src/lead_desk/portal/portal.js")
    assert "align-items: start; }" in needs_css and "need-wide" not in needs_css + needs
    assert needs.index("'need-meta'") < needs.index(
        "'need-head'"
    )  # the meta row comes first, so rows start at one height
    assert "white-space: normal; min-height: 40px" not in css
    # the three amber-looking states keep three different colours, from the one map that the ring, its legend, the
    # badges, the timeline and the feed all read (P.stateTone); the ring takes its colour from that map
    portal = _read("src/lead_desk/portal/portal.js")
    state_tone = dict(re.findall(r"(\w+): '(\w+)'", re.search(r"var STATE_TONE = \{([^}]*)\}", portal).group(1)))  # type: ignore[union-attr]
    tones = {k: state_tone[k] for k in ("did_not_finish", "unknown", "crm_unconfirmed")}
    assert len(set(tones.values())) == 3
    leads_css = _read("src/lead_desk/portal/pages/leads.css")
    for tone in {*state_tone.values(), "neutral"}:  # neutral: the fallback of stateTone() for an unknown state
        assert f".tl > li.tl-{tone}::before" in leads_css, tone  # every tone has its timeline dot rule
    assert "bad" not in (
        state_tone["did_not_finish"],
        state_tone["unknown"],
        state_tone["crm_unconfirmed"],
    )  # amber states are never red
    assert "tone: P.stateTone(k)" in ov
    for tone in tones.values():
        assert f".sw-{tone} " in css or f".sw-{tone}{{" in css
        assert f".arc-{tone} " in _read("src/lead_desk/portal/pages/overview.css")
    assert "border-style: dashed; cursor" not in css.split(".btn:disabled, .btn-primary:disabled {")[-1].split("}")[0]
    assert ".tabs:empty { display: none; }" in css and ".needs-list > .stack { grid-column: 1 / -1; }" in needs_css
    assert "tab tab-loading" in needs and "tabs-asof" in needs


def test_a_page_that_cannot_load_keeps_its_rows_dimmed_and_disables_its_controls() -> None:
    leads = _read("src/lead_desk/portal/pages/leads.js")
    assert "hideBox.disabled = !ok" in leads and "out.classList.add('is-stale')" in leads
    assert "heading: 'The lead list is not available'" in leads
    assert "P.stale() ? 'No requests were decided in the '" in _read("src/lead_desk/portal/pages/overview.js")
    assert "P.stale() ? h('span', { 'class': 'faint' }, 'Last known values')" in _read("demo_only/portal_demo.js")


# -- the hide switch covers every value of a lead in the panel, repeats included (real browser)
def test_hide_switch_masks_email_phone_and_message_everywhere_in_the_lead_panel(env) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    rig, base = fresh(env)
    ids = seed(rig)
    rig.desk.handle("webhook", "ui-hide", lead(phone="555 0177", message="Call me after nine at night."))
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 (no browser installed here)
            pytest.skip(f"no browser: {e}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.add_init_script("window.sessionStorage.setItem('lead-desk-hide-personal', '1')")
            page.goto(base + "/ui/#/leads")
            page.click(f'[data-lead-id="{ids["ada"]}"]')
            page.wait_for_selector('[data-testid="later-messages"] .diff-row')
            shown = page.inner_text('[data-testid="lead-drawer"]')
            for raw in (
                "ada@example",
                "Ada@Example",
                "555 0100",
                "555 0199",
                "555 0177",
                "Please call me",
                "after nine",
            ):
                assert raw not in shown, raw
            assert "Phone" in shown
        finally:
            browser.close()


def test_flipping_the_hide_switch_repaints_an_open_lead_panel(env) -> None:
    """The panel is built once; the switch handler must fill it again, so masking applies at once."""
    sync_api = pytest.importorskip("playwright.sync_api")
    rig, base = fresh(env)
    ids = seed(rig)
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 (no browser installed here)
            pytest.skip(f"no browser: {e}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.add_init_script("window.sessionStorage.setItem('lead-desk-hide-personal', '0')")  # shown, as chosen
            page.goto(base + "/ui/#/leads")
            page.click(f'[data-lead-id="{ids["ada"]}"]')
            page.wait_for_selector('[data-testid="later-messages"] .diff-row')
            assert "555 0100" in page.inner_text('[data-testid="lead-drawer"]')
            page.evaluate(
                (
                    "() => { const b = document.querySelector('[data-testid=leads-hide-personal]');"
                    " b.checked = true; b.dispatchEvent(new Event('change')); }"
                )
            )
            shown = page.inner_text('[data-testid="lead-drawer"]')
            assert "555 0100" not in shown and "555 0199" not in shown and "ada@example" not in shown.lower()
        finally:
            browser.close()


# -- personal data is masked on screen by default
def test_masking_is_one_shared_rule_and_on_by_default() -> None:
    portal = _read("src/lead_desk/portal/portal.js")
    assert "!== '0'" in portal and "catch (e) { return true; }" in portal  # default and unreadable storage: masked
    assert "privacy: { hidden: hidePersonal" in portal
    for rel in ("src/lead_desk/portal/pages/leads.js", "src/lead_desk/portal/pages/data.js"):
        text = _read(rel)
        assert "localStorage" not in text and "maskEmail" not in text, rel  # no second copy of the rule


def test_search_by_address_is_off_while_masked(env, monkeypatch: pytest.MonkeyPatch) -> None:
    lead_ids_containing(monkeypatch, "ada0007")  # every lead number spells ada: the probes must not depend on chance
    rig, base = fresh(env)
    seed(rig)
    assert get(base, "/api/ui/leads?q=ada%40example")[1]["total"] >= 1
    assert get(base, "/api/ui/leads?q=ada%40example&by_email=0")[1]["total"] == 0
    # a name is not searched while masked (the probe has a space: a lead number cannot spell it by chance)
    assert get(base, "/api/ui/leads?q=Ada%20Ex&by_email=0")[1]["total"] == 0
    assert get(base, "/api/ui/leads?q=Ada%20Ex&by_email=1")[1]["total"] >= 1
    assert get(base, "/api/ui/leads?by_email=2")[0] == 400
    leads = _read("src/lead_desk/portal/pages/leads.js")
    assert "&by_email=0" in leads and "searchable after you turn off hiding" in leads
    assert "by_email" in _read("contract.md")


def test_personal_values_are_masked_by_default_in_a_real_browser(env) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    rig, base = fresh(env)
    ids = seed(rig)
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 (no browser installed here)
            pytest.skip(f"no browser: {e}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(base + "/ui/#/leads")  # first visit: nothing stored
            page.wait_for_selector("[data-lead-id]")
            table = page.inner_text('[data-testid="leads-table"]')
            assert "example.test" not in table.lower() and "ada@" not in table.lower()
            assert page.is_visible('[data-testid="leads-search-hint"]')
            page.click(f'[data-lead-id="{ids["ada"]}"]')
            page.wait_for_selector('[data-testid="later-messages"] .diff-row')
            shown = page.inner_text('[data-testid="lead-drawer"]')
            assert "555 0100" not in shown and "ada@example" not in shown.lower()
            page.keyboard.press("Escape")
            page.evaluate("() => { window.sessionStorage.setItem('lead-desk-hide-personal', '0'); }")
            page.reload()
            page.wait_for_selector("[data-lead-id]")
            assert "ada@example.test" in page.inner_text('[data-testid="leads-table"]')
            assert not page.is_visible('[data-testid="leads-search-hint"]')
        finally:
            browser.close()


# -- no raw email, phone or message in the desk's log output
def test_desk_logs_never_carry_a_request_value(env, caplog, capfd) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    rig, base = fresh(env)
    canary = {"email": "canary.mail9@example.test", "phone": "555 0Canary9", "message": "canary-message-9"}
    payload = lead(name="Canary Person", **canary)
    call(base, "POST", "/leads", {"source": "form", "source_ref": "canary-1", "payload": payload})
    call(
        base,
        "POST",
        "/leads",
        {"source": "form", "source_ref": "canary-1", "payload": {**payload, "phone": "555 0Canary8"}},
    )
    call(
        base,
        "POST",
        "/leads",
        {"source": "webhook", "source_ref": "canary-2", "payload": {**payload, "phone": "5" + chr(1) + "55"}},
    )
    call(base, "POST", "/leads", {"source": "webhook", "source_ref": "canary-3", "payload": {**payload, "name": None}})
    call(base, "POST", "/leads", raw=b'{"canary.mail9@example.test": ')
    for path in ("/api/ui/leads?q=canary", "/api/ui/review", "/api/ui/log", "/api/ui/summary"):
        get(base, path)
    out, err = capfd.readouterr()
    seen = caplog.text + out + err
    for value in (*canary.values(), "555 0Canary8"):
        assert value not in seen, value


# -- owner decision 2026-11-12: the user-visible term is "Lead stage", never "Pipeline step"
def test_no_pipeline_step_wording_in_the_shipped_tree() -> None:
    bad = []
    for base in ("src", "docs", "scripts", "tests", "demo_only"):
        for p in (ROOT / base).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".js", ".css", ".md", ".json", ".sql", ".html", ".ps1", ".sh"}:
                if p == Path(__file__):
                    continue
                if re.search(r"pipeline[ _]step", p.read_text(encoding="utf-8", errors="ignore"), re.I):
                    bad.append(str(p.relative_to(ROOT)))
    for name in ("README.md", "contract.md"):
        if re.search(r"pipeline[ _]step", _read(name), re.I):
            bad.append(name)
    assert not bad, bad


# -- the side-effect warning for the lead status write must stay in the docs
def test_lead_status_write_warning_is_in_the_docs() -> None:
    for rel in ("README.md", "docs/limits.md"):
        text = " ".join(_read(rel).split())
        for phrase in (
            "this write can start automations in the CRM you connect",
            "Any automation that watches that field",
            "The feature is OFF unless `DESK_LEADFLOW_FILE` is set",
            "lists every automation that reacts to lead status changes",
            "one made-up contact",
        ):
            assert phrase in text, (rel, phrase)
    assert "lists every automation that reacts to lead status changes" in " ".join(
        _read("docs/real-services.md").split()
    )
    assert "Side effects in your HubSpot" in _read("docs/limits.md")
