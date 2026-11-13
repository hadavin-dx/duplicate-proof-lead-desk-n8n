"""Every number read from setting text goes through one helper (lead_desk.envnum): a bad value is a plain refusal that
names the SETTING, never Python's own message or the value, and the desk ends with exit code 4 and no traceback.

Two source checks below back that up. They are SAFETY NETS, NOT PROOF; what they cannot see:

* The scan (`_bare_casts`: no bare int(), float() or Decimal() on settings text, in src/, scripts/, demo_only/, load_tools/,
  tools/). FALSE NEGATIVES: taint that travels through a function's return value or a container handed between
  functions; a cast reached through getattr/eval; other number types (Fraction); a settings mapping that is neither named
  env/environ nor typed Mapping[str, str] / dict[str, str] and is read with a variable key or a lower-case literal key.
  FALSE POSITIVES: attribute taint is global per file (a `self.raw` assigned settings text anywhere taints `.raw`
  everywhere in that file); an upper-case literal key on JSON data looks like a setting name; `int(env_number(e, ...) * 2)`
  is flagged because the call sits inside a larger expression.
* The guard (`_unchecked_calls`: every env_number / number call gives `high=`). FALSE NEGATIVES: `high=` given as a
  variable that is None at run time; `**kwargs`; `partial(...)` objects that are built elsewhere and called later.
  The only allow-listed place is the money reader `_dec` in src/lead_desk/config.py (matched by its qualified name).

Each limit that was cheap to close has a fooling-form test (EVASIVE, GUARD_EVASIVE); the rest are listed here so nobody
reads a green scan as proof. Made-up data."""

# ruff: noqa: E501  (the case tables are one case per line)
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_real_check as rrc  # noqa: E402

from lead_desk.config import Config  # noqa: E402
from lead_desk.constants import CAP_COUNT_MAX, SIZE_CAP_BYTES_MAX, STALL_SECONDS_MAX  # noqa: E402
from lead_desk.crm_hubspot import HubspotSettings  # noqa: E402
from lead_desk.envnum import SettingRefused, env_number  # noqa: E402
from lead_desk.smtp_outbox import SmtpSettings  # noqa: E402
from lead_desk.smtp_provider import ProviderSettings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
JUNK = "abc-9"  # never appears in a message

LIVE = {
    "DESK_QUALIFIER": "live_claude",
    "DESK_LIVE_MODEL": "m",
    "DESK_LIVE_KEY_FILE": "k.txt",
    "DESK_LIVE_PRICE_DATE": "2026-01-01",
    "DESK_LIVE_LEDGER_PATH": "l.jsonl",
    "DESK_LIVE_RUN_ID": "r",
    "DESK_LIVE_PRICE_IN_PER_MTOK": "1",
    "DESK_LIVE_PRICE_OUT_PER_MTOK": "2",
    "DESK_LIVE_CAP_RUN_USD": "1",
    "DESK_LIVE_CAP_TOTAL_USD": "3",
}
PROVIDER = {
    "DESK_SMTPP_HOST": "mail.example.test",
    "DESK_SMTPP_USER": "u",
    "DESK_SMTPP_PASSWORD_FILE": "p.txt",
    "DESK_SMTPP_FROM": "a@example.test",
    "DESK_SMTPP_RECIPIENTS": "b@example.test",
    "DESK_SMTPP_MAX_SENDS": "5",
}
HUBSPOT = {"DESK_HUBSPOT_KEY_FILE": "k.txt", "DESK_HUBSPOT_MAX_CALLS": "5"}


def _config(name: str) -> Any:
    return Config.from_env({**LIVE, name: JUNK})


CASES: list[tuple[str, Any]] = [
    (n, lambda n=n: _config(n))
    for n in (
        "DESK_PORT",
        "DESK_FAKES_PORT",
        "DESK_SEND_TIMEOUT_SECONDS",
        "DESK_SIZE_CAP_BYTES",
        "DESK_STALL_SECONDS",
        "DESK_LIVE_TIMEOUT_SECONDS",
        "DESK_LIVE_MAX_TOKENS",
        "DESK_LIVE_PRICE_IN_PER_MTOK",
        "DESK_LIVE_PRICE_OUT_PER_MTOK",
        "DESK_LIVE_CAP_RUN_USD",
        "DESK_LIVE_CAP_TOTAL_USD",
    )
]
CASES += [(n, lambda n=n: SmtpSettings.from_env({n: JUNK})) for n in ("DESK_SMTP_PORT", "DESK_SMTP_TIMEOUT_SECONDS")]
CASES += [
    (n, lambda n=n: ProviderSettings.from_env({**PROVIDER, n: JUNK}))
    for n in ("DESK_SMTPP_PORT", "DESK_SMTPP_TIMEOUT_SECONDS", "DESK_SMTPP_MAX_SENDS")
]
CASES += [
    (n, lambda n=n: HubspotSettings.from_env({**HUBSPOT, n: JUNK}))
    for n in ("DESK_HUBSPOT_TIMEOUT_SECONDS", "DESK_HUBSPOT_MAX_429_RETRIES", "DESK_HUBSPOT_MAX_CALLS")
]
CASES += [(n, lambda n=n: rrc.number(n, {n: JUNK}, int, 8)) for n in ("REAL_PERSONS", "REAL_DRAIN_SECONDS")]


