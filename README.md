# duplicate-proof-lead-desk

**In plain words**

1. A sales lead can arrive twice: the same person fills in the web form and then writes by email, or the same request is sent again after a crash.
2. A business wants one follow-up email and one customer record (one "CRM row") for that person, not two.
3. This repository is a small demonstration of one way to get that: a program called "the desk" that writes every request down before it acts on it.
4. You can run it on your own computer in a few minutes. It uses made-up people and small stand-in programs for the CRM, the mail system and the AI step, so nothing real is sent.
5. What you will see: send the same lead twice and through two doors, and get one "new", one "duplicate" and one email. A web page (the portal) shows the same facts.
6. What is claimed: in the tests of this repository, with these stand-ins, the desk never sent a second email for the same lead by itself. If it cannot tell whether an email left, it does not guess and does not resend: it lists the follow-up for a person to check.
7. What is not claimed: that this works with any real CRM or mail system, that it suits production, or anything about load beyond what `docs/limits.md` states. It is not software to install for your business.
8. Who it is for: a business owner who wants to see the idea working, and a RevOps person or n8n freelancer who wants to read the code, the tests and the n8n workflows.
9. Licence: all rights reserved; you may read the code and run it on your own computer to evaluate it (see "Licence" at the end). Not legal advice.
10. Made by the founder of Hadavin (hadavin.com), an AI studio, with AI coding tools, and reviewed by the author.

## What you will see

After the steps in "Try it" below, three things confirm that it works. Every number below comes from the demo programs (the stand-ins), not from a real CRM or mail system.

* The first lead gets `"verdict": "new"`. The same request sent again gets `"replayed": true` and changes nothing. The same person through the other door gets `"verdict": "duplicate"`.
* The counts page shows 2 requests received, 1 new, 1 duplicate.
* The demo mail system holds exactly one email.

In a browser, http://127.0.0.1:8765/ui/ shows the portal: overview, leads, items that need a person, a request log, and what data the desk stores. See `docs/portal.md`.

## Words used here

* **Lead**: a person who asked to be contacted. **Same lead** means the same email address, trimmed of spaces and lower-cased, nothing more.
* **Follow-up**: the one email sent for a new lead, plus the CRM row that goes with it.
* **CRM**: the customer-record system where a lead is stored as a row.
* **Demo stand-ins**: small programs in this repository (`demo_only/`) that play the CRM, the mail system and the AI step and only count what they receive. Some of their web addresses and settings still contain the word `fake`.
* **The desk**: the program in `src/lead_desk`. **Desk's records**: its SQLite database file.
* **n8n**: a workflow tool where steps are drawn as boxes joined by lines. n8n is not part of this repository.
* **Request id**: a label the sender puts on each submission. The same label twice means "this is a repeat". The portal calls it "Request number".
* **Receipt**: the desk's first note about a request, written before anything else happens.
* **Verdict**: the desk's answer: `new` (first time this email is seen), `duplicate` (seen before under another request number) or `review` (incomplete, a person must look).
* **Replayed**: the same request number came again, so the desk gave back its stored answer and changed nothing.
* **Door**: the way a request arrived: `form` or `webhook`. The portal calls it "Source".
* **Keyed and blind**: a keyed mail system recognises a repeat of the same send key and sends only once; a blind one does not. The same two words apply to a CRM: a keyed CRM keeps one row however often it is asked with the same key; a blind CRM stores a row every time.
* **UNKNOWN**: the desk cannot tell whether a follow-up email left. It does not guess and does not resend; a person must check.
* **Crash point**: one of ten named moments in the desk's work (and an eleventh, N1, for the reminder email) where a test stops the desk on purpose, for example `D2`, the moment after an email was sent but before the desk noted it. The table is in `docs/tests.md`.
* **Reaper**: a background job inside the desk. It puts stuck follow-ups back in line, retries a refused send a few times with growing waits (1, 2, 4 and 8 seconds, up to 5 automatic tries in all), and settles receipts left open.
* **Webhook**: a web address that starts a workflow when something is posted to it.

## Try it

You need Linux or Windows (macOS was not tried), Python 3.12 or newer and git, and ports 8765 and 8766 on your computer must be free. The desk needs no extra packages.

