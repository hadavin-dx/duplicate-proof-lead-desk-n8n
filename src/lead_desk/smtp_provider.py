"""A BLIND outbox over a real mail provider (for example a mail provider's SMTP server), standard library only.

Differences from smtp_outbox.SmtpOutbox (loopback catcher, IP address only, no login):
  * the host is a NAME from config (certificate checked against that name, verification always ON);
  * STARTTLS (port 587) or TLS from the first byte (port 465), then login with a user name from config and a password
    read from a FILE at every send (never an argument, never printed, never in an error text);
  * the mail is NEVER sent to the lead's own address. Every message goes to one of your own test inboxes taken from
    `recipients` in config, chosen by a stable hash of the send key (one message per lead). The `to` the desk offers is
    only checked for header injection and then ignored. The made-up lead addresses can therefore never receive mail;
  * subject and body say they are demo mail; the lead id is in the header X-Send-Key (so the inbox can be counted by
    imap_count.py). The Message-ID and the header X-Send-Attempt are a NEW random token for every attempt (a provider
    may drop a second message with a repeated Message-ID, which would hide a double send); X-Send-Key is the count key.
Error table (same words as smtp_outbox): nothing left the desk -> SendUnreachable (connect, TLS, greeting, EHLO,
STARTTLS, login, MAIL, RCPT, the DATA command all failed or went silent); server said no before the text -> SendRefused
(a 4xx/5xx reply to any of those steps, a bad login, header-injection characters, empty key); once the text started
to go out, ANY doubt -> SendAmbiguous (timeout, hang-up, unreadable reply, any non-2xx final reply). The whole send is
ONE wall-clock budget (`timeout`). ONE try per call: no retry in here (the desk decides).
`sent` counts the messages the provider accepted (final 2xx) and `attempts` the sends that reached the network step.

Settings (env names): DESK_SMTPP_HOST, DESK_SMTPP_PORT, DESK_SMTPP_SECURITY (starttls or ssl), DESK_SMTPP_USER,
DESK_SMTPP_PASSWORD_FILE, DESK_SMTPP_FROM, DESK_SMTPP_RECIPIENTS (comma list), DESK_SMTPP_MESSAGE_ID_DOMAIN,
DESK_SMTPP_TIMEOUT_SECONDS, DESK_SMTPP_MAX_SENDS (hard cap of sends in one run, required)."""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import secrets
import socket
import ssl
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from email.errors import MessageError
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any, Final, Literal

from lead_desk.config import Config
from lead_desk.constants import CALL_DEADLINE_MAX_SECONDS, CAP_COUNT_MAX, LIVE_KEY_MIN_CHARS, PORT_MAX
from lead_desk.contract import (
    Outbox,
    SendAmbiguous,
    SendNoDescriptors,
    SendRefused,
    SendUnreachable,
    is_fd_exhaustion,
)
from lead_desk.envnum import SettingRefused, env_number
from lead_desk.run_stats import write_stats
from lead_desk.smtp_outbox import (
    _ADDRESS,
    _CRLF,
    _HOST_NAME,
    _PERMANENT_NEGATIVE,
    _POSITIVE_DONE,
    _WIRE_POLICY,
    _breaks_a_header,
    _Budget,
    _command,
    _dot_stuff,
    _read_reply,
)

_AUTH_OK: Final = 235
_STARTTLS_READY: Final = 220
_SEND_KEY = r"[A-Za-z0-9._:-]{1,64}"
SECRET_FILE_MAX_BYTES: Final = 4096
DEMO_SUBJECT_PREFIX: Final = "[Hadavin demo] "
DEMO_BODY_NOTE: Final = "This is a demo message about a made-up lead. Nobody real is being contacted.\n\n"


