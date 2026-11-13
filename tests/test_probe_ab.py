"""PROBE ONLY, DO NOT MERGE. Tests for the path-guard shapes of the Sonar measuring probe (PROBE-AB-V3-NOTE.md).

Shapes: the SPLIT, COMBINED, FIXED, INDEX, DICT and ITERATION helpers (tools/pathconfine.py), INLINE guards
(check_invariants, n8n_api and scrub_n8n_export), NAME-ONLY (check_invariants._drain_marker), and one positive
control (an equality check).
Made-up data only. The site tests run unchanged against the code before the probe (the base commit has no
tools/pathconfine.py): there the refusal assertions fail by behaviour (the old code opens any path); only the tests of
the helper functions themselves fail for the missing module."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from load_tools import check_invariants as ci
from load_tools import load_driver
from tools import scrub_n8n_export

REPO = Path(__file__).resolve().parent.parent
ROOTS_ENV = "LEAD_DESK_PATH_ROOTS"
HAS_HELPER = importlib.util.find_spec("tools.pathconfine") is not None
needs_helper = pytest.mark.skipif(not HAS_HELPER, reason="tools/pathconfine.py is part of the probe")


@pytest.fixture
def jail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(inside, outside): the only allowed folder is ``inside`` (plus the repository, which holds neither)."""
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    monkeypatch.chdir(inside)
    monkeypatch.setattr(tempfile, "tempdir", str(inside))
    monkeypatch.delenv(ROOTS_ENV, raising=False)
    if HAS_HELPER:
        monkeypatch.setattr("tools.pathconfine.REPO_ROOT", inside)
        monkeypatch.setattr("tools.pathconfine.FIXED_TEMP", str(inside))
    return inside, outside


