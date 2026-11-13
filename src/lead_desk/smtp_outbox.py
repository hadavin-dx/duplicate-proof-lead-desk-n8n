"""A test mail outbox over SMTP (standard library only), made to talk to a local test mail server (the catcher in
load_tools/catcher.py or any other test server you run). It is a blind outbox: it has no way to recognise a repeat, so a
repeat send sends again. Nothing here implies that real mail was sent anywhere.

Error table (what a person reads as "was the email sent?"):
  nothing left the desk  -> SendUnreachable: the connect failed (refused, connect timeout, unreachable), or the
                            server went silent / hung up before the message text was offered (greeting, EHLO,
                            MAIL, RCPT, the DATA command). A retry is safe.
  server said no         -> SendRefused: a 4xx or 5xx reply to the greeting, EHLO, MAIL FROM, RCPT TO or DATA
                            command, or a header-injection attempt caught before any network use. A retry is safe.
  maybe sent             -> SendAmbiguous: any failure once the message text started to go out, including a
                            timeout, a hang-up, a reply that cannot be read, or ANY non-2xx final reply (the
                            conservative choice: this desk's whole point is that a second email must not go out
                            by mistake, so every doubt after the text left is "maybe sent").
A `timeout` of zero or less means "use DESK_SMTP_TIMEOUT_SECONDS".
The whole send is bounded by ONE wall-clock budget (`timeout`); every socket read and write gets only what is left
of it, so a server that sends one byte at a time cannot stretch the call.

Settings come from SmtpSettings (env names below); nothing is hardcoded in the send path:
  DESK_SMTP_HOST, DESK_SMTP_PORT, DESK_SMTP_FROM, DESK_SMTP_MESSAGE_ID_DOMAIN, DESK_SMTP_TIMEOUT_SECONDS.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import socket
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any, Final, Literal

from lead_desk.config import Config, is_loopback_host
from lead_desk.constants import PORT_MAX, SEND_TIMEOUT_SECONDS_MAX
from lead_desk.contract import (
    Crm,
    Outbox,
    Qualifier,
    SendAmbiguous,
    SendNoDescriptors,
    SendRefused,
    SendUnreachable,
    is_fd_exhaustion,
)
from lead_desk.envnum import SettingRefused, env_number
from lead_desk.identity import PLAIN_ADDRESS

# SMTP reply classes (RFC 5321 section 4.2)
_POSITIVE_DONE: Final = 2
_POSITIVE_INTERMEDIATE: Final = 3
_REPLY_MAX_BYTES: Final = 65536  # a longer reply is treated as a broken server
_WIRE_POLICY: Final = policy.SMTP.clone(cte_type="7bit")  # text goes out as quoted-printable/base64, no 8BITMIME needed
_CRLF: Final = b"\r\n"
_BODY_END: Final = b"\r\n.\r\n"
_HOST_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_ADDRESS = PLAIN_ADDRESS  # no list, comment or quote characters (the pattern lives in identity.py)
_PERMANENT_NEGATIVE: Final = 5  # a 5xx reply class (RFC 5321 section 4.2.1)
_BARE_CODE_LEN: Final = 3  # a reply line may be the bare code (RFC 5321 section 4.2)

DEFAULT_SMTP_PORT: Final = 2525  # a non-privileged port; the real port comes from DESK_SMTP_PORT
DEFAULT_FROM: Final = "desk@example.test"
DEFAULT_MESSAGE_ID_DOMAIN: Final = "example.test"


@dataclass(frozen=True)
class SmtpSettings:
    host: str = Config().bind  # same loopback default the desk uses for its own bind
    port: int = DEFAULT_SMTP_PORT
    mail_from: str = DEFAULT_FROM
    message_id_domain: str = DEFAULT_MESSAGE_ID_DOMAIN
    timeout_seconds: float = Config().send_timeout_seconds  # default total budget of one send

    def __post_init__(self) -> None:
        try:  # an IP address only: a name would need a lookup, which has no time limit of its own
            ipaddress.ip_address(self.host)
        except ValueError:
            raise SettingRefused("DESK_SMTP_HOST must be an IP address (no name lookup is made)") from None
        # one rule (config.py), the same on every Python version; the banner relies on it
        if not is_loopback_host(self.host):
            raise SettingRefused(
                "DESK_SMTP_HOST must be a loopback address (the test mail server runs on this computer)"
            )
        if not 1 <= self.port <= PORT_MAX:
            raise SettingRefused(f"DESK_SMTP_PORT must be between 1 and {PORT_MAX}")
        if not _ADDRESS.fullmatch(self.mail_from) or not self.mail_from.isascii():
            raise SettingRefused("DESK_SMTP_FROM is not a plain ASCII address")
        if not _HOST_NAME.fullmatch(self.message_id_domain):
            raise SettingRefused("DESK_SMTP_MESSAGE_ID_DOMAIN must be a lower-case host name")
        if not (math.isfinite(self.timeout_seconds) and 0 < self.timeout_seconds <= SEND_TIMEOUT_SECONDS_MAX):
            raise SettingRefused(f"DESK_SMTP_TIMEOUT_SECONDS must be above 0 and at most {SEND_TIMEOUT_SECONDS_MAX}")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SmtpSettings:
        """Missing names keep the defaults above; a bad value raises ValueError."""
        kw: dict[str, Any] = {}
        if "DESK_SMTP_HOST" in env:
            kw["host"] = env["DESK_SMTP_HOST"]
        if "DESK_SMTP_PORT" in env:
            kw["port"] = env_number(env, "DESK_SMTP_PORT", int, low=1, high=PORT_MAX)
        if "DESK_SMTP_FROM" in env:
            kw["mail_from"] = env["DESK_SMTP_FROM"]
        if "DESK_SMTP_MESSAGE_ID_DOMAIN" in env:
            kw["message_id_domain"] = env["DESK_SMTP_MESSAGE_ID_DOMAIN"]
        if "DESK_SMTP_TIMEOUT_SECONDS" in env:
            kw["timeout_seconds"] = env_number(env, "DESK_SMTP_TIMEOUT_SECONDS", float, high=SEND_TIMEOUT_SECONDS_MAX)
        return cls(**kw)


def message_id_for(send_key: str, domain: str) -> str:
    """Stable Message-ID derived from the send key (a hash, so the key's characters never reach the header)."""
    return f"<{hashlib.sha256(send_key.encode('utf-8')).hexdigest()[:32]}@{domain}>"


class _Budget:
    """One wall-clock budget for a whole send. left() is what remains; it raises TimeoutError at zero."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def left(self) -> float:
        remaining = self._end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("send budget used up")
        return remaining


class _Closed(Exception):
    """The server closed the connection (or sent a broken reply) before a reply was complete."""


def _read_reply(sock: socket.socket, budget: _Budget, buf: bytearray) -> int:
    """Read one (possibly multi-line) reply; return its 3-digit code. Each recv gets only the budget left."""
    while True:
        end = buf.find(_CRLF)
        while end != -1:
            line = bytes(buf[:end])
            if len(line) == _BARE_CODE_LEN and line.isdigit():  # RFC 5321: a reply may be the bare code
                del buf[: end + 2]
                return int(line)
            if len(line) >= 4 and line[:3].isdigit() and line[3:4] in (b" ", b"-"):  # noqa: PLR2004
                if line[3:4] == b" ":
                    del buf[: end + 2]
                    return int(line[:3])
                del buf[: end + 2]
                end = buf.find(_CRLF)
                continue
            raise _Closed("unreadable reply")
        if len(buf) > _REPLY_MAX_BYTES:
            raise _Closed("reply too long")
        sock.settimeout(budget.left())
        chunk = sock.recv(4096)
        if not chunk:
            raise _Closed("connection closed")
        buf.extend(chunk)


def _command(sock: socket.socket, budget: _Budget, buf: bytearray, line: str) -> int:
    sock.settimeout(budget.left())
    sock.sendall(line.encode("ascii") + _CRLF)
    return _read_reply(sock, budget, buf)


def _dot_stuff(raw: bytes) -> bytes:
    """Message bytes (CRLF line ends) -> wire form: a leading '.' on a line is doubled; ends with CRLF.CRLF."""
    if not raw.endswith(_CRLF):
        raw += _CRLF
    if raw.startswith(b"."):
        raw = b"." + raw
    return raw.replace(b"\r\n.", b"\r\n..") + b".\r\n"


def _breaks_a_header(value: str) -> bool:
    """True for any character that could end a header line or start a new one: every control character (C0 and C1,
    DEL), the line and paragraph separators, and anything `str.splitlines` reads as a line end."""
    return len(value.splitlines()) > 1 or any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in value)


class SmtpOutbox:
    """capability "blind". Thread-safe: one short connection per send, no shared state."""

    def __init__(self, settings: SmtpSettings | None = None) -> None:
        self._s = settings or SmtpSettings()

    @property
    def sender_address(self) -> str:
        """The configured From address (read by the portal's data map; never stored or logged)."""
        return self._s.mail_from

    @property
    def capability(self) -> Literal["keyed", "blind"]:
        return "blind"

    def _build(self, send_key: str, to: str, subject: str, body: str) -> bytes:
        msg = EmailMessage(policy=_WIRE_POLICY)
        msg["From"] = self._s.mail_from
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False)  # wall-clock: calendar stamp
        msg["Message-ID"] = message_id_for(send_key, self._s.message_id_domain)
        msg["X-Send-Key"] = send_key
        msg.set_content(body, charset="utf-8")
        return msg.as_bytes()

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Literal["SENT", "ALREADY_SENT"]:
        if not math.isfinite(timeout):
            raise ValueError("send timeout must be a finite number")  # before any network use
        for value in (send_key, to, subject):
            if _breaks_a_header(value):
                raise SendRefused("line break or control character in a header value")  # before any network use
        if not _ADDRESS.fullmatch(to) or not to.isascii():
            raise SendRefused("recipient is not a plain address")
        if not send_key:
            raise SendRefused("empty send key")
        try:
            wire = _dot_stuff(self._build(send_key, to, subject, body))
        except (ValueError, MessageError) as e:  # ValueError covers UnicodeError; MessageError: refused
            raise SendRefused(type(e).__name__) from e
        budget = _Budget(timeout if timeout > 0 else self._s.timeout_seconds)
        sock = self._connect(budget)
        try:
            self._dialogue(sock, budget, to, wire)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return "SENT"

    def _connect(self, budget: _Budget) -> socket.socket:
        """Connect inside the send budget with no name lookup: the host is an IP address (SmtpSettings checks it)."""
        family = socket.AF_INET6 if ipaddress.ip_address(self._s.host).version == 6 else socket.AF_INET  # noqa: PLR2004
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 (nothing was connected yet: unreachable, a retry is safe)
            if is_fd_exhaustion(e):  # no file handle left: nothing was sent and it says nothing about the sender
                raise SendNoDescriptors(type(e).__name__) from e
            raise SendUnreachable(type(e).__name__) from e
        try:
            sock.settimeout(budget.left())
            sock.connect((self._s.host, self._s.port))
        except MemoryError:
            sock.close()
            raise
        except Exception as e:  # noqa: BLE001 (refused, connect timeout, unreachable network, anything odd)
            sock.close()
            if is_fd_exhaustion(e):
                raise SendNoDescriptors(type(e).__name__) from e
            raise SendUnreachable(type(e).__name__) from e
        return sock

    def _dialogue(self, sock: socket.socket, budget: _Budget, to: str, wire: bytes) -> None:
        buf = bytearray()
        # Phase 1: before the message text is offered. Nothing of the message can have left.
        try:
            code = _read_reply(sock, budget, buf)
            self._expect_ok(code, "greeting")
            ehlo = _command(sock, budget, buf, f"EHLO {self._s.message_id_domain}")
            if ehlo // 100 == _PERMANENT_NEGATIVE:  # an old server that does not know EHLO: say HELO once
                ehlo = _command(sock, budget, buf, f"HELO {self._s.message_id_domain}")
            self._expect_ok(ehlo, "ehlo")
            self._expect_ok(_command(sock, budget, buf, f"MAIL FROM:<{self._s.mail_from}>"), "mail")
            self._expect_ok(_command(sock, budget, buf, f"RCPT TO:<{to}>"), "rcpt")
            self._expect_ok(_command(sock, budget, buf, "DATA"), "data", intermediate=True)
        except (SendRefused, SendUnreachable, MemoryError):
            raise
        except Exception as e:  # noqa: BLE001 (known and unknown alike: nothing of the text left)
            raise SendUnreachable(type(e).__name__) from e
        # Phase 2: the text starts to go out. From here every doubt is "maybe sent".
        try:
            sock.settimeout(budget.left())
            sock.sendall(wire)
            code = _read_reply(sock, budget, buf)
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 (known and unknown alike: the text began to go out, maybe sent)
            raise SendAmbiguous(type(e).__name__) from e
        if code // 100 != _POSITIVE_DONE:
            raise SendAmbiguous(f"final reply {code}")
        try:  # polite goodbye; its failure changes nothing
            sock.settimeout(min(budget.left(), 0.2))
            sock.sendall(b"QUIT" + _CRLF)
        except OSError:  # TimeoutError is an OSError
            pass

    @staticmethod
    def _expect_ok(code: int, step: str, intermediate: bool = False) -> None:
        want = _POSITIVE_INTERMEDIATE if intermediate else _POSITIVE_DONE
        if code // 100 != want:
            raise SendRefused(f"{step} reply {code}")


def make_smtp_outbox(env: Mapping[str, str]) -> Outbox:
    """Factory for a launcher script: SmtpOutbox built from DESK_SMTP_* names."""
    return SmtpOutbox(SmtpSettings.from_env(env))


def make_outbox(config: Config) -> Outbox:
    """Entry point the loader calls when `config.outbox == "smtp"`: settings come from the DESK_SMTP_* environment."""
    return make_smtp_outbox(os.environ)


def load_smtp_adapters(config: Config, env: Mapping[str, str]) -> tuple[Crm, Outbox, Qualifier]:
    """The demo CRM and Qualifier (through loader.py) with the SMTP outbox in place of the HTTP one. `server.main`
    reaches the SMTP outbox through `DESK_OUTBOX=smtp` (loader.py calls `make_outbox`); a launcher script can call this
    function instead (DESK_ADAPTERS stays "demo", so the demo portal keeps loading)."""
    from lead_desk.loader import load_demo_adapters  # local import: keeps this module importable without the demo

    crm, _http_outbox, qualifier = load_demo_adapters(config)
    return crm, make_smtp_outbox(env), qualifier
