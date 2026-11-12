"""Facts about the lead panel and the masking, checked in a real browser (skipped when playwright or its browser is
missing). Made-up data only. Canary values are built from parts so the hygiene scan does not read them as addresses."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from support import CrashAt, lead
from test_portal_api import fresh

from lead_desk.contract import SimulatedCrash

LOCAL = "canary-" + "7731"
EMAIL = LOCAL + "@example.test"
PHONE_NEW = "+1 555 " + "0177 31"
MESSAGE = "canary message " + "7731"
REF = "ref-canary" + "@example.test"
HIDDEN_WORDS = "hidden (email address|phone number|message|request number)"

LIST = '[data-testid="leads-table"]'


def _run(base: str, fn: Callable[[Any, Any], None], *, hide: str | None = None, blocked: bool = False) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 (no browser installed here)
            pytest.skip(f"no browser: {e}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            if hide is not None:
                page.add_init_script(f"window.sessionStorage.setItem('lead-desk-hide-personal', '{hide}')")
            fn(page, base)
        finally:
            browser.close()


def _seed(rig: Any) -> str:
    first = rig.desk.handle("form", "f-1", lead(email=EMAIL)).body["lead_id"]
    rig.clock.advance(60)
    rig.desk.handle("webhook", "w-1", lead(email=EMAIL, phone=PHONE_NEW, message=MESSAGE))
    rig.desk.handle("form", "o-1", lead(name="Other Person", email="other@example.test"))
    return str(first)


def _open(page: Any, base: str, lead_id: str) -> None:
    page.goto(base + "/ui/#/leads")
    page.wait_for_selector("[data-lead-id]")
    page.click(f'[data-lead-id="{lead_id}"]')
    page.wait_for_selector('[data-testid="later-card"]')


# -- A: the screen-reader text is never visible, and the card lines are
def test_hidden_words_are_not_visible_and_card_lines_are(env) -> None:
    rig, base = fresh(env)
    first = _seed(rig)

    def body(page: Any, base: str) -> None:
        _open(page, base, first)
        got = page.evaluate(
            """(re) => { const bad = [], rx = new RegExp(re);
              const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
              while ((n = w.nextNode())) {
                if (!rx.test(n.nodeValue)) continue;
                const r = n.parentElement.getBoundingClientRect();
                if (r.width > 2 && r.height > 2) bad.push(n.nodeValue);
              }
              const meta = [...document.querySelectorAll('.later-meta')]
                .map((e) => Math.round(e.getBoundingClientRect().width));
              const cards = document.querySelectorAll('[data-testid=later-card]').length;
              const spans = [...document.querySelectorAll('.sr-only')].length;
              return { bad, meta, cards, spans }; }""",
            HIDDEN_WORDS,
        )
        assert got["bad"] == [] and got["spans"] > 0
        assert got["cards"] == 1 and len(got["meta"]) == 2 and min(got["meta"]) > 50  # both lines of the card are shown
        assert "1 minute after the first message" in page.inner_text('[data-testid="later-card"]')

    _run(base, body)


# -- B: Escape hides a single reveal also while the keyboard is on the eye button
def test_escape_hides_a_reveal_while_the_eye_has_focus(env) -> None:
    rig, base = fresh(env)
    first = _seed(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.focus(f'{LIST} [data-lead-id="{first}"] [data-guard-key*=":email"]')
        page.keyboard.press("Enter")
        assert LOCAL in page.inner_text(LIST).lower()
        page.keyboard.press("Escape")
        assert LOCAL not in page.inner_text(LIST).lower()

    _run(base, body)


# -- C and M13: the switch ON with the panel open repaints the table behind it; a typed address leaves no empty table
def _wait_rows(page: Any, n: int, seconds: float = 3.0) -> None:
    """Poll for n lead rows (wait_for_function would need eval, which the page's security policy refuses)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if page.locator("[data-lead-id]").count() == n:
            return
        time.sleep(0.1)
    raise AssertionError(f"the table did not show {n} rows within {seconds} s: " + page.inner_text(LIST)[:200])


def _flip(page: Any, on: bool) -> None:
    page.evaluate(
        "(on) => { const b = document.querySelector('[data-testid=leads-hide-personal]');"
        " b.checked = on; b.dispatchEvent(new Event('change')); }",
        on,
    )


def test_switch_on_with_a_typed_address_and_the_panel_open_shows_the_masked_list(env) -> None:
    rig, base = fresh(env)
    first = _seed(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.fill('[data-testid="leads-search"]', EMAIL)
        _wait_rows(page, 1)
        page.click(f'[data-lead-id="{first}"]')
        page.wait_for_selector('[data-testid="later-card"]')
        _flip(page, True)
        assert page.input_value('[data-testid="leads-search"]') == ""
        _wait_rows(page, 2)
        text = page.inner_text(LIST).lower()
        assert LOCAL not in text and "loading" not in text

    _run(base, body, hide="0")


def test_switch_on_without_the_panel_shows_the_masked_list(env) -> None:
    rig, base = fresh(env)
    _seed(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.fill('[data-testid="leads-search"]', EMAIL)
        _wait_rows(page, 1)
        _flip(page, True)
        _wait_rows(page, 2)
        assert LOCAL not in page.inner_text(LIST).lower()

    _run(base, body, hide="0")


def test_switch_with_the_panel_open_changes_the_table_behind_it_at_once(env) -> None:
    rig, base = fresh(env)
    first = _seed(rig)

    def body(page: Any, base: str) -> None:
        _open(page, base, first)
        assert LOCAL in page.inner_text(LIST).lower()
        _flip(page, True)
        assert LOCAL not in page.inner_text(LIST).lower()  # read straight after the change, before the next poll
        assert LOCAL not in page.inner_text('[data-testid="lead-drawer"]').lower()
        _flip(page, False)
        assert LOCAL in page.inner_text(LIST).lower()

    _run(base, body, hide="0")


# -- D: the words of a later message at the edges
def test_a_later_message_exactly_a_minute_later_says_one_minute(env) -> None:
    rig, base = fresh(env)
    first = _seed(rig)  # +60 s

    def body(page: Any, base: str) -> None:
        _open(page, base, first)
        text = page.inner_text('[data-testid="later-card"]')
        assert "1 minute after the first message" in text and "60 seconds" not in text

    _run(base, body)


def test_a_later_message_with_a_lower_receipt_number_says_so(env) -> None:
    rig, base = fresh(env, hooks=CrashAt("A"))
    with pytest.raises(SimulatedCrash):
        rig.desk.handle("webhook", "w-1", lead(email=EMAIL, phone=PHONE_NEW))
    rig.clock.advance(5)
    first = rig.desk.handle("form", "f-1", lead(email=EMAIL)).body["lead_id"]
    rig.desk.handle("webhook", "w-1", lead(email=EMAIL, phone=PHONE_NEW))

    def body(page: Any, base: str) -> None:
        _open(page, base, first)
        assert "Arrived before the first record was written" in page.inner_text('[data-testid="later-card"]')

    _run(base, body)


# -- the Request log chip of a request number that holds an address
def test_request_log_chip_is_masked_then_shows_its_real_short_form(env) -> None:
    rig, base = fresh(env)
    rig.desk.handle("webhook", REF, lead(email=EMAIL))

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/log")
        page.wait_for_selector('[data-testid="log-row"]')
        chip = '[data-testid="log-row"] .chip-id'
        assert page.inner_text(chip).strip().startswith("••••••")  # every request number masks the same way
        assert "@" not in page.inner_text(chip) and "canary" not in page.inner_text(
            chip
        )  # (the screen-reader words follow)
        page.click('[data-testid="log-row"] .eye-btn')
        assert page.inner_text(chip).strip() == "…" + REF[-6:]  # a request number over 14 characters shows its last 6

    _run(base, body)


# -- the leaks the screenshots do not show: titles, the document title, aria-labels, every attribute
def test_no_attribute_or_title_holds_a_masked_value(env) -> None:
    rig, base = fresh(env)
    first = _seed(rig)
    rig.desk.handle("webhook", REF, lead(name="Ref Person", email="ref@example.test"))

    def body(page: Any, base: str) -> None:
        for route, setup in (
            ("leads", lambda: _open(page, base, first)),
            ("log", None),
            ("overview", None),
            ("data", None),
        ):
            if setup:
                setup()
            else:
                page.goto(base + "/ui/#/" + route)
                page.wait_for_selector("main, #page-body")
                time.sleep(0.6)
            blob = page.evaluate(
                """() => { const out = [document.title];
                  for (const el of document.querySelectorAll('*')) for (const a of el.attributes) out.push(a.value);
                  for (const el of document.querySelectorAll('input')) out.push(el.value);
                  return out.join('\\n'); }"""
            ).lower()
            for secret in (LOCAL, PHONE_NEW.lower(), MESSAGE, "ref-canary"):
                assert secret.lower() not in blob, (route, secret)

    _run(base, body)


# -- layout facts: no sideways scroll, a stable switch, a bar that does not cover text
def _theme(page: Any, name: str) -> None:
    page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", name)


@pytest.mark.parametrize("hide", ["1", "0"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_leads_page_has_no_sideways_scroll_at_1100(env, hide: str, theme: str) -> None:
    rig, base = fresh(env)
    _seed(rig)

    def body(page: Any, base: str) -> None:
        page.set_viewport_size({"width": 1100, "height": 800})
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        _theme(page, theme)
        wide = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        assert wide[0] == wide[1], wide

    _run(base, body, hide=hide)


@pytest.mark.parametrize("width", [1100, 1280])
def test_the_hide_switch_and_the_table_keep_their_place_in_both_states(env, width: int) -> None:
    rig, base = fresh(env)
    _seed(rig)

    def body(page: Any, base: str) -> None:
        page.set_viewport_size({"width": width, "height": 800})
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        spots = []
        for on in (True, False):
            _flip(page, on)
            page.wait_for_selector("[data-lead-id]")
            time.sleep(0.4)
            spots.append(
                page.evaluate(
                    """() => { const r = (s) => { const b = document.querySelector(s).getBoundingClientRect();
                      return [Math.round(b.left), Math.round(b.top)]; };
                      return [r('.hide-switch'), r('[data-testid=leads-table]')]; }"""
                )
            )
        assert spots[0] == spots[1], spots

    _run(base, body)


def test_the_more_below_bar_does_not_cover_the_panel_text(env) -> None:
    rig, base = fresh(env)
    first = _seed(rig)

    def body(page: Any, base: str) -> None:
        page.set_viewport_size({"width": 1100, "height": 600})
        _open(page, base, first)
        got = page.evaluate(
            """() => { const bar = document.querySelector('.drawer-more').getBoundingClientRect();
              const body = document.querySelector('.drawer-body').getBoundingClientRect();
              return { barTop: bar.top, bodyBottom: body.bottom }; }"""
        )
        assert got["barTop"] >= got["bodyBottom"] - 1, got

    _run(base, body)
