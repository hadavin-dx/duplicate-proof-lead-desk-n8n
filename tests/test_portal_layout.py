"""Layout and masking facts of the portal, checked in a real browser (skipped
when playwright or its browser is missing). Made-up data only; long names are built so the layout is stressed."""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Any
from urllib.parse import quote

import pytest
from support import DEMO_LEADFLOW, REPO, lead, lead_ids_containing
from test_portal_api import fresh, get
from test_portal_lead_pages import _browse, _seed_names

from lead_desk import server

LONG_NAME = "Alexandria Montgomery-Featherstone"
LONG_COMPANY = "Analytical Engines Consolidated"
STATE = json.loads((REPO / "tests" / "portal_fixtures" / "demo-state.json").read_text(encoding="utf-8"))


def _flow_rig(env: Any, **over: Any) -> tuple[Any, str]:
    flow = copy.deepcopy(DEMO_LEADFLOW)
    flow["nudge_after_seconds"] = None
    return fresh(env, leadflow=flow, **over)


def _seed_long(rig: Any) -> list[str]:
    ids = []
    for i, nm in enumerate((LONG_NAME, "Grace Sample-Hopper", "Linus Demo")):
        email = f"{nm.split()[0].lower()}.extra.long.address{i}@example.test"
        ids.append(
            str(
                rig.desk.handle(
                    "form", f"s-{i + 7:04d}", lead(name=nm, email=email, company=LONG_COMPANY, phone="555 0100")
                ).body["lead_id"]
            )
        )
    rig.clock.advance(5)
    rig.desk.handle(
        "webhook",
        "s-0099",
        lead(name=LONG_NAME, email="alexandria.extra.long.address0@example.test", phone="555 0199", message="later"),
    )
    return ids


def _set(page: Any, theme: str, hide: str) -> None:
    page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", theme)


def _no_sideways(page: Any) -> list[int]:
    return page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")  # type: ignore[no-any-return]


