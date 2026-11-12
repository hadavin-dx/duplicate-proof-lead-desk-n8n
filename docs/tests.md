# Crash points, promises and tests

This page holds the detail behind the short summary in the README.

## How to run the tests

The tests need development tools, installed from a lock file into the `.venv` folder so that nothing else on your computer changes.

Windows PowerShell (Python 3.13 matches the lock file), with the `.venv` folder switched on as in the README, "Try it":

    python -m pip install --require-hashes -r requirements-dev-win.txt
    python -m pytest -q                                  # the tests

Linux (Python 3.12 matches the lock file), with the `.venv` folder switched on as in the README, "Try it":

    python -m pip install --require-hashes -r requirements-dev.txt
    python -m pytest -q                                  # the tests

The SonarQube job (`.github/workflows/sonar.yml`) also measures how much of the code the tests run. On Linux, instead of the install line above, install the dev lock and the coverage lock (it adds `pytest-cov` and `coverage`) in one pip call, as the job does; pip stops with an error if a package is pinned to two different versions in the two files. Then add the coverage options:

    python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt -r requirements-coverage.txt
    python -m pytest -q --cov --cov-report=xml:coverage.xml

The settings are in `pyproject.toml` (`[tool.coverage.run]`); the report `coverage.xml` is read by the SonarQube scan through `sonar.python.coverage.reportPaths`. The browser tests skip without Playwright, so the figure counts code that only they reach as not run.

The desk alone: `PYTHONPATH=src python -m lead_desk.server`. The crash scene: `PYTHONPATH=src python scripts/crash_desk.py`, then write a point name (for example `D2`) into the flag file it prints, and the next time the desk reaches that point it ends at once. On Windows set `PYTHONPATH` with `;` between entries when two are needed (`src;.`). The tests set the path for their own processes. Writing `blind` instead of `keyed` in the start command starts a mail system that cannot recognise repeats.

The desk's settings (the `DESK_*` names, plus the extra names of the real-services script and `N8N_URL`, `N8N_API_KEY_FILE` and the `DPLD_*` names; n8n's own `N8N_PORT` and similar are in `docs/n8n-recipe.md`) are in `docs/settings.md`; the exit codes of the desk are in `docs/limits.md`.

