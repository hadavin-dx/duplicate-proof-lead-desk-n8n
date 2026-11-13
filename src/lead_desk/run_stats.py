"""Optional counters file: an adapter that is given a path writes its counters there after every change (an atomic
replace), so a run script can read what the adapter really did even when the desk process was stopped hard.
Counts only: no address, no key, no text."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path


def write_stats(path: Path | None, data: Mapping[str, int]) -> None:
    if path is None:
        return
    tmp = Path(f"{path}.tmp")
    try:
        tmp.write_text(json.dumps(dict(data), sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a counters file must never break a send or a put
