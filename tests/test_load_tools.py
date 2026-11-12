"""Tests for load_tools: the seven checks, the load driver, the fake model server call log.
Made-up data, loopback only, no real model."""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from support import FakeClock

from demo_only import fake_model_server
from lead_desk.constants import FOLLOWUP_BODY_TEMPLATE, FOLLOWUP_SUBJECT_TEMPLATE
from load_tools import check_invariants as ci
from load_tools import load_driver

SCHEMA = Path(__file__).resolve().parent.parent / "src" / "lead_desk" / "schema.sql"
T0 = 1_000_000.0
RATE_20_FOR_3 = "rate:" + "20" + ":3"
RATE_10_FOR_3 = "rate:" + "10" + ":3"

# ---------- check_invariants: hand-built fixture ----------
# leads L1..L4 sent (mail + CRM), L5 failed (CRM done, no mail), L6 unknown (CRM intent only, no mail, no CRM row)
LEADS = {
    "L1": ("sent", 1),
    "L2": ("sent", 1),
    "L3": ("sent", 1),
    "L4": ("sent", 1),
    "L5": ("failed", 1),
    "L6": ("unknown", 2),
}


def make_book(path: Path, leads: dict[str, tuple[str, int]], updated: int = int(T0) + 5) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    for n, (lid, (state, done)) in enumerate(leads.items(), 1):
        conn.execute(
            "INSERT INTO receipts(source, source_ref, received_at, payload, outcome, outcome_lead_id, decided_at)"
            " VALUES ('form', ?, ?, '{}', 'new', ?, ?)",
            (f"r-{lid}", int(T0), lid, int(T0)),
        )
        conn.execute(
            "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at)"
            " VALUES (?, ?, 'form', 'Ada', ?, ?, ?)",
            (lid, n, f"{lid}@example.test", f"{lid}@example.test".lower(), int(T0)),
        )
        conn.execute(
            "INSERT INTO followups(lead_id, state, crm_done, updated_at, last_error) VALUES (?, ?, ?, ?, ?)",
            (lid, state, done, updated, "refused" if state in ("failed", "unknown") else None),
        )
    # a repeat of request r-L1 through the other door: a second receipt, a duplicate verdict, no lead
    conn.execute(
        "INSERT INTO receipts(source, source_ref, received_at, payload, outcome, outcome_lead_id, differing_fields,"
        " decided_at) VALUES ('webhook', 'r-L1', ?, '{}', 'duplicate', 'L1', '[]', ?)",
        (int(T0), int(T0)),
    )
    conn.commit()
    conn.close()


def make_world() -> dict[str, Any]:
    reqs: list[dict[str, Any]] = []
    for n, lid in enumerate(LEADS):
        reqs.append(
            {
                "request_id": f"r-{lid}",
                "door": "form",
                "intended_ts": T0 + n,
                "final_status": 201,
                "receipt_id": n + 1,
                "attempts": [{"ts": T0 + n, "code": 201, "error": None}],
            }
        )
    reqs.append(
        {
            "request_id": "r-L1",
            "door": "webhook",
            "intended_ts": T0,
            "final_status": 200,
            "receipt_id": len(LEADS) + 1,
            "attempts": [{"ts": T0, "code": 503, "error": None}, {"ts": T0 + 0.1, "code": 200, "error": None}],
        }
    )
    mail = [
        {
            "ts": T0,
            "send_key": k,
            "message_id": f"m-{k}",
            "to": f"{k}@example.test",
            "subject": FOLLOWUP_SUBJECT_TEMPLATE.format(name="Ada"),
            "body_sha256": hashlib.sha256(FOLLOWUP_BODY_TEMPLATE.format(name="Ada").encode()).hexdigest(),
            "mode": "keyed",
        }
        for k in ("L1", "L2", "L3", "L4")
    ]
    crm = [
        {"lead_id": k, "record": {"lead_id": k, "email": f"{k}@example.test"}} for k in ("L1", "L2", "L3", "L4", "L5")
    ]
    model = [{"ts": T0, "lead_id": k, "mode": "ok", "status": 200} for k in LEADS]
    model.append({"ts": T0, "lead_id": "L2", "mode": "rate_limit", "status": 429})
    return {"reqs": reqs, "mail": mail, "crm": crm, "model": model, "leads": dict(LEADS), "drained_ts": T0 + 8.0}


def run_world(tmp_path: Path, w: dict[str, Any], **kw: Any) -> dict[str, Any]:
    db = tmp_path / "desk.sqlite3"
    if db.exists():
        db.unlink()
    make_book(db, w["leads"])
    book = ci.read_book(db)
    kw.setdefault("max_failed", 1)  # the fixture has one failed lead on purpose
    kw.setdefault("max_unknown", 1)  # and one unknown lead
    return ci.run_checks(w["reqs"], book, w["mail"], w["crm"], w["model"], w["drained_ts"], **kw)


def failed(result: dict[str, Any]) -> list[int]:
    return [c["id"] for c in result["checks"] if not c["ok"]]


def test_clean_case_passes(tmp_path: Path) -> None:
    result = run_world(tmp_path, make_world())
    assert failed(result) == []
    assert result["ok"] is True
    assert [c["id"] for c in result["checks"]] == [1, 2, 3, 4, 5, 6, 7]
    assert result["checks"][5]["values"]["responses_503"] == 1


def fault_lost_receipt(w: dict[str, Any]) -> None:
    w["reqs"].append({"request_id": "r-gone", "door": "form", "intended_ts": T0, "final_status": 503, "attempts": []})


def fault_double_mail(w: dict[str, Any]) -> None:
    w["mail"].append(dict(w["mail"][0], message_id="m-again"))


def fault_unknown_with_mail(w: dict[str, Any]) -> None:
    w["mail"].append(dict(w["mail"][0], send_key="L6", message_id="m-L6", to="L6@example.test"))


