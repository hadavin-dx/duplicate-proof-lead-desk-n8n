# load_tools: load and invariant tools for the lead desk

Everything here uses made-up data and loopback addresses only. No real model, mail or CRM service is ever called.
Run every tool from the repository root with `PYTHONPATH=src:.` (on Windows use `;` between the entries: `src;.`), as in the other run commands in contract.md.

Every file path you give a tool on its command line (`--log`, `--facts`, `--requests`, `--book`, `--catcher`, `--crm`, `--model-log`, `--drain`, `--json`, `--out`, `--control`, and for `demo_only/fake_model_server.py` also `--key-file`, `--call-log`, `--control-file`) must lie inside the folder you started it in, the repository folder, or the system temporary folder; a path that leaves them (`..`, an absolute path elsewhere, a link that points out) is refused with a message that names the option, and nothing is read or written (a change from before: an outside path that used to work, for example a `--call-log` file in a folder outside the three allowed ones, is now refused; list its folder in `LEAD_DESK_PATH_ROOTS` to keep using it). To use another folder, name it in the environment variable `LEAD_DESK_PATH_ROOTS` (several folders separated by `:`, or `;` on Windows). A NEW output file may only use letters, digits, `.`, `_` and `-` in its name (a file that already exists may have any name except one with a control character; a new name that ends with a dot or is a Windows device name such as `nul`, `aux`, `con`, `prn`, `com1` or `lpt1`, with or without an extension, is refused on every system, so the file also opens on Windows). A file that is read and does not exist is reported as "no such file". A name with accented letters is found also when it is written in the other Unicode form (macOS stores `é` as two characters, other systems as one), below the allowed folder (upper and lower case must match on macOS, and the allowed folder itself must be written the way the system reports it). The same rule applies to `scripts/n8n_api.py import FILE` and `tools/scrub_n8n_export.py` (an n8n export kept in another folder, for example Downloads, is read after you copy it into the repository folder or list its folder in `LEAD_DESK_PATH_ROOTS`).

Not covered, on purpose: `scripts/run_real_check.py` reads its config from any folder (the documentation tells you to keep it outside the repository) and uses the paths named inside it as written, and `scripts/run_live_check.py` takes its key file and its spend ledger from any folder for the same reason (`docs/real-services.md`). The config of `run_real_check.py` is found by looking the name up in the folder listings from the top of the file system, so it must be a file in a folder you can list: a config below a folder that you can enter but not list (mode 0711 owned by another user), or one handed over as `--config <(command)` or `/dev/fd/N`, is refused with a message; copy it to a normal file first. What this check is: a fence against a WRONG argument (a faulty script, a copied path). It is not a defence against an agent or a person who controls the shell: they can start the tool in another folder or set `LEAD_DESK_PATH_ROOTS`. The allowed folders are wide, and a file that already exists in one of them (the repository, the shared system temporary folder) can be overwritten by an output option that names it: the check does not know which files in them matter. It does not stop another process that swaps a folder for a link between the check and the open. Hard links are not detected: a hard link is a second name for the same file contents, so a hard link made inside an allowed folder to a file outside it passes the check, and reading or writing it reads or changes the outside file (only someone who can write in an allowed folder can make one). A book on a Windows network path (`\\server\share\desk.sqlite3`) is refused by `--book`'s read-only open, because SQLite takes only an empty or `localhost` name after `file://`; copy it to a local drive (a mapped network drive is probably refused too, because Windows reports it by its network name; not measured). Not measured: SQLite on Windows itself (the URI text was tested on Linux only). The tools also start directly (`python load_tools/load_driver.py --help`, `python tools/scrub_n8n_export.py in.json out.json`) without setting `PYTHONPATH`.

| File | What it does |
|---|---|
| `load_driver.py` | Sends requests to the desk's `POST /leads` on a fixed schedule and writes the **request log**. |
| `check_invariants.py` | The seven checks, run after a scenario has drained. Exit code 0 only if all seven hold. |

## Input and output formats (the contract between the tools)

All times are epoch seconds as floating point numbers (`time.time()` on the same machine). One JSON object per line
in every `.jsonl` file.

### Request log (written by `load_driver.py`, read by check 1, 6, 7)
One line per INTENDED request, sorted by `intended_ts`:

