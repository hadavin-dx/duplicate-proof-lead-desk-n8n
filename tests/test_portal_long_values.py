"""A long value with no space in it (a name, a company, a desk note, a title) must stay inside its box on the places
that show desk text: the confirm question, the message at the corner, the Needs a person cards and the Leads table.
Real browser (skipped when playwright or its browser is missing); every element is compared with its container on
BOTH sides (an overflow to the left is invisible to scrollWidth). Made-up data only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support import lead
from test_portal_lead_pages import _browse, _flow_rig, _new

FIX = Path(__file__).parent / "portal_fixtures"
LONG = "W" * 200  # no space or hyphen anywhere: the browser has no natural place to break it
UNKNOWN = "FL0c4d8e2f6a19"  # the card of the fixture with the "I checked" buttons
SCHEMES = ["light", "dark"]
WIDTHS = [1280, 390]
JSON_HEAD = {"status": 200, "content_type": "application/json"}
# every element inside `root` whose box leaves the box of `root` (or the window), on either side
ESCAPES = """(root) => {
  const box = document.querySelector(root).getBoundingClientRect();
  const left = Math.max(box.left, 0) - 1, right = Math.min(box.right, innerWidth) + 1, out = [];
  for (const el of document.querySelectorAll(root + ' *')) {
    const r = el.getBoundingClientRect();
    if (r.width && (r.left < left || r.right > right)) {
      out.push(el.tagName + '.' + el.className + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
    }
  }
  return { out, box: [box.left, box.right], vw: innerWidth };
}"""


def _review() -> dict[str, Any]:
    d: dict[str, Any] = json.loads((FIX / "review.json").read_text())
    for it in d["items"]:
        if it["item_id"] == UNKNOWN:
            it["lead_name"], it["hint"] = LONG, LONG
        elif it["item_id"] == "R13":  # a request to check: the title and the reason text carry the long value
            it["title"], it["reason_text"] = "Incomplete request: " + LONG + " has a value the desk cannot accept", LONG
        elif it["item_id"] == "D2":  # an information card with a merged title and notes
            it["merged_title"], it["notes"] = "Repeat changed: " + LONG, [LONG, LONG]
    return d


def _serve_review(page: Any) -> None:
    text = json.dumps(_review())
    page.route("**/api/ui/review", lambda r: r.fulfill(body=text, **JSON_HEAD))


def _open_needs(page: Any, base: str) -> None:
    _serve_review(page)
    page.goto(base + "/ui/#/needs")
    page.wait_for_selector('[data-testid="needs-card"]')


def _ask(page: Any) -> None:  # press "I checked: it was sent" on the card of the long name
    page.locator(f'[data-item-id="{UNKNOWN}"] [data-action="resolve_sent"]').click()
    page.wait_for_selector('[data-testid="confirm-dialog"]')


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_a_long_name_stays_inside_the_confirm_question(env: Any, scheme: str, width: int) -> None:
    rig, base = _flow_rig(env)

    def body(page: Any, base: str) -> None:
        page.add_init_script("window.sessionStorage.setItem('lead-desk-hide-personal', '0')")  # names shown
        _open_needs(page, base)
        _ask(page)
        assert LONG in page.inner_text('[data-testid="confirm-dialog"] p')
        m = page.evaluate(ESCAPES, '[data-testid="confirm-dialog"]')
        assert not m["out"], m  # text and buttons lie inside the dialog box and the window
        d = page.evaluate("""() => { const e = document.querySelector('[data-testid="confirm-dialog"]');
          return [e.scrollWidth, e.clientWidth]; }""")
        assert d[0] <= d[1] + 1, d
        page.click('[data-testid="confirm-ok"]')  # the buttons can be pressed
        page.wait_for_selector('[data-testid="confirm-dialog"]', state="detached")

    _browse(base, body, width=width, scheme=scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_a_long_desk_note_stays_inside_the_message_at_the_corner(env: Any, scheme: str, width: int) -> None:
    rig, base = _flow_rig(env)

    def body(page: Any, base: str) -> None:
        _open_needs(page, base)
        page.route(
            f"**/review/{UNKNOWN}/resolve",
            lambda r: r.fulfill(status=409, content_type="application/json", body=json.dumps({"hint": LONG})),
        )
        _ask(page)
        page.click('[data-testid="confirm-ok"]')
        page.wait_for_selector('[data-testid="toast"]')
        assert LONG in page.inner_text('[data-testid="toast"]')
        m = page.evaluate(ESCAPES, '[data-testid="toast"]')
        assert not m["out"], m  # the text and its close button lie inside the message and the window
        x = page.evaluate("document.querySelector('[data-testid=\"toast\"] .toast-x').getBoundingClientRect().width")
        assert x >= 23, x  # the close button keeps its size

    _browse(base, body, width=width, scheme=scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("width", WIDTHS)
def test_long_values_stay_inside_their_needs_a_person_cards(env: Any, scheme: str, width: int) -> None:
    rig, base = _flow_rig(env)

    def body(page: Any, base: str) -> None:
        page.add_init_script("window.sessionStorage.setItem('lead-desk-hide-personal', '0')")
        for tab, item in (("action", UNKNOWN), ("action", "R13"), ("info", "D2")):
            _open_needs(page, base)
            if tab != "action":
                page.click(f'[data-testid="needs-tab-{tab}"]')
            card = f'[data-item-id="{item}"]'
            page.wait_for_selector(card)
            m = page.evaluate(ESCAPES, card)
            assert not m["out"], (item, m)  # name, heading, reason, notes and desk note stay inside the card

    _browse(base, body, width=width, scheme=scheme)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_a_long_company_does_not_squeeze_the_leads_table(env: Any, scheme: str) -> None:
    rig, base = _flow_rig(env)
    _new(rig, "ok-1", "Wenda Brook", "wenda@example.test")
    assert rig.desk.process("form", "long-1", lead(name=LONG, company=LONG, email="long@example.test")).status == 201

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.locator('[data-testid="leads-hide-personal"]').uncheck()
        m = page.evaluate("""() => { const w = document.querySelector('[data-testid="leads-table"]');
          const name = w.querySelector('tbody tr td:first-child').getBoundingClientRect().width;
          return { scroll: w.scrollWidth, client: w.clientWidth, name }; }""")
        assert m["scroll"] <= m["client"] + 1, m  # the table needs no sideways scroll for one long company
        assert m["name"] >= 120, m  # and the name column keeps a readable width

    _browse(base, body, width=1280, scheme=scheme)
