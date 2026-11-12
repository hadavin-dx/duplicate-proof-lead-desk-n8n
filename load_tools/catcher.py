"""A tiny SMTP catcher for the load runs (standard library only; made-up data only).

    python load_tools/catcher.py --out mail.jsonl [--host H] [--port N] [--control FILE]

Each accepted message becomes ONE line in the --out file (JSON): ts (epoch seconds), send_key (header X-Send-Key),
message_id, to (recipients joined by a comma), subject (decoded), body_sha256 (hex SHA-256 of the decoded text body
as UTF-8 with LF line ends, minus the one line break the sender adds at its end) and mode (the mode in force when it was
recorded). A message that cannot be parsed still leaves a line {"unparseable": true, ...} (the checker fails on it).
It prints `LISTENING <host> <port>` once the socket is open (use --port 0 for a free port).

Run-time mode: the CONTROL file (default `<out>.mode`) holds one word, read afresh on every command, so changing it
needs no restart. A missing or empty file means `ok`.
  ok               answer 250 as a normal server does.
  slow N           record the message, wait N seconds, then send the final 250 (N may be a decimal number).
  drop-after-data  record the message, then close the connection WITHOUT the final reply (the sender cannot tell
                   whether the mail arrived: the "maybe sent" case). The record carries mode "drop-after-data".
  reject           answer 550 to RCPT TO; nothing is recorded.
  down             close every new connection at once, without a greeting (to simulate a mail server that is
                   up but refusing; to simulate a closed port, stop the catcher).
An unknown word is treated as `ok` and noted on stderr once per change. Handles hundreds of connections at once
(asyncio, listen backlog 1024).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path

BACKLOG = 1024
LINE_LIMIT = 1 << 20  # one SMTP line (and one message) may be at most 1 MiB
IDLE_SECONDS = 30.0  # a client silent this long is dropped
KNOWN_MODES = ("ok", "slow", "drop-after-data", "reject", "down")
SMTP_OK, SMTP_READY, SMTP_BYE, SMTP_NO, SMTP_BAD_ORDER, SMTP_UNKNOWN = 250, 354, 221, 550, 503, 502


def read_mode(control: Path) -> tuple[str, float]:
    """(mode, seconds) from the control file; anything unreadable or unknown is ("ok", 0.0)."""
    try:
        words = control.read_text(encoding="utf-8").split()
    except OSError:
        return "ok", 0.0
    if not words or words[0] not in KNOWN_MODES:
        return "ok", 0.0
    seconds = 0.0
    if words[0] == "slow" and len(words) > 1:
        try:
            seconds = max(0.0, float(words[1]))
        except ValueError:
            seconds = 0.0
    return words[0], seconds


def record_of(raw: bytes, recipients: list[str], mode: str) -> dict[str, object]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part is not None else ""
    text = text.replace("\r\n", "\n")  # the sender's line ends travel as CRLF; the hash is over plain LF text
    if text.endswith("\n"):
        text = text[:-1]
    return {
        "ts": round(time.time(), 3),  # wall-clock: calendar stamp
        "send_key": str(msg.get("X-Send-Key", "")),
        "message_id": str(msg.get("Message-ID", "")),
        "to": ",".join(recipients),
        "subject": str(msg.get("Subject", "")),
        "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "mode": mode,
    }


class Catcher:
    def __init__(self, out: Path, control: Path) -> None:
        self.out, self.control = out, control
        self.unparseable = 0  # messages accepted but not readable; each also leaves an `unparseable` line in --out

    def _write(self, rec: dict[str, object]) -> None:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.out.open("a", encoding="utf-8") as f:  # one append of one whole line (single-threaded loop)
            f.write(line)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._session(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _say(writer: asyncio.StreamWriter, line: str) -> None:
        writer.write(line.encode("ascii") + b"\r\n")
        await writer.drain()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if read_mode(self.control)[0] == "down":
            return
        await self._say(writer, "220 catcher ready")
        recipients: list[str] = []
        have_mail = False
        while True:
            raw = await asyncio.wait_for(reader.readline(), IDLE_SECONDS)
            if not raw:
                return
            line = raw.decode("ascii", errors="replace").strip()
            verb = line.split(" ", 1)[0].upper()
            mode, seconds = read_mode(self.control)
            if verb in ("EHLO", "HELO"):
                await self._say(writer, "250 catcher")
            elif verb == "MAIL":
                have_mail, recipients = True, []
                await self._say(writer, "250 ok")
            elif verb == "RCPT":
                if mode == "reject":
                    await self._say(writer, f"{SMTP_NO} mailbox refused")
                else:
                    recipients.append(line.partition(":")[2].strip().strip("<>"))
                    await self._say(writer, "250 ok")
            elif verb == "DATA":
                if not have_mail or not recipients:
                    await self._say(writer, f"{SMTP_BAD_ORDER} need MAIL and RCPT first")
                    continue
                await self._say(writer, "354 end with a line holding one dot")
                data = await asyncio.wait_for(reader.readuntil(b"\r\n.\r\n"), IDLE_SECONDS)
                body = data[: -len(b"\r\n.\r\n")].replace(b"\r\n..", b"\r\n.")
                if body.startswith(b".."):
                    body = body[1:]
                try:
                    rec = record_of(body, recipients, mode)
                except Exception as e:  # noqa: BLE001 - any parse failure must still leave a visible line
                    self.unparseable += 1
                    print(f"UNPARSEABLE message #{self.unparseable}: {type(e).__name__}", file=sys.stderr, flush=True)
                    rec = {
                        "ts": round(time.time(), 3),  # wall-clock: calendar stamp
                        "unparseable": True,
                        "send_key": "",
                        "to": ",".join(recipients),
                        "mode": mode,
                    }
                self._write(rec)
                if mode == "drop-after-data":
                    return  # closed without the final reply
                if mode == "slow":
                    await asyncio.sleep(seconds)
                await self._say(writer, "250 queued")
                have_mail, recipients = False, []
            elif verb == "RSET":
                have_mail, recipients = False, []
                await self._say(writer, "250 ok")
            elif verb == "NOOP":
                await self._say(writer, "250 ok")
            elif verb == "QUIT":
                await self._say(writer, f"{SMTP_BYE} bye")
                return
            else:
                await self._say(writer, f"{SMTP_UNKNOWN} not supported")


async def serve(host: str, port: int, out: Path, control: Path) -> None:
    catcher = Catcher(out, control)
    server = await asyncio.start_server(catcher.handle, host, port, backlog=BACKLOG, limit=LINE_LIMIT)
    bound = server.sockets[0].getsockname()[1]
    print(f"LISTENING {host} {bound}", flush=True)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="JSONL file; one line per accepted message")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2525, help="0 = pick a free port (printed on stdout)")
    ap.add_argument("--control", type=Path, default=None, help="mode file (default: <out>.mode)")
    args = ap.parse_args(argv)
    control = args.control or args.out.with_name(args.out.name + ".mode")
    args.out.touch()
    try:
        asyncio.run(serve(args.host, args.port, args.out, control))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