1. On this repository's GitHub page press the green Code button and copy the address it shows.
2. Run `git clone <that address>`, then `cd` into the new folder.
3. Open ONE terminal there and run the lines of your system below, in order, in that same terminal (the first lines make a private Python folder, `.venv`, and switch it on for this terminal only; the scripts call `python`). If you open a new terminal later, switch `.venv` on again.
4. On Debian or Ubuntu you may first need `sudo apt install python3-venv`.
5. The start script starts the two demo servers and the desk as background programs and returns at once; they keep running until you run the reset script (last line of each block), which stops them.

**Linux**

    python3 -m venv .venv && . .venv/bin/activate
    export PYTHONPATH=src                      # tells Python where the code is
    bash scripts/start_all.sh keyed            # starts the demo servers and the desk in the background
    for i in 1 2 3 4 5 6 7 8 9 10; do curl -sf http://127.0.0.1:8765/counts >/dev/null && break; sleep 1; done; curl -i http://127.0.0.1:8765/counts # waits up to 10 seconds; it works when the first line starts with "HTTP/1" and has 200, and the numbers are all 0
    python scripts/feed.py --source form --index 1       # sends one lead; prints 201 ... "verdict": "new"
    python scripts/feed.py --source form --index 1       # the same request again: 201 and "replayed": true, nothing changes
    python scripts/feed.py --source webhook --index 1    # same person through the other door: "verdict": "duplicate"
    curl http://127.0.0.1:8765/counts              # received 2, new 1, duplicate 1 (the repeated request number is answered from the stored answer, not counted again)
    curl http://127.0.0.1:8766/fake/outbox         # "count": 1  (one email, not two)
    bash scripts/reset.sh                          # stops what start_all started and DELETES the desk's records and the logs (only inside ./data)

Run both scripts with `bash`, as above: they use bash's `[[ ]]` tests, so `sh scripts/start_all.sh` and `sh scripts/reset.sh` stop with "run this script with bash" (exit 2) in any shell where `[[` does not run, for example where `sh` is dash (Debian and Ubuntu). A shell that does run `[[` (busybox sh, zsh) goes on.

**Windows PowerShell**

    py -3 -m venv .venv                        # or python -m venv .venv; `py -3` avoids the Microsoft Store stub
    Set-ExecutionPolicy -Scope Process RemoteSigned   # only for this window: lets this window run the two local scripts below (a policy that blocks all scripts would stop them)
    .venv\Scripts\Activate.ps1
    $env:PYTHONPATH = "src"                    # tells Python where the code is
    .\scripts\start_all.ps1 keyed    # prints "started: ..." with two process ids
    Start-Sleep 3; Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/counts | Select-Object -ExpandProperty Content    # should answer with numbers (all 0)
    python scripts\feed.py --source form --index 1       # prints 201 ... "verdict": "new"
    python scripts\feed.py --source form --index 1       # the same request again: 201 and "replayed": true, nothing changes
    python scripts\feed.py --source webhook --index 1    # same person, other door: "verdict": "duplicate"
    Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/counts | Select-Object -ExpandProperty Content    # received 2, new 1, duplicate 1 (the replayed id is not counted again)
    Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8766/fake/outbox | Select-Object -ExpandProperty Content    # "count": 1
    .\scripts\reset.ps1        # stops what start_all started and DELETES the desk's records and the logs


If PowerShell refuses to run the scripts at all (a policy set by your organisation), start the two programs by hand instead, each in its own window with `PYTHONPATH` set as in `contract.md`, "Run commands": `python -m demo_only.fakes_server --outbox keyed --port 8766` and `python -m lead_desk.server`.

You have the demo working when the counts and the outbox match the comments above: 2 requests received, 1 new, 1 duplicate, and exactly one email. If `/counts` does not answer within a few seconds, read `data/desk.log` (on Windows also `data/desk.err.log`), run the reset script and start again. The desk answers 404 for `/` and `/health` on purpose: it has no such pages. On Windows, starting again on the same port right after a stop can fail for about two minutes (the operating system keeps the port closed for a while). The `/fake/...` paths are the demo servers' own addresses and keep that word in the URL.