The automatic checks in `.github/workflows/lint.yml` are: ruff (style), mypy (types), bandit (known insecure patterns), a schema check, gitleaks (secrets and other leaked values, on the files and the history), a scan for hardcoded addresses, links, absolute paths, e-mail addresses and long tokens, and the whole test suite. A second workflow, `.github/workflows/codeql.yml`, runs GitHub's CodeQL scan (Python, the portal's JavaScript and the workflow files themselves). `.github/dependabot.yml` asks for version updates of the GitHub Actions. A test (`test_workflows_are_pinned_and_limited`) fails if a workflow uses an Action that is not pinned by its full commit SHA, has no read-only top-level `permissions:` block, or uses a trigger that passes secrets to pull-request code. Two review services read the pull requests once their owner has switched them on in their own settings: CodeRabbit (configured by `.coderabbit.yaml`) and SonarQube Cloud (a scan run by `.github/workflows/sonar.yml`, configured by `sonar-project.properties`; it uses the repository secret `SONAR_TOKEN`, and SonarQube Cloud's automatic analysis must stay off for this project). These files only configure the services; whether a service is switched on, and what it found, is not stated in this repository. No result of any of these is claimed here. `tools/hygiene.py` also has an optional scan for a list of terms that the person running it supplies; it does nothing without that list. `.env` files are never used: the demo needs no secrets.

## Browser tests

Some tests drive the portal in a real web browser. For that they need Playwright (a Python package that remote-controls a browser) and a copy of the Chromium browser. Without them these tests are skipped, not failed. They are in `tests/test_portal_browser.py`, `tests/test_portal_lead_pages.py`, `tests/test_stage_confirm_browser.py`, three in `tests/test_portal_wording_and_masking.py`, a few in `tests/test_mode_safety.py`, and the layout and look tests (`tests/test_portal_layout.py`, `tests/test_portal_look.py`), which use the same browser helpers. **The checks in `.github/workflows/lint.yml` install only `requirements-dev.txt` and there is no browser job yet, so they do not run the browser tests;** the masking promise P10 rests on them, so run them on your own computer.

Playwright is not in the development lock files. It has its own optional lock file, `requirements-browser.txt` (Linux x86_64, Python 3.12) or `requirements-browser-win.txt` (Windows, Python 3.13), with hashes. It holds Playwright 1.63.0 and the three packages Playwright needs (greenlet, pyee and typing-extensions; typing-extensions has the same version as in the development lock). Nothing else is needed: the two sidebar tests read the colours of a screenshot inside the browser itself, so no image library such as Pillow is required. Install it after the development lock, in the same `.venv`:

    python -m pip install --require-hashes -r requirements-dev.txt
    python -m pip install --require-hashes -r requirements-browser.txt      # on Windows: requirements-browser-win.txt
    playwright install chromium

Two settings you may need before the tests run, only if the browser or Node is not in its default place (Node is a program that runs JavaScript; some tests run the portal's script with it). Linux (bash):

    export PLAYWRIGHT_BROWSERS_PATH=<folder that holds the browser>
    export PATH=<folder that holds node>:$PATH

Windows (PowerShell):

    $env:PLAYWRIGHT_BROWSERS_PATH = "<folder that holds the browser>"
    $env:Path = "<folder that holds node>;" + $env:Path

Then run the tests (the `-rs` option prints one line for each reason a test was skipped):

    python -m pytest -q -rs

Measured on Linux with Python 3.12, one run, 2026-11-12: a new `.venv` with both lock files installed with `--require-hashes`, Chromium build 1243 and Node 22 (v22.23.4) already present in folders of their own (written as placeholders above), so `playwright install chromium` itself was not run there. That step on a computer without a browser was not measured, and Windows was not measured (the Windows lock file was made but never installed).

* **The Chromium build must match the Playwright version.** A "build" is the numbered release of the browser that Playwright downloads. Playwright 1.63.0, the version in the lock, looks for build 1243, which is what `playwright install chromium` fetches for it. A newer Playwright wants a newer build: the Playwright 1.65.1 package names build 1254 (both build numbers were read from the `playwright/driver/package/browsers.json` file inside each package on 2026-11-12). If only build 1243 is installed, a newer Playwright does not find its build and every browser test is skipped with a "BrowserType.launch: Executable doesn't exist" message. Keep the Playwright version in the lock and the installed build together.
* **Where the browser is not in the default place,** set `PLAYWRIGHT_BROWSERS_PATH` to the folder that holds it: the tests start the browser with no path of their own.
* **Counts from earlier full runs, each on its own commit (Linux, Python 3.12):** without Playwright, 2349 passed and 116 skipped (two earlier review runs, on commits bee6fff4 and 43110e7; 2465 tests in all). With Playwright 1.63.0, the run on commit 43110e7 got 2 failed, 2460 passed and 3 skipped; the 2 failures were the two sidebar tests, which at that time needed Pillow. A run on commit 558d37f plus the earlier documentation fixes and this change's lock files, with Playwright, got 2462 passed and 3 skipped (2465 tests) in 958 s. The 3 skipped are: the secret scan in `tests/test_hygiene.py` (no `gitleaks` program installed), the load measurement (it runs only when `LEAD_DESK_LOAD_N` is set), and one test that runs only on Windows.
* **A full run on this lock:** Linux, Python 3.12, commit 2ce1f98 (the head of the branch `pr-3-tests` on 2026-11-12, before the coverage commit 0841f36) plus this change (the two browser lock files, the sidebar test change and the allowance lines), Playwright 1.63.0, Chromium build 1243, Node 22 (v22.23.4), no `gitleaks` program on the path, one serial process, 2516 tests: 2513 passed and 3 skipped in 16 minutes 33 seconds (993 s), no failure. The 3 skipped are the same three as above. The two sidebar tests pass in it: the browser reads the screenshot's pixels itself and no image library is installed. **Why 2462 became 2513:** commit 2ce1f98 holds 51 more tests than 558d37f (2516 against 2465, both counted with `python -m pytest --collect-only -q`): 49 in the new file `tests/test_except_clause_hierarchy.py`, 1 more in `tests/test_lead_crm_copy.py` and 1 more in `tests/test_portal_mode.py` (2465 + 51 = 2516; 2462 + 51 = 2513). Commit 0841f36 (the coverage settings) collects the same 2516 tests. That commit was not run in full. It adds a `[tool.coverage.run]` section in `pyproject.toml`, which only the `coverage` program reads, and it edits `exceptions.txt` and `.gitleaks.toml`, which `tests/test_hygiene.py` reads; it changes no other test or test setting. The hygiene tests were run with a real `gitleaks` program on the final tree and passed (10 tests passed with gitleaks 8.30.1 and DPLD_REQUIRE_SCAN=1).
* **Skips reported without Playwright (eight test files, 0841f36 plus this change):** the eight files are `test_mode_safety.py`, `test_portal_browser.py`, `test_portal_lead_pages.py`, `test_portal_mode.py`, `test_portal_wording_and_masking.py`, `test_portal_layout.py`, `test_portal_look.py` and `test_stage_confirm_browser.py`. Two runs, 2026-11-12, one process each, Python 3.12, no Playwright installed: with Node on the path, **114 passed and 113 skipped** (82.7 s and 83.0 s in two runs); with Node not on the path, **92 passed and 135 skipped** (80.6 s). The difference, 22 tests (114 - 92), is the set of tests that need Node: 1 in `test_mode_safety.py` (line 174), 2 in `test_portal_lead_pages.py` (line 488, "no node") and 19 in `test_portal_mode.py`. The 113 browser skips are counted by the line where the skip is reported, not by test file (22 at line 109 of `test_mode_safety.py`, 28 at line 27 of `test_portal_browser.py`, 60 at line 113 of `test_portal_lead_pages.py`, 3 in `test_portal_wording_and_masking.py` at lines 171, 204 and 257; the helper functions that skip sit in those files, so tests from other browser files are counted there too).
* **No full run without Node was done on this commit.** The only complete runs are the ones named above, all with Node on the path. Nothing in this document is a measured full-suite count for a computer without Node. To measure it, run on a computer with the lock installed (Linux bash; give that one command a `PATH` that does not hold the folder with Node, for example `PATH=$(dirname "$(command -v python)") python -m pytest -q -rs`, then check that `command -v node` prints nothing under that `PATH`). The number of "node is not installed" and "no node" lines in its output, 22 for the eight files, shows how many tests were skipped for that reason.
* **A count of 2329 passed and 135 skipped (from the project owner's own computer):** its cause is not known, and no figure here confirms it. The nearest fit, as an inference only, is a run without Node on the path (Node missing skips 22 tests in the eight files above), but the totals do not match a full run of this commit (2516 tests; 2329 + 135 = 2464), so the two numbers are not reconciled. What is unknown: that computer's operating system, whether `gitleaks` was installed there, whether Node was present, and the exact commit. If you see a count near 2329/135, run `python -m pytest -q -rs` and look for "node is not installed".

## Quality checks and what to do when a check fails

The commands for ruff, mypy, bandit and the schema check are listed under "Quality commands" in `contract.md`, and the same commands, in order, are in `.github/workflows/lint.yml` (that file also has the secret scan and the scan for hardcoded literals). Run them from the repository folder with the `.venv` folder switched on.

When a check fails: read the last lines of its output first; they name the file and line. For a test, run it alone (`python -m pytest -q tests/test_name.py::test_name`). A test that did not run is not a pass: to see why tests were skipped, run `python -m pytest -q -rs`, which prints one line per skip reason (for example "could not import 'playwright.sync_api'", "node is not installed", "no gitleaks program on PATH"). To make a missing secret scan a failure instead of a skip, set `DPLD_REQUIRE_SCAN=1` and `GITLEAKS_BIN` to the program's path.

## What each promise rests on

The desk was stopped on purpose at each of ten crash points and restarted; nine of them are simulated by an exception raised inside the program, and only D2 is a real process kill (`docs/limits.md`). The table says what had happened at each point and what the result was. "Keyed" and "blind" are explained in the README under "Words used here". The two columns are the kind of MAIL system. The outcome at C0 and C1 depends on the kind of CRM instead (both mail kinds gave the same result in the tests, `tests/test_flow.py`, `EXPECT_MORE`), so those two rows say so in words.

| Point | What had happened | Keyed mail system | Blind mail system |
|---|---|---|---|
| A | the receipt is written, no verdict yet | one email | one email |
| A2 | the verdict and the lead are written inside the open transaction but not yet committed (a real kill here loses them; the receipt stays open and the restart decides it) | one email | one email |
| B | the verdict is recorded | one email | one email |
| L | the AI label is stored | one email | one email |
| C0 | the desk has noted that it is about to write to the CRM, but has not written yet | with a keyed CRM (either mail system): the CRM is asked and holds one row, one email. With a blind CRM (either mail system): it is not asked again after a crash (a put may have started), so the CRM holds no row, no email is sent, and the follow-up is UNKNOWN (the reason is `crm_unconfirmed`) until a person looks | (same: this row depends on the CRM kind, not the mail kind) |
| C1 | the CRM write came back but the desk has not yet noted it | with a keyed CRM (either mail system): asked again with the same key, one email. With a blind CRM (either mail system): never asked twice, so one CRM row, no email, and the follow-up is UNKNOWN until a person looks in the CRM | (same: this row depends on the CRM kind, not the mail kind) |
| D1 | at the start of the send step, before the send is marked | one email | one email |
| D1b | the send is marked but the mail system has not been called | one email | no email, UNKNOWN |
| D2 | the email was sent but the result is not yet noted | one email | one email, follow-up UNKNOWN (the desk cannot tell whether it left, so it does not resend) |
| D3 | the result is noted | one email | one email |

The portal tests that run in Node (`tests/test_portal_mode.py`, the proof-panel probe in `tests/test_portal_lead_pages.py`) skip when `node` is not installed; GitHub's Ubuntu runners ship Node, so they should run in CI, but no CI run has been seen. The browser tests need Playwright, which `lint.yml` does not install, so they skip in CI and run only on a computer where you install it.

(Exact places: `src/lead_desk/hooks.py` and `contract.md`.) "D1b" is the moment just after the send is marked and just before the mail system is called. The stops are simulated inside the program for all ten points; an eleventh point, N1 (a reminder email was sent, its result not yet recorded), is simulated too and tested in `tests/test_reminder_email.py`. A real process kill (the desk process ends at once) was done only at D2, twice with the demo mail system over HTTP (keyed and blind) and once with a real mail catcher over SMTP (blind sender). The headline "one lead, one CRM row" is NOT claimed for a blind CRM at C0.


The table below says which tests show each promise. "Tests named" means the tests exist and are named here; it is not a run result.

| # | Promise, in plain words | Tests that show it |
|---|---|---|
| P1 | (a) A second submission with a different request number and the same email is recorded as a duplicate with the NAMES of the fields that differ; no second CRM row, no second email. (b) The same request number sent again gets the original answer marked `replayed` and changes nothing. | (a) `test_cross_source_dupe`, `test_dupe_diff_first_untouched`, `test_write_once_triggers`; (b) `test_replay_same_source_ref`, `test_concurrent_same_source_ref` |
| P2 | A duplicate never reaches the AI step: the demo AI's call counter does not go up for it. | `test_dedupe_before_ai` |
| P3 | A submission with no name, no email, an invalid email or a wrongly typed field goes to the review list with a reason and gets no follow-up. | `test_incomplete_to_review` |
| P4 | Stopped at each crash point and restarted: in these tests no run produced more than one lead, more than one CRM row or more than one email (see the crash-point table for what a blind CRM or mail system does). A wrong "not sent" answer by a person can cause a second email on a blind mail system. | `test_crash_resume_each_point` and `test_crash_resume_after_*` in `tests/test_flow.py`: together they stop the desk at each of the ten crash points, with keyed and with blind mail systems (C0 and C1 also with keyed and blind CRMs); real kills: `tests/test_http.py::test_crash_d2_subprocess` (keyed and blind, demo mail over HTTP), `test_crash_d2_subprocess_with_*` in `tests/test_http.py` (real mail catcher) |
| P5 | Ten requests with different request numbers and the same email, fired at once straight at the desk by a script: one new lead, nine duplicates, one email; the counts match the number the script says it sent. Larger bursts are reported separately in `docs/limits.md` (measurements, not part of the test run). | `tests/test_http.py::test_ten_parallel`, `test_reconcile_counts` |
| P6 | On a blind mail system, a send that may or may not have happened is marked UNKNOWN, listed for a person and never resent by the desk; Retry is refused for it. Retry pressed twice at once on a failed follow-up gives one email, also on a blind system. | `test_unknown_blind`, `test_failed_retry`, `test_retry_twice_concurrent` |
| P7 | A later message never changes the first record: the lead's first details stay exactly as they arrived, however many later messages come and whatever they say. | `test_later_first_record_untouched`, `test_dupe_diff_first_untouched`, `test_write_once_triggers` |
| P8 | The lead panel lists the later messages from the same email address, newest first (the newest 50; the total is always shown): when each arrived, how long after the first message, and each field that is new or different, with the text exactly as it was sent except white space at the very start and end (spaces, tabs, line breaks and other Unicode white space), which is removed by the same rule as for the first record. | `test_later_values_word_for_word`, `test_later_seconds_after_first`, `test_later_lower_receipt_number`, `test_later_order_by_receipt_number`, `test_later_burst_ten_each_own_values`, `test_lead_detail_lists_at_most_the_cap`, `test_later_reply_size_is_bounded`, `test_time_after_first_words` |
| P9 | The desk does not rewrite or summarise later messages, and no AI step runs for them: the list is the record. | `test_later_messages_never_call_the_ai`, `test_later_reply_keys_are_fixed`, `test_dedupe_before_ai` |
| P10 | Lead names, email addresses, phone numbers, messages and request numbers are masked on screen by default, in the lead list and in the lead panel, for the first record and for every later message. While they are masked, the search does not look in names or email addresses (it matches the company and the lead number only; a request number is not searched while masked), and its placeholder says names and addresses are searchable after hiding is turned off. Switching "Hide names and email addresses" OFF shows them and allows searching by name and by email address; switching it back ON masks the list and an open panel straight away and clears a search that was typed. The choice is kept for the browser tab's session only (session storage), so a new session starts masked. The small eye button beside a masked value shows that one value only, for 30 seconds at most, and does not turn on searching by name or address. On the demo controls page the lead pick list and the retry list show neutral labels ("Lead 1") or hidden names while masking is on. Companies, lead owners and approver names stay visible. No log line of the desk prints an email address, phone number or message: every log call in the code is checked, and a test runs later messages and a failed read with test values and finds none of them in the log or the error output. Masking is on screen only: the answer the page reads from the desk (visible in the browser's developer tools) still carries every value, and anyone who can reach the desk can ask it to search by name or email address. | the browser tests listed in "Browser tests" below and `test_personal_hidden_rule`, `test_reveal_one_value_rules`, `test_later_panel_rules_in_the_source`, `test_leads_email_search_needs_reveal`, `test_name_search_needs_reveal`, `test_demo_pick_lists_neutral`, `test_log_calls_carry_no_values`, `test_later_messages_log_no_values` |

The promises P1 to P10 are about the desk. What n8n did is reported separately below. Also tested, not a headline promise: odd input and requests (`tests/test_input_edge_cases.py`: half-character escapes, deeply nested JSON, `HEAD`, `OPTIONS` and malformed requests get a JSON answer, an email that grows when lower-cased, two drives of one new lead at once).

What `/counts` adds up: `received = new + duplicate + review + open`. Some counters live only in the desk's memory and start again at 0 after a restart, and the database refuses to change or delete receipts, leads and sent follow-ups. The details, with test names, are in `docs/limits.md` ("Counters and database rules").

The checks after a load run (`load_tools/`, see `load_tools/README.md`): a book in which every follow-up ended UNKNOWN fails whatever allowance is given (`test_all_unknown_book_*`); one address receiving more than one message fails (`test_two_messages_to_one_address_*`); a message whose text hash is not the follow-up text fails (`test_a_message_text_that_*`).

"Mutation check": the code is deliberately broken in one known way (on the test side only) and the named test must fail with the named symptom. `test_mutant_no_unique_key` removes the database rule that two leads cannot share an email and the lookup; the duplicate test must fail with "leads == 1" and "got 2". `test_mutant_no_claim` removes the check that only one retry may take a follow-up; the retry test must fail with "emails == 1" and "got 2". This shows the tests notice these two faults; it is not proof that the code is correct.

## Terms for developers

* **Atomic insert**: a database write that either happens completely or not at all.
* **UNIQUE key**: a database rule that refuses a second row with the same value (here, a second lead with the same email address).
* **Test doubles**: stand-in parts (the demo CRM, the demo mail system, the pre-written AI replies) used only in tests and demos.
* **CRM dump**: the list of records the demo CRM holds, read back from `/fake/crm`.
* **Host and Origin check**: the desk refuses web requests that name a wrong address or come from another web site.
* **UNKNOWN**: the desk is not sure whether a follow-up email left; it does not resend, and a person must check.

