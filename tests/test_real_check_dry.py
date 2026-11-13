"""scripts/run_real_check.py end to end against local fakes (--dry-fakes): no real service, made-up data only."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_real_check as rrc  # noqa: E402

from lead_desk.adapters import CrmError  # noqa: E402
from lead_desk.imap_count import ImapSettings  # noqa: E402

FILLER = "x-value-" + "0001"  # a made-up setting value, built from parts


def test_refuses_without_config_and_without_required_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert rrc.main([]) == rrc.EXIT_REFUSED
    cfg = tmp_path / "c.env"
    cfg.write_text("DESK_DB_PATH=x\n", encoding="utf-8")
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_REFUSED
    assert "lacks" in capsys.readouterr().out


def test_dry_fakes_run_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    code = rrc.main(["--dry-fakes", "--assume-yes"])
    out = capsys.readouterr().out
    assert code == 0, out[-3000:]
    assert rrc.DRY_KEY not in out and "@example" not in out  # no key, no address is printed
    result = Path(out.split("result file: ")[1].splitlines()[0])
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["summary"]["crm_contacts_per_lead_max"] == 1
    assert data["summary"]["desk_crash_exit_code"] not in (None, 0)
    per_box = data["summary"]["mails_per_mailbox_in_config_order"]
    assert len(per_box) == 2 and sum(per_box) == data["summary"]["mails_in_mailbox"]


# -- floors, masking, clean-up and config checks -------------------------------------------------------------------


def _book(states: dict[str, tuple[str, int, str]]) -> dict[str, Any]:
    """A desk book as read_book returns it: {lead id: (state, crm_done, last_error)}."""
    return {
        "leads": list(states),
        "followups": {lid: (s, d, 0) for lid, (s, d, _) in states.items()},
        "last_errors": {lid: e for lid, (_, _, e) in states.items()},
    }


def _floors(
    book: dict[str, Any],
    expected: int,
    settled: int = 0,
    mails: int = 0,
    crm: int = 0,
    sent: int | None = 0,
    crash: int | None = 137,
    sending: int = 99,  # follow-ups in flight at the kill (99: no limit but the desk's send workers)
    poll_failed: bool = False,
):
    per_lead: Counter[str] = Counter({f"M{i}": 1 for i in range(mails)})
    pairs = [(f"L{i}", str(i)) for i in range(crm)]
    stats = {} if sent is None else {"attempts": sent, "sent": sent}
    return {
        c["id"]: c
        for c in rrc.floor_checks(book, expected, settled, per_lead, pairs, stats, crash, sending, poll_failed)
    }


def test_a_run_where_every_follow_up_ended_unknown_fails_the_floor() -> None:
    book = _book({f"L{i}": ("unknown", 2, "ambiguous") for i in range(9)})
    got = _floors(book, expected=9)
    assert got["F1"]["ok"] is False  # nothing was sent and nothing explains it
    assert all(c["id"] in ("F1", "F2", "F3", "F4", "F5", "F6") for c in got.values())


def test_the_crash_window_excuses_only_the_rows_the_desk_marked_as_in_flight() -> None:
    sent = {f"S{i}": ("sent", 1, "") for i in range(5)}
    crashed = {f"U{i}": ("unknown", 1, rrc.CRASH_REASON) for i in range(4)}
    book = _book({**sent, **crashed})
    assert _floors(book, 9, settled=5, mails=5, crm=9, sent=6)["F1"]["ok"] is True
    other = {f"U{i}": ("unknown", 1, "ambiguous") for i in range(4)}  # unknown for another reason: not excused
    assert _floors(_book({**sent, **other}), 9, settled=5, mails=5, crm=9, sent=6)["F1"]["ok"] is False
    assert _floors(book, 9, settled=6, mails=5, crm=9, sent=6)["F1"]["ok"] is False  # the settled batch must be sent


def test_the_window_cannot_be_larger_than_the_desks_send_workers() -> None:
    from lead_desk.gate import SEND_WORKERS_MAX

    n = SEND_WORKERS_MAX + 2
    book = _book({f"U{i}": ("unknown", 1, rrc.CRASH_REASON) for i in range(n)})
    f1 = _floors(book, n)["F1"]
    assert f1["ok"] is False and f1["values"]["crash_window"] == SEND_WORKERS_MAX


def test_the_floor_needs_mails_crm_contacts_and_a_counter_file() -> None:
    book = _book({f"S{i}": ("sent", 1, "") for i in range(4)})
    assert all(c["ok"] for c in _floors(book, 4, mails=4, crm=4, sent=4).values())
    assert _floors(book, 4, mails=3, crm=4, sent=4)["F2"]["ok"] is False  # a sent follow-up without a mail
    assert _floors(book, 4, mails=4, crm=3, sent=4)["F3"]["ok"] is False  # a CRM-done lead without a contact
    assert _floors(book, 4, mails=4, crm=4, sent=None)["F4"]["ok"] is False  # no counter file proves nothing


def test_the_desk_may_not_send_more_than_leads_plus_unknown_leads() -> None:
    book = _book({"S1": ("sent", 1, ""), "S2": ("sent", 1, ""), "U1": ("unknown", 1, "ambiguous")})
    assert _floors(book, 3, mails=2, crm=3, sent=4)["F4"]["ok"] is True  # 3 leads + 1 unknown
    assert _floors(book, 3, mails=2, crm=3, sent=5)["F4"]["ok"] is False  # a fifth mail: a double send somewhere


def test_addresses_are_masked_in_a_failure_table() -> None:
    result = {
        "ok": False,
        "checks": [
            {
                "id": 1,
                "name": "n",
                "ok": False,
                "detail": "x",
                "offender_count": 2,
                "offenders": ["two-leads:ada@example.test", "form:r1-p1"],
                "values": {"who": "Bo <bo@example.test>"},
            }
        ],
    }
    text = rrc.render_table(rrc.mask_result(result))
    assert "@" not in text and "two-leads:<address>" in text and "form:r1-p1" in text
    assert rrc.mask_addresses("a@b.test and c.d@e.test") == "<address> and <address>"


def test_cleanup_goes_on_after_one_error_and_names_what_is_left() -> None:
    class Crm:
        def __init__(self) -> None:
            self.done: list[str] = []

        def archive(self, cid: str) -> None:
            if cid == "2":
                raise CrmError("boom")
            self.done.append(cid)

    crm = Crm()
    done, left = rrc.archive_all(crm, [("L1", "1"), ("L2", "2"), ("L3", "3")])  # type: ignore[arg-type]
    assert (done, left) == (2, ["2"]) and crm.done == ["1", "3"]


def test_mailbox_order_must_match_the_recipients() -> None:
    def box(user: str) -> ImapSettings:
        return ImapSettings("h", user, Path("p"))

    env = {"DESK_SMTPP_RECIPIENTS": "a@example.test, B@example.test"}
    assert rrc.check_mailbox_order(env, [box("A@example.test"), box("b@example.test")]) is None
    assert rrc.check_mailbox_order(env, [box("b@example.test"), box("a@example.test")]) is not None  # swapped
    assert rrc.check_mailbox_order(env, [box("a@example.test")]) is not None  # one mailbox missing


def _valid_config(tmp_path: Path, **over: str) -> Path:
    """A config that passes every check except the ones the test breaks on purpose (two inboxes, in order)."""
    return _config_file(
        tmp_path,
        **{
            "DESK_SMTPP_RECIPIENTS": "a@example.test,b@example.test",
            "REAL_IMAP_1_USER": "a@example.test",
            "REAL_IMAP_2_USER": "b@example.test",
            **over,
        },
    )


def _config_file(tmp_path: Path, **over: str) -> Path:
    values = dict.fromkeys(rrc.REQUIRED, FILLER) | {
        "DESK_DB_PATH": str(tmp_path / "run" / "desk.db"),
        "DESK_HUBSPOT_MAX_CALLS": "5",
        "REAL_HUBSPOT_READBACK_MAX_CALLS": "5",
        "DESK_SMTPP_HOST": "localhost",
        "DESK_SMTPP_FROM": "a@example.test",
        "DESK_SMTPP_RECIPIENTS": "a@example.test",
        "DESK_SMTPP_MAX_SENDS": "5",
    }
    cfg = tmp_path / "c.env"
    cfg.write_text("\n".join(f"{k}={v}" for k, v in (values | over).items()), encoding="utf-8")
    return cfg


def test_assume_yes_is_refused_for_a_real_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert rrc.main(["--config", str(_config_file(tmp_path)), "--assume-yes"]) == rrc.EXIT_REFUSED
    assert "self-test" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["DESK_HUBSPOT_MAX_CALLS", "REAL_PERSONS", "DESK_SMTPP_MAX_SENDS", "REAL_IMAP_1_PORT"])
def test_a_refusal_never_echoes_the_value_of_a_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    secret_value = "sec" + "ret-value-77"
    cfg = _config_file(tmp_path, **{name: secret_value})
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused" in out and secret_value not in out


def test_a_refusal_for_a_missing_file_shows_no_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _config_file(tmp_path, REAL_IMAP_1_CA_FILE=str(tmp_path / "no-such-folder" / "ca-name.pem"))
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_REFUSED
    assert "no-such-folder" not in capsys.readouterr().out


def test_dry_fakes_leave_no_certificate_or_key_folder_behind(capsys: pytest.CaptureFixture[str]) -> None:
    assert rrc.main(["--dry-fakes", "--assume-yes"]) == 0
    result = Path(capsys.readouterr().out.split("result file: ")[1].splitlines()[0])
    assert result.is_file() and not (result.parent.parent / "fakes").exists()  # certificate and key files are gone


def test_a_dry_run_where_every_send_ends_in_doubt_fails(capsys: pytest.CaptureFixture[str]) -> None:
    code = rrc.main(["--dry-fakes", "--assume-yes", "--dry-smtp-script", "final_452"])
    out = capsys.readouterr().out
    assert code == rrc.EXIT_FAILED, out[-2500:]
    assert "F1  FAIL" in out and "@" not in out.split("AT LEAST ONE CHECK FAILED")[0].split("#  result")[1]
    data = json.loads(Path(out.split("result file: ")[1].splitlines()[0]).read_text(encoding="utf-8"))
    assert data["summary"]["followup_states"].get("sent", 0) == 0


# -- crash check, setting checks, child environment ----------------------------------------------------------------


def test_a_crash_that_did_not_happen_fails_floor_f5() -> None:
    book = _book({f"S{i}": ("sent", 1, "") for i in range(4)})
    assert _floors(book, 4, mails=4, crm=4, sent=4, crash=137)["F5"]["ok"] is True
    assert _floors(book, 4, mails=4, crm=4, sent=4, crash=None)["F5"]["ok"] is False  # the desk never stopped
    assert _floors(book, 4, mails=4, crm=4, sent=4, crash=0)["F5"]["ok"] is False  # it ended on its own
    assert _floors(book, 4, mails=4, crm=4, sent=4, crash=1)["F5"]["ok"] is False  # an error, not the stop on purpose


@pytest.mark.parametrize("value", ["0", "-3", "4", "abc"])
def test_a_bad_readback_cap_is_refused_before_anything_real_is_called(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    cfg = _valid_config(tmp_path, REAL_HUBSPOT_READBACK_MAX_CALLS=value)
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused" in out and "READBACK_MAX_CALLS" in out and "Start?" not in out
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("name", ["REAL_DRAIN_SECONDS", "REAL_MAIL_WAIT_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_a_bad_time_setting_is_refused_before_anything_real_is_called(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, value: str
) -> None:
    cfg = _valid_config(tmp_path, **{name: value})
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "refused" in out and name in out and "soon" not in out


def test_no_terminal_to_answer_on_is_a_stop_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_terminal(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert rrc.main(["--config", str(_valid_config(tmp_path))]) == rrc.EXIT_STOPPED
    assert "no terminal" in capsys.readouterr().out


def test_the_desk_child_gets_desk_and_real_names_from_the_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNRELATED_" + "TOKEN", "x" * 8)
    monkeypatch.setenv("DESK_QUALIFIER", "live_claude")  # stale values left in the parent shell
    monkeypatch.setenv("DESK_HUBSPOT_BASE_URL", "stale-host-value")
    monkeypatch.setenv("DESK_HUBSPOT_ALLOW_ANY_BASE_URL", "yes")
    monkeypatch.setenv("REAL_PERSONS", "9")
    monkeypatch.setenv("PATH", "p")
    env = rrc.child_environment({"DESK_PORT": "1", "DESK_QUALIFIER": "recorded", "OTHER_NAME": "o"})
    assert env["DESK_PORT"] == "1" and env["DESK_QUALIFIER"] == "recorded" and env["PATH"] == "p"
    for stale in ("UNRELATED_" + "TOKEN", "DESK_HUBSPOT_BASE_URL", "DESK_HUBSPOT_ALLOW_ANY_BASE_URL", "REAL_PERSONS"):
        assert stale not in env  # nothing DESK_* or REAL_* comes from the parent shell
    assert "OTHER_NAME" not in env  # a config name outside DESK_* and REAL_* is not passed on either
    desk = rrc.Desk(env, Path("."), 1, "t")
    assert "UNRELATED_" + "TOKEN" not in desk.env and desk.env["DESK_PORT"] == "1"


def test_stale_shell_values_do_not_reach_the_child_and_the_banner_tells_the_truth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DESK_QUALIFIER", "live_claude")
    monkeypatch.setenv("DESK_HUBSPOT_ALLOW_ANY_BASE_URL", "yes")
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    made: list[dict[str, str]] = []
    real = rrc.child_environment

    def spy(e: dict[str, str]) -> dict[str, str]:
        made.append(real(e))
        return made[-1]

    monkeypatch.setattr(rrc, "child_environment", spy)
    cfg = _valid_config(tmp_path, DESK_QUALIFIER="live_claude")  # even the config cannot start the paid model alone
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_STOPPED
    out = capsys.readouterr().out
    child = made[0]
    assert child["DESK_QUALIFIER"] == "recorded" and child["DESK_HUBSPOT_ALLOW_ANY_BASE_URL"] == "no"
    assert "the recorded replies" in out and "worst case model cost" not in out  # the banner reads the same dict


def test_folders_the_server_did_not_have_are_named_as_not_checked() -> None:
    boxes = [ImapSettings("h", "u", Path("p"), folders=("INBOX", "Spamfolder")), ImapSettings("h", "v", Path("p"))]
    per_inbox = [{"INBOX": {}}, {}]  # box 1 lacks one folder; box 2 could not be read at all
    got = rrc.folders_not_found(per_inbox, boxes)
    assert got[0] == (1, "Spamfolder") and {n for n, _ in got} == {1, 2} and len(got) == 3


def test_the_imap_host_default_comes_from_constants() -> None:
    from lead_desk.constants import MAIL_IMAP_DEFAULT_HOST, MAIL_SPAM_FOLDER

    (box,) = rrc.imap_settings({"REAL_IMAP_1_USER": "u", "REAL_IMAP_1_PASSWORD_FILE": "p"})
    assert box.host == MAIL_IMAP_DEFAULT_HOST and box.folders == ("INBOX", MAIL_SPAM_FOLDER)


# -- mailbox polling and the crash allowance -----------------------------------------------------------------------


def _sequence(*results: Any) -> Any:
    """A stand-in for counts_by_inbox: answers (or raises) the given items in turn."""
    calls: list[int] = []

    def count(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        r = results[min(len(calls), len(results)) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    return count


def _poll(count: Any, wanted_ids: set[str], waits: list[float] | None = None, settle: float = 0.0) -> Any:
    boxes = [ImapSettings("h", "u", Path("p"))]
    return rrc.poll_mailboxes(
        boxes,
        wanted_ids,
        [wanted_ids],
        len(wanted_ids),
        30.0,
        settle,
        count=count,
        sleep=(waits if waits is not None else []).append,
    )


def test_one_more_poll_after_everything_was_seen_counts_a_late_second_delivery() -> None:
    first = [{"INBOX": {"L1": 1, "L2": 1}}]
    later = [{"INBOX": {"L1": 2, "L2": 1}}]  # L1 arrives a second time one poll later
    waits: list[float] = []
    got, logins, timed_out = _poll(_sequence(first, later), {"L1", "L2"}, waits, settle=7.0)
    assert got == later and logins == 2 and timed_out is False and waits == [7.0]  # the settle delay, then one more


def test_a_total_time_limit_after_a_full_poll_keeps_the_earlier_counts(capsys: pytest.CaptureFixture[str]) -> None:
    from lead_desk.imap_count import ImapTotalTimeout

    full = [{"INBOX": {"L1": 1}}]
    got, _, timed_out = _poll(_sequence(full, ImapTotalTimeout("x")), {"L1", "L2"})
    assert got == full and timed_out is True and "last poll hit the total time limit" in capsys.readouterr().out
    got2, _, timed_out2 = _poll(_sequence(ImapTotalTimeout("x")), {"L1"})
    assert got2 == [{}] and timed_out2 is True  # nothing to keep: the failure is reported, not hidden


def test_the_expected_keys_of_a_mailbox_follow_the_senders_recipient_hash() -> None:
    from lead_desk.smtp_provider import ProviderSettings, SmtpProviderOutbox

    pr = ProviderSettings("h", 587, "u", Path("p"), "f@example.test", ("a@example.test", "b@example.test"), 1)
    boxes = [ImapSettings("h", "a@example.test", Path("p")), ImapSettings("h", "B@example.test", Path("p"))]
    ids = {f"L-{n:03d}" for n in range(30)}
    got = rrc.expected_keys_by_inbox(ids, pr, boxes)
    assert got[0] | got[1] == ids and not got[0] & got[1] and got[0] and got[1]
    out = SmtpProviderOutbox(pr)
    assert all(out.recipient_for(k) == "a@example.test" for k in got[0])


def test_an_unknown_that_is_not_an_in_flight_row_at_the_kill_fails_the_floor() -> None:
    """The allowance comes from the follow-ups in state `sending` at the kill, not from a constant."""
    sent = {f"S{i}": ("sent", 1, "") for i in range(7)}
    book = _book({**sent, "U0": ("unknown", 1, rrc.CRASH_REASON)})
    assert _floors(book, 8, settled=4, mails=7, crm=8, sent=8, sending=1)["F1"]["ok"] is True  # it was in flight
    assert _floors(book, 8, settled=4, mails=7, crm=8, sent=8, sending=0)["F1"]["ok"] is False  # nothing was
    assert rrc.unknown_allowance(0) == 0 and rrc.unknown_allowance(2) == 2
    from lead_desk.gate import SEND_WORKERS_MAX

    assert rrc.unknown_allowance(SEND_WORKERS_MAX + 5) == SEND_WORKERS_MAX


def test_the_sending_rows_are_read_from_the_book(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "b.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE receipts (receipt_id INTEGER, source TEXT, source_ref TEXT, outcome TEXT)")
    c.execute("CREATE TABLE leads (lead_id TEXT, email_key TEXT, email TEXT, name TEXT)")
    c.execute("CREATE TABLE followups (lead_id TEXT, state TEXT, crm_done INT, updated_at INT, last_error TEXT)")
    rows = [("a", "sending", 1, 0, ""), ("b", "sent", 1, 0, ""), ("c", "sending", 0, 0, "")]
    c.executemany("INSERT INTO followups VALUES (?,?,?,?,?)", rows)
    c.commit()
    c.close()
    assert rrc.sending_rows(db) == 2
    assert rrc.sending_rows(tmp_path / "missing.db") == 0


def test_f4_uses_the_larger_of_the_desk_counter_and_the_mails_counted() -> None:
    book = _book({"S1": ("sent", 1, ""), "S2": ("sent", 1, "")})
    assert _floors(book, 2, mails=2, crm=2, sent=2)["F4"]["ok"] is True
    # the counter lost a count (0), but the mailboxes hold 3 mails for 2 leads: a double send must not hide
    assert _floors(book, 2, mails=3, crm=2, sent=0)["F4"]["ok"] is False


def test_f3_says_in_its_output_that_it_relies_on_list_after_create() -> None:
    book = _book({"S1": ("sent", 1, "")})
    assert "UNVERIFIED" in _floors(book, 1, mails=1, crm=1, sent=1)["F3"]["detail"]


# -- config names, mailbox polling, read-back errors and the banner ----------------------------------------------------


def test_a_lower_case_setting_name_is_refused_and_never_reaches_a_live_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _valid_config(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\ndesk_qualifier=live_claude\n", encoding="utf-8")
    started: list[str] = []
    monkeypatch.setattr(rrc.Desk, "start", lambda self: started.append("desk"))
    monkeypatch.setattr("builtins.input", lambda prompt="": started.append("asked") or "y")
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_REFUSED
    assert "upper-case" in capsys.readouterr().out and started == []
    # the second line of defence: even a dict that holds the name never passes it on
    child = rrc.child_environment({"desk_qualifier": "live_claude", "DESK_PORT": "1"})
    assert "desk_qualifier" not in child and "DESK_QUALIFIER" not in {k.upper() for k in child if k != "DESK_PORT"}


def test_an_existing_database_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = tmp_path / "old.db"
    old.write_bytes(b"x")
    assert rrc.main(["--config", str(_valid_config(tmp_path, DESK_DB_PATH=str(old)))]) == rrc.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "already exists" in out and "Start?" not in out


def test_the_banner_shows_the_mailbox_reading_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert rrc.main(["--config", str(_valid_config(tmp_path))]) == rrc.EXIT_STOPPED
    out = capsys.readouterr().out
    assert f"{rrc.IMAP_LOGINS_MAX + rrc.POLL_EXTRA_LOGINS} logins x 2 mailboxes" in out


def test_the_banner_shows_the_live_run_cap_and_the_live_host_is_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live = {
        "DESK_LIVE_MODEL": "model-x",
        "DESK_LIVE_KEY_FILE": str(tmp_path / "k"),
        "DESK_LIVE_PRICE_IN_PER_MTOK": "1",
        "DESK_LIVE_PRICE_OUT_PER_MTOK": "5",
        "DESK_LIVE_PRICE_DATE": "2026-01-01",
        "DESK_LIVE_CAP_RUN_USD": "1.25",
        "DESK_LIVE_CAP_TOTAL_USD": "3",
        "DESK_LIVE_LEDGER_PATH": str(tmp_path / "ledger"),
        "DESK_LIVE_RUN_ID": "r1",
    }
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert rrc.main(["--config", str(_valid_config(tmp_path, **live)), "--live-model"]) == rrc.EXIT_STOPPED
    assert "per-run cap US$1.25" in capsys.readouterr().out
    other = "https://" + "model-host" + ".example.test"
    cfg = _valid_config(tmp_path, DESK_LIVE_BASE_URL=other, **live)
    assert rrc.main(["--config", str(cfg), "--live-model"]) == rrc.EXIT_REFUSED  # a stranger's host: no key goes there
    assert "not the provider's own address" in capsys.readouterr().out
    # the host check lives in the settings themselves (tests/test_live_claude.py); here only that the script
    # cannot be talked past it: a process variable cannot switch it off, only the config file can
    monkeypatch.setenv("DESK_LIVE_ALLOW_ANY_BASE_URL", "yes")
    assert rrc.main(["--config", str(cfg), "--live-model"]) == rrc.EXIT_REFUSED
    assert "not the provider's own address" in capsys.readouterr().out


def test_a_mailbox_user_without_a_password_file_and_a_bad_folder_are_clear_refusals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="REAL_IMAP_2_PASSWORD_FILE is required"):
        rrc.imap_settings({"REAL_IMAP_1_USER": "u", "REAL_IMAP_1_PASSWORD_FILE": "p", "REAL_IMAP_2_USER": "v"})
    base = {"REAL_IMAP_1_USER": "u", "REAL_IMAP_1_PASSWORD_FILE": "p"}
    for folder in ("Spamfölder", 'a"b', "a" + chr(92) + "b"):
        with pytest.raises(ValueError, match="REAL_IMAP_1_FOLDERS"):
            rrc.imap_settings({**base, "REAL_IMAP_1_FOLDERS": folder})
    cfg = _valid_config(tmp_path, REAL_IMAP_1_FOLDERS="Spamfölder")
    assert rrc.main(["--config", str(cfg)]) == rrc.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "REAL_IMAP_1_FOLDERS" in out and "Spamf" not in out and "Traceback" not in out


def test_dry_fakes_with_a_config_says_the_config_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_fakes(*_a: Any, **_k: Any) -> Any:
        raise ValueError("stop here")

    monkeypatch.setattr(rrc, "DryFakes", no_fakes)
    assert rrc.main(["--dry-fakes", "--assume-yes", "--config", str(tmp_path / "c.env")]) == rrc.EXIT_REFUSED
    assert "--config is ignored" in capsys.readouterr().out


def test_a_failed_late_poll_sets_the_flag_and_fails_the_floor(capsys: pytest.CaptureFixture[str]) -> None:
    from lead_desk.imap_count import ImapCountError

    first = [{"INBOX": {"L1": 1, "L2": 1}}]
    got, _, failed = _poll(_sequence(first, ImapCountError("x")), {"L1", "L2"}, settle=0.0)
    assert got == first and failed is True and "mailbox read failed" in capsys.readouterr().out
    _, _, ok_failed = _poll(_sequence(first, first), {"L1", "L2"})
    assert ok_failed is False
    book = _book({f"S{i}": ("sent", 1, "") for i in range(4)})
    assert _floors(book, 4, mails=4, crm=4, sent=4, poll_failed=True)["F6"]["ok"] is False
    assert _floors(book, 4, mails=4, crm=4, sent=4)["F6"]["ok"] is True


def test_the_slow_scan_runs_on_the_first_and_the_last_poll_only() -> None:
    seen: list[bool] = []
    answers = [[{"INBOX": {}}], [{"INBOX": {}}], [{"INBOX": {"L1": 1}}], [{"INBOX": {"L1": 1}}]]

    def count(*_a: Any, **k: Any) -> Any:
        seen.append(k["scan"])
        return answers[len(seen) - 1]

    _poll(count, {"L1"})
    assert seen == [True, False, False, True]  # first, two waiting polls without the scan, then the last one


def test_a_poll_never_lowers_a_count_an_earlier_poll_saw() -> None:
    both = [{"INBOX": {"L1": 1, "L2": 1}}]
    answers = [[{"INBOX": {"L1": 1}}], [{"INBOX": {}}], both, both]
    got, _, _ = _poll(_sequence(*answers), {"L1", "L2"})
    assert got == both
    merged = rrc.merge_counts([{"INBOX": {"L1": 2}}], [{"INBOX": {"L1": 1}, "Spam": {"L9": 1}}])
    assert merged == [{"INBOX": {"L1": 2}, "Spam": {"L9": 1}}]


def test_only_sent_follow_ups_end_the_wait_and_a_failed_leads_mail_does_not() -> None:
    both = [{"INBOX": {"L1": 1, "L2": 1}}]
    answers = [[{"INBOX": {"L2": 1}}], both, both]  # first: only the mail of a lead that did not end `sent`
    boxes = [ImapSettings("h", "u", Path("p"))]
    got, logins, failed = rrc.poll_mailboxes(
        boxes, {"L1", "L2"}, [{"L1"}], 1, 30.0, 0.0, count=_sequence(*answers), sleep=lambda _s: None, sent_ids={"L1"}
    )
    assert logins == 3 and failed is False and got == both


def test_a_first_poll_that_runs_out_of_time_is_tried_once_more_with_more_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lead_desk.imap_count import ImapTotalTimeout

    totals: list[float] = []
    full = [{"INBOX": {"L1": 1}}]
    results = iter([ImapTotalTimeout("x"), full, full])

    def count(boxes: Any, *_a: Any, **_k: Any) -> Any:
        totals.append(boxes[0].total_seconds)
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    boxes = [ImapSettings("h", "u", Path("p"), total_seconds=10.0)]
    got, logins, failed = rrc.poll_mailboxes(boxes, {"L1"}, [{"L1"}], 1, 30.0, 0.0, count=count, sleep=lambda _s: None)
    assert got == full and failed is False and logins == 3
    assert totals == [10.0, 20.0, 10.0]  # the doubled limit is for the one retried poll only
    assert "trying once more with more time" in capsys.readouterr().out


def test_a_failed_crm_read_back_ends_with_counts_a_result_file_and_a_clean_up_note(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lead_desk.adapters import CrmNothingStored

    class Broken:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def rows_with_prefix(self, _prefix: str) -> Any:
            raise CrmNothingStored("the hubspot call cap is used up")

    monkeypatch.setattr(rrc, "HubspotCrm", Broken)
    code = rrc.main(["--dry-fakes", "--assume-yes", "--cleanup"])
    out = capsys.readouterr().out
    assert code == rrc.EXIT_FAILED and "Traceback" not in out
    assert "read-back failed" in out and "NOT counted" in out and "NOT archived" in out and "by hand" in out
    path = Path(out.split("result file: ")[1].splitlines()[0])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is False and "may still be in HubSpot" in data["note"]


# -- made-up lead addresses (example.com is the reserved example domain; `.test` may be refused by a real CRM) -------


def test_made_up_lead_addresses_use_the_reserved_example_domain_and_are_never_recipients() -> None:
    assert rrc.EMAIL_DOMAIN == "example.com"
    plan = rrc.make_plan("tagx", 8)
    emails = {it["envelope"]["payload"]["email"] for part in plan.values() for it in part}
    assert emails and all(e.endswith("@example.com") for e in emails), emails
    recipients = {
        "inbox-a@example.test",
        "inbox-b@example.test",
    }  # what the dry run configures; the real run uses its own
    assert emails.isdisjoint(recipients)


def test_the_banner_names_the_domain_from_the_constant(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rrc, "EMAIL_DOMAIN", "altdomain")

    def no_terminal(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert rrc.main(["--dry-fakes"]) == rrc.EXIT_STOPPED
    out = capsys.readouterr().out
    assert "addresses at altdomain" in out and "example.test" not in out


def test_one_dropped_connection_on_the_last_poll_does_not_fail_the_last_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One mailbox connection dropped on the LAST poll does not fail the read of the mail counts: that poll is
    repeated once after a pause; two drops in a row fail."""
    from lead_desk.imap_count import ImapConnectionError

    full = [{"INBOX": {"L1": 1}}]
    waits: list[float] = []
    stats: dict[str, int] = {}
    boxes = [ImapSettings("h", "u", Path("p"))]
    got, logins, failed = rrc.poll_mailboxes(
        boxes,
        {"L1"},
        [{"L1"}],
        1,
        30.0,
        0.0,
        count=_sequence([{"INBOX": {}}], full, ImapConnectionError("x"), full),
        sleep=waits.append,
        stats=stats,
    )
    assert got == full and failed is False and logins == 4
    assert stats == {"poll_repeats": 1} and rrc.REPEAT_POLL_SECONDS in waits  # a short pause, via the injected sleep
    assert "trying that poll once more" in capsys.readouterr().out
    _, _, still_failed = _poll(_sequence(full, ImapConnectionError("x"), ImapConnectionError("x")), {"L1"})
    assert still_failed is True  # a second failure in a row is a real failure


