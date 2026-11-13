"""PROBE ONLY, DO NOT MERGE (a measuring draft: it carries the shapes under test, not a finished design).

One place that decides whether a file path typed on a command line may be opened. Standard library only.

Class of bug: a command-line tool opened whatever path it was given (``Path(argv[2]).read_text()``), so a path written
by a faulty script or by an AI agent that drives the tool (``../../secret``, an absolute path, a link that points out
of the work folder) was read or overwritten. Standard cure (CWE-22, OWASP "Path Traversal"): resolve the path to its
real location (``os.path.realpath`` follows links and removes ``..``) and refuse unless that location lies inside a
folder the tool is allowed to use. ``confine`` is that check; every tool calls it on the argument and uses ONLY the
returned path afterwards, never the original text.

The probe has five spellings, each differing from its pair in ONE thing (see PROBE-AB-V3-NOTE.md): ``confine`` (existing
or new file, new name from a dict table), ``confine_iter`` (same, new name by iteration over a constant),
``confine_existing`` (a file that must exist, no new-name branch), ``confine_existing_fixed`` (same, allowed folders
without the environment) and ``confine_existing_ix`` (same as ``confine_existing``, but the allowed folder is chosen
by an INDEX and the walk gets the folder and the relative text as two separate values, never as a returned pair; it
shares no helper function with the other four except ``allowed_roots``, ``PathRefused`` and two constants).

How the returned path is made: after the check, the file is looked up BY NAME in a directory listing, one folder level
at a time, starting at the allowed folder (which comes from this module, not from the argument). The returned text is
the listing's own entry. So the text that gets opened is never the argument's text, only something the file system
itself reported. A name that is not in the listing yet (an output file that does not exist) is rebuilt character by
character from a table of allowed characters (letters, digits, ``.``, ``_``, ``-``); any other character in a NEW
file's name is refused (an existing file may have any name).

Allowed folders (the same words everywhere): the folder the tool was started in, the repository folder, and the
operating system's temporary folder (so a test or a scratch run works). A person who keeps logs elsewhere lists the
extra folders in ``LEAD_DESK_PATH_ROOTS`` (folders separated by ``os.pathsep``). The list is read on every call.

What is refused, each with a message that names the argument and never prints the value: not text or a path; empty;
a NUL or another control character; longer than ``MAX_PATH_CHARS``; a path that cannot be resolved; a link loop (a
link that still is a link after resolution); a folder on the path that does not exist or cannot be listed; a path
whose real location is outside every allowed folder (``..``, absolute path, link that leaves the folder, other drive).

What this is NOT: a defence against a person or an agent that controls the shell (it can start the tool in another
folder or set ``LEAD_DESK_PATH_ROOTS``). It fences a wrong argument. It also does not defend against a second local
process that swaps a folder for a link between the check and the open (two steps; the tools are single-user command
lines). On a case-insensitive file system that is not Windows (macOS default) the folders of a path must be written
in the case the file system reports.
"""

from __future__ import annotations

import os
import re
import string
import tempfile
from collections.abc import Sequence
from pathlib import Path

MAX_PATH_CHARS = 4096  # a whole path, in characters (the usual Linux limit; Windows allows less)
MAX_NAME_CHARS = 255  # one file name of a NEW file
ROOTS_ENV = "LEAD_DESK_PATH_ROOTS"
REPO_ROOT = Path(__file__).resolve().parents[1]
# the temporary folder of the fixed-roots variant is a CONSTANT, so nothing on its call path reads the environment
# (``tempfile.gettempdir`` reads TMPDIR/TEMP/TMP); built by ``os.path.join``, not written as one literal
FIXED_TEMP = os.path.join(os.sep, "tmp")

# C0 (U+0000-U+001F), DEL and C1 (U+0080-U+009F): NUL ends a name early in C code, the rest reorder or hide text
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# the characters a NEW file's name may use. Two spellings of the same rule, one per probe shape (see the note):
# a table looked up by the character (``confine``), and a constant string searched by iteration (``confine_iter``)
_NAME_CHARS = {c: c for c in string.ascii_letters + string.digits + "._-"}
_NAME_ALPHABET = string.ascii_letters + string.digits + "._-"


class PathRefused(ValueError):
    """A path argument was refused. The text names the argument and the rule, never the value."""


def allowed_roots() -> tuple[str, ...]:
    """The real locations of the allowed folders: current folder, repository, temporary folder, and the extra
    folders in ``LEAD_DESK_PATH_ROOTS``. A listed folder that is empty text is ignored."""
    extra = [p for p in os.environ.get(ROOTS_ENV, "").split(os.pathsep) if p]
    candidates = [os.getcwd(), str(REPO_ROOT), tempfile.gettempdir(), *extra]
    return tuple(dict.fromkeys(os.path.realpath(c) for c in candidates))