@pytest.mark.parametrize(("name", "build"), CASES, ids=[n for n, _ in CASES])
def test_every_number_setting_names_itself_and_never_echoes_the_value(name: str, build: Any) -> None:
    with pytest.raises(ValueError) as e:
        build()
    assert str(e.value) == f"{name} is not a number"  # the one wording, from the one helper
    assert JUNK not in str(e.value)


def test_the_helper_refuses_non_finite_numbers_and_checks_ranges() -> None:
    for junk in ("nan", "inf", "-inf"):
        for kind in (float, Decimal):
            with pytest.raises(ValueError, match="X is not a number"):
                env_number({"X": junk}, "X", kind)
    assert env_number({"X": "5"}, "X", int, low=1, high=9) == 5
    assert env_number({}, "X", int, "7") == 7  # the default is cast the same way
    with pytest.raises(ValueError, match="X must be between 1 and 9"):
        env_number({"X": "10"}, "X", int, low=1, high=9)
    with pytest.raises(ValueError, match="X must be at least 1"):
        env_number({"X": "0"}, "X", int, low=1)
    with pytest.raises(ValueError, match="X must be at most 9"):
        env_number({"X": "10"}, "X", int, high=9)


def test_a_bad_port_in_the_fakes_url_is_a_named_refusal() -> None:
    with pytest.raises(ValueError, match="DESK_FAKES_URL has a bad port"):
        Config.from_env({"DESK_FAKES_URL": "http://127.0.0.1:notaport"})


def _run_desk(tmp_path: Path, extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "DESK_DB_PATH": str(tmp_path / "d.sqlite3"), "DESK_PORT": "0"}
    env.update(extra)
    return subprocess.run(  # noqa: S603  # nosec B603 - our own module, fixed arguments
        [sys.executable, "-m", "lead_desk.server"], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env
    )


@pytest.mark.parametrize(
    ("extra", "name"),
    [
        ({"DESK_PORT": JUNK}, "DESK_PORT"),
        ({"DESK_STALL_SECONDS": JUNK}, "DESK_STALL_SECONDS"),
        ({"DESK_OUTBOX": "smtp", "DESK_SMTP_PORT": JUNK}, "DESK_SMTP_PORT"),
        ({"DESK_OUTBOX": "smtp_provider", **PROVIDER, "DESK_SMTPP_PORT": JUNK}, "DESK_SMTPP_PORT"),
        ({"DESK_CRM": "hubspot", **HUBSPOT, "DESK_HUBSPOT_MAX_429_RETRIES": JUNK}, "DESK_HUBSPOT_MAX_429_RETRIES"),
    ],
    ids=["port", "stall", "smtp-port", "provider-port", "hubspot-retries"],
)
def test_a_bad_setting_ends_the_desk_with_exit_4_naming_it_and_no_traceback(
    tmp_path: Path, extra: dict[str, str], name: str
) -> None:
    r = _run_desk(tmp_path, extra)
    out = r.stdout + r.stderr
    assert r.returncode == 4, out[-600:]
    assert f"the desk refuses a setting: {name} is not a number" in r.stdout
    assert "Traceback" not in out and JUNK not in out


# ---------------------------------------------------------------- range, strictness, and exit 4 for every kind of bad number
BAD_NUMBERS = [
    "1e999",
    str(10**4000),
    "99999999999",
    "-5",
    "nan",
    "inf",
    "-inf",
    "0x10",
    "1_000",
    "5 5",
    " 5",
    "١٢٣",
    "1e3",
]

RANGE_CASES: list[tuple[str, Any]] = [
    (n, lambda v, n=n: Config.from_env({**LIVE, n: v}))
    for n in ("DESK_PORT", "DESK_FAKES_PORT", "DESK_SIZE_CAP_BYTES", "DESK_STALL_SECONDS", "DESK_SEND_TIMEOUT_SECONDS")
]
RANGE_CASES += [
    (n, lambda v, n=n: SmtpSettings.from_env({n: v})) for n in ("DESK_SMTP_PORT", "DESK_SMTP_TIMEOUT_SECONDS")
]
RANGE_CASES += [
    (n, lambda v, n=n: ProviderSettings.from_env({**PROVIDER, n: v}))
    for n in ("DESK_SMTPP_PORT", "DESK_SMTPP_TIMEOUT_SECONDS", "DESK_SMTPP_MAX_SENDS")
]
RANGE_CASES += [
    (n, lambda v, n=n: HubspotSettings.from_env({**HUBSPOT, n: v}))
    for n in ("DESK_HUBSPOT_TIMEOUT_SECONDS", "DESK_HUBSPOT_MAX_429_RETRIES", "DESK_HUBSPOT_MAX_CALLS")
]
RANGE_CASES += [
    (n, lambda v, n=n: Config.from_env({**LIVE, n: v})) for n in ("DESK_LIVE_TIMEOUT_SECONDS", "DESK_LIVE_MAX_TOKENS")
]


@pytest.mark.parametrize("bad", BAD_NUMBERS)
@pytest.mark.parametrize(("name", "build"), RANGE_CASES, ids=[n for n, _ in RANGE_CASES])
def test_a_junk_or_huge_or_odd_number_is_refused_by_name_never_echoed(name: str, build: Any, bad: str) -> None:
    with pytest.raises(ValueError) as e:
        build(bad)
    msg = str(e.value)
    assert isinstance(e.value, SettingRefused)
    assert name in msg, msg  # the refusal names the setting
    assert bad not in msg, msg  # and never echoes the value


