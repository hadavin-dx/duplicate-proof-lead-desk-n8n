"""HubspotCrm against the fake HubSpot server (loopback, plain http). Made-up data only; no network beyond loopback."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from support import HOST, stop_within

from demo_only import fake_hubspot as fh
from lead_desk.adapters import CrmError, CrmNothingStored, HttpCrm
from lead_desk.config import Config
from lead_desk.constants import HUBSPOT_BASE_URL, HUBSPOT_HOST_SUFFIXES
from lead_desk.crm_hubspot import (
    CrmBusy,
    CrmCapReached,
    CrmConfigError,
    CrmConflictUnreadable,
    CrmEmailTaken,
    CrmRefused,
    HubspotCrm,
    HubspotSettings,
)
from lead_desk.loader import load_demo_adapters

KEY = "k" + "e" * 15 + "-for-tests-0001"  # built from parts; not a real key
REC = {
    "name": "Ada Example",
    "email": "Ada@Example.test",
    "company": "Example Ltd",
    "source": "form",
    "ai_label": "hot",
}


class Rig:
    def __init__(self, tmp: Path, mode: str = "ok") -> None:
        self.key_file = tmp / "key.txt"
        self.key_file.write_text(KEY + "\n", encoding="utf-8")
        self.state = fh.HubspotState(KEY, mode)
        self.srv, self.th, self.url = fh.serve(self.state, HOST)

    def crm(self, **kw: Any) -> HubspotCrm:
        s: dict[str, Any] = {
            "key_file": self.key_file,
            "max_calls": 50,
            "base_url": self.url,
            "timeout_seconds": 2.0,
            "allow_any_base_url": True,  # the fake listens on loopback, outside the vendor's domains
        }
        return HubspotCrm(HubspotSettings(**(s | kw)))

    def stop(self) -> None:
        stop_within(lambda: fh.stop(self.srv, self.th, self.state), "fake HubSpot server")


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    yield r
    r.stop()


def mode(r: Rig, m: str, **kw: Any) -> None:
    r.state.mode = m
    for k, v in kw.items():
        setattr(r.state, k, v)


def test_put_creates_one_contact_with_our_property(rig: Rig) -> None:
    c = rig.crm()
    assert c.capability == "keyed"
    assert c.put("L-1", REC) == "PUT"
    (row,) = rig.state.contacts.values()
    assert row["properties"] == {
        "email": "ada@example.test",
        "hadavin_lead_id": "L-1",
        "firstname": "Ada",
        "lastname": "Example",
        "company": "Example Ltd",
    }
    assert c.stats()["created"] == 1 and c.stats()["calls"] == 1
    assert rig.state.auth_seen == ["Bearer " + KEY]


def test_second_put_same_lead_is_already_present_without_a_second_contact(rig: Rig) -> None:
    c = rig.crm()
    assert c.put("L-1", REC) == "PUT"
    assert c.put("L-1", REC) == "ALREADY_PRESENT"
    assert len(rig.state.contacts) == 1
    assert c.stats()["conflicts"] == 1 and c.stats()["gets"] == 1


def test_409_without_an_id_is_confirmed_by_email(rig: Rig) -> None:
    mode(rig, "dup409")
    c = rig.crm()
    c.put("L-1", REC)
    assert c.put("L-1", REC) == "ALREADY_PRESENT"
    assert any("idProperty=email" in p for _, p in rig.state.calls)
    assert len(rig.state.contacts) == 1


def test_real_409_wording_is_read_back_by_the_named_id(rig: Rig) -> None:
    """HubSpot is assumed to answer a second create of the same
    email with 409 and a message like 'Contact already exists. Existing ID: <id>'. The adapter reads that id and
    confirms by id, not by email; it does not rely on the category text."""
    rec = {**REC, "email": "ada@example.com"}
    c = rig.crm()
    assert c.put("L-1", rec) == "PUT"
    (cid,) = rig.state.contacts
    rig.state.calls.clear()
    assert c.put("L-1", rec) == "ALREADY_PRESENT"
    assert any(p.startswith(f"/crm/v3/objects/contacts/{cid}?") for _, p in rig.state.calls), rig.state.calls
    assert not any("idProperty=email" in p for _, p in rig.state.calls)
    body = {"status": "error", "message": f"Contact already exists. Existing ID: {cid}", "category": "CONFLICT"}
    from lead_desk import crm_hubspot as mod

    assert mod._EXISTING_ID.search(json.dumps(body)).group(1) == cid  # type: ignore[union-attr]


def test_the_fake_refuses_dot_test_like_the_real_crm_when_asked(rig: Rig) -> None:
    rig.state.reject_test_domain = True
    with pytest.raises(CrmRefused):
        rig.crm().put("L-1", REC)  # REC is ada@example.test
    assert not rig.state.contacts
    assert rig.crm().put("L-1", {**REC, "email": "ada@example.com"}) == "PUT"


def test_same_email_other_lead_id_is_taken_and_nothing_is_changed(rig: Rig) -> None:
    c = rig.crm()
    c.put("L-1", REC)
    with pytest.raises(CrmEmailTaken) as e:
        c.put("L-2", REC)
    assert e.value.nothing_stored is True
    assert next(iter(rig.state.contacts.values()))["properties"]["hadavin_lead_id"] == "L-1"


def test_same_person_different_email_makes_a_second_contact(rig: Rig) -> None:
    """The case the CRM cannot catch (its only key is the email): documented, and counted at our adapter."""
    c = rig.crm()
    c.put("L-1", REC)
    c.put("L-2", {**REC, "email": "ada.e@example.test"})
    assert len(rig.state.contacts) == 2


def test_timeout_after_create_then_retry_finds_it_and_does_not_double(rig: Rig) -> None:
    mode(rig, "timeout_after_create")
    c = rig.crm(timeout_seconds=0.5)
    with pytest.raises(CrmError) as e:
        c.put("L-1", REC)
    assert e.value.nothing_stored is False  # unclear: the contact is in the CRM
    assert len(rig.state.contacts) == 1
    assert c.put("L-1", REC) == "ALREADY_PRESENT"  # a keyed CRM is asked again
    assert len(rig.state.contacts) == 1


def test_slow_answer_inside_budget_is_ok_and_beyond_it_is_unclear(rig: Rig) -> None:
    mode(rig, "slow", delay=0.2)
    assert rig.crm(timeout_seconds=2.0).put("L-1", REC) == "PUT"
    mode(rig, "slow", delay=1.5)
    with pytest.raises(CrmError) as e:
        rig.crm(timeout_seconds=0.4).put("L-2", {**REC, "email": "b@example.test"})
    assert e.value.nothing_stored is False


@pytest.mark.parametrize("m", ["r5xx", "r5xx_after_store"])
def test_5xx_is_unclear_and_the_retry_is_safe(rig: Rig, m: str) -> None:
    mode(rig, m, times=1)
    c = rig.crm()
    with pytest.raises(CrmError) as e:
        c.put("L-1", REC)
    assert e.value.nothing_stored is False
    assert c.put("L-1", REC) in ("PUT", "ALREADY_PRESENT")
    assert len(rig.state.contacts) == 1


def test_429_waits_the_named_seconds_then_succeeds(rig: Rig) -> None:
    mode(rig, "r429", times=1, retry_after="0.2")
    c = rig.crm()
    assert c.put("L-1", REC) == "PUT"
    assert c.stats()["rate_limited"] == 1 and c.stats()["calls"] == 2


def test_429_uses_the_backoff_without_retry_after_and_gives_up_busy(rig: Rig) -> None:
    mode(rig, "r429", times=99)
    c = rig.crm(max_429_retries=1, timeout_seconds=3.0)
    with pytest.raises(CrmBusy) as e:
        c.put("L-1", REC)
    assert e.value.nothing_stored is True and e.value.retry_after is None
    assert c.stats()["rate_limited"] == 2 and not rig.state.contacts


def test_429_with_a_wait_longer_than_the_budget_does_not_sleep(rig: Rig) -> None:
    mode(rig, "r429", times=99, retry_after="60")
    with pytest.raises(CrmBusy) as e:
        rig.crm(timeout_seconds=1.0).put("L-1", REC)
    assert e.value.retry_after == 60.0


@pytest.mark.parametrize("m", ["unauthorized", "forbidden_400"])
def test_4xx_is_refused_and_stored_nothing(rig: Rig, m: str) -> None:
    mode(rig, m)
    with pytest.raises(CrmRefused) as e:
        rig.crm().put("L-1", REC)
    assert e.value.nothing_stored is True
    assert KEY not in str(e.value) and not rig.state.contacts


def test_malformed_201_is_unclear(rig: Rig) -> None:
    mode(rig, "malformed")
    with pytest.raises(CrmError) as e:
        rig.crm().put("L-1", REC)
    assert e.value.nothing_stored is False


def test_not_reached_is_stored_nothing(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.stop()  # the port is closed now
    with pytest.raises(CrmNothingStored):
        r.crm(timeout_seconds=6.0).put("L-1", REC)  # a closed port can be refused slowly on some systems


def test_a_rejected_call_still_gets_its_answer_when_the_body_arrives_after_the_close(rig: Rig) -> None:
    """http.client sends the headers first, the body after. The fake answers a rejected call (401) at once and closes;
    if the body is then still unread, the system resets the connection and the client loses the answer (Windows:
    ConnectionAbortedError). The fake reads the body before it answers. Here the body is sent late on purpose."""
    body = b'{"properties":{"email":"a@example.test"}}' * 30
    head = (
        "POST /crm/v3/objects/contacts HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer wrong\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode()
    with socket.create_connection(rig.srv.server_address[:2], timeout=5) as c:
        c.sendall(head)
        time.sleep(0.3)  # the fake has answered and closed by now
        try:
            c.sendall(body)
        except OSError:
            pass  # the reset can already show here; the answer must still be readable below
        time.sleep(0.3)
        assert c.recv(200).startswith(b"HTTP/1.1 401")


def test_call_cap_stops_before_the_network(rig: Rig) -> None:
    c = rig.crm(max_calls=1)
    c.put("L-1", REC)
    n = len(rig.state.calls)
    with pytest.raises(CrmCapReached):
        c.put("L-2", {**REC, "email": "c@example.test"})
    assert len(rig.state.calls) == n


@pytest.mark.parametrize("text", [None, "", "   \n", "short"])
def test_key_file_missing_or_empty_is_a_classed_config_error_without_the_path(rig: Rig, text: str | None) -> None:
    if text is None:
        rig.key_file.unlink()
    else:
        rig.key_file.write_text(text, encoding="utf-8")
    with pytest.raises(CrmConfigError) as e:
        rig.crm().put("L-1", REC)
    assert str(rig.key_file) not in repr(e.value) and rig.key_file.name not in repr(e.value)
    assert e.value.__cause__ is None
    assert not rig.state.calls


def test_key_is_read_at_call_time_and_never_appears_in_errors_or_logs(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    c = rig.crm()
    rig.key_file.write_text("n" + "e" * 15 + "-key-0002", encoding="utf-8")  # rotated after the adapter was built
    with pytest.raises(CrmRefused) as e:
        c.put("L-1", REC)
    assert KEY not in repr(e.value) and "Bearer" not in repr(e.value)
    assert KEY not in caplog.text
    rig.key_file.write_text(KEY, encoding="utf-8")
    assert c.put("L-1", REC) == "PUT"


@pytest.mark.parametrize("bad", ["", "L 1", "L\r\n1", "a" * 65, 'L"1'])
def test_lead_id_outside_the_allowed_set_is_refused_before_the_network(rig: Rig, bad: str) -> None:
    with pytest.raises(CrmRefused):
        rig.crm().put(bad, REC)
    assert not rig.state.calls


def test_read_back_list_search_lag_and_archive(rig: Rig) -> None:
    mode(rig, "search_lag", lag=0.6)
    c = rig.crm()
    for i in (1, 2):
        c.put(f"RUN9-{i}", {**REC, "email": f"u{i}@example.test"})
    c.put("OTHER-1", {**REC, "email": "z@example.test"})
    ids = dict(c.rows_with_prefix("RUN9-"))
    assert sorted(ids) == ["RUN9-1", "RUN9-2"]
    assert c.wait_visible("RUN9-1", 0.05) is None  # not in the index yet
    lag = c.wait_visible("RUN9-2", 5.0)
    assert lag is not None and lag >= 0.1
    c.archive(ids["RUN9-1"])
    assert [lid for lid, _ in c.rows_with_prefix("RUN9-")] == ["RUN9-2"]
    assert c.stats()["searches"] >= 2


def test_concurrent_puts_of_one_lead_make_one_contact(rig: Rig) -> None:
    c = rig.crm()
    out: list[str] = []

    def go() -> None:
        out.append(c.put("L-1", REC))

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out.count("PUT") == 1 and len(rig.state.contacts) == 1


def test_settings_validation_and_env() -> None:
    with pytest.raises(ValueError, match="plain http"):
        HubspotSettings(Path("k"), 1, base_url="http://example.com")
    with pytest.raises(ValueError, match="DESK_HUBSPOT_MAX_CALLS must be a whole number between 1 and"):  # zero
        HubspotSettings(Path("k"), 0)
    with pytest.raises(ValueError, match="DESK_HUBSPOT_TIMEOUT_SECONDS"):
        HubspotSettings(Path("k"), 1, timeout_seconds=99)
    with pytest.raises(ValueError, match="DESK_HUBSPOT_MAX_CALLS is required"):  # missing
        HubspotSettings.from_env({"DESK_HUBSPOT_KEY_FILE": "x"})
    s = HubspotSettings.from_env({"DESK_HUBSPOT_KEY_FILE": "x", "DESK_HUBSPOT_MAX_CALLS": "5"})
    assert s.base_url.startswith("https://") and s.max_calls == 5


def test_config_and_loader_select_hubspot_and_default_stays_fake(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    assert Config().crm == "fake"
    assert Config.from_env({"DESK_CRM": "hubspot"}).crm == "hubspot"
    with pytest.raises(ValueError, match="DESK_CRM must be"):
        Config(crm="zoho")
    monkeypatch.setenv("DESK_HUBSPOT_KEY_FILE", str(rig.key_file))
    monkeypatch.setenv("DESK_HUBSPOT_MAX_CALLS", "9")
    monkeypatch.setenv("DESK_HUBSPOT_BASE_URL", rig.url)
    monkeypatch.setenv("DESK_HUBSPOT_ALLOW_ANY_BASE_URL", "yes")
    crm, _, _ = load_demo_adapters(Config(crm="hubspot", outbox="smtp"))
    assert isinstance(crm, HubspotCrm) and not isinstance(crm, HttpCrm)
    monkeypatch.delenv("DESK_HUBSPOT_KEY_FILE")
    with pytest.raises(ValueError, match="DESK_HUBSPOT_KEY_FILE"):
        load_demo_adapters(Config(crm="hubspot", outbox="smtp"))
    assert os.environ.get("DESK_HUBSPOT_KEY_FILE") is None


# -- redirects, rotated keys, listing and conflicts ----------------------------------------------------------------


def test_a_redirect_to_another_host_is_never_followed_and_the_key_never_leaves(rig: Rig, tmp_path: Path) -> None:
    other_key = tmp_path / "other.txt"
    other_key.write_text(KEY, encoding="utf-8")
    other = fh.HubspotState(KEY, "ok")
    srv, th, url = fh.serve(other, HOST)
    try:
        mode(rig, "redirect", redirect_to=url)
        with pytest.raises(CrmError) as e:
            rig.crm().put("L-1", REC)
        assert not isinstance(e.value, CrmNothingStored)  # a 3xx is unclear, not "stored nothing"
        assert other.calls == [] and other.auth_seen == []  # the other host saw no request, so no Authorization
        assert len(rig.state.calls) == 1  # asked once, not retried
    finally:
        stop_within(lambda: fh.stop(srv, th, other), "fake HubSpot server")


def test_rotated_key_never_appears_in_errors_or_logs(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rotated = "n" + "e" * 15 + "-key-0002"
    c = rig.crm()
    rig.key_file.write_text(rotated, encoding="utf-8")  # the NEW key, refused by the fake (it expects KEY)
    with caplog.at_level("DEBUG"), pytest.raises(CrmRefused) as e:
        c.put("L-1", REC)
    assert rotated not in repr(e.value) and rotated not in str(e.value) and rotated not in caplog.text
    assert rotated not in repr(e.value.__cause__) and rotated not in repr(e.value.__context__)


def test_429_waits_at_least_the_named_seconds(rig: Rig) -> None:
    mode(rig, "r429", times=1, retry_after="0.5")
    t0 = time.monotonic()
    assert rig.crm().put("L-1", REC) == "PUT"
    assert time.monotonic() - t0 >= 0.45


def test_listing_pages_are_followed_until_the_end(rig: Rig) -> None:
    rig.state.page_max = 2
    c = rig.crm()
    for i in range(5):
        c.put(f"RUN7-{i}", {**REC, "email": f"p{i}@example.test"})
    before = len(rig.state.calls)
    rows = c.rows_with_prefix("RUN7-")
    assert sorted(lid for lid, _ in rows) == [f"RUN7-{i}" for i in range(5)]
    assert sum(1 for m, _ in rig.state.calls[before:] if m == "GET") == 3  # 2 + 2 + 1 rows: three pages


def test_a_hanging_listing_ends_inside_one_budget_not_fifty(rig: Rig) -> None:
    mode(rig, "list_hang")
    t0 = time.monotonic()
    try:
        with pytest.raises(CrmError):
            rig.crm(timeout_seconds=0.5).rows_with_prefix("RUN7-")
        assert time.monotonic() - t0 < 3.0  # one request budget (0.5 s), not timeout x 50
    finally:
        rig.state.release.set()


def test_recycle_bin_makes_the_conflict_loud_and_unclear(rig: Rig) -> None:
    c = rig.crm()
    assert c.put("L-1", REC) == "PUT"
    ((_, cid),) = c.rows_with_prefix("L-")
    c.archive(cid)  # now in the bin: the email stays reserved
    with pytest.raises(CrmConflictUnreadable) as e:
        c.put("L-1", REC)
    assert e.value.nothing_stored is False and "recycle bin" in str(e.value)
    assert not rig.state.contacts  # nothing was stored again


def test_the_fake_creates_atomically() -> None:
    st = fh.HubspotState(KEY, "ok")
    made: list[bool] = []

    def go() -> None:
        made.append(st.create_if_absent({"email": "a@example.test"})[1])

    ts = [threading.Thread(target=go) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert made.count(True) == 1 and len(st.contacts) == 1


_SUF1, _SUF2 = HUBSPOT_HOST_SUFFIXES  # the vendor names live in constants.py only


def _https(host: str) -> str:
    return f"https://{host}"


@pytest.mark.parametrize(
    "url", ["https://example.test", _https(f"api.{_SUF1}.example.test"), _https(f"{_SUF2}.example.test"), _https("x")]
)
def test_the_key_goes_only_to_hubspot_hosts_unless_overridden(url: str) -> None:
    with pytest.raises(ValueError, match="vendor's own domains"):
        HubspotSettings(Path("k"), 1, base_url=url)
    assert HubspotSettings(Path("k"), 1, base_url=url, allow_any_base_url=True).base_url == url
    for ok in (HUBSPOT_BASE_URL, _https(f"api.{_SUF2}"), _https(_SUF1.upper())):
        assert HubspotSettings(Path("k"), 1, base_url=ok).base_url == ok
    s = HubspotSettings.from_env(
        {"DESK_HUBSPOT_KEY_FILE": "k", "DESK_HUBSPOT_MAX_CALLS": "1", "DESK_HUBSPOT_BASE_URL": url,
         "DESK_HUBSPOT_ALLOW_ANY_BASE_URL": "yes"}
    )  # fmt: skip
    assert s.allow_any_base_url


def test_a_bad_number_names_the_setting_and_not_its_value() -> None:
    for name, val in (("DESK_HUBSPOT_MAX_CALLS", "sec" + "ret-1"), ("DESK_HUBSPOT_TIMEOUT_SECONDS", "sec" + "ret-2")):
        env = {"DESK_HUBSPOT_KEY_FILE": "k", "DESK_HUBSPOT_MAX_CALLS": "5", name: val}
        with pytest.raises(ValueError) as e:
            HubspotSettings.from_env(env)
        assert name in str(e.value) and val not in str(e.value)


def test_the_adapter_budget_is_clamped_to_the_desks_send_timeout(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_HUBSPOT_KEY_FILE", str(rig.key_file))
    monkeypatch.setenv("DESK_HUBSPOT_MAX_CALLS", "9")
    monkeypatch.setenv("DESK_HUBSPOT_BASE_URL", rig.url)
    monkeypatch.setenv("DESK_HUBSPOT_ALLOW_ANY_BASE_URL", "yes")
    monkeypatch.setenv("DESK_HUBSPOT_TIMEOUT_SECONDS", "8")
    from lead_desk.crm_hubspot import make_crm

    assert make_crm(Config(send_timeout_seconds=2.0, crm="hubspot")).s.timeout_seconds == 2.0
    assert make_crm(Config(send_timeout_seconds=30.0, crm="hubspot")).s.timeout_seconds == 8.0  # own value is lower
    monkeypatch.setenv("DESK_HUBSPOT_TIMEOUT_SECONDS", "9")
    assert make_crm(Config(send_timeout_seconds=30.0, crm="hubspot")).s.timeout_seconds == 9.0  # never above 9


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        (b"\xef\xbb\xbf" + KEY.encode() + b"\r\n", True),  # a byte-order mark that some text editors add is fine
        (KEY.encode() + b"\n" + KEY.encode(), False),  # two lines
        ("﻿".encode("utf-16") + KEY.encode("utf-16-le"), False),  # UTF-16
        (b"k" * 5000, False),  # far too long for a key
    ],
)
def test_key_file_formats(rig: Rig, raw: bytes, ok: bool) -> None:
    rig.key_file.write_bytes(raw)
    if ok:
        assert rig.crm().put("L-1", REC) == "PUT"
    else:
        with pytest.raises(CrmConfigError) as e:
            rig.crm().put("L-1", REC)
        assert KEY not in str(e.value) and rig.key_file.name not in str(e.value)
        assert not rig.state.calls


# -- settings, key files, request time limit and printouts ---------------------------------------------------------


def test_a_409_whose_lead_id_matches_but_whose_email_differs_is_not_this_leads_contact(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lead_desk import crm_hubspot as mod

    c = rig.crm()
    other = {"properties": {"hadavin_lead_id": "L-1", "email": "someone.else@example.test"}}
    monkeypatch.setattr(c, "_call", lambda *a, **k: mod._Reply(200, other, None))
    with pytest.raises(CrmEmailTaken):
        c._confirm_existing("L-1", "ada@example.test", {"message": "Existing ID: 7"}, time.monotonic() + 5)
    same = {"properties": {"hadavin_lead_id": "L-1", "email": "ADA@example.test "}}
    monkeypatch.setattr(c, "_call", lambda *a, **k: mod._Reply(200, same, None))
    assert c._confirm_existing("L-1", "ada@example.test", {}, time.monotonic() + 5) == "ALREADY_PRESENT"


def test_the_read_back_asks_for_the_email_property_too(rig: Rig) -> None:
    c = rig.crm()
    c.put("L-1", REC)
    c.put("L-1", REC)
    assert any("properties=email" in p for m, p in rig.state.calls if m == "GET")


def test_archive_is_counted_as_an_archive_not_as_a_read(rig: Rig) -> None:
    c = rig.crm()
    c.put("L-1", REC)
    (cid,) = rig.state.contacts
    c.archive(cid)
    assert c.stats()["archives"] == 1 and c.stats()["gets"] == 0


def test_one_request_is_bounded_as_a_whole_even_when_the_status_line_trickles_in(tmp_path: Path) -> None:
    """The socket timeout is per read, so a server that sends one byte at a time inside it could keep the call alive
    for as long as it liked. The whole request (headers included) ends at the budget."""
    import socket

    srv = socket.socket()
    srv.bind((HOST, 0))
    srv.listen(1)
    stop = threading.Event()

    def trickle() -> None:
        conn, _ = srv.accept()
        conn.settimeout(1.0)
        try:
            conn.recv(65536)
            for ch in b"HTTP/1.1 201 Created\r\nX-Slow: " + b"a" * 400:
                if stop.is_set():
                    break
                conn.sendall(bytes([ch]))
                time.sleep(0.1)
        except OSError:
            pass
        finally:
            conn.close()

    th = threading.Thread(target=trickle, daemon=True)
    th.start()
    kf = tmp_path / "key.txt"
    kf.write_text(KEY, encoding="utf-8")
    settings = HubspotSettings(
        kf, 5, base_url=f"http://{HOST}:{srv.getsockname()[1]}", timeout_seconds=1.0, allow_any_base_url=True
    )
    try:
        t0 = time.monotonic()
        with pytest.raises(CrmError):
            HubspotCrm(settings).put("L-1", REC)
        assert time.monotonic() - t0 < 2.5
    finally:
        stop.set()
        srv.close()
        th.join(3)


def test_settings_do_not_print_the_key_file_path() -> None:
    marker = "marker-folder-name"
    assert marker not in repr(HubspotSettings(Path(marker) / "k", 1))