def fault_crm_missing(w: dict[str, Any]) -> None:
    w["crm"] = [r for r in w["crm"] if r["lead_id"] != "L3"]


def fault_five_model_calls(w: dict[str, Any]) -> None:
    w["model"] += [{"ts": T0, "lead_id": "L4", "mode": "ok", "status": 200}] * 4


def fault_pending_left(w: dict[str, Any]) -> None:
    w["leads"]["L4"] = ("pending", 1)
    w["mail"] = [m for m in w["mail"] if m["send_key"] != "L4"]


def fault_sent_without_mail(w: dict[str, Any]) -> None:
    w["mail"] = [m for m in w["mail"] if m["send_key"] != "L2"]


def fault_crm_extra(w: dict[str, Any]) -> None:
    w["crm"].append({"lead_id": "L5-ghost", "record": {}})


@pytest.mark.parametrize(
    ("fault", "check_id", "offender"),
    [
        (fault_lost_receipt, 1, "form:r-gone"),
        (fault_double_mail, 3, "L1"),
        (fault_sent_without_mail, 3, "L2"),
        (fault_crm_missing, 4, "L3"),
        (fault_crm_extra, 4, "L5-ghost"),
        (fault_five_model_calls, 5, "L4"),
        (fault_pending_left, 2, "L4"),
    ],
)
def test_planted_fault_fails_one_check(tmp_path: Path, fault: Any, check_id: int, offender: str) -> None:
    w = make_world()
    fault(w)
    result = run_world(tmp_path, w)
    assert failed(result) == [check_id]
    assert offender in result["checks"][check_id - 1]["offenders"]
    assert result["ok"] is False


def fault_wrong_recipient(w: dict[str, Any]) -> None:
    w["mail"][0] = dict(w["mail"][0], to="someone-else@example.test")


def fault_unreadable_message(w: dict[str, Any]) -> None:
    w["mail"].append({"ts": T0, "unparseable": True, "send_key": "", "to": "", "mode": "ok"})


@pytest.mark.parametrize(
    ("fault", "offender"),
    [(fault_wrong_recipient, "wrong-recipient:L1"), (fault_unreadable_message, "unparseable-message")],
)
def test_mail_that_cannot_be_matched_fails_check_3(tmp_path: Path, fault: Any, offender: str) -> None:
    w = make_world()
    fault(w)
    result = run_world(tmp_path, w)
    assert failed(result) == [3]
    assert offender in result["checks"][2]["offenders"]


def test_all_unknown_book_fails_unless_allowed(tmp_path: Path) -> None:
    # a run whose every follow-up ended `unknown` (no mail, no CRM row) proves nothing
    w = _total_loss_world({f"L{n}": ("unknown", 2) for n in range(1, 7)})
    assert failed(run_world(tmp_path, w, max_unknown=0)) == [2]
    assert failed(run_world(tmp_path, w, max_unknown=5)) == [2]
    assert failed(run_world(tmp_path, w, max_unknown=6)) == [2]  # no allowance lets an all-unknown book through
    assert failed(run_world(tmp_path, w, max_unknown=60)) == [2]
    assert failed(run_world(tmp_path, make_world(), max_unknown=1)) == []  # the fixture has exactly one unknown row


def test_unknown_row_present_is_allowed(tmp_path: Path) -> None:
    w = make_world()
    w["crm"].append({"lead_id": "L6", "record": {}})  # the put may have landed: a listed unknown
    assert failed(run_world(tmp_path, w)) == []


def test_503_ceiling_and_drain_ceiling(tmp_path: Path) -> None:
    w = make_world()
    assert failed(run_world(tmp_path, w, max_503_rate=0.01)) == [6]  # 1 of 8 attempts
    assert failed(run_world(tmp_path, w, max_503_rate=0.5)) == []
    assert failed(run_world(tmp_path, w, max_drain_seconds=1.0)) == [7]  # last intended send T0+5, drained T0+8


def _drain(requests_at: float, book_at: int, drained_ts: float | None = None) -> dict[str, Any]:
    book = {"followups": {"L1": ("sent", 1, book_at)}}
    return ci.check_drain([{"intended_ts": requests_at}], book, drained_ts, 120.0)


def test_a_book_stamped_before_the_first_send_is_its_own_failure() -> None:
    """A clock stepped back, or files from different runs: the drain moment is before the FIRST send (the check compares
    with the first send only, so a backward step smaller than the run length goes undetected). It must be a
    failure with its own line, not a value clamped to 0 s that passes."""
    res = _drain(requests_at=1_700_003_600.0, book_at=1_700_000_000)
    assert res["ok"] is False and "BEFORE the first intended send" in res["detail"]
    assert res["values"]["drain_seconds"] < -3000


def test_whole_second_stamps_may_sit_up_to_one_second_before_the_last_send() -> None:
    """The book cuts its stamps to whole seconds: 100.7 s sent and a stamp of 100 is a normal, instant drain."""
    res = _drain(requests_at=1_700_000_100.7, book_at=1_700_000_100)
    assert res["ok"] is True and res["values"]["drain_seconds"] == 0.0
    assert _drain(requests_at=1_700_000_101.2, book_at=1_700_000_100)["ok"] is False  # 1.2 s before: not rounding


def test_a_schedule_that_ends_with_a_repeat_is_a_normal_drain() -> None:
    """The last send made no follow-up row (a repeat): the last row change is many seconds before the last send."""
    book = {"followups": {"L1": ("sent", 1, 1_700_000_002)}}
    reqs = [{"intended_ts": 1_700_000_000.0}, {"intended_ts": 1_700_000_030.0}]
    res = ci.check_drain(reqs, book, None, 120.0)
    assert res["ok"] is True and res["values"]["drain_seconds"] == 0.0


