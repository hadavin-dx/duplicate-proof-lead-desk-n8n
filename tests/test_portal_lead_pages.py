"""The portal side of lead owner, lead stage, reminder and later-message approval, lead-name masking, the
sidebar at 1100 px and the demo proof panel. API facts run in process; page facts run in a real browser (skipped when
playwright or its browser is missing). Made-up data only; canary names are built from parts."""

from __future__ import annotations

import copy
import time
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest
from support import DEMO_LEADFLOW, call, lead
from test_later_approval import APPROVERS, make_later
from test_portal_api import fresh, get, sql

NAME = "Wenda " + "Brook"  # a lead name the page must never show while names are masked
NAME_2 = "Orla " + "Finch"
SEEN_KEY = "lead-desk-hide-personal"


def _flow() -> dict[str, Any]:
    flow = copy.deepcopy(DEMO_LEADFLOW)
    flow["nudge_after_seconds"] = None  # reminders off: no reminder email, no send worker needed here
    return flow


def _flow_rig(env: Any, **over: Any) -> tuple[Any, str]:
    return fresh(env, leadflow=_flow(), **over)


def _new(rig: Any, ref: str, name: str = NAME, email: str = "wenda@example.test") -> str:
    reply = rig.desk.process("form", ref, lead(name=name, email=email))
    assert reply.status == 201, reply.body
    return str(reply.body["lead_id"])


# ---------------------------------------------------------------------------------------------------------------- API
def test_leads_rows_carry_owner_step_and_label_and_are_null_when_the_flow_is_off(env: Any) -> None:
    rig, base = _flow_rig(env)
    lid = _new(rig, "w-1")
    row = get(base, "/api/ui/leads")[1]["rows"][0]
    assert row["lead_id"] == lid and row["owner"] in {"Asha", "Ravi"} and row["step"] == "new"
    assert row["step_label"] == "New"
    rig2, base2 = fresh(env)
    _new(rig2, "w-2")
    off = get(base2, "/api/ui/leads")[1]["rows"][0]
    assert (off["owner"], off["step"], off["step_label"]) == (None, None, None)


def test_lead_detail_carries_flow_keys_and_steps_in_order(env: Any) -> None:
    rig, base = _flow_rig(env)
    lid = _new(rig, "w-1")
    d = get(base, f"/api/ui/leads/{lid}")[1]
    assert d["owner"] in {"Asha", "Ravi"} and d["step"] == "new" and d["step_label"] == "New"
    assert [s["id"] for s in d["steps"]] == ["new", "contacted", "replied", "qualified", "won", "closed"]
    assert d["reminder"] is None and d["crm_copy"] == "waiting" and d["approval"] is None
    rig2, base2 = fresh(env)
    off = get(base2, f"/api/ui/leads/{_new(rig2, 'w-2')}")[1]
    assert off["owner"] is None and off["steps"] == [] and off["crm_copy"] is None and off["approval"] is None


def test_lead_detail_shows_approval_list_and_the_stored_decision(env: Any) -> None:
    rig, base = _flow_rig(env, later_mode="approval", approvers=APPROVERS, approvers_label="demo approver")
    rid = make_later(rig)
    lid = rig.query("SELECT lead_id FROM leads")[0][0]
    d = get(base, f"/api/ui/leads/{lid}")[1]
    assert d["approval"] == {"label": "demo approver", "approvers": [{"id": i, "name": n} for i, n in APPROVERS]}
    assert d["duplicates"][0]["receipt_id"] == rid and d["duplicates"][0]["decision"] is None
    status, body = call(base, "POST", f"/review/D{rid}/resolve", {"decision": "approve:appr-01"})
    assert status == 200 and body["decided_by_name"] == APPROVERS[0][1]
    one = get(base, f"/api/ui/leads/{lid}")[1]["duplicates"][0]["decision"]
    assert one["decision"] == "approved" and one["decided_by"] == "appr-01" and one["channel"] == "portal"


