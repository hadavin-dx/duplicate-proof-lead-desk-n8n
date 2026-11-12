"""The portal asks the desk whether the demo extension is loaded (summary.mode) and never guesses by a failing request.
Made-up data only. The browser part runs the real src/lead_desk/portal/portal.js in node with a tiny stand-in page."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_portal_api import FakeDemo, fresh, get

from lead_desk import server

PORTAL_JS = Path(__file__).resolve().parent.parent / "src" / "lead_desk" / "portal" / "portal.js"

# A stand-in page (tests/portal_probe.js): every element is the same inert object; fetch records its paths.
PROBE = Path(__file__).resolve().parent / "portal_probe.js"

# the notice when the desk runs without the demo extension: the normal demo notice, plus what is not connected
NO_EXTENSION_LINE = (
    "Every lead is demo data. The demo CRM and demo mail are not connected. "
    "Nothing here is sent to real customers or real systems."
)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run_portal(tmp_path: Path, mode: dict[str, bool]) -> dict:
    out = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
        ["node", str(PROBE), str(PORTAL_JS), json.dumps(mode)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_without_the_extension_the_page_never_asks_for_demo_state(tmp_path: Path) -> None:
    """Without the demo extension the page never asks /api/demo/state (it would answer 404 and log a console error)."""
    seen = _run_portal(tmp_path, {"demo_tools": False, "sample_data": True})
    assert any("/api/ui/summary" in c for c in seen["calls"])  # the harness really ran the page
    assert not [c for c in seen["calls"] if "/api/demo/" in c], seen["calls"]
    assert NO_EXTENSION_LINE in seen["strip"]
    assert seen["hidden"] is False


@needs_node
def test_without_the_extension_and_without_sample_data_nothing_is_said(tmp_path: Path) -> None:
    seen = _run_portal(tmp_path, {"demo_tools": False, "sample_data": False})
    assert not [c for c in seen["calls"] if "/api/demo/" in c]
    assert seen["strip"] == "" and seen["hidden"] is True


@needs_node
def test_with_the_extension_the_page_asks_for_demo_state(tmp_path: Path) -> None:
    seen = _run_portal(tmp_path, {"demo_tools": True, "sample_data": True})
    assert any("/api/demo/state" in c for c in seen["calls"])


def test_summary_mode_follows_what_the_desk_loaded(env, monkeypatch) -> None:
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: None)
    _, base = fresh(env)
    status, out = get(base, "/api/ui/summary")
    assert status == 200 and out["mode"] == {"demo_tools": False, "sample_data": True, "real_services": []}
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: FakeDemo())
    _, base2 = fresh(env)
    assert get(base2, "/api/ui/summary")[1]["mode"]["demo_tools"] is True


# -- one word, one meaning (the Needs a person number counts more than the incomplete requests)
PORTAL_DIR = PORTAL_JS.parent


def _portal_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(PORTAL_DIR.rglob("*.js")))


def test_the_request_group_is_not_called_needs_a_person() -> None:
    """The request group is not named "Needs a person": that is the tile's name, and the two show different numbers."""
    text = _portal_text()
    assert "review: 'Needs a person'" not in text
    assert "label: 'Needs a person', value: c.review" not in text
    assert "label: 'Request to check', value: c.review" in text
    assert "Requests and follow-ups a person has to decide." in text  # said where the tile is


def test_one_name_for_the_hide_switch_and_calm_wording_for_a_missing_time() -> None:
    text = _portal_text() + (PORTAL_DIR.parent.parent.parent / "demo_only" / "portal_demo.js").read_text(
        encoding="utf-8"
    )
    # the switch has one name; no other name for it may appear
    assert "Hide contact details" not in text and "Hiding personal details" not in text
    assert "Hide names and email addresses" in text and "Hiding names and email addresses" in text
    assert "Hide email addresses" not in text and "Hiding email addresses" not in text
    assert "'time unknown'" not in text and "time not saved" in text


def test_the_group_name_covers_requests_the_desk_could_not_store_or_decide() -> None:
    """The group name is not "Incomplete request": it also holds internal_error and internal_unstorable."""
    text = _portal_text()
    assert "review: 'Incomplete request'" not in text
    assert text.count("review: 'Request to check'") == 3  # verdict, result and kind: one meaning
    assert "could not store or decide" in text


def test_a_failed_card_keeps_the_desks_own_title() -> None:
    """A failed card shows the desk's own title: "was not sent" would be wrong when the email may have left."""
    text = _portal_text()
    assert "title: { failed:" not in text and "failed: 'The follow-up email was not sent'" not in text
    assert "kind === 'failed' ? 'The follow-up email was not sent'" not in text