def test_a_dropped_first_poll_connection_is_repeated_too() -> None:
    from lead_desk.imap_count import ImapConnectionError

    full = [{"INBOX": {"L1": 1}}]
    got, _, failed = _poll(_sequence(ImapConnectionError("x"), full, full), {"L1"})
    assert got == full and failed is False


def test_a_login_or_folder_error_is_never_repeated() -> None:
    """A wrong password repeated at once is a lockout risk: only connection-class errors get a second try."""
    from lead_desk.imap_count import ImapCountError

    full = [{"INBOX": {"L1": 1}}]
    calls: list[int] = []
    inner = _sequence(full, ImapCountError("login"), full)

    def count(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return inner(*a, **k)

    _, logins, failed = _poll(count, {"L1"})
    assert failed is True and logins == 2 and len(calls) == 2  # no repeat


def test_the_imap_module_marks_only_connection_errors_as_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imaplib

    from lead_desk import imap_count
    from lead_desk.imap_count import ImapConnectionError, ImapCountError

    pw = tmp_path / "pw.txt"
    pw.write_text("pw-" + "for-" + "tests-" + "only\n", encoding="utf-8")
    box = ImapSettings("h", "u", pw)
    cases = ((OSError("reset"), True), (imaplib.IMAP4.abort("x"), True), (imaplib.IMAP4.error("NO"), False))
    for raised, want_connection in cases:

        def boom(*_a: Any, _e: BaseException = raised, **_k: Any) -> Any:
            raise _e

        monkeypatch.setattr(imaplib, "IMAP4_SSL", boom)
        with pytest.raises(ImapCountError) as e:
            imap_count.counts_by_key(box, "L")
        assert isinstance(e.value, ImapConnectionError) is want_connection


def test_the_repeat_count_is_in_the_f6_details() -> None:
    book = _book({f"S{i}": ("sent", 1, "") for i in range(4)})
    per_lead: Counter[str] = Counter({f"M{i}": 1 for i in range(4)})
    pairs = [(f"L{i}", str(i)) for i in range(4)]
    checks = rrc.floor_checks(book, 4, 0, per_lead, pairs, {"attempts": 4, "sent": 4}, 137, 99, False, 1)
    f6 = next(c for c in checks if c["id"] == "F6")
    assert f6["values"]["poll_repeats"] == 1


class _StubImap:
    """A stand-in for imaplib.IMAP4_SSL: login and select can be made to fail."""

    def __init__(self, login: BaseException | None = None, select: Any = None) -> None:
        self._login, self._select = login, select

    def login(self, *_a: Any) -> None:
        if self._login:
            raise self._login

    def select(self, *_a: Any, **_k: Any) -> Any:
        raise self._select() if callable(self._select) else self._select

    def logout(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


@pytest.mark.parametrize(
    ("make", "repeatable"),
    [
        (lambda: _StubImap(login=__import__("imaplib").IMAP4.abort("EOF")), False),  # a hang-up in the login
        (lambda: _StubImap(select=OSError("reset")), True),  # a reset after a good login
        (lambda: _StubImap(select=TimeoutError()), False),  # a step that timed out
    ],
)
def test_only_a_reset_after_a_good_login_is_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Any, repeatable: bool
) -> None:
    import imaplib

    from lead_desk import imap_count
    from lead_desk.imap_count import ImapConnectionError, ImapCountError

    pw = tmp_path / "pw.txt"
    pw.write_text("pw-" + "for-" + "tests-" + "only\n", encoding="utf-8")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *_a, **_k: make())
    with pytest.raises(ImapCountError) as e:
        imap_count.counts_by_key(ImapSettings("h", "u", pw), "L")
    assert isinstance(e.value, ImapConnectionError) is repeatable


