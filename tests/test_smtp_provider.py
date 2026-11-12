"""SmtpProviderOutbox and the IMAP counter against scripted loopback TLS servers. Made-up data only."""

from __future__ import annotations

import ssl
from collections import Counter
from collections.abc import Iterator
from email import message_from_bytes
from pathlib import Path
from typing import Any

import pytest

from demo_only.fake_imap import FakeImap
from demo_only.fake_smtp_tls import FAKE_PASSWORD, FakeSmtp, make_cert
from lead_desk.config import Config
from lead_desk.constants import MAIL_SPAM_FOLDER
from lead_desk.contract import SendAmbiguous, SendRefused, SendUnreachable
from lead_desk.imap_count import ImapCountError, ImapSettings, counts_by_inbox, counts_by_key, counts_for_keys
from lead_desk.loader import load_demo_adapters
from lead_desk.smtp_provider import ProviderSettings, SmtpProviderOutbox, make_outbox, read_secret_file

HOST = "localhost"  # the certificate names it; verification stays ON
PW = FAKE_PASSWORD  # made up, built from parts in demo_only/fake_smtp_tls.py
LEAD_MAIL = "ada@example.test"
INBOXES = ("inbox-a@example.test", "inbox-b@example.test")
SUBJECT, BODY = "Thanks, Ada", "Hi Ada, we got your message."


class Certs:
    def __init__(self, tmp: Path) -> None:
        made = make_cert(tmp, HOST)
        if made is None:
            pytest.skip("the openssl program is not installed, so no test certificate can be made")
        self.cert, self.key = made
        self.ctx = ssl.create_default_context(cafile=str(self.cert))  # trusts the test certificate; checks the name


@pytest.fixture
def certs(tmp_path: Path) -> Certs:
    return Certs(tmp_path)


@pytest.fixture
def pwfile(tmp_path: Path) -> Path:
    p = tmp_path / "pw.txt"
    p.write_text(
        PW[:7] + " " + PW[7:] + "\n", encoding="utf-8"
    )  # providers show an app password in groups: spaces dropped
    return p


@pytest.fixture
def servers() -> Iterator[list[Any]]:
    made: list[Any] = []
    yield made
    for s in made:
        s.close()


def start(certs: Certs, servers: list[Any], script: str = "ok", implicit_tls: bool = False) -> FakeSmtp:
    srv = FakeSmtp(certs.cert, certs.key, HOST, script, implicit_tls)
    servers.append(srv)
    return srv


def outbox(srv: FakeSmtp, certs: Certs, pwfile: Path, **kw: Any) -> SmtpProviderOutbox:
    base: dict[str, Any] = {
        "host": HOST,
        "port": srv.port,
        "user": srv.user,
        "password_file": pwfile,
        "mail_from": "sender@example.test",
        "recipients": INBOXES,
        "max_sends": 20,
        "security": "ssl" if srv.implicit_tls else "starttls",
        "timeout_seconds": 3.0,
    }
    return SmtpProviderOutbox(ProviderSettings(**(base | kw)), ssl_context=certs.ctx)