def test_the_next_step_line_matches_the_buttons_of_the_card() -> None:
    """The page shows the desk's one next-step text (api next_step) and writes none of its own."""
    text = _portal_text()
    assert "nextStep(" not in text and "d.next_step" in text
    api = (Path(__file__).resolve().parents[1] / "src/lead_desk/portal_api.py").read_text(encoding="utf-8")
    assert "crm_unconfirmed" in api and "The CRM entry is there" in api and "The CRM entry is not there" in api


def test_the_data_page_row_follows_the_real_hide_switch() -> None:
    """The row follows the personal hide switch (Portal.privacy), not the desk's masking setting (always false)."""
    data_js = (PORTAL_DIR / "pages" / "data.js").read_text(encoding="utf-8")
    assert "a.masking" not in data_js
    assert "P.privacy.hidden()" in data_js
    assert "lead-desk-hide-personal" in (PORTAL_DIR / "portal.js").read_text(encoding="utf-8")


def _css() -> str:
    return (Path(__file__).resolve().parents[1] / "src/lead_desk/portal/app.css").read_text(encoding="utf-8")


def test_table_status_chips_wrap_and_the_status_column_keeps_a_floor() -> None:
    """Static CSS check: proves the rules exist, not how the browser lays them out."""
    css = _css()
    assert ".table td .pill { white-space: normal;" in css  # a long chip wraps instead of widening its column
    assert ".log-out .table td:last-child" in css and "min-width: 9.5rem" in css  # status words are never cut
    assert ".table-wrap { overflow-x: auto;" in css  # last resort: the table scrolls, nothing is clipped
    assert ".legend li { display: flex; white-space: nowrap; }" in css.replace(
        ".ov .card .splitbar .legend li", ".legend li"
    )


def _read(rel: str) -> str:
    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def test_layout_rules_and_wording_are_in_the_source() -> None:
    """Source-text and CSS-text checks: they prove the rule or word is there, not how a browser draws it.
    Each fails when its rule or word is removed."""
    css, needs_css = _read("src/lead_desk/portal/app.css"), _read("src/lead_desk/portal/pages/needs.css")
    # the log table fits its card at 1100: slimmer rail and margin, Received stacks its two lines
    assert "@media (max-width: 1200px)" in css and "grid-template-columns: 208px" in css
    assert ".log-when { flex-direction: column;" in css
    assert ".leads-out .table td:last-child" in css  # the leads table keeps its last-change time on one line
    # the needs cards: each starts at the top, no stretched empty band
    assert ".needs-list, body .needs-list { align-items: start; }" in needs_css
    # one name for the demo data, no jargon
    portal = _read("src/lead_desk/portal/portal.js")
    assert NO_EXTENSION_LINE in portal and "SAMPLE" not in portal and "demo tools are not installed" not in portal
    assert "keyed outbox" not in _read("src/lead_desk/portal_api.py")
    # the Needs tile: one nowrap link, no repeated list of kinds; the empty activity card is a real empty state
    ov = _read("src/lead_desk/portal/pages/overview.js")
    assert (
        "'class': 'nowrap'" in ov
        and "Review these" in ov
        and "emptyState({ heading: P.stale() ? 'No requests were decided" in ov
    )
    # the donut legend percentages add up to 100 (largest remainder)
    assert "pcts[i]" in portal and "Math.round(100 * p.value / total)" not in portal
    # the personal hide switch is a muted line, not an amber settings row
    data = _read("src/lead_desk/portal/pages/data.js")
    assert "accessRow(hideSwitchOn()" not in data and "data-hide-note" in data
    # one label for the CRM row of the drawer, and the CRM strip flags rows beyond this desk's leads
    demo = _read("demo_only/portal_demo.js")
    assert demo.count("'CRM entries'") >= 2 and "CRM entries for this lead" not in demo
    assert "another desk may write to the same demo CRM" in demo
    assert "one card per lead" in _read("src/lead_desk/portal/pages/needs.js").lower()


