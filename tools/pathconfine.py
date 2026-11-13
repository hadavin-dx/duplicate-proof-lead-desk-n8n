"""One place that decides whether a file path typed on a command line may be opened. Standard library only.

Class of bug: a command-line tool opened whatever path it was given (``Path(argv[2]).read_text()``), so a path written
by a faulty script or by an AI agent that drives the tool (``../../secret``, an absolute path, a link that points out
of the work folder) was read or overwritten. Standard cure (CWE-22, OWASP "Path Traversal"): resolve the path to its
real location (``os.path.realpath`` follows links and removes ``..``) and refuse unless that location lies inside a
folder the tool is allowed to use. Three functions do that, one per kind of use; every tool calls the one that fits on
the argument and uses ONLY the returned path afterwards, never the original text:

* ``confine``: a file that is written (it may exist, or be new);
* ``confine_existing``: a file that is read (it must exist);
* ``confine_anywhere``: a file that is read and that the operator names on purpose, from any folder (see below).

How the returned path is made: after the check, the file is looked up BY NAME in a directory listing, one folder level
at a time, starting at the allowed folder (which comes from this module, not from the argument). The returned text is
the listing's own entry. So the text that gets opened is never the argument's text, only something the file system
itself reported. A name is matched as written, then, if that fails, in its Unicode normal form (NFC) and with
``os.path.normcase`` (so a name typed as NFC finds the file stored as NFD, as on macOS, and Windows ignores case). A
name that is not in the listing is, for a file that is read, a refusal ("no such file"); for a written file
(``confine``) it is a new name, rebuilt character by character from a table of allowed characters (letters, digits,
``.``, ``_``, ``-``). A new name is refused when it uses any other character, ends with a dot, or is a Windows device
name (``nul``, ``aux``, ``con``, ``prn``, ``com0`` to ``com9``, ``lpt0`` to ``lpt9``, with or without an extension),
on every operating system, like the core ``pathguard`` (R6, R7), so that a file made here also opens on Windows. An
EXISTING file may have any name except one with a control character (refused always, see below).

Allowed folders (the same words everywhere): the folder the tool was started in, the repository folder, and the
operating system's temporary folder (so a test or a scratch run works). A person who keeps logs elsewhere lists the
extra folders in ``LEAD_DESK_PATH_ROOTS`` (folders separated by ``os.pathsep``). The list is read on every call. The
three folders are wide: inside them an existing file (source code, a workflow file, another person's file in the
shared temporary folder) can be overwritten by an output option that names it. The check fences a wrong path, not a
wrong file name inside an allowed folder.

``confine_anywhere`` is for the settings file of a real run, which the documentation says to keep outside the
repository. It does the same listing lookup, but starts it at the top of the file system (the drive or share of the
resolved path: ``os.path.splitroot``, so a Windows network share works too) and has no containment test: every
folder of the machine counts. The checks on the text and the link resolution still apply; the file must exist. The
result is the listing's own path, with no fallback to the argument. Consequence, said plainly: a file below a folder
that can be entered but not listed (mode 0711 owned by another user), or one that has no listed name (``/dev/fd/63``
from a process substitution ``<(...)``), is refused; copy the file to a folder you can list.

What is refused, each with a message that names the argument and never prints the value: not text or a path; empty;
a NUL or another control character; longer than ``MAX_PATH_CHARS``; a path that cannot be resolved; a link loop (a
link that still is a link after resolution); a folder on the path that does not exist or cannot be listed; a path
that names a folder (an allowed folder itself, any other folder, or the top of the file system); a path whose real
location is outside every allowed folder (``..``, absolute path, link that leaves the folder, other drive).

What this is NOT: a defence against a person or an agent that controls the shell (it can start the tool in another
folder or set ``LEAD_DESK_PATH_ROOTS``). It fences a wrong argument. It also does not defend against a second local
process that swaps a folder for a link between the check and the open (two steps; the tools are single-user command
lines). On a case-insensitive file system that is not Windows (macOS default) the names below the allowed folder
may differ in Unicode form (see above) but not in case, and the allowed folder itself must be written the way the file
system reports it.

``readonly_sqlite_uri`` builds the connection string for a read-only SQLite file from a path that one of the three
functions returned: the path, percent-encoded by ``Path.as_uri`` (a ``?`` or ``#`` in a file name cannot start a new
option), plus the fixed option ``mode=ro``.
"""

from __future__ import annotations