@pytest.mark.parametrize(
    ("name", "build", "ok", "over"),
    [
        ("DESK_PORT", lambda v: Config.from_env({"DESK_PORT": v}), "65535", "65536"),
        ("DESK_FAKES_PORT", lambda v: Config.from_env({"DESK_FAKES_PORT": v}), "1", "0"),
        (
            "DESK_SIZE_CAP_BYTES",
            lambda v: Config.from_env({"DESK_SIZE_CAP_BYTES": v}),
            str(SIZE_CAP_BYTES_MAX),
            str(SIZE_CAP_BYTES_MAX + 1),
        ),
        (
            "DESK_STALL_SECONDS",
            lambda v: Config.from_env({"DESK_STALL_SECONDS": v}),
            str(STALL_SECONDS_MAX),
            str(STALL_SECONDS_MAX + 1),
        ),
        ("DESK_SEND_TIMEOUT_SECONDS", lambda v: Config.from_env({"DESK_SEND_TIMEOUT_SECONDS": v}), "600", "601"),
        ("DESK_SMTP_PORT", lambda v: SmtpSettings.from_env({"DESK_SMTP_PORT": v}), "65535", "65536"),
        ("DESK_SMTP_TIMEOUT_SECONDS", lambda v: SmtpSettings.from_env({"DESK_SMTP_TIMEOUT_SECONDS": v}), "600", "601"),
        (
            "DESK_SMTPP_MAX_SENDS",
            lambda v: ProviderSettings.from_env({**PROVIDER, "DESK_SMTPP_MAX_SENDS": v}),
            str(CAP_COUNT_MAX),
            str(CAP_COUNT_MAX + 1),
        ),
        (
            "DESK_SMTPP_TIMEOUT_SECONDS",
            lambda v: ProviderSettings.from_env({**PROVIDER, "DESK_SMTPP_TIMEOUT_SECONDS": v}),
            "9",
            "9.5",
        ),
        (
            "DESK_HUBSPOT_MAX_CALLS",
            lambda v: HubspotSettings.from_env({**HUBSPOT, "DESK_HUBSPOT_MAX_CALLS": v}),
            str(CAP_COUNT_MAX),
            str(CAP_COUNT_MAX + 1),
        ),
        (
            "DESK_HUBSPOT_TIMEOUT_SECONDS",
            lambda v: HubspotSettings.from_env({**HUBSPOT, "DESK_HUBSPOT_TIMEOUT_SECONDS": v}),
            "9",
            "9.5",
        ),
        (
            "DESK_HUBSPOT_MAX_429_RETRIES",
            lambda v: HubspotSettings.from_env({**HUBSPOT, "DESK_HUBSPOT_MAX_429_RETRIES": v}),
            "5",
            "6",
        ),
    ],
)
def test_each_number_has_a_named_ceiling_and_its_top_value_passes(name: str, build: Any, ok: str, over: str) -> None:
    build(ok)  # the top of the range passes
    with pytest.raises(SettingRefused, match=name):
        build(over)


def test_post_init_range_errors_name_the_setting_too() -> None:
    for kw, name in (
        ({"port": 70000}, "DESK_PORT"),
        ({"fakes_port": 0}, "DESK_FAKES_PORT"),
        ({"size_cap_bytes": 0}, "DESK_SIZE_CAP_BYTES"),
        ({"stall_seconds": -1}, "DESK_STALL_SECONDS"),
        ({"send_timeout_seconds": 1e308}, "DESK_SEND_TIMEOUT_SECONDS"),
        ({"stall_seconds": 10**4000}, "DESK_STALL_SECONDS"),
    ):
        with pytest.raises(SettingRefused, match=name):
            Config(**kw)
    with pytest.raises(SettingRefused, match="DESK_SMTPP_MAX_SENDS"):
        ProviderSettings.from_env({**PROVIDER, "DESK_SMTPP_MAX_SENDS": "0"})
    with pytest.raises(SettingRefused, match="DESK_HUBSPOT_MAX_CALLS"):
        HubspotSettings.from_env({**HUBSPOT, "DESK_HUBSPOT_MAX_CALLS": "0"})
    with pytest.raises(SettingRefused, match="DESK_LIVE_CAP_RUN_USD"):
        Config.from_env({**LIVE, "DESK_LIVE_CAP_RUN_USD": "5", "DESK_LIVE_CAP_TOTAL_USD": "3"})


def test_ordinary_values_are_unchanged() -> None:
    c = Config.from_env(
        {
            "DESK_PORT": "0",
            "DESK_SIZE_CAP_BYTES": "65536",
            "DESK_STALL_SECONDS": "120",
            "DESK_SEND_TIMEOUT_SECONDS": "5.0",
        }
    )
    assert (c.port, c.size_cap_bytes, c.stall_seconds, c.send_timeout_seconds) == (0, 65536, 120, 5.0)
    assert Config(send_timeout_seconds=30.0).send_timeout_seconds == 30.0  # above the 9 s call cut-off is still allowed
    assert env_number({"X": "1.5e2"}, "X", float) == 150.0
    assert env_number({"X": "-0"}, "X", int) == 0