def test_words_and_chips_are_in_the_source() -> None:
    """Source-text checks: they prove the words and rules are there, not how a browser draws them."""
    portal, demo = _read("src/lead_desk/portal/portal.js"), _read("demo_only/portal_demo.js")
    needs, data = _read("src/lead_desk/portal/pages/needs.js"), _read("src/lead_desk/portal/pages/data.js")
    # "reply" is only the email a lead gets; the AI step is a rating, so these phrases must not appear
    for text in (demo, data):
        assert "AI replies" not in text and "pre-written replies" not in text and "Replies are looked up" not in text
    assert "The email each new lead gets." in _read("src/lead_desk/portal/pages/overview.js")
    # outcome unknown is the email; the CRM case and the maybe-left failure have their own chips
    assert "crm_unconfirmed: 'CRM not confirmed'" in portal and "did_not_finish: 'Did not finish'" in portal
    assert "it.state_view" in needs and "state_view" in _read("src/lead_desk/portal/pages/leads.js")
    assert "state_view" in _read("src/lead_desk/portal/pages/log.js")
    # the mail stand-in has one name; these other names and the word "made-up" must not appear on screen
    assert "demo outbox" not in demo and "Demo email" not in demo and "made-up" not in demo.split("*/", 1)[1]
    # plain words
    assert "corrected submission" not in needs.replace("Send a corrected submission with a NEW request number", "")
    for jargon in ("Reference from the sender", "Pages only read", "Stop on purpose", "Set the stop", "stop setting"):
        assert jargon not in portal + data + demo + needs, jargon
    # one word per meaning: the mail step, the request number, the lead book; a webhook is not an email
    for variant in ("mail system", "lead book", "database file", "same reference", "Email message", "request id"):
        assert variant not in portal + data + needs, variant
    assert "webhook: 'Webhook'" in portal
    # time not saved is not shown for the CRM steps of a healthy lead
    assert "add('CRM entry written', false" in _read("src/lead_desk/portal/pages/leads.js")
    # the strip's explanation of a total-only CRM
    assert "does not record which lead an entry belongs to" in demo
    assert "demo-view" not in demo  # the strip shows only what the demo CRM really holds
    # the chips fit: one line for the log result chip from 1500 px, nav item does not wrap
    css = _read("src/lead_desk/portal/app.css")
    assert (
        "@media (min-width: 1500px) { .log-out .table td:nth-child(4) .pill { max-width: none; white-space: nowrap; } }"
        in css
    )
    assert ".nav a { gap: var(--s2);" in css


