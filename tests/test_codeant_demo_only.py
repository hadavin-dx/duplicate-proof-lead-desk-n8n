"""Findings about demo_only/ from the CodeAnt review: the demo extension learns the desk's real port when the desk
listens on port 0. Made-up data, loopback only."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from test_portal_demo import wired

from lead_desk import server
from lead_desk.loader import KnowsBoundAddress


def test_the_demo_extension_gets_the_real_port_when_the_desk_listens_on_port_zero(env: Any) -> None:
    _, furl = env.fakes("blind", "keyed")
    rig = wired(env, furl)
    srv = server.make_server(rig.desk, replace(rig.desk.config, port=0, bind="127.0.0.1"))
    try:
        demo = srv.demo
        assert demo is not None
        assert isinstance(demo, KnowsBoundAddress)  # the optional part of the DemoPortal contract (loader.py)
        assert demo.desk_url == f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.server_close()


def test_a_fixed_port_keeps_the_address_from_the_settings(env: Any) -> None:
    _, furl = env.fakes("blind", "keyed")
    rig = wired(env, furl)
    from demo_only.portal_demo import make

    d = make(rig.desk)
    d.desk_url = "http://127.0.0.1:1"
    d.set_bound_address("127.0.0.1", 55555)
    assert d.desk_url == "http://127.0.0.1:1"  # an address that is already known is never replaced


def test_the_burst_summary_counts_slots_that_got_no_http_answer() -> None:
    """demo_only/portal_demo.js `describe` is cut out of the file and run in node: a slot that ended as 'timeout' or
    'error' (a word, not a status number) must show in the pills, not vanish."""
    import json
    import re
    import shutil
    import subprocess
    from pathlib import Path

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    src = (Path(__file__).resolve().parents[1] / "demo_only" / "portal_demo.js").read_text(encoding="utf-8")
    start = src.index("  function describe(r, label) {")
    end = src.index("\n  }\n", start) + 4
    answers = [
        {"ref": "a", "status": 201, "verdict": "new", "replayed": False},
        {"ref": "b", "status": 200, "verdict": "duplicate", "replayed": False},
        {"ref": "c", "status": "timeout", "verdict": None, "replayed": False},
        {"ref": "d", "status": "error", "verdict": None, "replayed": False},
        {"ref": "e", "status": 400, "verdict": None, "replayed": False},
    ]
    js = (
        "function h(tag, attrs) { const k = []; for (let i = 2; i < arguments.length; i++) { const x = arguments[i];"
        " if (Array.isArray(x)) k.push(...x); else if (x !== null && x !== undefined) k.push(x); } return { tag, k,"
        " appendChild(x) { k.push(x); return x; } }; }\n"
        "const P = { icon: () => '', shownRef: (r) => r };\n"
        + src[start:end]
        + "\nconst text = (n) => typeof n === 'string' ? n : (n && n.k ? n.k.map(text).join(' ') : '');\n"
        "const out = describe({ data: { answers: JSON.parse(process.argv[1]) } }, 'x');\n"
        "console.log(JSON.stringify(text(out.k[1])));\n"
    )
    done = subprocess.run(  # noqa: S603
        ["node", "-e", js, json.dumps(answers)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    pills = re.sub(r"\s+", " ", json.loads(done.stdout))
    assert "1 refused" in pills and "2 without an answer" in pills, pills


def test_every_request_of_the_double_press_check_frees_the_button_when_it_gets_no_answer() -> None:
    """Source check, not a browser run: each P.api call inside the double-press click handler (the two presses, the
    desk summary poll and the demo state read) must have a rejection handler that frees the button (dblFail)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "demo_only" / "portal_demo.js").read_text(encoding="utf-8")
    block = src[src.index("dbl.addEventListener('click'") :]
    block = block[: block.index("\n      });\n") + 1]
    assert block.count("P.api(") == 4  # the two presses (one Promise.all), the summary poll, the state read
    assert block.count(", dblFail)") == 3, block.count(
        ", dblFail)"
    )  # the Promise.all, the summary poll, the state read