@dataclass(frozen=True)
class ProviderSettings:
    host: str
    port: int
    user: str = field(repr=False)  # addresses and paths are kept out of every printout
    password_file: Path = field(repr=False)
    mail_from: str = field(repr=False)
    recipients: tuple[str, ...] = field(repr=False)
    max_sends: int  # hard cap of sends in one run; no default
    security: Literal["starttls", "ssl"] = "starttls"
    message_id_domain: str = "example.invalid"
    timeout_seconds: float = Config().send_timeout_seconds
    stats_file: Path | None = None  # counters are written here after every change (run scripts read it)
    ca_file: Path | None = None  # extra trusted certificate authority file (verification stays ON)

    def __post_init__(self) -> None:
        if not _HOST_NAME.fullmatch(self.host):
            raise SettingRefused("DESK_SMTPP_HOST must be a lower-case host name")
        if not 1 <= self.port <= PORT_MAX:
            raise SettingRefused(f"DESK_SMTPP_PORT must be between 1 and {PORT_MAX}")
        if self.security not in ("starttls", "ssl"):
            raise SettingRefused("DESK_SMTPP_SECURITY must be starttls or ssl")
        if not self.user or _breaks_a_header(self.user) or not self.user.isascii():
            raise SettingRefused("DESK_SMTPP_USER must be plain ASCII text")
        for a in (self.mail_from, *self.recipients):
            if not _ADDRESS.fullmatch(a) or not a.isascii():
                raise SettingRefused("DESK_SMTPP_FROM and DESK_SMTPP_RECIPIENTS must be plain ASCII addresses")
        if not self.recipients:
            raise SettingRefused("DESK_SMTPP_RECIPIENTS needs at least one test recipient")
        if not _HOST_NAME.fullmatch(self.message_id_domain):
            raise SettingRefused("DESK_SMTPP_MESSAGE_ID_DOMAIN must be a lower-case host name")
        if type(self.max_sends) is not int or not 1 <= self.max_sends <= CAP_COUNT_MAX:
            raise SettingRefused(f"DESK_SMTPP_MAX_SENDS must be a whole number between 1 and {CAP_COUNT_MAX}")
        if not (math.isfinite(self.timeout_seconds) and 0 < self.timeout_seconds <= CALL_DEADLINE_MAX_SECONDS):
            raise SettingRefused("DESK_SMTPP_TIMEOUT_SECONDS must be above 0 and at most the desk's call deadline")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ProviderSettings:
        for n in (
            "DESK_SMTPP_HOST",
            "DESK_SMTPP_USER",
            "DESK_SMTPP_PASSWORD_FILE",
            "DESK_SMTPP_FROM",
            "DESK_SMTPP_RECIPIENTS",
            "DESK_SMTPP_MAX_SENDS",
        ):
            if not env.get(n, "").strip():
                raise SettingRefused(f"{n} is required for the smtp provider")
        kw: dict[str, Any] = {}
        if "DESK_SMTPP_SECURITY" in env:
            kw["security"] = env["DESK_SMTPP_SECURITY"]
        if "DESK_SMTPP_MESSAGE_ID_DOMAIN" in env:
            kw["message_id_domain"] = env["DESK_SMTPP_MESSAGE_ID_DOMAIN"]
        if "DESK_SMTPP_TIMEOUT_SECONDS" in env:
            kw["timeout_seconds"] = env_number(env, "DESK_SMTPP_TIMEOUT_SECONDS", float, high=CALL_DEADLINE_MAX_SECONDS)
        for name, setting in (("DESK_SMTPP_STATS_FILE", "stats_file"), ("DESK_SMTPP_CA_FILE", "ca_file")):
            if env.get(name):
                kw[setting] = Path(env[name])
        default_port = 465 if kw.get("security") == "ssl" else 587
        return cls(
            host=env["DESK_SMTPP_HOST"].strip().lower(),
            port=env_number(env, "DESK_SMTPP_PORT", int, low=1, high=PORT_MAX)
            if "DESK_SMTPP_PORT" in env
            else default_port,
            user=env["DESK_SMTPP_USER"],
            password_file=Path(env["DESK_SMTPP_PASSWORD_FILE"]),
            mail_from=env["DESK_SMTPP_FROM"],
            recipients=tuple(x.strip() for x in env["DESK_SMTPP_RECIPIENTS"].split(",") if x.strip()),
            max_sends=env_number(env, "DESK_SMTPP_MAX_SENDS", int, low=1, high=CAP_COUNT_MAX),
            **kw,
        )


