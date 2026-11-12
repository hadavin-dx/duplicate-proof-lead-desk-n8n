"""The one helper for every number read from setting text (DESK_* environment names, config-file lines).

A bad value is a plain SettingRefused (a ValueError) that names the SETTING and never echoes the value or Python's own
message. Every numeric parse of settings text in src/, scripts/, load_tools/, tools/ and the demo folder goes through
`env_number`, never a bare int(), float() or Decimal() on text that came from the environment."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import SplitResult, urlsplit

_MISSING: Any = object()
_WHOLE = re.compile(r"[+-]?[0-9]+")  # ASCII digits only: no other-script digits, underscores, spaces or exponent
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class SettingRefused(ValueError):
    """A setting holds a value the desk refuses. The message names the setting (its DESK_* or REAL_* name) and never
    the value. `server.main` turns exactly this error into the clean exit 4; any other ValueError is a real bug."""


def env_number(
    env: Mapping[str, str],
    name: str,
    cast: Callable[[str], Any],
    default: Any = _MISSING,
    *,
    low: Any = None,
    high: Any = None,
) -> Any:
    """`cast(env[name])` (or `cast(default)` when the name is absent and a default is given). SettingRefused messages:
    "NAME is not a number" (junk text; after ASCII spaces, tabs and line ends are stripped from both ends, text that is
    not plain ASCII digits, a sign, a point and an exponent; a float or Decimal that is not finite; a whole number that
    is too long) and, when `low` or `high` is given, "NAME must be
    between LOW and HIGH" (or "at least LOW" / "at most HIGH")."""
    raw = default if (default is not _MISSING and name not in env) else env[name]
    if isinstance(raw, str):
        raw = raw.strip(" \t\r\n")  # spaces around a value are harmless (`set X=8765 ` in cmd.exe, env files)
        pattern = _WHOLE if cast is int else _DECIMAL
        if not (raw.isascii() and pattern.fullmatch(raw)):
            raise SettingRefused(f"{name} is not a number")
    try:
        value = cast(raw)
    except (ValueError, ArithmeticError, TypeError):
        raise SettingRefused(f"{name} is not a number") from None
    if isinstance(value, Decimal):
        finite = value.is_finite()
    elif isinstance(value, float):
        finite = math.isfinite(value)
    else:
        finite = True
    if not finite:
        raise SettingRefused(f"{name} is not a number")
    if (low is not None and value < low) or (high is not None and value > high):
        if low is not None and high is not None:
            raise SettingRefused(f"{name} must be between {low} and {high}")
        raise SettingRefused(f"{name} must be at least {low}" if low is not None else f"{name} must be at most {high}")
    return value


def split_url(name: str, text: str) -> SplitResult:
    """`urlsplit(text)` for the value of setting `name`: text that cannot be split (an unclosed bracket such as
    `http://[/`) is a SettingRefused naming the setting, never a bare ValueError."""
    try:
        return urlsplit(text)
    except ValueError:
        raise SettingRefused(f"{name} is not a valid address") from None
