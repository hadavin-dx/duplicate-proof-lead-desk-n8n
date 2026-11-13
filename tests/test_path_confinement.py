"""Path confinement for every command-line path (tools/pathconfine.py) and the tools that use it.
Class: a tool opened whatever path it was given, so ``..``, an absolute path or a link out of the work folder was read
or overwritten. Each tool test below fails on the code before the fix (the tool opened the file outside the folder).
Made-up data only; the "outside" folder is a sibling folder of the jail in the test's own temporary folder."""

from __future__ import annotations

import ast
import importlib
import json
import ntpath
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types
import unicodedata
from pathlib import Path
from typing import Any

import pytest

import tools.pathconfine as pc
from load_tools import check_invariants as ci
from load_tools import load_driver
from tests.support import start_proc, stop_proc
from tools import scrub_n8n_export

REPO = Path(__file__).resolve().parent.parent
SECRET = "made-up outside text"


@pytest.fixture
def jail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(inside, outside): the only allowed folder is ``inside``; ``outside`` is next to it and is not allowed."""
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    monkeypatch.chdir(inside)
    monkeypatch.setattr(pc.tempfile, "tempdir", str(inside))
    monkeypatch.setattr(pc, "REPO_ROOT", inside)
    monkeypatch.delenv(pc.ROOTS_ENV, raising=False)
    return inside, outside


# ---------- the helper ----------


def test_inside_path_is_accepted_and_returned_resolved(jail: tuple[Path, Path]) -> None:
    inside, _ = jail
    (inside / "a.jsonl").write_text("x", encoding="utf-8")
    assert pc.confine("a.jsonl", what="f") == (inside / "a.jsonl").resolve()
    assert pc.confine(inside / "sub" / ".." / "a.jsonl", what="f") == (inside / "a.jsonl").resolve()
    assert pc.confine_existing("a.jsonl", what="f") == (inside / "a.jsonl").resolve()
    out = pc.confine(inside / "new-output.json", what="f")  # an output need not exist
    assert out == inside.resolve() / "new-output.json"
    with pytest.raises(pc.PathRefused, match="no such file"):  # a file that is read must exist
        pc.confine_existing(inside / "new-output.json", what="f")


def test_dot_dot_escape_is_refused(jail: tuple[Path, Path]) -> None:
    with pytest.raises(pc.PathRefused, match="outside the allowed"):
        pc.confine("../outside/secret.txt", what="--log")


def test_absolute_path_outside_is_refused(jail: tuple[Path, Path]) -> None:
    _, outside = jail
    with pytest.raises(pc.PathRefused, match="--log"):
        pc.confine(outside / "secret.txt", what="--log")


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symbolic link needs a privilege on Windows")
def test_symlink_that_leaves_the_folder_is_refused(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    (inside / "link").symlink_to(outside / "secret.txt")
    (inside / "dirlink").symlink_to(outside, target_is_directory=True)
    with pytest.raises(pc.PathRefused):
        pc.confine(inside / "link", what="f")
    with pytest.raises(pc.PathRefused):
        pc.confine(inside / "dirlink" / "new.json", what="f")


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", "a\nb", "a\x1b[0m", "a" * (pc.MAX_PATH_CHARS + 1)])
def test_odd_input_is_refused(jail: tuple[Path, Path], bad: str) -> None:
    with pytest.raises(pc.PathRefused):
        pc.confine(bad, what="f")


@pytest.mark.parametrize("fn", ["confine", "confine_existing", "confine_anywhere"])
@pytest.mark.parametrize("empty", ["", " ", "   "])
def test_empty_or_blank_text_is_refused_as_empty_by_every_function(
    jail: tuple[Path, Path], fn: str, empty: str
) -> None:
    with pytest.raises(pc.PathRefused, match="empty path"):
        getattr(pc, fn)(empty, what="f")


def test_not_a_path_is_refused(jail: tuple[Path, Path]) -> None:
    with pytest.raises(pc.PathRefused, match="not a path"):
        pc.confine(None, what="f")  # type: ignore[arg-type]
    with pytest.raises(pc.PathRefused, match="not a path"):
        pc.confine(b"bytes", what="f")  # type: ignore[arg-type]


def test_refusal_never_prints_the_value(jail: tuple[Path, Path]) -> None:
    with pytest.raises(pc.PathRefused) as err:
        pc.confine("../outside/very-private-name.txt", what="--log")
    assert "very-private-name" not in str(err.value)


def test_a_prefix_sibling_is_not_inside(jail: tuple[Path, Path]) -> None:
    inside, _ = jail
    sibling = inside.parent / (inside.name + "-evil")
    sibling.mkdir()
    with pytest.raises(pc.PathRefused):
        pc.confine(sibling / "f", what="f")


def test_extra_folder_from_the_environment(jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _, outside = jail
    monkeypatch.setenv(pc.ROOTS_ENV, str(outside))
    assert pc.confine(outside / "ok.json", what="f").parent == outside.resolve()
    (outside / "have.json").write_text("{}", encoding="utf-8")
    assert pc.confine_existing(outside / "have.json", what="f") == outside.resolve() / "have.json"


def test_sqlite_uri_encodes_query_and_fragment_characters(jail: tuple[Path, Path]) -> None:
    inside, _ = jail
    name = "bo?ok#x&mode=rw.db"
    if sys.platform == "win32":  # a question mark cannot be in a Windows file name
        name = "bo#ok&mode=rw.db"
    path = inside / name
    sqlite3.connect(path).close()
    uri = pc.readonly_sqlite_uri(path)
    assert uri.endswith("?mode=ro") and uri.count("?") == 1 and "#" not in uri and "mode=rw" not in uri.split("?")[0]
    conn = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):  # really read-only
            conn.execute("CREATE TABLE t (a)")
    finally:
        conn.close()


# ---------- review findings V-1 to V-5 (rv13) ----------


def _scandir_refusing(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    """Make listing ``folder`` fail as it does for a folder that can be entered but not read (mode 0711)."""
    real_scandir = os.scandir
    target = os.path.realpath(folder)

    def scandir(path: Any = ".") -> Any:
        if os.path.realpath(path) == target:
            raise PermissionError(13, "listing not allowed")
        return real_scandir(path)

    monkeypatch.setattr(pc.os, "scandir", scandir)


def test_v1_anywhere_refuses_a_file_below_a_folder_that_cannot_be_listed_and_says_so(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fallback: the result always comes from a listing (what the engine accepts); the cost is this refusal."""
    _, outside = jail
    cfg = outside / "a" / "b" / "cfg.env"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("A=1\n", encoding="utf-8")
    assert pc.confine_anywhere(cfg, what="--config") == cfg.resolve()  # listable: read from anywhere
    _scandir_refusing(monkeypatch, outside / "a")
    with pytest.raises(pc.PathRefused, match="cannot be listed"):
        pc.confine_anywhere(cfg, what="--config")
    with pytest.raises(pc.PathRefused, match="cannot be listed"):
        pc.confine_existing(cfg, what="--config", roots=[outside])


_AS_NOBODY = """
import os, sys
sys.dont_write_bytecode = True
os.setgid(65534); os.setuid(65534)
sys.path.insert(0, sys.argv[1])
import tools.pathconfine as pc
try:
    print(pc.confine_anywhere(sys.argv[2], what="--config").read_text(encoding="utf-8").strip())
except pc.PathRefused as e:
    print("refused:", e)
"""


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to start as another user")
def test_v1_a_config_below_a_traverse_only_folder_is_refused_with_a_message_as_an_unprivileged_user() -> None:
    top = Path(tempfile.mkdtemp(prefix="pathconfine-"))  # not tmp_path: pytest keeps that folder private (0700)
    try:
        cfg = top / "a" / "b" / "cfg.env"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("A=1\n", encoding="utf-8")
        cfg.chmod(0o644)
        listable = top / "c.env"
        listable.write_text("B=2\n", encoding="utf-8")
        listable.chmod(0o644)
        for folder, mode in ((top, 0o755), (top / "a", 0o711), (top / "a" / "b", 0o755)):
            folder.chmod(mode)
        for parent in top.parents:  # the unprivileged user must be able to enter every folder above
            if parent.stat().st_mode & 0o001 == 0:
                pytest.skip("a folder above the temporary folder cannot be entered by other users")
        outs = []
        for target in (cfg, listable):
            proc = start_proc(["-I", "-c", _AS_NOBODY, str(REPO), str(target)], dict(os.environ))
            try:
                code = proc.wait(timeout=60)
            finally:
                out = stop_proc(proc).decode("utf-8", "replace")
            assert code == 0, out
            outs.append(out.strip())
        assert outs[0] == "refused: --config: a folder on the path cannot be listed"
        assert outs[1] == "B=2"
    finally:
        shutil.rmtree(top, ignore_errors=True)


def test_v1_a_name_the_listing_does_not_show_is_refused_not_opened() -> None:
    if not os.path.isdir("/dev/fd"):
        pytest.skip("no /dev/fd on this system")
    read_end, write_end = os.pipe()  # what `--config <(command)` hands over
    try:
        os.write(write_end, b"A=1\n")
        os.close(write_end)
        write_end = -1
        with pytest.raises(pc.PathRefused):  # the raw argument is never used as a fallback
            pc.confine_anywhere(f"/dev/fd/{read_end}", what="--config")
    finally:
        os.close(read_end)
        if write_end >= 0:
            os.close(write_end)


@pytest.mark.parametrize("name", ["my log.jsonl", "café.jsonl", "a?b", "x" * 300])
def test_v2_a_missing_file_that_is_read_says_no_such_file(jail: tuple[Path, Path], name: str) -> None:
    with pytest.raises(pc.PathRefused, match="no such file") as err:
        pc.confine_existing(name, what="--requests")
    assert "letters" not in str(err.value)
    with pytest.raises(pc.PathRefused, match="letters|not usable"):  # the same name as a NEW file keeps its message
        pc.confine(name, what="--json")