def read_secret_file(path: Path, what: str = "smtp password") -> str:
    """The one line of a secret file (spaces inside an app password are dropped: providers show them in groups).
    At most SECRET_FILE_MAX_BYTES are read; a UTF-8 byte-order mark is accepted; a file of several lines or in another
    text format is refused with a fixed, helpful text. Raises SendRefused (never the path, never the content)."""
    try:
        with path.open("rb") as f:
            raw = f.read(SECRET_FILE_MAX_BYTES + 1)
    except OSError:
        raise SendRefused(f"the {what} file cannot be read") from None
    if len(raw) > SECRET_FILE_MAX_BYTES:
        raise SendRefused(f"the {what} file is too large: it must hold only the password")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise SendRefused(f"the {what} file is UTF-16 text: save it as plain UTF-8 text")
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeError:
        raise SendRefused(f"the {what} file is not UTF-8 text: save it as plain UTF-8 text") from None
    if "\n" in text or "\r" in text:
        raise SendRefused(f"the {what} file has more than one line: it must hold only the password")
    secret = "".join(text.split())
    if len(secret) < LIVE_KEY_MIN_CHARS or not secret.isascii() or not secret.isprintable():
        raise SendRefused(f"the {what} file holds no password")
    return secret


def _expect(code: int, step: str, intermediate: bool = False) -> None:
    if code // 100 != (3 if intermediate else _POSITIVE_DONE):
        raise SendRefused(f"{step} reply {code}")