@pytest.mark.parametrize("junk", ["١٢٣", "1_000", "5 5", " 5", "+", "", "1.5", "0x10", "1e3"])
def test_a_whole_number_must_be_plain_ascii_digits(junk: str) -> None:
    with pytest.raises(SettingRefused, match="X is not a number"):
        env_number({"X": junk}, "X", int)


@pytest.mark.parametrize("junk", ["١", "1_0", "5 5", " 5", ".", "e5", "1e", "nan", "inf", "Infinity"])
def test_a_decimal_must_be_plain_ascii(junk: str) -> None:
    for kind in (float, Decimal):
        with pytest.raises(SettingRefused, match="X is not a number"):
            env_number({"X": junk}, "X", kind)


@pytest.mark.parametrize(
    ("extra", "name"),
    [
        ({"DESK_PORT": "99999"}, "DESK_PORT"),
        ({"DESK_PORT": "-1"}, "DESK_PORT"),
        ({"DESK_STALL_SECONDS": str(10**4000)}, "DESK_STALL_SECONDS"),
        ({"DESK_SIZE_CAP_BYTES": "99999999999"}, "DESK_SIZE_CAP_BYTES"),
        ({"DESK_SEND_TIMEOUT_SECONDS": "1e999"}, "DESK_SEND_TIMEOUT_SECONDS"),
        ({"DESK_SEND_TIMEOUT_SECONDS": "nan"}, "DESK_SEND_TIMEOUT_SECONDS"),
        ({"DESK_OUTBOX": "smtp_provider", **PROVIDER, "DESK_SMTPP_MAX_SENDS": "99999999999"}, "DESK_SMTPP_MAX_SENDS"),
        ({"DESK_CRM": "hubspot", **HUBSPOT, "DESK_HUBSPOT_MAX_CALLS": str(10**4000)}, "DESK_HUBSPOT_MAX_CALLS"),
        ({"DESK_OUTBOX": "smtp", "DESK_SMTP_PORT": "0"}, "DESK_SMTP_PORT"),
    ],
    ids=[
        "port-big",
        "port-neg",
        "stall-huge",
        "size-big",
        "timeout-inf",
        "timeout-nan",
        "sends-big",
        "calls-huge",
        "smtp-port-0",
    ],
)
def test_an_out_of_range_setting_ends_the_desk_with_exit_4_naming_it(
    tmp_path: Path, extra: dict[str, str], name: str
) -> None:
    r = _run_desk(tmp_path, extra)
    out = r.stdout + r.stderr
    assert r.returncode == 4, out[-600:]
    assert f"the desk refuses a setting: {name}" in r.stdout
    assert "Traceback" not in out


def test_a_plain_value_error_that_is_not_a_setting_is_not_called_a_setting_refusal(tmp_path: Path) -> None:
    replies = tmp_path / "replies.json"
    replies.write_text("{not json", encoding="utf-8")
    r = _run_desk(tmp_path, {"DESK_REPLIES_PATH": str(replies)})
    assert r.returncode != 4, (r.stdout + r.stderr)[-600:]
    assert "refuses a setting" not in r.stdout


# ---------------------------------------------------------------- accepted spellings, pinned refusal rules, bad addresses
@pytest.mark.parametrize(
    ("cast", "text", "expected"),
    [
        (int, "+5", 5),
        (int, "00", 0),
        (int, "-0", 0),
        (int, "8765", 8765),
        (int, " 8765 ", 8765),  # a shell line `set DESK_PORT=8765 `, an env file with a trailing space
        (int, "8765\r\n", 8765),
        (int, "\t7\t", 7),
        (float, ".5", 0.5),
        (float, "5.", 5.0),
        (float, "0.25", 0.25),
        (float, "1e3", 1000.0),
        (float, "1E-2", 0.01),
        (float, "-0", 0.0),
        (Decimal, "0.25", Decimal("0.25")),
        (Decimal, "5", Decimal(5)),
        (Decimal, " 1.5 ", Decimal("1.5")),
    ],
)
def test_these_spellings_are_accepted(cast: Any, text: str, expected: Any) -> None:
    assert env_number({"X": text}, "X", cast) == expected


@pytest.mark.parametrize(
    ("cast", "text", "rule"),
    [
        (int, "1_000", "an underscore is refused"),
        (float, "1_0.5", "an underscore is refused"),
        (int, "١٢٣", "digits of another script are refused"),
        (Decimal, "١", "digits of another script are refused"),
        (int, " 5", "a no-break space is not a plain space: refused"),
        (int, "5 5", "a space inside the number is refused"),
        (int, "", "an empty value is refused"),
        (int, "   ", "spaces only is refused"),
        (int, "+", "a sign alone is refused"),
        (int, "0x10", "hexadecimal is refused"),
        (int, "1.5", "a point is not allowed in a whole number"),
        (int, "1e3", "an exponent is not allowed in a whole number"),
        (float, ".", "a point alone is refused"),
        (float, "e5", "an exponent alone is refused"),
        (float, "1e", "a broken exponent is refused"),
        (float, "nan", "nan is refused"),
        (float, "inf", "inf is refused"),
        (Decimal, "Infinity", "Infinity is refused"),
        (float, "1e999", "a value that overflows to infinity is refused"),
        (int, "9" * 4400, "a whole number of more than 4300 digits is refused"),
    ],
)
def test_each_refusal_rule_is_pinned(cast: Any, text: str, rule: str) -> None:
    with pytest.raises(SettingRefused, match="X is not a number"):
        env_number({"X": text}, "X", cast)