def _demo_strip(arg: dict[str, object]) -> dict:
    probe = Path(__file__).resolve().parent / "portal_demo_probe.js"
    demo_js = Path(__file__).resolve().parents[1] / "demo_only" / "portal_demo.js"
    out = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
        ["node", str(probe), str(demo_js), json.dumps(arg)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_the_crm_strip_flags_entries_beyond_this_desks_leads() -> None:
    """Renders the real demo extension. 36 entries beside 30 leads are flagged, so they do not read as a duplicate."""
    same = _demo_strip({"count": 30, "new": 30})
    assert "more than this desk has leads" not in same["text"] and same["warn"] is False
    more = _demo_strip({"count": 33, "new": 30})
    assert "It holds 3 more than this desk has leads" in more["text"] and more["warn"] is True
    # the number shown is the CRM's own count and matches its sentence (30 leads + 3)
    assert "33 CRM entries" in " ".join(more["text"].split())


def test_vocabulary_and_layout_rules_are_in_the_source() -> None:
    """Source-text checks (rules and words exist, not how a browser draws them)."""
    ov, leads = _read("src/lead_desk/portal/pages/overview.js"), _read("src/lead_desk/portal/pages/leads.js")
    needs, data = _read("src/lead_desk/portal/pages/needs.js"), _read("src/lead_desk/portal/pages/data.js")
    css, needs_css = _read("src/lead_desk/portal/app.css"), _read("src/lead_desk/portal/pages/needs.css")
    # the overview donut, legend and live feed use the desk's state words
    assert "'did_not_finish', 'unknown', 'crm_unconfirmed'" in ov and "r.state_view || r.state" in ov
    # the drawer shows the email and the CRM separately, and a failed banner is red
    assert "step('Follow-up email'" in leads and "step('CRM'" in leads and "'note-bad'" in leads
    assert "(no time kept)" in leads and "no time is kept for this step" not in leads
    # words: the AI receiver is not the rating, one name for the CRM entry, lead not person, Web form capitalised
    assert "Given to the AI rating" not in data and "The email address is sent to the AI." in data
    assert "rate each new lead" in data and "call each new lead" not in data
    assert "Every lead the desk has stored" in leads and ".toLowerCase()" not in needs.split("Source: ")[1][:60]
    assert "CRM row" not in _read("src/lead_desk/portal_api.py")
    assert "'Did not finish'" in _read("src/lead_desk/portal/portal.js")
    # layout: the more-below hint is a strip, the Needs spare height goes between parts, amber card for did not finish
    assert ".drawer-more { position: static;" in css and "align-content: start; align-self: start" in needs_css
    assert 'data-view="did_not_finish"' in needs_css and "'data-view'" in needs
    assert "td:nth-child(2) { white-space: nowrap; max-width: 9rem" in css
    assert "@media (max-width: 1200px) { .dt-two" in _read("src/lead_desk/portal/pages/data.css")
    assert "the the AI" not in data


def test_shared_words_and_no_leftover_hooks_in_the_page_source() -> None:
    """One word per meaning in the page source, and no attributes that only a test would read."""
    portal = _read("src/lead_desk/portal/portal.js")
    needs = _read("src/lead_desk/portal/pages/needs.js")
    pages = "src/lead_desk/portal/pages/"
    sources = portal + needs + _read(pages + "overview.js") + _read(pages + "leads.js")
    assert "dataset.jsErrors" not in portal and "dataset.cspViolations" not in portal and "bump(" not in portal
    assert (
        "Details for support" not in portal + needs
        and portal.count("Technical details") == 1
        and needs.count("Technical details") == 1
    )
    assert "new RegExp" not in needs and "web_form" not in portal
    assert "labels.door" not in sources and "labels.source" in sources
    assert "Request record number" not in sources and "Receipt number" in sources
    assert "desk record" not in sources and "request record" not in sources
    data = _read("src/lead_desk/portal/pages/data.js")
    assert "cannot drift" not in data and "Built from the fields the AI step is set up to receive" in data
    assert "declared inputs" not in data and "Database file encryption" not in data


# -- the pages while the desk does not answer (the real portal.js and page files, run in node)
STATE_PROBE = Path(__file__).resolve().parent / "portal_state_probe.js"


def _state_probe(scenario: str) -> object:
    out = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
        ["node", str(STATE_PROBE), str(PORTAL_DIR), scenario],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_actions_pause_while_the_desk_is_offline_or_answers_with_a_problem() -> None:
    """Retry stays off on last known values, with a line saying why; it wakes when the desk is back."""
    phases = {p["phase"]: p for p in _state_probe("pause")}  # type: ignore[union-attr]
    line = "Actions are paused until the desk answers."
    for name in ("answering", "back", "recovered"):
        assert phases[name]["buttons"] == [{"action": "retry", "disabled": False, "outline": True}], name
        assert phases[name]["notes"] == [], name
    for name in ("offline", "degraded"):
        assert phases[name]["buttons"] == [{"action": "retry", "disabled": True, "outline": True}], name
        assert [n for n in phases[name]["notes"]] == [line], name


@needs_node
def test_route_ids_from_the_address_bar_cannot_reach_object_prototype() -> None:
    """Hash ids such as __proto__ fall back to the overview and leave Object.prototype untouched."""
    seen = _state_probe("route-proto")
    got = {r["hash"]: r["rendered"] for r in seen["out"]}  # type: ignore[index]
    assert got == {
        "#/__proto__": "overview",
        "#/constructor": "overview",
        "#/toString": "overview",
        "#/prototype": "overview",
        "#/hasOwnProperty/x": "overview",
        "#/needs": "needs",
    }
    assert seen["polluted"] == [] and seen["unmountOnProto"] is True  # type: ignore[index]


@needs_node
def test_a_fresh_load_whose_first_answer_never_arrives_shows_the_desk_is_not_answering() -> None:
    """No earlier data and the first request fails: the whole page says the desk is not answering (no skeleton left)."""
    seen = _state_probe("fresh-fail")
    assert "The desk is not answering" in seen["text"]  # type: ignore[index]
    assert "Nothing has loaded yet" in seen["text"] and "skeleton" not in seen["testids"]  # type: ignore[index]


def test_pause_is_one_flag_and_the_desk_side_is_unchanged() -> None:
    """The pause reads the shell's own offline/problem flag; the desk's /review answers are not touched by it."""
    portal, needs = _read("src/lead_desk/portal/portal.js"), _read("src/lead_desk/portal/pages/needs.js")
    assert "function stale()" in portal and "emit('link'" in portal and "stale: stale" in portal
    assert (
        "disabled: P.stale()" in needs
        and "P.on('link'" in needs
        and "Actions are paused until the desk answers." in needs
    )
    assert "paused" not in _read("src/lead_desk/portal_api.py")


def test_chip_wording_and_needs_page_text_are_in_the_source() -> None:
    """Source-text checks (the word or rule exists, not how a browser draws it); each fails when its word is removed."""
    needs, leads = _read("src/lead_desk/portal/pages/needs.js"), _read("src/lead_desk/portal/pages/leads.js")
    log, ov = _read("src/lead_desk/portal/pages/log.js"), _read("src/lead_desk/portal/pages/overview.js")
    portal, api = _read("src/lead_desk/portal/portal.js"), _read("src/lead_desk/portal_api.py")
    demo = _read("demo_only/portal_demo.js")
    # the Needs intro: each badge word is quoted and defined once, in a short list of the badges on the page
    assert "“Request to check” means a request with missing details" in needs and "Request to checks" not in needs
    assert "“Failed” means the email was not sent" in needs and "function definitions(items)" in needs
    assert "What do" not in needs  # the collapsed row explains why, it does not define the badges a second time
    # the lead panel: the table row and the timeline say the same for a lead whose CRM entry is unclear
    assert "P.ui.waitingChip()" in leads and "'Follow-up email: waiting for the CRM check'" in leads
    assert "not tried yet" not in leads and "No email was tried yet" not in portal
    # a did-not-finish dot is amber, never red: the timeline dot takes its colour from the one state-to-colour map
    # (portal.js STATE_TONE), where did_not_finish is amber ('warn') and only a plain failed is red ('bad')
    state_tone = dict(re.findall(r"(\w+): '(\w+)'", re.search(r"var STATE_TONE = \{([^}]*)\}", portal).group(1)))  # type: ignore[union-attr]
    assert state_tone["did_not_finish"] == "warn" and state_tone["failed"] == "bad"
    leads_css = _read("src/lead_desk/portal/pages/leads.css")
    for tone in {*state_tone.values(), "neutral"}:  # neutral: the fallback of stateTone() for an unknown state
        assert f".tl > li.tl-{tone}::before" in leads_css, tone  # every tone has its timeline dot rule
    assert "bad" not in (
        state_tone["did_not_finish"],
        state_tone["unknown"],
        state_tone["crm_unconfirmed"],
    )  # amber states are never red
    assert "P.stateTone(d.state_view || fu.state)" in leads
    assert "? 'bad' : 'warn'" not in leads  # no second, local colour rule that could disagree with the map
    # the banner names both buttons exactly as the card does
    assert "“I checked: it was sent” or “I checked: it was not sent”" in api
    assert "“I checked”. The desk" not in api
    # one list cell, used by both lists: the email state, and the CRM state on its own chip
    assert "P.ui.followupChips(" in leads and "P.ui.followupChips(" in log
    assert (
        "CRM: not confirmed" not in portal + needs + leads + log + ov and "text: 'Waiting for the CRM check'" in portal
    )
    assert "label: 'Request number'" in log and "No lead linked" in needs
    assert "label: 'Follow-up'," in log and "Follow-up now" not in log
    # the mail step is explained where the reader must act
    assert "mail step (the part that sends the email)" in api and "Emails to this address" in demo
    # the demo block: no empty label, grammar for one entry, plain words for a CRM that counts everything
    assert "h('dt', null, '')" not in demo and "'CRM entry'" in demo and "it counts every entry it receives" in demo
    assert "Does not check: counts every entry it gets." not in demo
    assert "'Request', itemSel" in demo and "'Item', itemSel" not in demo
    # the donut centre counts follow-ups, the axis says ago, the bridge line is there
    assert (
        "unit: 'follow-ups'" in ov
        and "return min + 'm'" in ov
        and "' min ago'" not in ov
        and "Needs a person also counts follow-ups, not only requests to check." in ov
    )
    # loading: five tiles, a word, tab placeholders; the degraded Overview says last known like the offline one
    assert "i < 5; i++" in ov and "Loading" in portal and "tab-loading" in needs
    assert "P.stale(); // offline or answering with a problem" in demo
    # a Retry that comes with "check first" is outlined, not the main button
    assert "it.kind === 'failed') ? 'btn-outline'" in needs


# -- the pages in node: axis labels, last-update marks, the demo notice line, Retry cards, the pause flag
@needs_node
def test_the_activity_axis_uses_short_labels_that_fit() -> None:
    """Five marks plus "now" in the narrow chart: the long phrase ran into its neighbour at 1280 px."""
    rows = {p["phase"]: p for p in _state_probe("stale")}  # type: ignore[union-attr]
    assert rows["answering"]["ticks"] == ["5m", "4m", "3m", "2m", "1m", "now"]
    assert rows["answering"]["link"] is False  # the send-a-request link needs the demo extension


@needs_node
def test_every_number_and_card_says_it_shows_the_last_update_while_the_desk_is_not_answering() -> None:
    rows = {p["phase"]: p for p in _state_probe("stale")}  # type: ignore[union-attr]
    for name in ("offline", "degraded"):
        assert rows[name]["kpis"] >= 4 and rows[name]["marked"] == rows[name]["kpis"], name  # every number card
        assert rows[name]["cardNotes"] >= 5, name  # reconcile, sources, follow-ups, activity, feed
    for name in ("answering", "recovered"):
        assert rows[name]["marked"] == 0 and rows[name]["cardNotes"] == 0, name


@needs_node
def test_the_demo_notice_line_never_claims_demo_without_an_answer() -> None:
    """While waiting it says it is checking; when the first answer fails it says it could not check."""
    loading, failed = _state_probe("loading"), _state_probe("fresh-fail")
    assert loading["line"] == "Checking whether this desk shows demo data…"  # type: ignore[index]
    assert failed["line"] == "Could not check whether this desk shows demo data."  # type: ignore[index]
    assert "DEMO" not in failed["line"] and "Loading" in loading["text"]  # type: ignore[index]
    assert loading["text"].count("Loading") == 1  # one word, then each card has its own title  # type: ignore[index]
    for title in ("Where requests come from", "Follow-ups", "Activity", "Live feed"):
        assert title in loading["text"], title  # type: ignore[index]


@needs_node
def test_retry_looks_the_same_on_failed_and_did_not_finish_cards_and_says_to_check_first() -> None:
    seen = _state_probe("cards")
    cards = {c["view"]: c for c in seen["cards"]}  # type: ignore[index]
    assert cards["failed"]["buttons"] == ["outline"] and cards["did_not_finish"]["buttons"] == ["outline"]
    assert cards["failed"]["checkFirst"] and cards["did_not_finish"]["checkFirst"]
    # the badge meanings: each once, and only for the badges that are on the page
    define = seen["define"]  # type: ignore[index]
    assert "“Failed” means" in define and "“Did not finish” means the desk tried the follow-up and stopped" in define
    assert "Outcome unknown" not in define and "CRM not confirmed" not in define and "Request to check" not in define
    assert seen["text"].count("“Failed” means") == 1  # type: ignore[index]


@needs_node
def test_the_demo_extensions_own_failing_poll_does_not_pause_the_buttons() -> None:
    """Only the summary and review answers decide "the desk does not answer well"."""
    row = _state_probe("demo-poll")
    assert row["buttons"] == [{"action": "retry", "disabled": False, "outline": True}] and row["notes"] == []  # type: ignore[index]


def test_pause_flag_sources_and_new_layout_rules() -> None:
    """Source-text and CSS-text checks (the rule exists, not how a browser draws it); each fails when removed."""
    portal, ov = _read("src/lead_desk/portal/portal.js"), _read("src/lead_desk/portal/pages/overview.js")
    needs, leads = _read("src/lead_desk/portal/pages/needs.js"), _read("src/lead_desk/portal/pages/leads.js")
    data, demo = _read("src/lead_desk/portal/pages/data.js"), _read("demo_only/portal_demo.js")
    css, needs_css = _read("src/lead_desk/portal/app.css"), _read("src/lead_desk/portal/pages/needs.css")
    # one pause flag: the shell's, limited to the two answers every page lives on, and cleared when a poll stops
    assert "STALE_KEYS = ['summary', 'review']" in portal and "delete bad[key]" in portal
    assert "var on = P.stale();" in ov and "failure !== null && summary !== null" not in ov
    assert "P.stale()" in demo and "dbl.disabled = dblBusy || !items.length || P.stale()" in demo
    # the offline pages: the search and filter wait with an error list, Needs shows no tab placeholders under an error
    assert "P.demo && !P.stale()" in ov  # no live "send a request" link while the numbers are the last known ones
    assert "function listWorks(ok)" in leads and "P.ui.clear(tabBar)" in needs
    # the timeline names the CRM entry like the table and keeps the capital of Webhook
    assert "'CRM entry written'" in leads and "'Sent to the CRM'" not in leads and "function viaSource(" in leads
    # the hide switch row of the Data page has its On or Off chip
    assert "data-hide-chip" in data
    # a waiting card has a plain word for waiting; the Leads page explains why the badge can be larger than the list
    assert "“Waiting” means the desk has the item" in needs and "can be larger than the leads listed here" in leads
    # CSS: a light track when off, a readable zero bar and stale donut, loading blocks, the lead panel gap
    assert ".hide-switch input:not(:checked) { background: var(--surface-3);" in css
    assert "var(--surface); box-shadow: 0 0 0 1px" in css
    assert ".ov.is-stale { filter: none; }" in css
    assert ".ov-h0 { height: 3px; background: var(--border-strong); }" in css
    assert ".skel.skel-card" in css and "kpi-asof" in css
    assert ".need-main { align-content: start; }" in needs_css and "min-height: 2.6em" not in needs_css
    assert ".dm-lead .dt-foot { padding-top: 0;" in _read("demo_only/portal_demo.css")


# -- later messages and masking (claims P8 and P10); the probe modes of tests/portal_probe.js
def _probe_mode(mode: str) -> dict:
    out = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
        ["node", str(PROBE), str(PORTAL_JS), json.dumps("demo"), mode],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_personal_hidden_rule() -> None:
    got = _probe_mode("hidden")
    # no stored value, '1', an unknown value and unreadable storage: masked; only '0' reveals
    assert (got["none"], got["one"], got["zero"], got["other"], got["blocked"]) == (True, True, False, True, True)
    # storage that works: the choice is read back from storage (another tab's change is not hidden by a stale copy)
    assert (got["setOff"], got["keptOff"], got["otherTab"]) == (False, "0", True)
    assert got["events"] == [False, True, False, True]  # every set tells the listeners
    # storage whose write fails: the choice lasts for this visit
    assert (got["memOff"], got["memOn"]) == (False, True)
    # a request number is data the sender chose, so EVERY one is masked while the switch is on (a plain number, one
    # with an address in it, a long one), and shown as it is, or as an ellipsis and its last 6 characters, when off
    mask = "••••••"
    assert got["refShownOff"] == "a@example.test" and got["refPlainOff"] == "ui-1"
    assert got["refShortOff"] == "…" + ("x" * 20 + "@b.example")[-6:] and got["refShortPlainOff"] == "ui-1"
    assert (got["refMasked"], got["refPlain"], got["refShort"], got["refLongMasked"]) == (mask,) * 4
    assert got["maskedEmail"] == "•••@•••••"  # no character of the address kept
    assert got["maskedPhone"] == "••• ••• ••••"


@needs_node
def test_time_after_first_words() -> None:
    got = _probe_mode("after-first")
    assert got["words"] == [
        "In the same second as the first message",  # 0
        "1 second after the first message",  # 1
        "59 seconds after the first message",  # 59
        "1 minute after the first message",  # 60
        "1 minute after the first message",  # 119
        "2 minutes after the first message",  # 120
        "59 minutes after the first message",  # 3599
        "1 hour after the first message",  # 3600
        "2 hours after the first message",  # 7380
        "23 hours after the first message",  # 86399
        "1 day after the first message",  # 86400
        "2 days after the first message",  # 172800
        "Arrived after the first message; the desk clock gave it an earlier time, so no gap is shown",  # -10, rid 9 > 5
        "Arrived before the first record was written",  # rid 3 < first 5
    ]
    assert got["rel"] == ["just now", "just now", "5s ago", "1m ago", "1h ago", "1d ago"]


def test_later_panel_rules_in_the_source() -> None:
    """Source pins: the rule is in the source, not how a browser draws it (the evidence run checks that)."""
    leads = _read("src/lead_desk/portal/pages/leads.js")
    portal = _read("src/lead_desk/portal/portal.js")
    for pin in (
        "shownValue(f.field, f.first, 'lead:'",
        "shownValue(f.field, f['this'], 'later:'",
        "shownValue('phone', l.phone, 'lead:'",
        "'data-testid': 'later-messages'",
        "Same email address, new request. The first record never changes.",
        "'Showing the newest '",
        "function paint(d)",
        "P.on('personal', applyMask)",
        "if (P.privacy.hidden()) p += '&by_email=0';",
    ):
        assert pin in leads, pin
    assert "timeline-more" not in leads and "dup-diff" not in leads and "repeat differences" not in leads
    assert leads.count("&by_email=0") == 1
    render = leads.split("function render(d, status, force)", 1)[1].split("function paint(d)", 1)[0]
    assert "if (drawer && !force) return;" in render and "paint(d);" in render  # the pause is in render only
    apply_mask = leads.split("function applyMask()", 1)[1].split("hideBox.addEventListener", 1)[0]
    assert (
        "paint(lastData)" in apply_mask and "drawer" not in apply_mask.split("paint(lastData)")[0].split("else if")[-1]
    )
    assert "q = ''; input.value = '';" in apply_mask and "restart();" in apply_mask
    assert "setAttribute('aria-label', input.placeholder)" in leads
    # the key and the storage are named once, in portal.js
    assert "localStorage" not in leads and "sessionStorage" not in leads and "lead-desk-hide-personal" not in leads
    assert "!== '0'; } catch (e) { return true; }" in portal and "function shownRef(" in portal
    assert "var t = shownRef(id);" in portal  # shortRef (and so requestRef) goes through shownRef
    assert "t.charAt(0)" not in portal.split("function maskEmail", 1)[1].split("\n", 1)[0]
    assert "addEventListener('storage'" not in portal  # session storage never reaches another tab
    # every place that prints a request number goes through the masking rule
    pages = "".join(_read(f"src/lead_desk/portal/pages/{n}.js") for n in ("leads", "log", "overview", "needs", "data"))
    for bad in ("+ fr.source_ref", "+ r.request_id", "+ f.ref"):
        assert bad not in pages, bad
    assert "P.ui.refNode(d.first_receipt.source_ref" in leads and "P.shownRef(u.source_ref)" in leads
    assert "line-through" not in _read("src/lead_desk/portal/pages/leads.css").split(".diff-old", 1)[1].split("}", 1)[0]


# -- one value shown with its eye button (memory only; the switch ON clears it)
def _reveal_probe(portal_js: Path) -> dict:
    out = subprocess.run(  # noqa: S603 (fixed argument list, no shell)
        ["node", str(PROBE), str(portal_js), json.dumps("demo"), "reveal"],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _check_reveal(got: dict) -> None:
    assert got["start"] == [False, False] and got["oneOn"] == [True, False]  # two values are independent
    assert got["bothOn"] == [True, True] and got["aOff"] == [False, True]  # a second click hides one again
    assert got["guardedMasked"] is True and got["guardedEmpty"] == ""  # an empty value gets no eye
    assert got["afterSwitchOn"] == [False, False]  # the switch ON clears every single reveal
    assert got["cleared"] is False  # Escape, a page change and closing the panel call clear()
    assert got["guardedOff"] == "a@example.test"  # the switch OFF: the plain value, no eye
    assert got["writes"] == [] and got["ms"] == 30000  # nothing but the switch is ever stored; one named time


@needs_node
def test_reveal_one_value_rules() -> None:
    _check_reveal(_reveal_probe(PORTAL_JS))


@needs_node
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "    emit('reveal');\n    // the button",
            "    window.sessionStorage.setItem('reveal', key);\n    emit('reveal');\n    // the button",
        ),  # persisted
        ("    if (hidePersonal()) clearReveals(true); // the switch ON", "    // the switch ON"),  # survives switch ON
        (
            "function revealOn(key) { return key in revealed; }",
            "function revealOn(key) { return true; }",
        ),  # shared by all
    ],
)
def test_mutant_reveal(tmp_path: Path, old: str, new: str) -> None:
    text = PORTAL_JS.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.count(old) == 1, old
    bad = tmp_path / "portal.js"
    bad.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(AssertionError):
        _check_reveal(_reveal_probe(bad))


