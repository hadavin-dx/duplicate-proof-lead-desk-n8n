"""Edge cases: odd bytes, odd requests, odd email addresses, the AI step on two drives.
Made-up data only. Each test here fails when its guard is removed."""

from __future__ import annotations

from support import HOST, call, lead, post_lead, raw_exchange, run_threads


def test_lone_surrogate_is_review(env) -> None:
    """A JSON escape for half a character is valid JSON but cannot be stored: the receipt is kept, verdict review."""
    rig = env.rig()
    base = env.serve(rig)
    body = b'{"source":"form","source_ref":"s1","payload":{"name":"\\ud800","email":"ada@example.test"}}'
    status, out = call(base, "POST", "/leads", raw=body)
    assert (status, out["verdict"], out["reason"]) == (202, "review", "bad_field:name")
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 1
    assert rig.leads() == 0
    # a half character in a key we do not read is kept as text and replays cleanly
    body2 = b'{"source":"form","source_ref":"s2","payload":{"name":"Ada","email":"ada@example.test","\\ud800":1}}'
    first = call(base, "POST", "/leads", raw=body2)
    again = call(base, "POST", "/leads", raw=body2)
    assert first[0] == 201
    assert (again[0], again[1]["replayed"], again[1]["payload_matches"]) == (201, True, True)


def test_deeply_nested_json_is_bad_envelope(env) -> None:
    rig = env.rig()
    base = env.serve(rig)
    depth = 5000
    body = b'{"source":"form","source_ref":"deep","payload":' + b"[" * depth + b"]" * depth + b"}"
    assert len(body) < rig.config.size_cap_bytes
    status, out = call(base, "POST", "/leads", raw=body)
    assert (status, out) == (400, {"error": "bad_envelope", "receipt_id": None})
    assert call(base, "GET", "/counts")[1]["rejected_envelopes"] == 1
    assert rig.query("SELECT count(*) FROM receipts")[0][0] == 0
    # the resolve route reads JSON too: the same bomb is a bad decision, not a server error
    status, out = call(base, "POST", "/review/Fx/resolve", raw=b"[" * depth + b"]" * depth)
    assert (status, out) == (400, {"error": "bad_decision"})


def test_head_options_and_malformed_requests_get_json(env) -> None:
    rig = env.rig()
    base = env.serve(rig)
    status, out = raw_exchange(base, f"OPTIONS /leads HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    assert (status, out) == (405, {"error": "method_not_allowed"})
    status, out = raw_exchange(
        base, f"HEAD /counts HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode()
    )  # a HEAD reply has no body
    assert (status, out) == (405, None)
    status, out = raw_exchange(base, b"TRACE / HTTP/1.1\r\nHost: x\r\n\r\n")  # a method the server does not know
    assert (status, out) == (501, {"error": "not_implemented"})
    status, out = raw_exchange(base, b"GET /a b HTTP/1.1\r\n\r\n")  # a request line with four parts
    assert (status, out) == (400, {"error": "bad_request"})


def test_long_lowercase_email_is_review(env) -> None:
    """One capital letter with a dot (U+0130) becomes two characters in lower case: 134 typed, 268 as the key."""
    rig = env.rig()
    base = env.serve(rig)
    email = "İ" * 130 + "@example.test"
    assert len(email) <= 254 < len(email.lower())
    status, out = post_lead(base, "form", "long-1", lead(email=email))
    assert (status, out["verdict"], out["reason"]) == (202, "review", "invalid_email")
    assert rig.leads() == 0


def test_two_drives_one_email(env) -> None:
    """Two threads drive the same pending follow-up at once. The AI step may run on both (a cost, at most 2 calls);
    the stored label is one value, and exactly one email and one CRM row result."""
    rig = env.rig(replies={"ada@example.test": "hot"})
    d = rig.desk
    lead_id = d.process("form", "p1", lead()).body["lead_id"]  # process only: nothing drives the follow-up yet
    assert rig.qualifier.calls == 0
    run_threads([lambda: d.run_followup(lead_id), lambda: d.run_followup(lead_id)])
    assert rig.qualifier.calls in (1, 2)
    assert d.lead(lead_id).body["followup"]["ai_label"] == "hot"
    assert rig.crm.rows[lead_id]["ai_label"] == "hot"
    assert (rig.outbox.count, rig.crm.count) == (1, 1)
    assert rig.state(lead_id)[0] == "sent"