def test_drain_falls_back_to_the_book(tmp_path: Path) -> None:
    w = make_world()
    w["drained_ts"] = None
    result = run_world(tmp_path, w)
    assert result["checks"][6]["values"]["source"].startswith("book")
    assert failed(result) == []


def test_offenders_capped_at_ten(tmp_path: Path) -> None:
    w = make_world()
    w["reqs"] += [{"request_id": f"x{i}", "door": "form", "intended_ts": T0, "final_status": 503} for i in range(25)]
    c = run_world(tmp_path, w)["checks"][0]
    assert c["offender_count"] == 25
    assert len(c["offenders"]) == ci.FIRST_N


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    w = make_world()
    make_book(tmp_path / "desk.sqlite3", w["leads"])

    def write(name: str, rows: list[dict[str, Any]]) -> Path:
        p = tmp_path / name
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    crm = tmp_path / "crm.json"
    crm.write_text(json.dumps({"count": len(w["crm"]), "rows": w["crm"]}), encoding="utf-8")
    drain = tmp_path / "drain.json"
    drain.write_text(json.dumps({"drained_ts": w["drained_ts"]}), encoding="utf-8")
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({"intended_requests": len(w["reqs"])}), encoding="utf-8")
    argv = [
        "--requests", str(write("req.jsonl", w["reqs"])),
        "--book", str(tmp_path / "desk.sqlite3"),
        "--catcher", str(write("mail.jsonl", w["mail"])),
        "--crm", str(crm),
        "--model-log", str(write("model.jsonl", w["model"])),
        "--drain", str(drain),
        "--facts", str(facts),
        "--json", str(tmp_path / "out.json"),
        "--max-failed", "1",
        "--max-unknown", "1",
    ]  # fmt: skip
    assert ci.main(argv) == ci.EXIT_OK
    assert "ALL 7 CHECKS HOLD" in capsys.readouterr().out
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["ok"] is True
    # a live AI run must show a model call for every sent lead, unless that is switched off on purpose
    no_call = deepcopy(argv)
    no_call[no_call.index("--model-log") + 1] = str(
        write("model2.jsonl", [m for m in w["model"] if m["lead_id"] != "L3"])
    )
    monkeypatch.delenv("DESK_QUALIFIER", raising=False)
    assert ci.main(no_call) == ci.EXIT_OK
    monkeypatch.setenv("DESK_QUALIFIER", "live_claude")
    assert ci.main(no_call) == ci.EXIT_FAIL
    assert ci.main([*no_call, "--no-model-required"]) == ci.EXIT_OK
    monkeypatch.delenv("DESK_QUALIFIER")
    facts.write_text(json.dumps({"intended_requests": len(w["reqs"]), "qualifier": "live_claude"}), encoding="utf-8")
    assert ci.main(no_call) == ci.EXIT_FAIL  # the driver's facts say the AI step was live: no env variable needed
    facts.write_text(json.dumps({"intended_requests": len(w["reqs"])}), encoding="utf-8")
    # a catcher log without recipients cannot be compared: the command (fake and dry runs) fails it by default
    blank = deepcopy(argv)
    blank[blank.index("--catcher") + 1] = str(write("mail3.jsonl", [{**m, "to": ""} for m in w["mail"]]))
    assert ci.main(blank) == ci.EXIT_FAIL
    assert ci.main([*blank, "--no-require-comparable"]) == ci.EXIT_OK  # a real mailbox log, switched off on purpose
    bad = deepcopy(argv)
    bad[bad.index("--catcher") + 1] = str(write("mail2.jsonl", w["mail"] + [w["mail"][0]]))
    assert ci.main(bad) == ci.EXIT_FAIL
    bad[bad.index("--catcher") + 1] = str(tmp_path / "missing.jsonl")
    assert ci.main(bad) == ci.EXIT_INPUT
    short = deepcopy(argv)
    facts.write_text(json.dumps({"intended_requests": len(w["reqs"]) + 1}), encoding="utf-8")
    assert ci.main(short) == ci.EXIT_FAIL  # one intended request has no line in the log
    facts.unlink()
    assert ci.main(short) == ci.EXIT_INPUT  # no facts file: the run cannot be compared, so it cannot pass


def test_two_messages_to_one_address_fail_check_3_whatever_their_keys(tmp_path: Path) -> None:
    w = make_world()
    w["mail"][1]["to"] = w["mail"][0]["to"]  # L2's mail went to L1's address too
    result = run_world(tmp_path, w)
    assert 3 in failed(result)
    offenders = result["checks"][2]["offenders"]
    assert "address-shared:L1" in offenders and "address-shared:L2" in offenders
    assert not any("@" in o for o in offenders)  # the key is named, never the address


def test_a_message_text_that_is_not_the_followup_text_fails_check_3(tmp_path: Path) -> None:
    w = make_world()
    w["mail"][2]["body_sha256"] = hashlib.sha256(b"some other text").hexdigest()
    assert "wrong-body:L3" in run_world(tmp_path, w)["checks"][2]["offenders"]
    w2 = make_world()
    del w2["mail"][2]["body_sha256"]  # no recorded hash: nothing to compare, nothing failed
    assert failed(run_world(tmp_path, w2)) == []


def test_a_follow_up_text_that_differs_from_the_constant_is_a_wrong_text(tmp_path: Path) -> None:
    """A fixture that hard-codes a different text makes a healthy book fail check 3: the checker is right to
    refuse it, so fixtures must build the text from the constant."""
    other_text = "Hi Ada, we received your message. (OTHER TEXT)"
    w = make_world()
    w["mail"][2]["body_sha256"] = hashlib.sha256(other_text.encode()).hexdigest()
    res = run_world(tmp_path, w)
    assert failed(res) == [3] and "wrong-body:L3" in res["checks"][2]["offenders"]
    assert failed(run_world(tmp_path, make_world())) == []  # the same world with the real text is healthy