def _inside(real: str, root: str) -> bool:
    """True when ``real`` is ``root`` or lies below it. Compared with ``os.path.commonpath`` after ``normcase``
    (Windows ignores case); a path on another drive makes commonpath raise, which counts as outside."""
    try:
        return os.path.commonpath([os.path.normcase(real), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _new_name(name: str, what: str) -> str:
    """Table shape: a NEW file's name rebuilt from a dict keyed by the character (a dict READ keyed by text)."""
    if len(name) > MAX_NAME_CHARS or not name.strip("."):
        raise PathRefused(f"{what}: not usable as the name of a new file")
    try:
        return "".join(_NAME_CHARS[c] for c in name)
    except KeyError:
        raise PathRefused(f"{what}: the name of a new file may only use letters, digits, '.', '_' and '-'") from None


def _new_name_iter(name: str, what: str) -> str:
    """Iteration shape: the same rule, but each output character is the ELEMENT of a constant string that equals the
    input character; the input character is only used in a comparison, no dict is read with it as a key."""
    if len(name) > MAX_NAME_CHARS or not name.strip("."):
        raise PathRefused(f"{what}: not usable as the name of a new file")
    chars = [next((k for k in _NAME_ALPHABET if k == c), "") for c in name]  # "" (a fixed default): not in the alphabet
    if "" in chars:
        raise PathRefused(f"{what}: the name of a new file may only use letters, digits, '.', '_' and '-'")
    return "".join(chars)


def _entry(current: str, part: str, what: str) -> os.DirEntry[str] | None:
    """The directory entry called ``part`` in ``current`` (None when there is none)."""
    try:
        with os.scandir(current) as listing:
            return next((e for e in listing if os.path.normcase(e.name) == os.path.normcase(part)), None)
    except OSError:
        raise PathRefused(f"{what}: a folder on the path cannot be listed") from None


def _step(entry: os.DirEntry[str], last: bool, what: str) -> str:
    if entry.is_symlink():  # resolution removed every link that works: what is left is a loop
        raise PathRefused(f"{what}: a link on the path does not resolve (a loop)")
    if not last and not entry.is_dir(follow_symlinks=False):
        raise PathRefused(f"{what}: a folder on the path is not a folder")
    return entry.path


def _locate(root: str, parts: list[str], what: str) -> str:
    """Walk ``parts`` below ``root`` through listings; the result is the listing's own path text. A part that is not
    listed yet (the last one only) is a NEW name, rebuilt with the table shape."""
    current = root
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        entry = _entry(current, part, what)
        if entry is None:
            if not last:
                raise PathRefused(f"{what}: a folder on the path does not exist")
            return os.path.join(current, _new_name(part, what))
        current = _step(entry, last, what)
    return current


def _locate_iter(root: str, parts: list[str], what: str) -> str:
    """Same as ``_locate`` but a new name is rebuilt with the iteration shape."""
    current = root
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        entry = _entry(current, part, what)
        if entry is None:
            if not last:
                raise PathRefused(f"{what}: a folder on the path does not exist")
            return os.path.join(current, _new_name_iter(part, what))
        current = _step(entry, last, what)
    return current


def _locate_existing(root: str, parts: list[str], what: str) -> str:
    """Same walk, for a file that must already exist: there is NO new-name branch (a missing part is a refusal)."""
    current = root
    for index, part in enumerate(parts):
        entry = _entry(current, part, what)
        if entry is None:
            raise PathRefused(f"{what}: no such file or folder")
        current = _step(entry, index == len(parts) - 1, what)
    return current


def _text(path: str | os.PathLike[str], what: str) -> str:
    """The checks on the text of the argument."""
    if not isinstance(path, (str, os.PathLike)):
        raise PathRefused(f"{what}: not a path")
    text = os.fspath(path)
    if not isinstance(text, str) or not text.strip():
        raise PathRefused(f"{what}: empty path")
    if _CONTROL.search(text):
        raise PathRefused(f"{what}: contains a control character")
    if len(text) > MAX_PATH_CHARS:
        raise PathRefused(f"{what}: path too long")
    return text


def _real_inside(text: str, allowed: Sequence[str], what: str) -> tuple[str, str]:
    """(allowed folder, parts below it) for the real location of ``text``; refused when it is in none of ``allowed``."""
    try:
        real = os.path.realpath(text)  # follows links, removes "..", makes it absolute
    except (OSError, RuntimeError, ValueError) as exc:  # a link loop, a NUL, an unreadable folder
        raise PathRefused(f"{what}: cannot be resolved") from exc
    for root in allowed:
        if _inside(real, root):
            relative = os.path.relpath(real, root)
            return root, "" if relative == os.curdir else relative
    raise PathRefused(f"{what}: path is outside the allowed folders (add a folder with {ROOTS_ENV})")


def _parts(relative: str) -> list[str]:
    return [] if not relative else relative.split(os.sep)


def fixed_roots() -> tuple[str, ...]:
    """Allowed folders WITHOUT the environment: current folder, repository, and the constant ``FIXED_TEMP`` (probe
    shape: no environment variable is read in our code or in the standard library on the call path of
    ``confine_existing_fixed``)."""
    return tuple(dict.fromkeys(os.path.realpath(c) for c in (os.getcwd(), str(REPO_ROOT), FIXED_TEMP)))


def confine(path: str | os.PathLike[str], *, what: str) -> Path:
    """COMBINED shape: existing or NEW file (new name from the dict table); allowed folders include the environment."""
    text = _text(path, what)
    root, relative = _real_inside(text, allowed_roots(), what)
    return Path(_locate(root, _parts(relative), what))


def confine_iter(path: str | os.PathLike[str], *, what: str) -> Path:
    """COMBINED shape with the ITERATION spelling of the new-name rule (the only difference from ``confine``)."""
    text = _text(path, what)
    root, relative = _real_inside(text, allowed_roots(), what)
    return Path(_locate_iter(root, _parts(relative), what))


def confine_existing(path: str | os.PathLike[str], *, what: str) -> Path:
    """SPLIT (reads) shape: the file must exist; no new-name branch; allowed folders include the environment."""
    text = _text(path, what)
    root, relative = _real_inside(text, allowed_roots(), what)
    return Path(_locate_existing(root, _parts(relative), what))


def confine_existing_fixed(path: str | os.PathLike[str], *, what: str) -> Path:
    """``confine_existing`` with ``fixed_roots`` (no environment read): the only difference from it."""
    text = _text(path, what)
    root, relative = _real_inside(text, fixed_roots(), what)
    return Path(_locate_existing(root, _parts(relative), what))


# ---- the INDEX variant: the same job as ``confine_existing``, with its own internal functions ----
# What differs from ``confine_existing``: (1) the allowed folder is picked by an integer index into the roots (no
# ``(root, relative)`` pair is returned by any function); (2) the relative text is a separate local; (3) the helper
# functions below are copies, so a taint that sticks to a returned pair or to one shared helper cannot reach this path.


def _ix_inside(real: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(real), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _ix_pick(real: str, roots: Sequence[str]) -> int:
    """The index of the first allowed folder that holds ``real`` (-1 when none does)."""
    for index, root in enumerate(roots):
        if _ix_inside(real, root):
            return index
    return -1


def _ix_entry(current: str, part: str, what: str) -> os.DirEntry[str] | None:
    try:
        with os.scandir(current) as listing:
            return next((e for e in listing if os.path.normcase(e.name) == os.path.normcase(part)), None)
    except OSError:
        raise PathRefused(f"{what}: a folder on the path cannot be listed") from None


def _ix_step(entry: os.DirEntry[str], last: bool, what: str) -> str:
    if entry.is_symlink():
        raise PathRefused(f"{what}: a link on the path does not resolve (a loop)")
    if not last and not entry.is_dir(follow_symlinks=False):
        raise PathRefused(f"{what}: a folder on the path is not a folder")
    return entry.path


def confine_existing_ix(path: str | os.PathLike[str], *, what: str) -> Path:
    """INDEX shape (reads): like ``confine_existing`` (file must exist, environment roots) with the folder picked by
    index and no returned pair."""
    if not isinstance(path, (str, os.PathLike)):
        raise PathRefused(f"{what}: not a path")
    text = os.fspath(path)
    if not isinstance(text, str) or not text.strip():
        raise PathRefused(f"{what}: empty path")
    if _CONTROL.search(text):
        raise PathRefused(f"{what}: contains a control character")
    if len(text) > MAX_PATH_CHARS:
        raise PathRefused(f"{what}: path too long")
    roots = allowed_roots()
    try:
        real = os.path.realpath(text)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathRefused(f"{what}: cannot be resolved") from exc
    index = _ix_pick(real, roots)
    if index < 0:
        raise PathRefused(f"{what}: path is outside the allowed folders (add a folder with {ROOTS_ENV})")
    relative = os.path.relpath(real, roots[index])
    parts = [] if relative == os.curdir else relative.split(os.sep)
    current = roots[index]
    for position, part in enumerate(parts):
        entry = _ix_entry(current, part, what)
        if entry is None:
            raise PathRefused(f"{what}: no such file or folder")
        current = _ix_step(entry, position == len(parts) - 1, what)
    return Path(current)