class SmtpProviderOutbox:
    """capability "blind". One short connection per send; thread-safe."""

    def __init__(self, settings: ProviderSettings, ssl_context: ssl.SSLContext | None = None) -> None:
        """`ssl_context` is for a server with its own certificate; None = the system's trusted
        authorities, host name and chain checked."""
        self._s = settings
        self._ctx = ssl_context or (
            ssl.create_default_context(cafile=str(settings.ca_file)) if settings.ca_file else None
        )
        self._lock = threading.Lock()
        self.attempts = 0
        self.sent = 0

    @property
    def sender_address(self) -> str:
        """The configured From address (read by the portal's data map; never stored or logged)."""
        return self._s.mail_from

    @property
    def capability(self) -> Literal["keyed", "blind"]:
        return "blind"

    def recipient_for(self, send_key: str) -> str:
        """Your own test inbox for this lead: a stable hash of the key picks one of `recipients`."""
        n = int.from_bytes(hashlib.sha256(send_key.encode("utf-8")).digest()[:8], "big")
        return self._s.recipients[n % len(self._s.recipients)]

    def _build(self, send_key: str, subject: str, body: str) -> bytes:
        attempt = secrets.token_hex(12)  # new for every send: a repeated Message-ID could hide a second delivery
        msg = EmailMessage(policy=_WIRE_POLICY)
        msg["From"] = self._s.mail_from
        msg["To"] = self.recipient_for(send_key)
        msg["Subject"] = DEMO_SUBJECT_PREFIX + subject
        msg["Date"] = formatdate(localtime=False)  # wall-clock: calendar stamp
        msg["Message-ID"] = f"<{attempt}@{self._s.message_id_domain}>"
        msg["X-Send-Key"] = send_key  # the COUNT key: the same for every attempt of one lead
        msg["X-Send-Attempt"] = attempt
        msg.set_content(DEMO_BODY_NOTE + body, charset="utf-8")
        return msg.as_bytes()

    def send(self, send_key: str, to: str, subject: str, body: str, timeout: float) -> Literal["SENT", "ALREADY_SENT"]:
        if not math.isfinite(timeout):
            raise ValueError("send timeout must be a finite number")
        for value in (send_key, to, subject):
            if _breaks_a_header(value):
                raise SendRefused("line break or control character in a header value")
        if not re.fullmatch(_SEND_KEY, send_key):
            raise SendRefused("send key has characters outside the allowed set")
        if not _ADDRESS.fullmatch(to) or not to.isascii():
            raise SendRefused("recipient is not a plain address")
        password = read_secret_file(self._s.password_file)  # before any network use
        try:
            wire = _dot_stuff(self._build(send_key, subject, body))
        except (ValueError, UnicodeError, MessageError) as e:
            raise SendRefused(type(e).__name__) from e
        with self._lock:
            if self.attempts >= self._s.max_sends:
                raise SendRefused("the send cap is used up")  # nothing was sent
            self.attempts += 1
            write_stats(self._s.stats_file, {"attempts": self.attempts, "sent": self.sent})
        budget = _Budget(min(timeout, self._s.timeout_seconds) if timeout > 0 else self._s.timeout_seconds)
        sock = self._connect(budget)
        try:
            self._dialogue(sock, budget, password, self.recipient_for(send_key), wire)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            self.sent += 1
            write_stats(self._s.stats_file, {"attempts": self.attempts, "sent": self.sent})
        return "SENT"

    def _connect(self, budget: _Budget) -> socket.socket:
        try:
            sock = socket.create_connection((self._s.host, self._s.port), timeout=budget.left())
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 (refused, name lookup, timeout: nothing was sent, a retry is safe)
            if is_fd_exhaustion(e):
                raise SendNoDescriptors(type(e).__name__) from e
            raise SendUnreachable(type(e).__name__) from e
        return sock

    def _wrap(self, sock: socket.socket, budget: _Budget) -> ssl.SSLSocket:
        sock.settimeout(budget.left())
        return (self._ctx or ssl.create_default_context()).wrap_socket(
            sock, server_hostname=self._s.host
        )  # verifies name + chain

    def _dialogue(self, sock: socket.socket, budget: _Budget, password: str, rcpt: str, wire: bytes) -> None:
        s = self._s
        buf = bytearray()
        conn: socket.socket = sock
        try:  # Phase 1: nothing of the message text can have left
            if s.security == "ssl":
                conn = self._wrap(sock, budget)
            _expect(_read_reply(conn, budget, buf), "greeting")
            ehlo = _command(conn, budget, buf, f"EHLO {s.message_id_domain}")
            if ehlo // 100 == _PERMANENT_NEGATIVE:
                ehlo = _command(conn, budget, buf, f"HELO {s.message_id_domain}")
            _expect(ehlo, "ehlo")
            if s.security == "starttls":
                if _command(conn, budget, buf, "STARTTLS") != _STARTTLS_READY:
                    raise SendRefused("starttls refused")
                conn = self._wrap(sock, budget)
                buf.clear()
                _expect(_command(conn, budget, buf, f"EHLO {s.message_id_domain}"), "ehlo after tls")
            token = base64.b64encode(f"\0{s.user}\0{password}".encode()).decode("ascii")
            if _command(conn, budget, buf, "AUTH PLAIN " + token) != _AUTH_OK:
                raise SendRefused("login refused")
            _expect(_command(conn, budget, buf, f"MAIL FROM:<{s.mail_from}>"), "mail")
            _expect(_command(conn, budget, buf, f"RCPT TO:<{rcpt}>"), "rcpt")
            _expect(_command(conn, budget, buf, "DATA"), "data", intermediate=True)
        except (SendRefused, SendUnreachable, MemoryError):
            raise
        except Exception as e:  # noqa: BLE001 (known and unknown alike: nothing of the text left)
            raise SendUnreachable(type(e).__name__) from e
        try:  # Phase 2: the text starts to go out; from here every doubt is "maybe sent"
            conn.settimeout(budget.left())
            conn.sendall(wire)
            code = _read_reply(conn, budget, buf)
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SendAmbiguous(type(e).__name__) from e
        if code // 100 != _POSITIVE_DONE:
            raise SendAmbiguous(f"final reply {code}")
        try:
            conn.settimeout(min(budget.left(), 0.2))
            conn.sendall(b"QUIT" + _CRLF)
        except (OSError, TimeoutError):
            pass


def make_outbox(config: object) -> Outbox:
    """Entry point the loader calls when the sender is "smtp_provider": settings from the DESK_SMTPP_* environment.
    The adapter's time budget never exceeds the desk's own send timeout (config.send_timeout_seconds) or the call
    deadline."""
    s = ProviderSettings.from_env(os.environ)
    limit = min(float(getattr(config, "send_timeout_seconds", CALL_DEADLINE_MAX_SECONDS)), CALL_DEADLINE_MAX_SECONDS)
    return SmtpProviderOutbox(replace(s, timeout_seconds=limit) if s.timeout_seconds > limit else s)