def test_the_checkers_followup_text_is_the_desks_text() -> None:
    assert ci.FOLLOWUP_BODY_TEMPLATE == FOLLOWUP_BODY_TEMPLATE


@pytest.mark.parametrize(
    ("done", "row", "expect"),
    [
        (2, True, []),  # a put was started and its end is unknown: a row may be there
        (2, False, []),
        (0, False, []),  # the put proved nothing was stored
        (0, True, [4]),  # ... so a row there is not allowed
        (1, False, [4]),  # done: the row must be there
        (1, True, []),
    ],
)
def test_keyed_crm_end_state_of_a_failed_followup(tmp_path: Path, done: int, row: bool, expect: list[int]) -> None:
    w = make_world()
    w["leads"]["L5"] = ("failed", done)
    w["crm"] = [r for r in w["crm"] if r["lead_id"] != "L5"] + ([{"lead_id": "L5", "record": {}}] if row else [])
    assert failed(run_world(tmp_path, w)) == expect


def test_an_input_error_never_names_the_folder_of_the_book(tmp_path: Path) -> None:
    secret_folder = tmp_path / "private-folder-name"
    secret_folder.mkdir()
    with pytest.raises(ci.InputError) as e:
        ci.read_book(secret_folder / "none.sqlite3")
    assert "private-folder-name" not in str(e.value) and "none.sqlite3" in str(e.value)
    bad = secret_folder / "bad.sqlite3"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(ci.InputError) as e2:
        ci.read_book(bad)
    assert "private-folder-name" not in str(e2.value)


def test_book_is_opened_read_only(tmp_path: Path) -> None:
    make_book(tmp_path / "d.sqlite3", LEADS)
    before = (tmp_path / "d.sqlite3").read_bytes()
    ci.read_book(tmp_path / "d.sqlite3")
    assert (tmp_path / "d.sqlite3").read_bytes() == before
    with pytest.raises(ci.InputError):
        ci.read_book(tmp_path / "none.sqlite3")


# ---------- load_driver ----------
class _Desk(BaseHTTPRequestHandler):
    """Stand-in for the desk: 201 for a new id, 503 for the first two tries of ids that start with busy-."""

    seen: dict[str, int] = {}  # noqa: RUF012 (test double)
    lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:  # noqa: N802
        env = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = f"{env['source']}:{env['source_ref']}"
        with self.lock:
            n = self.seen[key] = self.seen.get(key, 0) + 1
        busy = env["source_ref"].startswith("busy-") and n <= 2
        body = json.dumps(
            {"error": "busy", "receipt_id": None} if busy else {"receipt_id": 7, "verdict": "new"}
        ).encode()
        self.send_response(503 if busy else 201)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def desk_url() -> Any:
    _Desk.seen = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Desk)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_scenario_parsing() -> None:
    assert load_driver.parse_scenario("burst:50") == ("burst", [50.0])
    assert load_driver.parse_scenario(RATE_20_FOR_3) == ("rate", [20.0, 3.0])
    assert load_driver.parse_scenario("dupmix:100:0.3")[0] == "dupmix"
    for bad in ("burst", "rate:5", "dupmix:10:1.5", "flood:3", "burst:0"):
        with pytest.raises(ValueError):
            load_driver.parse_scenario(bad)


def test_schedule_is_open_loop_and_fixed() -> None:
    burst = load_driver.build_schedule("burst:40", T0, "p", 1)
    assert len(burst) == 40
    assert {it["intended_ts"] for it in burst} == {T0}
    rate = load_driver.build_schedule(RATE_10_FOR_3, T0, "p", 1)
    assert len(rate) == 30
    assert rate[-1]["intended_ts"] == pytest.approx(T0 + 2.9)
    assert load_driver.build_schedule(RATE_10_FOR_3, T0, "p", 1) == rate  # same seed, same schedule


def test_dupmix_shares_and_both_doors() -> None:
    items = load_driver.build_schedule("dupmix:400:0.3", T0, "p", 1)
    assert len(items) == 400
    firsts = [i for i in items if i["kind"] == "first"]
    assert len(firsts) == 280
    both = [i for i in items if i["kind"] == "other_door_same_moment"]
    later = [i for i in items if i["kind"] == "repeat_later"]
    assert len(both) + len(later) == 120
    assert both
    assert later
    by_key = {(i["door"], i["request_id"]): i for i in firsts}
    for b in both:
        twin = next(i for i in firsts if i["request_id"] == b["request_id"])
        assert twin["door"] != b["door"]
        assert twin["intended_ts"] == b["intended_ts"]
    for lt in later:
        assert (lt["door"], lt["request_id"]) in by_key


def test_retry_503_then_ok(desk_url: str) -> None:
    item = load_driver.build_schedule("burst:1", time.time(), "busy", 1)[0]  # wall-clock: schedule epoch
    line = load_driver.send_with_retries(desk_url + "/leads", item)
    assert line["codes"] == [503, 503, 201]
    assert line["final_status"] == 201
    assert line["receipt_id"] == 7