| Field | Meaning |
|---|---|
| `request_id` | The `source_ref` sent to the desk. |
| `door` | `form` or `webhook` (the envelope's `source`). A desk receipt is identified by (`door`, `request_id`). |
| `intended_ts` | The moment the schedule said to send (fixed before the run; open loop). |
| `sent_ts` | Calendar time (wall clock) when the first try was sent. |
| `lag_s` | How late the first try was sent against the schedule, measured with the monotonic clock (a step of the wall clock cannot change it). A large value means the driver fell behind; a value a little below 0 means it woke slightly early. |
| `done_ts` | When the request was finished (last reply or last failure). |
| `attempts` | List, one entry per try: `ts`, `code` (HTTP status, or `null`), `error` (class name of the network error, or `null`), `latency_s`. |
| `codes` | The same as a flat list: the HTTP status, or the error class name for a try with no reply. |
| `final_status` | Status of the last try (a number), or the error class name (text) if the last try had no reply. |
| `receipt_id`, `verdict` | From the last reply body (`null` if none). |
| `scenario`, `kind`, `worker` | `kind` is `first`, `repeat_later` or `other_door_same_moment`. |

Retry policy (the load driver is a stand-in for a patient sender such as n8n or a mail relay): a 503 or a network error
(refused, reset, timeout) is retried by TIME, not by count. The wait starts at `BURST_BACKOFF_SECONDS` (0.1 s), doubles
after each try and never exceeds `BURST_BACKOFF_MAX_SECONDS` (1 s); a `Retry-After` from the desk is honoured up to that maximum; no new try starts more than
`BURST_RETRY_DEADLINE_SECONDS` (30 s) after the first. Reason: a desk restart takes some time, and a fixed number of quick tries can all
fail before the desk is back, so those requests would never be delivered. Other codes (200, 201,
202, 4xx, 500) end the request, so an accepted answer is never sent twice. A request still unanswered at the deadline
is logged with `accepted: false`, `gave_up: "deadline"` and the last code or error as `final_status`, so a desk that
stays dead is reported, never hidden. Extra per-request fields: `attempt_count`, `first_attempt_ts`, `last_attempt_ts`,
`accepted`, `gave_up` (`deadline`, or `driver_error` for a request the driver itself could not finish; such a line has `attempt_count` 0 and no attempt times). The burst script (`demo_only/burst.py`) and the portal demo's burst use the same rule (`post_with_retries`); neither has a count limit.

Machine facts are written once at the start to `<log>.facts.json`: `time_wait_count`, `ulimit_n`, `somaxconn`,
`nproc` (cpu count and `ulimit -u`), `memory` (kB), `python`, `platform`, plus the scenario, worker and thread counts.
`max_in_flight` = workers x threads: a burst larger than that is not fully at one moment.

### Desk book (the desk's SQLite file; opened read-only)
Tables used: `receipts(source, source_ref, receipt_id, outcome)`, `leads(lead_id, email_key)`, `followups(lead_id, state, crm_done, updated_at, last_error)`.

### SMTP catcher log (written by `catcher.py`, read by check 3)
One line per accepted message: `{ts, send_key, message_id, to, subject, body_sha256, mode}`. `send_key` equals the
follow-up's `lead_id` (contract.md, "Follow-up drive"). `send_key` and `to` are read: the recipient must equal the lead's e-mail address (letter case ignored). A line `{"unparseable": true, ...}` stands for a message the catcher accepted but could not read; check 3 fails on it.

### CRM dump (read by check 4)
JSON: either the fakes server reply of `GET /fake/crm` (`{"count": n, "rows": [{"lead_id", "record"}]}`) or a bare list
of such rows. Only `lead_id` is read.

### Model call log (written by the fake model server with `--call-log`, read by check 5)
One line per call to `/v1/messages`: `{ts, lead_id, mode, status}`. `mode` is the mode in force for the call, `status`
the HTTP status sent (`499` = the client had gone before the answer was written; only in `timeout` mode; that line is written after the hold).

### Drain marker (optional, read by check 7)
JSON `{"drained_ts": <epoch seconds>}`. The checker works out the drain moment itself: the newest `followups.updated_at` of the
book (whole-second resolution). A marker can only make the drain LATER than the book shows, never earlier, so a marker that
claims an early drain cannot make check 7 pass. Nothing in this folder writes a marker; the book is enough.

## The seven checks (`python -m load_tools.check_invariants --help`)

1. **Receipts and verdicts.** Every distinct (`door`, `request_id`) of the request log has a receipt in the book. A request with no
   receipt (whatever its retries) is lost. Every receipt has a verdict (a receipt with no verdict fails); verdict `review` fails unless
   `--max-review` allows it; new + duplicate + review + still open = receipts (this is the whole equation the desk's `/counts` and `/api/ui/summary` hold: there is no `failed` receipt verdict; `rejected_envelopes` counts requests the desk refused BEFORE a receipt existed, lives in the desk's memory and starts again at 0 after a restart; failed follow-ups are counted apart, in check 2); every request id (one person) has exactly ONE `new` verdict and no
   email address has two leads (a doubled lead means a doubled follow-up). The number of lines in the request log must equal
   `intended_requests` of the driver's facts file (`--facts`, default `<requests>.facts.json`, which must exist), so a driver that dropped a
   request cannot pass. Also listed: a receipt number the client was given that differs from the book. An EMPTY request log fails.
2. **States add up.** `pending + sending + sent + failed + unknown = number of leads`, every lead has one follow-up
   row, and `pending = sending = 0`. Also every `failed` or `unknown` row has a `last_error`;
   no more than `--max-failed` (default 0) follow-ups ended `failed` and no more than `--max-unknown` (default 0) ended `unknown` (a run with a crash or a slow mail system sets its own numbers; a run in which EVERY follow-up ended `unknown` proves nothing and fails whatever `--max-unknown` says); and the number of receipts decided `new` equals
   the number of leads, so a book that lost its leads, or lost every follow-up, cannot pass.
3. **Messages.** At most one catcher message per `send_key`; no key that belongs to no lead; every settled follow-up
   (`sent`, `failed`, `unknown`) with zero messages must be exactly one in state `failed` or `unknown`, and every
   `failed`/`unknown` one must have zero. (An `unknown` follow-up that really did send is reported in the values, not failed: that is what `unknown` means. A `failed` one with a message fails.) A message whose recorded recipient is not the lead's address fails (`wrong-recipient:<lead>`; a message with no recorded recipient, such as a count read from a mailbox, is not compared); two messages to one address under different keys fail (`address-shared:<key>`); a message whose text hash (recorded by the catcher) differs from the follow-up text for that lead's name fails (`wrong-body:<key>`). a message whose subject is not the follow-up subject fails (`wrong-subject:<key>`). Messages with no recorded recipient, subject or text hash are COUNTED as `not_comparable` (shown in the detail and values); with `--require-comparable` (the default of this command, which reads fake and dry runs where the catcher records all three) a count above 0 fails the check (`not-comparable:<n>`), so a catcher that stopped recording cannot turn these checks into silent passes. Use `--no-require-comparable` only for a log from a real mailbox. Follow-ups still `pending`/`sending` are left to check 2.
4. **CRM.** Allowed rows = the leads with `crm_done = 1` (each must be there) plus the "listed unknowns": leads with
   `crm_done = 2` (a put was started, its end unknown) or a follow-up in state `unknown` (each may or may not be there).
   Fails on a missing `crm_done = 1` row, a row for any other lead, a lead twice, or a row for a lead the book does not hold.
   A row whose record carries a lead id or email that is not its lead's fails (`wrong-row-id:<lead>`, `wrong-row-email:<lead>`); a row with an empty record is `not_comparable` (failed under `--require-comparable`).
   Also fails `sent-without-crm:<lead>`: a follow-up in state `sent` whose `crm_done` is not 1.
   End-state rule for a keyed CRM (a follow-up that ended `failed`): `crm_done = 0` means the put proved nothing was stored (no row allowed), `crm_done = 2` means a put was started and its end is unknown (a row may be there), `crm_done = 1` means done (a row must be there). Any other row/mark combination fails.
5. **Model calls per lead** are at most `--model-bound` (default 3), not "exactly one". Every lead that ended `sent` must also have at least one model call when `--model-required` is on. It is on by default when the environment says `DESK_QUALIFIER=live_claude` (the checker cannot see the desk's own setting, so run it with the same environment) and off otherwise; `--model-required` and `--no-model-required` force it either way. The driver records `DESK_QUALIFIER` in the facts file, and the checker also turns the check on when that file says `live_claude`, so a checker run in another shell still knows.
6. **503 rate** = responses with code 503 / all tries, plus how many requests saw a 503 and how many ended on one.
   Reported always; it fails only above `--max-503-rate` (default: no ceiling).
7. **Drain time** = drain moment (computed from the book, see above) minus the last `intended_ts`; fails above `--max-drain-seconds` (default 120).

Each check lists its first 10 offending ids (check 1: `door:request_id`; others: lead ids). Output: a plain-text
table, then the JSON result (or `--json PATH`). Exit code: 0 all hold, 1 a check failed, 2 an input file is missing or
not in the formats above.

## Scenarios (`load_driver.py --scenario ...`)

* `burst:N` N requests all intended at the same moment, doors alternating.
* `rate:R:D` R requests per second for D seconds (intended times 1/R apart).
* `dupmix:TOTAL:FRACTION[:RATE]` TOTAL requests at RATE per second (default 200); FRACTION of them are repeats of an
  earlier request id: half through the OTHER door at the same intended moment, half later (up to 2 s) through the same door.

Use a new `--id-prefix` for every run against the same desk book.

## Fake model server (`demo_only/fake_model_server.py`)

The model stand-in for load runs is the one fake model server of the repository (same request and reply shape
the desk's live qualifier uses). Start it with `python -m demo_only.fake_model_server --port 48767 --call-log model.jsonl
[--control-file mode.txt]`. Switch the mode with `POST /control {"mode": "..."}`, or through the control file (one mode
word or `{"mode": ...}`, read on every call, wins over `POST /control`). Load-run names and server names are the same
modes: `rate_limit` = `r429`, `malformed_json` = `malformed`, `malformed_label` = `badlabel`, `server_error` = `r5xx`;
`ok`, `slow`, `timeout`, `refusal` are the same in both lists (the server also has `mixed` and `drip`).

## SMTP catcher (`catcher.py`, made-up data only)

`catcher.py` is a small mail server for the load runs. It keeps every accepted message as one line of JSON in
a file, so a check can count what really arrived. It is standard library only.

Start: `python load_tools/catcher.py --out mail.jsonl --port 0` (prints `LISTENING <host> <port>`). Flags: `--host`
(default 127.0.0.1), `--port` (default 2525; 0 = free port), `--control FILE` (default `<out>.mode`).

Record fields: `ts`, `send_key` (header X-Send-Key), `message_id`, `to`, `subject`, `body_sha256` (SHA-256 of the
text body as UTF-8, LF line ends, minus the one line break the sender adds), `mode`.

### Changing the behaviour while it runs

The control file holds one word and is read again on every command. No restart. A missing or empty file means `ok`.

| Word | What the catcher does |
|---|---|
| `ok` | normal server, answers 250 |
| `slow N` | records the message, waits N seconds, then answers 250 |
| `drop-after-data` | records the message, then closes without the final answer ("maybe sent") |
| `reject` | answers 550 to the recipient; nothing is recorded |
| `down` | closes every new connection at once; to simulate a closed port, stop the catcher |

Example: `echo "slow 3" > mail.jsonl.mode`, later `echo ok > mail.jsonl.mode`.

### Why not aiosmtpd

aiosmtpd (version 1.4.6, licence Apache-2.0, PyPI upload date 2024-05-18) is not a dependency
of this repository and was not used: these tools prefer no new dependency, and the catcher needs behaviour a library hides (record the message and then drop the connection,
a delay before the last answer, a mode switched by a file). The stdlib version is short. If a full-strength test server is needed later, aiosmtpd is the fallback.

### Sender side

`src/lead_desk/smtp_outbox.py` (`SmtpOutbox`, settings `DESK_SMTP_HOST`, `DESK_SMTP_PORT`, `DESK_SMTP_FROM`,
`DESK_SMTP_MESSAGE_ID_DOMAIN`, `DESK_SMTP_TIMEOUT_SECONDS`). Tests: `tests/test_smtp_outbox.py`.
