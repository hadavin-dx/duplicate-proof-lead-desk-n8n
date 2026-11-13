"""A step of the wall clock must change no duration, deadline or check of the TOOLS. The wall clock can be corrected
while a run is going. Durations and deadlines use the monotonic clock;
the wall clock is kept only for calendar stamps, and every such line says so with `# wall-clock:` (guard test below).

One thing is wall-clock by necessity and is tested honestly, not hidden: the stamps the DESK writes into the lead book
(`updated_at`, `received_at`; `desk.default_now`, whole seconds) are calendar seconds, because they must survive a
restart and a monotonic clock does not. The stuck-row grace and the retry back-off of the reaper compare against them.
A forward step of at least the grace (30 s) therefore makes a live `sending` row look stuck, and a backward step makes
a stuck row look fresh until the clock catches up (`_restamp_future` then restarts its age). The tests at the end step
the desk clock forward and backward under the reaper and check the safe outcome: a keyed row is asked again with the
same key (no second email), a blind row goes to `unknown` and is never re-sent, and no row is lost. The cost is
unwanted work, never a second email: a follow-up put back five times in a row ends `failed`
(`docs/limits.md`, "What is not claimed")."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from support import CountingCrm, CrashAt, lead

from lead_desk.contract import SimulatedCrash

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_real_check as rrc  # noqa: E402

from load_tools import load_driver  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FOLDERS = ("src", "scripts", "tools", "load_tools", "demo_only", "tests")
# Every way to read the calendar clock: time.time / time_ns (also as a name), `from time import time`, datetime.now /
# utcnow / today, date.today, gmtime / localtime / ctime, formatdate, and file times (st_mtime, getmtime, ...). A line
# that really needs one says why with `# wall-clock:` on the same line.
WALL = re.compile(
    r"\btime\.time(_ns)?\b|\bfrom\s+time\s+import\b[^\n]*\btime(_ns)?\b|\bdatetime\.(now|utcnow|today)\("
    r"|\bdate\.today\(|\b(gm|local)time\(|\btime\.ctime\(|\bformatdate\(|\bst_[mca]time(_ns)?\b|\bget[mca]time\("
    # the module under another name (`import time as wall`, `from datetime import datetime as D`): the lines above would
    # not see `wall.time()`, so the renaming itself is flagged (nobody in this repository renames these modules)
    r"|\bimport\s+(time|datetime)\s+as\b|\bfrom\s+datetime\s+import\b[^\n]*\bas\b"
)


def _step_clock(monkeypatch: pytest.MonkeyPatch, delta: float, after: float) -> None:
    """From `after` real seconds on, the wall clock reads `delta` seconds off (monotonic and sleep stay true)."""
    real_time, start = time.time, time.monotonic()  # wall-clock: the step is added to the real calendar clock

    def stepped() -> float:
        return real_time() + (delta if time.monotonic() - start >= after else 0.0)

    monkeypatch.setattr(time, "time", stepped)


@pytest.mark.parametrize("delta", [-120.0, 3600.0])
def test_dry_run_survives_a_wall_clock_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], delta: float
) -> None:
    _step_clock(monkeypatch, delta, after=0.5)
    code = rrc.main(["--dry-fakes", "--assume-yes"])
    out = capsys.readouterr().out
    result = Path(out.split("result file: ")[1].splitlines()[0])
    data = json.loads(result.read_text(encoding="utf-8"))
    assert 0 <= data["summary"]["seconds_total"] < 600, data["summary"]["seconds_total"]
    assert data["summary"]["mail_last_poll_failed"] is False, [ln for ln in out.splitlines() if "mailbox" in ln][-12:]
    f6 = [c for c in data["checks"]["checks"] if c["id"] == "F6"]
    assert f6 and f6[0]["ok"], f6
    assert code == 0, out[-3000:]


@pytest.mark.parametrize("delta", [-120.0, 3600.0])
def test_retry_deadline_and_latency_ignore_a_wall_clock_step(monkeypatch: pytest.MonkeyPatch, delta: float) -> None:
    """Every attempt answers 503: the retries stop only when the MONOTONIC deadline is over (a forward wall-clock
    step must not end them early) and no latency is negative (a backward step). The deadline is 100 s and the steady
    fake clock moves 1.5 s per try, so the loop must make about 60 tries; a loop that counts its tries (five, six,
    ten) fails the bound below."""
    calls = {"n": 0}
    real_time = time.time  # wall-clock: the step is added to the real calendar clock

    def post(url: str, body: bytes) -> tuple[int | None, str | None, dict[str, Any], float | None]:
        calls["n"] += 1
        monkeypatch.setattr(time, "time", lambda: real_time() + delta)  # the clock steps during the first attempt
        return 503, None, {}, None

    ticks = {"t": 0.0}

    def mono() -> float:
        ticks["t"] += 0.5  # a steady clock: every read is half a second later
        return ticks["t"]

    monkeypatch.setattr(load_driver, "_post", post)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", mono)
    monkeypatch.setattr(load_driver, "REQUEST_DEADLINE_SECONDS", 100.0)
    item = {"request_id": "r", "door": "d", "intended_ts": real_time(), "scenario": "s", "kind": "new", "envelope": {}}
    line = load_driver.send_with_retries("http://127.0.0.1:1/leads", item)
    assert calls["n"] >= 50, (
        calls
    )  # a wall-clock step of +3600 s would have ended it after one try; a count loop sooner
    assert ticks["t"] < 400, ticks  # and it did stop at the monotonic deadline
    assert all(a["latency_s"] >= 0 for a in line["attempts"])


def test_wall_clock_is_only_used_for_calendar_stamps() -> None:
    """Guard: any read of the calendar clock in the code folders and the tests (WALL above) must say `# wall-clock:`
    on the same line (why a calendar time is needed); a line that is only a comment reads nothing. Durations,
    deadlines, timeouts and poll windows use the monotonic clock."""
    bad = []
    for folder in FOLDERS:
        for path in sorted((REPO / folder).rglob("*.py")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if WALL.search(line) and "# wall-clock:" not in line and not line.lstrip().startswith("#"):
                    bad.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert not bad, "use time.monotonic() for durations, or mark a calendar stamp with '# wall-clock:'\n" + "\n".join(
        bad
    )


@pytest.mark.parametrize(
    "line",
    [
        "x = time" + ".time()",
        "f = time" + ".time",
        "from time" + " import sleep, time",
        "d = datetime" + ".now()",
        "d = datetime.datetime" + ".utcnow()",
        "d = date" + ".today()",
        "n = os.stat(p).st_" + "mtime",
        "n = os.path.get" + "mtime(p)",
        "s = time.gm" + "time()",
        "m['Date'] = format" + "date()",
        "import time" + " as wall",
        "import datetime" + " as dt",
        "from date" + "time import date, datetime as D",
    ],
)
def test_the_wall_guard_sees_every_way_to_read_the_calendar(line: str) -> None:
    assert WALL.search(line), line


@pytest.mark.parametrize(
    "line",
    [
        "t = time.monotonic()",
        "time.sleep(1)",
        "from time import sleep",
        "x = runtime",
        "from datetime import timedelta",
    ],
)
def test_the_wall_guard_leaves_the_monotonic_clock_alone(line: str) -> None:
    assert not WALL.search(line), line


def test_a_wall_clock_step_does_not_move_the_load_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The open-loop schedule waits on the monotonic clock from one wall-clock reading: a step of the wall clock
    after that moves nothing (the schedule never sleeps until an absolute calendar time)."""
    base_wall, base_mono = 5000.0, time.monotonic()
    before = load_driver.seconds_until(base_wall + 100.0, base_wall, base_mono)
    real = time.time  # wall-clock: the step is added to the real calendar clock
    for delta in (-3600.0, 3600.0):
        monkeypatch.setattr(time, "time", lambda d=delta: real() + d)
        assert load_driver.seconds_until(base_wall + 100.0, base_wall, base_mono) == pytest.approx(before, abs=0.5)
    assert 99.0 < before <= 100.0