@pytest.mark.parametrize("width", [1100, 1366])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_no_sideways_scroll_revealed_leads_search_and_drawers(env: Any, theme: str, width: int) -> None:
    rig, base = _flow_rig(env)
    ids = _seed_long(rig)
    init = "window.sessionStorage.setItem('lead-desk-hide-personal', '0')"

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        _set(page, theme, "0")
        page.wait_for_timeout(300)
        assert (w := _no_sideways(page))[0] <= w[1], ("leads", w)
        heads = page.evaluate(
            "() => Object.fromEntries([...document.querySelectorAll('.leads-out thead th')]"
            ".map(x => [x.textContent.trim(), getComputedStyle(x).display]))"
        )
        hidden = "none" if width <= 1300 else "table-cell"  # both fall away only up to 1300 px: the room is used
        assert heads["Requests"] == hidden and heads["Source"] == hidden, heads
        page.fill('[data-testid="leads-search"]', "Alexandria")
        page.wait_for_timeout(700)
        assert (w := _no_sideways(page))[0] <= w[1], ("search", w)
        page.fill('[data-testid="leads-search"]', "")
        page.wait_for_timeout(700)
        for lid in ids:
            page.click(f'[data-lead-id="{lid}"]')
            page.wait_for_selector('[data-testid="lead-drawer"]')
            page.wait_for_timeout(500)
            assert (w := _no_sideways(page))[0] <= w[1], ("drawer", lid, w)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

    _browse(base, body, width=width, init=init, scheme=theme)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_requests_column_is_back_at_1440(env: Any, theme: str) -> None:
    rig, base = _flow_rig(env)
    _seed_long(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        _set(page, theme, "1")
        shown = page.evaluate(
            "() => [...document.querySelectorAll('.leads-out thead th')].filter("
            "x => getComputedStyle(x).display !== 'none').map(x => x.textContent.trim())"
        )
        assert "Requests" in shown and "Source" in shown, shown

    _browse(base, body, width=1440, scheme=theme)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_masked_email_and_last_change_stay_on_one_line(env: Any, theme: str) -> None:
    rig, base = _flow_rig(env)
    _seed_long(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        _set(page, theme, "1")
        got = page.evaluate(
            """() => { const rows = [...document.querySelectorAll('[data-testid=lead-row]')];
              const h = (e) => Math.round(e.getBoundingClientRect().height);
              const mail = rows.map(r => r.querySelector('.lead-mail')).filter(Boolean).map(h);
              const last = rows.map(r => r.querySelector('td:nth-last-child(1) time, td:nth-last-child(1)')).map(h);
              return { mail, last, line: parseFloat(getComputedStyle(document.body).lineHeight) }; }"""
        )
        assert got["mail"] and max(got["mail"]) <= got["line"] * 1.6, got  # one line, not three
        assert max(got["last"]) <= got["line"] * 2.6 + 24, got

    _browse(base, body, width=1100, scheme=theme)


class _DemoStub:
    """The real demo extension files, with a recorded state: what the overview strip draws, without a fakes server."""

    def __init__(self, sent: int = 3, reminders: int = 4) -> None:
        from demo_only import portal_demo

        self.state = copy.deepcopy(STATE)
        self.state["fakes"]["outbox"].update(count=sent + reminders, followups=sent, reminders=reminders)

        self.assets = {
            "portal_demo.js": ((portal_demo.HERE / "portal_demo.js").read_bytes(), "text/javascript; charset=utf-8"),
            "portal_demo.css": ((portal_demo.HERE / "portal_demo.css").read_bytes(), "text/css; charset=utf-8"),
        }

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, Any]:
        return (200, self.state) if (method, path) == ("GET", "state") else (404, {"error": "not_found"})


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1100, 1280, 1920])
def test_overview_proof_strip_layout(env: Any, monkeypatch: pytest.MonkeyPatch, theme: str, width: int) -> None:
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: _DemoStub())
    rig, base = _flow_rig(env)
    _seed_long(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector('[data-testid="proof"]')
        _set(page, theme, "1")
        time.sleep(0.4)
        got = page.evaluate(
            """() => { const p = document.querySelector('[data-testid=proof]');
              const box = (e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; };
              const figs = [...p.querySelectorAll('.dm-figure')].map(box);
              const labels = [...p.querySelectorAll('.dm-figure-label')].map(e => e.getBoundingClientRect().height);
              const pill = p.querySelector('.pill'); const lh = parseFloat(getComputedStyle(p).lineHeight) || 20;
              return { p: box(p), figs, labels, pill: pill ? box(pill) : null, h: p.getBoundingClientRect().height,
                       w: [document.documentElement.scrollWidth, document.documentElement.clientWidth], lh }; }"""
        )
        shot = os.environ.get("UI4_SHOT_DIR")
        if shot:
            page.screenshot(path=f"{shot}/proof-{theme}-{width}.png")
        assert got["w"][0] <= got["w"][1], got
        assert len(got["figs"]) == 2 and got["pill"], got

        def hit(a: list[float], b: list[float]) -> bool:
            return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

        if got["pill"]:
            assert not any(hit(got["pill"], f) for f in got["figs"]), got  # the pill never covers a figure
        assert max(got["labels"]) <= 3 * 20, got  # a label is at most 3 lines, not one word per line
        if width >= 1280:
            assert got["h"] <= 170, got  # one calm row, not a 230 px tall block

    _browse(base, body, width=width, scheme=theme)


@pytest.mark.parametrize(
    "probe", ["Alexandria", "alexandria", "s-0007", "s-00", "extra.long", "example.test", "Ada Sa", "ada.sample"]
)
def test_masked_search_matches_no_name_email_or_request_number(
    env: Any, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    """The probes are text a lead number (the letter L and 12 hex characters) cannot spell by chance; and every lead
    number here is forced to hold "ada" and "0007", so the test would catch a random draw that does."""
    lead_ids_containing(monkeypatch, "ada0007")
    rig, base = _flow_rig(env)
    _seed_long(rig)
    rig.desk.handle("form", "s-ada", lead(name="Ada Sample", email="ada.sample@example.test"))  # a name-derived number
    assert get(base, f"/api/ui/leads?q={quote(probe)}&by_email=0")[1]["total"] == 0, probe
    if probe in {"s-0007", "Alexandria", "Ada Sa"}:  # and the same words do find the lead when the switch is off
        assert get(base, f"/api/ui/leads?q={quote(probe)}&by_email=1")[1]["total"] >= 1
    assert get(base, "/api/ui/leads?q=s-ada&by_email=0")[1]["total"] == 0


def test_masked_search_finds_a_lead_number_by_design(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead number is searchable while names are masked (the box says "Company or lead number"): a probe that
    sits inside a lead number finds that lead by its number, never by its name."""
    lead_ids_containing(monkeypatch, "ada0007")
    rig, base = _flow_rig(env)
    _seed_long(rig)
    everyone = get(base, "/api/ui/leads?by_email=0")[1]["total"]
    assert everyone >= 3
    assert get(base, "/api/ui/leads?q=ada&by_email=0")[1]["total"] == everyone  # every number holds ada
    assert get(base, "/api/ui/leads?q=0007&by_email=0")[1]["total"] == everyone


def test_demo_data_holds_no_name_derived_request_number() -> None:
    """The demo's own request numbers are opaque (ui-<epoch>-<n>, or burst-<n>): never a name or an address."""
    import re

    from demo_only import portal_demo

    assert re.fullmatch(portal_demo.REF_PREFIX_RE.pattern, "ui-1730000000000")
    assert not re.search(r"ada|grace|linus", portal_demo.REF_AUTO_PREFIX, re.I)


_CONTRAST_JS = r"""() => {
  const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const over = (f, b) => [0, 1, 2].map(i => f[i] * f[3] + b[i] * (1 - f[3]));
  const lum = (c) => { const t = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * t[0] + 0.7152 * t[1] + 0.0722 * t[2]; };
  const bgOf = (el) => { let stack = []; for (let e = el; e; e = e.parentElement) { const s = getComputedStyle(e);
      const c = parse(s.backgroundColor); if (c && c[3] > 0) { stack.push(c); if (c[3] === 1) break; } }
    let b = [255, 255, 255]; if (getComputedStyle(document.documentElement).colorScheme.includes('dark') || document.documentElement.dataset.theme === 'dark') b = [9, 13, 22];
    for (let i = stack.length - 1; i >= 0; i--) b = over(stack[i], b); return b; };
  const out = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
  while ((n = w.nextNode())) { const t = n.nodeValue.trim(); if (!t) continue; const el = n.parentElement;
    const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2 || el.closest('.sr-only, svg, [disabled], h1')) continue;
    const s = getComputedStyle(el); let op = 1; for (let e = el; e; e = e.parentElement) op *= parseFloat(getComputedStyle(e).opacity);
    const bg = bgOf(el); const fg = parse(s.color); if (!fg) continue; const f = over([fg[0], fg[1], fg[2], fg[3] * op], bg);
    const L1 = lum(f), L2 = lum(bg); const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    out.push({ t: t.slice(0, 40), ratio: Math.round(ratio * 100) / 100, size: parseFloat(s.fontSize), cls: el.className.toString().slice(0, 30) }); }
  return out; }"""  # noqa: E501 (one JavaScript block)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("route", ["overview", "leads", "needs", "log", "data"])
def test_every_visible_text_has_aa_contrast(env: Any, route: str, theme: str) -> None:
    rig, base = _flow_rig(env)
    ids = _seed_long(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + f"/ui/#/{route}")
        page.wait_for_timeout(900)
        _set(page, theme, "0")
        page.wait_for_timeout(300)
        found = page.evaluate(_CONTRAST_JS)
        if route == "leads":
            page.click(f'[data-lead-id="{ids[0]}"]')
            page.wait_for_selector('[data-testid="lead-drawer"]')
            page.wait_for_timeout(500)
            found += page.evaluate(_CONTRAST_JS)
        low = [f for f in found if f["ratio"] < 4.5]
        assert not low, low[:12]

    _browse(base, body, width=1280, scheme=theme, init="window.sessionStorage.setItem('lead-desk-hide-personal', '0')")


@pytest.mark.parametrize("hide", ["1", "0"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_needs_cards_with_their_lead_number_have_aa_contrast(env: Any, theme: str, hide: str) -> None:
    """The small lead number on a Needs card (the way to tell two masked cards apart) and the card's other texts."""
    rig, base = _flow_rig(env)
    _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/needs")
        page.wait_for_selector('[data-testid="need-lead-number"]')
        _set(page, theme, hide)
        # (a button on a gradient has no single background colour: its white text is checked against every stop below)
        low = [f for f in page.evaluate(_CONTRAST_JS) if f["ratio"] < 4.5 and "btn-primary" not in f["cls"]]
        assert not low, low[:12]
        stops = page.evaluate(
            "() => [...document.querySelectorAll('.btn-primary:not([disabled])')]"
            r".flatMap((b) => getComputedStyle(b).backgroundImage.match(/rgb\([^)]*\)/g) || [])"
        )
        assert stops, "the primary button has no gradient stops to measure"
        for stop in stops:
            channels = [int(v) / 255 for v in stop[4:-1].split(",")[:3]]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
            assert 1.05 / (lum + 0.05) >= 4.5, (stop, "white text on this stop is below 4.5:1")

    _browse(
        base, body, width=1280, scheme=theme, init=f"window.sessionStorage.setItem('lead-desk-hide-personal', '{hide}')"
    )


# -- the eye button never covers the text it belongs to, masked or shown (a long name or address is cut by an ellipsis
# INSIDE its own span; the button sits after it, outside the cut)
UNBROKEN_NAME = "Wolfeschlegelsteinhausenbergerdorff" * 2  # 70 characters, no space to break at

_BUTTON_FACTS_JS = """() => [...document.querySelectorAll('[data-testid=lead-row] .lead-cell .guard')].map((g) => {
  const val = g.querySelector('.guard-val'), v = val.getBoundingClientRect();
  const b = g.querySelector('.eye-btn').getBoundingClientRect();
  const td = g.closest('td').getBoundingClientRect();
  const at = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
  return { text: val.textContent.length, gap: Math.round((b.left - v.right) * 10) / 10,
           inside: b.right <= td.right + 0.5, clickable: !!at && !!at.closest('.eye-btn'),
           cut: val.scrollWidth > val.clientWidth + 1,
  // text wider than its box must be clipped by it: left visible it is painted UNDER the button
  painted_under: val.scrollWidth > val.clientWidth + 1 && getComputedStyle(val).overflowX === 'visible',
  // the layout rules that keep the clip inside the value (leads.css): the guard is a flex row with a gap, and the cell
  // that holds it does not clip (the value does): measured, so deleting a rule shows here even when the pixels agree
  guard_display: getComputedStyle(g).display, guard_min_width: getComputedStyle(g).minWidth,
  host_display: getComputedStyle(g.parentElement).display,
  host_overflow: getComputedStyle(g.parentElement).overflowX }; })"""


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1100, 1280, 1300])
def test_masked_values_and_their_eye_buttons_do_not_overlap(env: Any, theme: str, width: int) -> None:
    rig, base = _flow_rig(env)
    _seed_long(rig)
    rig.desk.handle("form", "s-0300", lead(name=UNBROKEN_NAME, email="unbroken." + "x" * 60 + "@example.test"))

    def check(page: Any, where: str) -> list[dict[str, Any]]:
        got = page.evaluate(_BUTTON_FACTS_JS)
        assert len(got) >= 8, (where, got)  # a name and an address in each of four rows
        assert all(g["gap"] >= 0 for g in got), (where, got)  # the button starts after the text ends
        assert all(g["inside"] and g["clickable"] for g in got), (where, got)  # and is neither clipped nor covered
        assert not any(g["painted_under"] for g in got), (where, got)  # and no text runs on beneath it
        # the rules behind that, read from the computed layout (a deleted rule in leads.css fails here)
        assert all(g["guard_display"] == "flex" and g["guard_min_width"] == "0px" for g in got), (where, got)
        assert all(g["host_display"] == "flex" and g["host_overflow"] == "visible" for g in got), (where, got)
        return list(got)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id] .lead-cell .eye-btn")
        _set(page, theme, "1")
        check(page, "masked")
        # every value shown on purpose: the long ones are cut by their own ellipsis, and the button still sits clear
        # of the text, inside the cell, and can be clicked
        page.evaluate(
            "() => document.querySelectorAll('.lead-cell [data-guard-key]').forEach("
            "(b) => Portal.reveal.toggle(b.getAttribute('data-guard-key')))"
        )
        page.wait_for_function("() => document.querySelectorAll('.lead-cell .guard .is-masked').length === 0")
        shown = check(page, "shown")
        assert any(g["cut"] and g["text"] > 40 for g in shown), shown  # the very long unbroken values were really cut

    _browse(base, body, width=width, scheme=theme)


REF_NAME_LIKE = "ada-lovelace-42"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_name_like_request_number_is_never_in_a_masked_page_and_its_eye_shows_it(env: Any, theme: str) -> None:
    """A request number is data the sender chose, so it is masked like a name on EVERY page and panel (Overview, Leads
    table and drawer, later-message card, Needs, Request log, Data); the page text and the markup (tooltips, labels,
    row attributes) hold no part of it. One eye shows exactly ONE request number (the others stay masked), in the
    drawer ledger, in a later-message card and in the Request log."""
    rig, base = _flow_rig(env)
    lid = str(
        rig.desk.handle("form", REF_NAME_LIKE, lead(name="Ada Sample", email="ada.sample@example.test")).body["lead_id"]
    )
    rig.clock.advance(5)
    rig.desk.handle(
        "webhook",
        REF_NAME_LIKE + "-b",
        lead(name="Ada Sample", email="ada.sample@example.test", phone="555 0199", message="later"),
    )
    rig.clock.advance(5)
    rig.desk.handle(
        "form", REF_NAME_LIKE + "-r", {"name": "Ada Sample"}
    )  # no address: a request for a person to look at
    DOTS = "•" * 6

    def clean(page: Any, where: str) -> None:
        html = page.content().lower()
        text = page.inner_text("body").lower()
        assert "lovelace" not in html and "lovelace" not in text, where

    def body(page: Any, base: str) -> None:
        # every page, masked: nothing of the request number anywhere in the text or the markup
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector('[data-testid="feed-item"]')
        clean(page, "overview")
        assert any(DOTS in (t or "") for t in page.eval_on_selector_all(".ov-text", "(els) => els.map((e) => e.title)"))
        page.goto(base + "/ui/#/needs")
        page.wait_for_selector('[data-testid="needs-card"]')
        clean(page, "needs")
        page.goto(base + "/ui/#/data")
        page.wait_for_selector('[data-testid="data-hide-chip"]')
        clean(page, "data")
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector(f'[data-lead-id="{lid}"]')
        clean(page, "leads table")
        page.click(f'[data-lead-id="{lid}"]')
        page.wait_for_selector('[data-testid="proof-ledger"]')
        page.wait_for_selector('[data-testid="later-card"]')
        clean(page, "lead drawer")
        ledger = page.locator('[data-testid="proof-ledger"] .ledger-step').filter(has_text="Request number")
        later = page.locator('[data-testid="later-card"] .later-meta').first
        assert DOTS in ledger.inner_text() and DOTS in later.inner_text()  # both masked
        # ONE eye in the drawer ledger: that request number shows (short form), the later message's stays masked
        ledger.locator(".eye-btn").click()
        ledger.filter(has_text="ace-42").wait_for()
        assert DOTS in later.inner_text() and "e-42-b" not in later.inner_text()
        # ONE eye in the later-message card: that card shows its own short form
        later.locator(".eye-btn").click()
        later.filter(has_text="e-42-b").wait_for()
        assert "lovelace" not in page.content().lower()  # the short form only (an ellipsis and the last six characters)
        page.keyboard.press("Escape")  # hides every single reveal
        page.goto(base + "/ui/#/log")
        page.wait_for_selector(".log-req .eye-btn")
        clean(page, "request log")
        assert page.locator(".log-req .eye-btn").count() == 3
        oldest = page.locator(".log-req").last  # newest first: the first request sent is the last row
        newest = page.locator(".log-req").first
        oldest.locator(".eye-btn").click()  # one value shown on purpose
        oldest.filter(has_text="ace-42").wait_for()
        assert DOTS in newest.inner_text() and "e-42-r" not in newest.inner_text()  # the others stay masked
        assert "lovelace" not in page.content().lower()

    _browse(base, body, width=1280, scheme=theme)


def test_request_number_masking_is_in_the_shared_rule_not_in_one_page(env: Any) -> None:
    """The rule lives in the shared code every page calls (Portal.shownRef, shortRef, requestRef), so no page can
    forget it: while the switch is on EVERY request number (plain, address-like, long) is six dots; with it off the
    number shows as it is, or as an ellipsis and its last six characters."""
    rig, base = _flow_rig(env)
    dots = "•" * 6
    long_ref = "x" * 30

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/overview")
        page.wait_for_function("() => window.Portal && Portal.shownRef && Portal.ui && Portal.ui.requestRef")
        probe = (
            "() => ['ui-1', 'ada-lovelace-42', 'a@example.test', 'x'.repeat(30)].map((r) => Portal.shownRef(r))"
            ".concat([Portal.ui.shortRef('x'.repeat(30)), Portal.ui.requestRef('ada-lovelace-42')])"
        )
        page.evaluate("() => Portal.privacy.set(true)")
        assert page.evaluate(probe) == [dots] * 5 + ["Request " + dots]
        page.evaluate("() => Portal.privacy.set(false)")
        assert page.evaluate(probe) == [
            "ui-1",
            "ada-lovelace-42",
            "a@example.test",
            long_ref,
            "…xxxxxx",
            "Request …ace-42",
        ]

    _browse(base, body, width=1280)


def test_the_contract_names_the_width_the_leads_table_drops_columns_at() -> None:
    """The width in contract.md is the width in leads.css (a stale number there once told readers 1280)."""
    import re

    css = (REPO / "src" / "lead_desk" / "portal" / "pages" / "leads.css").read_text(encoding="utf-8")
    contract = (REPO / "contract.md").read_text(encoding="utf-8")
    in_css = re.search(r"@media \(max-width: (\d+)px\) \{\s*\.leads-out \.table th:nth-child\(3\)", css)
    in_contract = re.search(r"At (\d+) px and narrower the Leads table hides Source and Requests", contract)
    assert in_css and in_contract and in_css.group(1) == in_contract.group(1), (in_css, in_contract)
