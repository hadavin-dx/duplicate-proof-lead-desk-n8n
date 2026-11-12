"""The importable contract: protocols and exceptions."""

from __future__ import annotations

import errno
from typing import Literal, Mapping, Protocol


class Crm(Protocol):
    def put(self, lead_id: str, record: Mapping[str, str]) -> Literal["PUT", "ALREADY_PRESENT"]:
        """Idempotent by lead_id."""
        ...

    def set_fields(self, lead_id: str, email_key: str, crm_owner_id: str, crm_status: str) -> None:
        """Copy the lead owner (`crm_owner_id`, "" = leave the CRM's owner alone) and the lead stage's CRM status
        to the lead's contact. Asking again with the same values is harmless. Raises CrmNothingStored (provably
        nothing changed) or CrmError (unclear)."""
        ...


class Outbox(Protocol):
    @property
    def capability(self) -> Literal["keyed", "blind"]: ...

    def send(
        self, send_key: str, to: str, subject: str, body: str, timeout: float
    ) -> Literal["SENT", "ALREADY_SENT"]: ...


class Qualifier(Protocol):
    def qualify(self, lead_id: str, email_key: str) -> Literal["hot", "warm", "cold"]: ...


class SendRefused(Exception): ...


class SendUnreachable(Exception): ...


class SendAmbiguous(Exception): ...


class CallTimeout(Exception):
    """A call to an adapter (the AI step, the CRM, the sender, a capability read) did not answer within the desk's
    TOTAL deadline (Desk._bounded). The desk stopped waiting; the call keeps running in an abandoned worker thread and
    its late answer is thrown away. The call may or may not have had an effect on the other side."""


class ModelTimeout(CallTimeout):
    """The AI step did not answer within MODEL_CALL_BUDGET_SECONDS. The desk stopped waiting (the call keeps running
    in an abandoned worker thread, its late answer is thrown away). Recorded as `internal:ModelTimeout`; the try is
    used up."""


class CrmTimeout(CallTimeout):
    """The CRM put did not answer within the desk's total deadline. The row may or may not be in the CRM: a keyed CRM is
    simply asked again with the same key; any other CRM ends `unknown` / `crm_unconfirmed` (a person looks). Recorded
    as `internal:CrmTimeout` on a keyed CRM; the try is used up."""


class RefreshTimeout(CallTimeout):
    """A capability or mode read (GET /fake/info) did not answer within the desk's total deadline: the mode counts as
    unreadable, the same as a failed read."""


class ModelAnswerLost(Exception):
    """The AI step answered, but the database stayed locked while the answer was being recorded, so the answer was
    thrown away. Recorded as `internal:ModelAnswerLost`; the try is used up (the model was really asked)."""


class AdapterBusy(Exception):
    """Too many workers of one kind (`kind`: model, crm, send or refresh) are still running, abandoned ones included
    (the caps are in constants.ADAPTER_WORKER_CAPS), or no worker thread could be started: the desk starts no new call.
    Nothing was asked and nothing was sent: the try is given back and the row waits (`<kind>_busy`)."""

    nothing_stored = True  # a CRM put that was never started stored nothing (the intent mark is reset)

    def __init__(self, kind: str, text: str = "") -> None:
        super().__init__(text or f"too many {kind} calls are still running")
        self.kind = kind


class ModelBusy(AdapterBusy):
    """Too many AI calls are still running (MODEL_HUNG_MAX): the desk starts no new one. Nothing was asked: the try is
    given back and the row waits (`model_busy`)."""

    def __init__(self, text: str = "") -> None:
        super().__init__("model", text)


class CrmUnreachable(Exception):
    """The CRM's mode could not be read (it is down) while an earlier try may already have put the row there.
    Nothing was asked in this try. Recorded as `internal:CrmUnreachable`; the try is used up."""


class OutboxModeMismatch(SendRefused): ...


class SimulatedCrash(BaseException): ...  # BaseException on purpose: no `except Exception` may swallow it


class SendNoDescriptors(SendUnreachable):
    """The desk ran out of file handles before the request could be made: nothing was sent, and it says nothing
    about the sender. Not a failed automatic try; the follow-up waits and is tried again."""


_FD_ERRNOS = (errno.EMFILE, errno.ENFILE)
_CHAIN_DEPTH = 4


def is_fd_exhaustion(exc: BaseException | None) -> bool:
    """True when `exc`, its URLError reason or its cause is the operating system refusing a new file handle."""
    for _ in range(_CHAIN_DEPTH):  # bounded walk: exception chains are short
        if exc is None:
            return False
        if isinstance(exc, SendNoDescriptors) or (isinstance(exc, OSError) and exc.errno in _FD_ERRNOS):
            return True
        reason = getattr(exc, "reason", None)
        exc = reason if isinstance(reason, BaseException) else exc.__cause__
    return False
