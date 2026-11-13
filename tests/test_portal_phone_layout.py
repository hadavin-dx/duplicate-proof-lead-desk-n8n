"""At phone width (390 px) and at 700 px the portal keeps its icon-only rail and its page margin, no page is wider
than the window, and EVERY box of every page lies inside the window (a card, a tile, a search box, a text block: its
right edge is not beyond the window edge). The 860 px rules come before the 1200 px rules in app.css, so the later rules
used to win and the wide rail and margin stayed (the page then ran 60 to 240 px out of the window). The two size ranges
(up to 860 px, from 860.5 px; up to 1200 px, from 1200.5 px) leave no gap: this is tested at a width with a half pixel.
Real browser (skipped when playwright or its browser is missing). Made-up data only."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from test_portal_lead_pages import _browse, _flow_rig, _seed_names

FIX = Path(__file__).parent / "portal_fixtures"
JSON_HEAD = {"status": 200, "content_type": "application/json"}
PAGES = {
    "overview": '[data-testid="feed-item"]',
    "leads": "[data-lead-id]",
    "needs": '[data-testid="needs-card"]',
    "log": "table tbody tr",
    "data": "#main .card",
    "lead-panel": '[data-testid="lead-drawer"] .drawer-head h2',
}
# the page has finished drawing when the fonts are loaded and two animation frames have passed
SETTLED = """() => document.fonts.ready.then(() => new Promise(
  (done) => requestAnimationFrame(() => requestAnimationFrame(() => done(true)))))"""
# every box that has a size, except what lies inside a deliberate sideways scroller (the tabs bar, a wide table), the
# 1 px screen-reader-only texts and the skip link (parked off the window on purpose until it gets the focus): its right
# edge must not be beyond the window edge, its left edge not before it
BOXES = """() => {
  const vw = document.documentElement.clientWidth, out = [];
  let checked = 0;
  const inScroller = (el) => {
    for (let q = el.parentElement; q && q !== document.body; q = q.parentElement) {
      const o = getComputedStyle(q).overflowX;
      if (o === 'auto' || o === 'scroll') return true;
    }
    return false;
  };
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    const tiny = r.width <= 1 && r.height <= 1, inSvg = el.closest('svg') && el.tagName !== 'svg';
    if (tiny || el.classList.contains('skip') || inSvg || inScroller(el)) continue;
    checked += 1;
    if (r.right > vw + 0.5 || r.left < -0.5) {
      const name = el.tagName.toLowerCase() + '.' + String(el.className).split(' ')[0];
      out.push(name + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
    }
  }
  return { vw, checked, out: out.slice(0, 12), count: out.length,
           page: document.documentElement.scrollWidth, window: document.documentElement.clientWidth };
}"""


def _open(page: Any, base: str, name: str) -> None:
    if name == "needs":  # the cards of every kind, from the fixture
        text = (FIX / "review.json").read_text()
        page.route("**/api/ui/review", lambda r: r.fulfill(body=text, **JSON_HEAD))
    page.goto(base + f"/ui/#/{'leads' if name == 'lead-panel' else name}")
    page.wait_for_selector(PAGES["leads" if name == "lead-panel" else name])
    if name == "lead-panel":
        page.locator("[data-lead-id]").first.click()
        page.wait_for_selector(PAGES[name])
        page.wait_for_function(
            "() => Math.abs(document.querySelector('[data-testid=\"lead-drawer\"]').getBoundingClientRect().right"
            " - innerWidth) < 0.5"
        )  # the panel slides in from the right: it has stopped
    page.wait_for_function(SETTLED)


@pytest.mark.parametrize("width", [390, 700])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("page_name", list(PAGES))
def test_no_page_is_wider_than_a_phone_window_and_every_box_lies_inside_it(
    env: Any, scheme: str, width: int, page_name: str
) -> None:
    rig, base = _flow_rig(env)
    _seed_names(rig)

    def body(page: Any, base: str) -> None:
        _open(page, base, page_name)
        m = page.evaluate(BOXES)
        assert m["page"] <= m["window"], m  # no sideways scroll of the page (a wide table scrolls inside its card)
        assert m["checked"] >= 10, m  # the check looked at something
        assert not m["out"], m  # and no box (a card, a tile, a search box) lies beyond a window edge

    _browse(base, body, width=width, scheme=scheme)


@pytest.mark.parametrize("width, rail", [(390, 64), (700, 64), (860, 64), (861, 208), (1200, 208), (1201, 240)])
def test_the_rail_is_icon_only_up_to_860_px_and_the_page_margin_follows(env: Any, width: int, rail: int) -> None:
    rig, base = _flow_rig(env)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector("#main")
        m = page.evaluate(
            """() => ({ rail: parseFloat(getComputedStyle(document.querySelector('.shell')).gridTemplateColumns),
              margin: parseFloat(getComputedStyle(document.querySelector('.page')).paddingLeft) })"""
        )
        assert m["rail"] == rail, m
        if width <= 860:
            assert m["margin"] == 16, m  # the 860 px page margin (16 px), not the 24 px of the 1200 px rule

    _browse(base, body, width=width)


@pytest.mark.parametrize("width, rail", [(860.5, 208), (1200.5, 240)])
def test_a_window_with_a_half_pixel_falls_into_a_size_range_too(env: Any, width: float, rail: int) -> None:
    """A page cannot be 860.5 px wide in a browser window, but a frame can: its media queries use its own width.
    (Ranges written as min-width 861 and max-width 860 left such a width in neither block: the wide rail won.)"""
    rig, base = _flow_rig(env)

    def body(page: Any, base: str) -> None:
        def no_embed_ban(
            route: Any,
        ) -> None:  # the desk forbids frames (CSP); this test page is the only one that allows it
            headers = {
                k: v
                for k, v in route.fetch().headers.items()
                if k not in ("content-security-policy", "x-frame-options")
            }
            route.fulfill(response=route.fetch(), headers=headers)

        page.route("**/ui/", no_embed_ban)
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector("#main")
        page.evaluate(
            """(w) => { const f = document.createElement('iframe');
              f.style.cssText = `position:fixed;top:0;left:0;height:800px;width:${w}px;border:0`;
              f.src = '/ui/#/leads'; document.body.appendChild(f); return new Promise((r) => { f.onload = r; }); }""",
            width,
        )
        frame = page.frames[-1]
        frame.wait_for_selector("#main")
        m = frame.evaluate(
            """() => ({ rail: parseFloat(getComputedStyle(document.querySelector('.shell')).gridTemplateColumns),
              wide: matchMedia('(width > 860px)').matches, narrow: matchMedia('(max-width: 860px)').matches })"""
        )
        assert m["rail"] == rail, m
        assert m["wide"] != m["narrow"], m  # the two ranges are complements: exactly one holds

    _browse(base, body, width=1400)


def test_the_tabs_do_not_wrap_and_nothing_of_the_active_tab_is_clipped_at_390_px(env: Any) -> None:
    rig, base = _flow_rig(env)

    def body(page: Any, base: str) -> None:
        _open(page, base, "needs")
        page.keyboard.press("Tab")  # the next focus counts as a keyboard focus (a visible ring)
        page.evaluate("() => document.querySelector('.tabs .tab').focus()")
        m = page.evaluate(
            """() => {
              const tabs = document.querySelector('.tabs'), first = document.querySelector('.tabs .tab');
              const t = tabs.getBoundingClientRect(), r = first.getBoundingClientRect(), cs = getComputedStyle(first);
              const grow = Math.max(0, parseFloat(cs.outlineOffset) + parseFloat(cs.outlineWidth));
              const glow = (cs.boxShadow.match(/(\\d+)px\\s*$/) || [0, 0])[1];  // the spread of the ring's glow
              const halo = Number(glow);
              const room = parseFloat(getComputedStyle(tabs).paddingBottom);
              return { height: r.height,
                       wrap: cs.whiteSpace, ringTop: r.top - grow - halo - t.top,
                       ringLeft: r.left - grow - halo - t.left,
                       ringBottom: t.bottom - 1 - (r.bottom + grow + halo), underlineRoom: room };
            }"""
        )
        assert m["wrap"] == "nowrap" and m["height"] < 60, m  # the first tab is not three lines high
        assert m["ringTop"] >= 0 and m["ringLeft"] >= 0, m  # the focus ring is not cut off at the top or the left
        assert m["underlineRoom"] >= 1, (
            m
        )  # the 2 px underline of the active tab reaches 1 px below the tab: room for it

    _browse(base, body, width=390)


def test_no_size_range_starts_one_pixel_after_another_one_ends() -> None:
    """`(min-width: 861px)` next to `(max-width: 860px)` leaves the widths between 860 and 861 px in neither block (a
    window with a half pixel, for example by zoom: Chromium rounds the window width, other engines may not). A range
    from the next pixel is written `(width > 860px)` instead. No browser needed: the style sheets are read as text."""
    css = "\n".join(p.read_text(encoding="utf-8") for p in sorted((Path(__file__).parents[1] / "src").rglob("*.css")))
    css += (Path(__file__).parents[1] / "demo_only" / "portal_demo.css").read_text(encoding="utf-8")
    queries = re.findall(r"@media[^{]*", css)
    upper = {int(n) for q in queries for n in re.findall(r"max-width: *(\d+)px", q)}
    lower = {int(n) for q in queries for n in re.findall(r"min-width: *(\d+)px", q)}
    assert upper, "no media queries found: the check read nothing"
    assert not [n for n in lower if n - 1 in upper], sorted(n for n in lower if n - 1 in upper)