# ----------------------------------------------------------------------------------------------- demo proof panel data
def test_fakes_outbox_counts_reminders_apart(env: Any) -> None:
    from lead_desk.constants import NUDGE_KEY_PREFIX

    srv, url = env.fakes()
    for key in ("L000000000001", NUDGE_KEY_PREFIX + "L000000000001"):
        req = urllib.request.Request(  # noqa: S310 (loopback fakes server)
            url + "/fake/outbox",
            data=('{"key":"%s","to":"a@example.test","subject":"s","body":"b"}' % key).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()  # noqa: S310
    out = call(url, "GET", "/fake/outbox")[1]
    assert out["count"] == 2 and out["reminders"] == 1


def test_demo_state_splits_follow_up_emails_from_reminders() -> None:
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "demo_only" / "portal_demo.py").read_text(encoding="utf-8")
    assert '"reminders": reminders' in text and '"followups": count - reminders' in text
    js = (pathlib.Path(__file__).resolve().parents[1] / "demo_only" / "portal_demo.js").read_text(encoding="utf-8")
    assert "f.outbox.followups" in js and "proof-reminders" in js and "Follow-up emails in demo mail" in js


# ---------------------------------------------------------------------------------------------------------- browser
def _browse(
    base: str,
    fn: Callable[[Any, Any], None],
    *,
    width: int = 1280,
    height: int = 900,
    scheme: str = "light",
    init: str | None = None,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001 (no browser installed here)
            pytest.skip(f"no browser: {e}")
        try:
            ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
            page = ctx.new_page()
            if init:
                page.add_init_script(init)
            fn(page, base)
        finally:
            browser.close()


def _seed_names(rig: Any) -> str:
    first = _new(rig, "w-1")
    rig.desk.process("webhook", "w-2", lead(name=NAME, email="wenda@example.test", phone="555 0177"))
    _new(rig, "w-3", NAME_2, "orla@example.test")
    now = int(time.time())  # wall-clock: the nudge row is stamped like the desk's own calendar stamps
    sql(
        rig,
        "INSERT INTO nudges(lead_id, state, attempts, updated_at, last_error) VALUES (?, 'failed', 1, ?, 'refused')",
        (first, now),
    )
    return first


def _no_leak(page: Any, route: str, selector: str) -> None:
    page.goto(page.url.split("#")[0] + "#/" + route)
    page.wait_for_selector(selector)
    page.wait_for_timeout(300)
    html = page.content()
    assert NAME not in html and NAME_2 not in html, route


@pytest.mark.parametrize(
    ("route", "selector"),
    [
        ("overview", '[data-testid="feed-item"]'),
        ("leads", "[data-lead-id]"),
        ("log", '[data-testid="log-row"]'),
        ("needs", '[data-testid="needs-card"]'),
    ],
)
def test_lead_names_are_masked_by_default_on_every_page_and_an_eye_shows_one(
    env: Any, route: str, selector: str
) -> None:
    rig, base = _flow_rig(env)
    _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/")
        page.wait_for_selector("#main")
        _no_leak(page, route, selector)
        eyes = page.locator(f'{selector} [data-guard-key^="name:"], #main [data-guard-key^="name:"]')
        assert eyes.count() >= 1, route  # a name has its own eye, like an address
        eyes.first.click()
        page.wait_for_timeout(200)
        assert NAME in page.inner_text("#main") or NAME_2 in page.inner_text("#main")

    _browse(base, body)


def test_the_lead_panel_header_masks_the_name_and_follows_the_eye(env: Any) -> None:
    rig, base = _flow_rig(env)
    first = _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.click(f'[data-lead-id="{first}"]')
        page.wait_for_selector('[data-testid="lead-drawer"] .drawer-head h2')
        assert NAME not in page.content()
        assert page.get_attribute('[data-testid="lead-drawer"]', "aria-label") == "Lead"
        page.locator('[data-testid="lead-drawer"] .drawer-head [data-guard-key^="name:"]').click()
        assert NAME in page.inner_text('[data-testid="lead-drawer"] .drawer-head h2')

    _browse(base, body)


def test_a_shown_state_does_not_outlive_the_session_and_old_local_storage_is_ignored(env: Any) -> None:
    rig, base = _flow_rig(env)
    _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        assert NAME not in page.content()
        page.evaluate(f"() => {{ window.localStorage.setItem('{SEEN_KEY}', '0'); }}")  # what the old build stored
        page.reload()
        page.wait_for_selector("[data-lead-id]")
        assert NAME not in page.content()  # a stale shown state in local storage changes nothing
        page.uncheck('[data-testid="leads-hide-personal"]')
        assert NAME in page.inner_text('[data-testid="leads-table"]')
        assert page.evaluate(f"() => window.sessionStorage.getItem('{SEEN_KEY}')") == "0"
        assert page.evaluate(f"() => window.localStorage.getItem('{SEEN_KEY}')") == "0"  # untouched, never read
        page.context.clear_cookies()
        fresh_page = page.context.browser.new_context().new_page()  # a new session: nothing carried over
        fresh_page.goto(base + "/ui/#/leads")
        fresh_page.wait_for_selector("[data-lead-id]")
        assert NAME not in fresh_page.content()

    _browse(base, body)


def test_the_switch_says_names_too(env: Any) -> None:
    rig, base = _flow_rig(env)
    _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        assert "names" in page.inner_text(".hide-switch").lower()

    _browse(base, body)


def test_ui_bodies_hold_raw_values_because_masking_is_on_screen_only(env: Any) -> None:
    """Honest check: the JSON the page loads carries the real name. The mask is a display rule, not a data rule."""
    rig, base = _flow_rig(env)
    _seed_names(rig)
    assert NAME in str(get(base, "/api/ui/leads")[1]) and NAME in str(get(base, "/api/ui/review")[1])


@pytest.mark.parametrize("width", [1100, 1280, 1366, 1440, 1920])
def test_leads_table_shows_owner_and_step_inside_the_card_and_source_hides_up_to_1300(env: Any, width: int) -> None:
    rig, base = _flow_rig(env)
    _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        head = page.inner_text('[data-testid="leads-table"] thead')
        assert "Lead owner" in head and "Step" in head and "Last change" in head
        m = page.evaluate(
            "() => { const w = document.querySelector('.leads-out .table-wrap');"
            " const th = [...document.querySelectorAll('.leads-out thead th')].map("
            "x => [x.textContent.trim(), x.getBoundingClientRect().right, getComputedStyle(x).display]);"
            " return {sw: w.scrollWidth, cw: w.clientWidth, th: th,"
            " page: document.documentElement.scrollWidth, vw: window.innerWidth}; }"
        )
        assert m["sw"] <= m["cw"] + 1, m  # no sideways scroll inside the card: "Last change" stays on it
        assert m["page"] <= m["vw"], m
        source = [x for x in m["th"] if x[0] == "Source"][0]
        assert (source[2] == "none") == (width <= 1300), (width, source)  # the room is used as soon as there is some
        last = [x for x in m["th"] if x[0] == "Last change"][0]
        assert last[1] <= m["vw"], m

    _browse(base, body, width=width)


def test_the_step_control_moves_a_lead_forward_only(env: Any) -> None:
    rig, base = _flow_rig(env)
    first = _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.click(f'[data-lead-id="{first}"]')
        page.wait_for_selector('[data-testid="step-select"]')
        opts = page.eval_on_selector_all('[data-testid="step-select"] option', "o => o.map(x => x.value)")
        assert opts[0] == "" and opts[1] == "contacted" and "new" not in opts  # only later steps are offered
        assert page.is_disabled('[data-testid="step-move"]')
        page.select_option('[data-testid="step-select"]', "replied")
        page.click('[data-testid="step-move"]')
        # the poll redraws the panel, so the element can be missing for a moment: wait on the text
        page.wait_for_function(
            "() => document.querySelector('[data-testid=\"step-now\"]')?.textContent === 'Replied'", timeout=15000
        )
        opts = page.eval_on_selector_all('[data-testid="step-select"] option', "o => o.map(x => x.value)")
        assert "contacted" not in opts and "replied" not in opts and "qualified" in opts

    _browse(base, body)
    assert rig.query("SELECT step FROM lead_state WHERE lead_id=?", (first,))[0][0] == "replied"


def test_the_panel_fills_even_when_the_lead_answer_comes_after_the_old_panel_closed(env: Any) -> None:
    """Moving a lead redraws the open panel: the old panel is closed (its 'close' event comes a moment later) and a
    new one opens. If the answer for the new panel is slow, the old panel's close handler used to clear the new
    panel's state, so it stayed on its loading bars for good (a 1 in 10 failure of the step test on Linux)."""
    rig, base = _flow_rig(env)
    first = _seed_names(rig)
    slow = {"on": False}

    def late(route: Any) -> None:
        if slow["on"] and route.request.method == "GET":
            got = route.fetch()
            time.sleep(0.6)  # fault injection: the answer arrives after the old panel's close event
            route.fulfill(response=got)
        else:
            route.continue_()

    def body(page: Any, base: str) -> None:
        page.route("**/api/ui/leads/L*", late)
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.click(f'[data-lead-id="{first}"]')
        page.wait_for_selector('[data-testid="step-select"]')
        page.select_option('[data-testid="step-select"]', "replied")
        slow["on"] = True
        page.click('[data-testid="step-move"]')
        page.wait_for_function(
            "() => document.querySelector('[data-testid=\"step-now\"]')?.textContent === 'Replied'", timeout=15000
        )

    _browse(base, body)


def test_the_reminder_card_has_one_seen_action_and_the_ledger_names_the_reminder(env: Any) -> None:
    rig, base = _flow_rig(env)
    first = _seed_names(rig)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/needs")
        page.wait_for_selector('[data-kind="reminder_to_check"]')
        card = page.locator('[data-kind="reminder_to_check"]').first
        assert "Reminder to check" in card.inner_text() and NAME not in card.inner_text()
        acts = card.locator("button[data-action]")
        assert acts.count() == 1 and acts.first.get_attribute("data-action") == "resolve_seen"
        acts.first.click()
        page.click('[data-testid="confirm-ok"]')
        page.wait_for_selector('[data-testid="toast"]')
        assert "marked as seen" in page.inner_text('[data-testid="toast"]')

    _browse(base, body)
    assert rig.query("SELECT handled FROM nudges WHERE lead_id=?", (first,))[0][0] == 1


def _approval_rig(env: Any) -> tuple[Any, str, str, int]:
    rig, base = _flow_rig(env, later_mode="approval", approvers=APPROVERS, approvers_label="demo approver")
    rid = make_later(rig)
    return rig, base, str(rig.query("SELECT lead_id FROM leads")[0][0]), rid


def test_approve_and_deny_live_in_the_lead_drawer_with_a_confirm_dialog_and_a_decided_line(env: Any) -> None:
    rig, base, lid, rid = _approval_rig(env)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/needs")
        page.wait_for_selector("#main")
        assert page.locator('[data-testid="approve-button"]').count() == 0  # never on Needs
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.click(f'[data-lead-id="{lid}"]')
        page.wait_for_selector('[data-testid="approval"]')
        assert page.is_disabled('[data-testid="approve-button"]') and page.is_disabled('[data-testid="deny-button"]')
        assert page.get_attribute('[data-testid="approve-button"]', "title") == "Choose an approver first"
        text = page.inner_text('[data-testid="approval"]')
        assert "name is picked, not verified" in text and "demo approver" in text
        page.select_option('[data-testid="approver-select"]', "appr-02")
        page.click('[data-testid="approve-button"]')
        assert "saved in the desk only" in page.inner_text('[data-testid="confirm-dialog"]')
        page.click('[data-testid="confirm-cancel"]')
        assert page.locator('[data-testid="decided-line"]').count() == 0
        page.click('[data-testid="approve-button"]')
        page.click('[data-testid="confirm-ok"]')
        page.wait_for_selector('[data-testid="decided-line"]')
        line = page.inner_text('[data-testid="decided-line"]')
        assert line.startswith("Approved by " + APPROVERS[1][1]) and "(name picked, not verified)" in line
        assert page.locator('[data-testid="approve-button"]').count() == 0

    _browse(base, body)
    assert rig.query("SELECT decision, approver_id FROM later_decisions WHERE receipt_id=?", (rid,)) == [
        ("approved", "appr-02")
    ]


def test_a_losing_decision_shows_who_was_first(env: Any) -> None:
    rig, base, lid, rid = _approval_rig(env)

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.click(f'[data-lead-id="{lid}"]')
        page.wait_for_selector('[data-testid="approval"]')
        assert (
            call(base, "POST", f"/review/D{rid}/resolve", {"decision": "deny:appr-01"})[0] == 200
        )  # someone else first
        page.select_option('[data-testid="approver-select"]', "appr-02")
        page.click('[data-testid="approve-button"]')
        page.click('[data-testid="confirm-ok"]')
        page.wait_for_selector('[data-testid="decided-line"]')
        assert page.inner_text('[data-testid="decided-line"]').startswith("Denied by " + APPROVERS[0][1])

    _browse(base, body)


def test_mode_off_shows_no_approval_and_no_step_control(env: Any) -> None:
    rig, base = fresh(env)
    rig.desk.process("form", "x-1", lead(email="ada@example.test"))
    rig.desk.process("webhook", "x-2", lead(email="ada@example.test", phone="555 0199"))
    lid = rig.query("SELECT lead_id FROM leads")[0][0]

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.click(f'[data-lead-id="{lid}"]')
        page.wait_for_selector('[data-testid="later-card"]')
        assert page.locator('[data-testid="approval"], [data-testid="step-control"]').count() == 0

    _browse(base, body)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_sidebar_column_is_one_colour_to_the_page_bottom_at_1100(env: Any, scheme: str) -> None:
    """At 1200 px and below the strip under the sidebar was the page colour, not the sidebar's: a white block under
    the sidebar when the page is taller than the window. Measured on a full-page screenshot."""
    rig, base = _flow_rig(env)
    for i in range(12):
        _new(rig, f"s-{i}", f"Sidney Hart{i}", f"s{i}@example.test")

    def body(page: Any, base: str) -> None:
        import base64

        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.wait_for_timeout(500)
        height = page.evaluate("() => document.documentElement.scrollHeight")
        assert height > 900, height  # the page really is taller than the window
        assert height - 40 > 900, ("second sample point is inside the rail", height)  # the rail is 900 px high
        # A third point, on the brand mark (a fixed element in the rail's header): its colour is far from the rail's,
        # so a reader that returned one value for every point would be caught below.
        box = page.locator(".mark").first.bounding_box()
        assert box is not None
        mark_pt = [round(box["x"]) + 4, round(box["y"] + box["height"] / 2)]
        # The browser reads the screenshot's pixels itself (no image library): a blank page in the same browser
        # decodes the PNG onto a canvas. A blank page, because the portal page's own rules could refuse a data: image.
        shot = "data:image/png;base64," + base64.b64encode(page.screenshot(full_page=True)).decode()
        reader = page.context.new_page()
        try:
            got = reader.evaluate(
                """async ([src, points]) => {
                    const img = new Image();
                    img.src = src;
                    await img.decode();
                    const canvas = document.createElement("canvas");
                    canvas.width = img.naturalWidth;
                    canvas.height = img.naturalHeight;
                    const ctx = canvas.getContext("2d");
                    ctx.drawImage(img, 0, 0);
                    return {size: [canvas.width, canvas.height],
                            px: points.map(([x, y]) => Array.from(ctx.getImageData(x, y, 1, 1).data))};
                }""",
                [shot, [[60, 880], [60, height - 40], mark_pt]],
            )
        finally:
            reader.close()
        assert got["size"][1] > height - 40, got["size"]  # both sample points lie inside the screenshot
        above, below, mark = got["px"]
        assert max(abs(a - b) for a, b in zip(above[:3], mark[:3], strict=True)) > 60, (
            "the reader returns the same colour for different points",
            above,
            mark,
        )
        assert above[3] == below[3] == 255, ("not opaque", above, below)  # opaque: the PNG's own colours
        assert all(abs(a - b) <= 6 for a, b in zip(above[:3], below[:3], strict=True)), (
            "sidebar and strip differ",
            above,
            below,
        )

    _browse(base, body, width=1100, scheme=scheme)


# ------------------------------------------------------------------------------------ demo proof panel (node probe)
def _proof(arg: dict[str, int]) -> dict[str, Any]:
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    here = Path(__file__).resolve().parent
    run = subprocess.run(  # noqa: S603
        [node, str(here / "portal_proof_probe.js"), str(here.parent / "demo_only" / "portal_demo.js"), json.dumps(arg)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(run.stdout)  # type: ignore[no-any-return]


def test_proof_panel_compares_follow_ups_only_and_shows_reminders_on_their_own_line() -> None:
    out = _proof({"sent": 1, "count": 2, "followups": 1, "reminders": 1})
    assert not out["mismatch"] and out["reminders"]
    assert "Reminder emails in the demo mail: 1" in out["text"] and "not follow-ups" in out["text"]
    assert "Follow-up emails in demo mail" in out["text"]


def test_proof_panel_still_flags_a_real_difference_and_an_older_fakes_server_works() -> None:
    assert _proof({"sent": 2, "count": 3, "followups": 1, "reminders": 2})["mismatch"]
    old = _proof({"sent": 1, "count": 1})  # no followups or reminders key: the total is used, no reminder line
    assert not old["mismatch"] and not old["reminders"]


# ------------------------------------------------------------------------------------- masking: search and demo lists
def test_name_search_needs_reveal(env: Any) -> None:
    """A person must not be able to probe whether a name exists while names are masked: the page sends by_email=0,
    and the desk then matches only company and lead number. The placeholder says so."""
    rig, base = _flow_rig(env)
    _seed_names(rig)
    assert get(base, "/api/ui/leads?q=Wenda&by_email=0")[1]["total"] == 0
    assert get(base, "/api/ui/leads?q=Wenda&by_email=1")[1]["total"] >= 1

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        assert "name" not in (page.get_attribute('[data-testid="leads-search"]', "placeholder") or "").lower()
        assert "after you turn off hiding" in page.inner_text('[data-testid="leads-search-hint"]')
        page.fill('[data-testid="leads-search"]', "Wenda")
        page.wait_for_selector("text=No leads match")  # the probe finds nothing: nothing to learn about the name
        assert NAME not in page.content()
        page.uncheck('[data-testid="leads-hide-personal"]')
        assert "name" in (page.get_attribute('[data-testid="leads-search"]', "placeholder") or "").lower()
        page.fill('[data-testid="leads-search"]', "Wenda")
        page.wait_for_function("() => document.querySelectorAll('[data-lead-id]').length >= 1")
        assert NAME in page.inner_text('[data-testid="leads-table"]')

    _browse(base, body)


def test_demo_pick_lists_neutral(env: Any, monkeypatch: Any) -> None:
    from test_portal_demo import KeyedCrm, make, wired

    from lead_desk import server

    srv, furl = env.fakes("blind", "keyed")
    rig = wired(env, furl, crm_cls=KeyedCrm, leadflow=_flow())
    rig.clock.t = int(time.time())  # wall-clock: the desk stamps its own calendar time
    demo = make(rig.desk)
    monkeypatch.setattr(server, "load_demo_portal", lambda desk: demo)
    base = env.serve(rig)
    demo.desk_url = base
    names = [p["name"] for p in demo._people]
    lid = _new(rig, "dm-1", names[0], "dm-person@example.com")
    sql(rig, "UPDATE followups SET state='failed', last_error='refused', updated_at=? WHERE lead_id=?", (5, lid))

    def texts(page: Any, sel: str) -> list[str]:
        return page.eval_on_selector_all(f"{sel} option", "o => o.map(x => x.textContent)")

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/demo")
        page.wait_for_selector('[data-testid="demo-person"]')
        page.wait_for_function(
            "() => document.querySelectorAll('[data-testid=\"demo-retry-item\"] option').length >= 1"
        )
        shown = texts(page, '[data-testid="demo-person"]')
        assert shown[:2] == ["Lead 1", "Lead 2"] and len(shown) == len(names)
        assert not any(n in page.content() for n in names)  # neither pick list holds a name
        page.evaluate("() => Portal.privacy.set(false)")  # names shown
        page.wait_for_timeout(200)
        assert texts(page, '[data-testid="demo-person"]')[0].startswith(names[0])
        assert names[0] in texts(page, '[data-testid="demo-retry-item"]')[0]
        page.evaluate("() => Portal.privacy.set(true)")  # hidden again: relabelled at once, selection kept
        page.wait_for_timeout(200)
        assert texts(page, '[data-testid="demo-person"]')[0] == "Lead 1"
        assert texts(page, '[data-testid="demo-retry-item"]')[0].startswith("Lead 1: ")  # not a row of dots
        assert not any(n in page.content() for n in names)
        page.select_option('[data-testid="demo-person"]', "2")
        page.click('[data-testid="demo-send"]')  # the result line must not name the lead either
        page.wait_for_selector('[data-testid="demo-burst-result"]:not([hidden])')
        assert "Lead 2" in page.inner_text('[data-testid="demo-burst-result"]')  # the result line names no lead
        assert not any(n in page.content() for n in names)

    _browse(base, body)


def test_step_and_approval_buttons_wake_up_when_the_desk_answers_again(env: Any) -> None:
    """The Move, Approve and Deny buttons are disabled while the desk does not answer, and must be enabled again when it
    answers (a chosen step or approver is still chosen), or a button stays dead after the desk returns. Behaviour in a
    browser: the desk's answers are cut, the buttons go dead, the answers come back, the buttons wake up."""
    rig, base, lid, rid = _approval_rig(env)
    pattern = "**/api/ui/**"

    def body(page: Any, base: str) -> None:
        page.goto(base + "/ui/#/leads")
        page.wait_for_selector("[data-lead-id]")
        page.click(f'[data-lead-id="{lid}"]')
        page.wait_for_selector('[data-testid="approval"]')
        page.select_option('[data-testid="step-select"]', "replied")
        page.select_option('[data-testid="approver-select"]', "appr-02")
        for button in ("step-move", "approve-button", "deny-button"):
            assert page.is_enabled(f'[data-testid="{button}"]'), button
        page.route(pattern, lambda r: r.abort())  # the desk stops answering
        page.wait_for_function("() => Portal.stale()", timeout=30000)
        for button in ("step-move", "approve-button", "deny-button"):
            page.wait_for_selector(f'[data-testid="{button}"][disabled]', timeout=30000)
        page.unroute(pattern)  # the desk answers again
        page.wait_for_function("() => !Portal.stale()", timeout=30000)
        for button in ("step-move", "approve-button", "deny-button"):
            page.wait_for_selector(f'[data-testid="{button}"]:not([disabled])', timeout=30000)

    _browse(base, body)
