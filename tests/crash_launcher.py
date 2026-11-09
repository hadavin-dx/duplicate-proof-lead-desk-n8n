"""Subprocess launcher for test_crash_d2_subprocess. Contains no hook code (contract.md, Crash hook ownership):
it only calls lead_desk.server.main(hooks=FlagFileHooks(path)). Usage: python tests/crash_launcher.py <flag file>."""

from __future__ import annotations

import sys
from pathlib import Path

from lead_desk.hooks import FlagFileHooks
from lead_desk.server import main

if __name__ == "__main__":
    main(hooks=FlagFileHooks(Path(sys.argv[1])))
