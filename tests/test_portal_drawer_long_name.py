"""The lead panel's header keeps its title, status chip and close button on screen when a name is one long unbroken
string (a real browser; skipped when playwright or its browser is missing). Made-up data only."""

from __future__ import annotations

from typing import Any

import pytest
from test_portal_lead_pages import _browse, _flow_rig, _new

LONG_NAME = "W" * 200  # no space or hyphen anywhere: the browser has no natural place to break it
HEAD = '[data-testid="lead-drawer"] .drawer-head'
MEASURE = """() => {
  const rect = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.right, r.width, r.top]; };
  const head = document.querySelector('[data-testid="lead-drawer"] .drawer-head');
  return {
    scroll: head.scrollWidth, client: head.clientWidth, vw: innerWidth,
    box: rect(document.querySelector('[data-testid="lead-drawer"]')),
    h2: rect(head.querySelector('h2')), pill: rect(head.querySelector('.pill')),
    close: rect(document.querySelector('[data-testid="drawer-close"]')),
  };
}"""
# the panel slides in from the right: it has stopped when its right edge is the window edge
STOPPED = """() => { const r = document.querySelector('[data-testid="lead-drawer"]').getBoundingClientRect();
  return Math.abs(r.right - innerWidth) < 0.5; }"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1100, 390])
def test_a_long_unbroken_name_stays_inside_the_lead_panel_header(env: Any, scheme: str, width: int) -> None:
    rig, base = _flow_rig(env)
    lead_id = _new(rig, "long-1", LONG_NAME, "long@example.test")

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.locator('[data-testid="leads-hide-personal"]').uncheck()  # names shown: the long text is on screen
        page.click(f'[data-lead-id="{lead_id}"]')
        page.wait_for_selector(HEAD + " h2")
        assert LONG_NAME in page.inner_text(HEAD + " h2")
        page.wait_for_function(STOPPED)
        m = page.evaluate(MEASURE)
        assert m["scroll"] <= m["client"] + 1, m  # the header does not run out of its own box
        assert m["close"][2] >= 35, m  # the close button keeps its size (a squeezed button is 32 wide or less)
        left, right = m["box"][0] - 1, min(m["box"][1], m["vw"]) + 1
        for part in ("h2", "pill", "close"):  # every part lies inside the panel and the window (left side too)
            assert left <= m[part][0] and m[part][1] <= right, (part, m)
        first_line = m["h2"][3] + 36  # a title of many lines: the chip and the close button stay beside its first line
        assert m["pill"][3] < first_line and m["close"][3] < first_line, m
        page.click('[data-testid="drawer-close"]')  # and it can be clicked
        page.wait_for_selector('[data-testid="lead-drawer"]', state="detached")

    _browse(base, body, width=width, scheme=scheme)
