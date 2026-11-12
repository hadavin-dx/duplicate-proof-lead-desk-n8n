# HubSpot free-account probe

A one-shot script that answers one question: does a free HubSpot account accept the API writes the desk needs
(set the owner and the lead status on a contact, then delete it)? Result: the only real-service try, run by the author on one free HubSpot account, logs not shipped. In the first run create, lead-status write, read-back and permanent delete passed, and the owner list was refused (403 `MISSING_SCOPES`) until the token also had the owners read scope; after that a second run passed every step, and a third run, after a second user was added, listed two owners and passed again. Each run used 6 or 7 of the 8 calls and left no contact behind.

Run it on the free demo account only, never on an account that holds real contacts. Creating or assigning a contact can
trigger things on the account side that the script cannot see: an owner assignment can send that user a notification
email, and any workflow or sequence on the account that reacts to a new contact or a new owner can fire. A fresh demo
account with no workflows and a throw-away owner is the safe place.

What it does, in order (7 calls on the normal path, never more than 8 including retries):

1. Checks whether the contact property `dpld_tag` exists (it is only set when it does).
2. Reads up to 10 owners; prints the count and the last 4 characters of each id.
3. Creates ONE contact with a made-up address `probe-<random>@example.com`.
4. Sets the first owner on it (by email), in its own call.
5. Sets the lead status `OPEN` on it, in its own call (two calls, so the table says which of the two a free plan refuses).
6. Reads it back and compares the values that were written.
7. Always deletes it: permanent delete first; if that is refused and the contact id is known, the normal delete (archive).
   When the create answer was lost, only the permanent delete by email is tried (HubSpot documents the normal delete for an id only).

Call arithmetic. The hard budget is 8 calls, every retry after a "busy" answer (429) counts. The first 7 are spent on the
steps above; the normal steps may use at most 7 calls, so the 8th call is always kept for the permanent delete. The
normal path uses 7, which leaves 1 spare: that spare pays for the archive-delete fallback or one retry. If any step
needed a retry, the fallback delete has no call left, and the script then prints a WARNING naming the contact. Worst
case is exactly 8 calls. If the answer to the create call is lost (timeout, a 5xx answer, or no id in the reply) the script
cannot know whether the contact exists, so it uses its cleanup call to delete by the made-up email address; if that is
refused it prints a WARNING with the exact address (`probe-...@example.com`) to look for and delete by hand.

It prints PASS/FAIL for: owners readable, contact create, owner write, lead-status write, read-back equal, delete,
plus the HTTP status classes seen. It never prints the token or a header. For a refused call (a 4xx answer) it prints HubSpot's error category and the first 100 characters of the message, with the token replaced by `***` if it appears there; it prints no other part of a reply. The desk's own change of owner and lead status is one combined call (`crm_hubspot.py`), while the probe makes two separate calls so the table shows which one is refused: the probe does not test the combined call.

Run (after you have agreed to the 7 calls; the token is a private-app token that you put in an environment variable of
your own terminal window for this one run, and never type as an argument; close the window afterwards. This is the one
place in this repository where a secret is read from a variable: the desk and the other scripts read secrets from a
file, see `SECURITY.md`):

    python -I scripts/hubspot_probe.py --plan
    python -I scripts/hubspot_probe.py --yes-spend-calls 7

Private-app scopes used (the list is what the script calls; HubSpot's own page for each endpoint was not readable in
full when this was written, so the exact scope names are UNVERIFIED against the live scope list; the runs showed that the
owners read scope is needed, check the others when creating the token):

- contacts read: owner read-back, the `dpld_tag` property check (this needs the contacts schema read scope; if the token
  lacks it, HubSpot answers 403 and the script reads that as "property absent" and skips it, which is harmless),
- contacts write: create and update,
- owners read: the owner list,
- the permanent (privacy) delete may need a further scope; if it is refused the script falls back to the normal delete,
  which only archives the contact.

`--token-env NAME` changes the variable name (default `HUBSPOT_PROBE_TOKEN`); it must be a plain variable name, anything
else is refused without being printed. `--dry-run http://127.0.0.1:PORT` runs the flow against a local stub (loopback
only); the tests do this. If the delete step prints a WARNING, delete the named probe contact by hand.

## What was checked against HubSpot's current documentation (read on 2026-11-11)

Confirmed from HubSpot's developer pages and changelog on that date: create is `POST /crm/v3/objects/contacts` with
`{"properties": {...}}`; update by email is `PATCH /crm/v3/objects/contacts/{email}?idProperty=email`; read by email is
`GET` on the same path with `idProperty=email` and a comma-separated `properties`; normal delete is
`DELETE /crm/v3/objects/contacts/{id}` (recycling bin); the owners list is `GET /crm/v3/owners` and its entries carry a
string `id` to use when assigning an owner; the permanent delete is `POST /crm/v3/objects/contacts/gdpr-delete`, and
when a contact is identified by email the body must be `{"objectId": "<email>", "idProperty": "email"}` (HubSpot
changelog entry on the contact GDPR deletion endpoints).

Answered by the runs: a free account accepted the owner write and the `hs_lead_status` write; an address ending in
`.test` was refused with a 400 on create (the message was not printed in that run) while `example.com` addresses were
accepted, so made-up addresses that go to HubSpot must end in a real domain such as `example.com`.

UNVERIFIED (could not be confirmed from the documentation or by the runs): the exact scope names; whether the owner
assignment sends a notification email; and what HubSpot does with an email that gdpr-delete cannot find (a third-party
page says it may be added to a block list, which is harmless for a random address). The runs showed that `OPEN` was
accepted as a lead status on that account and that the permanent delete answered 204.

Sources (names only): HubSpot developers site, CRM API contacts page; HubSpot developers site, CRM API owners page;
HubSpot developers changelog, "changes to the gdpr deletion endpoints for the crm contact object api"; one third-party
connector reference for the delete status. Read on 2026-11-11.