import os
import re
import string
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType

MAX_PATH_CHARS = 4096  # a whole path, in characters (the usual Linux limit; Windows allows less)
MAX_NAME_CHARS = 255  # one file name of a NEW file
ROOTS_ENV = "LEAD_DESK_PATH_ROOTS"
REPO_ROOT = Path(__file__).resolve().parents[1]

# C0 (U+0000-U+001F), DEL and C1 (U+0080-U+009F): NUL ends a name early in C code, the rest reorder or hide text
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# the characters a NEW file's name may use, as a table: each character of the name is looked up here and the table's
# own value is used, so the new name is built from this table and not copied from the argument
_NAME_CHARS = {c: c for c in string.ascii_letters + string.digits + "._-"}
# names Windows treats as a device, with or without an extension (core pathguard R7, and Microsoft's list, which has
# COM0 and LPT0 since 2024; the table has no "$" and no digit other than 0-9, so CONIN$ and the superscript digits
# cannot occur in a new name)
_DEVICES = frozenset({"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(10)})


class PathRefused(ValueError):
    """A path argument was refused. The text names the argument and the rule, never the value."""


def allowed_roots() -> tuple[str, ...]:
    """The real locations of the allowed folders: current folder (left out when it was deleted), repository,
    temporary folder, and the extra folders in ``LEAD_DESK_PATH_ROOTS``. A listed folder that is empty text is
    ignored."""
    extra = [p for p in os.environ.get(ROOTS_ENV, "").split(os.pathsep) if p]
    try:
        here = [os.getcwd()]
    except OSError:  # the folder the tool was started in no longer exists
        here = []
    candidates = [*here, str(REPO_ROOT), tempfile.gettempdir(), *extra]
    return tuple(dict.fromkeys(os.path.realpath(c) for c in candidates))


def _inside(real: str, root: str) -> bool:
    """True when ``real`` is ``root`` or lies below it. Compared with ``os.path.commonpath`` after ``normcase``
    (Windows ignores case); a path on another drive makes commonpath raise, which counts as outside."""
    try:
        return os.path.commonpath([os.path.normcase(real), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


def _new_name(name: str, what: str) -> str:
    """A file name that is not in the listing yet, rebuilt from the table of allowed characters."""
    if len(name) > MAX_NAME_CHARS or not name.strip(".") or name.endswith("."):
        raise PathRefused(f"{what}: not usable as the name of a new file (empty, too long, or ends with a dot)")
    if name.split(".", 1)[0].lower() in _DEVICES:
        raise PathRefused(f"{what}: a Windows device name cannot be the name of a new file")
    try:
        return "".join(_NAME_CHARS[c] for c in name)
    except KeyError:
        raise PathRefused(f"{what}: the name of a new file may only use letters, digits, '.', '_' and '-'") from None


def _key(name: str) -> str:
    """A name as compared in a listing: Unicode normal form NFC, then ``normcase`` (Windows: lower case)."""
    return os.path.normcase(unicodedata.normalize("NFC", name))


def _match(listing: Iterable[os.DirEntry[str]], part: str) -> os.DirEntry[str] | None:
    """The entry named ``part``: the exact name wins, else the first one equal after ``_key`` (NFC, case on Windows)."""
    loose = None
    for e in listing:
        if e.name == part:
            return e
        if loose is None and _key(e.name) == _key(part):
            loose = e
    return loose


def _entry(current: str, part: str, what: str) -> os.DirEntry[str] | None:
    """The directory entry called ``part`` in ``current`` (None when there is none)."""
    try:
        with os.scandir(current) as listing:
            return _match(listing, part)
    except OSError:
        raise PathRefused(f"{what}: a folder on the path cannot be listed") from None


def _step(entry: os.DirEntry[str], last: bool, what: str) -> str:
    if entry.is_symlink():  # resolution removed every link that works: what is left is a loop
        raise PathRefused(f"{what}: a link on the path does not resolve (a loop)")
    is_folder = entry.is_dir(follow_symlinks=False)
    if not last and not is_folder:
        raise PathRefused(f"{what}: a folder on the path is not a folder")
    if last and is_folder:
        raise PathRefused(f"{what}: names a folder, not a file")
    return entry.path


def _locate(root: str, parts: list[str], what: str) -> str:
    """Walk ``parts`` below ``root`` through listings; the result is the listing's own path text. A part that is not
    listed yet (the last one only) is a NEW name, rebuilt from the table."""
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


def _locate_existing(root: str, parts: list[str], what: str) -> str:
    """Same walk, for a file that must already exist: there is NO new-name branch (a missing part is a refusal)."""
    current = root
    for index, part in enumerate(parts):
        entry = _entry(current, part, what)
        if entry is None:
            raise PathRefused(f"{what}: no such file")
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


def _resolve(text: str, what: str) -> str:
    try:
        return os.path.realpath(text)  # follows links, removes "..", makes it absolute
    except (OSError, RuntimeError, ValueError) as exc:  # a link loop, a NUL, an unreadable folder
        raise PathRefused(f"{what}: cannot be resolved") from exc


def _real_inside(text: str, allowed: Sequence[str], what: str) -> tuple[str, str]:
    """(allowed folder, parts below it as text) for the real location of ``text``; refused when it is in none of
    ``allowed`` or when it is an allowed folder itself."""
    real = _resolve(text, what)
    for root in allowed:
        if _inside(real, root):
            relative = os.path.relpath(real, root)
            if relative == os.curdir:
                raise PathRefused(f"{what}: names a folder, not a file")
            return root, relative
    raise PathRefused(f"{what}: path is outside the allowed folders (add a folder with {ROOTS_ENV})")


def _split_root(real: str, path_module: ModuleType = os.path) -> tuple[str, str]:
    """(top of the file system, the rest) of an absolute path: ``/`` and the rest on Linux and macOS, ``C:\\`` or
    ``\\\\server\\share\\`` and the rest on Windows. ``path_module`` is ``os.path``; a test hands in ``ntpath``."""
    drive, root, tail = path_module.splitroot(real)
    return drive + root, tail


def _real_anywhere(text: str, what: str) -> tuple[str, str]:
    """(top of the file system, parts below it as text) for the real location of ``text``: no folder is refused."""
    top, tail = _split_root(_resolve(text, what))
    if not tail:
        raise PathRefused(f"{what}: names a folder, not a file")
    return top, tail


def _parts(relative: str) -> list[str]:
    return relative.split(os.sep)


def _roots(roots: Sequence[str | os.PathLike[str]] | None) -> tuple[str, ...]:
    """The allowed folders: the default set, or the ones given (real locations)."""
    if roots is None:
        return allowed_roots()
    return tuple(os.path.realpath(r) for r in roots)


def confine(path: str | os.PathLike[str], *, what: str, roots: Sequence[str | os.PathLike[str]] | None = None) -> Path:
    """For a file that is WRITTEN (it may exist, or be new): the real location of ``path``, refused unless it lies
    inside an allowed folder (``roots``, or the defaults in ``allowed_roots``). ``what`` names the argument in the
    error, for example ``"--log"``. A new name must pass ``_new_name``. Links in the path are followed, so a link that
    leaves the folder is refused. Returns a ``Path`` built from the directory listing (see the module text), never
    from the argument."""
    text = _text(path, what)
    root, relative = _real_inside(text, _roots(roots), what)
    return Path(_locate(root, _parts(relative), what))


def confine_existing(
    path: str | os.PathLike[str], *, what: str, roots: Sequence[str | os.PathLike[str]] | None = None
) -> Path:
    """For a file that is READ: as ``confine``, but the file must exist ("no such file" otherwise); there is no
    new-name branch."""
    text = _text(path, what)
    root, relative = _real_inside(text, _roots(roots), what)
    return Path(_locate_existing(root, _parts(relative), what))


def confine_anywhere(path: str | os.PathLike[str], *, what: str) -> Path:
    """For a file that is READ and that the operator names on purpose (the settings file of a real run): the same
    listing lookup as ``confine_existing``, started at the top of the file system, with no containment test and no
    fallback. See the module text."""
    text = _text(path, what)
    top, relative = _real_anywhere(text, what)
    return Path(_locate_existing(top, _parts(relative), what))


def readonly_sqlite_uri(path: str | os.PathLike[str]) -> str:
    """``file:`` connection string that opens an existing SQLite file read-only. The path is made absolute and
    percent-encoded by ``Path.as_uri`` (a ``?`` or ``#`` in a file name cannot start a new option); the only text added
    is the fixed option, never a value from the caller. It does not check WHERE the file is: use it on the result of
    ``confine_existing`` or ``confine_anywhere``."""
    return Path(path).absolute().as_uri() + "?mode=ro"
