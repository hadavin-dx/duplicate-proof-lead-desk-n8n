"""SmtpOutbox against the load_tools catcher and small scripted servers. Made-up data only; loopback only."""

from __future__ import annotations

import errno
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from support import HOST, REPO, free_port, wait_until

from lead_desk.config import Config
from lead_desk.contract import SendAmbiguous, SendNoDescriptors, SendRefused, SendUnreachable
from lead_desk.smtp_outbox import SmtpOutbox, SmtpSettings, load_smtp_adapters, make_smtp_outbox, message_id_for

TO = "ada@example.test"
SUBJECT, BODY = "Thanks for getting in touch, Ada", "Hi Ada, we received your message. (DEMO EMAIL)"
BUDGET = 3.0
SHORT = 0.6


class Catch:
    def __init__(self, port: int, out: Path, control: Path) -> None:
        self.port, self.out, self.control = port, out, control

    def mode(self, text: str) -> None:
        self.control.write_text(text, encoding="utf-8")

    def records(self) -> list[dict[str, str]]:
        return [json.loads(x) for x in self.out.read_text(encoding="utf-8").splitlines() if x.strip()]

    def outbox(self, port: int | None = None) -> SmtpOutbox:
        return SmtpOutbox(SmtpSettings(host=HOST, port=port or self.port))


@pytest.fixture
def catch(tmp_path: Path) -> Iterator[Catch]:
    out, control = tmp_path / "mail.jsonl", tmp_path / "mail.ctl"
    cmd = [
        sys.executable,
        str(REPO / "load_tools" / "catcher.py"),
        "--out",
        str(out),
        "--host",
        HOST,
        "--port",
        "0",
        "--control",
        str(control),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)  # noqa: S603
    try:
        assert proc.stdout is not None
        parts = proc.stdout.readline().split()
        assert parts[:2] == ["LISTENING", HOST]
        yield Catch(int(parts[2]), out, control)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        if proc.stdout:
            proc.stdout.close()


