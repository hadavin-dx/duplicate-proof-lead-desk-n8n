"""A tiny scripted IMAP server over TLS for tests (LOGIN, SELECT, SEARCH, FETCH, LOGOUT). Loopback only. Demo-only.
`folders` maps a folder name to a list of X-Send-Key values (one message each). SEARCH HEADER matches by substring, like
Gmail; with `header_search_blind=True` it finds nothing (a server that splits a header value into words), and only
SEARCH ALL then lists the messages."""

from __future__ import annotations

import socket
import ssl
import threading
import time
from pathlib import Path

from demo_only.fake_smtp_tls import FAKE_PASSWORD, close_listener


class FakeImap:
    def __init__(
        self,
        cert: Path,
        key: Path,
        host: str,
        folders: dict[str, list[str]],
        port: int = 0,
        header_search_blind: bool = False,
    ) -> None:
        self.folders = folders
        self.header_search_blind = header_search_blind
        self.delay = 0.0  # seconds waited before every SEARCH answer (a slow server)
        self.greeting_delay = 0.0  # seconds waited before the server greeting (a slow connect)
        self.blind_keys: set[str] = set()  # the search never finds messages with these keys (the scan still sees them)
        self.user, self.password = "reader-user", FAKE_PASSWORD
        self.logins: list[tuple[str, str]] = []
        self.commands: list[str] = []
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        self._ctx = ctx
        self._srv = socket.socket()
        self._srv.bind((host, port))
        self._srv.listen(4)
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
            conn = self._ctx.wrap_socket(raw, server_side=True)
            f = conn.makefile("rwb", buffering=0)
            time.sleep(self.greeting_delay)
            f.write(b"* OK ready\r\n")
            folder = ""
            while True:
                line = f.readline().decode().rstrip("\r\n")
                if not line:
                    return
                tag, _, rest = line.partition(" ")
                word = rest.split(" ", 1)[0].upper()
                self.commands.append(word)
                if word == "CAPABILITY":
                    f.write(f"* CAPABILITY IMAP4rev1\r\n{tag} OK done\r\n".encode())
                elif word == "LOGIN":
                    _, u, p = rest.split(" ", 2)
                    self.logins.append((u.strip('"'), p.strip('"')))
                    ok = (u.strip('"'), p.strip('"')) == (self.user, self.password)
                    f.write(f"{tag} {'OK' if ok else 'NO'} login\r\n".encode())
                elif word in ("SELECT", "EXAMINE"):
                    folder = rest.split(" ", 1)[1].strip('"')
                    if folder in self.folders:
                        f.write(f"* {len(self.folders[folder])} EXISTS\r\n{tag} OK selected\r\n".encode())
                    else:
                        f.write(f"{tag} NO no such folder\r\n".encode())
                elif word == "SEARCH":
                    time.sleep(self.delay)
                    want = rest.rsplit(" ", 1)[1].strip('"')
                    if rest.strip().upper().endswith("ALL"):
                        ids = [str(i + 1) for i in range(len(self.folders[folder]))]
                    elif self.header_search_blind:
                        ids = []
                    else:
                        ids = [
                            str(i + 1)
                            for i, k in enumerate(self.folders[folder])
                            if want in k and k not in self.blind_keys
                        ]
                    f.write(f"* SEARCH {' '.join(ids)}\r\n{tag} OK done\r\n".encode())
                elif word == "FETCH":
                    n = int(rest.split(" ")[1])
                    hdr = f"X-Send-Key: {self.folders[folder][n - 1]}\r\n\r\n".encode()
                    f.write(
                        f"* {n} FETCH (BODY[HEADER.FIELDS (X-SEND-KEY)] {{{len(hdr)}}}\r\n".encode() + hdr + b")\r\n"
                    )
                    f.write(f"{tag} OK done\r\n".encode())
                elif word == "LOGOUT":
                    f.write(f"* BYE\r\n{tag} OK bye\r\n".encode())
                    return
                else:
                    f.write(f"{tag} BAD\r\n".encode())
        except (OSError, ValueError, IndexError, KeyError):
            return
        finally:
            raw.close()
