"""Crash points: every name is accepted by the shared crash hook, is really called by the desk, and a crash inside the
start-up sweep itself is recovered by the next start. Made-up data only."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
from support import CountingCrm, CrashAt, child_env, lead

from lead_desk.contract import SimulatedCrash
from lead_desk.hooks import ALL_HOOK_POINTS, HOOK_POINTS, MORE_HOOK_POINTS, Hooks

REPO = Path(__file__).resolve().parents[1]
_CHILD = (
    "import sys; from pathlib import Path; from lead_desk.hooks import FlagFileHooks; "
    "h = FlagFileHooks(Path(sys.argv[1])); h.crash_point(sys.argv[2], None); print('survived')"
)


def test_the_point_lists_are_consistent() -> None:
    assert ALL_HOOK_POINTS == (*HOOK_POINTS, *MORE_HOOK_POINTS)
    assert len(set(ALL_HOOK_POINTS)) == len(ALL_HOOK_POINTS)


@pytest.mark.parametrize("point", ALL_HOOK_POINTS)
def test_flag_file_hooks_kill_at_every_named_point(tmp_path: Path, point: str) -> None:
    """The flag names `point`: the process ends with 137 and the one-shot flag is gone; another name does nothing."""
    flag = tmp_path / "crash.flag"
    flag.write_text(point, encoding="utf-8")
    other = next(p for p in ALL_HOOK_POINTS if p != point)
    calm = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHILD, str(flag), other], env=child_env(), capture_output=True, text=True, timeout=60
    )
    assert calm.returncode == 0 and "survived" in calm.stdout and flag.exists()
    hit = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHILD, str(flag), point], env=child_env(), capture_output=True, text=True, timeout=60
    )
    assert hit.returncode == 137, hit.stderr
    assert "simulate crash" in hit.stdout and "survived" not in hit.stdout
    assert not flag.exists()


def test_every_name_in_the_desk_is_accepted_and_every_accepted_name_is_called() -> None:
    """A renamed or new point in desk.py that the lists do not know (or a listed point the desk never calls) fails."""
    tree = ast.parse((REPO / "src" / "lead_desk" / "desk.py").read_text(encoding="utf-8"))
    called = {
        str(n.args[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "crash_point"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    }
    assert called == set(ALL_HOOK_POINTS)


def test_crash_script_uses_the_shared_hook_and_no_names_of_its_own() -> None:
    """scripts/crash_desk.py has no name check of its own: it only hands FlagFileHooks to the server."""
    text = (REPO / "scripts" / "crash_desk.py").read_text(encoding="utf-8")
    assert "FlagFileHooks(flag)" in text and "main(hooks=" in text
    assert not re.search(r"""["'](A2?|B|L|C[01]|D[123]b?)["']""", text)


class _Recorder(Hooks):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def crash_point(self, name: str, conn: object) -> None:
        self.seen.append(name)


@pytest.mark.parametrize("crm", ["keyed", "blind"])
def test_one_ordinary_lead_passes_every_point_once(env, crm: str) -> None:
    rec = _Recorder()
    rig = env.rig(outbox="keyed", hooks=rec, crm=CountingCrm(crm))
    rig.desk.handle("form", "sub-1", lead())
    assert rec.seen == ["A", "A2", "B", "L", "C0", "C1", "D1", "D1b", "D2", "D3"]
    assert set(rec.seen) == set(ALL_HOOK_POINTS) - {"N1"}  # N1 is a reminder point: test_reminder_crash_gives_unknown


# -- a second crash inside startup() itself
@pytest.mark.parametrize("crm", ["keyed", "blind"])
@pytest.mark.parametrize("kind", ["keyed", "blind"])
@pytest.mark.parametrize(("first", "during"), [("A", "A2"), ("B", "D1"), ("B", "C0"), ("B", "D2")])
def test_crash_during_the_startup_sweep_is_recovered_by_the_next_start(
    env, kind: str, first: str, during: str, crm: str
) -> None:
    """First crash at `first`; the restart then crashes AGAIN at `during` while startup() is still sweeping (open
    receipts, then pending follow-ups); a third start finishes: one lead, one CRM row, never a second email."""
    rig = env.rig(outbox=kind, hooks=CrashAt(first), crm=CountingCrm(crm))
    with pytest.raises(SimulatedCrash):
        rig.desk.handle("form", "sub-1", lead())
    second = CrashAt(during)
    with pytest.raises(SimulatedCrash):
        rig.restart(hooks=second)  # the crash happens INSIDE startup()
    assert second.fired == [during]
    rig.restart()
    assert rig.leads() == 1 and len(rig.crm.calls) <= 1, rig.crm.calls
    assert rig.outbox.count <= 1
    state, err = rig.state()
    if crm == "blind" and during == "C0":  # a blind CRM is not asked again after a crash: a person looks
        assert (state, err, rig.crm.count, rig.outbox.count) == ("unknown", "crm_unconfirmed", 0, 0)
        return
    assert rig.crm.count == 1
    if state == "unknown":  # a blind sender killed at D2 may have sent: a person looks, nothing is sent again
        assert kind == "blind" and during == "D2" and err == "crash_while_sending" and rig.outbox.count == 1
    else:
        assert (state, rig.outbox.count) == ("sent", 1)
    again = rig.desk.handle("form", "sub-1", lead())
    assert again.body["replayed"] is True and rig.outbox.count <= 1
