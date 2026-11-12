"""Run the desk so that it can be stopped on purpose at one named moment (used to try the crash recovery by hand).

Run from the repository root:  PYTHONPATH=src python scripts/crash_desk.py   (on Windows: set PYTHONPATH=src first)
It starts the normal desk with the shared crash hook. Nothing happens until you create the flag file; its content is
one point name from lead_desk.hooks.ALL_HOOK_POINTS. The next time the desk reaches that point it prints
"simulate crash" and ends the process at once with exit code 137 (like a power cut), and the flag file is removed.
The flag file sits next to the database file (git-ignored folder). No hook code lives here on purpose.
"""

from __future__ import annotations

from lead_desk.config import Config
from lead_desk.hooks import FlagFileHooks
from lead_desk.server import main

if __name__ == "__main__":
    flag = Config.from_env().db_path.parent / "crash.flag"
    print(f"crash flag file: {flag}  (write a point name such as D2 into it to arm the crash)")
    main(hooks=FlagFileHooks(flag))