BAD_ADDRESSES = ["http://[/", "http://[::1", "http://[a"]


@pytest.mark.parametrize("bad", BAD_ADDRESSES)
@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("DESK_FAKES_URL", lambda v: Config.from_env({"DESK_FAKES_URL": v})),
        ("DESK_LIVE_BASE_URL", lambda v: Config.from_env({**LIVE, "DESK_LIVE_BASE_URL": v})),
        ("DESK_HUBSPOT_BASE_URL", lambda v: HubspotSettings.from_env({**HUBSPOT, "DESK_HUBSPOT_BASE_URL": v})),
    ],
    ids=["fakes-url", "live-base-url", "hubspot-base-url"],
)
def test_an_address_setting_that_cannot_be_split_is_a_named_refusal(name: str, build: Any, bad: str) -> None:
    with pytest.raises(SettingRefused, match=name) as e:
        build(bad)
    assert bad not in str(e.value)


@pytest.mark.parametrize(
    ("extra", "name"),
    [
        ({"DESK_FAKES_URL": "http://[/"}, "DESK_FAKES_URL"),
        ({"DESK_FAKES_URL": "http://[::1"}, "DESK_FAKES_URL"),
        ({"DESK_QUALIFIER": "live_claude", **LIVE, "DESK_LIVE_BASE_URL": "http://[/"}, "DESK_LIVE_BASE_URL"),
        ({"DESK_CRM": "hubspot", **HUBSPOT, "DESK_HUBSPOT_BASE_URL": "http://[::1"}, "DESK_HUBSPOT_BASE_URL"),
    ],
    ids=["fakes-url", "fakes-url-ipv6", "live-base-url", "hubspot-base-url"],
)
def test_a_bad_address_setting_ends_the_desk_with_exit_4_naming_it(
    tmp_path: Path, extra: dict[str, str], name: str
) -> None:
    r = _run_desk(tmp_path, extra)
    out = r.stdout + r.stderr
    assert r.returncode == 4, out[-600:]
    assert f"the desk refuses a setting: {name} is not a valid address" in r.stdout
    assert "Traceback" not in out


def test_stall_seconds_zero_is_accepted() -> None:
    """0 is accepted ("must not be negative"). With 0 every open receipt is older than the
    cut-off, so the reaper decides it at its next pass and the review list shows it as stalled at once; the verdict is
    written once under the decide lock, so that is early, not wrong (docs/settings.md says so)."""
    assert Config(stall_seconds=0).stall_seconds == 0
    assert Config.from_env({"DESK_STALL_SECONDS": "0"}).stall_seconds == 0
    with pytest.raises(SettingRefused, match="DESK_STALL_SECONDS"):
        Config.from_env({"DESK_STALL_SECONDS": "-1"})


