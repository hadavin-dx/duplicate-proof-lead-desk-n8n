"""scripts/hubspot_probe.py against a loopback stub: no real service, made-up data only."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from support import start_serving, stop_serving

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hubspot_probe as hp  # noqa: E402

TOKEN = "pat-" + "na1-" + "0000-1111-2222"  # a made-up token, built from parts
ENV = "PROBE_TOKEN_FOR_TESTS"
OWNER = "90001234"
OK_PATCH = {"properties": {"hs_lead_status": "OPEN", "hubspot_owner_id": OWNER}}


def _step(method: str, path: str) -> str:
    if "/properties/" in path:
        return "tag"
    if "/owners" in path:
        return "owners"
    if path.endswith("gdpr-delete"):
        return "gdpr"
    return {"POST": "create", "PATCH": "patch", "DELETE": "delete"}.get(method, "readback")


class Stub:
    """Answers by step name; a step maps to a list of (status, body) used in order, the last one repeats."""

    def __init__(self, plan: dict[str, list[tuple[int, Any]]]) -> None:
        self.plan = plan
        self.log: list[str] = []
        self.auth: set[str] = set()
        self.retry_after = "0"
        self.bodies: list[Any] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def _go(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    stub.bodies.append((self.command, self.path, json.loads(self.rfile.read(n))))
                stub.auth.add(self.headers.get("Authorization", ""))
                step = _step(self.command, self.path)
                stub.log.append(step)
                seq = stub.plan.get(step, [(200, {})])
                status, body = seq[min(stub.log.count(step) - 1, len(seq) - 1)]
                if status == 0:
                    self.connection.close()
                    return
                raw = json.dumps(body).encode()
                self.send_response(status)
                if status == hp.HTTP_TOO_MANY:
                    self.send_header("Retry-After", stub.retry_after)
                if 300 <= status < 400:
                    self.send_header("Location", self.path)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = do_DELETE = _go  # noqa: N815

            def log_message(self, *args: Any) -> None:
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        start_serving(self.srv)

    def close(self) -> None:
        stop_serving(self.srv)


HAPPY: dict[str, list[tuple[int, Any]]] = {
    "tag": [(404, {})],
    "owners": [(200, {"results": [{"id": OWNER}]})],
    "create": [(201, {"id": "5550001"})],
    "patch": [(200, {})],
    "readback": [(200, OK_PATCH)],
    "gdpr": [(204, {})],
}


@pytest.fixture
def make(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Stub]]:
    made: list[Stub] = []
    monkeypatch.setenv(ENV, TOKEN)

    def _make(**over: list[tuple[int, Any]]) -> Stub:
        s = Stub({**HAPPY, **over})
        made.append(s)
        return s

    yield _make
    for s in made:
        s.close()


def go(s: Stub, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    args = ["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", ENV, "--dry-run", s.url]
    rc = hp.main(args, sleep=lambda _: None)
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_happy_path_count_matches_plan(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make()
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_OK, out
    assert len(s.log) == hp.PLANNED_CALLS  # the plan count is the real count
    assert s.log == ["tag", "owners", "create", "patch", "patch", "readback", "gdpr"]
    assert hp.PLANNED_CALLS == 7 and len(s.log) < hp.CALL_BUDGET  # one call spare for the fallback delete
    assert "permanent" in out and f"calls used: {hp.PLANNED_CALLS}" in out
    assert s.auth == {"Bearer " + TOKEN}  # the token did reach the (local) server
    assert TOKEN not in out and OWNER not in out  # masked ids only


def test_plan_prints_count_and_calls_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert hp.main(["--plan"]) == hp.EXIT_OK
    assert f"planned calls: {hp.PLANNED_CALLS}" in capsys.readouterr().out


def test_refuses_wrong_count_missing_token_and_token_argument(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert hp.main(["--yes-spend-calls", "5", "--token-env", ENV]) == hp.EXIT_REFUSED
    assert hp.main([]) == hp.EXIT_REFUSED
    assert hp.main(["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", ENV]) == hp.EXIT_REFUSED
    with pytest.raises(SystemExit):
        hp.main(["--token=" + TOKEN])
    assert TOKEN not in capsys.readouterr().out


def test_dry_run_refuses_non_loopback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    rc = hp.main(["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", ENV, "--dry-run", "http://example.test"])
    assert rc == hp.EXIT_REFUSED and TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("step", "reply", "deleted"),
    [
        ("create", [(403, {})], False),  # nothing was created, nothing to clean
        ("patch", [(403, {"message": "missing scope"})], True),
        ("patch", [(500, {})], True),
        ("readback", [(500, {})], True),
        ("owners", [(403, {})], True),
        ("readback", [(0, None)], True),  # hang-up
    ],
)
def test_cleanup_after_failure_at_each_step(
    make: Callable[..., Stub],
    capsys: pytest.CaptureFixture[str],
    step: str,
    reply: list[tuple[int, Any]],
    deleted: bool,
) -> None:
    s = make(**{step: reply})
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL
    assert ("gdpr" in s.log) is deleted
    assert "FAIL" in out and TOKEN not in out  # a 4xx names its error text (first 100 chars) but never the token


def test_token_echoed_in_a_4xx_message_is_masked(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(patch=[(401, {"category": "INVALID_AUTHENTICATION", "message": f"Authentication failed for {TOKEN} now"})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL
    assert TOKEN not in out and "***" in out and "INVALID_AUTHENTICATION" in out


def test_delete_falls_back_to_normal_delete(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(gdpr=[(403, {})], delete=[(204, {})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_OK and s.log[-2:] == ["gdpr", "delete"] and "normal delete" in out


def test_both_deletes_fail_warns_and_fails(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(gdpr=[(403, {})], delete=[(500, {})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL and "may still exist" in out and TOKEN not in out


def test_429_retries_are_counted_and_the_budget_stops_the_run(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    s = make(owners=[(429, {})])  # always busy: 1 + MAX_429_WAITS attempts, then FAIL; the flow goes on
    rc, out = go(s, capsys)
    assert s.log.count("owners") == 1 + hp.MAX_429_WAITS and rc == hp.EXIT_FAIL
    # no owner, one patch: tag 1 + owners 3 + create 1 + patch 1 + read back 1 = 7 = the normal limit; then the delete
    assert s.log == ["tag", "owners", "owners", "owners", "create", "patch", "readback", "gdpr"]
    assert "calls used: 8 of 8" in out
    s2 = make(tag=[(429, {})], owners=[(429, {})], create=[(429, {})])  # the budget runs out before create answers
    rc, out = go(s2, capsys)
    assert s2.log == ["tag"] * 3 + ["owners"] * 3 + ["create"] and "STOPPED" in out and rc == hp.EXIT_FAIL
    assert "calls used: 7 of 8" in out and "gdpr" not in s2.log  # create was refused (429), nothing to clean


def test_budget_stop_still_deletes_with_reserve(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(owners=[(429, {}), (200, {"results": []})], readback=[(429, {})])
    rc, out = go(s, capsys)
    assert s.log == ["tag", "owners", "owners", "create", "patch", "readback", "readback", "gdpr"]
    assert "calls used: 8 of 8" in out and rc == hp.EXIT_FAIL and TOKEN not in out


def test_gdpr_429_forever_stays_inside_budget(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(gdpr=[(429, {})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL and "may still exist" in out
    assert len(s.log) == hp.CALL_BUDGET and s.log.count("gdpr") == 2  # 7 used before cleanup: gdpr x2 hits the cap
    assert "calls used: 8 of 8" in out


def test_cleanup_ignores_normal_limit_but_not_the_hard_budget() -> None:
    p = hp.Probe("http://127.0.0.1:9", "x", lambda _: None, lambda _: None)
    p.used = hp.CALL_BUDGET - hp.DELETE_RESERVE
    with pytest.raises(hp._Stop):
        p.call("x", "GET", "/a")  # a normal call stops at the limit
    p.used = hp.CALL_BUDGET
    with pytest.raises(hp._Stop):
        p.call("x", "GET", "/a", cleanup=True)  # nothing goes past the hard budget
    p.used = hp.CALL_BUDGET - 1
    p.call("x", "GET", "/a", cleanup=True)  # the reserve is spendable by cleanup
    assert p.used == hp.CALL_BUDGET


@pytest.mark.parametrize("reply", [[(0, None)], [(500, {})]])
def test_unknown_create_outcome_deletes_by_email(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str], reply: list[tuple[int, Any]]
) -> None:
    s = make(create=reply)
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL and s.log == ["tag", "owners", "create", "gdpr"]
    _, path, body = s.bodies[-1]
    assert path.endswith("gdpr-delete") and body["idProperty"] == "email" and body["objectId"].endswith("@example.com")
    assert "NOT NEEDED (nothing" not in out and "PASS (permanent)" in out


def test_unknown_create_outcome_that_cannot_be_deleted_warns_with_the_email(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    s = make(create=[(0, None)], gdpr=[(500, {})], delete=[(500, {})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL and "look for probe-" in out and "@example.com in HubSpot" in out
    assert len(s.log) <= hp.CALL_BUDGET


def test_create_id_missing_deletes_by_email(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(create=[(201, {})])
    rc, _ = go(s, capsys)
    assert rc == hp.EXIT_FAIL and s.log[-1] == "gdpr" and s.bodies[-1][2]["idProperty"] == "email"


def test_unknown_create_but_gdpr_404_is_not_needed(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    s = make(create=[(500, {})], gdpr=[(404, {})])
    _, out = go(s, capsys)
    assert "NOT NEEDED (no contact" in out and "WARNING" not in out and s.log[-1] == "gdpr"


def test_create_403_says_not_needed(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(create=[(403, {})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL and "NOT NEEDED (nothing was created)" in out and s.log == ["tag", "owners", "create"]


def test_owner_and_lead_status_are_written_in_separate_calls(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    s = make(patch=[(200, {}), (403, {})])  # the second PATCH (lead status) is refused
    rc, out = go(s, capsys)
    patches = [b for m, _, b in s.bodies if m == "PATCH"]
    assert patches == [{"properties": {"hubspot_owner_id": OWNER}}, {"properties": {"hs_lead_status": "OPEN"}}]
    assert rc == hp.EXIT_FAIL and "owner write          PASS" in out and "lead-status write    FAIL" in out
    assert "gdpr" in s.log


def test_tag_property_present_is_written(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(tag=[(200, {})])
    rc, out = go(s, capsys)
    create = next(b for m, p, b in s.bodies if m == "POST" and p.endswith("/contacts"))
    assert rc == hp.EXIT_OK and create["properties"]["dpld_tag"] == "probe" and "present, will be set" in out


def test_no_owner_skips_owner_write_and_still_passes(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    s = make(owners=[(200, {"results": []})], readback=[(200, {"properties": {"hs_lead_status": "OPEN"}})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_OK and "SKIPPED (no owner found)" in out and s.log.count("patch") == 1


def test_redirect_is_not_followed(make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]) -> None:
    s = make(owners=[(302, {})])
    rc, out = go(s, capsys)
    assert s.log.count("owners") == 1 and rc == hp.EXIT_FAIL and "owners: 3xx" in out


def test_proxy_environment_is_ignored(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")  # a dead proxy: any use of it makes every call fail
    for k in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)  # else the loopback host could be exempt and hide the check
    s = make()
    importlib.reload(hp)  # the opener is built at import time, so it must be built again with the proxy variables set
    try:
        rc, _ = go(s, capsys)
    finally:
        monkeypatch.undo()
        importlib.reload(hp)
    assert rc == hp.EXIT_OK and len(s.log) == hp.PLANNED_CALLS


@pytest.mark.parametrize("value", ["-5", "nan", "inf", "-inf", "abc"])
def test_odd_retry_after_does_not_crash(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str], value: str
) -> None:
    waits: list[float] = []
    s = make(owners=[(429, {}), (200, {"results": []})])
    s.retry_after = value
    rc = hp.main(
        ["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", ENV, "--dry-run", s.url], sleep=waits.append
    )
    capsys.readouterr()
    assert rc in (hp.EXIT_OK, hp.EXIT_FAIL) and waits and all(0 <= w <= hp.MAX_WAIT_SECONDS for w in waits)


def test_token_pasted_as_env_name_is_never_echoed(capsys: pytest.CaptureFixture[str]) -> None:
    rc = hp.main(["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", TOKEN])
    out = capsys.readouterr()
    assert rc == hp.EXIT_REFUSED and TOKEN not in out.out + out.err and "pat-" not in out.out


def test_no_token_in_exception_text(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    # nothing listens on this port: the transport error text must not reach the output
    args = ["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", ENV, "--dry-run", "http://127.0.0.1:9"]
    rc = hp.main(args, sleep=lambda _: None)
    out = capsys.readouterr()
    assert rc == hp.EXIT_FAIL and TOKEN not in out.out + out.err and "no answer" in out.out


def test_unset_token_variable_name_is_not_echoed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "A_NAME_THAT_COULD_BE_A_PASTED_TOKEN"[:32]
    monkeypatch.delenv(name, raising=False)
    rc = hp.main(["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", name])
    out = capsys.readouterr()
    assert rc == hp.EXIT_REFUSED and name not in out.out + out.err and "missing" in out.out


def test_token_env_name_longer_than_32_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    long_name = "X" * 33  # looks like a name but could be a pasted hyphen-less token
    rc = hp.main(["--yes-spend-calls", str(hp.PLANNED_CALLS), "--token-env", long_name])
    out = capsys.readouterr()
    assert rc == hp.EXIT_REFUSED and long_name not in out.out and "NAME of an environment variable" in out.out


@pytest.mark.parametrize(
    ("readback", "field"),
    [
        ({"properties": {"hs_lead_status": "OPEN", "hubspot_owner_id": "123"}}, "hubspot_owner_id"),  # wrong owner
        ({"properties": {"hs_lead_status": "NEW", "hubspot_owner_id": OWNER}}, "hs_lead_status"),  # wrong status
        ({"properties": {"hs_lead_status": "OPEN"}}, "hubspot_owner_id"),  # owner missing
    ],
)
def test_read_back_with_different_values_fails_and_names_the_field(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str], readback: Any, field: str
) -> None:
    s = make(readback=[(200, readback)])
    rc, out = go(s, capsys)
    line = next(x for x in out.splitlines() if x.startswith("read-back equal"))
    assert rc == hp.EXIT_FAIL and "FAIL" in line and f"{field} differs" in line
    assert "gdpr" in s.log


def test_lost_create_then_successful_delete_says_create_outcome_unknown(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    s = make(create=[(500, {})])
    _, out = go(s, capsys)
    assert "create outcome unknown" in out and "PASS (permanent)" in out


def test_lost_create_never_uses_a_by_email_normal_delete(
    make: Callable[..., Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    s = make(create=[(0, None)], gdpr=[(403, {})])
    rc, out = go(s, capsys)
    assert rc == hp.EXIT_FAIL and "delete" not in s.log and "look for probe-" in out
