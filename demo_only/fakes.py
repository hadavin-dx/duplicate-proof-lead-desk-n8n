"""In-process fakes. Signatures follow contract.md ("Demo files").
Demo-only code: reached from src/ only through lead_desk.loader."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from lead_desk.adapters import CrmNothingStored
from lead_desk.contract import SendAmbiguous, SendRefused, SendUnreachable

FailMode = Literal["none", "refuse", "unreachable", "drop", "record_then_drop"]
LABELS = ("hot", "warm", "cold")
DEFAULT_LABEL = "warm"


class FakeCrm:
    """In-process Crm, keyed by lead_id. rows maps lead_id -> record."""

    rows: dict[str, Mapping[str, str]]
    fields: dict[str, dict[str, str]]  # lead_id -> {"owner_id", "status"} as last copied by set_fields

    def __init__(self) -> None:
        self.rows = {}
        self.fields = {}
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return len(self.rows)

    def put(self, lead_id: str, record: Mapping[str, str]) -> Literal["PUT", "ALREADY_PRESENT"]:
        with self._lock:
            if lead_id in self.rows:
                return "ALREADY_PRESENT"
            self.rows[lead_id] = dict(record)
            return "PUT"

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        with self._lock:
            if lead_id not in self.rows:
                raise CrmNothingStored("no contact")  # same answer as the stand-in server's 404 and HubSpot's 404
            self.fields[lead_id] = {"owner_id": crm_owner_id, "status": crm_status}


class _FakeOutbox:
    messages: list[dict[str, str]]  # every post: key, to, subject (blind stores every post; keyed one per key)
    delay: float  # seconds waited inside send(); default 0.0
    _keyed = False

    def __init__(self) -> None:
        self.messages = []
        self.delay = 0.0
        self._fail: FailMode = "none"
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def capability(self) -> Literal["keyed", "blind"]:
        return "keyed" if self._keyed else "blind"

    def fail_next(self, mode: FailMode) -> None:
        """refuse -> SendRefused, nothing stored; unreachable -> SendUnreachable, nothing stored;
        drop -> SendAmbiguous, nothing stored; record_then_drop -> stores, then SendAmbiguous. Next send only."""
        if mode not in ("none", "refuse", "unreachable", "drop", "record_then_drop"):
            raise ValueError(mode)
        with self._lock:
            self._fail = mode

    def keys(self) -> list[str]:
        """Test helper: the key of every stored message, in order."""
        return [m["key"] for m in self.messages]

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Literal["SENT", "ALREADY_SENT"]:
        if self.delay:
            time.sleep(self.delay)  # waited before anything is stored, like the fakes server with fail_next none
        with self._lock:
            mode, self._fail = self._fail, "none"
            if mode == "refuse":
                raise SendRefused("fake outbox refused")
            if mode == "unreachable":
                raise SendUnreachable("fake outbox unreachable")
            if mode == "drop":
                raise SendAmbiguous("fake outbox dropped the request")
            already = self._keyed and send_key in self.keys()
            if not already:
                self.messages.append({"key": send_key, "to": to, "subject": subject})
            if mode == "record_then_drop":
                raise SendAmbiguous("fake outbox stored the message and dropped the reply")
            return "ALREADY_SENT" if already else "SENT"


class KeyedOutbox(_FakeOutbox):
    """In-process Outbox, capability "keyed"."""

    _keyed = True


class BlindOutbox(_FakeOutbox):
    """In-process Outbox, capability "blind"."""

    _keyed = False


class RecordedQualifier:
    """In-process and demo Qualifier. Reads replies.json (contract.md, section "Demo files"):
    email key -> "hot"|"warm"|"cold".
    The constructor raises (fail loud, never an empty dict) when the file is missing or is not valid JSON.
    A key with no entry gets "warm". An entry whose value is not one of the three labels raises a KeyError-class
    error that names the label (raised by qualify, when that key is asked for)."""

    mode = "recorded"  # read by the portal's /api/ui/data-map: replies come from a list, no live AI is called
    calls: int  # fake-only counter read by test_dedupe_before_ai

    def __init__(self, replies_path: Path) -> None:
        data = json.loads(Path(replies_path).read_text(encoding="utf-8"))  # FileNotFoundError / ValueError: loud
        if not isinstance(data, dict):
            raise ValueError("replies file must hold one JSON object")
        self._replies: dict[str, object] = data
        self.calls = 0
        self._lock = threading.Lock()

    def qualify(self, lead_id: str, email_key: str) -> Literal["hot", "warm", "cold"]:
        with self._lock:
            self.calls += 1
        label = self._replies.get(email_key, DEFAULT_LABEL)
        if label == "hot" or label == "warm" or label == "cold":
            return label
        raise KeyError(f"bad label {label!r} for {email_key}")
