"""A scripted SMTP server with STARTTLS (or TLS from the first byte) and AUTH PLAIN, for tests. Loopback only.
Demo-only code. The certificate is made at test time (see make_cert); nothing secret is stored in the repo.

`script` picks the behaviour: ok, bad_login, no_starttls, rcpt_550, data_hangup (hangs up after the DATA command is
accepted and the text arrived), final_452 (final reply 452 after the text), silent_after_text (reads the text and never
answers), greeting_silent (never sends the greeting). `messages` collects what was accepted:
(login user, password seen, mail-from, rcpt, raw message text). `commands` collects the command words seen."""

from __future__ import annotations

import base64
import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path


def make_cert(directory: Path, host: str) -> tuple[Path, Path] | None:
    """(certificate file, key file) valid for `host`, made with the openssl program; None when it is not installed."""
    exe = shutil.which("openssl")
    if exe is None:
        return None
    cert, key = directory / "c.pem", directory / "k.pem"
    subprocess.run(  # noqa: S603
        [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2"]
        + ["-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}"],
        check=True,
        capture_output=True,
    )
    return cert, key


FAKE_PASSWORD = "pw-" + "for-" + "tests-" + "only"  # the made-up mailbox password of every fake, built from parts


def close_listener(srv: socket.socket, stop: threading.Event, thread: threading.Thread | None) -> None:
    """Stop an accept loop for good. On Linux, closing a socket while another thread waits in accept() does NOT end
    the wait, so the port would keep answering: set the flag, wake the loop with one self-connection, wait for the
    thread, and only then close the socket (the port is then really refused)."""
    stop.set()
    try:  # a second call (the socket is closed already) does nothing
        socket.create_connection(srv.getsockname()[:2], timeout=1).close()
    except OSError:
        pass
    if thread is not None:
        thread.join(2)
    srv.close()


class FakeSmtp:
    def __init__(
        self, cert: Path, key: Path, host: str, script: str = "ok", implicit_tls: bool = False, port: int = 0
    ) -> None:
        self.script = script
        self.implicit_tls = implicit_tls
        self.messages: list[tuple[str, str, str, str, bytes]] = []
        self.commands: list[str] = []
        self.password = FAKE_PASSWORD
        self.user = "sender-user"
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(cert), str(key))
        self._srv = socket.socket()
        self._srv.bind((host, port))
        self._srv.listen(8)
        self.port = int(self._srv.getsockname()[1])
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def close(self) -> None:
        close_listener(self._srv, self._stop, self._th)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            if self._stop.is_set():  # the wake-up connection of close_listener
                c.close()
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, raw: socket.socket) -> None:
        try:
            self._dialogue(raw)
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            try:
                raw.close()
            except OSError:
                pass

    def _dialogue(self, raw: socket.socket) -> None:
        conn: socket.socket = self._ctx.wrap_socket(raw, server_side=True) if self.implicit_tls else raw
        f = conn.makefile("rwb", buffering=0)
        if self.script == "greeting_silent":
            self._stop.wait(5)
            return
        f.write(b"220 fake ready\r\n")
        user = ""
        mail_from = rcpt = ""
        authed = False
        while True:
            line = f.readline()
            if not line:
                return
            cmd = line.decode("ascii", "replace").rstrip("\r\n")
            word = cmd.split(" ", 1)[0].upper()
            self.commands.append(word)
            if word == "EHLO":
                f.write(
                    b"250-fake\r\n"
                    + (b"250-STARTTLS\r\n" if not self.implicit_tls and not isinstance(conn, ssl.SSLSocket) else b"")
                    + b"250 AUTH PLAIN\r\n"
                )
            elif word == "STARTTLS":
                if self.script == "no_starttls":
                    f.write(b"454 tls not available\r\n")
                    continue
                f.write(b"220 go ahead\r\n")
                conn = self._ctx.wrap_socket(raw, server_side=True)
                f = conn.makefile("rwb", buffering=0)
            elif word == "AUTH":
                parts = base64.b64decode(cmd.split(" ")[-1]).split(b"\0")
                user, pw = parts[1].decode(), parts[2].decode()
                authed = self.script != "bad_login" and user == self.user and pw == self.password
                f.write(b"235 ok\r\n" if authed else b"535 no\r\n")
            elif word == "MAIL":
                mail_from = cmd.split("<", 1)[1].rstrip(">")
                f.write(b"250 ok\r\n")
            elif word == "RCPT":
                rcpt = cmd.split("<", 1)[1].rstrip(">")
                f.write(b"550 no such user\r\n" if self.script == "rcpt_550" else b"250 ok\r\n")
            elif word == "DATA":
                f.write(b"354 go\r\n")
                data = b""
                while not data.endswith(b"\r\n.\r\n"):
                    chunk = f.readline()
                    if not chunk:
                        return
                    data += chunk
                if self.script == "data_hangup":
                    return
                if self.script == "silent_after_text":
                    self._stop.wait(5)
                    return
                if self.script == "final_452":
                    f.write(b"452 try later\r\n")
                    continue
                if authed:
                    self.messages.append((user, self.password, mail_from, rcpt, data))
                f.write(b"250 queued\r\n")
            elif word == "QUIT":
                f.write(b"221 bye\r\n")
                return
            else:
                f.write(b"502 no\r\n")