def test_v2_check_invariants_names_the_missing_reader_file(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    inside, _ = jail
    args = _ci_args(inside) | {"--requests": "my log.jsonl"}
    assert ci.main([w for pair in args.items() for w in pair]) == ci.EXIT_INPUT
    assert "--requests: no such file" in capsys.readouterr().err


def test_v2_an_existing_name_with_a_control_character_is_refused_for_everyone(jail: tuple[Path, Path]) -> None:
    inside, _ = jail
    odd = inside / "nl\nname"
    try:
        odd.write_text("x", encoding="utf-8")
    except OSError:
        pytest.skip("this file system cannot store a newline in a name")
    with pytest.raises(pc.PathRefused, match="control character"):
        pc.confine(str(odd), what="f")
    with pytest.raises(pc.PathRefused, match="control character"):
        pc.confine_existing(str(odd), what="f")
    assert "any name except one with a control character" in (REPO / "load_tools" / "README.md").read_text("utf-8")
    assert "any name except one with a control character" in (REPO / "tools" / "pathconfine.py").read_text("utf-8")


DEVICE_STEMS = ["con", "prn", "aux", "nul", *[f"com{n}" for n in range(10)], *[f"lpt{n}" for n in range(10)]]


@pytest.mark.parametrize("stem", DEVICE_STEMS)
@pytest.mark.parametrize("variant", ["{}", "{}.txt", "{}.tar.gz", "{}."])
def test_v3_a_new_name_may_not_be_a_windows_device_name(stem: str, variant: str) -> None:
    for text in (stem, stem.upper(), stem.capitalize()):
        name = variant.format(text)
        with pytest.raises(pc.PathRefused, match="device|dot"):
            pc._new_name(name, "--json")


@pytest.mark.skipif(not hasattr(ntpath, "isreserved"), reason="ntpath.isreserved needs Python 3.13")
def test_v3_our_device_list_covers_what_python_says_windows_reserves() -> None:
    reserved = [
        n
        for n in (f"{s}{e}" for s in ("con", "prn", "aux", "nul", "com", "lpt") for e in "0123456789")
        if ntpath.isreserved(n)
    ]
    assert set(reserved) <= set(DEVICE_STEMS)  # ours is a superset: com0 and lpt0 are on Microsoft's list, not Python's
    for stem in DEVICE_STEMS:
        assert not ntpath.isreserved(f"x{stem}.txt")
        if stem not in ("com0", "lpt0"):  # Python 3.13 does not know these two
            assert ntpath.isreserved(f"{stem}.txt")
    for name in ("console.json", "nulled", "com10", "lpt10.txt", "auxiliary.log", "a.nul", "com.json", "con-1.json"):
        assert pc._new_name(name, "--json") == name  # similar, but not a device


@pytest.mark.parametrize("name", ["new.", "new..", "a.b."])
def test_v3_a_new_name_that_ends_with_a_dot_is_refused(jail: tuple[Path, Path], name: str) -> None:
    with pytest.raises(pc.PathRefused, match="dot"):
        pc.confine(name, what="--json")
    # an EXISTING file of that name (Linux) is still read: the rule is about NEW names only
    if sys.platform != "win32":
        (Path.cwd() / name).write_text("x", encoding="utf-8")
        assert pc.confine(name, what="--json").name == name
        assert pc.confine_existing(name, what="--json").name == name


def test_v3_a_device_named_output_is_refused_before_anything_is_created(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    inside, _ = jail
    args = _ci_args(inside) | {"--json": "nul.json"}
    assert ci.main([w for pair in args.items() for w in pair]) == ci.EXIT_INPUT
    assert "--json:" in capsys.readouterr().err and not (inside / "nul.json").exists()


def _entry(name: str, path: str) -> Any:
    class Entry:
        def __init__(self) -> None:
            self.name, self.path = name, path

        def is_symlink(self) -> bool:
            return False

        def is_dir(self, follow_symlinks: bool = True) -> bool:
            return False

    return Entry()


def test_v4_a_fake_macos_listing_is_matched_in_the_other_unicode_form(monkeypatch: pytest.MonkeyPatch) -> None:
    nfd = unicodedata.normalize("NFD", "café.json")  # the form macOS stores
    nfc = unicodedata.normalize("NFC", "café.json")
    assert nfd != nfc
    listing = [_entry("other.json", "/x/other.json"), _entry(nfd, "/x/" + nfd)]
    assert pc._match(listing, nfc) is listing[1]  # typed NFC, stored NFD
    assert pc._match(listing, nfd) is listing[1]
    assert pc._match(listing, "cafe.json") is None  # not the same name
    both = [_entry(nfc, "/x/" + nfc), _entry(nfd, "/x/" + nfd)]  # Linux can hold both: the exact one wins
    assert pc._match(both, nfd) is both[1] and pc._match(both, nfc) is both[0]


def test_v4_confine_finds_a_file_and_a_folder_typed_in_the_other_unicode_form(jail: tuple[Path, Path]) -> None:
    inside, _ = jail
    nfd_dir = unicodedata.normalize("NFD", "dossié")
    nfd_file = unicodedata.normalize("NFD", "café.json")
    try:
        (inside / nfd_dir).mkdir()
        (inside / nfd_dir / nfd_file).write_text("{}", encoding="utf-8")
        stored_as_typed = (inside / nfd_dir / nfd_file).exists() and not (
            inside / unicodedata.normalize("NFC", "dossié")
        ).exists()
    except OSError:
        pytest.skip("this file system cannot store these names")
    if not stored_as_typed:
        pytest.skip("this file system folds the two Unicode forms itself")
    typed = unicodedata.normalize("NFC", "dossié") + "/" + unicodedata.normalize("NFC", "café.json")
    got = pc.confine(typed, what="--requests")
    assert got == inside.resolve() / nfd_dir / nfd_file and got.read_text(encoding="utf-8") == "{}"


def test_v5_inside_compares_whole_folders_not_text_prefixes() -> None:
    root = os.path.realpath(os.sep + "base" + os.sep + "in")
    sibling = os.path.realpath(os.sep + "base" + os.sep + "in-evil" + os.sep + "x")
    assert pc._inside(sibling, root) is False  # a text prefix, not a folder: startswith would say True
    assert pc._inside(os.path.join(root, "sub", "x"), root) is True
    assert pc._inside(root, root) is True
    assert pc._inside(os.path.realpath(os.sep + "base"), root) is False


def test_v5_the_path_length_limit_is_checked_before_resolving(jail: tuple[Path, Path]) -> None:
    long_path = "/".join(["d"] * (pc.MAX_PATH_CHARS // 2 + 1))  # many short names: no single name is too long
    assert len(long_path) > pc.MAX_PATH_CHARS
    with pytest.raises(pc.PathRefused, match="too long"):
        pc.confine(long_path, what="f")
    at_limit = "/".join(["d"] * (pc.MAX_PATH_CHARS // 2))[: pc.MAX_PATH_CHARS]
    assert len(at_limit) <= pc.MAX_PATH_CHARS
    with pytest.raises(pc.PathRefused) as err:
        pc.confine(at_limit, what="f")
    assert "too long" not in str(err.value)  # refused for another reason (the folders do not exist)


@pytest.mark.parametrize("kind", ["confine", "confine_existing", "confine_anywhere"])
def test_v5_a_file_in_the_middle_of_the_path_is_refused_as_not_a_folder(jail: tuple[Path, Path], kind: str) -> None:
    inside, _ = jail
    (inside / "f.txt").write_text("x", encoding="utf-8")
    with pytest.raises(pc.PathRefused, match="is not a folder"):
        getattr(pc, kind)(inside / "f.txt" / "x", what="f")


def test_v5_the_listing_match_ignores_case_where_the_system_does(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, _ = jail
    (inside / "sub").mkdir()
    (inside / "sub" / "f.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pc.os.path, "normcase", str.lower)  # what ntpath.normcase does on Windows
    assert pc.confine("Sub/F.JSON", what="f") == inside.resolve() / "sub" / "f.json"
    monkeypatch.setattr(pc.os.path, "normcase", lambda s: s)  # what posixpath does: case matters
    with pytest.raises(pc.PathRefused, match="does not exist"):
        pc.confine("Sub/f.json", what="f")


# ---------- the tools ----------


CI_FLAGS = ("--requests", "--book", "--catcher", "--crm", "--model-log", "--drain", "--facts", "--json")


def _ci_args(inside: Path) -> dict[str, str]:
    for name in ("r.jsonl", "b.db", "c.jsonl", "m.json", "l.jsonl", "d.json", "f.json"):  # inputs must exist
        (inside / name).touch()
    return {
        "--requests": "r.jsonl",
        "--book": "b.db",
        "--catcher": "c.jsonl",
        "--crm": "m.json",
        "--model-log": "l.jsonl",
        "--drain": "d.json",
        "--facts": "f.json",
        "--json": "out.json",
    }


@pytest.mark.parametrize("flag", CI_FLAGS)
def test_check_invariants_refuses_each_path_outside_before_reading_anything(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    inside, outside = jail
    opened: list[str] = []
    for name in ("read_jsonl", "read_crm", "read_book", "_intended", "_drain_marker", "_facts_say_live", "run_checks"):
        monkeypatch.setattr(ci, name, lambda *a, _n=name, **k: opened.append(_n))
    args = _ci_args(inside) | {flag: str(outside / "x.json")}
    code = ci.main([w for pair in args.items() for w in pair])
    err = capsys.readouterr().err
    assert code == ci.EXIT_INPUT and f"{flag}: path is outside" in err
    assert opened == [] and not (outside / "x.json").exists() and not (inside / "out.json").exists()


def test_check_invariants_main_reads_and_writes_inside(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, _ = jail
    for name in ("r.jsonl", "c.jsonl", "l.jsonl"):
        (inside / name).write_text('{"a": 1}\n', encoding="utf-8")
    (inside / "m.json").write_text('[{"lead_id": "L1"}]', encoding="utf-8")
    (inside / "r.jsonl.facts.json").write_text(
        json.dumps({"intended_requests": 3, "qualifier": "live_claude"}), encoding="utf-8"
    )
    (inside / "d.json").write_text(json.dumps({"drained_ts": 7}), encoding="utf-8")
    seen: list[tuple[Any, ...]] = []

    def fake_checks(*a: Any) -> dict[str, Any]:
        seen.append(a)
        return {"ok": True}

    monkeypatch.setattr(ci, "read_book", lambda p: {"book": p.name})
    monkeypatch.setattr(ci, "run_checks", fake_checks)
    monkeypatch.setattr(ci, "render_table", lambda r: "table")
    monkeypatch.delenv("DESK_QUALIFIER", raising=False)
    args = _ci_args(inside) | {"--facts": "r.jsonl.facts.json"}
    assert ci.main([w for pair in args.items() for w in pair]) == ci.EXIT_OK
    rows, book, mail, crm, model, drain, *_ = seen[0]
    assert rows == mail == model == [{"a": 1}] and crm == [{"lead_id": "L1"}] and drain == 7.0
    assert book == {"book": "b.db"} and seen[0][10] == 3 and seen[0][12] is True  # intended 3; model required (live)
    assert (inside / "out.json").is_file()
    # the facts file defaults to <requests>.facts.json
    for flag in ("--facts", "--json"):
        del args[flag]
    assert ci.main([w for pair in args.items() for w in pair]) == ci.EXIT_OK
    assert capsys.readouterr().err == ""


def test_the_book_reader_uses_the_helper_for_a_name_with_a_question_mark(jail: tuple[Path, Path]) -> None:
    """The old connection string was f"file:{path}?mode=ro": a book named ``x?mode=rw.db`` changed its own options."""
    inside, _ = jail
    book = inside / "x?mode=rw.db"
    conn = sqlite3.connect(book)
    conn.execute("CREATE TABLE receipts (receipt_id INTEGER, source TEXT, source_ref TEXT, outcome TEXT)")
    conn.execute("CREATE TABLE leads (lead_id TEXT, email_key TEXT, email TEXT, name TEXT)")
    conn.execute("CREATE TABLE followups (lead_id TEXT, state TEXT, crm_done INT, updated_at INT, last_error TEXT)")
    conn.execute("INSERT INTO leads VALUES ('L1', 'k', 'a@example.test', 'n')")
    conn.commit()
    conn.close()
    assert ci.read_book(book)["leads"] == ["L1"]
    uri = pc.readonly_sqlite_uri(book)
    assert "%3F" in uri and uri.endswith("?mode=ro")  # the question mark of the name is encoded
    ro = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO leads VALUES ('L2', 'k', 'b@example.test', 'n')")
    finally:
        ro.close()


def test_load_driver_run_refuses_log_and_facts_outside(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    with pytest.raises(pc.PathRefused):
        load_driver.run("http://127.0.0.1:1", "burst:1", outside / "log.jsonl", inside / "f.json", 1, 1, 1, "p")
    with pytest.raises(pc.PathRefused):
        load_driver.run("http://127.0.0.1:1", "burst:1", inside / "log.jsonl", outside / "f.json", 1, 1, 1, "p")
    assert not list(outside.iterdir())


def test_load_driver_main_writes_nothing_outside_and_prints_a_refusal(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _, outside = jail
    args = ["--base-url", "http://127.0.0.1:1", "--scenario", "burst:1"]
    assert load_driver.main([*args, "--log", str(outside / "l.jsonl")]) == 2  # no traceback
    assert load_driver.main([*args, "--log", "l.jsonl", "--facts", str(outside / "f.json")]) == 2
    err = capsys.readouterr().err
    assert "ERROR: --log: path is outside" in err and "ERROR: --facts: path is outside" in err
    assert not list(outside.iterdir())


def test_scrub_refuses_outside_in_and_out(jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    inside, outside = jail
    (outside / "in.json").write_text(json.dumps({"name": "w", "nodes": []}), encoding="utf-8")
    (inside / "in.json").write_text(json.dumps({"name": "w", "nodes": []}), encoding="utf-8")
    assert scrub_n8n_export.main(["x", str(outside / "in.json"), str(inside / "out.json")]) == 2
    assert not (inside / "out.json").exists()
    assert scrub_n8n_export.main(["x", str(inside / "in.json"), str(outside / "out.json")]) == 2
    assert not (outside / "out.json").exists()
    captured = capsys.readouterr()
    assert "refused" in captured.err and captured.out == ""  # an error goes to the error stream, not to the output
    assert scrub_n8n_export.main(["x"]) == 2
    captured = capsys.readouterr()
    assert "usage:" in captured.err and captured.out == ""
    assert scrub_n8n_export.main(["x", str(inside / "in.json"), str(inside / "out.json")]) == 0
    assert (inside / "out.json").is_file()


def test_n8n_api_import_refuses_a_file_outside(jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    inside, outside = jail
    (outside / "wf.json").write_text(json.dumps({"name": "made-up"}), encoding="utf-8")
    (inside / "key.txt").write_text("made-up-key-for-the-test\n", encoding="utf-8")
    monkeypatch.setenv("N8N_API_KEY_FILE", str(inside / "key.txt"))
    monkeypatch.setenv("N8N_URL", "http://127.0.0.1:1")
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    sys.modules.pop("n8n_api", None)
    mod = importlib.import_module("n8n_api")
    sent: list[str] = []
    monkeypatch.setattr(mod, "call", lambda *a, **k: sent.append("call") or (200, {}))
    with pytest.raises(SystemExit, match="cannot read a workflow"):
        mod.main(["n8n_api.py", "import", str(outside / "wf.json")])
    assert sent == []  # refused before any request


def _rrc(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    return importlib.import_module("run_real_check")


def test_run_real_check_reads_a_config_outside_the_allowed_folders(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented use (docs/real-services.md step 1): the config is kept in a folder outside the repository."""
    inside, outside = jail
    (outside / "run.env").write_text("REAL_PERSONS=3\n", encoding="utf-8")
    mod = _rrc(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert mod.main(["--config", str(outside / "run.env")]) == mod.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "the config lacks" in out and "outside the allowed" not in out  # read, then judged on its content
    assert mod.main(["--config", "run\x1b.env"]) == mod.EXIT_REFUSED  # the operator's file is a checked text too
    assert "control character" in capsys.readouterr().out


def test_a_real_run_with_config_and_book_outside_reaches_the_y_n_question_and_the_book(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """MAJOR-1 of the first review: config, DESK_DB_PATH and run folder outside cwd, repository and temp folder. The
    refusal (if any) must come before the question, and the book must be readable afterwards (sending_rows)."""
    inside, outside = jail
    mod = _rrc(monkeypatch)
    keys = outside / "secrets"
    keys.mkdir()
    values = dict.fromkeys(mod.REQUIRED, "x") | {
        "DESK_DB_PATH": str(outside / "run" / "desk.db"),
        # the addresses are named, all on this computer (never a real host); the allow line is what the self-test sets
        "DESK_HUBSPOT_BASE_URL": "http://127.0.0.1:9",
        "DESK_HUBSPOT_ALLOW_ANY_BASE_URL": "yes",
        "REAL_IMAP_1_HOST": "localhost",
        "REAL_IMAP_2_HOST": "localhost",
        "DESK_HUBSPOT_MAX_CALLS": "5",
        "REAL_HUBSPOT_READBACK_MAX_CALLS": "5",
        "DESK_SMTPP_HOST": "localhost",
        "DESK_SMTPP_FROM": "a@example.test",
        "DESK_SMTPP_RECIPIENTS": "a@example.test,b@example.test",
        "DESK_SMTPP_MAX_SENDS": "5",
        "REAL_IMAP_1_USER": "a@example.test",
        "REAL_IMAP_2_USER": "b@example.test",
    }
    for name in [k for k in values if k.endswith(("_KEY_FILE", "_PASSWORD_FILE"))]:  # a real file, as newer checks want
        (keys / name).write_text("made-up-secret-value\n", encoding="utf-8")
        values[name] = str(keys / name)
    cfg = outside / "run.env"
    cfg.write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert mod.main(["--config", str(cfg)]) == mod.EXIT_STOPPED  # got to the question: nothing refused before it
    out = capsys.readouterr().out
    assert "refused" not in out and "stopped, nothing was called" in out
    # the book of that run, outside the allowed folders, is read by the run's own reads (the first desk's stop, the end)
    book = outside / "run" / "desk.db"
    book.parent.mkdir()
    conn = sqlite3.connect(book)
    conn.execute("CREATE TABLE receipts (receipt_id INTEGER, source TEXT, source_ref TEXT, outcome TEXT)")
    conn.execute("CREATE TABLE leads (lead_id TEXT, email_key TEXT, email TEXT, name TEXT)")
    conn.execute("CREATE TABLE followups (lead_id TEXT, state TEXT, crm_done INT, updated_at INT, last_error TEXT)")
    conn.execute("INSERT INTO followups VALUES ('L1', 'sending', 0, 0, '')")
    conn.commit()
    conn.close()
    assert mod.sending_rows(book) == 1  # not 0: the book was read, not refused


def test_a_real_run_with_every_path_outside_the_allowed_folders_runs_to_the_end(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The whole documented real run (against the local fakes, no real service), config and book outside the roots."""
    inside, outside = jail
    mod = _rrc(monkeypatch)
    fakes = mod.DryFakes(tmp_path / "fx")
    try:
        env = fakes.env | {"DESK_DB_PATH": str(outside / "run" / "desk.db")}
        cfg = outside / "run.env"
        cfg.write_text("\n".join(f"{k}={v}" for k, v in env.items()), encoding="utf-8")
        for name, value in env.items():  # the start question is answered "y" ONLY when every address is this computer
            if name.endswith(("_BASE_URL", "_HOST")) and "ALLOW" not in name:
                assert value.split("://")[-1].split(":")[0] in ("127.0.0.1", "localhost"), name
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        code = mod.main(["--config", str(cfg)])
    finally:
        fakes.close()
    out = capsys.readouterr().out
    assert code == 0, out[-3000:]
    assert "cannot read the desk's book" not in out and "refused" not in out
    assert json.loads((outside / "run" / "result.json").read_text(encoding="utf-8"))["summary"]


def test_a_new_file_name_is_rebuilt_from_allowed_characters_only(jail: tuple[Path, Path]) -> None:
    inside, _ = jail
    ok = pc.confine("out-1_a.b.json", what="f")
    assert ok == inside.resolve() / "out-1_a.b.json"
    for bad in ("a b.json", "a?b.json", "a#b.json", "ä.json", "a:b", "...", "x" * 300):
        with pytest.raises(pc.PathRefused, match="new file|letters"):
            pc.confine(bad, what="f")
    (inside / "has space?.json").write_text("{}", encoding="utf-8")  # an EXISTING file may have any name
    assert pc.confine("has space?.json", what="f") == inside.resolve() / "has space?.json"


def test_a_missing_folder_on_the_path_is_refused_for_an_output(jail: tuple[Path, Path]) -> None:
    with pytest.raises(pc.PathRefused, match="does not exist"):
        pc.confine("nope/out.json", what="f")


def test_the_returned_path_is_the_resolved_target_of_an_inside_link(jail: tuple[Path, Path]) -> None:
    if sys.platform == "win32":
        pytest.skip("creating a symbolic link needs a privilege on Windows")
    inside, _ = jail
    (inside / "real.jsonl").write_text("{}", encoding="utf-8")
    (inside / "alias.jsonl").symlink_to(inside / "real.jsonl")
    assert pc.confine("alias.jsonl", what="f") == inside.resolve() / "real.jsonl"


def test_a_link_loop_is_refused_and_not_a_traceback(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    if sys.platform == "win32":
        pytest.skip("creating a symbolic link needs a privilege on Windows")
    inside, _ = jail
    (inside / "ln1").symlink_to(inside / "ln2")
    (inside / "ln2").symlink_to(inside / "ln1")
    with pytest.raises(pc.PathRefused, match="loop"):
        pc.confine("ln1", what="f")
    args = _ci_args(inside) | {"--requests": "ln1"}
    assert ci.main([w for pair in args.items() for w in pair]) == ci.EXIT_INPUT
    assert "a link on the path does not resolve" in capsys.readouterr().err
    assert load_driver.main(["--base-url", "http://127.0.0.1:1", "--scenario", "burst:1", "--log", "ln1"]) == 2


def test_anywhere_accepts_a_folder_outside_but_not_odd_text_or_a_missing_folder(jail: tuple[Path, Path]) -> None:
    _, outside = jail
    (outside / "run.env").write_text("A=1\n", encoding="utf-8")
    assert pc.confine_anywhere(outside / "run.env", what="--config") == outside.resolve() / "run.env"
    with pytest.raises(pc.PathRefused):
        pc.confine_existing(outside / "run.env", what="--config")
    with pytest.raises(pc.PathRefused, match="control character"):
        pc.confine_anywhere("a\x1bb", what="--config")
    with pytest.raises(pc.PathRefused, match="no such file"):  # also a missing LAST name: a read must exist
        pc.confine_anywhere(outside / "nope.env", what="--config")
    with pytest.raises(pc.PathRefused, match="no such file"):
        pc.confine_anywhere(outside / "nope" / "run.env", what="--config")


def test_catcher_refuses_out_and_control_outside_and_creates_nothing(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from load_tools import catcher

    served: list[tuple[Path, Path]] = []

    async def fake_serve(host: str, port: int, out: Path, control: Path) -> None:  # never really listens
        served.append((out, control))

    monkeypatch.setattr(catcher, "serve", fake_serve)
    inside, outside = jail
    with pytest.raises(SystemExit) as e1:
        catcher.main(["--out", str(outside / "mail.jsonl"), "--port", "0"])
    with pytest.raises(SystemExit) as e2:
        catcher.main(["--out", "mail.jsonl", "--control", str(outside / "mode"), "--port", "0"])
    assert (e1.value.code, e2.value.code) == (2, 2) and served == []
    assert not list(outside.iterdir()) and not Path("mail.jsonl").exists()
    err = capsys.readouterr().err
    assert "--out: path is outside" in err and "--control: path is outside" in err
    catcher.main(["--out", "mail.jsonl", "--port", "0"])  # inside: reaches serve with the checked paths
    assert served == [(inside.resolve() / "mail.jsonl", inside.resolve() / "mail.jsonl.mode")]


@pytest.mark.parametrize(
    "script", ["load_tools/load_driver.py", "load_tools/check_invariants.py", "load_tools/catcher.py"]
)
def test_a_load_tool_started_as_a_script_shows_its_help_without_pythonpath(script: str) -> None:
    from support import start_proc, stop_proc

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = start_proc([str(REPO / script), "--help"], env)
    proc.wait(timeout=60)
    code, out = proc.returncode, stop_proc(proc)
    assert code == 0, out[-500:]
    assert b"usage" in out.lower()


def test_random_paths_never_leave_the_allowed_folder(jail: tuple[Path, Path]) -> None:
    """Fuzz (fixed seed): whatever is built from these pieces is refused, or lies inside the allowed folder."""
    import random

    inside, outside = jail
    (inside / "sub").mkdir()
    (inside / "sub" / "f.jsonl").write_text("{}", encoding="utf-8")
    (outside / "secret.jsonl").write_text("{}", encoding="utf-8")
    if sys.platform != "win32":
        (inside / "out-link").symlink_to(outside, target_is_directory=True)
        (inside / "in-link").symlink_to(inside / "sub", target_is_directory=True)
    pieces = [
        "..",
        ".",
        "sub",
        "f.jsonl",
        "out-link",
        "in-link",
        "new.json",
        "outside",
        inside.name,
        str(outside),
        "/",
        "",
    ]
    rng = random.Random(20261113)
    root = inside.resolve()
    for _ in range(600):
        text = os.sep.join(rng.choice(pieces) for _ in range(rng.randint(1, 6)))
        if rng.random() < 0.3:
            text = str(inside) + os.sep + text
        try:
            got = pc.confine(text, what="f")
        except pc.PathRefused:
            continue
        assert got == root or root in got.parents, text
        assert os.path.commonpath([str(got), str(root)]) == str(root), text


_SANITIZERS = frozenset({"confine", "confine_existing", "confine_anywhere", "_confined"})
# methods of a path object (the receiver is the path); a write method's argument is CONTENT, not a path
_PATH_METHODS = frozenset(
    {
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "touch",
        "unlink",
        "mkdir",
        "rmdir",
        "rename",
        "exists",
        "is_file",
        "is_dir",
        "stat",
        "iterdir",
        "glob",
        "rglob",
        "chmod",
        "symlink_to",
        "hardlink_to",
        "resolve",
    }
)
_PATH_ARG_METHODS = frozenset({"rename", "replace", "symlink_to", "hardlink_to"})  # the argument is a path too
# ``replace`` is a path method only with ONE argument (``str.replace`` and ``bytes.replace`` take two)
# module functions that act on a file: os.remove, shutil.copyfile, sqlite3.connect, ... (os.fspath and the like only
# compute a name, so they are not file operations)
_OS_FILE_FUNCTIONS = frozenset(
    {
        "remove",
        "unlink",
        "rename",
        "replace",
        "renames",
        "makedirs",
        "mkdir",
        "rmdir",
        "removedirs",
        "stat",
        "lstat",
        "listdir",
        "scandir",
        "walk",
        "open",
        "chmod",
        "chown",
        "link",
        "symlink",
        "readlink",
        "truncate",
        "utime",
        "access",
        "startfile",
        "exists",
        "isfile",
        "isdir",
        "islink",
        "getsize",
        "getmtime",
        "lexists",
    }
)
_NUMBER_MAKERS = frozenset({"int", "float", "bool", "len", "ord", "range"})  # the result is a number, never a path
_CONTENT_READERS = frozenset({"read_text", "read_bytes", "read", "readline", "readlines", "loads", "load"})
_MUTATORS = frozenset({"append", "extend", "update", "add", "insert", "setdefault", "appendleft"})
_FLAGGED_SITES = (
    "load_tools/check_invariants.py",
    "load_tools/load_driver.py",
    "scripts/n8n_api.py",
    "scripts/run_real_check.py",
    "tools/scrub_n8n_export.py",
    "load_tools/catcher.py",
    "demo_only/fake_model_server.py",
)
# functions whose parameters are NOT command-line text although they open a file: (file, function) -> why
_PARAMETER_ALLOWLIST: dict[tuple[str, str], tuple[tuple[str, ...], str]] = {
    ("load_tools/load_driver.py", "_read_int"): (("kernel_file",), "a fixed kernel file named inside the module"),
    ("load_tools/load_driver.py", "_worker"): (("args",), "its tuple is built by run() from the path run() confined"),
    ("load_tools/catcher.py", "read_mode"): (("control",), "the mode file that main() confined, passed on by serve()"),
    ("load_tools/catcher.py", "serve"): (("out", "control"), "the files that main() confined"),
    ("demo_only/fake_model_server.py", "make_model_server"): (
        ("call_log", "control_file"),
        "the files that main() confined (a test hands it paths of its own)",
    ),
}
# a function in the list above is a "path taker": a call of it with raw text is reported (its callers are checked)
# scripts/run_real_check.py reads the paths named in its own settings file on purpose (docs/real-services.md): only
# its command line is in scope, that is ``main`` and ``load_env``
_PARAMETER_SCOPE: dict[str, frozenset[str]] = {"scripts/run_real_check.py": frozenset({"main", "load_env"})}


def _is_sanitizer_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id in _SANITIZERS) or (isinstance(f, ast.Attribute) and f.attr in _SANITIZERS)


def _base_name(node: ast.AST) -> str | None:
    """The plain name at the bottom of ``a.b[0].c`` (what a store into an attribute or element changes weakly)."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _join(*states: set[str] | None) -> set[str] | None:
    live = [s for s in states if s is not None]
    return set().union(*live) if live else None


def _target_names(target: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


class _Taint:
    """May-taint walk over one module. A name is CLEAN only when every path that reaches a use assigns it the result of
    a path helper (confine, confine_existing, confine_anywhere): the state of two branches is joined by union, a loop
    and a ``try`` join every state seen inside, and whatever the walk does not model counts as raw (the parameters of
    every function, the content of a file, any call that is given raw text). It reports a file operation (``open``, a
    path method, ``os.*``, ``shutil.*``, ``sqlite3.connect``) whose path is raw."""

    def __init__(self, rel: str = "") -> None:
        self.rel = rel
        self.found: list[tuple[int, str]] = []
        self.accs: list[set[str]] = []
        self.sinkvars: set[str] = set()
        self.takers = {name: v[0] for (rel2, name), v in _PARAMETER_ALLOWLIST.items() if rel2 == rel}
        self.signatures: dict[str, list[str]] = {}

    # ---- expressions: report sinks, return whether the value is raw ----
    def expr(self, node: ast.AST | None, env: set[str]) -> bool:
        if node is None:
            return False
        if _is_sanitizer_call(node):
            assert isinstance(node, ast.Call)
            for x in (*node.args, *[k.value for k in node.keywords]):
                self.expr(x, env)  # the arguments are still evaluated (a nested open is still reported)
            return False
        if isinstance(node, ast.NamedExpr):
            raw = self.expr(node.value, env)
            if raw:
                env.add(node.target.id)  # an assignment inside an expression may not run: it never makes a name clean
            return raw
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner = set(env)
            for gen in node.generators:
                if self.expr(gen.iter, inner):
                    inner.update(_target_names(gen.target))
                for cond in gen.ifs:
                    self.expr(cond, inner)
            elts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            return any([self.expr(e, inner) for e in elts])
        if isinstance(node, ast.IfExp):  # the test picks a value, it does not become the value
            self.expr(node.test, env)
            return any([self.expr(node.body, env), self.expr(node.orelse, env)])
        if isinstance(node, ast.Lambda):
            inner = set(env) | {a.arg for a in (*node.args.args, *node.args.kwonlyargs)}
            return self.expr(node.body, inner)
        if isinstance(node, ast.Name):
            return node.id in env
        if isinstance(node, ast.Attribute) and node.attr == "argv":
            return True
        child_raw = [self.expr(c, env) for c in ast.iter_child_nodes(node)]  # no short cut: every child is visited
        raw = any(child_raw)
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _NUMBER_MAKERS:
                raw = False
            if isinstance(f, ast.Attribute) and f.attr == "parse_args":
                return True
            if isinstance(f, ast.Attribute) and f.attr in _CONTENT_READERS:
                raw = True  # the content of a file (even a checked one) is not a checked path
            self.check_sink(node, env)
            if isinstance(f, ast.Attribute) and f.attr in _MUTATORS and raw:
                base = _base_name(f.value)
                if base:
                    env.add(base)
        return raw

    def check_sink(self, call: ast.Call, env: set[str]) -> None:
        f = call.func
        args = [*call.args, *[k.value for k in call.keywords]]
        bad = False
        if isinstance(f, ast.Name):
            if f.id == "open":
                bad = any(self.expr_quiet(a, env) for a in args)
            elif f.id in self.sinkvars:  # a bound file method kept in a name and called later
                bad = True
            elif f.id in self.takers:  # a function that opens its parameter: what it is given must be checked
                names = self.signatures.get(f.id, [])
                given = [(names[i] if i < len(names) else "", a) for i, a in enumerate(call.args)]
                given += [(k.arg or "", k.value) for k in call.keywords]
                bad = any(n in self.takers[f.id] and self.expr_quiet(a, env) for n, a in given)
        elif isinstance(f, ast.Attribute):
            owner = f.value
            module = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
            if (
                (module in ("os", "path") and f.attr in _OS_FILE_FUNCTIONS)
                or module == "shutil"
                or (module == "sqlite3" and f.attr == "connect")
                or (module == "glob" and f.attr in ("glob", "iglob"))
            ):
                bad = any(self.expr_quiet(a, env) for a in args)
            elif f.attr in _PATH_METHODS or (f.attr == "replace" and len(call.args) == 1 and not call.keywords):
                bad = self.expr_quiet(owner, env) or (
                    f.attr in _PATH_ARG_METHODS and any(self.expr_quiet(a, env) for a in args)
                )
        if bad:
            self.found.append((call.lineno, ast.unparse(call)))

    def expr_quiet(self, node: ast.AST, env: set[str]) -> bool:
        probe = _Taint(self.rel)
        probe.sinkvars = self.sinkvars
        probe.takers = self.takers
        probe.signatures = self.signatures
        return probe.expr(node, set(env))

    # ---- statements ----
    def snap(self, env: set[str]) -> None:
        for acc in self.accs:
            acc |= env

    def assign(self, target: ast.AST, raw: bool, env: set[str], weak: set[str]) -> None:
        if isinstance(target, ast.Name):
            if raw:
                env.add(target.id)
            elif target.id not in weak:
                env.discard(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for t in target.elts:
                self.assign(t, raw, env, weak)
        elif isinstance(target, ast.Starred):
            self.assign(target.value, raw, env, weak)
        else:  # an attribute or an element: the rest of the object keeps its state, so only ever add
            base = _base_name(target)
            if base and raw:
                env.add(base)

    def block(self, stmts: list[ast.stmt], env: set[str] | None, weak: set[str]) -> set[str] | None:
        """``None`` is "no path reaches here" (after return, raise, break, continue)."""
        for s in stmts:
            if env is None:
                return None
            env = self.stmt(s, env, weak)
            if env is not None:
                self.snap(env)
        return env

    def loop(self, body: list[ast.stmt], orelse: list[ast.stmt], env: set[str], weak: set[str]) -> set[str] | None:
        acc = set(env)
        self.accs.append(acc)
        cur: set[str] | None = set(env)
        for _ in range(2):  # a second pass lets a value assigned late in the body reach the top of the next round
            cur = self.block(body, set((cur or set()) | acc), weak)
        self.accs.pop()
        self.snap(acc)
        return self.block(orelse, set(acc | (cur or set())), weak)  # every state seen in the body, a break included

    def stmt(self, s: ast.stmt, env: set[str], weak: set[str]) -> set[str] | None:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.function(s, env)
            return env
        if isinstance(s, ast.ClassDef):
            for st in s.body:
                self.stmt(st, set(env), weak)
            return env
        if isinstance(s, (ast.Global, ast.Nonlocal)):
            weak |= set(s.names)
            return env
        if isinstance(s, ast.Assign):
            raw = self.expr(s.value, env)
            if raw and isinstance(s.value, ast.Attribute) and s.value.attr in _PATH_METHODS:
                self.sinkvars.update(n for t in s.targets for n in _target_names(t))
            for t in s.targets:
                self.assign(t, raw, env, weak)
            return env
        if isinstance(s, ast.AnnAssign):
            self.assign(s.target, self.expr(s.value, env), env, weak)
            return env
        if isinstance(s, ast.AugAssign):
            raw = self.expr(s.value, env) or self.expr_quiet(s.target, env)
            self.assign(s.target, raw, env, weak)
            return env
        if isinstance(s, ast.If):
            self.expr(s.test, env)
            a = self.block(s.body, set(env), weak)
            b = self.block(s.orelse, set(env), weak)
            return _join(a, b)
        if isinstance(s, (ast.For, ast.AsyncFor)):
            raw = self.expr(s.iter, env)
            self.assign(s.target, raw, env, weak)
            return self.loop(s.body, s.orelse, env, weak)
        if isinstance(s, ast.While):
            self.expr(s.test, env)
            return self.loop(s.body, s.orelse, env, weak)
        if isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                raw = self.expr(item.context_expr, env)
                if item.optional_vars is not None:
                    self.assign(item.optional_vars, raw, env, weak)
            return self.block(s.body, env, weak)
        if isinstance(s, (ast.Try, ast.TryStar)):
            acc = set(env)
            self.accs.append(acc)
            after = self.block(s.body, set(env), weak)
            self.accs.pop()
            self.snap(acc)
            outs = [self.block(s.orelse, set(after), weak) if after is not None else None]
            for h in s.handlers:
                henv = set(acc | (after or set()))  # a handler can start from any state seen inside the body
                if h.name:
                    henv.discard(h.name)
                outs.append(self.block(h.body, henv, weak))
            joined = _join(*outs)
            self.block(s.finalbody, set(acc | (joined or set())), weak)  # the final block also runs on the way out
            return self.block(s.finalbody, joined, weak) if joined is not None else None
        if isinstance(s, ast.Match):
            raw = self.expr(s.subject, env)
            outs = [set(env)]
            for case in s.cases:
                cenv = set(env)
                if raw:
                    cenv.update(
                        n.name for n in ast.walk(case.pattern) if isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name
                    )
                    cenv.update(n.rest for n in ast.walk(case.pattern) if isinstance(n, ast.MatchMapping) and n.rest)
                outs.append(self.block(case.body, cenv, weak))
            return _join(*outs)
        if isinstance(s, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            for c in ast.iter_child_nodes(s):
                self.expr(c, env)
            self.snap(env)
            return None
        if isinstance(s, (ast.Expr, ast.Assert, ast.Delete)):
            for c in ast.iter_child_nodes(s):
                self.expr(c, env)
            return env
        for c in ast.iter_child_nodes(s):  # anything not modelled: visit its expressions, keep the state
            if isinstance(c, ast.expr):
                self.expr(c, env)
        return env

    def function(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, outer: set[str]) -> None:
        args = fn.args
        params = [*getattr(args, "posonlyargs", []), *args.args, *args.kwonlyargs]
        extra = [a for a in (args.vararg, args.kwarg) if a is not None]
        allowed = (self.rel, fn.name) in _PARAMETER_ALLOWLIST
        scope = _PARAMETER_SCOPE.get(self.rel)
        in_scope = scope is None or fn.name in scope
        names = {p.arg for p in (*params, *extra) if p.arg not in ("self", "cls") and (in_scope or p.arg == "path")}
        env = set(outer) | (set() if allowed else names)
        self.block(fn.body, env, set())

    def run(self, tree: ast.Module) -> list[tuple[int, str]]:
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.signatures[n.name] = [a.arg for a in (*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs)]
        env: set[str] = set()
        self.block(tree.body, env, set())
        return sorted(set(self.found))


def _raw_opens(tree: ast.Module, rel: str = "") -> list[tuple[int, str]]:
    """(line, text) of every file operation whose path is raw. Raw: the parameters of every function, the result of
    ``parse_args``, ``sys.argv``, the content of a file, and anything computed from those. Clean: only the result of a
    path helper, and only when EVERY path to the use assigned it (see ``_Taint``)."""
    return _Taint(rel).run(tree)


# (the lines of a function that a tool file could contain, the line numbers that must be reported)
_RAW_SHAPES = {
    "plain": (
        [
            "def main(argv):",
            "    a = ap.parse_args(argv)",
            "    return Path(a.out).read_text()",
        ],
        [3],
    ),
    "via a name": (
        [
            "def main(argv):",
            "    out = argv[1]",
            "    with open(out) as fh:",
            "        return fh",
        ],
        [3],
    ),
    "walrus": (
        [
            "def main(a):",
            "    if (p := a.out):",
            "        open(p)",
        ],
        [3],
    ),
    "walrus that cannot clean": (
        [
            "def main(a):",
            "    p = a.out",
            "    x = a and (p := confine(p, what='x'))",
            "    open(p)",
        ],
        [4],
    ),
    "for over argv": (
        [
            "def main():",
            "    for x in sys.argv[1:]:",
            "        open(x)",
        ],
        [3],
    ),
    "with as": (
        [
            "def main(a):",
            "    with ctx(a.out) as q:",
            "        open(q)",
        ],
        [3],
    ),
    "comprehension": (
        [
            "def main(a):",
            "    return [open(p) for p in a.paths]",
        ],
        [2],
    ),
    "parameter not named path": (
        [
            "def _w(target):",
            "    return open(target)",
        ],
        [2],
    ),
    "method": (
        [
            "class C:",
            "    def m(self, name):",
            "        return open(name)",
        ],
        [3],
    ),
    "os.remove": (
        [
            "def main(a):",
            "    os.remove(a.out)",
        ],
        [2],
    ),
    "os.rename": (
        [
            "def main(a):",
            "    os.rename(a.out, 'x')",
        ],
        [2],
    ),
    "shutil.copyfile": (
        [
            "def main(a):",
            "    shutil.copyfile('x', a.out)",
        ],
        [2],
    ),
    "shutil.rmtree": (
        [
            "def main(a):",
            "    shutil.rmtree(a.out)",
        ],
        [2],
    ),
    "Path.rename": (
        [
            "def main(a):",
            "    Path('x').rename(a.out)",
        ],
        [2],
    ),
    "Path.replace one argument": (
        [
            "def main(a):",
            "    Path('x').replace(a.out)",
        ],
        [2],
    ),
    "exists": (
        [
            "def main(a):",
            "    return Path(a.out).exists()",
        ],
        [2],
    ),
    "os.path.exists": (
        [
            "def main(a):",
            "    return os.path.exists(a.out)",
        ],
        [2],
    ),
    "sqlite": (
        [
            "def main(a):",
            "    return sqlite3.connect(a.db)",
        ],
        [2],
    ),
    "helper in one branch only": (
        [
            "def main(a):",
            "    if a.x:",
            "        p = confine(a.out, what='x')",
            "    else:",
            "        p = Path(a.out)",
            "    open(p)",
        ],
        [6],
    ),
    "helper in the other branch only": (
        [
            "def main(a):",
            "    if a.x:",
            "        p = Path(a.out)",
            "    else:",
            "        p = confine(a.out, what='x')",
            "    open(p)",
        ],
        [6],
    ),
    "attribute store keeps the name raw": (
        [
            "def main(a):",
            "    a.x = 1",
            "    open(a.out)",
        ],
        [3],
    ),
    "attribute store of clean text": (
        [
            "def main(a):",
            "    a.host = '127.0.0.1'",
            "    out = a.out",
            "    open(out)",
        ],
        [4],
    ),
    "element store": (
        [
            "def main(a):",
            "    d = {}",
            "    d['k'] = a.out",
            "    open(d['k'])",
        ],
        [4],
    ),
    "list append": (
        [
            "def main(a):",
            "    d = []",
            "    d.append(a.out)",
            "    open(d[0])",
        ],
        [4],
    ),
    "break keeps the raw state": (
        [
            "def main(a):",
            "    p = a.out",
            "    for i in a.items:",
            "        p = a.out",
            "        if i:",
            "            break",
            "        p = confine(p, what='x')",
            "    open(p)",
        ],
        [8],
    ),
    "try handler": (
        [
            "def main(a):",
            "    p = a.out",
            "    try:",
            "        p = confine(p, what='x')",
            "        risky()",
            "    except OSError:",
            "        open(p)",
        ],
        [7],
    ),
    "try then fall through": (
        [
            "def main(a):",
            "    p = a.out",
            "    try:",
            "        risky()",
            "        p = confine(p, what='x')",
            "    except OSError:",
            "        pass",
            "    open(p)",
        ],
        [8],
    ),
    "global": (
        [
            "def main(a):",
            "    global G",
            "    G = a.out",
            "    G = confine(G, what='x')",
            "    open(G)",
        ],
        [5],
    ),
    "bound method": (
        [
            "def main(a):",
            "    w = Path(a.out).write_text",
            "    w('x')",
        ],
        [3],
    ),
    "right operand replaces the base": (
        [
            "def main(a):",
            "    base = confine(a.d, what='x')",
            "    open(base / a.name)",
        ],
        [3],
    ),
    "Path(clean, raw)": (
        [
            "def main(a):",
            "    base = confine(a.d, what='x')",
            "    open(Path(base, a.name))",
        ],
        [3],
    ),
    "joinpath": (
        [
            "def main(a):",
            "    base = confine(a.d, what='x')",
            "    open(base.joinpath(a.name))",
        ],
        [3],
    ),
    "content read from a file used as a path": (
        [
            "def main(a):",
            "    cfg = confine(a.c, what='x')",
            "    open(Path(cfg.read_text()))",
        ],
        [3],
    ),
    "nested function": (
        [
            "def main(a):",
            "    def inner():",
            "        return open(a.out)",
            "    return inner()",
        ],
        [3],
    ),
    "raw passed to a path taker": (
        [
            "async def serve(host, port, out, control):",
            "    pass",
            "",
            "",
            "def main(a):",
            "    return serve(a.host, a.port, a.out, a.control)",
        ],
        [6],
    ),
}

_CLEAN_SHAPES = {
    "helper result": (
        [
            "def main(a):",
            "    out = confine(a.out, what='x')",
            "    return open(out)",
        ],
        [],
    ),
    "helper in both branches": (
        [
            "def main(a):",
            "    if a.x:",
            "        p = confine(a.out, what='x')",
            "    else:",
            "        p = confine(a.o2, what='y')",
            "    open(p)",
        ],
        [],
    ),
    "helper before the loop and in it": (
        [
            "def main(a):",
            "    p = confine(a.out, what='x')",
            "    for i in a.items:",
            "        p = confine(i, what='y')",
            "        open(p)",
        ],
        [],
    ),
    "helper after a raise in the handler": (
        [
            "def main(a):",
            "    try:",
            "        p = confine(a.out, what='x')",
            "    except OSError:",
            "        raise SystemExit(2)",
            "    open(p)",
        ],
        [],
    ),
    "checked path joined with a fixed name": (
        [
            "def main(a):",
            "    base = confine(a.d, what='x')",
            "    open(base / 'x.txt')",
        ],
        [],
    ),
    "tainted content written to a fixed file": (
        [
            "def main(argv):",
            "    n = int(argv[0])",
            "    Path('fixed.txt').write_text(str(n))",
        ],
        [],
    ),
    "text replace": (
        [
            "def main(a):",
            "    return a.out.replace('x', 'y')",
        ],
        [],
    ),
    "os.fspath only computes a name": (
        [
            "def main(a):",
            "    return os.fspath(a.out)",
        ],
        [],
    ),
    "a number made from raw text": (
        [
            "def main(a):",
            "    n = int(a.n)",
            "    return Path('f%d' % n).exists()",
        ],
        [],
    ),
}


def _lines_of(shape: tuple[list[str], list[int]]) -> list[int]:
    return [line for line, _ in _raw_opens(ast.parse("\n".join(shape[0]) + "\n"), "load_tools/catcher.py")]


@pytest.mark.parametrize("name", sorted(_RAW_SHAPES))
def test_the_open_finder_reports_every_raw_shape(name: str) -> None:
    assert _lines_of(_RAW_SHAPES[name]) == _RAW_SHAPES[name][1], name


@pytest.mark.parametrize("name", sorted(_CLEAN_SHAPES))
def test_the_open_finder_passes_a_checked_shape(name: str) -> None:
    assert _lines_of(_CLEAN_SHAPES[name]) == _CLEAN_SHAPES[name][1], name


@pytest.mark.parametrize("rel", _FLAGGED_SITES)
def test_no_flagged_site_opens_a_raw_argument(rel: str) -> None:
    """Siblings guard on real file operations, not on an import line. In each tool file, no file operation (``open``,
    a path method such as ``read_text`` or ``rename``, ``os.remove``-like and ``shutil`` calls, ``sqlite3.connect``) may
    act on text that came from the command line or from the parameters of a function unless a path helper (confine,
    confine_existing, confine_anywhere) made the path on EVERY way to that point. What it cannot see (listed, not
    hidden): a path that reaches a file operation through a call into another function (only the path takers named in
    ``_PARAMETER_ALLOWLIST`` are followed), ``getattr`` and ``eval`` tricks, a file operation in another module, and the
    paths that ``scripts/run_real_check.py`` reads from its own settings file (outside ``main`` and ``load_env`` that
    file is not in scope). Reading a file does not make its content a checked path. The shapes it does handle are in
    ``_RAW_SHAPES`` and ``_CLEAN_SHAPES``."""
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    assert _raw_opens(tree, rel) == [], rel


# ---------- P3 final: the helper at every flagged site, the rv17 findings, the three public functions ----------


def _book(path: Path, *, state: str = "queued") -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE receipts (receipt_id INTEGER, source TEXT, source_ref TEXT, outcome TEXT)")
    conn.execute("CREATE TABLE leads (lead_id TEXT, email_key TEXT, email TEXT, name TEXT)")
    conn.execute("CREATE TABLE followups (lead_id TEXT, state TEXT, crm_done INT, updated_at INT, last_error TEXT)")
    conn.execute("INSERT INTO leads VALUES ('L1', 'k', 'a@example.test', 'n')")
    conn.execute("INSERT INTO followups VALUES ('L1', ?, 0, 0, '')", (state,))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("name", ["read_jsonl", "read_crm", "_intended", "_drain_marker"])
def test_each_check_invariants_reader_refuses_an_outside_path_when_called_directly(
    jail: tuple[Path, Path], name: str
) -> None:
    """The flagged sites (read_jsonl, read_crm, _intended, _drain_marker) check the path THEMSELVES: on the code
    before this patch, ``main`` was the only guard (and before P3 not even that), so a direct call read the file."""
    inside, outside = jail
    for folder in (inside, outside):
        (folder / "x.json").write_text('{"intended_requests": 1, "drained_ts": 1, "rows": []}\n', encoding="utf-8")
    fn = getattr(ci, name)
    with pytest.raises(ci.InputError, match="path is outside"):
        fn(outside / "x.json")
    with pytest.raises(ci.InputError, match="path is outside"):
        fn(Path("..") / "outside" / "x.json")
    with pytest.raises(ci.InputError, match="no such file"):
        fn(inside / "missing.json")


def test_the_readers_still_read_inside_and_in_an_env_folder(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = jail
    (inside / "r.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (inside / "m.json").write_text('[{"lead_id": "L1"}]', encoding="utf-8")
    (inside / "f.json").write_text('{"intended_requests": 4, "qualifier": "x"}', encoding="utf-8")
    (inside / "d.json").write_text('{"drained_ts": 2.5}', encoding="utf-8")
    assert ci.read_jsonl(inside / "r.jsonl") == [{"a": 1}]
    assert ci.read_crm(inside / "m.json") == [{"lead_id": "L1"}]
    assert ci._intended(inside / "f.json") == 4
    assert ci._facts_say_live(inside / "f.json") is False
    assert ci._drain_marker(inside / "d.json") == 2.5
    assert ci._drain_marker(None) is None
    (outside / "f.json").write_text('{"intended_requests": 5, "qualifier": "x"}', encoding="utf-8")
    assert ci._facts_say_live(outside / "f.json") is True  # outside: fail closed ("live": the model check stays on)
    monkeypatch.setenv(pc.ROOTS_ENV, str(outside))  # the environment folders count for the fact check too
    assert ci._facts_say_live(outside / "f.json") is False and ci._intended(outside / "f.json") == 5


def test_facts_say_live_refuses_a_missing_file_as_live(jail: tuple[Path, Path]) -> None:
    inside, _ = jail
    assert ci._facts_say_live(inside / "none.json") is True


def test_read_book_checks_its_own_path_and_has_an_explicit_anywhere_mode(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    _book(inside / "in.db")
    _book(outside / "out.db")
    assert ci.read_book(inside / "in.db")["leads"] == ["L1"]
    with pytest.raises(ci.InputError, match="path is outside"):
        ci.read_book(outside / "out.db")
    assert ci.read_book(outside / "out.db", anywhere=True)["leads"] == ["L1"]  # the real run's book (its config's path)
    with pytest.raises(ci.InputError, match="no such file") as err:
        ci.read_book(inside / "none.db")
    assert "none.db" in str(err.value) and inside.name not in str(err.value)
    with pytest.raises(ci.InputError, match="no such file"):
        ci.read_book(outside / "none.db", anywhere=True)
    with pytest.raises(ci.InputError):  # a folder is not a book
        ci.read_book(inside, anywhere=False)


def test_load_env_resolves_its_own_path_from_the_top_of_the_file_system(jail: tuple[Path, Path]) -> None:
    _, outside = jail
    mod = _rrc_module()
    (outside / "run.env").write_text("A=1\nB='two'\n", encoding="utf-8")
    assert mod.load_env(outside / "run.env") == {"A": "1", "B": "two"}
    assert mod.load_env(str(outside / "x" / ".." / "run.env")) == {"A": "1", "B": "two"}  # type: ignore[arg-type]
    for bad in ("a\x1bb", outside / "nope.env", outside, ""):
        with pytest.raises(pc.PathRefused):  # a raw open would raise FileNotFoundError, IsADirectoryError or similar
            mod.load_env(bad)  # type: ignore[arg-type]


def _rrc_module() -> Any:
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        return importlib.import_module("run_real_check")
    finally:
        sys.path.remove(str(REPO / "scripts"))


def test_real_check_book_reads_use_the_anywhere_mode(jail: tuple[Path, Path]) -> None:
    _, outside = jail
    mod = _rrc_module()
    _book(outside / "run.db", state="sending")
    assert mod.sending_rows(outside / "run.db") == 1
    assert mod.sending_rows(outside / "none.db") == 0  # unreadable counts 0, as documented


@pytest.mark.parametrize("fn", ["confine", "confine_existing", "confine_anywhere"])
@pytest.mark.parametrize("char", ["\x00", "\x01", "\x1f", "\x7f", "\x80", "\x85", "\x9f"])
def test_c0_del_and_c1_controls_are_refused_by_every_function(jail: tuple[Path, Path], fn: str, char: str) -> None:
    with pytest.raises(pc.PathRefused, match="control character"):
        getattr(pc, fn)(f"a{char}b", what="f")


@pytest.mark.parametrize("char", ["\xa0", "\u00a1", "\u00ff", "\u0100"])
def test_the_characters_next_to_the_control_ranges_are_not_refused_as_controls(
    jail: tuple[Path, Path], char: str
) -> None:
    (Path.cwd() / f"a{char}b").write_text("x", encoding="utf-8")
    assert pc.confine_existing(f"a{char}b", what="f").name == f"a{char}b"


def test_the_system_temporary_folder_is_an_allowed_folder(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inside, _ = jail
    temp = tmp_path / "systemtemp"
    temp.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(pc.tempfile, "tempdir", str(temp))
    assert pc.confine(temp / "x.json", what="f") == temp.resolve() / "x.json"
    assert str(temp.resolve()) in pc.allowed_roots()
    with pytest.raises(pc.PathRefused, match="outside"):
        pc.confine(elsewhere / "x.json", what="f")
    assert str(inside.resolve()) in pc.allowed_roots()  # the working folder and the repository are roots too


def test_the_repository_and_the_working_folder_are_allowed_folders(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, work, other = tmp_path / "repo", tmp_path / "work", tmp_path / "other"
    for folder in (repo, work, other):
        folder.mkdir()
    monkeypatch.setattr(pc, "REPO_ROOT", repo)
    monkeypatch.chdir(work)
    monkeypatch.setattr(pc.tempfile, "tempdir", str(other))
    assert pc.confine(repo / "x", what="f").parent == repo.resolve()
    assert pc.confine("x", what="f").parent == work.resolve()
    monkeypatch.setattr(pc.tempfile, "tempdir", str(tmp_path / "nowhere"))
    with pytest.raises(pc.PathRefused, match="outside"):
        pc.confine(other / "x", what="f")


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symbolic link needs a privilege on Windows")
def test_explicit_roots_are_resolved_so_a_link_as_a_root_works(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    (outside / "f.json").write_text("{}", encoding="utf-8")
    (inside / "rootlink").symlink_to(outside, target_is_directory=True)
    assert (
        pc.confine_existing(outside / "f.json", what="f", roots=[inside / "rootlink"]) == outside.resolve() / "f.json"
    )
    assert pc.confine(inside / "rootlink" / "n.json", what="f", roots=[inside / "rootlink"]).parent == outside.resolve()


@pytest.mark.parametrize("fn", ["confine", "confine_existing"])
def test_a_path_that_names_an_allowed_folder_itself_is_refused_as_a_folder(jail: tuple[Path, Path], fn: str) -> None:
    inside, _ = jail
    for text in (inside, ".", str(inside) + os.sep, inside / "sub" / ".."):
        with pytest.raises(pc.PathRefused, match="names a folder, not a file"):
            getattr(pc, fn)(text, what="--json")


def test_the_top_of_the_file_system_is_refused_as_a_folder_in_anywhere_mode() -> None:
    with pytest.raises(pc.PathRefused, match="names a folder"):
        pc.confine_anywhere(os.path.abspath(os.sep), what="--config")


def test_split_root_gives_the_top_of_each_kind_of_path() -> None:
    assert pc._split_root("/a/b/c.env", __import__("posixpath")) == ("/", "a/b/c.env")
    assert pc._split_root("C:\\cfg\\run.env", ntpath) == ("C:\\", "cfg\\run.env")
    assert pc._split_root("\\\\server\\share\\cfg\\run.env", ntpath) == ("\\\\server\\share\\", "cfg\\run.env")
    assert pc._split_root("\\\\?\\C:\\cfg\\run.env", ntpath) == ("\\\\?\\C:\\", "cfg\\run.env")
    assert pc._split_root("\\\\server\\share\\", ntpath)[1] == ""  # the share itself: a folder


def test_anywhere_starts_its_walk_at_the_top_of_the_resolved_path_without_a_containment_test(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows network share (the rv17 Major): the resolved path is a share path that no ``X:\\`` contains; the walk
    must start at the share, whatever it is, and no test of containment may refuse it."""
    _, outside = jail
    (outside / "run.env").write_text("A=1\n", encoding="utf-8")
    seen: list[str] = []
    real_scandir = os.scandir

    def scandir(path: Any = ".") -> Any:
        seen.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(pc.os, "scandir", scandir)
    monkeypatch.setattr(pc, "_split_root", lambda real, mod=os.path: ("/", real[1:]))  # same as POSIX, to watch it
    assert pc.confine_anywhere(outside / "run.env", what="--config") == outside.resolve() / "run.env"
    assert seen[0] == "/"  # the first listing is the top, not an allowed folder
    monkeypatch.setattr(pc, "_inside", lambda *a: (_ for _ in ()).throw(AssertionError("containment used")))
    assert pc.confine_anywhere(outside / "run.env", what="--config") == outside.resolve() / "run.env"


@pytest.mark.skipif(sys.platform == "win32", reason="deleting the working folder is refused on Windows")
def test_a_deleted_working_folder_is_not_a_traceback(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    keep = tmp_path / "keep"
    keep.mkdir()
    monkeypatch.setattr(pc.tempfile, "tempdir", str(keep))
    monkeypatch.chdir(gone)
    gone.rmdir()
    roots = pc.allowed_roots()  # no FileNotFoundError
    assert str(keep.resolve()) in roots and str(gone.resolve()) not in roots
    assert pc.confine(keep / "x.json", what="f").parent == keep.resolve()
    with pytest.raises(pc.PathRefused):
        pc.confine("relative.json", what="f")  # a relative name resolves into the vanished folder: outside


def test_the_listing_path_is_returned_not_the_argument_text(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    (inside / "Real.jsonl").write_text("{}", encoding="utf-8")
    got = pc.confine_existing(f"{inside}/./sub/../Real.jsonl", what="f")
    assert str(got) == os.path.join(os.path.realpath(inside), "Real.jsonl")
    (outside / "c.env").write_text("A=1\n", encoding="utf-8")
    assert str(pc.confine_anywhere(f"{outside}/./x/../c.env", what="f")) == os.path.join(
        os.path.realpath(outside), "c.env"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symbolic link needs a privilege on Windows")
def test_anywhere_follows_a_link_to_the_real_file(jail: tuple[Path, Path]) -> None:
    inside, outside = jail
    (outside / "c.env").write_text("A=1\n", encoding="utf-8")
    (inside / "alias.env").symlink_to(outside / "c.env")
    assert pc.confine_anywhere(inside / "alias.env", what="f") == outside.resolve() / "c.env"


def test_scrub_started_as_a_script_without_pythonpath_works(tmp_path: Path) -> None:
    """rv17 NIT-6: ``python tools/scrub_n8n_export.py in.json out.json`` raised ModuleNotFoundError."""
    (tmp_path / "in.json").write_text(json.dumps({"name": "w", "nodes": [], "connections": {}}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = start_proc(
        [str(REPO / "tools" / "scrub_n8n_export.py"), str(tmp_path / "in.json"), str(tmp_path / "o.json")], env
    )
    proc.wait(timeout=60)
    out = stop_proc(proc)
    assert proc.returncode == 0, out[-500:]
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["name"] == "w"


def test_the_readme_says_what_the_wide_folders_allow() -> None:
    text = (REPO / "load_tools" / "README.md").read_text(encoding="utf-8")
    assert "overwritten" in text and "WRONG argument" in text
    assert "<(command)" in text and "mode 0711" in text  # the cost of the listing lookup for --config is said


# every flagged site: the function that holds the file operation must call one of the three helpers by itself
SITES = [
    ("load_tools/check_invariants.py", "read_jsonl", {"confine_existing"}),
    ("load_tools/check_invariants.py", "read_crm", {"confine_existing"}),
    ("load_tools/check_invariants.py", "read_book", {"confine_existing", "confine_anywhere"}),
    ("load_tools/check_invariants.py", "_intended", {"confine_existing"}),
    ("load_tools/check_invariants.py", "_facts_say_live", {"confine_existing"}),
    ("load_tools/check_invariants.py", "_drain_marker", {"confine_existing"}),
    ("load_tools/load_driver.py", "run", {"confine"}),
    ("tools/scrub_n8n_export.py", "main", {"confine", "confine_existing"}),
    ("scripts/n8n_api.py", "main", {"confine_existing"}),
    ("scripts/run_real_check.py", "load_env", {"confine_anywhere"}),
    ("load_tools/catcher.py", "main", {"confine"}),
]


@pytest.mark.parametrize(("rel", "func", "helpers"), SITES)
def test_each_flagged_function_calls_the_helper_itself_and_never_an_inline_guard(
    rel: str, func: str, helpers: set[str]
) -> None:
    import ast

    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func)
    called = {c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert helpers <= called, (rel, func, called)
    # the INLINE guard (realpath + commonpath in the tool) is the shape the analyser keeps refusing: not in tools
    attrs = {c.attr for c in ast.walk(node) if isinstance(c, ast.Attribute)}
    assert not {"realpath", "commonpath"} & attrs, (rel, func)


# -- follow-up A: a foreign `tools` package, hard links, Windows network paths ------------------------------


@pytest.mark.parametrize("script", ["check_invariants.py", "load_driver.py"])
def test_a_script_start_survives_a_foreign_tools_package(tmp_path: Path, script: str) -> None:
    """Class: the fallback import assumed that any failure means "the repository folder is not on the path yet". A
    ``tools`` package of another project (here an empty one, first on PYTHONPATH) is found first and cached, so
    adding the repository folder afterwards changed nothing and the start failed with ModuleNotFoundError."""
    foreign = tmp_path / "foreign" / "tools"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tmp_path / "foreign") + os.pathsep + str(REPO / "src")
    proc = start_proc([str(REPO / "load_tools" / script), "--help"], env)
    proc.wait(timeout=60)
    out = stop_proc(proc).decode("utf-8", "replace")
    assert proc.returncode == 0, out[-500:]
    assert "usage" in out.lower()


@pytest.mark.parametrize("script", ["check_invariants.py", "load_driver.py", "catcher.py"])
def test_a_foreign_tools_pathconfine_that_lacks_the_name_falls_back_to_ours(tmp_path: Path, script: str) -> None:
    """The first import of a foreign ``tools.pathconfine`` that lacks the name raises ImportError (not its subclass
    ModuleNotFoundError): the fallback must catch that too, drop the foreign package and load the repository's."""
    foreign = tmp_path / "foreign" / "tools"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("", encoding="utf-8")
    (foreign / "pathconfine.py").write_text("OTHER = 1\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tmp_path / "foreign") + os.pathsep + str(REPO / "src")
    proc = start_proc([str(REPO / "load_tools" / script), "--help"], env)
    proc.wait(timeout=60)
    out = stop_proc(proc).decode("utf-8", "replace")
    assert proc.returncode == 0, out[-500:]
    assert "usage" in out.lower()


_KEEP_OURS = """
import runpy, sys, types
mod = types.ModuleType("tools.keepme")  # a module of THIS repository's tools package, already imported
mod.__file__ = sys.argv[2] + "/tools/keepme.py"
sys.modules["tools.keepme"] = mod
sys.path.insert(0, sys.argv[1])  # a foreign `tools` package comes first
runpy.run_path(sys.argv[3], run_name="not_main")
assert sys.modules["tools.keepme"] is mod, "a module of this repository was dropped"
import tools.pathconfine as pc
assert pc.__file__.startswith(sys.argv[2]), pc.__file__
print("kept")
"""


@pytest.mark.parametrize("script", ["check_invariants.py", "load_driver.py", "catcher.py"])
def test_the_fallback_drops_only_foreign_tools_modules(tmp_path: Path, script: str) -> None:
    """Only the ``tools`` modules that are not under the repository are dropped: one of ours that is already imported
    keeps its identity (a second copy would break an ``isinstance`` check or a patch made on the first)."""
    foreign = tmp_path / "foreign" / "tools"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPO / "src")
    proc = start_proc(
        ["-I", "-c", _KEEP_OURS, str(tmp_path / "foreign"), str(REPO), str(REPO / "load_tools" / script)], env
    )
    proc.wait(timeout=60)
    out = stop_proc(proc).decode("utf-8", "replace")
    assert proc.returncode == 0, out[-800:]
    assert out.strip().endswith("kept"), out[-500:]


def test_a_hard_link_to_an_outside_file_passes_the_check(jail: tuple[Path, Path]) -> None:
    """Pins the documented limit (tools/pathconfine.py, load_tools/README.md): a hard link is another name for the same
    contents, there is no path to follow, so the check accepts it and a write goes through to the outside file."""
    inside, outside = jail
    target = outside / "secret.txt"
    target.write_text(SECRET, encoding="utf-8")
    try:
        os.link(target, inside / "alias.txt")
    except OSError as exc:  # a file system without hard links
        pytest.skip(f"no hard links here: {exc}")
    read = pc.confine_existing(inside / "alias.txt", what="f")
    assert read.parent == inside.resolve() and read.read_text(encoding="utf-8") == SECRET
    pc.confine(inside / "alias.txt", what="f").write_text("changed", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "changed"


def test_the_hard_link_limit_is_documented() -> None:
    assert "Hard links are not detected" in (pc.__doc__ or "")
    assert "Hard links are not detected" in (REPO / "load_tools" / "README.md").read_text(encoding="utf-8")


def _unc_uri(_path: object) -> str:
    """Stand-in for ``readonly_sqlite_uri`` as it behaves on Windows for a book on a share (ntpath on this system)."""
    return pc.readonly_sqlite_uri(r"\\\\server\\share\\x.db", ntpath)


def test_read_book_turns_the_network_path_refusal_into_an_input_error(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal of a Windows network path used to leave ``read_book`` as a ``PathRefused`` traceback, but every
    caller (check_invariants.main, scripts/run_real_check.py) catches only ``InputError``."""
    inside, _ = jail
    (inside / "b.db").write_text("", encoding="utf-8")
    monkeypatch.setattr(ci, "readonly_sqlite_uri", _unc_uri)
    for anywhere in (False, True):
        with pytest.raises(ci.InputError, match="device or network path"):
            ci.read_book(inside / "b.db", anywhere=anywhere)


def test_check_invariants_main_reports_a_network_path_book_as_an_input_error(
    jail: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, _ = jail
    for name in ("r.jsonl", "c.jsonl", "l.jsonl"):
        (inside / name).write_text('{"a": 1}\n', encoding="utf-8")
    (inside / "m.json").write_text("[]", encoding="utf-8")
    (inside / "b.db").write_text("", encoding="utf-8")
    (inside / "r.jsonl.facts.json").write_text(json.dumps({"intended_requests": 1}), encoding="utf-8")
    monkeypatch.setattr(ci, "readonly_sqlite_uri", _unc_uri)
    monkeypatch.delenv("DESK_QUALIFIER", raising=False)
    args = _ci_args(inside) | {"--facts": "r.jsonl.facts.json"}
    del args["--drain"]
    assert ci.main([w for pair in args.items() for w in pair]) == ci.EXIT_INPUT
    err = capsys.readouterr().err
    assert err.startswith("INPUT ERROR:") and "device or network path" in err and "Traceback" not in err


def test_run_real_check_reads_a_network_path_book_as_unreadable_not_a_traceback(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, _ = jail
    (inside / "b.db").write_text("", encoding="utf-8")
    monkeypatch.setattr(ci, "readonly_sqlite_uri", _unc_uri)
    rrc = _rrc(monkeypatch)
    assert rrc.sending_rows(inside / "b.db") == 0  # "an unreadable book counts 0"


def test_a_windows_network_path_is_refused_for_the_sqlite_uri() -> None:
    """The text ``Path.as_uri`` builds for a share has a host name after ``file://``; SQLite accepts only an empty one
    or ``localhost`` (SQLite's documentation of its URI names), so the open cannot work: refuse it with a clear
    message. Linux cannot run SQLite on Windows: only the URI text and the refusal are measured here.
    """
    from pathlib import PureWindowsPath

    unc = r"\\server\share\x.db"
    assert PureWindowsPath(unc).as_uri() == "file://server/share/x.db"
    for text in (
        unc,
        "//server/share/x.db",
        "\\/server\\share/x.db",  # mixed separators: ntpath treats them as a network path too
        "/\\server/share/x.db",
        "\\\\?\\UNC\\server\\share\\x.db",
        "//?/UNC/server/share/x.db",
        "\\\\.\\C:\\x.db",  # a device path and an extended-length local path: not a share, but refused too
        "\\\\?\\C:\\x.db",
        "\\\\localhost\\c$\\x.db",
    ):
        with pytest.raises(pc.PathRefused, match="device or network path"):
            pc.readonly_sqlite_uri(text, ntpath)
    # a relative name is made absolute first: in a current folder that is a share it builds a share path
    in_a_share: Any = types.SimpleNamespace(splitroot=ntpath.splitroot, abspath=lambda _t: r"\\server\share\x.db")
    with pytest.raises(pc.PathRefused, match="device or network path"):
        pc._refuse_network_path("x.db", in_a_share)
    pc._refuse_network_path("C" + r":\books\x.db", ntpath)  # a drive letter is fine
    pc._refuse_network_path("//server/share/x.db")  # on this system it is an ordinary path


# ---------- follow-up B: the fake model server's path options, the message and stream nits ----------


def _fake_model_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[dict[str, Any]]:
    """Run demo_only.fake_model_server.main() with these arguments, without a socket; returns what it handed to
    make_model_server. A refusal is a SystemExit."""
    from demo_only import fake_model_server as fms

    handed: list[dict[str, Any]] = []

    class _Stop(Exception):
        pass

    def fake_make(**kw: Any) -> Any:
        handed.append(kw)
        raise _Stop

    monkeypatch.setattr(fms, "make_model_server", fake_make)
    monkeypatch.setattr(sys, "argv", ["fake_model_server", "--port", "0", *argv])
    try:
        fms.main()
    except _Stop:
        pass
    return handed


@pytest.mark.parametrize("option", ["--call-log", "--control-file", "--key-file"])
def test_fake_model_server_refuses_each_path_option_outside_the_allowed_folders(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], option: str
) -> None:
    _, outside = jail
    (outside / "x.txt").write_text("fake-key-for-the-test\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _fake_model_main(monkeypatch, [option, str(outside / "x.txt")])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert f"refused: {option}: path is outside" in err and pc.ROOTS_ENV in err
    assert sorted(p.name for p in outside.iterdir()) == ["x.txt"]
    with pytest.raises(SystemExit):  # a new file outside is not created either
        _fake_model_main(monkeypatch, [option, str(outside / "new.txt")])
    assert not (outside / "new.txt").exists()


def test_fake_model_server_hands_the_checked_paths_on_and_reads_the_key_from_the_checked_file(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, _ = jail
    (inside / "k.txt").write_text("fake-key-for-the-test\n", encoding="utf-8")
    argv = ["--key-file", "k.txt", "--call-log", "calls.jsonl", "--control-file", "mode.txt"]
    handed = _fake_model_main(monkeypatch, argv)
    assert handed == [
        {
            "port": 0,
            "mode": "ok",
            "key": "fake-key-for-the-test",
            "call_log": inside.resolve() / "calls.jsonl",
            "control_file": inside.resolve() / "mode.txt",
        }
    ]
    with pytest.raises(SystemExit):  # a key file that does not exist is refused, not a traceback
        _fake_model_main(monkeypatch, ["--key-file", "none.txt"])


def test_n8n_api_import_message_names_the_rule_and_the_folder_list(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    inside, outside = jail
    (outside / "wf.json").write_text(json.dumps({"name": "made-up"}), encoding="utf-8")
    (inside / "key.txt").write_text("made-up-key-for-the-test\n", encoding="utf-8")
    monkeypatch.setenv("N8N_API_KEY_FILE", str(inside / "key.txt"))
    monkeypatch.setenv("N8N_URL", "http://127.0.0.1:1")
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    sys.modules.pop("n8n_api", None)
    mod = importlib.import_module("n8n_api")
    for target in (outside / "wf.json", inside / "missing.json"):
        with pytest.raises(SystemExit) as e:
            mod.main(["n8n_api.py", "import", str(target)])
        msg = str(e.value.code)
        assert "cannot read a workflow" in msg and "FILE:" in msg
        assert "folder you started in" in msg and pc.ROOTS_ENV in msg
        assert str(target) not in msg  # the path text is not echoed
        assert msg.count(pc.ROOTS_ENV) == 1  # said once, not twice


def test_scrub_script_started_without_pythonpath_refuses_a_missing_file_and_shows_usage(tmp_path: Path) -> None:
    """Started as a script with no PYTHONPATH (the documented way to run it): a refusal and the usage text end with
    exit code 2 and a message, never a traceback. Which stream each goes to is checked in-process, with capsys."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    script = str(REPO / "tools" / "scrub_n8n_export.py")
    cases = (
        ([str(tmp_path / "in.json"), str(tmp_path / "out.json")], "refused: in.json: no such file"),
        ([], "usage: python -m tools.scrub_n8n_export"),
    )
    for args, start in cases:
        proc = start_proc([script, *args], env)
        proc.wait(timeout=60)
        text = stop_proc(proc).decode("utf-8", "replace")
        assert proc.returncode == 2 and text.startswith(start), text[-500:]
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("bad", [None, 123, b"book.db"])
def test_read_book_refuses_an_argument_that_is_not_a_path_instead_of_failing_on_its_name(bad: Any) -> None:
    with pytest.raises(ci.InputError, match="not a path"):
        ci.read_book(bad)
    with pytest.raises(ci.InputError, match="not a path"):
        ci.read_book(bad, anywhere=True)


def test_read_book_label_shows_only_printable_characters(jail: tuple[Path, Path]) -> None:
    with pytest.raises(ci.InputError) as err:
        ci.read_book("x" + chr(0x202E) + "y" + chr(0x1B) + "[31m.db")  # a bidi mark and a control character
    shown = str(err.value)
    assert chr(0x1B) not in shown and chr(0x202E) not in shown and "book x" in shown


def test_catcher_default_control_file_for_an_out_name_with_a_space(
    jail: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name with a space may exist (an existing file keeps its name) but cannot be a NEW file's name, so the default
    ``<out>.mode`` was refused; the default now maps every character the helper does not allow to ``_``."""
    from load_tools import catcher

    served: list[tuple[Path, Path]] = []

    async def fake_serve(host: str, port: int, out: Path, control: Path) -> None:
        served.append((out, control))

    monkeypatch.setattr(catcher, "serve", fake_serve)
    inside, _ = jail
    (inside / "my mail.jsonl").write_text("", encoding="utf-8")
    catcher.main(["--out", "my mail.jsonl", "--port", "0"])
    mode_name = catcher.default_control_name("my mail.jsonl")
    assert served == [(inside.resolve() / "my mail.jsonl", inside.resolve() / mode_name)]
    assert mode_name.startswith("my_mail.jsonl.") and mode_name.endswith(".mode")
    assert catcher.default_control_name("plain.jsonl") == "plain.jsonl.mode"
    # two output names that differ only in replaced characters never share one mode file
    names = ["my mail.jsonl", "my_mail.jsonl", "bé.jsonl", "bè.jsonl", "a b.jsonl", "a?b.jsonl"]
    assert len({catcher.default_control_name(n) for n in names}) == len(names)
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", catcher.default_control_name(n)) for n in names)