def test_reveal_rules_are_in_the_source() -> None:
    portal = _read("src/lead_desk/portal/portal.js")
    leads = _read("src/lead_desk/portal/pages/leads.js")
    assert "var REVEAL_MS = 30000;" in portal and portal.count("REVEAL_MS") >= 3
    reveal_block = portal.split("function guarded(", 1)[1].split("function maskEmail", 1)[0]
    assert "<button" not in reveal_block and "h('button', { 'class': 'eye-btn', type: 'button'" in reveal_block
    for pin in ("'aria-pressed'", "'aria-label': label", "'Show ' : ", "'hidden ' + word", "e.stopPropagation()"):
        assert pin in reveal_block.replace("(on ? 'Hide ' : 'Show ')", "'Show ' : "), pin
    assert "localStorage" not in reveal_block and "sessionStorage" not in reveal_block
    assert "console." not in reveal_block and "location" not in reveal_block
    assert "e.key === 'Escape') clearReveals()" in portal and "clearReveals(true);\n    clear(byId('toasts'))" in portal
    # keys are the lead id (or receipt) plus the field, never a row position
    for key in (
        "'lead:' + r.lead_id + ':email'",
        "'lead:' + l.lead_id + ':phone'",
        "'later:' + d.lead.lead_id + ':' + u.receipt_id",
    ):
        assert key in leads, key
    assert "P.reveal.clear()" in leads and "P.on('reveal'" in leads
    assert "does not turn name or address search on." in leads