# ---- the desk's own persisted stamps ARE wall-clock: what a step does under the reaper
@pytest.mark.parametrize("kind", ["keyed", "blind"])
@pytest.mark.parametrize("step", ["forward", "backward"])
def test_a_desk_clock_step_under_the_reaper_is_safe(env, kind: str, step: str) -> None:
    """Crash at D2 (the email left, the result is not noted): the row stays `sending`. Then the desk clock steps
    forward by far more than the grace (the row looks stuck at once) or backward (it looks fresh, then ages normally).
    Keyed sender: the row goes back to pending and is asked again with the same key, still one email. Blind sender:
    the row goes to `unknown` and is never sent again. Either way: one lead, one CRM row, no row lost."""
    rig = env.rig(outbox=kind, hooks=CrashAt("D2"), crm=CountingCrm("keyed"))
    with pytest.raises(SimulatedCrash):
        rig.desk.handle("form", "clk-1", lead())
    assert rig.state()[0] == "sending" and rig.outbox.count == 1
    grace = rig.desk.stuck_grace_seconds
    if step == "forward":
        rig.clock.advance(grace + 5)
        assert rig.desk.reap_once()["requeued"] == 1
    else:
        rig.clock.advance(-3600)
        assert rig.desk.reap_once()["requeued"] == 0  # looks fresh: left alone, not lost
        assert rig.state()[0] == "sending"
        rig.clock.advance(grace + 5)  # the clock catches up with the stamp the reaper set
        assert rig.desk.reap_once()["requeued"] == 1
    for _ in range(4):  # a keyed row waits out its back-off, then is asked again
        rig.clock.advance(60)
        rig.desk.reap_once()
    state, err = rig.state()
    assert rig.leads() == 1 and rig.crm.count == 1 and rig.outbox.count == 1
    if kind == "keyed":
        assert state == "sent", (state, err)
    else:
        assert state == "unknown" and err == "crash_while_sending"


def test_the_logged_lag_ignores_a_wall_clock_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The worker logs `lag_s` from the monotonic schedule: a wall-clock step after the schedule started (here one hour
    forward) must not show up as lag, and the request still goes out only at its intended moment."""
    real = time.time  # wall-clock: the step is added to the real calendar clock
    item = {"request_id": "r1", "door": "form", "intended_ts": real() + 0.3, "scenario": "s", "kind": "first"}
    sent_mono: list[float] = []

    def fake_send(url: str, it: dict[str, Any]) -> dict[str, Any]:
        sent_mono.append(time.monotonic())
        return {"request_id": it["request_id"], "lag_s": 99999.0}

    monkeypatch.setattr(load_driver, "send_with_retries", fake_send)
    part = tmp_path / "part"
    start = time.monotonic()
    real_sleep = time.sleep

    def sleep_then_step(seconds: float) -> None:
        real_sleep(seconds)
        monkeypatch.setattr(time, "time", lambda: real() + 3600.0)  # the clock steps while the worker waits

    monkeypatch.setattr(time, "sleep", sleep_then_step)
    load_driver._worker((0, "http://127.0.0.1:1", [item], 1, part))
    line = json.loads(part.read_text(encoding="utf-8").splitlines()[0])
    assert -0.01 <= line["lag_s"] < 0.5
    assert sent_mono[0] - start >= 0.29
