"""One desk per lead book."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from typing import IO


class LockHeldError(BlockingIOError):
    """Another desk holds the lock for this lead book."""


class LockUnavailableError(OSError):
    """The lock could not be taken for a reason other than another desk (folder missing or not writable, the lock
    file name is a directory, the lock call failed). Still a refusal: the desk must not start."""


# What msvcrt.locking raises when ANOTHER process holds the byte (a "locking violation" is EACCES; a deadlock
# report is EDEADLK, which Windows also names EDEADLOCK). Every other error code is not "held by another desk".
_WINDOWS_HELD_ERRNOS = frozenset({errno.EACCES, errno.EDEADLK})
_LOCK_TRIES = 3  # a lock file replaced between open and lock is opened again this many times


def lock_path(db_path: Path | str) -> Path:
    """`<resolved db path>.lock`. The path is resolved first (symlinks, `..`), so two names for the same lead book
    give the same lock file."""
    return Path(f"{Path(db_path).resolve()}.lock")


def acquire(db_path: Path | str) -> IO[bytes]:
    """Open and hold `<resolved db_path>.lock` non-blocking; return the open handle (keep it for the life of the
    process).

    Linux: fcntl.flock(fd, LOCK_EX | LOCK_NB). Windows: seek(0), then msvcrt.locking(fd, LK_NBLCK, 1) (one byte at
    offset 0). Platform branches use `sys.platform` so mypy strict passes on both systems; the imports sit inside
    the branches.

    Raises LockHeldError (a BlockingIOError) when ANOTHER DESK holds the lock (the caller prints "another desk is
    using this database" and exits with constants.EXIT_ANOTHER_DESK). Any other OSError (the folder cannot be
    written, the lock file cannot be opened, the lock call failed for another reason) is raised as
    LockUnavailableError, so the caller can print a different message; it is still a clean refusal, never a second
    desk. On Windows only a locking violation (EACCES) or a deadlock report (EDEADLK) means another desk holds the
    lock. After locking, the lock file on disk must still be the file this process holds (it may have been deleted or
    replaced meanwhile); if not, it is opened again, and after _LOCK_TRIES such tries LockUnavailableError is raised."""
    path = lock_path(db_path)
    for _ in range(_LOCK_TRIES):
        try:
            f = open(path, "a+b")  # noqa: SIM115 (the handle is the lock; the caller keeps it)
        except OSError as e:
            raise LockUnavailableError(f"cannot open the lock file: {type(e).__name__}") from e
        try:
            if sys.platform == "win32":
                import msvcrt

                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as e:
                    if e.errno in _WINDOWS_HELD_ERRNOS:  # Windows reports a held byte as a plain OSError
                        raise LockHeldError(*e.args) from e
                    raise  # any other code: the outer handler makes it a LockUnavailableError
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # BlockingIOError when held elsewhere
            if _same_file(f, path):
                return f
        except BlockingIOError as e:
            f.close()
            raise LockHeldError(*e.args) from e
        except OSError as e:
            f.close()
            raise LockUnavailableError(f"cannot take the lock: {type(e).__name__}") from e
        except BaseException:
            f.close()
            raise
        f.close()  # the file this process locked is no longer the one at the path: try again with the current one
    raise LockUnavailableError("lock file kept changing while it was being locked")


def _same_file(f: IO[bytes], path: Path) -> bool:
    if sys.platform == "win32":
        return True  # an open lock file cannot be deleted or replaced on Windows
    try:
        a, b = os.fstat(f.fileno()), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
