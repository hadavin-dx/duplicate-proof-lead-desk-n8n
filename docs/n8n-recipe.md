# The n8n workflows of the demo, what each one does

Who this is for: someone who has used n8n a little and wants to read what the workflows of the demo do, or to import them unmodified into their own n8n for evaluation. The workflows in `n8n/`
("Lead Desk: Web Form Intake", "Lead Desk: Email Intake", "Lead Desk: Duplicate Test (10 at once)") were run against the desk on 2026-11-09, on one computer, in a real n8n (version in `versions.md`) through its REST interface; lines say "checked" where that run showed it (tried once by the author, logs not shipped). **Every step marked
"not checked" has not been tried; treat it as a plan, not a fact.** (One later change to the `proof` workflow was not run in n8n: see section 3.) The n8n screen (browser) was not used: importing through the
"Import from file" menu is not tested. Menu and field names in n8n change between versions; the names below were not compared with the screen.

**Licence.** The workflows in `n8n/` may be imported unmodified into your own n8n to evaluate them. Anything else (editing them, building variants, sharing or publishing exports) needs the licence holder's written permission (see `LICENSE`; it covers the documents, workflows and scripts too).

Made-up data and demo servers only. Use no real credentials, no n8n licence key and no real e-mail address in n8n. **Checked (n8n version in `versions.md`, 2026-11-09; tried once by the author, logs not shipped):** the placeholder node ids (`node-1`, `node-2`, ...)
are accepted on import (`scripts/n8n_api.py import <file>` does the import; it needs an n8n API key in a file you name, see the top of that script); n8n creates its own web-address id for each webhook node when you import or activate, and the webhook keeps the same address
(`/webhook/desk-form`), because the path is kept in the file (it is a plain word, not an id). The files contain no credentials, so there was nothing to attach.
After the import you only have to switch the workflow on (the file's `"active"` flag is ignored by the import call; `scripts/n8n_api.py activate <name>` does it).
The `proof` workflow has a manual start and a web-call start; the web-call start is there so it can be run without the screen
(`POST /webhook/proof-run`).

## 0. Before you start

* n8n version used: see `versions.md` (section "n8n (demo only)"); regular mode (no queue), one process,
  SQLite inside n8n. Not checked: that the workflows work on any other version.
* n8n needs an owner account on first start. Use your own business e-mail in YOUR n8n only. The runs used its free community licence; whether the workflows here also run
  without it is not checked (they use only the Webhook, Edit Fields, HTTP Request, Code and Manual Trigger nodes).
  Never commit the e-mail, the password or the key anywhere.
* Start the demo servers and the desk first (README, "Try it"). From the same computer, n8n reaches them at the host and
  ports from the settings table in `docs/settings.md`. **If n8n runs in Docker, `localhost` inside the container is the container, not
  your computer.** The desk listens on this computer only (`DESK_BIND`). The desk answers 403 `forbidden_host` to any `Host` name it does not know (anything but `127.0.0.1`, `localhost`, `[::1]`): if n8n reaches the desk under another name, list that name in `DESK_ALLOWED_HOSTS` (`docs/settings.md`). Checked: n8n run directly with Node reached the desk and the demo servers on `127.0.0.1`. Not tried: n8n in Docker.
* **n8n's port.** The demo ran n8n on port 5679 (n8n's own default is 5678). `scripts/n8n_api.py` (setting `N8N_URL`, default `http://127.0.0.1:5679`) and the sending box of the `proof` workflow (its URL is `http://127.0.0.1:5679/webhook/desk-form`) expect that port, so start n8n with `N8N_PORT=5679` to evaluate the workflows.

## 1. The desk workflows (n8n in front of the desk)

Goal: a web form submission and an email-style message, both arriving at n8n, both end up as one request to the desk.
n8n adds nothing clever. The desk decides what is new.

**Request id** ("Request number" on the portal screens). Checked: the id inside the request body (`submission_id`, `message_id`) works, and a replay of the same id was recognised (`"replayed": true`). n8n's own execution id was not tried. The desk needs one id per
submission that stays the same when n8n re-sends it. Candidates: the form's own submission id, the email's message id, or n8n's
execution id (the execution id changes on a re-run, so it is the wrong choice for a retry). The workflows carry the id inside the
request body (`submission_id`, `message_id`), which is the stable choice.