def test_starttls_login_and_one_message_to_a_test_inbox_never_to_the_lead(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = start(certs, servers)
    ob = outbox(srv, certs, pwfile)
    assert ob.capability == "blind"
    assert ob.send("RUN1-L1", LEAD_MAIL, SUBJECT, BODY, 3.0) == "SENT"
    ((user, _pw, mail_from, rcpt, raw),) = srv.messages
    assert user == srv.user and mail_from == "sender@example.test"
    assert rcpt in INBOXES and rcpt == ob.recipient_for("RUN1-L1")
    assert LEAD_MAIL.encode() not in raw
    msg = message_from_bytes(raw)
    assert msg["X-Send-Key"] == "RUN1-L1" and msg["To"] == rcpt
    assert msg["Subject"].startswith("[Hadavin demo] ") and "made-up lead" in msg.get_payload(decode=True).decode()
    assert msg["Message-ID"].startswith("<") and "RUN1-L1" not in msg["Message-ID"]
    assert srv.commands.count("STARTTLS") == 1 and (ob.attempts, ob.sent) == (1, 1)


def test_implicit_tls_port(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = start(certs, servers, implicit_tls=True)
    assert outbox(srv, certs, pwfile).send("RUN1-L2", LEAD_MAIL, SUBJECT, BODY, 3.0) == "SENT"
    assert len(srv.messages) == 1


def test_recipients_are_spread_and_stable(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    ob = outbox(start(certs, servers), certs, pwfile)
    picks = {ob.recipient_for(f"RUN1-L{i}") for i in range(40)}
    assert picks == set(INBOXES)
    assert ob.recipient_for("RUN1-L7") == ob.recipient_for("RUN1-L7")


def test_certificate_not_trusted_is_unreachable_and_nothing_is_sent(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = start(certs, servers)
    ob = SmtpProviderOutbox(
        ProviderSettings(HOST, srv.port, srv.user, pwfile, "sender@example.test", INBOXES, 5, timeout_seconds=3.0)
    )  # default trust store: the test certificate is not in it
    with pytest.raises(SendUnreachable):
        ob.send("RUN1-L3", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert not srv.messages and "AUTH" not in srv.commands


def test_certificate_for_another_name_is_unreachable(
    certs: Certs, pwfile: Path, servers: list[Any], tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    made = make_cert(other, "other.test")
    assert made is not None
    srv = FakeSmtp(made[0], made[1], HOST)  # serves a certificate that names other.test, we connect to localhost
    servers.append(srv)
    ctx = ssl.create_default_context(cafile=str(made[0]))  # the chain is trusted, the NAME is wrong
    ob = SmtpProviderOutbox(
        ProviderSettings(HOST, srv.port, srv.user, pwfile, "sender@example.test", INBOXES, 5, timeout_seconds=3.0), ctx
    )
    with pytest.raises(SendUnreachable):
        ob.send("RUN1-L3", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert not srv.messages and "AUTH" not in srv.commands


def test_starttls_not_offered_is_refused_and_no_password_goes_in_clear(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = start(certs, servers, "no_starttls")
    with pytest.raises(SendRefused):
        outbox(srv, certs, pwfile).send("RUN1-L4", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert "AUTH" not in srv.commands


def test_bad_login_is_refused_without_the_password_in_the_error(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = start(certs, servers, "bad_login")
    with pytest.raises(SendRefused) as e:
        outbox(srv, certs, pwfile).send("RUN1-L5", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert PW not in repr(e.value) and not srv.messages


def test_recipient_refused_before_the_text_is_refused(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = start(certs, servers, "rcpt_550")
    with pytest.raises(SendRefused):
        outbox(srv, certs, pwfile).send("RUN1-L6", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert "DATA" not in srv.commands


@pytest.mark.parametrize("script", ["data_hangup", "silent_after_text", "final_452"])
def test_any_doubt_after_the_text_started_is_ambiguous(
    certs: Certs, pwfile: Path, servers: list[Any], script: str
) -> None:
    srv = start(certs, servers, script)
    with pytest.raises(SendAmbiguous):
        outbox(srv, certs, pwfile, timeout_seconds=3.0).send("RUN1-L8", LEAD_MAIL, SUBJECT, BODY, 3.0)


def test_silent_greeting_is_unreachable_inside_the_budget(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = start(certs, servers, "greeting_silent")
    with pytest.raises(SendUnreachable):
        outbox(srv, certs, pwfile).send("RUN1-L9", LEAD_MAIL, SUBJECT, BODY, 0.8)


def test_connection_refused_is_unreachable(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = start(certs, servers)
    ob = outbox(srv, certs, pwfile)
    srv.close()
    with pytest.raises(SendUnreachable):
        ob.send("RUN1-L9", LEAD_MAIL, SUBJECT, BODY, 3.0)


@pytest.mark.parametrize(
    ("key", "to", "subject"),
    [
        ("RUN1-L1\r\nBcc: x@example.test", LEAD_MAIL, SUBJECT),
        ("RUN1-L1", "a@example.test\r\nBcc: x@example.test", SUBJECT),
        ("RUN1-L1", LEAD_MAIL, "Hi\r\nBcc: x@example.test"),
        ("", LEAD_MAIL, SUBJECT),
        ("RUN 1", LEAD_MAIL, SUBJECT),
    ],
)
def test_header_injection_and_odd_keys_are_refused_before_the_network(
    certs: Certs, pwfile: Path, servers: list[Any], key: str, to: str, subject: str
) -> None:
    srv = start(certs, servers)
    ob = outbox(srv, certs, pwfile)
    with pytest.raises(SendRefused):
        ob.send(key, to, subject, BODY, 3.0)
    assert not srv.commands and ob.attempts == 0


@pytest.mark.parametrize("content", [None, "", "  \n", "x"])
def test_password_file_missing_or_empty_is_refused_without_path_or_network(
    certs: Certs, pwfile: Path, servers: list[Any], content: str | None
) -> None:
    srv = start(certs, servers)
    ob = outbox(srv, certs, pwfile)
    if content is None:
        pwfile.unlink()
    else:
        pwfile.write_text(content, encoding="utf-8")
    with pytest.raises(SendRefused) as e:
        ob.send("RUN1-L1", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert str(pwfile) not in repr(e.value) and pwfile.name not in repr(e.value)
    assert not srv.commands


def test_send_cap_stops_before_the_network(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = start(certs, servers)
    ob = outbox(srv, certs, pwfile, max_sends=1)
    ob.send("RUN1-L1", LEAD_MAIL, SUBJECT, BODY, 3.0)
    with pytest.raises(SendRefused):
        ob.send("RUN1-L2", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert len(srv.messages) == 1


def test_body_with_a_leading_dot_line_is_not_cut(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = start(certs, servers)
    outbox(srv, certs, pwfile).send("RUN1-L1", LEAD_MAIL, SUBJECT, "line\n.\nafter", 3.0)
    assert b"after" in srv.messages[0][4]


def test_settings_validation() -> None:
    base: dict[str, Any] = {
        "host": HOST,
        "port": 587,
        "user": "u",
        "password_file": Path("p"),
        "mail_from": "a@example.test",
        "recipients": INBOXES,
        "max_sends": 1,
    }
    for bad in ({"recipients": ()}, {"host": "bad host x"}, {"security": "plain"}, {"max_sends": 0}, {"port": 0}):
        with pytest.raises(ValueError):
            ProviderSettings(**(base | bad))
    with pytest.raises(ValueError, match="DESK_SMTPP_MAX_SENDS"):
        ProviderSettings.from_env(
            {
                k: "x"
                for k in (
                    "DESK_SMTPP_HOST",
                    "DESK_SMTPP_USER",
                    "DESK_SMTPP_PASSWORD_FILE",
                    "DESK_SMTPP_FROM",
                    "DESK_SMTPP_RECIPIENTS",
                )
            }
        )


def test_loader_selects_the_provider_outbox(pwfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "DESK_SMTPP_HOST": HOST,
        "DESK_SMTPP_USER": "u",
        "DESK_SMTPP_PASSWORD_FILE": str(pwfile),
        "DESK_SMTPP_FROM": "a@example.test",
        "DESK_SMTPP_RECIPIENTS": ",".join(INBOXES),
        "DESK_SMTPP_MAX_SENDS": "3",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert Config().outbox == "http"
    _, ob, _ = load_demo_adapters(Config(outbox="smtp_provider"))
    assert isinstance(ob, SmtpProviderOutbox)


# -- IMAP counter ---------------------------------------------------------------------------------------------------


def test_imap_counts_exact_keys_per_folder_and_prints_nothing(
    certs: Certs, pwfile: Path, servers: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    srv = FakeImap(
        certs.cert,
        certs.key,
        HOST,
        {"INBOX": ["RUN1-L1", "RUN1-L1", "RUN1-L10", "OTHER-L1"], MAIL_SPAM_FOLDER: ["RUN1-L2"]},
    )
    servers.append(srv)
    s = ImapSettings(HOST, srv.user, pwfile, port=srv.port, timeout_seconds=3.0, ssl_context=certs.ctx)
    got = counts_by_key(s, "RUN1-")
    assert got == {"INBOX": {"RUN1-L1": 2, "RUN1-L10": 1}, MAIL_SPAM_FOLDER: {"RUN1-L2": 1}}
    assert capsys.readouterr().out == "" and srv.logins == [(srv.user, PW)]


def test_imap_failures_are_classed_without_secrets(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": []})
    servers.append(srv)
    s = ImapSettings(HOST, "someone-else", pwfile, port=srv.port, timeout_seconds=3.0, ssl_context=certs.ctx)
    with pytest.raises(ImapCountError) as e:
        counts_by_key(s, "RUN1-")
    assert PW not in repr(e.value)
    with pytest.raises(ImapCountError):
        counts_by_key(s, 'RUN1-" OR ALL')  # a prefix with quote characters never reaches the server
    assert srv.logins == [("someone-else", PW)]


def test_imap_counts_by_inbox_keeps_mailboxes_apart(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    from lead_desk.imap_count import counts_by_inbox

    a = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["RUN1-L1"]})
    b = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["RUN1-L2", "RUN1-L2"]})
    servers.extend([a, b])
    boxes = [
        ImapSettings(HOST, x.user, pwfile, port=x.port, folders=("INBOX",), timeout_seconds=3.0, ssl_context=certs.ctx)
        for x in (a, b)
    ]
    assert counts_by_inbox(boxes, "RUN1-") == [{"INBOX": {"RUN1-L1": 1}}, {"INBOX": {"RUN1-L2": 2}}]


# -- certificate trust, password files and counting ----------------------------------------------------------------


def test_imap_default_trust_rejects_an_untrusted_certificate_and_sends_no_password(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["RUN1-L1"]})  # self-signed, not in the system store
    servers.append(srv)
    s = ImapSettings(HOST, srv.user, pwfile, port=srv.port, folders=("INBOX",), timeout_seconds=3.0)  # ssl_context None
    assert s.ssl_context is None
    with pytest.raises(ImapCountError):
        counts_by_key(s, "RUN1-")
    with pytest.raises(ImapCountError):
        counts_for_keys(s, ["RUN1-L1"])
    assert srv.logins == []  # the app password was never sent to the unverified server


def test_imap_certificate_for_another_name_is_rejected(
    certs: Certs, pwfile: Path, servers: list[Any], tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    made = make_cert(other, "other.test")
    assert made is not None
    srv = FakeImap(made[0], made[1], HOST, {"INBOX": []})
    servers.append(srv)
    s = ImapSettings(HOST, srv.user, pwfile, port=srv.port, timeout_seconds=3.0, ssl_context=certs.ctx)
    with pytest.raises(ImapCountError):
        counts_by_key(s, "RUN1-")
    assert srv.logins == []


def test_smtp_implicit_tls_with_an_untrusted_certificate_is_unreachable_and_sends_nothing(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = start(certs, servers, implicit_tls=True)
    ob = SmtpProviderOutbox(  # no ssl_context and no CA file: the system store does not know the test certificate
        ProviderSettings(
            HOST, srv.port, srv.user, pwfile, "sender@example.test", INBOXES, 5, security="ssl", timeout_seconds=3.0
        )
    )
    with pytest.raises(SendUnreachable):
        ob.send("RUN1-L3", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert not srv.messages and "AUTH" not in srv.commands


def test_every_attempt_has_its_own_message_id_and_attempt_header_but_the_same_count_key(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = start(certs, servers)
    ob = outbox(srv, certs, pwfile)
    for _ in range(3):
        ob.send("RUN1-L1", LEAD_MAIL, SUBJECT, BODY, 3.0)
    msgs = [message_from_bytes(m[4]) for m in srv.messages]
    assert [m["X-Send-Key"] for m in msgs] == ["RUN1-L1"] * 3  # the count key stays the same
    assert len({m["Message-ID"] for m in msgs}) == 3  # a provider cannot drop a repeat as a duplicate
    assert len({m["X-Send-Attempt"] for m in msgs}) == 3
    assert all(m["X-Send-Attempt"] in m["Message-ID"] and "RUN1-L1" not in m["Message-ID"] for m in msgs)


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        (b"\xef\xbb\xbf" + PW.encode() + b"\r\n", None),  # a byte-order mark that some text editors add is accepted
        (PW.encode() + b"\nsecond line", "more than one line"),
        (PW.encode("utf-16"), "UTF-16"),
        (b"\xff\xfe\x00", "UTF-16"),
        (b"\x80\x81" + PW.encode(), "not UTF-8"),
        (b"p" * 5000, "too large"),
    ],
)
def test_password_file_formats_give_a_helpful_fixed_text(tmp_path: Path, raw: bytes, words: str | None) -> None:
    p = tmp_path / "x.txt"
    p.write_bytes(raw)
    if words is None:
        assert read_secret_file(p) == PW
        return
    with pytest.raises(SendRefused) as e:
        read_secret_file(p, "mailbox password")
    assert words in str(e.value) and "mailbox password" in str(e.value) and p.name not in str(e.value)


def test_a_multi_line_password_file_is_refused_before_the_network(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = start(certs, servers)
    pwfile.write_text(PW + "\nsecond line\n", encoding="utf-8")
    with pytest.raises(SendRefused, match="more than one line"):
        outbox(srv, certs, pwfile).send("RUN1-L1", LEAD_MAIL, SUBJECT, BODY, 3.0)
    assert srv.commands == []


def test_imap_password_file_problems_are_count_errors_without_the_path(
    certs: Certs, tmp_path: Path, servers: list[Any]
) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": []})
    servers.append(srv)
    bad = tmp_path / "secret-name.txt"
    bad.write_text("one\ntwo", encoding="utf-8")
    s = ImapSettings(HOST, srv.user, bad, port=srv.port, timeout_seconds=3.0, ssl_context=certs.ctx)
    with pytest.raises(ImapCountError) as e:
        counts_by_key(s, "RUN1-")
    assert "secret-name" not in str(e.value) and "more than one line" in str(e.value)


def test_counts_for_keys_are_exact_and_summed_per_key_across_both_inboxes(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    a = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001", "L-0010", "OTHER"]})
    b = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001"]})  # the same key in the OTHER inbox
    servers.extend([a, b])
    boxes = [
        ImapSettings(HOST, x.user, pwfile, port=x.port, folders=("INBOX",), timeout_seconds=3.0, ssl_context=certs.ctx)
        for x in (a, b)
    ]
    per_box = counts_by_inbox(boxes, "L", keys=["L-001", "L-0010", "L-002"])
    assert per_box == [{"INBOX": {"L-001": 1, "L-0010": 1}}, {"INBOX": {"L-001": 1}}]
    total: Counter[str] = Counter()
    for box in per_box:
        for folder in box.values():
            total.update(folder)
    assert total["L-001"] == 2 and total["L-0010"] == 1  # exact keys, summed over the inboxes


def test_counts_for_keys_fall_back_to_the_fetched_headers_when_the_search_finds_nothing(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = FakeImap(
        certs.cert, certs.key, HOST, {"INBOX": ["L-001", "L-001", "L-0010", "OTHER"]}, header_search_blind=True
    )
    servers.append(srv)
    s = ImapSettings(
        HOST, srv.user, pwfile, port=srv.port, folders=("INBOX",), timeout_seconds=3.0, ssl_context=certs.ctx
    )
    assert counts_by_key(s, "L-") == {"INBOX": {}}  # the prefix search sees nothing on such a server
    assert counts_for_keys(s, ["L-001", "L-0010"]) == {"INBOX": {"L-001": 2, "L-0010": 1}}


def test_counts_for_keys_refuse_bad_keys(certs: Certs, pwfile: Path) -> None:
    s = ImapSettings(HOST, "u", pwfile, ssl_context=certs.ctx)
    for bad in ([], ['L-1" OR ALL'], ["x" * 65]):
        with pytest.raises(ImapCountError):
            counts_for_keys(s, bad)


def test_make_outbox_budget_is_clamped_to_the_desks_send_timeout(pwfile: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "DESK_SMTPP_HOST": HOST,
        "DESK_SMTPP_USER": "u",
        "DESK_SMTPP_PASSWORD_FILE": str(pwfile),
        "DESK_SMTPP_FROM": "a@example.test",
        "DESK_SMTPP_RECIPIENTS": ",".join(INBOXES),
        "DESK_SMTPP_MAX_SENDS": "3",
        "DESK_SMTPP_TIMEOUT_SECONDS": "8",
    }.items():
        monkeypatch.setenv(k, v)
    short = make_outbox(Config(send_timeout_seconds=2.0, outbox="smtp_provider"))
    long = make_outbox(Config(send_timeout_seconds=30.0, outbox="smtp_provider"))
    assert isinstance(short, SmtpProviderOutbox) and isinstance(long, SmtpProviderOutbox)
    assert short._s.timeout_seconds == 2.0 and long._s.timeout_seconds == 8.0  # noqa: SLF001


def test_a_bad_number_in_the_settings_names_the_setting_not_the_value() -> None:
    env = {
        k: "x@example.test" if k in ("DESK_SMTPP_FROM", "DESK_SMTPP_RECIPIENTS") else "ok-" + k.lower()
        for k in (
            "DESK_SMTPP_HOST",
            "DESK_SMTPP_USER",
            "DESK_SMTPP_PASSWORD_FILE",
            "DESK_SMTPP_FROM",
            "DESK_SMTPP_RECIPIENTS",
        )
    }
    with pytest.raises(ValueError) as e:
        ProviderSettings.from_env({**env, "DESK_SMTPP_MAX_SENDS": "sec" + "ret-9"})
    assert "DESK_SMTPP_MAX_SENDS" in str(e.value) and "ret-9" not in str(e.value)


# -- folders, the total time limit and printouts -------------------------------------------------------------------


def _imap(certs: Certs, pwfile: Path, srv: Any, **kw: Any) -> ImapSettings:
    base: dict[str, Any] = {"port": srv.port, "timeout_seconds": 3.0, "ssl_context": certs.ctx, "folders": ("INBOX",)}
    return ImapSettings(HOST, srv.user, pwfile, **(base | kw))


def test_a_folder_the_server_does_not_have_is_left_out_not_shown_as_empty(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001"]})
    servers.append(srv)
    s = _imap(certs, pwfile, srv, folders=("INBOX", "Spamfolder"))  # a folder name that is not the default
    assert counts_for_keys(s, ["L-001"]) == {"INBOX": {"L-001": 1}}
    assert counts_by_key(s, "L-") == {"INBOX": {"L-001": 1}}


def test_a_key_the_search_misses_triggers_the_scan_and_the_larger_count_wins(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    """Fallback per MISSING key: when one wanted key is found by the search and another is not, the headers are scanned
    too, and per folder and key the larger of the two counts is kept (a second delivery is never hidden)."""
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001", "L-0020", "L-0020"]})
    srv.blind_keys = {"L-0020"}  # the search finds L-001 but never L-0020 (a server that splits header values)
    servers.append(srv)
    s = _imap(certs, pwfile, srv)
    got = counts_for_keys(s, ["L-001", "L-0020"])  # L-0020 was not found by the search: the scan runs
    assert got == {"INBOX": {"L-001": 1, "L-0020": 2}}
    srv.commands.clear()
    counts_for_keys(s, ["L-001"])  # every key found by the search: no scan
    assert "FETCH" in srv.commands and srv.commands.count("SEARCH") == 1


def test_one_call_is_bounded_in_total_not_only_per_step(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001"]})
    srv.delay = 0.2
    servers.append(srv)
    s = _imap(certs, pwfile, srv, total_seconds=0.3)
    with pytest.raises(ImapCountError, match="total time limit"):
        counts_for_keys(s, [f"L-00{n}" for n in range(1, 9)])  # eight searches of 0.2 s each
    assert srv.commands.count("SEARCH") < 8  # it stopped part-way instead of finishing every search


def test_a_non_ascii_user_is_a_clear_fixed_error_not_a_crash(certs: Certs, pwfile: Path, servers: list[Any]) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": []})
    servers.append(srv)
    s = ImapSettings(HOST, "usér", pwfile, port=srv.port, timeout_seconds=3.0, ssl_context=certs.ctx)
    with pytest.raises(ImapCountError, match="characters the mailbox login cannot carry"):
        counts_by_key(s, "L-")


def test_settings_do_not_print_addresses_or_paths() -> None:
    text = repr(
        ProviderSettings("host.example.test", 587, "user-marker", Path("pw-marker"), "from-marker@example.test",
                         ("rcpt-marker@example.test",), 1)
    ) + repr(ImapSettings("host.example.test", "user-marker", Path("pw-marker")))  # fmt: skip
    for marker in ("user-marker", "pw-marker", "from-marker", "rcpt-marker"):
        assert marker not in text


# -- scan merging and which mailbox a key belongs to ---------------------------------------------------------------


def test_the_larger_count_wins_and_a_scan_never_replaces_the_search_result(
    certs: Certs, pwfile: Path, servers: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan sees only the newest header (FALLBACK_SCAN_MAX=1) and so only L-0020; the search saw L-001 twice.
    A merge that replaced or took the smaller count would lose the two L-001 mails."""
    monkeypatch.setattr("lead_desk.imap_count.FALLBACK_SCAN_MAX", 1)
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001", "L-001", "L-0020"]})
    srv.blind_keys = {"L-0020"}
    servers.append(srv)
    got = counts_for_keys(_imap(certs, pwfile, srv), ["L-001", "L-0020"])
    assert got == {"INBOX": {"L-001": 2, "L-0020": 1}}


def test_a_key_that_belongs_to_the_other_mailbox_does_not_trigger_a_scan(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    a = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-A1"]})
    b = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-B1"]})
    servers += [a, b]
    keys = ["L-A1", "L-B1"]
    got = counts_by_inbox([_imap(certs, pwfile, a), _imap(certs, pwfile, b)], "L", keys, [{"L-A1"}, {"L-B1"}])
    assert got == [{"INBOX": {"L-A1": 1}}, {"INBOX": {"L-B1": 1}}]
    assert a.commands.count("SEARCH") == 2 and b.commands.count("SEARCH") == 2  # one per key, no SEARCH ALL


def test_a_key_missing_from_its_own_mailbox_still_triggers_the_scan(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    a = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-A1"]})
    servers.append(a)
    counts_for_keys(_imap(certs, pwfile, a), ["L-A1", "L-A2"], expected=["L-A1", "L-A2"])
    assert a.commands.count("SEARCH") == 3  # two key searches and the SEARCH ALL of the scan


def test_after_the_total_limit_the_connection_is_closed_without_a_logout(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001"]})
    srv.delay = 0.2
    servers.append(srv)
    with pytest.raises(ImapCountError, match="total time limit"):
        counts_for_keys(_imap(certs, pwfile, srv, total_seconds=0.3), [f"L-00{n}" for n in range(1, 9)])
    assert "LOGOUT" not in srv.commands
    fast = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001"]})
    servers.append(fast)
    counts_for_keys(_imap(certs, pwfile, fast), ["L-001"])
    assert "LOGOUT" in fast.commands  # inside the limit the session still ends politely


# -- folder names, scan switch and the connect time -------------------------------------------------------------------


@pytest.mark.parametrize("folder", ["Spamf\u00f6lder", 'a"b', "a" + chr(92) + "b", ""])
def test_a_folder_name_the_program_cannot_send_is_a_clear_fixed_error(
    certs: Certs, pwfile: Path, servers: list[Any], folder: str
) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": []})
    servers.append(srv)
    with pytest.raises(ImapCountError, match="folder name has characters this program cannot send"):
        counts_for_keys(_imap(certs, pwfile, srv, folders=("INBOX", folder)), ["L-001"])
    assert srv.commands == []  # refused before any connection work


def test_scan_false_never_runs_the_slow_scan_even_when_an_expected_key_is_missing(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001", "L-0020"]})
    srv.blind_keys = {"L-0020"}
    servers.append(srv)
    s = _imap(certs, pwfile, srv)
    assert counts_for_keys(s, ["L-001", "L-0020"], scan=False) == {"INBOX": {"L-001": 1}}
    assert srv.commands.count("SEARCH") == 2  # one search per key, no SEARCH ALL
    assert counts_for_keys(s, ["L-001", "L-0020"], scan=True) == {"INBOX": {"L-001": 1, "L-0020": 1}}


def test_the_total_time_limit_counts_from_before_the_connection_and_the_greeting(
    certs: Certs, pwfile: Path, servers: list[Any]
) -> None:
    srv = FakeImap(certs.cert, certs.key, HOST, {"INBOX": ["L-001"]})
    srv.greeting_delay = 0.6  # a slow connect, well inside the single-step limit of 3 s
    servers.append(srv)
    with pytest.raises(ImapCountError, match="total time limit"):
        counts_for_keys(_imap(certs, pwfile, srv, total_seconds=0.3), ["L-001"])
    assert "LOGIN" not in srv.commands  # it stopped right after the greeting, before logging in


def test_a_closed_fake_server_refuses_new_connections(certs: Certs, servers: list[Any]) -> None:
    """On Linux, closing a listening socket while a thread waits in accept() leaves the port open; the fakes must
    really stop listening, so that tests of 'connection refused' do not depend on timing."""
    import socket

    smtp = start(certs, servers)
    imap = FakeImap(certs.cert, certs.key, HOST, {"INBOX": []})
    servers.append(imap)
    for fake in (smtp, imap):
        port = fake.port
        socket.create_connection((HOST, port), timeout=2).close()  # open while running
        fake.close()
        with pytest.raises(OSError):
            socket.create_connection((HOST, port), timeout=2)
