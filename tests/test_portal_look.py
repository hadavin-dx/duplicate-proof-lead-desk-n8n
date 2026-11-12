"""Facts about the look of the portal, measured in a real browser from computed styles (skipped when playwright or its
browser is missing): one colour per follow-up state, a visible gap between ring slices, cards of one row as high as
each other, and text contrast. Made-up data only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_portal_api import fresh
from test_portal_browser import _run, _seed
from test_portal_browser import _theme as _set_theme

FIX = Path(__file__).parent / "portal_fixtures"
STATES = ["sent", "pending", "sending", "failed", "did_not_finish", "unknown", "crm_unconfirmed"]


def _theme(page: Any, name: str) -> None:
    _set_theme(page, name)
    page.wait_for_timeout(450)  # colours fade over 160 ms: measure after the fade, not in it


THEMES = ["light", "dark"]
JSON_HEAD = {"status": 200, "content_type": "application/json"}

# contrast ratio (WCAG 2) of the visible text in a scope, from computed styles
CONTRAST_JS = (Path(__file__).parent / "portal_contrast_probe.js").read_text(encoding="utf-8")


def _json(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())  # type: ignore[no-any-return]


def _serve_json(page: Any, pattern: str, body: Any) -> None:
    text = json.dumps(body)
    page.route(pattern, lambda r: r.fulfill(body=text, **JSON_HEAD))


def _low(page: Any, root: str | None = None, floor: float = 4.5) -> list[Any]:
    return [x for x in page.evaluate(CONTRAST_JS, root) if x["ratio"] < floor]


def _summary_with_every_state() -> dict[str, Any]:
    d = _json("summary.json")
    d["followups"] = {
        "sent": 12,
        "pending": 4,
        "sending": 3,
        "failed": 2,
        "did_not_finish": 3,
        "unknown": 3,
        "crm_unconfirmed": 3,
    }
    return d


def _detail_in_state(state: str) -> dict[str, Any]:
    d = _json("lead-detail.json")
    d["state_view"] = state
    d["followup"]["state"] = {"did_not_finish": "failed", "crm_unconfirmed": "pending"}.get(state, state)
    return d


# -- one colour per state: ring slice, legend dot, timeline dot and badge say the same thing
@pytest.mark.parametrize("theme", THEMES)
def test_one_colour_per_follow_up_state_in_ring_legend_timeline_and_badge(env, theme: str) -> None:
    rig, base = fresh(env)
    first = _seed(rig)

    def body(page: Any, base: str) -> None:
        _serve_json(page, "**/api/ui/summary*", _summary_with_every_state())
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector('[data-testid="followups-bar"] .legend li')
        _theme(page, theme)
        ring = page.evaluate(
            """() => {
              const arcs = [...document.querySelectorAll('[data-testid=followups-bar] circle:not(.arc-track)')]
                .map((c) => getComputedStyle(c).stroke);
              const legend = [...document.querySelectorAll('[data-testid=followups-bar] .legend li')].map((li) => ({
                label: li.querySelector('.legend-label').textContent,
                dot: getComputedStyle(li.querySelector('.sw')).backgroundColor,
                tone: [...li.querySelector('.sw').classList].find((c) => c.startsWith('sw-') && c !== 'sw')
                  .slice(3) }));
              return { arcs, legend }; }"""
        )
        names = {
            "Sent": "sent",
            "Queued": "pending",
            "Sending": "sending",
            "Failed": "failed",
            "Did not finish": "did_not_finish",
            "Outcome unknown": "unknown",
            "CRM not confirmed": "crm_unconfirmed",
        }
        legend = {names[x["label"]]: x for x in ring["legend"]}
        assert sorted(legend) == sorted(STATES), ring["legend"]
        arc = {names[x["label"]]: ring["arcs"][i] for i, x in enumerate(ring["legend"])}
        for s in STATES:  # the ring slice and its legend dot are the same colour
            assert arc[s].replace("rgb", "") == legend[s]["dot"].replace("rgb", ""), (s, arc[s], legend[s])
        assert len({legend[s]["dot"] for s in STATES}) == len(STATES), "two states share one colour"

        badge = page.evaluate(
            """(states) => states.map((s) => { const el = window.Portal.ui.pill(s); document.body.appendChild(el);
              const cs = getComputedStyle(el); const r = { s, bg: cs.backgroundColor, cls: el.className };
              el.remove(); return r; })""",
            STATES,
        )
        for b in badge:  # the badge carries the tone of its state, and no two states share a badge colour
            assert "pill-" + legend[b["s"]]["tone"] in b["cls"].split(), (b, legend[b["s"]])
        assert len({b["bg"] for b in badge}) == len(STATES), badge

        dots = {}
        for s in STATES:
            _serve_json(page, f"**/api/ui/leads/{first}*", _detail_in_state(s))
            page.goto(base + "/ui/#/leads")
            page.wait_for_selector(f'[data-lead-id="{first}"]')
            page.click(f'[data-lead-id="{first}"]')
            _theme(page, theme)
            page.wait_for_selector('[data-testid="timeline"] li')
            dots[s] = page.evaluate(
                """() => { const li = [...document.querySelectorAll('[data-testid=timeline] li')].pop();
                  return getComputedStyle(li, '::before').backgroundColor; }"""
            )
            page.reload()
            page.unroute(f"**/api/ui/leads/{first}*")
        for s in STATES:
            assert dots[s] == legend[s]["dot"], (s, dots[s], legend[s]["dot"])

    _run(base, body)


# -- the ring: a visible gap between two slices (the red and the orange slice must not touch)
@pytest.mark.parametrize("theme", THEMES)
def test_ring_slices_have_a_visible_gap(env, theme: str) -> None:
    rig, base = fresh(env)

    def body(page: Any, base: str) -> None:
        _serve_json(page, "**/api/ui/summary*", _summary_with_every_state())
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector('[data-testid="followups-bar"] circle')
        _theme(page, theme)
        arcs = page.evaluate(
            """() => [...document.querySelectorAll('[data-testid=followups-bar] circle:not(.arc-track)')].map((c) => ({
              dash: parseFloat(c.getAttribute('stroke-dasharray')),
              start: -parseFloat(c.getAttribute('stroke-dashoffset')) }))"""
        )
        assert len(arcs) == len(STATES)
        for a, b in zip(arcs, arcs[1:], strict=False):
            assert b["start"] - (a["start"] + a["dash"]) >= 1.9, (a, b)  # out of 100 round the ring

    _run(base, body)


# -- cards of one row are as high as each other
def _rows(page: Any, selector: str) -> dict[int, list[int]]:
    got = page.evaluate(
        """(sel) => [...document.querySelectorAll(sel)].map((e) => { const b = e.getBoundingClientRect();
          return [Math.round(b.top + scrollY), Math.round(b.height)]; })""",
        selector,
    )
    rows: dict[int, list[int]] = {}
    for top, height in got:
        rows.setdefault(top, []).append(height)
    return rows


@pytest.mark.parametrize("width", [1280, 1920])
def test_overview_cards_of_one_row_are_equally_high(env, width: int) -> None:
    rig, base = fresh(env)
    _seed(rig)

    def body(page: Any, base: str) -> None:
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector('[data-testid="doors"]')
        page.wait_for_timeout(500)
        rows = _rows(page, ".ov-mid > div > .card")
        assert len(rows) == 2 and all(len(r) == 2 for r in rows.values()), rows
        for top, heights in rows.items():
            assert max(heights) - min(heights) <= 1, (top, heights)

    _run(base, body)


@pytest.mark.parametrize("width", [1280, 1920])
def test_needs_cards_of_one_row_are_equally_high_and_none_is_left_alone(env, width: int) -> None:
    rig, base = fresh(env)

    def body(page: Any, base: str) -> None:
        review = _json("review.json")
        review["items"] = [i for i in review["items"] if i["tab"] == "action"]
        assert len(review["items"]) % 2 == 1  # an odd count: one card is last on its own
        _serve_json(page, "**/api/ui/review*", review)
        page.set_viewport_size({"width": width, "height": 1000})
        page.goto(base + "/ui/#/needs")
        page.wait_for_selector(".need")
        page.wait_for_timeout(500)
        rows = _rows(page, ".needs-list > .need")
        for top, heights in rows.items():
            assert max(heights) - min(heights) <= 1, (top, heights)
        wide = page.evaluate(
            """() => { const l = document.querySelector('.needs-list').getBoundingClientRect();
              const last = [...document.querySelectorAll('.needs-list > .need')].pop().getBoundingClientRect();
              return [Math.round(l.width), Math.round(last.width)]; }"""
        )
        assert abs(wide[0] - wide[1]) <= 1, wide  # the last, odd card takes the whole row

    _run(base, body)


# -- contrast: all text at least 4.5 to 1, in both themes
@pytest.mark.parametrize("theme", THEMES)
def test_demo_controls_text_has_enough_contrast(env, theme: str) -> None:
    rig, base = fresh(env)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/demo")
        page.wait_for_selector(".dm-page, .dm-action")
        page.wait_for_timeout(800)
        _theme(page, theme)
        assert not _low(page, ".main"), _low(page, ".main")

    _run(base, body)


@pytest.mark.parametrize("theme", THEMES)
def test_every_badge_text_has_enough_contrast_including_the_hot_rating(env, theme: str) -> None:
    rig, base = fresh(env)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/overview")
        page.wait_for_selector('[data-testid="doors"]')
        _theme(page, theme)
        page.evaluate(
            """(names) => { const box = document.createElement('div'); box.id = 'pill-box';
              names.forEach((n) => box.appendChild(window.Portal.ui.pill(n))); document.body.appendChild(box); }""",
            [*STATES, "hot", "warm", "cold", "new", "duplicate", "review", "open"],
        )
        assert not _low(page, "#pill-box"), _low(page, "#pill-box")

    _run(base, body)
