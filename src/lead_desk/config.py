"""The one validated home for ports and defaults."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from lead_desk.constants import (
    ADAPTER_CHOICES,
    APPROVER_ID_PATTERN,
    APPROVER_NAME_MAX,
    APPROVERS_FILE_MAX_BYTES,
    CRM_CHOICES,
    CRM_STAND_IN,
    HOST_NAME_MAX,
    LATER_MODES,
    LEADFLOW_CRM_VALUE_PATTERN,
    LEADFLOW_FILE_MAX_BYTES,
    LEADFLOW_ID_PATTERN,
    LEADFLOW_LABEL_MAX,
    LEADFLOW_NAME_MAX,
    LEADFLOW_OFF,
    LEADFLOW_ROTA_MAX,
    LEADFLOW_STEPS_MAX,
    LIVE_API_VERSION,
    LIVE_DEFAULT_BASE_URL,
    LIVE_DEFAULT_EFFORT,
    LIVE_DEFAULT_MAX_TOKENS,
    LIVE_DEFAULT_TIMEOUT_SECONDS,
    LIVE_LEDGER_USD_MAX,
    LIVE_MAX_TOKENS_MAX,
    LIVE_PRICE_MAX,
    LIVE_PRICE_MIN,
    LOOPBACK_HOSTS,
    MODEL_CALL_BUDGET_SECONDS,
    NUDGE_AFTER_MAX_SECONDS,
    OUTBOX_CHOICES,
    OUTBOX_STAND_INS,
    PORT_MAX,
    QUALIFIER_CHOICES,
    QUALIFIER_STAND_IN,
    SEND_TIMEOUT_SECONDS_MAX,
    SIZE_CAP_BYTES_MAX,
    STALL_SECONDS_MAX,
    WILDCARD_BINDS,
)
from lead_desk.envnum import SettingRefused, env_number, split_url
from lead_desk.identity import PLAIN_ADDRESS, encodable, has_control_char
from lead_desk.urls import desk_origin

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_NAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$|^\[[0-9a-f:]+\]$")


def is_loopback_host(name: str) -> bool:
    """True for localhost and for a loopback address (brackets allowed); a wildcard, a name or any other address is
    reachable from, or reaches, other computers. Fails closed: anything it cannot read is not loopback."""
    if name.lower() == "localhost":
        return True
    bare = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    if "[" in bare or "]" in bare or "%" in bare:  # one bracket, or a scope id (::1%eth0): not a plain address
        return False
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # the same answer on every Python version (3.13 changed is_loopback for mapped forms)
    return ip.is_loopback


def is_unspecified(name: str) -> bool:
    """True for a bind name that means "every interface", in any spelling: the all-zeros IPv4 address, ::, [::], and
    the short forms the system resolver reads as all zeros (a lone digit string such as the single character 0)."""
    bare = name.strip("[]")
    try:
        return ipaddress.ip_address(bare).is_unspecified
    except ValueError:
        pass
    try:
        return socket.inet_aton(bare) == bytes(4)
    except (OSError, ValueError):  # a name the system resolver cannot read (embedded NUL, lone surrogate)
        return False


_LIVE_ENV = {
    "model": "DESK_LIVE_MODEL",
    "price_date": "DESK_LIVE_PRICE_DATE",
    "run_id": "DESK_LIVE_RUN_ID",
    "effort": "DESK_LIVE_EFFORT",
    "api_version": "DESK_LIVE_API_VERSION",
    "base_url": "DESK_LIVE_BASE_URL",
    "price_in_per_mtok": "DESK_LIVE_PRICE_IN_PER_MTOK",
    "price_out_per_mtok": "DESK_LIVE_PRICE_OUT_PER_MTOK",
    "cap_run_usd": "DESK_LIVE_CAP_RUN_USD",
    "cap_total_usd": "DESK_LIVE_CAP_TOTAL_USD",
}


def _live_env(field_name: str) -> str:
    """The setting name that fills a LiveSettings field (so every refusal names the setting)."""
    return _LIVE_ENV.get(field_name, field_name)


@dataclass(frozen=True)
class LiveSettings:
    """Settings of the live model step (live_claude.py). Money-class: the model id, the prices (with the date they
    were read from the vendor's page), both caps and the ledger path have NO default; the caller sets them."""

    model: str
    key_file: Path = field(repr=False)  # the key is read from this file only; it is never an argument, never printed
    price_in_per_mtok: Decimal  # US$ per million input tokens
    price_out_per_mtok: Decimal  # US$ per million output tokens
    price_date: str  # when the prices were read from the vendor's page (any non-empty text, for the cost log)
    cap_run_usd: Decimal
    cap_total_usd: Decimal
    ledger_path: Path
    run_id: str
    base_url: str = LIVE_DEFAULT_BASE_URL
    api_version: str = LIVE_API_VERSION
    timeout_seconds: float = LIVE_DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = LIVE_DEFAULT_MAX_TOKENS
    effort: str = LIVE_DEFAULT_EFFORT  # output_config effort sent with every call
    allow_any_base_url: bool = False  # True lifts the check that the key goes only to the provider or this computer

    def __post_init__(self) -> None:
        for text_name in ("model", "price_date", "run_id", "effort", "api_version", "base_url"):
            if not isinstance(getattr(self, text_name), str):
                raise SettingRefused(f"{_live_env(text_name)} must be text")
        if not isinstance(self.allow_any_base_url, bool):
            raise SettingRefused("DESK_LIVE_ALLOW_ANY_BASE_URL must be True or False")
        if not isinstance(self.key_file, Path) or not isinstance(self.ledger_path, Path):
            raise SettingRefused("DESK_LIVE_KEY_FILE and DESK_LIVE_LEDGER_PATH must be paths")
        if type(self.max_tokens) is not int or type(self.timeout_seconds) not in (int, float):
            raise SettingRefused("DESK_LIVE_MAX_TOKENS must be a whole number and DESK_LIVE_TIMEOUT_SECONDS a number")
        if not self.model.strip() or not self.price_date.strip() or not self.run_id.strip():
            raise SettingRefused("DESK_LIVE_MODEL, DESK_LIVE_PRICE_DATE and DESK_LIVE_RUN_ID must not be empty")
        for name in ("price_in_per_mtok", "price_out_per_mtok", "cap_run_usd", "cap_total_usd"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
                raise SettingRefused(f"{_live_env(name)} must be a positive number")
            low, high = (
                (Decimal(LIVE_PRICE_MIN), Decimal(LIVE_PRICE_MAX))
                if name.startswith("price")
                else (Decimal(0), Decimal(LIVE_LEDGER_USD_MAX))
            )
            if v > high or v < low:  # a price that underflows or overflows the worst-case sum would void the cap
                raise SettingRefused(f"{_live_env(name)} is outside the allowed range")
        if self.cap_run_usd > self.cap_total_usd:
            raise SettingRefused("DESK_LIVE_CAP_RUN_USD is above DESK_LIVE_CAP_TOTAL_USD")
        if not 0 < self.timeout_seconds <= MODEL_CALL_BUDGET_SECONDS:
            raise SettingRefused("DESK_LIVE_TIMEOUT_SECONDS must be above 0 and at most the desk's AI call budget")
        if not self.effort.strip() or not self.api_version.strip():
            raise SettingRefused("DESK_LIVE_EFFORT and DESK_LIVE_API_VERSION must not be empty")
        if not 1 <= self.max_tokens <= LIVE_MAX_TOKENS_MAX:
            raise SettingRefused("DESK_LIVE_MAX_TOKENS must be between 1 and the allowed maximum")
        parts = split_url("DESK_LIVE_BASE_URL", self.base_url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise SettingRefused("DESK_LIVE_BASE_URL must be an http(s) address with a host and no path")
        if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise SettingRefused("DESK_LIVE_BASE_URL: plain http is allowed for a loopback address only")
        host = parts.hostname.lower()
        own = (urlsplit(LIVE_DEFAULT_BASE_URL).hostname or "").lower()
        if not self.allow_any_base_url and host != own and host not in {h.strip("[]") for h in LOOPBACK_HOSTS}:
            raise SettingRefused(
                "DESK_LIVE_BASE_URL is not the provider's own address (DESK_LIVE_ALLOW_ANY_BASE_URL=yes allows it)"
            )


def _dec(e: Mapping[str, str], name: str) -> Decimal:
    if name not in e:
        raise SettingRefused(f"{name} is required for the live model")
    return env_number(e, name, Decimal)  # type: ignore[no-any-return]


def _live_from_env(e: Mapping[str, str]) -> LiveSettings:
    need = (
        "DESK_LIVE_MODEL",
        "DESK_LIVE_KEY_FILE",
        "DESK_LIVE_PRICE_DATE",
        "DESK_LIVE_LEDGER_PATH",
        "DESK_LIVE_RUN_ID",
    )
    for n in need:
        if not e.get(n, "").strip():
            raise SettingRefused(f"{n} is required for the live model")
    kw: dict[str, Any] = {}
    if "DESK_LIVE_BASE_URL" in e:
        kw["base_url"] = e["DESK_LIVE_BASE_URL"]
    if "DESK_LIVE_API_VERSION" in e:
        kw["api_version"] = e["DESK_LIVE_API_VERSION"]
    if "DESK_LIVE_TIMEOUT_SECONDS" in e:
        kw["timeout_seconds"] = env_number(e, "DESK_LIVE_TIMEOUT_SECONDS", float, high=MODEL_CALL_BUDGET_SECONDS)
    if "DESK_LIVE_EFFORT" in e:
        kw["effort"] = e["DESK_LIVE_EFFORT"]
    if "DESK_LIVE_MAX_TOKENS" in e:
        kw["max_tokens"] = env_number(e, "DESK_LIVE_MAX_TOKENS", int, low=1, high=LIVE_MAX_TOKENS_MAX)
    if e.get("DESK_LIVE_ALLOW_ANY_BASE_URL", "").strip().lower() == "yes":
        kw["allow_any_base_url"] = True
    return LiveSettings(
        model=e["DESK_LIVE_MODEL"],
        key_file=Path(e["DESK_LIVE_KEY_FILE"]),
        price_in_per_mtok=_dec(e, "DESK_LIVE_PRICE_IN_PER_MTOK"),
        price_out_per_mtok=_dec(e, "DESK_LIVE_PRICE_OUT_PER_MTOK"),
        price_date=e["DESK_LIVE_PRICE_DATE"],
        cap_run_usd=_dec(e, "DESK_LIVE_CAP_RUN_USD"),
        cap_total_usd=_dec(e, "DESK_LIVE_CAP_TOTAL_USD"),
        ledger_path=Path(e["DESK_LIVE_LEDGER_PATH"]),
        run_id=e["DESK_LIVE_RUN_ID"],
        **kw,
    )


_APPROVER_ID = re.compile(APPROVER_ID_PATTERN, re.ASCII)
_APPROVER_FIELDS = ("id", "name")  # one approver = (id, name)


def _check_approvers(approvers: tuple[tuple[str, str], ...], label: str) -> None:
    """The one check of an approvers list (Config runs it, so the list the loader fills in is checked too). Messages
    name the setting and the entry number, never the text."""
    if not isinstance(label, str) or has_control_char(label) or not encodable(label) or len(label) > APPROVER_NAME_MAX:
        raise SettingRefused("the approvers list label is not one short line of text")
    ids: set[str] = set()
    names: set[str] = set()
    for n, entry in enumerate(approvers, 1):
        ok = isinstance(entry, tuple) and len(entry) == len(_APPROVER_FIELDS) and all(isinstance(x, str) for x in entry)
        if not ok or not _APPROVER_ID.fullmatch(entry[0]):
            raise SettingRefused(f"approver {n}: the id is not a short lower-case id")
        if not 1 <= len(entry[1]) <= APPROVER_NAME_MAX or has_control_char(entry[1]) or not encodable(entry[1]):
            raise SettingRefused(f"approver {n}: the name must be 1 to {APPROVER_NAME_MAX} characters on one line")
        if entry[0] in ids or entry[1] in names:
            raise SettingRefused(f"approver {n}: the id or the name is used twice")
        ids.add(entry[0])
        names.add(entry[1])


def read_approvers(path: Path) -> tuple[tuple[tuple[str, str], ...], str]:
    """Read an approvers file: JSON `{"label": "...", "approvers": [{"id": "...", "name": "..."}, ...]}` (label is
    optional). Returns ((id, name), ...) and the label. Only the file shape is checked here; Config checks the
    values. Raises SettingRefused (naming DESK_APPROVERS_FILE and the entry number, never the text)."""
    try:
        with path.open("rb") as f:
            raw = f.read(APPROVERS_FILE_MAX_BYTES + 1)
    except OSError:
        raise SettingRefused("the approvers file cannot be read") from None
    if len(raw) > APPROVERS_FILE_MAX_BYTES:
        raise SettingRefused("the approvers file is too big")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeError is a ValueError
        raise SettingRefused("the approvers file is not valid JSON text") from None
    listed = data.get("approvers") if isinstance(data, dict) else None
    label = data.get("label", "") if isinstance(data, dict) else ""
    if not isinstance(listed, list) or not isinstance(label, str):
        raise SettingRefused("the approvers file needs a list named approvers (and a text label, if any)")
    out: list[tuple[str, str]] = []
    for n, item in enumerate(listed, 1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("name"), str):
            raise SettingRefused(f"approver {n}: each entry needs a text id and a text name")
        out.append((item["id"], item["name"]))
    return tuple(out), label


_LEADFLOW_ID = re.compile(LEADFLOW_ID_PATTERN, re.ASCII)
_LEADFLOW_CRM_VALUE = re.compile(LEADFLOW_CRM_VALUE_PATTERN, re.ASCII)
_LEADFLOW_TOP_KEYS = frozenset(
    {"rota", "steps", "closed", "first", "after_followup", "reminder_until", "nudge_after_seconds"}
)
_LEADFLOW_NEEDED = _LEADFLOW_TOP_KEYS - {"nudge_after_seconds"}
_STEPS_MIN = 3  # first < after_followup < reminder_until must all be in the file's steps list


def _one_line(text: str, longest: int) -> bool:
    return 1 <= len(text) <= longest and not has_control_char(text) and encodable(text)


@dataclass(frozen=True)
class Leadflow:
    """The lead owner list (rota), the lead stages and the reminder wait, from the file DESK_LEADFLOW_FILE.
    rota = (id, name, crm_owner_id) per person; steps = (id, label, crm_status) per step, the file's steps list
    followed by Closed as the LAST entry, so rank(step_id) is the index and Closed has the highest rank. Every shape
    rule is checked here, so a Leadflow built in a test is checked too. Messages name the entry, never the value.
    WARNING: the lead stage's crm_status is written to the CRM's lead status field; any automation in the
    CRM that watches that field may react to it and the desk cannot see it. The feature is off unless
    DESK_LEADFLOW_FILE is set."""

    rota: tuple[tuple[str, str, str], ...]
    steps: tuple[tuple[str, str, str], ...]
    first: str
    after_followup: str
    reminder_until: str
    nudge_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.rota) <= LEADFLOW_ROTA_MAX:
            raise SettingRefused(f"the lead owner list needs 1 to {LEADFLOW_ROTA_MAX} entries")
        ids: set[str] = set()
        names: set[str] = set()
        for n, entry in enumerate(self.rota, 1):
            ok = isinstance(entry, tuple) and len(entry) == 3 and all(isinstance(x, str) for x in entry)  # noqa: PLR2004
            if not ok or not _LEADFLOW_ID.fullmatch(entry[0]):
                raise SettingRefused(f"rota entry {n}: the id is not a short lower-case id")
            if not _one_line(entry[1], LEADFLOW_NAME_MAX):
                raise SettingRefused(
                    f"rota entry {n}: the name must be 1 to {LEADFLOW_NAME_MAX} characters on one line"
                )
            if entry[2] != "" and not _LEADFLOW_CRM_VALUE.fullmatch(entry[2]):
                raise SettingRefused(f"rota entry {n}: the crm_owner_id is not empty or a short plain id")
            if entry[0] in ids or entry[1] in names:
                raise SettingRefused(f"rota entry {n}: the id or the name is used twice")
            ids.add(entry[0])
            names.add(entry[1])
        file_steps = len(self.steps) - 1  # Closed is the last entry and is not counted
        if not _STEPS_MIN <= file_steps <= LEADFLOW_STEPS_MAX:
            raise SettingRefused(f"the steps list needs {_STEPS_MIN} to {LEADFLOW_STEPS_MAX} entries")
        seen: set[str] = set()
        for n, entry in enumerate(self.steps, 1):
            name = "closed" if n == len(self.steps) else f"steps entry {n}"
            ok = isinstance(entry, tuple) and len(entry) == 3 and all(isinstance(x, str) for x in entry)  # noqa: PLR2004
            if not ok or not _LEADFLOW_ID.fullmatch(entry[0]):
                raise SettingRefused(f"{name}: the id is not a short lower-case id")
            if not _one_line(entry[1], LEADFLOW_LABEL_MAX):
                raise SettingRefused(f"{name}: the label must be 1 to {LEADFLOW_LABEL_MAX} characters on one line")
            if not _LEADFLOW_CRM_VALUE.fullmatch(entry[2]):
                raise SettingRefused(f"{name}: the crm_status is not a short plain value")
            if entry[0] in seen:
                raise SettingRefused(f"{name}: the id is used twice")
            seen.add(entry[0])
        listed = [s[0] for s in self.steps[:-1]]
        for key in ("first", "after_followup", "reminder_until"):
            if getattr(self, key) not in listed:
                raise SettingRefused(f"{key} is not a step of the steps list")
        if not listed.index(self.first) < listed.index(self.after_followup):
            raise SettingRefused("after_followup must come after first")
        if not listed.index(self.after_followup) < listed.index(self.reminder_until):
            raise SettingRefused("reminder_until must come after after_followup")
        wait = self.nudge_after_seconds
        if wait is not None and not (type(wait) is int and 1 <= wait <= NUDGE_AFTER_MAX_SECONDS):
            raise SettingRefused(f"nudge_after_seconds must be a whole number from 1 to {NUDGE_AFTER_MAX_SECONDS}")

    def rank(self, step_id: str) -> int:
        """The index of a step in `steps` (Closed is last); ValueError for an unknown id."""
        return [s[0] for s in self.steps].index(step_id)


def _leadflow_entry(item: object, keys: tuple[str, ...], where: str) -> tuple[str, ...]:
    if not isinstance(item, dict) or set(item) != set(keys) or not all(isinstance(item[k], str) for k in keys):
        raise SettingRefused(f"{where}: each entry needs exactly the text keys " + ", ".join(keys))
    return tuple(item[k] for k in keys)


def read_leadflow(path: Path) -> Leadflow:
    """Read a leadflow file; every refusal names DESK_LEADFLOW_FILE (and the entry, never a value)."""
    try:
        return _read_leadflow(path)
    except SettingRefused as e:
        text = str(e)
        raise SettingRefused(text if text.startswith("DESK_LEADFLOW_FILE") else f"DESK_LEADFLOW_FILE: {text}") from None


def _read_leadflow(path: Path) -> Leadflow:
    """Read a leadflow file (the shape is in docs/settings.md). Raises SettingRefused naming DESK_LEADFLOW_FILE and the
    entry, never a value. `nudge_after_seconds` absent or null = reminders off (None)."""
    try:
        with path.open("rb") as f:
            raw = f.read(LEADFLOW_FILE_MAX_BYTES + 1)
    except OSError:
        raise SettingRefused("DESK_LEADFLOW_FILE cannot be read") from None
    if len(raw) > LEADFLOW_FILE_MAX_BYTES:
        raise SettingRefused("DESK_LEADFLOW_FILE is too big")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeError is a ValueError
        raise SettingRefused("DESK_LEADFLOW_FILE is not valid JSON text") from None
    if not isinstance(data, dict) or not _LEADFLOW_NEEDED <= set(data) <= _LEADFLOW_TOP_KEYS:
        raise SettingRefused(
            "DESK_LEADFLOW_FILE needs exactly the keys rota, steps, closed, first, after_followup, "
            "reminder_until and optionally nudge_after_seconds"
        )
    rota_in, steps_in = data["rota"], data["steps"]
    if not isinstance(rota_in, list) or not isinstance(steps_in, list):
        raise SettingRefused("DESK_LEADFLOW_FILE: rota and steps must be lists")
    rota = tuple(
        _leadflow_entry(x, ("id", "name", "crm_owner_id"), f"rota entry {n}") for n, x in enumerate(rota_in, 1)
    )
    steps = tuple(
        _leadflow_entry(x, ("id", "label", "crm_status"), f"steps entry {n}") for n, x in enumerate(steps_in, 1)
    )
    closed = _leadflow_entry(data["closed"], ("id", "label", "crm_status"), "closed")
    for key in ("first", "after_followup", "reminder_until"):
        if not isinstance(data[key], str):
            raise SettingRefused(f"DESK_LEADFLOW_FILE: {key} must be text")
    return Leadflow(
        rota=rota,  # type: ignore[arg-type]
        steps=(*steps, closed),  # type: ignore[arg-type]
        first=data["first"],
        after_followup=data["after_followup"],
        reminder_until=data["reminder_until"],
        nudge_after_seconds=data.get("nudge_after_seconds"),
    )


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("./data/desk.sqlite3")
    bind: str = "127.0.0.1"
    port: int = 8765  # 0 = the system picks a free port
    adapters: str = "demo"  # selects the demo adapters through loader.py
    fakes_host: str = "127.0.0.1"
    fakes_port: int = 8766
    replies_path: Path | None = None  # None = the loader supplies its own default
    send_timeout_seconds: float = 5.0  # per network operation; also used for the CRM call
    size_cap_bytes: int = 65536
    stall_seconds: int = 120
    allowed_hosts: tuple[str, ...] = ()  # extra Host names the portal gate accepts (for example a Docker host name)
    qualifier: str = "recorded"  # the AI step: "recorded" (replies list) or "live_claude" (needs `live`)
    outbox: str = "http"  # the sender: "http" (the fakes server) or "smtp" (module lead_desk.smtp_outbox)
    live: LiveSettings | None = None  # required when qualifier == "live_claude"
    crm: str = "fake"  # the CRM: "fake" (the fakes server) or "hubspot" (module lead_desk.crm_hubspot)
    later_mode: str = "off"  # "off" (a later message is only listed) or "approval" (a person approves or denies it)
    approvers_path: Path | None = None  # DESK_APPROVERS_FILE; None = the loader supplies its own default list
    approvers: tuple[tuple[str, str], ...] = ()  # (id, name) pairs; filled by the loader at start-up
    approvers_label: str = ""  # shown next to the approver choice, from the approvers file
    leadflow_path: Path | None = None  # DESK_LEADFLOW_FILE; None = lead owner, lead stage and reminder are off
    leadflow: Leadflow | None = None  # filled by server.main from the file
    nudge_to: str = field(default="", repr=False)  # DESK_NUDGE_TO: the one address every reminder goes to

    @property
    def fakes_url(self) -> str:
        """Built from host and port by lead_desk.urls (the stand-ins are loopback only; see Config.problem)."""
        return desk_origin(self.fakes_host, self.fakes_port)

    @property
    def real_services(self) -> tuple[str, ...]:
        """The kinds in use that reach a real service ("crm", "mail", "ai"); empty when every kind is a stand-in. The
        one place that decides it: the portal banner, the data page and the demo controls all read this. The "smtp"
        mail kind talks to a test mail server, so it is not listed."""
        return tuple(
            name
            for name, real in (
                ("crm", self.crm != CRM_STAND_IN),
                ("mail", self.outbox not in OUTBOX_STAND_INS),
                ("ai", self.qualifier != QUALIFIER_STAND_IN),
            )
            if real
        )

    def public_bind_problem(self) -> str | None:
        """The one rule about who may reach the desk: with any real service (see real_services) the desk starts only on
        a loopback address, because the desk has no sign-in and any program that can reach the port can write to that
        service (POST /leads, the review routes, the lead stage route). None = fine. server.main asks here."""
        if not self.real_services or is_loopback_host(self.bind):
            return None
        return (
            "DESK_BIND is not a loopback address while a real service is set ("
            + ", ".join(self.real_services)
            + "): anyone who can reach the port could write to it. Bind to 127.0.0.1"
        )

    def reminder_setting_problem(self) -> str | None:
        """The one rule about reminders, with its one wording: they are on exactly when the leadflow file has a wait
        AND an address is set. None = fine. Desk() and server.main() both ask here and refuse with this text."""
        if self.leadflow is None:
            return None
        reminders_on = self.leadflow.nudge_after_seconds is not None
        if reminders_on and not self.nudge_to:
            return "reminders are on in the leadflow file but DESK_NUDGE_TO is not set"
        if not reminders_on and self.nudge_to:
            return "DESK_NUDGE_TO is set but the leadflow file turns reminders off"
        return None

    def __post_init__(self) -> None:
        if not 0 <= self.port <= PORT_MAX:
            raise SettingRefused(f"DESK_PORT must be between 0 and {PORT_MAX}")
        if not 1 <= self.fakes_port <= PORT_MAX:
            raise SettingRefused(f"DESK_FAKES_PORT must be between 1 and {PORT_MAX}")
        if self.adapters not in ADAPTER_CHOICES:
            raise SettingRefused("DESK_ADAPTERS must be one of " + ", ".join(ADAPTER_CHOICES))
        if self.qualifier not in QUALIFIER_CHOICES:
            raise SettingRefused("DESK_QUALIFIER must be one of " + ", ".join(QUALIFIER_CHOICES))
        if self.outbox not in OUTBOX_CHOICES:
            raise SettingRefused("DESK_OUTBOX must be one of " + ", ".join(OUTBOX_CHOICES))
        if self.crm not in CRM_CHOICES:
            raise SettingRefused("DESK_CRM must be one of " + ", ".join(CRM_CHOICES))
        if self.later_mode not in LATER_MODES:
            raise SettingRefused("DESK_LATER_MODE must be one of " + ", ".join(LATER_MODES))
        if self.approvers_path is not None and self.later_mode != "approval":
            raise SettingRefused("DESK_APPROVERS_FILE is set but DESK_LATER_MODE is not approval")
        _check_approvers(self.approvers, self.approvers_label)
        if self.nudge_to and self.leadflow_path is None:
            raise SettingRefused("DESK_NUDGE_TO is set but DESK_LEADFLOW_FILE is not")
        if self.nudge_to and not (PLAIN_ADDRESS.fullmatch(self.nudge_to) and self.nudge_to.isascii()):
            raise SettingRefused("DESK_NUDGE_TO must be one plain address")
        if self.leadflow is not None and self.leadflow_path is None:
            raise SettingRefused("a leadflow is given but DESK_LEADFLOW_FILE is not set")
        if self.qualifier == "live_claude" and self.live is None:
            raise SettingRefused("qualifier live_claude needs the live model settings (DESK_LIVE_*)")
        if not self.bind or not self.fakes_host:
            raise SettingRefused("DESK_BIND and DESK_FAKES_HOST must not be empty")
        if not is_loopback_host(self.fakes_host):  # the stand-ins are local by construction: the banner relies on it
            raise SettingRefused(
                "DESK_FAKES_HOST (or DESK_FAKES_URL) must be this computer: localhost or a loopback address"
            )
        if not (math.isfinite(self.send_timeout_seconds) and 0 < self.send_timeout_seconds <= SEND_TIMEOUT_SECONDS_MAX):
            raise SettingRefused(f"DESK_SEND_TIMEOUT_SECONDS must be above 0 and at most {SEND_TIMEOUT_SECONDS_MAX}")
        if not 1 <= self.size_cap_bytes <= SIZE_CAP_BYTES_MAX:
            raise SettingRefused(f"DESK_SIZE_CAP_BYTES must be between 1 and {SIZE_CAP_BYTES_MAX}")
        if not 0 <= self.stall_seconds <= STALL_SECONDS_MAX:
            raise SettingRefused(f"DESK_STALL_SECONDS must be between 0 and {STALL_SECONDS_MAX}")
        for name in self.allowed_hosts:
            if (
                len(name) > HOST_NAME_MAX
                or not _HOST_NAME.fullmatch(name)
                or name.strip("[]") in WILDCARD_BINDS
                or is_unspecified(name)
            ):
                raise SettingRefused("DESK_ALLOWED_HOSTS holds a name that is not a lower-case host name")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Read DESK_DB_PATH, DESK_BIND, DESK_PORT, DESK_ADAPTERS, DESK_FAKES_URL (split into host and port),
        DESK_FAKES_HOST, DESK_FAKES_PORT, DESK_REPLIES_PATH, DESK_SEND_TIMEOUT_SECONDS, DESK_SIZE_CAP_BYTES,
        DESK_STALL_SECONDS, DESK_ALLOWED_HOSTS (comma list of extra Host names), DESK_QUALIFIER, DESK_OUTBOX and, when
        DESK_QUALIFIER is live_claude, the DESK_LIVE_* names (see _live_from_env). Missing names keep the defaults
        above. Bad values raise ValueError.
        Precedence: DESK_FAKES_URL wins over DESK_FAKES_HOST / DESK_FAKES_PORT when both are set. The URL scheme
        must be http (anything else raises ValueError). A non-empty URL path other than "/" raises ValueError (it
        would be silently dropped otherwise). A query, a fragment and userinfo (`user:pw@`) are ignored: only the
        host and the port are taken. An IPv6 host keeps its brackets in fakes_host."""
        e = os.environ if env is None else env
        kw: dict[str, Any] = {}
        if "DESK_DB_PATH" in e:
            kw["db_path"] = Path(e["DESK_DB_PATH"])
        if "DESK_BIND" in e:
            kw["bind"] = e["DESK_BIND"]
        if "DESK_PORT" in e:
            kw["port"] = env_number(e, "DESK_PORT", int, low=0, high=PORT_MAX)
        if "DESK_ADAPTERS" in e:
            kw["adapters"] = e["DESK_ADAPTERS"]
        if "DESK_FAKES_HOST" in e:
            kw["fakes_host"] = e["DESK_FAKES_HOST"]
        if "DESK_FAKES_PORT" in e:
            kw["fakes_port"] = env_number(e, "DESK_FAKES_PORT", int, low=1, high=PORT_MAX)
        if "DESK_FAKES_URL" in e:  # applied after HOST/PORT below, so it takes precedence
            parts = split_url("DESK_FAKES_URL", e["DESK_FAKES_URL"])
            if parts.scheme != "http":
                raise SettingRefused("DESK_FAKES_URL must use http")
            if parts.path not in ("", "/"):
                raise SettingRefused("DESK_FAKES_URL must not have a path")
            try:
                url_port = parts.port
            except ValueError:
                raise SettingRefused("DESK_FAKES_URL has a bad port") from None
            if parts.hostname is None or url_port is None:
                raise SettingRefused("DESK_FAKES_URL needs a host and a port")
            kw["fakes_host"] = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            kw["fakes_port"] = url_port
        if "DESK_REPLIES_PATH" in e:
            kw["replies_path"] = Path(e["DESK_REPLIES_PATH"])
        if "DESK_SEND_TIMEOUT_SECONDS" in e:
            kw["send_timeout_seconds"] = env_number(
                e, "DESK_SEND_TIMEOUT_SECONDS", float, high=SEND_TIMEOUT_SECONDS_MAX
            )
        if "DESK_SIZE_CAP_BYTES" in e:
            kw["size_cap_bytes"] = env_number(e, "DESK_SIZE_CAP_BYTES", int, low=1, high=SIZE_CAP_BYTES_MAX)
        if "DESK_STALL_SECONDS" in e:
            kw["stall_seconds"] = env_number(e, "DESK_STALL_SECONDS", int, low=0, high=STALL_SECONDS_MAX)
        if "DESK_ALLOWED_HOSTS" in e:
            kw["allowed_hosts"] = (
                tuple(n.strip() for n in e["DESK_ALLOWED_HOSTS"].split(",")) if e["DESK_ALLOWED_HOSTS"].strip() else ()
            )
        if "DESK_QUALIFIER" in e:
            kw["qualifier"] = e["DESK_QUALIFIER"]
        if "DESK_OUTBOX" in e:
            kw["outbox"] = e["DESK_OUTBOX"]
        if "DESK_CRM" in e:
            kw["crm"] = e["DESK_CRM"]
        if "DESK_LATER_MODE" in e:
            kw["later_mode"] = e["DESK_LATER_MODE"]
        if "DESK_APPROVERS_FILE" in e:
            kw["approvers_path"] = Path(e["DESK_APPROVERS_FILE"])
        flow = e.get("DESK_LEADFLOW_FILE", "").strip()
        if flow and flow != LEADFLOW_OFF:  # unset, empty and the word off all mean: the feature is off
            kw["leadflow_path"] = Path(flow)
        if e.get("DESK_NUDGE_TO", ""):
            kw["nudge_to"] = e["DESK_NUDGE_TO"]
        if kw.get("qualifier") == "live_claude":
            kw["live"] = _live_from_env(e)
        return cls(**kw)