Example inputs (made-up):

    form:    {"submission_id":"sub-0001","name":"Ada Example","email":"Ada@example.com","phone":"555 0100","company":"Example Ltd","message":"Please call me."}
    webhook: {"message_id":"msg-0001","from_name":"Ada Example","from_email":"ada@example.com","subject":"Quote","body":"Please call me."}

The **web form source** workflow ("Lead Desk: Web Form Intake") has three nodes: a Webhook node that receives the form (path `desk-form`; no authentication, local demo only), an Edit Fields node that sets `source` to `form`, takes `source_ref` from `submission_id` and copies `name`, `email`, `phone`, `company` and `message` into `payload`, and an HTTP Request node that posts `source`, `source_ref` and `payload` to the desk's `/leads` address (host and port from `docs/settings.md`). Its timeout is well above the desk's default send timeout of 5 seconds, so a slow answer ends the call in n8n later than in the desk, and a 400 or a 413 shows red in n8n.

The Webhook node returns the desk's reply (verdict `new`, `duplicate` or `review`). Checked: the reply body passes through unchanged, but **the HTTP status does not**: the caller always gets 200 when the desk answered, even where the desk said 201 or 202 (the verdict is in the body). A 400 or 413 from the desk, or a desk that cannot be reached, makes the caller get n8n's `500 {"message":"Error in workflow"}`.

The **email source** (the `webhook` source) workflow ("Lead Desk: Email Intake") has the same three nodes with the Webhook path `desk-webhook`; it sets `source` to `webhook` and takes `source_ref` from `message_id`, `payload.name` from `from_name`, `payload.email` from `from_email` and `payload.message` from `body` (`subject` is ignored by the desk).

What was checked after importing, in this order:
1. Import works. 2. The form call reaches the desk (desk answers 201, verdict `new`). 3. The email call reaches the desk
(same lead: 200, verdict `duplicate`). 4. The id is stable (the same submission twice: second reply carries `"replayed": true`).
5. A request with no email shows 202 `review` and gets no follow-up. 6. A body that is not valid JSON, or one over the size cap, shows red (400, 413).
7. n8n's automatic re-send against a desk stopped halfway: the "Send to the desk" node has "Retry On Fail" on, 3 tries, 1 second apart. With the desk stopped and started again about one second after the call, the call succeeded on the second try (about one second after the call), one lead, one CRM row, one e-mail. With the desk down for a few seconds the call failed once all three tries were used up (within about 2 seconds). The retry also repeats a 400 or 413 (3 tries each): the desk counts each rejected call 3 times. n8n's "Retry On Fail" cannot skip retries for 4xx replies, and the node's other option ("Never Error") would hide the red mark, so the 3-fold counting is an effect of this setting.

## 2. The n8n-only patterns (n8n without the desk), described only

These workflows are not shipped and no result about them is claimed here; this section only says what the patterns were. Each pattern is the desk workflow with the desk call replaced by: **dedupe step, then POST to the demo CRM, then POST to the demo outbox.** The request bodies for the demo servers are in the `/fake/...` rows of the HTTP table in `contract.md`. The demo servers ran in blind counting mode (`--crm blind --outbox blind`; the CRM default is `keyed`, which would hide a second row).

* **(a)** n8n's Remove Duplicates node on the email, with default settings.
* **(b)** a database insert that refuses a second row with the same email.
* **(c)** a Data Table upsert on the email column, where equal created and updated times mean the row is new.
* **(d)** is (a) with n8n limited to one request at a time.

## 3. The "Proof" workflow (the check inside n8n)

Purpose: people trust what they see in n8n, so the counts are shown on an n8n screen. The counts come from the demo servers' own counters, never from n8n's
own "success" mark: in the one try, n8n's executions list showed success for a duplicate caller that was answered with an error, so that list is not proof of "no double email". No n8n-only result is claimed.