The reset script refuses, with one line of reason and nothing deleted, in these cases (exit code): 2 the database is not inside `./data` (or `RESET_STOP_WAIT_SECONDS`, which both `scripts/reset.sh` and `scripts/reset.ps1` check before they stop or delete anything, is not a whole number from 0 to 999 without a leading zero; an empty value counts as not set and means 10), 3 a process still holds the desk's database, its lock file or data folder cannot be looked at, or (Linux) `ps` is not installed, 4 something still answers on the desk's or the demo server's port, 5 a process did not stop in time, 6 a pid file names another program.

## What is claimed, and what is not

Claimed, for this demo setup only:

* The same request number sent again gets the stored answer and changes nothing.
* The same email under a different request number is a duplicate: no second CRM row, no second email, and the AI step is not asked.
* An incomplete request goes to a review list with a reason and gets no follow-up.
* The desk was stopped at ten named crash points (and an eleventh, N1, for the reminder email) and restarted. Of the ten, nine are simulated by an exception inside the program; only one (D2) is a real process kill. The tests assert at most one lead, one CRM row and one email. Where it cannot tell whether an email left, the follow-up is listed as UNKNOWN for a person.
* The desk does not send a follow-up twice by itself. An unconfirmed send is listed for a person. A person who answers "not sent" wrongly on a blind mail system can cause a second email.

Not claimed:

