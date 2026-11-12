"""The ONLY file in src/ that may name demo_only."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from lead_desk.adapters import HttpCrm, HttpOutbox
from lead_desk.config import Config, read_approvers
from lead_desk.constants import HUBSPOT_MODULE, SMTP_MODULE, SMTP_PROVIDER_MODULE
from lead_desk.contract import Crm, Outbox, Qualifier
from lead_desk.envnum import SettingRefused
from lead_desk.live_claude import LiveQualifier

if TYPE_CHECKING:
    from lead_desk.desk import Desk

_DEMO_MODULES = ("demo_only", "demo_only.portal_demo")


class DemoPortal(Protocol):
    """What the portal needs from the demo extension (demo_only/portal_demo.py)."""

    assets: Mapping[str, tuple[bytes, str]]  # "portal_demo.js" -> (bytes, "text/javascript; charset=utf-8")

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, Any]: ...


def load_demo_portal(desk: Desk) -> DemoPortal | None:
    """None ONLY when the demo module is absent: ModuleNotFoundError whose .name is "demo_only" or
    "demo_only.portal_demo". Also None unless the desk runs with the demo adapters (config.adapters == "demo") and
    every kind is a stand-in: the burst and crash controls write leads, so they never load next to a real CRM, mail
    provider or live AI step. Any other import error propagates (a broken extension fails loud)."""
    if desk.config.adapters != "demo" or desk.config.real_services:
        return None
    try:
        mod = importlib.import_module("demo_only.portal_demo")
    except ModuleNotFoundError as e:
        if e.name in _DEMO_MODULES:
            return None
        raise
    return cast(DemoPortal, mod.make(desk))


def _kind_module(name: str, what: str) -> Any:
    """A kind module imported only when it is selected; a missing module is a clear error, never a silent fallback."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name != name:
            raise
        raise RuntimeError(what + " is set but the module " + name + " cannot be imported") from e


def load_demo_adapters(config: Config) -> tuple[Crm, Outbox, Qualifier]:
    """The CRM is always HttpCrm on config.fakes_url. The sender is HttpOutbox on config.fakes_url (outbox "http";
    raises FakesUnreachable when /fake/info cannot be read) or the SMTP sender (outbox "smtp"): the module
    lead_desk.smtp_outbox, imported only now, offers make_outbox(config); if the module cannot be imported the error
    is a clear RuntimeError, never a silent fallback. The AI step is a RecordedQualifier on config.replies_path
    (None = the replies.json next to the demo_only package; qualifier "recorded") or a LiveQualifier
    (qualifier "live_claude", settings in config.live)."""
    crm = (
        cast(Crm, _kind_module(HUBSPOT_MODULE, "crm hubspot").make_crm(config))
        if config.crm == "hubspot"
        else HttpCrm(config.fakes_url, config.send_timeout_seconds)
    )
    outbox: Outbox
    if config.outbox in ("smtp", "smtp_provider"):
        name = SMTP_PROVIDER_MODULE if config.outbox == "smtp_provider" else SMTP_MODULE
        outbox = cast(Outbox, _kind_module(name, "outbox " + config.outbox).make_outbox(config))
    else:
        outbox = HttpOutbox(config.fakes_url, config.send_timeout_seconds)
    qualifier: Qualifier
    if config.qualifier == "live_claude":
        if config.live is None:
            raise ValueError("qualifier live_claude needs the live model settings")
        qualifier = LiveQualifier(config.live)
    else:
        try:
            fakes = importlib.import_module("demo_only.fakes")  # dynamic: src is type-checked without the demo folder
        except ModuleNotFoundError as e:
            if e.name not in (*_DEMO_MODULES, "demo_only.fakes"):
                raise
            raise RuntimeError(
                "the desk is set to use the pre-written demo answers (the default), but the folder demo_only is "
                "missing. Put the demo_only folder back, or set DESK_QUALIFIER=live_claude."
            ) from e
        replies = config.replies_path or Path(str(fakes.__file__)).resolve().parent / "replies.json"
        qualifier = fakes.RecordedQualifier(replies)
    return crm, outbox, qualifier


def load_approvers(config: Config) -> tuple[tuple[tuple[str, str], ...], str]:
    """The approvers for approval mode: the file in config.approvers_path (DESK_APPROVERS_FILE) when set, else the
    approvers.json next to the demo_only package (the replies.json pattern). Returns ((id, name), ...) and the label.
    A missing demo_only folder is a SettingRefused (approval mode needs the file); any other import error propagates."""
    if config.approvers_path is not None:
        return read_approvers(config.approvers_path)
    try:
        fakes = importlib.import_module("demo_only.fakes")  # dynamic: src is type-checked without the demo folder
    except ModuleNotFoundError as e:
        if e.name not in (*_DEMO_MODULES, "demo_only.fakes"):
            raise
        raise SettingRefused("approval mode needs the approvers file (DESK_APPROVERS_FILE)") from e
    return read_approvers(Path(str(fakes.__file__)).resolve().parent / "approvers.json")