def test_a_failure_after_the_total_time_limit_is_not_repeated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imaplib
    from types import SimpleNamespace

    from lead_desk import imap_count
    from lead_desk.imap_count import ImapConnectionError, ImapCountError

    now = [0.0]

    def late_reset() -> BaseException:
        now[0] = 1000.0  # the time limit (10 s) passes during this step
        return OSError("reset")

    pw = tmp_path / "pw.txt"
    pw.write_text("pw-" + "for-" + "tests-" + "only\n", encoding="utf-8")
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *_a, **_k: _StubImap(select=late_reset))
    monkeypatch.setattr(imap_count, "time", SimpleNamespace(monotonic=lambda: now[0]))
    with pytest.raises(ImapCountError) as e:
        imap_count.counts_by_key(ImapSettings("h", "u", pw, total_seconds=10.0), "L")
    assert not isinstance(e.value, ImapConnectionError)


def test_drain_is_measured_from_the_real_send_not_from_plan_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class: a timing check measured from the wrong origin. The plan is built at t=0; the settle, crash wait and
    restart take 500 s on a loaded box; the last re-send happens at t=500 and the book drains 20 s later. With the
    plan-creation stamp the check read 520 s and failed the 90 s ceiling; from the real send it is 20 s."""
    from load_tools import check_invariants as ci

    clock = [0.0]
    monkeypatch.setattr(rrc, "epoch_now", lambda: clock[0])
    plan = rrc.make_plan("tg", 12)
    item = plan["after"][0]
    sent: list[float] = []

    def fake_send(url: str, it: dict[str, Any]) -> dict[str, Any]:
        sent.append(it["intended_ts"])
        return {"intended_ts": it["intended_ts"], "final_status": 201}

    monkeypatch.setattr(rrc, "send_with_retries", fake_send)
    clock[0] = 500.0  # earlier phases took this long
    line = rrc.send_item("http://127.0.0.1:1/leads", item)
    assert sent == [500.0] and line["intended_ts"] == 500.0
    book = {"followups": {"L1": ("sent", 1, 520)}}
    assert ci.check_drain([line], book, None, 90.0)["ok"] is True
    stale = {**line, "intended_ts": item["intended_ts"]}  # the old behaviour: the stamp from make_plan
    assert ci.check_drain([stale], book, None, 90.0)["ok"] is False


def test_made_up_addresses_that_may_reach_a_real_crm_do_not_use_the_test_domain() -> None:
    """The free HubSpot refused a create for a `.test` address (probe 2026-11-11: 400 on a .test address, 201 on
    example.com), so every made-up lead address a check script builds ends in example.com."""
    import run_live_check as rlc

    assert rrc.EMAIL_DOMAIN == "example.com"
    mails = [m for _, m in rlc._emails(3)]
    assert mails and all(m.endswith("@example.com") for m in mails)
    for name in ("run_live_check.py", "run_real_check.py"):
        text = (Path(__file__).resolve().parents[1] / "scripts" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "made" in line and ".up." in line:
                assert ".test" not in line, line


def test_the_dry_run_waits_up_to_240_seconds_for_the_recovery_drain_and_the_real_default_stays_150(
    tmp_path: Path,
) -> None:
    """The dry run proves logic, not speed: its drain ceiling is 240 s (the drain took 44 s idle and 114 s under four
    heavy jobs), and the real run keeps its own default of 150 s."""
    try:
        fakes = rrc.DryFakes(tmp_path)
    except ValueError:
        pytest.skip("the openssl program is needed for the dry fakes")
    try:
        assert fakes.env["REAL_DRAIN_SECONDS"] == "240"
    finally:
        fakes.close()
    assert rrc.positive_seconds("REAL_DRAIN_SECONDS", {}, 150.0) == 150.0  # absent: the real default
