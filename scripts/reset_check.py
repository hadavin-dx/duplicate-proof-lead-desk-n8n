"""Run by reset.sh and reset.ps1 AFTER they stopped the desk and BEFORE they delete anything. Exit 0 = nothing uses the
lead book: no process holds its lock file and nothing answers on the desk's or the demo server's port. Exit 3 = a
process still holds the database or the lock file cannot be tested, 4 = something still answers on a port.
Prints one line, no values."""

from __future__ import annotations

import socket
import sys

from lead_desk import lockfile
from lead_desk.config import Config

LOOPBACK = "127.0.0.1"
CONNECT_SECONDS = 1.0


def answers(port: int) -> bool:
    try:
        with socket.create_connection((LOOPBACK, port), timeout=CONNECT_SECONDS):
            return True
    except OSError:
        return False


def main() -> int:
    config = Config.from_env()
    # only "there is no lock file" means that nothing holds the book; a lock file that cannot be tested is a refusal,
    # and a refused run must not create the file (acquire would)
    try:
        has_lock_file: bool | None = lockfile.lock_path(config.db_path).exists()
    except OSError:  # an unreadable folder: exists() raises instead of answering
        has_lock_file = None
    if has_lock_file is None:
        print("refused: the data folder cannot be looked at, so it is not known whether the desk runs; nothing deleted")
        return 3
    if has_lock_file:
        try:
            lockfile.acquire(config.db_path).close()
        except lockfile.LockHeldError:
            print("refused: a process still holds the database (the desk is running); nothing was deleted")
            return 3
        except lockfile.LockUnavailableError:
            print(
                "refused: the lock file of the database cannot be tested, so it is not known whether the desk runs; "
                "nothing was deleted"
            )
            return 3
    for port in (config.port, config.fakes_port):
        if port and answers(port):
            print(f"refused: something still answers on port {port}; nothing was deleted")
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