# -- the screen-reader class has its rule (a renamed selector once made the hidden words visible)
def _screen_reader_css_ok(css: str, js: str) -> bool:
    used = re.search(r"'class': 'sr-only'", js) is not None
    rule = re.search(r"(?m)^\.sr-only \{[^}]*position: absolute;[^}]*clip: rect\(0 0 0 0\);[^}]*\}", css)
    meta = re.search(r"\.later-meta \{[^}]*\}", css + _read("src/lead_desk/portal/pages/leads.css"))
    return used and rule is not None and meta is not None and "position: absolute" not in meta.group(0)


def test_every_hidden_word_class_has_its_rule() -> None:
    assert _screen_reader_css_ok(_read("src/lead_desk/portal/app.css"), _portal_text())


def test_mutant_screen_reader_rule_deleted() -> None:
    css = _read("src/lead_desk/portal/app.css")
    lines = [ln for ln in css.splitlines() if not ln.startswith(".sr-only {")]
    assert len(lines) < len(css.splitlines()) and not _screen_reader_css_ok("\n".join(lines), _portal_text())
    renamed = css.replace(".sr-only {", ".later-meta {", 1)  # the actual slip: the selector renamed
    assert not _screen_reader_css_ok(renamed, _portal_text())


def test_one_way_to_say_an_email_was_sent() -> None:
    """Badges, KPI and buttons say Sent / not sent; sentences say 'was sent' and 'may already have been sent'."""
    texts = _portal_text() + _read("src/lead_desk/portal_api.py") + _read("contract.md") + _read("README.md")
    texts += _read("demo_only/portal_demo.js") + "".join(_read(f"docs/{n}") for n in ("limits.md", "settings.md"))
    assert not re.search(r"\b(went out|gone out|go out|goes out|did not go out)\b", texts)
    assert "may already have been sent" in texts and "the email was not sent" in texts


def test_every_wording_of_the_hide_switch_names_request_numbers() -> None:
    """The one switch also hides request numbers, so each place that says what it hides must say so (a reader who sees
    dots where a request number was must be able to find out why)."""
    text = _portal_text()
    for anchor in (
        "On by default. Hides lead names",
        "Hiding names and email addresses on screen",
        "is shown in this table",
    ):
        i = text.index(anchor)
        assert "request number" in text[i : i + 260], anchor