# ---------------------------------------------------------------- the scan: no bare number cast on environment text
ENV_NAMES = {
    "env",
    "environ",
    "config_env",
    "os_environ",
}  # plus any parameter typed Mapping[str, str] or dict[str, str]
CASTS = {"int", "float", "Decimal"}
SANITIZERS = {
    "env_number",
    "number",
    "positive_seconds",
}  # these return a checked number: not environment text any more
LOADERS = {"load_env", "getenv"}  # calls whose result is settings text
ENV_KEY = re.compile(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+")  # a literal key such as "DESK_PORT" marks a settings mapping
SCAN_FOLDERS = ("src", "scripts", "demo_only", "load_tools", "tools")


def _direct_only(source: str) -> list[int]:
    """A direct-only scan (the contrast for the full one): a cast whose own argument
    mentions an env name, a Subscript/Call on one, or `.environ` / `getenv`."""
    hits = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in CASTS and n.args:
            for m in ast.walk(n.args[0]):
                if isinstance(m, ast.Subscript | ast.Call):
                    base = m.value if isinstance(m, ast.Subscript) else getattr(m.func, "value", None)
                    if isinstance(base, ast.Name) and base.id in ENV_NAMES:
                        hits.append(n.lineno)
                        break
                    if isinstance(base, ast.Attribute) and base.attr == "environ":
                        hits.append(n.lineno)
                        break
    return hits


def _is_cast_expr(node: ast.AST, aliases: set[str]) -> bool:
    """int, float, Decimal, decimal.Decimal, builtins.int, an alias of one, or functools.partial(<one of these>)."""
    if isinstance(node, ast.Name):
        return node.id in CASTS or node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in CASTS
    if isinstance(node, ast.Call):
        fn = node.func
        is_partial = (isinstance(fn, ast.Name) and fn.id == "partial") or (
            isinstance(fn, ast.Attribute) and fn.attr == "partial"
        )
        return is_partial and bool(node.args) and _is_cast_expr(node.args[0], aliases)
    return False


def _cast_aliases(tree: ast.AST) -> set[str]:
    """Names that stand for a number cast: `_i = int`, `conv = functools.partial(int)`, `from decimal import Decimal
    as D`, `from builtins import int as I`."""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module in ("decimal", "builtins"):
                for a in n.names:
                    if a.name in CASTS and (a.asname or a.name) not in aliases and a.asname:
                        aliases.add(a.asname)
                        changed = True
            if isinstance(n, ast.Assign | ast.AnnAssign) and n.value is not None and _is_cast_expr(n.value, aliases):
                for tgt in n.targets if isinstance(n, ast.Assign) else [n.target]:  # `conv: Any = int` too
                    if isinstance(tgt, ast.Name) and tgt.id not in aliases:
                        aliases.add(tgt.id)
                        changed = True
    return aliases


def _scopes(tree: ast.AST) -> list[ast.AST]:
    """The module and every function."""
    return [tree, *[n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]]


def _walk_scope(scope: ast.AST) -> list[ast.AST]:
    """Every node of a scope; the module scope leaves out the function bodies (they are scopes of their own)."""
    if not isinstance(scope, ast.Module):
        return list(ast.walk(scope))
    out: list[ast.AST] = []
    stack: list[ast.AST] = [scope]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(c for c in ast.iter_child_nodes(n) if not isinstance(c, ast.FunctionDef | ast.AsyncFunctionDef))
    return out


def _touches(node: ast.AST, tainted: set[str], attrs: set[str]) -> bool:
    """Does this expression hold settings text? A tainted name, `.environ`/`getenv`, an attribute that was assigned
    settings text somewhere in the file (`self.raw`), a call of a loader, or a subscript / `.get` with a literal key
    that looks like a setting name (`cfg["DESK_PORT"]`), whatever the mapping is called."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in SANITIZERS:
        return False  # a checked number is not environment text any more
    for m in ast.walk(node):
        if isinstance(m, ast.Name) and m.id in tainted:
            return True
        if isinstance(m, ast.Attribute) and (m.attr in ("environ", "getenv", "environb") or m.attr in attrs):
            return True
        if isinstance(m, ast.Call):
            name = m.func.id if isinstance(m.func, ast.Name) else getattr(m.func, "attr", "")
            if name in LOADERS:
                return True
            if name == "get" and m.args and isinstance(m.args[0], ast.Constant) and isinstance(m.args[0].value, str):
                if ENV_KEY.fullmatch(m.args[0].value):
                    return True
        if isinstance(m, ast.Subscript) and isinstance(m.slice, ast.Constant) and isinstance(m.slice.value, str):
            if ENV_KEY.fullmatch(m.slice.value):
                return True
    return False


def _bound(target: ast.AST) -> list[ast.AST]:
    """The names and attributes an assignment target binds (a store into `x[k]` binds neither)."""
    if isinstance(target, ast.Name | ast.Attribute):
        return [target]
    if isinstance(target, ast.Tuple | ast.List):
        return [b for el in target.elts for b in _bound(el)]
    if isinstance(target, ast.Starred):
        return _bound(target.value)
    return []


def _analyse(tree: ast.AST) -> tuple[dict[int, set[str]], set[str]]:
    """Names (per scope) and attributes that hold settings text. Module-level taint is visible in every function; an
    attribute assigned settings text anywhere in the file (`self.raw = ...`) is tainted wherever it is read."""
    imported: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "os":
            imported.update(a.asname or a.name for a in n.names if a.name in ("environ", "getenv", "environb"))
    scopes = _scopes(tree)
    by_scope: dict[int, set[str]] = {id(s): set(ENV_NAMES) | imported for s in scopes}
    attrs: set[str] = set()
    for s in scopes:
        if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in (*s.args.posonlyargs, *s.args.args, *s.args.kwonlyargs):
                if arg.annotation is not None and ast.unparse(arg.annotation).startswith(
                    ("Mapping[str, str]", "dict[str, str]")
                ):
                    by_scope[id(s)].add(arg.arg)  # def f(e: Mapping[str, str]): e is a settings mapping
    changed = True
    while changed:
        changed = False
        for s in scopes:
            cur = by_scope[id(s)]
            if s is not tree:
                cur |= by_scope[id(tree)]  # module-level taint reaches the functions
            for n in _walk_scope(s):
                targets: list[ast.expr] = []
                value: ast.AST | None = None
                if isinstance(n, ast.Assign):
                    targets, value = list(n.targets), n.value
                elif isinstance(n, ast.AnnAssign) and n.value is not None:
                    targets, value = [n.target], n.value
                elif isinstance(n, ast.NamedExpr | ast.AugAssign):
                    targets, value = [n.target], n.value
                elif isinstance(n, ast.For | ast.comprehension):
                    targets, value = [n.target], n.iter
                if value is None or not _touches(value, cur, attrs):
                    continue
                for tgt in targets:
                    for node in _bound(tgt):
                        if isinstance(node, ast.Name) and node.id not in cur:
                            cur.add(node.id)
                            changed = True
                        if isinstance(node, ast.Attribute) and node.attr not in attrs:
                            attrs.add(node.attr)
                            changed = True
    return by_scope, attrs


def _bare_casts(source: str) -> list[int]:
    """Lines where int(), float() or Decimal() (also under an alias, `decimal.Decimal`, `functools.partial`, `map`, or
    as an argparse `type=`) is applied to settings text. COVERED: text read from `os.environ`, `os.getenv`, a name
    imported from os, a parameter typed `Mapping[str, str]` or `dict[str, str]`, `load_env(...)`, or any mapping read with a literal key that
    looks like a setting name; followed through assignments, loops, walrus, module-level names, and attributes such as
    `self.raw`. NOT COVERED: taint that travels through a function's return value or through a container passed
    between functions, a cast reached through getattr/eval, and numbers read from other files."""
    tree = ast.parse(source)
    aliases = _cast_aliases(tree)
    by_scope, attrs = _analyse(tree)
    hits: set[int] = set()
    for scope in _scopes(tree):
        tainted = by_scope[id(scope)]
        for n in _walk_scope(scope):
            if not isinstance(n, ast.Call):
                continue
            if _is_cast_expr(n.func, aliases) and any(_touches(a, tainted, attrs) for a in n.args):
                hits.add(n.lineno)
            if isinstance(n.func, ast.Name) and n.func.id == "map" and n.args and _is_cast_expr(n.args[0], aliases):
                if any(_touches(a, tainted, attrs) for a in n.args[1:]):
                    hits.add(n.lineno)
            if isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument":
                kws = {k.arg: k.value for k in n.keywords}
                kind = kws.get("type")
                if kind is not None and _is_cast_expr(kind, aliases) and "default" in kws:
                    if _touches(kws["default"], tainted, attrs):
                        hits.add(n.lineno)
    return sorted(hits)


EVASIVE = {
    "two step": 'raw = env.get("X")\nx = int(raw)',
    "two step alias chain": 'raw = os.environ["X"]\nv = raw.strip()\nx = float(v)',
    "decimal attribute": 'x = decimal.Decimal(env["X"])',
    "builtins attribute": 'x = builtins.int(env["X"])',
    "from os import getenv": 'from os import getenv\nx = int(getenv("X"))',
    "from os import environ renamed": 'from os import environ as E\nx = int(E["X"])',
    "environ copied to another name": 'cfg = dict(os.environ)\nx = int(cfg["X"])',
    "environ copied by merge": 'cfg = {**os.environ, "A": "1"}\nx = int(cfg["A"])',
    "strip after alias": 'raw = os.environ.get("X", "1")\nx = int(raw.strip())',
    "map int": 'parts = os.environ["X"].split(",")\nxs = list(map(int, parts))',
    "argparse type with env default": 'ap.add_argument("--n", type=int, default=os.environ["N"])',
    "for loop over env text": 'for v in os.environ["X"].split(","):\n    x = int(v)',
    "walrus": "if (raw := env.get('X')):\n    x = int(raw)",
    "module level name read in a function": 'RAW = os.environ["X"]\ndef f():\n    return int(RAW)',
    "self attribute across methods": (
        'class Holder(object):\n    def a(self):\n        self.raw = os.environ["X"]\n    def b(self):\n        return int(self.raw)'
    ),
    "cast alias": '_i = int\nx = _i(os.environ["X"])',
    "annotated cast alias": 'conv: Any = int\nx = conv(os.environ["X"])',
    "Decimal imported as D": 'from decimal import Decimal as D\nx = D(os.environ["X"])',
    "functools.partial": 'import functools\nconv = functools.partial(int)\nx = conv(os.environ["X"])',
    "unannotated mapping, literal key": 'def f(cfg):\n    return int(cfg["DESK_PORT"])',
    "unannotated mapping, get with literal key": 'def f(settings):\n    return float(settings.get("REAL_PERSONS", "3"))',
    "dict[str, str] typed parameter": 'def f(e: dict[str, str]):\n    return int(e["A"])',
    "load_env result": 'cfg = load_env(path)\nx = int(cfg["Y"])',
    "load_env result inside a function": 'def f(path):\n    c = load_env(path)\n    return int(c["n"])',
}


@pytest.mark.parametrize("label", sorted(EVASIVE))
def test_the_scan_catches_each_evasive_form_that_a_direct_only_scan_misses(label: str) -> None:
    src = EVASIVE[label]
    assert _bare_casts(src), f"the scan missed: {label}"
    assert not _direct_only(src), f"a direct-only scan catches this form too: {label}"


def test_the_scan_catches_the_direct_forms_and_lets_checked_numbers_pass() -> None:
    assert _bare_casts('x = int(env["DESK_PORT"])') == [1]
    assert _bare_casts('def f(e: Mapping[str, str]):\n    return int(e["A"])') == [2]  # a typed settings mapping
    assert _bare_casts('def f(e):\n    e = os.environ\n    return int(e["A"])') == [3]
    assert _bare_casts('x = float(os.environ.get("A", "1"))') == [1]
    assert _bare_casts('x = Decimal(env["A"])') == [1]
    assert _bare_casts('x = env_number(e, "A", int)') == []
    assert _bare_casts('p = number("A", env, int, 3)\nx = int(p)') == []  # a checked number is not environment text
    assert _bare_casts("x = int(other)") == []
    assert _bare_casts('data = json.loads(s)\nx = int(data["a"])') == []
    assert _bare_casts("def f(code):\n    return int(code)") == []  # a name that is not settings text
    assert _bare_casts('def f(row):\n    return int(row["count"])') == []  # a lower-case key is not a setting name


def test_no_bare_int_float_or_decimal_is_applied_to_environment_text() -> None:
    problems = []
    for folder in SCAN_FOLDERS:
        for path in sorted((REPO / folder).rglob("*.py")):
            for line in _bare_casts(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(REPO)}:{line}")
    assert not problems, "use lead_desk.envnum.env_number instead of a bare cast: " + ", ".join(problems)


# ---------------------------------------------------------------- the guard: every env_number call gives a real ceiling
CHECKED_CALLS = {"env_number", "number"}  # `number` (scripts/run_real_check.py) passes its own `high` on to env_number
GUARD_ALLOW = {("src/lead_desk/config.py", "_dec")}  # money reader: its range is enforced by LiveSettings.__post_init__


def _unchecked_calls(source: str, rel: str = "x.py") -> list[int]:
    """Lines of calls to env_number / number (bare name, `module.env_number`, an `import ... as` / `from ... import ... as`
    alias, an assignment alias `x = env_number`, or `functools.partial(env_number, ...)`, at module level or in a function)
    that pass no `high=` or `high=None`. The allow-list matches the QUALIFIED function name (`_dec`, not `outer._dec` or
    `_dec.inner`)."""
    tree = ast.parse(source)
    names = set(CHECKED_CALLS)

    def refers(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in names
        if isinstance(node, ast.Attribute):
            return node.attr in names
        return False

    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            names.update(a.asname for a in n.names if a.asname and a.name in CHECKED_CALLS)
    changed = True
    while changed:  # an assignment alias: alias = env_number
        changed = False
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign | ast.AnnAssign) and n.value is not None and refers(n.value):
                for tgt in n.targets if isinstance(n, ast.Assign) else [n.target]:  # `parse: Any = env_number` too
                    if isinstance(tgt, ast.Name) and tgt.id not in names:
                        names.add(tgt.id)
                        changed = True
    hits: list[int] = []

    def visit(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                f"{qual}.{child.name}"
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and qual
                else (child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else qual)
            )
            if isinstance(child, ast.Call):
                f = child.func
                called = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                is_partial = called == "partial" and bool(child.args) and refers(child.args[0])
                if (called in names or is_partial) and (rel, qual) not in GUARD_ALLOW:
                    high = next((k.value for k in child.keywords if k.arg == "high"), None)
                    if high is None or (isinstance(high, ast.Constant) and high.value is None):
                        hits.append(child.lineno)
            visit(child, inner)

    visit(tree, "")
    return hits


def _name_only_guard(source: str) -> list[int]:
    """A name-only guard: bare-name env_number calls inside a function
    that have no `high` keyword at all; `_dec` and `number` exempt."""
    hits = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) or fn.name in ("_dec", "number"):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "env_number":
                if not any(k.arg == "high" for k in n.keywords):
                    hits.append(n.lineno)
    return hits


GUARD_EVASIVE = {
    "high=None": 'def f(e):\n    return env_number(e, "X", int, high=None)',
    "module.env_number": 'def f(e):\n    return envnum.env_number(e, "X", int)',
    "from import as alias": "from lead_desk.envnum import env_number as en\ndef f(e):\n    return en(e, 'X', int)",
    "module level call": 'X = env_number(ENV, "X", int)',
    "the number helper without high": 'def f(env):\n    return number("X", env, int, 3)',
    "assignment alias": 'alias = env_number\ndef f(e):\n    return alias(e, "X", int)',
    "annotated assignment alias": 'parse: Any = env_number\ndef f(e):\n    return parse(e, "X", int)',
    "functools.partial of env_number": 'import functools\ng = functools.partial(env_number, ENV, "X", int)',
    "attribute on an imported module alias": (
        'import lead_desk.envnum as m\ndef f(e):\n    return m.env_number(e, "X", int)'
    ),
}


@pytest.mark.parametrize("label", sorted(GUARD_EVASIVE))
def test_the_guard_catches_each_form_the_name_only_guard_misses(label: str) -> None:
    src = GUARD_EVASIVE[label]
    assert _unchecked_calls(src), f"the guard missed: {label}"
    assert not _name_only_guard(src), f"the name-only guard already caught: {label}"


def test_the_allow_list_matches_the_qualified_name_only() -> None:
    cfg = "src/lead_desk/config.py"
    ok = "def _dec(e, n):\n    return env_number(e, n, Decimal)"
    assert not _unchecked_calls(ok, cfg)  # the money reader itself
    nested = "def _dec(e, n):\n    def inner():\n        return env_number(e, n, Decimal)\n    return inner"
    assert _unchecked_calls(nested, cfg)  # a function nested inside it is not allow-listed
    other = 'def outer():\n    def _dec():\n        return env_number(e, "X", int)'
    assert _unchecked_calls(other, cfg)  # a nested function that is merely named _dec is not allow-listed
    assert _unchecked_calls(ok, "src/lead_desk/other.py")  # nor is the same name in another file


def test_the_guard_accepts_a_real_ceiling() -> None:
    assert not _unchecked_calls('def f(e):\n    return env_number(e, "X", int, high=5)')
    assert not _unchecked_calls('def f(env):\n    return number("X", env, int, 3, high=9)')
    assert not _unchecked_calls('def f(e):\n    return env_number(e, "X", int, high=CAP_COUNT_MAX)')


def test_every_env_number_call_in_the_code_gives_a_ceiling() -> None:
    """A call without `high=` accepts any size (10**4000 reaches a cap or a clock). Only the money reader `_dec` (its
    range is checked by LiveSettings) is allow-listed; `envnum.py` defines the helper itself."""
    problems = []
    for folder in SCAN_FOLDERS:
        for path in sorted((REPO / folder).rglob("*.py")):
            if path.name == "envnum.py":
                continue
            rel = path.relative_to(REPO).as_posix()
            problems += [f"{rel}:{line}" for line in _unchecked_calls(path.read_text(encoding="utf-8"), rel)]
    assert not problems, "env_number / number without a real high=: " + ", ".join(problems)