class Script:
    """A one-connection scripted SMTP server: `stage` says where it misbehaves."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.sock = socket.socket()
        self.sock.bind((HOST, 0))
        self.sock.listen(4)
        self.port = int(self.sock.getsockname()[1])
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _dribble(self, conn: socket.socket, text: bytes) -> None:
        for i in range(len(text)):
            conn.sendall(text[i : i + 1])
            time.sleep(0.2)

    def _run(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            try:
                self._talk(conn)
            except OSError:
                pass

    def _talk(self, conn: socket.socket) -> None:
        if self.stage == "greeting_dribble":
            self._dribble(conn, b"220 slow server says hello\r\n")
            return
        if self.stage == "greeting_554":
            conn.sendall(b"554 no service\r\n")
            return
        conn.sendall(b"220 ok\r\n")
        f = conn.makefile("rb")
        while True:
            line = f.readline()
            if not line:
                return
            verb = line.split(b" ", 1)[0].strip().upper()
            if self.stage == "bare_codes" and verb != b"DATA":
                conn.sendall(b"250\r\n")
                continue
            if self.stage in ("ehlo_500", "ehlo_helo_500") and verb == b"EHLO":
                conn.sendall(b"500 what is EHLO\r\n")
                continue
            if self.stage == "ehlo_helo_500" and verb == b"HELO":
                conn.sendall(b"501 and HELO neither\r\n")
                continue
            if self.stage == "data_554" and verb == b"DATA":
                conn.sendall(b"554 no\r\n")
                return
            if verb == b"DATA":
                conn.sendall(b"354 go\r\n")
                while f.readline() != b".\r\n":
                    pass
                if self.stage == "final_dribble":
                    self._dribble(conn, b"250 queued for the long road home\r\n")
                elif self.stage == "final_550":
                    conn.sendall(b"550 rejected after the text\r\n")
                elif self.stage == "final_garbage":
                    conn.sendall(b"what is this\r\n")
                elif self.stage == "bare_codes":
                    conn.sendall(b"250\r\n")
                elif self.stage in ("ehlo_500", "ehlo_helo_500"):
                    conn.sendall(b"250 queued\r\n")
                return
            if self.stage == "data_450" and verb == b"MAIL":
                conn.sendall(b"450 try later\r\n")
            elif self.stage == "silent_rcpt" and verb == b"RCPT":
                time.sleep(5)
            else:
                conn.sendall(b"250 ok\r\n")

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def script() -> Iterator[Callable[[str], Script]]:
    made: list[Script] = []

    def make(stage: str) -> Script:
        s = Script(stage)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def box(port: int) -> SmtpOutbox:
    return SmtpOutbox(SmtpSettings(host=HOST, port=port))


# -- mapping
def test_capability_is_blind() -> None:
    assert SmtpOutbox().capability == "blind"


def test_sent_headers_and_utf8(catch: Catch) -> None:
    subject, body = "Grüße, Ada ☃", "Zażółć gęślą jaźń\n.leading dot line\nend"
    assert catch.outbox().send("K-1", TO, subject, body, BUDGET) == "SENT"
    (rec,) = catch.records()
    assert rec["send_key"] == "K-1"
    assert rec["message_id"] == message_id_for("K-1", SmtpSettings().message_id_domain)
    assert rec["to"] == TO and rec["subject"] == subject
    assert rec["body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert rec["mode"] == "ok"


def test_blind_two_sends_two_messages(catch: Catch) -> None:
    ob = catch.outbox()
    assert ob.send("K-2", TO, SUBJECT, BODY, BUDGET) == "SENT"
    assert ob.send("K-2", TO, SUBJECT, BODY, BUDGET) == "SENT"
    recs = catch.records()
    assert [r["send_key"] for r in recs] == ["K-2", "K-2"]
    assert recs[0]["message_id"] == recs[1]["message_id"]


def test_closed_port_is_unreachable() -> None:
    with pytest.raises(SendUnreachable):
        box(free_port()).send("K-3", TO, SUBJECT, BODY, 5.0)


def test_rcpt_550_is_refused(catch: Catch) -> None:
    catch.mode("reject")
    with pytest.raises(SendRefused):
        catch.outbox().send("K-4", TO, SUBJECT, BODY, BUDGET)
    assert catch.records() == []


def test_mail_from_450_is_refused(script: Callable[[str], Script]) -> None:
    with pytest.raises(SendRefused):
        box(script("data_450").port).send("K-5", TO, SUBJECT, BODY, BUDGET)


def test_greeting_554_is_refused(script: Callable[[str], Script]) -> None:
    with pytest.raises(SendRefused):
        box(script("greeting_554").port).send("K-6", TO, SUBJECT, BODY, BUDGET)


def test_drop_after_data_is_ambiguous(catch: Catch) -> None:
    catch.mode("drop-after-data")
    with pytest.raises(SendAmbiguous):
        catch.outbox().send("K-7", TO, SUBJECT, BODY, BUDGET)
    (rec,) = catch.records()  # the message DID arrive: that is why it is ambiguous, not refused
    assert rec["send_key"] == "K-7" and rec["mode"] == "drop-after-data"


def test_slow_final_reply_is_ambiguous(catch: Catch) -> None:
    catch.mode("slow 2")
    t0 = time.monotonic()
    with pytest.raises(SendAmbiguous):
        catch.outbox().send("K-8", TO, SUBJECT, BODY, SHORT)
    assert time.monotonic() - t0 < SHORT + 1.0
    assert len(catch.records()) == 1


def test_final_550_and_garbage_ambiguous(script: Callable[[str], Script]) -> None:
    for stage in ("final_550", "final_garbage"):
        with pytest.raises(SendAmbiguous):
            box(script(stage).port).send("K-9", TO, SUBJECT, BODY, BUDGET)


def test_silent_before_text_is_unreachable(script: Callable[[str], Script]) -> None:
    t0 = time.monotonic()
    with pytest.raises(SendUnreachable):  # nothing of the message was offered, so a retry is safe
        box(script("silent_rcpt").port).send("K-10", TO, SUBJECT, BODY, SHORT)
    assert time.monotonic() - t0 < SHORT + 1.0


# -- total deadline
def test_deadline_dribbled_greeting(script: Callable[[str], Script]) -> None:
    t0 = time.monotonic()
    with pytest.raises(SendUnreachable):
        box(script("greeting_dribble").port).send("K-11", TO, SUBJECT, BODY, SHORT)
    assert time.monotonic() - t0 < SHORT + 0.6  # the full greeting would take about 5 s


def test_deadline_dribbled_final_reply(script: Callable[[str], Script]) -> None:
    t0 = time.monotonic()
    with pytest.raises(SendAmbiguous):
        box(script("final_dribble").port).send("K-12", TO, SUBJECT, BODY, SHORT)
    assert time.monotonic() - t0 < SHORT + 0.6  # each byte arrives within a per-read limit; the total must stop it


# -- header injection: refused BEFORE any network use (a closed port would otherwise say "unreachable")
@pytest.mark.parametrize("field", ["to", "subject", "key"])
@pytest.mark.parametrize("bad", ["\r\nBcc: x@example.test", "a\nb", "a\rb", "a\x00b"])
def test_line_break_refused_early(field: str, bad: str) -> None:
    args = {"key": "K-13", "to": TO, "subject": SUBJECT}
    args[field] = args[field] + bad if field != "to" else "ada" + bad + "@example.test"
    with pytest.raises(SendRefused):
        box(free_port()).send(args["key"], args["to"], args["subject"], BODY, BUDGET)


def test_odd_recipients_refused_early() -> None:
    for bad in ("", "no-at-sign", "a b@example.test", "<a@example.test>", "ädä@example.test"):
        with pytest.raises(SendRefused):
            box(free_port()).send("K-14", bad, SUBJECT, BODY, BUDGET)


# -- load
def test_50_concurrent_sends_all_land(catch: Catch) -> None:
    ob = catch.outbox()
    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(lambda i: ob.send(f"K-c{i}", TO, SUBJECT, f"{BODY} {i}", 10.0), range(50)))
    assert results == ["SENT"] * 50
    wait_until(lambda: len(catch.records()) == 50, what="50 records")
    assert {r["send_key"] for r in catch.records()} == {f"K-c{i}" for i in range(50)}


def test_catcher_takes_200_connections(catch: Catch) -> None:
    held = []
    try:
        for _ in range(200):
            s = socket.create_connection((HOST, catch.port), timeout=5)
            held.append(s)
        assert all(s.recv(4).startswith(b"220") for s in held)
    finally:
        for s in held:
            s.close()


# -- settings and wiring
def test_settings_from_env_and_checks() -> None:
    s = SmtpSettings.from_env(
        {"DESK_SMTP_HOST": HOST, "DESK_SMTP_PORT": "2600", "DESK_SMTP_MESSAGE_ID_DOMAIN": "mail.example.test"}
    )
    assert s.port == 2600 and s.message_id_domain == "mail.example.test"
    assert make_smtp_outbox({}).capability == "blind"
    for bad in ({"DESK_SMTP_PORT": "0"}, {"DESK_SMTP_FROM": "x y@z"}, {"DESK_SMTP_MESSAGE_ID_DOMAIN": "A B"}):
        with pytest.raises(ValueError):
            SmtpSettings.from_env(bad)
    with pytest.raises(ValueError):
        SmtpSettings.from_env({"DESK_SMTP_TIMEOUT_SECONDS": "0"})


def test_loader_swaps_in_smtp_outbox(env, tmp_path: Path) -> None:  # noqa: ANN001
    srv, url = env.fakes("keyed")
    host, port = url.removeprefix("http://").split(":")
    cfg = Config(
        db_path=tmp_path / "d.sqlite3",
        fakes_host=host,
        fakes_port=int(port),
        replies_path=REPO / "demo_only" / "replies.json",
    )
    _crm, outbox, _q = load_smtp_adapters(cfg, {"DESK_SMTP_PORT": "2601"})
    assert isinstance(outbox, SmtpOutbox) and outbox.capability == "blind"


# ---------- rules for the live-run tooling ----------
@pytest.mark.parametrize("name", ["localhost", "[::1]", "mail.example.test", "", "127.0.0.1 "])
def test_host_must_be_an_ip_address(name: str) -> None:
    with pytest.raises(ValueError, match="IP address"):
        SmtpSettings(host=name)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0])
def test_settings_timeout_must_be_finite_and_positive(bad: float) -> None:
    with pytest.raises(ValueError, match="DESK_SMTP_TIMEOUT_SECONDS"):
        SmtpSettings(host=HOST, timeout_seconds=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_send_nan_timeout_refused_first(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        box(1).send("K-1", TO, SUBJECT, BODY, bad)  # port 1 would be refused: the error comes first


@pytest.mark.parametrize("to", ["a,b@example.test", "a;b@example.test", 'a"b@example.test', "a(b)@example.test"])
def test_address_with_list_or_quote_characters_is_refused(to: str) -> None:
    with pytest.raises(SendRefused):
        box(1).send("K-1", to, SUBJECT, BODY, BUDGET)


@pytest.mark.parametrize("bad", ["a\x85b", "a\x7fb", "a b", "a\x1bb", "a\x0bb"])
@pytest.mark.parametrize("field", ["send_key", "subject"])
def test_every_control_or_line_separator_refused_in_headers(bad: str, field: str) -> None:
    key, subject = ("K-1", SUBJECT)
    if field == "send_key":
        key = bad
    else:
        subject = bad
    with pytest.raises(SendRefused):
        box(1).send(key, TO, subject, BODY, BUDGET)


def test_non_ascii_mail_from_is_refused() -> None:
    with pytest.raises(ValueError, match="ASCII"):
        SmtpSettings(host=HOST, mail_from="désk@example.test")


def test_bare_three_digit_reply_is_a_reply(script: Callable[[str], Script]) -> None:
    assert box(script("bare_codes").port).send("K-1", TO, SUBJECT, BODY, BUDGET) == "SENT"


def test_ehlo_5xx_falls_back_to_helo(script: Callable[[str], Script]) -> None:
    assert box(script("ehlo_500").port).send("K-1", TO, SUBJECT, BODY, BUDGET) == "SENT"


def test_helo_refusal_is_a_refusal(script: Callable[[str], Script]) -> None:
    with pytest.raises(SendRefused):
        box(script("ehlo_helo_500").port).send("K-1", TO, SUBJECT, BODY, BUDGET)


def test_data_5xx_is_a_refusal_not_ambiguous(script: Callable[[str], Script]) -> None:
    with pytest.raises(SendRefused):
        box(script("data_554").port).send("K-1", TO, SUBJECT, BODY, BUDGET)


def test_unexpected_error_before_the_text_is_unreachable(monkeypatch: pytest.MonkeyPatch, catch: Catch) -> None:
    def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("odd")

    monkeypatch.setattr("lead_desk.smtp_outbox._read_reply", boom)
    with pytest.raises(SendUnreachable):
        catch.outbox().send("K-1", TO, SUBJECT, BODY, BUDGET)
    assert catch.records() == []


def _fail_reply_number(monkeypatch: pytest.MonkeyPatch, number: int, error: type[BaseException]) -> None:
    import lead_desk.smtp_outbox as mod

    real = mod._read_reply
    calls = {"n": 0}

    def late(sock: socket.socket, budget: object, buf: bytearray) -> int:
        calls["n"] += 1
        if calls["n"] == number:  # 1 greeting, 2 EHLO, 3 MAIL, 4 RCPT, 5 DATA, 6 the final reply of the text
            raise error("odd")
        return real(sock, budget, buf)  # type: ignore[arg-type]

    monkeypatch.setattr(mod, "_read_reply", late)


def test_unexpected_error_after_the_text_began_is_ambiguous(monkeypatch: pytest.MonkeyPatch, catch: Catch) -> None:
    _fail_reply_number(monkeypatch, 6, RuntimeError)
    with pytest.raises(SendAmbiguous):
        catch.outbox().send("K-1", TO, SUBJECT, BODY, BUDGET)


@pytest.mark.parametrize("number", [1, 6])
def test_memory_error_is_never_swallowed(monkeypatch: pytest.MonkeyPatch, catch: Catch, number: int) -> None:
    _fail_reply_number(monkeypatch, number, MemoryError)
    with pytest.raises(MemoryError):
        catch.outbox().send("K-1", TO, SUBJECT, BODY, BUDGET)


def test_module_docstring_does_not_claim_real_mail() -> None:
    import lead_desk.smtp_outbox as mod

    doc = mod.__doc__ or ""
    assert doc.splitlines()[0].startswith("A test mail outbox") and "real mail outbox" not in doc


@pytest.mark.parametrize("where", ["socket", "connect"])
def test_no_file_handle_is_no_descriptors_not_unreachable(
    monkeypatch: pytest.MonkeyPatch, catch: Catch, where: str
) -> None:
    """EMFILE while making the connection says nothing about the mail system: the desk must not spend a try on it."""
    err = OSError(errno.EMFILE, "too many open files")
    if where == "socket":

        def no_socket(*_a: object, **_k: object) -> socket.socket:
            raise err

        monkeypatch.setattr("lead_desk.smtp_outbox.socket.socket", no_socket)
    else:
        real = socket.socket.connect

        def no_connect(self: socket.socket, *_a: object) -> None:
            raise err

        monkeypatch.setattr(socket.socket, "connect", no_connect)
        _ = real
    with pytest.raises(SendNoDescriptors):
        catch.outbox().send("K-1", TO, SUBJECT, BODY, BUDGET)
    assert catch.records() == []
