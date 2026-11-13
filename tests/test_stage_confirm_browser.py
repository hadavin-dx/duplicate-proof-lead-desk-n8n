"""The second question before a stage move is written to a real CRM, in a real browser (skipped when playwright or its
browser is missing): it does not outlive the panel, a double click does not answer it, it lapses after a few seconds,
the warning is in view without scrolling, and the confirming button is not the plain Move button. Made-up data only."""

from __future__ import annotations

from typing import Any

import pytest
from support import lead
from test_mode_safety import _browse, _lead_and_open, _open_panel

MOVE = '[data-testid="step-move"]'
SELECT = '[data-testid="step-select"]'
ASK = "Yes, write to the CRM"


def _step(rig: Any, lid: str) -> str:
    return str(rig.query("SELECT step FROM lead_state WHERE lead_id=?", (lid,))[0][0])


def _arm(page: Any) -> None:
    page.select_option(SELECT, "replied")
    page.click(MOVE)
    assert page.inner_text(MOVE) == ASK


def test_closing_and_reopening_the_panel_asks_again_from_the_start(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        _arm(page)
        page.click('[data-testid="drawer-close"]')
        page.wait_for_selector('[data-testid="lead-drawer"]', state="detached")
        page.click(f'[data-lead-id="{lid}"]')
        page.wait_for_selector(SELECT)
        assert page.input_value(SELECT) == "" and page.inner_text(MOVE) == "Move"
        page.select_option(SELECT, "replied")
        assert page.inner_text(MOVE) == "Move"  # the old question is gone: a click only asks again
        assert _step(rig, lid) == "new"

    _browse(base, body)


def test_a_double_click_does_not_answer_the_question(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        page.select_option(SELECT, "replied")
        page.evaluate(
            "() => { const b = document.querySelector('[data-testid=\"step-move\"]'); b.click(); b.click(); }"
        )  # two clicks at once
        page.wait_for_timeout(1500)
        assert page.inner_text(MOVE) == ASK and _step(rig, lid) == "new"  # asked, not answered, nothing written

    _browse(base, body)


def test_a_click_after_the_pause_answers_the_question(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        _arm(page)
        page.wait_for_timeout(700)
        page.click(MOVE)
        page.wait_for_function("() => document.querySelector('[data-testid=\"step-now\"]')?.textContent === 'Replied'")
        assert _step(rig, lid) == "replied"

    _browse(base, body)


def test_the_question_lapses_after_eight_seconds(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        page.clock.install()
        _open_panel(page, base, lid)
        _arm(page)
        page.clock.pause_at(
            page.evaluate("(Date.now() + 500) / 1000")
        )  # epoch seconds  # from here only the test moves time: no slow-runner drift
        page.clock.run_for(5000)
        assert page.inner_text(MOVE) == ASK  # still asked at about 5.5 seconds
        page.clock.run_for(3000)
        assert page.inner_text(MOVE) == "Move" and _step(rig, lid) == "new"  # lapsed after 8: a click asks again

    _browse(base, body)


def test_a_question_asked_for_one_lead_does_not_survive_a_look_at_another_lead(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")
    other = str(rig.desk.process("form", "c-other", lead(email="other@example.test")).body["lead_id"])

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        _arm(page)
        show = '(id) => document.querySelector(`[data-lead-id="${id}"]`).click()'  # the panel covers the list
        page.evaluate(show, other)  # the panel is replaced, not closed
        page.wait_for_function(
            '() => document.querySelector(\'[data-testid="lead-drawer"] [data-testid="step-select"]\')'
        )
        page.wait_for_timeout(600)  # past the double-click gap
        page.evaluate(show, lid)
        page.wait_for_function("() => document.querySelector('[data-testid=\"step-select\"]')?.value === ''")
        page.wait_for_timeout(600)
        assert page.input_value(SELECT) == "" and page.inner_text(MOVE) == "Move"  # asks from the start again
        page.select_option(SELECT, "replied")
        page.click(MOVE)
        assert page.inner_text(MOVE) == ASK and _step(rig, lid) == "new"  # one click only asks

    _browse(base, body)


def test_arming_moves_nothing_and_says_how_long_the_question_lasts(env: Any) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        page.select_option(SELECT, "replied")
        page.wait_for_timeout(700)  # the panel slides in: measure when it has stopped
        find = (
            '() => { const r = s => document.querySelector(`[data-testid="${s}"]`).getBoundingClientRect();'
            " const c = document.querySelector('[data-testid=\"step-control\"]').getBoundingClientRect();"
            " return [r('step-move').top, r('step-hint').top, r('step-move').bottom, r('step-hint').height,"
            " c.height, c.bottom]; }"
        )
        before = page.evaluate(find)
        assert page.inner_text('[data-testid="step-hint"]') == "A lead only moves forward."
        assert before[1] >= before[2]  # its own row: it starts below the bottom of the Move button
        page.click(MOVE)
        page.wait_for_timeout(400)
        assert page.inner_text('[data-testid="step-hint"]') == "Click again within 8 s."
        assert page.evaluate(find) == before  # nothing moved, nothing grew

    _browse(base, body)


def _lum(rgb: tuple[float, float, float]) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_confirm_button_has_readable_text_and_stands_out_from_the_note(env: Any, scheme: str) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        _arm(page)
        page.wait_for_timeout(400)  # the colour changes with a short transition
        got = page.evaluate(
            """() => { const rgb = e => getComputedStyle(e).backgroundColor;
              const b = document.querySelector('[data-testid="step-move"]');
              return { fill: rgb(b), ink: getComputedStyle(b).color,
                       edge: getComputedStyle(b).borderTopColor,
                       note: rgb(document.querySelector('[data-testid="step-crm-warning"]')) }; }"""
        )

        def parse(c: str) -> tuple[float, float, float]:
            nums = [
                float(x) for x in c[c.index("(") + 1 : c.index(")")].replace("/", ",").replace(" ", ",").split(",") if x
            ]
            assert len(nums) == 3 or nums[3] == 1, c  # opaque colours only: a blend would need the page behind it
            return nums[0], nums[1], nums[2]

        fill, ink, note, edge = (parse(got[k]) for k in ("fill", "ink", "note", "edge"))
        assert _ratio(ink, fill) >= 4.5, got  # the words on the button
        assert _lum(ink) < _lum(fill), got  # dark words on a bright amber fill in both themes, one look
        assert max(_ratio(fill, note), _ratio(edge, note)) >= 3, (
            got
        )  # the button (its fill or its edge) against the note above it

    _browse(base, body, scheme=scheme)


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [1100, 1440])
def test_the_warning_is_fully_in_view_in_the_panel_and_the_confirm_button_is_amber(
    env: Any, width: int, scheme: str
) -> None:
    rig, base, lid = _lead_and_open(env, "hubspot")

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid)
        seen = page.evaluate(
            """() => { const w = document.querySelector('[data-testid="step-crm-warning"]').getBoundingClientRect();
              const b = document.querySelector('.drawer-body').getBoundingClientRect();
              const m = document.querySelector('[data-testid="step-move"]').getBoundingClientRect();
              return { wTop: w.top, wBottom: w.bottom, bTop: b.top, bBottom: b.bottom, mBottom: m.bottom,
                       mTop: m.top, scroll: document.querySelector('.drawer-body').scrollTop }; }"""
        )
        assert seen["scroll"] == 0 and seen["bTop"] <= seen["wTop"] and seen["wBottom"] <= seen["bBottom"], seen
        assert seen["wBottom"] <= seen["mTop"], seen  # above the choice, not below it
        assert seen["mBottom"] <= seen["bBottom"], seen  # and the Move row is in view too
        plain = page.eval_on_selector(MOVE, "e => getComputedStyle(e).backgroundColor")
        _arm(page)
        page.wait_for_timeout(400)  # the button's colour changes with a short transition
        asked = page.eval_on_selector(MOVE, "e => getComputedStyle(e).backgroundColor")
        amber = page.evaluate(
            "() => { const t = document.createElement('i'); t.style.background = 'var(--warn-btn-bg)';"
            " document.body.appendChild(t); const c = getComputedStyle(t).backgroundColor; t.remove(); return c; }"
        )
        assert asked != plain and asked == amber, (plain, asked, amber)

    _browse(base, body, width=width, scheme=scheme)


GAVE_UP = "The desk gave up after several tries."
LATER = " Moving the lead to a later stage starts the copy afresh."
LAST = " A lead already at the last stage is not copied again."


@pytest.mark.parametrize(("to", "text"), [("closed", GAVE_UP + LAST), ("contacted", GAVE_UP + LATER)])
def test_the_note_after_a_give_up_promises_only_what_the_lead_can_do(env: Any, to: str, text: str) -> None:
    import sqlite3

    from lead_desk.constants import MIRROR_MAX_TRIES

    rig, base, lid = _lead_and_open(env, "hubspot")
    if to == "closed":
        assert rig.desk.set_step(lid, "closed").status == 200
    con = sqlite3.connect(rig.db_path)
    con.execute("UPDATE lead_state SET crm_tries=? WHERE lead_id=?", (MIRROR_MAX_TRIES, lid))
    con.execute("UPDATE followups SET crm_done=1 WHERE lead_id=?", (lid,))
    con.commit()
    con.close()

    def body(page: Any, base: str) -> None:
        _open_panel(page, base, lid) if to != "closed" else _open_closed(page, base, lid)
        note = page.locator('[data-testid="proof-ledger"] .chip-with-note .step-note').inner_text()
        assert note == text, note

    _browse(base, body)


def _open_closed(page: Any, base: str, lid: str) -> None:  # a closed lead has no later step, so no select
    page.goto(base + "/ui/#/leads")
    page.wait_for_selector(f'[data-lead-id="{lid}"]')
    page.click(f'[data-lead-id="{lid}"]')
    page.wait_for_selector('[data-testid="step-now"]')