The workflow "proof" (manual start, or a web call to `/webhook/proof-run`) sends ten requests for the same lead with different request numbers at once, waits a few seconds, reads the desk's counts and the demo CRM's and demo mail system's counters, and shows the result on screen, for example "emails: 1, CRM rows: 1, expected 1". **Checked: all ten requests were sent together.** The desk stored all ten within the same second (the stored time has a resolution of one second, so this shows "together", not milliseconds), and the result was 1 new lead and 9 duplicates, 1 CRM row, 1 e-mail (on the first import and again after a clean re-import, 2026-11-09). The same holds when the ten calls go through the n8n web address of the web form source. The burst script (`demo_only/burst.py`, see `contract.md`) fires the same burst without n8n. **Changed after that run, and not run in n8n:** the line `result` of the `proof` workflow used to say PASS when the demo mail system and the demo CRM each counted 1. It now says PASS only when, in addition, the desk answered all ten requests with exactly one `new` and nine `duplicate` (nine requests that got no answer with a verdict, for example a refusal or a "busy" answer, used to be able to print PASS). That new rule was checked only by running its text in node with made-up values (`tests/test_n8n_proof_result.py`); n8n itself was not started for it. What is not proven: that n8n's own expression engine accepts the text, and that it finds the output of the step "Wait for the desk to finish" from the last step. What the owner should run once: first remove the old copy of the workflow, because `scripts/n8n_api.py` has no delete command and an import only adds a second workflow with the same name (and `activate` refuses when two workflows share a name): open the n8n screen (not tried here, and its menu names were not compared with the screen), find the workflow "Lead Desk: Duplicate Test (10 at once)" in the workflow list and delete it. Then import `n8n/proof.json` again with `python scripts/n8n_api.py import n8n/proof.json`, start it by hand, and check that the screen shows `PASS` with emails 1 and CRM rows 1. If n8n shows an error on the `result` field instead, the fault is in this expression, not in the desk. A MISMATCH is not always a fault of the duplicate protection: if the desk is busy and answers one of the ten with 503 "busy" (it can, under ten at once), that request has no verdict and the line says MISMATCH; start the workflow again.

## 4. What happened with the desk behind n8n

The three workflows in `n8n/` ("Lead Desk: Web Form Intake", "Lead Desk: Email Intake", "Lead Desk: Duplicate Test (10 at once)"; files `desk-form.json`, `desk-webhook.json`, `proof.json`) were built in the n8n version of `versions.md` through n8n's REST interface and run against the desk and the demo servers. `docs/n8n-recipe.md` describes them (see "Licence" for what you may do with them). Findings, in plain words:

* **n8n answers 200 whenever the desk answered**, even when the desk said 201 (new) or 202 (review). The verdict (`new`, `duplicate`, `review`) is only in the reply body, so a caller must read the body, not the status number.
* **A desk refusal shows as an n8n error.** If the desk answers 400 (bad request) or 413 (too large), the caller gets HTTP 500 from n8n (`Error in workflow`). Because the sending box has Retry On Fail on (3 tries, 1 second apart; see `n8n/desk-form.json`), n8n repeats such a refused request 3 times, so the desk counts each refused call 3 times.
* **Restart tolerance is short.** With the desk restarted at once, the second try succeeded (the answer came after about one second). With the desk down for a few seconds, all three tries were used up and the call failed with 500. For longer outages raise the number of tries or the wait.
* **Ten at once through the `proof` workflow:** 1 email and 1 CRM row in the runs made (first import, and again after a clean re-import); the desk stored all ten requests within the same second (the stored time has a resolution of one second, so this shows "together", not milliseconds). These runs used the earlier result rule (one email and one CRM row); see section 3 for what changed afterwards and what was not run in n8n.
* The workflows import with placeholder node ids and must be switched on by hand (the import call ignores the `active` flag).
* **The desk refuses a caller that uses a real host name.** It accepts `Host: 127.0.0.1`, `localhost` and `[::1]`. If n8n (or anything else) reaches the desk under another name, for example a container name or a machine name, every request gets 403 `forbidden_host` until that name is listed in `DESK_ALLOWED_HOSTS` (see `docs/settings.md`).
* **Not checked here:** importing through the n8n screen ("Import from file"); running the workflows without a free registered n8n community licence; n8n in Docker; keyed mail mode with n8n; other n8n versions; millisecond overlap of the ten requests; the new result rule of the `proof` workflow (exactly one `new` and nine `duplicate`) in a real n8n, including how n8n finds the output of the step "Wait for the desk to finish" from the last step.