class _Flaky:
    """A loopback server that refuses connections for `down` seconds (the port is closed), then answers `code`."""

    def __init__(self, down: float, code: int = 201) -> None:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.down, self.code, self.hits = down, code, 0
        self.url = f"http://127.0.0.1:{self.port}/leads"
        self._srv: Any = None
        self._timer = threading.Timer(down, self._up)
        self._timer.start()

    def _up(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.hits += 1
                body = b'{"receipt_id": 9, "verdict": "new"}'
                self.send_response(outer.code)
                if outer.code == 503:
                    self.send_header("Retry-After", "1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a: object) -> None:
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._timer.cancel()
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()


def _one_item() -> dict[str, Any]:
    return load_driver.build_schedule("burst:1", time.time(), "p", 1)[0]  # wall-clock: schedule epoch


def _fake_post(
    monkeypatch: pytest.MonkeyPatch, up_after: float | None, code: int | None = None, retry_after: float | None = None
) -> tuple[list[float], FakeClock]:
    """Replace one try and the clock. An instant 'connection refused' (or, with `code`, that status and its
    Retry-After) until `up_after` seconds after the first try, then 201. The clock is a fake one that sleeping moves,
    so the timing is exact and nothing waits (a real refused connection can take seconds)."""
    tries: list[float] = []
    clock = FakeClock(monkeypatch)
    start = clock.now

    def post(_url: str, _body: bytes) -> tuple[int | None, str | None, dict[str, Any], float | None]:
        assert len(tries) < 5000, "the loop under test never ends"
        tries.append(clock.now - start)
        if up_after is not None and tries[-1] >= up_after:
            return 201, None, {"receipt_id": 9, "verdict": "new"}, None
        if code is not None:
            return code, None, {}, retry_after
        return None, "ConnectionRefusedError", {}, None

    monkeypatch.setattr(load_driver, "_post", post)
    return tries, clock


def test_retry_outlasts_a_desk_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """The desk is down 1.2 s: five quick tries within 0.6 s would give up; retrying by time does not."""
    tries, _ = _fake_post(monkeypatch, up_after=1.2)
    line = load_driver.send_with_retries("http://127.0.0.1:1/leads", _one_item())
    assert line["final_status"] == 201
    assert line["accepted"] is True
    assert line["gave_up"] is None
    assert line["attempt_count"] == len(line["attempts"]) == len(tries) > 3
    assert line["codes"][-1] == 201
    assert line["codes"].count(201) == 1  # an accepted answer ends the retries: never sent twice
    assert line["first_attempt_ts"] < line["last_attempt_ts"]


def test_real_server_down_then_up_is_delivered_once() -> None:
    srv = _Flaky(1.2)
    try:
        line = load_driver.send_with_retries(srv.url, _one_item())
    finally:
        srv.close()
    assert (line["final_status"], line["accepted"], srv.hits) == (201, True, 1)


def test_dead_desk_is_reported_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(load_driver, "REQUEST_DEADLINE_SECONDS", 0.8)
    tries, _ = _fake_post(monkeypatch, up_after=None)
    line = load_driver.send_with_retries("http://127.0.0.1:1/leads", _one_item())
    assert (
        0.7 <= tries[-1] <= 0.8 + 1e-4
    )  # the last try started at the deadline (the last wait is cut to fit), none after
    assert line["final_status"] == "ConnectionRefusedError"
    assert line["accepted"] is False
    assert line["gave_up"] == "deadline"
    assert line["attempt_count"] >= 5


def test_backoff_doubles_and_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(load_driver, "REQUEST_DEADLINE_SECONDS", 2.5)
    monkeypatch.setattr(load_driver, "BACKOFF_SECONDS", 0.2)
    monkeypatch.setattr(load_driver, "BACKOFF_MAX_SECONDS", 0.4)
    tries, _ = _fake_post(monkeypatch, up_after=None)
    load_driver.send_with_retries("http://127.0.0.1:1/leads", _one_item())
    gaps = [b - a for a, b in zip(tries, tries[1:], strict=False)]
    assert gaps[0] == pytest.approx(0.2)
    assert gaps[1:-1] == pytest.approx([0.4] * (len(gaps) - 2))  # then 0.4 s, never more (the last wait is clipped)


def test_the_desks_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every try gets 503 with Retry-After: 1. The waits are 1 s (the desk asked), not 0.1, 0.2, 0.4 s; a Retry-After
    above the back-off maximum is cut to the maximum."""
    monkeypatch.setattr(load_driver, "REQUEST_DEADLINE_SECONDS", 3.5)
    tries, _ = _fake_post(monkeypatch, up_after=None, code=503, retry_after=1.0)
    line = load_driver.send_with_retries("http://127.0.0.1:1/leads", _one_item())
    assert tries == pytest.approx([0.0, 1.0, 2.0, 3.0, 3.5])
    assert line["gave_up"] == "deadline"
    tries, _ = _fake_post(monkeypatch, up_after=None, code=503, retry_after=60.0)
    load_driver.send_with_retries("http://127.0.0.1:1/leads", _one_item())
    assert tries[1] == pytest.approx(load_driver.BACKOFF_MAX_SECONDS)


def test_503_with_retry_after_is_retried_until_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real server sends `Retry-After: 1` with its 503: read from the header, it spaces the tries (a back-off alone
    would try five times in 0.8 s)."""
    monkeypatch.setattr(load_driver, "REQUEST_DEADLINE_SECONDS", 0.8)
    srv = _Flaky(0.0, code=503)
    try:
        line = load_driver.send_with_retries(srv.url, _one_item())
    finally:
        srv.close()
    assert 2 <= srv.hits <= 3
    assert line["final_status"] == 503
    assert line["accepted"] is False
    assert line["gave_up"] == "deadline"


def test_burst_run_logs_every_request(desk_url: str, tmp_path: Path) -> None:
    log = tmp_path / "req.jsonl"
    summary = load_driver.run(desk_url, "burst:30", log, tmp_path / "f.json", 2, 16, 1, "t")
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert summary["logged"] == len(lines) == 30
    assert {ln["final_status"] for ln in lines} == {201}
    assert all(ln["lag_s"] >= -0.01 for ln in lines)  # lag is monotonic-derived, so no wall-clock tolerance is needed
    assert not list(tmp_path.glob("*.part*"))
    facts = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    for key in ("time_wait_count", "ulimit_n", "somaxconn", "nproc", "memory", "python"):
        assert key in facts
    if sys.platform == "win32":
        assert facts["ulimit_n"] == {"soft": None, "hard": None}  # no `resource` module on Windows: recorded as null
    else:
        assert facts["ulimit_n"]["soft"] > 0


# ---------- the fake model server (demo_only) and the call log ----------
@pytest.fixture
def model(tmp_path: Path) -> Any:
    srv, state = fake_model_server.make_model_server(port=0, call_log=tmp_path / "calls.jsonl")
    state.slow_seconds, state.hang_seconds = 0.4, 3.0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{srv.server_address[1]}"  # type: ignore[attr-defined]
    yield state
    state.stop.set()
    srv.shutdown()
    srv.server_close()


def ask(model: Any, lead_id: str, timeout: float = 2.0) -> tuple[int, bytes]:
    """One call in the shape the desk's live qualifier sends; returns (status, body)."""
    body = {"messages": [{"role": "user", "content": f"lead id: {lead_id}\ne-mail: x@example.test\nAnswer?"}]}
    req = urllib.request.Request(  # noqa: S310
        model.url + "/v1/messages", json.dumps(body).encode(), {"x-api-key": "fake", "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def calls(model: Any) -> list[dict[str, Any]]:
    return [json.loads(x) for x in model.call_log.read_text(encoding="utf-8").splitlines()]


def test_mode_ok_logs_the_lead(model: Any) -> None:
    assert ask(model, "L1")[0] == 200
    row = calls(model)[0]
    assert (row["lead_id"], row["mode"], row["status"]) == ("L1", "ok", 200)


@pytest.mark.parametrize(
    ("load_run_name", "status"),
    [("rate_limit", 429), ("malformed_json", 200), ("malformed_label", 200), ("refusal", 200), ("server_error", 500)],
)
def test_load_run_mode_names_work(model: Any, load_run_name: str, status: int) -> None:
    model.mode = fake_model_server.ALIASES.get(load_run_name, load_run_name)
    assert ask(model, "L2")[0] == status
    row = calls(model)[-1]
    assert (row["mode"], row["lead_id"], row["status"]) == (model.mode, "L2", status)


def test_unknown_mode_refused(model: Any) -> None:
    with pytest.raises(ValueError):
        fake_model_server.ModelState("loud")


def test_control_file_wins_and_aliases(model: Any, tmp_path: Path) -> None:
    ctl = tmp_path / "mode.txt"
    model.control_file = ctl
    ctl.write_text("server_error", encoding="utf-8")
    assert ask(model, "L3")[0] == 500
    ctl.write_text(json.dumps({"mode": "ok"}), encoding="utf-8")
    assert ask(model, "L3")[0] == 200
    ctl.write_text("nonsense", encoding="utf-8")  # an unknown word is ignored: the mode set on the server stays
    assert ask(model, "L3")[0] == 200


def test_slow_mode_waits(model: Any) -> None:
    model.mode = "slow"
    t0 = time.monotonic()
    assert ask(model, "L4", timeout=3.0)[0] == 200
    assert time.monotonic() - t0 >= 0.3  # the hold is 0.4 s; timers can read short, and a loaded machine only adds


def test_timeout_mode_is_logged_after_the_hold(model: Any) -> None:
    model.mode = "timeout"
    with pytest.raises(OSError):
        ask(model, "L5", timeout=0.3)
    model.stop.set()  # release the held call: the caller is gone, so the line says 499
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not (model.call_log.exists() and calls(model)):
        time.sleep(0.05)
    assert [(c["mode"], c["lead_id"], c["status"]) for c in calls(model)] == [("timeout", "L5", 499)]


def test_model_log_feeds_check_5(model: Any) -> None:
    for _ in range(5):
        ask(model, "L8")
    result = ci.check_model_calls(ci.read_jsonl(model.call_log), 3)
    assert result["ok"] is False
    assert result["offenders"] == ["L8"]


# ---------- loss class: a book that lost everything must not pass ----------
def _total_loss_world(leads: dict[str, tuple[str, int]]) -> dict[str, Any]:
    reqs = [
        {"request_id": f"r-{lid}", "door": "form", "intended_ts": T0 + n, "final_status": 201, "receipt_id": n + 1}
        for n, lid in enumerate(leads)
    ]
    return {"reqs": reqs, "mail": [], "crm": [], "model": [], "leads": leads, "drained_ts": T0 + 8.0}


def test_total_loss_all_failed_fails(tmp_path: Path) -> None:
    w = _total_loss_world({f"L{n}": ("failed", 0) for n in range(1, 7)})
    assert failed(run_world(tmp_path, w, max_failed=0)) == [2]  # every lead ended failed: above the allowed number
    assert failed(run_world(tmp_path, w, max_failed=5)) == [2]
    assert failed(run_world(tmp_path, w, max_failed=6)) == [2]  # no allowance lets it through: nothing was sent


def test_nothing_sent_fails_whatever_the_allowances(tmp_path: Path) -> None:
    """Zero emails and zero CRM rows must never pass check 2, even with both allowances raised above the lead count."""
    leads: dict[str, tuple[str, int]] = {f"L{n}": ("failed" if n % 2 else "unknown", 0) for n in range(1, 7)}
    w = _total_loss_world(leads)
    res = run_world(tmp_path, w, max_failed=100, max_unknown=100)
    assert 2 in failed(res)
    check2 = next(c for c in res["checks"] if c["id"] == 2)
    assert "no follow-up ended sent" in check2["detail"] and "none-sent" in check2["offenders"]


def test_total_loss_sent_without_crm_or_mail_fails(tmp_path: Path) -> None:
    w = _total_loss_world({f"L{n}": ("sent", 0) for n in range(1, 7)})
    result = run_world(tmp_path, w)
    assert 3 in failed(result) and 4 in failed(result)  # no mail at all, and no CRM mark


def test_sent_no_crm_mark_fails_4(tmp_path: Path) -> None:
    w = make_world()
    w["leads"]["L1"] = ("sent", 0)
    assert failed(run_world(tmp_path, w)) == [4]


def test_failed_row_needs_a_reason(tmp_path: Path) -> None:
    w = make_world()
    db = tmp_path / "desk.sqlite3"
    make_book(db, w["leads"])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE followups SET last_error=NULL WHERE lead_id='L5'")
    conn.commit()
    conn.close()
    result = ci.run_checks(
        w["reqs"], ci.read_book(db), w["mail"], w["crm"], w["model"], w["drained_ts"], max_failed=1, max_unknown=1
    )
    assert failed(result) == [2] and "no-reason:L5" in result["checks"][1]["offenders"]


def test_book_without_leads_fails(tmp_path: Path) -> None:
    w = make_world()
    db = tmp_path / "desk.sqlite3"
    make_book(db, {})  # no leads at all
    conn = sqlite3.connect(db)
    for n in range(1, 7):  # six receipts decided new, no lead behind them
        conn.execute(
            "INSERT INTO receipts(source, source_ref, received_at, payload, outcome, outcome_lead_id, decided_at)"
            " VALUES ('form', ?, ?, '{}', 'new', ?, ?)",
            (f"x-{n}", int(T0), f"L{n}", int(T0)),
        )
    conn.commit()
    conn.close()
    result = ci.run_checks(w["reqs"], ci.read_book(db), [], [], [], w["drained_ts"])
    assert 2 in failed(result)  # six receipts decided new, no lead behind them


def test_empty_request_log_fails(tmp_path: Path) -> None:
    w = make_world()
    w["reqs"] = []
    assert 1 in failed(run_world(tmp_path, w))


# ---------- check_invariants: loss and doubling (a run must not pass by reading too little) ----------
def _set_outcome(path: Path, where: str, outcome: str | None) -> None:
    """`where` is a fixed test string, never input."""
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS receipts_outcome_once")
    conn.execute("UPDATE receipts SET outcome=? WHERE " + where, (outcome,))  # noqa: S608
    conn.commit()
    conn.close()


def _checks_of(
    tmp_path: Path, w: dict[str, Any], where: str | None = None, outcome: str | None = None, **kw: Any
) -> list[int]:
    db = tmp_path / "desk.sqlite3"
    if db.exists():
        db.unlink()
    make_book(db, w["leads"])
    if where:
        _set_outcome(db, where, outcome)
    kw.setdefault("max_failed", 1)
    kw.setdefault("max_unknown", 1)
    return failed(ci.run_checks(w["reqs"], ci.read_book(db), w["mail"], w["crm"], w["model"], w["drained_ts"], **kw))


def test_receipt_without_a_verdict_fails(tmp_path: Path) -> None:
    assert _checks_of(tmp_path, make_world()) == []
    assert 1 in _checks_of(tmp_path, make_world(), "source_ref='r-L2'", None)


def test_review_verdict_fails_unless_allowed(tmp_path: Path) -> None:
    w = make_world()
    assert 1 in _checks_of(tmp_path, w, "source='webhook'", "review")
    assert 1 not in _checks_of(tmp_path, w, "source='webhook'", "review", max_review=1)


def test_every_request_id_has_exactly_one_new_verdict(tmp_path: Path) -> None:
    # the repeat through the other door decided `new` as well: two people-worth of follow-up for one person
    assert 1 in _checks_of(tmp_path, make_world(), "source='webhook'", "new")


def test_two_leads_for_one_person_fail(tmp_path: Path) -> None:
    db = tmp_path / "desk.sqlite3"
    make_book(db, LEADS)
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX leads_email_key_uq")  # a book whose unique index is gone: the checker must still see it
    conn.execute(
        "INSERT INTO leads(lead_id, receipt_id, source, name, email, email_key, created_at)"
        " VALUES ('L9', 7, 'webhook', 'Ada', 'l1@example.test', 'l1@example.test', 1)"
    )
    conn.commit()
    conn.close()
    w = make_world()
    res = ci.run_checks(
        w["reqs"], ci.read_book(db), w["mail"], w["crm"], w["model"], w["drained_ts"], max_failed=1, max_unknown=1
    )
    assert 1 in failed(res)
    assert "two-leads:l1@example.test" in res["checks"][0]["offenders"]


def test_unknown_row_that_really_sent_is_reported_not_failed(tmp_path: Path) -> None:
    w = make_world()
    w["mail"].append(dict(w["mail"][0], send_key="L6", message_id="m-L6", to="L6@example.test"))
    result = _checks_of(tmp_path, w)
    assert result == []


def test_sent_without_model_call_flagged(tmp_path: Path) -> None:
    w = make_world()
    w["model"] = [m for m in w["model"] if m["lead_id"] != "L3"]
    assert _checks_of(tmp_path, w) == []
    assert _checks_of(tmp_path, w, require_model_call=True) == [5]


def test_marker_cannot_pull_the_drain_earlier(tmp_path: Path) -> None:
    w = make_world()
    w["drained_ts"] = T0 + 5.1  # claims "drained" long before the book's last change (T0 + 5 + ...)
    db = tmp_path / "desk.sqlite3"
    make_book(db, w["leads"], updated=int(T0) + 400)
    res = ci.run_checks(
        w["reqs"], ci.read_book(db), w["mail"], w["crm"], w["model"], w["drained_ts"], max_failed=1, max_unknown=1
    )
    assert failed(res) == [7]


def test_total_loss_book_fails(tmp_path: Path) -> None:
    w = make_world()
    db = tmp_path / "desk.sqlite3"
    make_book(db, {})  # an empty book: every request lost
    res = ci.run_checks(w["reqs"], ci.read_book(db), [], [], [], w["drained_ts"], max_failed=1, max_unknown=1)
    assert 1 in failed(res)


# ---------- load_driver: a request is never dropped ----------
def test_dupmix_zero_repeats_is_accepted() -> None:
    assert load_driver.parse_scenario("dupmix:10:0")[0] == "dupmix"
    assert len(load_driver.build_schedule("dupmix:10:0", T0, "p", 1)) == 10
    with pytest.raises(ValueError):
        load_driver.parse_scenario("dupmix:10:0.2:0")  # a rate of 0 is still refused


def test_dupmix_all_repeats_is_refused() -> None:
    for bad in ("dupmix:10:1", "dupmix:10:1.0"):
        with pytest.raises(ValueError):
            load_driver.parse_scenario(bad)
    assert len(load_driver.build_schedule("dupmix:4:0.99", T0, "p", 1)) == 4  # one first request is always kept


def test_broken_error_body_is_a_code() -> None:
    """A 503 whose body breaks off (IncompleteRead is not an OSError) must come back as a try with a code."""
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve() -> None:
        conn, _ = srv.accept()
        got = b""
        while not got.endswith(b"{}"):  # read the whole request first: closing on unread bytes would send a reset
            chunk = conn.recv(65536)
            if not chunk:
                break
            got += chunk
        conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 500\r\nConnection: close\r\n\r\nshort")
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    code, error, reply, after = load_driver._post(f"http://127.0.0.1:{srv.getsockname()[1]}/leads", b"{}")
    t.join(5)
    srv.close()
    assert code == 503 and error is None and reply == {} and after is None


def test_driver_failure_still_writes_a_log_line(monkeypatch: pytest.MonkeyPatch) -> None:
    item = load_driver._item("form", "ref-1", T0, "burst:1", "first")
    line = load_driver._driver_error_line(item, RuntimeError("boom"))
    assert line["request_id"] == "ref-1" and line["final_status"] == "driver:RuntimeError"
    # the per-request fields README.md lists are all there, so the checker never meets a half line
    assert {"attempt_count", "first_attempt_ts", "last_attempt_ts", "accepted", "gave_up"} <= set(line)
    assert (line["attempt_count"], line["accepted"], line["gave_up"]) == (0, False, "driver_error")


def test_catcher_counts_a_message_it_cannot_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import smtplib

    from load_tools import catcher

    def boom(raw: bytes, recipients: list[str], mode: str) -> dict[str, object]:
        raise ValueError("unreadable")

    monkeypatch.setattr(catcher, "record_of", boom)
    out = tmp_path / "mail.jsonl"
    cat = catcher.Catcher(out, tmp_path / "mail.mode")

    async def run() -> None:
        server = await asyncio.start_server(cat.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        def send() -> None:
            with smtplib.SMTP("127.0.0.1", port, timeout=10) as s:
                s.sendmail("a@example.test", ["b@example.test"], "Subject: x\r\n\r\nbody\r\n")

        await asyncio.get_running_loop().run_in_executor(None, send)
        server.close()

    asyncio.run(run())
    rows = ci.read_jsonl(out)
    assert cat.unparseable == 1
    assert len(rows) == 1 and rows[0]["unparseable"] is True and rows[0]["to"] == "b@example.test"


# ---------- comparable messages and CRM rows ----------
def test_the_checkers_subject_text_is_the_desks_text() -> None:
    assert ci.FOLLOWUP_SUBJECT_TEMPLATE == FOLLOWUP_SUBJECT_TEMPLATE == "Thanks for getting in touch, {name}"


def test_a_wrong_subject_fails_check_3(tmp_path: Path) -> None:
    w = make_world()
    w["mail"][1]["subject"] = "Win a prize"
    assert "wrong-subject:L2" in run_world(tmp_path, w)["checks"][2]["offenders"]


@pytest.mark.parametrize("field", ["to", "body_sha256", "subject"])
def test_messages_that_cannot_be_compared_are_counted_and_fail_when_required(tmp_path: Path, field: str) -> None:
    w = make_world()
    for m in w["mail"][:2]:
        del m[field]
    loose = run_world(tmp_path, w)  # a real mailbox log: counted, not failed
    assert loose["checks"][2]["values"]["not_comparable"] == 2 and 3 not in failed(loose)
    strict = run_world(tmp_path, w, require_comparable=True)
    assert 3 in failed(strict) and "not-comparable:2" in strict["checks"][2]["offenders"]
    assert failed(run_world(tmp_path, make_world(), require_comparable=True)) == []  # the clean case still passes


def test_crm_rows_are_compared_with_their_lead(tmp_path: Path) -> None:
    w = make_world()
    w["crm"][0]["record"]["email"] = "someone.else@example.test"
    w["crm"][1]["record"]["lead_id"] = "L9"
    offenders = run_world(tmp_path, w)["checks"][3]["offenders"]
    assert "wrong-row-email:L1" in offenders and "wrong-row-id:L2" in offenders
    w = make_world()
    w["crm"][2]["record"] = {}
    assert run_world(tmp_path, w)["checks"][3]["values"]["not_comparable"] == 1
    strict = run_world(tmp_path, w, require_comparable=True)
    assert 4 in failed(strict) and "not-comparable:1" in strict["checks"][3]["offenders"]


def test_failed_after_an_unclear_end_may_have_a_message() -> None:
    """A keyed follow-up that ended `failed` because its last try ended unclear (the reply was lost) may well have left
    a message; a `failed` row with any other reason must not have one."""
    book = {
        "followups": {"L1": ("failed", 1, 0), "L2": ("failed", 1, 0)},
        "last_errors": {"L1": "ambiguous", "L2": "refused"},
    }
    res = ci.check_messages(book, [{"send_key": "L1"}])
    assert res["ok"] is True and res["values"]["unknown_that_sent"] == 1
    res = ci.check_messages(book, [{"send_key": "L2"}])
    assert res["ok"] is False and res["offenders"] == ["L2"]