def _write_all(folder: Path) -> None:
    (folder / "a.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (folder / "f.json").write_text(json.dumps({"intended_requests": 3, "qualifier": "live_claude"}), encoding="utf-8")
    (folder / "g.json").write_text(json.dumps({"intended_requests": 3, "qualifier": "fake"}), encoding="utf-8")
    (folder / "crm.json").write_text('[{"lead_id": "L1"}]', encoding="utf-8")
    (folder / "drain.json").write_text(json.dumps({"drained_ts": 7}), encoding="utf-8")


def _read_sites(p: Path) -> dict[str, object]:
    """The five checker functions, called with one path each; the value is the result or the exception class."""
    out: dict[str, object] = {}
    for name, fn in (
        ("jsonl", ci.read_jsonl),
        ("crm", ci.read_crm),
        ("intended", ci._intended),
        ("live", ci._facts_say_live),
    ):
        try:
            out[name] = fn(p)
        except ci.InputError:
            out[name] = ci.InputError
    return out


# ---- every shape: inside works, outside / .. / link out / prefix sibling are refused ----


def test_all_checker_sites_inside_then_outside(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    _write_all(inside)
    _write_all(outside)
    assert ci.read_jsonl(inside / "a.jsonl") == [{"a": 1}]  # SPLIT helper
    assert ci._intended(inside / "f.json") == 3  # COMBINED helper
    assert ci.read_crm(inside / "crm.json") == [{"lead_id": "L1"}]  # INLINE
    assert ci._facts_say_live(inside / "f.json") is True  # SPLIT-FIXED helper
    assert ci._facts_say_live(inside / "g.json") is False  # exists, says not live
    for name, fn in (
        ("a.jsonl", ci.read_jsonl),
        ("f.json", ci._intended),
        ("crm.json", ci.read_crm),
    ):
        for bad in (outside / name, Path("..") / "outside" / name):
            with pytest.raises(ci.InputError, match="outside"):
                fn(bad)
    # a refused facts path counts as "live" (fail closed: the model-required check goes on, never off); never read
    (outside / "g.json").write_text(json.dumps({"intended_requests": 3, "qualifier": "fake"}), encoding="utf-8")
    assert ci._facts_say_live(outside / "g.json") is True


def test_links_and_prefix_sibling_are_refused_at_every_checker_site(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    _write_all(outside)
    evil = inside.parent / (inside.name + "-evil")  # a folder whose name starts with the allowed folder's name
    evil.mkdir()
    _write_all(evil)
    for src in (outside, evil):
        for name, fn in (
            ("a.jsonl", ci.read_jsonl),
            ("f.json", ci._intended),
            ("crm.json", ci.read_crm),
        ):
            if src is outside and sys.platform != "win32":
                (inside / f"link-{name}").symlink_to(outside / name)
                target: Path = inside / f"link-{name}"
            else:
                target = src / name
            with pytest.raises(ci.InputError, match="outside"):
                fn(target)
    assert ci._facts_say_live(evil / "g.json") is True  # refused: fail closed


@pytest.mark.skipif(sys.platform == "win32", reason="NUL in a path is refused by the OS API on Windows differently")
def test_nul_in_a_path_is_a_clean_refusal_never_a_traceback(jail: tuple[Path, Path]) -> None:
    got = _read_sites(Path("a\x00b"))
    assert got["crm"] is ci.InputError
    assert got["live"] is True  # refused: fail closed
    assert got["jsonl"] is ci.InputError and got["intended"] is ci.InputError


def test_roots_from_the_environment_reach_the_inline_and_the_helper_sites(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = jail
    _write_all(outside)
    with pytest.raises(ci.InputError, match="outside"):
        ci.read_crm(outside / "crm.json")
    monkeypatch.setenv(ROOTS_ENV, str(outside))
    assert ci.read_crm(outside / "crm.json") == [{"lead_id": "L1"}]  # INLINE
    assert ci._facts_say_live(outside / "g.json") is True  # SPLIT-FIXED ignores the environment: refused, fail closed
    assert ci.read_jsonl(outside / "a.jsonl") == [{"a": 1}]  # SPLIT helper (environment roots)
    assert ci._intended(outside / "f.json") == 3  # COMBINED helper
    assert ci._probe_index_read(outside / "a.jsonl") > 0  # INDEX helper (environment roots)
    monkeypatch.setenv(ROOTS_ENV, "")  # an empty value adds nothing
    with pytest.raises(ci.InputError, match="outside"):
        ci.read_crm(outside / "crm.json")
    with pytest.raises(ci.InputError, match="outside"):
        ci._probe_index_read(outside / "a.jsonl")


# ---- scrub: SPLIT read (178, replicate) and INLINE write (179) ----


def test_scrub_read_is_split_helper_and_write_is_inline(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = jail
    wf = json.dumps({"name": "w", "nodes": []})
    (inside / "in.json").write_text(wf, encoding="utf-8")
    (outside / "in.json").write_text(wf, encoding="utf-8")
    assert scrub_n8n_export.main(["x", str(outside / "in.json"), "out.json"]) == 2
    assert scrub_n8n_export.main(["x", "in.json", str(outside / "out.json")]) == 2
    assert scrub_n8n_export.main(["x", "in.json", "../outside/out.json"]) == 2
    assert scrub_n8n_export.main(["x", "missing.json", "out.json"]) == 2  # a missing input is refused, not a traceback
    assert not (outside / "out.json").exists() and "refused" in capsys.readouterr().out
    assert scrub_n8n_export.main(["x", "in.json", "out.json"]) == 0
    assert json.loads((inside / "out.json").read_text(encoding="utf-8"))["name"] == "w"
    if sys.platform != "win32":
        (inside / "link-out.json").symlink_to(outside / "out.json")
        (inside / "link-in.json").symlink_to(outside / "in.json")
        assert scrub_n8n_export.main(["x", "in.json", "link-out.json"]) == 2
        assert scrub_n8n_export.main(["x", "link-in.json", "out.json"]) == 2
        assert not (outside / "out.json").exists()
        assert scrub_n8n_export.main(["x", "in.json", "a\x00b"]) == 2  # NUL: refused, no traceback
        assert scrub_n8n_export.main(["x", "a\x00b", "out.json"]) == 2
    # LEAD_DESK_PATH_ROOTS: honoured by the INLINE write (179) and by the SPLIT read (178)
    monkeypatch.setenv(ROOTS_ENV, str(outside))
    assert scrub_n8n_export.main(["x", "in.json", str(outside / "out.json")]) == 0
    assert (outside / "out.json").is_file()
    assert scrub_n8n_export.main(["x", str(outside / "in.json"), "out2.json"]) == 0


# ---- the helper functions themselves (they only exist in the probe) ----


@needs_helper
def test_inside_rejects_the_prefix_sibling_directly(tmp_path: Path) -> None:
    from tools.pathconfine import _inside

    root = tmp_path / "in"
    root.mkdir()
    assert _inside(str(root / "x"), str(root)) and _inside(str(root), str(root))
    assert not _inside(str(tmp_path / "in-evil" / "x"), str(root))
    assert not _inside(str(tmp_path), str(root))


@needs_helper
def test_five_helpers_on_existing_new_and_missing(jail: tuple[Path, Path]) -> None:
    from tools.pathconfine import (
        PathRefused,
        confine,
        confine_existing,
        confine_existing_fixed,
        confine_existing_ix,
        confine_iter,
    )

    inside, _ = jail
    (inside / "here.txt").write_text("x", encoding="utf-8")
    (inside / "odd name é.txt").write_text("x", encoding="utf-8")
    for fn in (confine, confine_iter):  # may name a new file
        assert fn("here.txt", what="w") == inside / "here.txt"
        assert fn("odd name é.txt", what="w") == inside / "odd name é.txt"  # existing: any name
        assert fn("new-1.txt", what="w") == inside / "new-1.txt"
        for bad in ("new name.txt", "néw.txt", "a;b", "...", "x" * 300):
            with pytest.raises(PathRefused):
                fn(bad, what="w")
    for efn in (confine_existing, confine_existing_fixed, confine_existing_ix):  # existing only
        assert efn("here.txt", what="r") == inside / "here.txt"
        assert efn(inside / "odd name é.txt", what="r") == inside / "odd name é.txt"
        with pytest.raises(PathRefused, match="no such file"):
            efn("new-1.txt", what="r")
    for fn2 in (confine, confine_iter, confine_existing, confine_existing_fixed, confine_existing_ix):
        for bad2 in ("", "  ", "a\x00b", "a\nb", "x" * 5000, "../outside/x", 5, None, b"here.txt"):
            with pytest.raises(PathRefused):
                fn2(bad2, what="w")  # type: ignore[arg-type]


@needs_helper
def test_dict_table_and_iteration_rebuild_agree_on_every_character() -> None:
    """The only difference between confine and confine_iter is how the new name is rebuilt: same results."""
    from tools.pathconfine import PathRefused, _new_name, _new_name_iter

    for name in (
        "a",
        "A-z_9.x",
        "x.",
        ".x",
        "é",
        "a b",
        "a/b",
        "a\\b",
        "a:b",
        "\u202e",
        "~",
        "a" * 255,
        "a" * 256,
        ".",
    ):
        try:
            want: str | type = _new_name(name, "w")
        except PathRefused:
            want = PathRefused
        try:
            got: str | type = _new_name_iter(name, "w")
        except PathRefused:
            got = PathRefused
        assert got == want, name
    for code in range(0x20, 0x250):
        c = chr(code)
        assert (_new_name("a" + c, "w") if c.isalnum() and c.isascii() or c in "._-" else None) == (
            _new_name_iter("a" + c, "w") if c.isalnum() and c.isascii() or c in "._-" else None
        )


@needs_helper
def test_fixed_roots_has_no_environment_and_existing_fixed_ignores_it(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.pathconfine import PathRefused, allowed_roots, confine_existing, confine_existing_fixed, fixed_roots

    inside, outside = jail
    (outside / "f.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv(ROOTS_ENV, str(outside))
    assert str(outside.resolve()) in allowed_roots() and str(outside.resolve()) not in fixed_roots()
    assert confine_existing(outside / "f.txt", what="r") == outside.resolve() / "f.txt"
    with pytest.raises(PathRefused, match="outside"):
        confine_existing_fixed(outside / "f.txt", what="r")


@needs_helper
def test_fixed_roots_never_ask_the_standard_library_for_the_temporary_folder(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No environment read in our code and none through ``tempfile``: the temporary folder is a constant."""
    from tools import pathconfine

    def boom() -> str:
        raise AssertionError("fixed_roots must not call tempfile.gettempdir")

    monkeypatch.setattr(tempfile, "gettempdir", boom)
    roots = pathconfine.fixed_roots()
    assert str(Path(pathconfine.FIXED_TEMP).resolve()) in roots
    with pytest.raises(AssertionError):
        pathconfine.allowed_roots()  # the other roots function does ask (one place only)


@needs_helper
def test_index_helper_agrees_with_confine_existing_everywhere(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INDEX helper and ``confine_existing`` differ only in how they pick the folder: same answer (path or the
    refusal) for every input, with and without an extra folder from the environment."""
    from tools.pathconfine import PathRefused, confine_existing, confine_existing_ix

    inside, outside = jail
    (inside / "sub").mkdir()
    (inside / "sub" / "f.txt").write_text("x", encoding="utf-8")
    (outside / "o.txt").write_text("x", encoding="utf-8")
    cases: list[object] = [
        "sub/f.txt",
        "sub",
        ".",
        "sub/../sub/f.txt",
        "sub/missing.txt",
        "missing/f.txt",
        "sub/f.txt/x",
        str(inside / "sub" / "f.txt"),
        str(outside / "o.txt"),
        "../outside/o.txt",
        "",
        " ",
        "a\x00b",
        "a\nb",
        "x" * 5000,
        7,
        None,
    ]
    if sys.platform != "win32":
        (inside / "ln").symlink_to(inside / "sub" / "f.txt")
        (inside / "ln-out").symlink_to(outside / "o.txt")
        (inside / "loop").symlink_to(inside / "loop")
        cases += ["ln", "ln-out", "loop", "loop/x"]
    for env in (None, str(outside)):
        if env is None:
            monkeypatch.delenv(ROOTS_ENV, raising=False)
        else:
            monkeypatch.setenv(ROOTS_ENV, env)
        for case in cases:
            outcomes = []
            for fn in (confine_existing, confine_existing_ix):
                try:
                    outcomes.append(("path", fn(case, what="w")))  # type: ignore[arg-type]
                except PathRefused as e:
                    outcomes.append(("refused", str(e)))
            assert outcomes[0] == outcomes[1], (env, case)


@needs_helper
def test_index_helper_refuses_a_prefix_sibling_and_picks_the_right_folder(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.pathconfine import PathRefused, confine_existing_ix

    inside, outside = jail
    evil = inside.parent / (inside.name + "-evil")
    evil.mkdir()
    (evil / "f.txt").write_text("x", encoding="utf-8")
    (outside / "o.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PathRefused, match="outside"):
        confine_existing_ix(evil / "f.txt", what="r")
    monkeypatch.setenv(ROOTS_ENV, os.pathsep.join([str(evil), str(outside)]))  # the LAST roots hold the file
    assert confine_existing_ix(outside / "o.txt", what="r") == outside.resolve() / "o.txt"
    assert confine_existing_ix(evil / "f.txt", what="r") == evil.resolve() / "f.txt"


def test_probe_index_read_and_the_positive_control(jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    inside, outside = jail
    _write_all(inside)
    _write_all(outside)
    assert ci._probe_index_read(inside / "a.jsonl") == len('{"a": 1}\n')
    for bad in (outside / "a.jsonl", Path("..") / "outside" / "a.jsonl", Path("a\x00b"), Path("nope.jsonl")):
        with pytest.raises(ci.InputError):
            ci._probe_index_read(bad)
    if not hasattr(ci, "_probe_control"):  # old code: nothing to measure here
        return
    folder = inside / "ctl"
    folder.mkdir()
    monkeypatch.setattr(ci, "DRAIN_DIR", str(folder))
    (folder / ci.CONTROL_NAMES[0]).write_text("12345", encoding="utf-8")
    assert ci._probe_control(Path(ci.CONTROL_NAMES[0])) == 5
    assert ci._probe_control(Path(ci.CONTROL_NAMES[1])) is None  # allowed name, no such file: no crash
    (folder / ci.CONTROL_NAMES[1]).write_text("123", encoding="utf-8")
    assert ci._probe_control(Path(ci.CONTROL_NAMES[1])) == 3  # both constants work
    for other in ("../outside/a.jsonl", "..", "a.jsonl", "", "readme.md", "a\x00b"):
        assert ci._probe_control(Path(other)) is None
    assert not (outside / "created").exists()


@needs_helper
def test_load_driver_run_refuses_outside_before_any_write(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    with pytest.raises(ValueError, match="outside"):  # ITERATION shape (log)
        load_driver.run("http://127.0.0.1:1", "burst:1", outside / "l.jsonl", inside / "f.json", 1, 1, 1, "p")
    with pytest.raises(ValueError, match="outside"):  # DICT shape (facts)
        load_driver.run("http://127.0.0.1:1", "burst:1", inside / "l.jsonl", outside / "f.json", 1, 1, 1, "p")
    for bad in ("new name.jsonl", "../outside/l.jsonl"):
        with pytest.raises(ValueError):
            load_driver.run("http://127.0.0.1:1", "burst:1", Path(bad), inside / "f.json", 1, 1, 1, "p")
    assert not list(outside.iterdir())
    assert not list(inside.iterdir())


def test_load_driver_run_refuses_outside_by_behaviour(jail: tuple[Path, Path]) -> None:
    """Runs on the old code too: there the driver starts a run and writes outside (the fail-before measurement)."""
    inside, outside = jail
    try:
        load_driver.run("http://127.0.0.1:1", "burst:1", outside / "l.jsonl", inside / "f.json", 1, 1, 1, "p")
    except Exception as e:  # noqa: BLE001  (any refusal; the point is that nothing is written outside)
        assert "outside" in str(e)
    assert not list(outside.iterdir())


# ---- NAME-ONLY shape: check_invariants._drain_marker ----


def test_drain_marker_name_only(jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    inside, outside = jail
    if not hasattr(ci, "DRAIN_DIR"):  # the old code: the path is used as typed
        (outside / "drain.json").write_text(json.dumps({"drained_ts": 7}), encoding="utf-8")
        with pytest.raises(ci.InputError, match="outside"):
            ci._drain_marker(outside / "drain.json")
        return
    folder = inside / "drain-dir"
    folder.mkdir()
    monkeypatch.setattr(ci, "DRAIN_DIR", str(folder))
    (folder / "drain.json").write_text(json.dumps({"drained_ts": 7}), encoding="utf-8")
    (outside / "drain.json").write_text(json.dumps({"drained_ts": 99}), encoding="utf-8")
    (outside / "only-outside.json").write_text(json.dumps({"drained_ts": 99}), encoding="utf-8")
    assert ci._drain_marker(None) is None
    assert ci._drain_marker(Path("drain.json")) == 7.0
    # only the NAME counts: the folder part of what was typed is dropped, so this reads the file in the folder
    assert ci._drain_marker(outside / "drain.json") == 7.0
    assert ci._drain_marker(Path("../outside/drain.json")) == 7.0
    for bad in (outside / "only-outside.json", Path("../outside/only-outside.json"), Path(".."), Path("a\x00b")):
        with pytest.raises(ci.InputError):
            ci._drain_marker(bad)
    if sys.platform != "win32":
        (folder / "link.json").symlink_to(outside / "only-outside.json")  # a link is the operator's own doing
        assert ci._drain_marker(Path("link.json")) == 99.0
    assert not (outside / "created").exists()


# ---- INLINE shape: n8n_api import ----


def test_n8n_import_inline(jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    inside, outside = jail
    (inside / "wf.json").write_text(json.dumps({"name": "made-up"}), encoding="utf-8")
    (outside / "wf.json").write_text(json.dumps({"name": "made-up"}), encoding="utf-8")
    (inside / "key.txt").write_text("made-up-key-for-the-test\n", encoding="utf-8")
    monkeypatch.setenv("N8N_API_KEY_FILE", str(inside / "key.txt"))
    monkeypatch.setenv("N8N_URL", "http://127.0.0.1:1")
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    sys.modules.pop("n8n_api", None)
    mod = importlib.import_module("n8n_api")
    sent: list[str] = []
    monkeypatch.setattr(mod, "call", lambda *a, **k: sent.append("call") or (200, {}))
    for bad in (str(outside / "wf.json"), "../outside/wf.json", "a\x00b"):
        with pytest.raises(SystemExit, match="cannot read a workflow"):
            mod.main(["n8n_api.py", "import", bad])
    assert sent == []
    assert mod.main(["n8n_api.py", "import", "wf.json"]) == 0
    assert sent == ["call"]
    monkeypatch.setenv(ROOTS_ENV, str(outside))  # LEAD_DESK_PATH_ROOTS reaches the inline guard
    assert mod.main(["n8n_api.py", "import", str(outside / "wf.json")]) == 0
    assert sent == ["call", "call"]