* Anything about a real CRM, a real mail system or a real AI model. Only `scripts/hubspot_probe.py` touched a real service (one free HubSpot account, logs not shipped); everything else, including the desk's HubSpot adapter, ran against stand-ins. The "keyed" mail system is a simulation; a real provider may remember a send key for a shorter or longer time.
* Production fitness, a legal opinion, a guarantee of any kind.
* The same person with two email addresses is two leads; two people sharing one address are one lead.
* The desk is for one computer and one process, has no login, and listens on this computer only unless you change that. No penetration test was done.
* Masking in the portal is on screen only: lead names, email addresses and phone numbers are hidden on the page by default (the switch "Hide names and email addresses"; its state lasts for the browser tab's session), the search does not look in names or email addresses while they are hidden, and the answer the page reads from the desk still carries every value, names included (see `docs/portal.md`).
* Loads of 10,000 requests were not run. The measured figures in `docs/limits.md` come from one Windows computer, were tried by the author, and depend on the machine; their raw logs are not shipped.
* The desk's time stamps use the computer's clock, so a clock that jumps by 30 seconds or more can make the desk ask again for a follow-up that was still being sent (`docs/limits.md`, item 5).
* The full list, with test names, is in `docs/limits.md`.

## Optional features

They are off in the desk itself; the demo start scripts switch on the lead flow.

* **Approval mode** (`DESK_LATER_MODE=approval`): a repeat that brings new details can be approved or denied by a person picked from a list of approvers. Approving or denying only records that choice: the approver is a name picked from the list and is not verified, and nothing is executed by it. Off by default; then a repeat is only listed. The reply shapes are in `contract.md`, the settings in `docs/settings.md`.
* **Lead owner, lead stage and reminder** (`DESK_LEADFLOW_FILE`): each new lead gets an owner from a short list, can be moved forward through named lead stages, and one reminder email is sent when a lead is still before the "reminder until" step after the wait (the follow-up state is not looked at). Only leads created while the feature is on are covered (`docs/limits.md`). Off when the setting is unset or is the word `off`. The demo start scripts switch it on with `demo_only/leadflow.json`; set `DESK_LEADFLOW_FILE=off` to turn it off there.

> **WARNING: this write can start automations in the CRM you connect.** When lead stages are on, the desk writes the contact's lead status field (in HubSpot `hs_lead_status`). Any automation that watches that field (a workflow, sequence, lead rotation, scoring rule, notification, integration or report) may react to our write, for example by emailing the contact, creating a task, changing the owner or sending a chat alert. The desk neither sees nor controls those automations, and it does not know what the automations of the CRM's owner do. The feature is OFF unless `DESK_LEADFLOW_FILE` is set to a file; the only values the desk writes are the lead status values named in that file's stage list. **Required before you point this at a real CRM:** the CRM admin lists every automation that reacts to lead status changes and confirms each one is safe or switched off, and the first test uses one made-up contact. The desk has no sign-in, so any program that can reach its port could cause this write (the step route) or create contacts and send mail (`POST /leads`, the review routes). With any real service set (a real CRM, the real mail sender or the live AI step) the desk therefore starts only on a loopback address and refuses any other `DESK_BIND`.

* **Not in this demo:** handling of later messages by an AI step (the desk never rewrites or summarises a later message), and suggested replies.

## About n8n

The first question of the project was whether n8n on its own handles duplicates and a crash between the CRM step and the email step. A few n8n-only patterns were tried once, by the author, on one computer with one n8n version (`versions.md`), against the demo stand-ins; only the simplest patterns were tested and no other tool was compared. The workflows and logs of those tries are not in this repository, so no result is stated here and none can be reproduced from it. What is shipped: three n8n workflows that put the desk behind n8n (`n8n/`), described in `docs/n8n-recipe.md`, which you can import into your own n8n unmodified to evaluate them. n8n has its own licence; read it before you use n8n.

## Tests and more commands

The tests need development tools, installed from a lock file into the `.venv` folder so that nothing else on your computer changes. The commands for Windows and Linux are in `docs/tests.md`; the test command itself is `python -m pytest -q`. Supported: Windows with Python 3.13 and `requirements-dev-win.txt`, or Linux x86_64 with Python 3.12 and `requirements-dev.txt`; any other combination was not tried. If pip prints a hash mismatch, you are using the wrong lock file or Python version for your computer. The full run takes about 20 minutes, and it needs a git clone (some tests read the list of tracked files with git).

The desk alone: `PYTHONPATH=src python -m lead_desk.server`. Writing `blind` instead of `keyed` in the start command starts a mail system that cannot recognise repeats. On Windows, set `PYTHONPATH` with `;` between entries when two are needed (`src;.`).

## Where to read more

* `docs/tests.md`: the crash-point table, the ten promises with the tests that show them, the mutation check, how to run the tests.
* `docs/portal.md`: the portal pages, what only reads and what writes, the Host and Origin check.
* `docs/limits.md`: what is not claimed in full, load figures, counters, exit codes.
* `docs/settings.md`: every setting.
* `docs/n8n-recipe.md`: the n8n workflows.
* `docs/real-services.md`: an optional script to try the desk against a free HubSpot account and two mailboxes (this script was not run against real services: only `scripts/hubspot_probe.py` touched a real service, on one free HubSpot account, logs not shipped).
* `docs/why-this-code.md`: why each part is own code, and where another CRM, mail service or AI service plugs in. `contract.md`: the exact replies the code and tests agree on. `versions.md`: the versions needed to run.

## Layout

* `src/lead_desk`: the desk.
* `demo_only/`: everything that exists only for demos and tests (demo servers, pre-written AI replies, burst script, canned leads); the desk reaches it only through `src/lead_desk/loader.py`.
* `tests/`: the tests. `scripts/`: run and demo helpers.
* `load_tools/`: a load driver, the checks that run after a load, a small test mail server (`load_tools/README.md`).
* `tools/`: the repository scan (`tools/hygiene.py`) and the n8n export scrubber, for the licence holder's own use.
* `n8n/`: the three n8n workflows. `docs/`: the pages above.

The follow-up email is fixed demo text: the subject is `Thanks for getting in touch, {name}` and the body is one line ending in `(DEMO EMAIL)`; the AI label is not part of it. The checks configured to run on pull requests and pushes to main are listed in `docs/tests.md`; the browser tests are not among them (they need Playwright), so run them on your own computer.

## Trademarks

HubSpot, Gmail and Google, n8n and Anthropic are names of their owners. This project is not affiliated with, endorsed by or sponsored by any of them; the names appear only to say which services the desk and its check scripts talk to.

## Licence

See `LICENSE`. All rights reserved by the author. You may view the code, download it, run it on your own computer for evaluation and testing, and fork it on GitHub (to the extent GitHub's Terms of Service give that right); nothing else without the author's written permission. In particular you may not hand on copies, publish changed copies, use it commercially or offer it as a service. The licence covers everything in this repository: the code, the documents, the n8n workflows and the scripts. The workflows in `n8n/` may be imported unmodified into your own n8n to evaluate them. Editing them, building variants, or sharing or publishing exports needs the licence holder's written permission. To ask for permission, write to the address in `SECURITY.md` or through the GitHub profile of the organisation that publishes this repository. No warranty. The author may record and publish demo videos. The full text is in `LICENSE`; it has not been reviewed by a lawyer. Outside contributions (pull requests) are not accepted. See `SECURITY.md` to report a problem.
