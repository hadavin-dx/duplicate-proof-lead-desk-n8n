# Tables generated from the raw files (harness/summarise.py). Do not edit by hand.

## Summary (counted from the raw files)

* Variant a, ten at once (attempts 20, invalid 0): 18 of 20 (95% interval 69.9 to 97.2 percent) ended with exactly 1 CRM row and 1 email; runs where the replay added posts: 0 of 20.
* Variant b, ten at once (attempts 20, invalid 0): 20 of 20 (95% interval 83.9 to 100.0 percent) ended with exactly 1 CRM row and 1 email; runs where the replay added posts: 0 of 20.
* Variant c, ten at once (attempts 75, invalid 55): 20 of 20 (95% interval 83.9 to 100.0 percent) ended with exactly 1 CRM row and 1 email; runs where the replay added posts: 0 of 20.
* Variant d, ten at once (attempts 55, invalid 35): 20 of 20 (95% interval 83.9 to 100.0 percent) ended with exactly 1 CRM row and 1 email; runs where the replay added posts: 0 of 20.
* Variant a kill runs: Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).
* Variant a kill runs (attempts 15, invalid 0): CRM request in flight (first step): 5 of 5 ended with 1 CRM row and 0 emails after one retry press; the two email points: 9 of 10 ended with 1 CRM row and 1 email.
* Variant b kill runs: Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).
* Variant b kill runs (attempts 15, invalid 0): CRM request in flight (first step): 5 of 5 ended with 1 CRM row and 0 emails after one retry press; the two email points: 10 of 10 ended with 1 CRM row and 1 email.
* Variant c kill runs: Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).
* Variant c kill runs (attempts 15, invalid 0): CRM request in flight (first step): 5 of 5 ended with 1 CRM row and 0 emails after one retry press; the two email points: 10 of 10 ended with 1 CRM row and 1 email.
* Variant d kill runs: Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows; 2 more attempts kept in invalid/). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).
* Variant d kill runs (attempts 17, invalid 2): CRM request in flight (first step): 5 of 5 ended with 1 CRM row and 0 emails after one retry press; the two email points: 10 of 10 ended with 1 CRM row and 1 email.

## Attempts, valid runs and invalid reasons (nothing is dropped without being counted)

An invalid attempt is not a failure of n8n and is not hidden: it is a run that did not meet the rule for a valid run, so it is not counted in any result above. It is listed here with its reason. For the 2026-11-09 run, "kept in invalid/" means that the original run moved those attempts to the folder `published_run/invalid/` and the text in brackets is the label of the file they were moved to; a new run keeps every attempt in its normal result file and names the reason.

An attempt is invalid when: no ten executions listed or no overlap between them (variant d: ten not sent within 100 ms), a wait for quiet ran into its time limit (timeout) or the stand-in could not be reached, or for a kill run the trigger was never seen, no process was killed (taskkill failed or the port was still busy), n8n did not restart, or an execution still showed running after the kill and the retry although nothing else moved (stale_running_after_kill: counted and listed, never part of the valid headline).

Test configuration a default (ten at once): attempts 20, valid 20, invalid 0 (none).
Test configuration b default (ten at once): attempts 20, valid 20, invalid 0 (none).
Test configuration c default (ten at once): attempts 75, valid 20, invalid 55 (kept in invalid/ (SETUP PROBLEM IF false) x20, kept in invalid/ (SETUP PROBLEM2 executions list empty after login race) x35).
Test configuration d limit1 (ten at once): attempts 55, valid 20, invalid 35 (kept in invalid/ (35 attempts invalid by overlap rule) x35).
Of the 35 attempts of d kept in invalid/ (counted here from their raw rows): 1 got HTTP 404 on all ten requests (the webhook was not registered yet: a harness fault) and 34 have no overlap data (n8n listed no executions, so the overlap rule could not be applied).
Test configuration a (kill runs): attempts 15, valid 15, invalid 0 (none).
Test configuration b (kill runs): attempts 15, valid 15, invalid 0 (none).
Test configuration c (kill runs): attempts 15, valid 15, invalid 0 (none).
Test configuration d (kill runs): attempts 17, valid 15, invalid 2 (kept in invalid/ (SETUP PROBLEM retry not pressed) x2).
Test configuration e K3 (kill runs): attempts 10, valid 5, invalid 5 (kept in invalid/ (INVALID sweeper workflow errored) x5).
Test configuration e K2 (kill runs): attempts 6, valid 5, invalid 1 (kept in invalid/ (INVALID sweeper workflow errored) x1).
Test configuration e K1 (kill runs): attempts 5, valid 5, invalid 0 (none).
Test configuration e (ten at once): attempts 10, valid 5, invalid 5 (kept in invalid/ (first pass sweeper expression broken but no pending rows) x5).

The runs of one test configuration are not independent: one n8n process, one database and one laptop serve all of them, one after another. The 95% intervals (Wilson) treat them as independent trials, so they are optimistic (too narrow); read them as a floor on the uncertainty, not as a promise. 20 of 20 clean runs does NOT mean that duplicates cannot happen: it means that none was seen in 20 tries on one machine.

## Ten at once, then the same ten again as a replay (variants a to d)

The replay is sent as soon as the first ten have been quiet for at least 3 seconds (the published run waited about 5 seconds), so the replay timing differs a little and replay numbers are not strictly comparable to the published run.

### Variant a (n8n default concurrency)

Valid runs 20. Runs ending with exactly 1 CRM row and 1 email: 18 of 20 (95% interval 69.9 to 97.2 percent). Runs with more: 2 of 20 (95% interval 2.8 to 30.1 percent).
Final (CRM rows, emails) after the ten: (1, 1) x18, (3, 3) x1, (8, 8) x1.
Posts added by the replay (CRM, emails): (0, 0) x20.

| run | replies (10 hits) | CRM / email after hits | replay added CRM / email |
|---|---|---|---|
| 1 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 2 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 3 | 200x8, 500x2 | 8 / 8 | +0 / +0 |
| 4 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 5 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 6 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 7 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 8 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 9 | 200x3, 500x7 | 3 / 3 | +0 / +0 |
| 10 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 11 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 12 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 13 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 14 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 15 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 16 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 17 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 18 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 19 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 20 | 200x1, 500x9 | 1 / 1 | +0 / +0 |

### Variant b (n8n default concurrency)

Valid runs 20. Runs ending with exactly 1 CRM row and 1 email: 20 of 20 (95% interval 83.9 to 100.0 percent). Runs with more: 0 of 20 (95% interval 0.0 to 16.1 percent).
Final (CRM rows, emails) after the ten: (1, 1) x20.
Posts added by the replay (CRM, emails): (0, 0) x20.

| run | replies (10 hits) | CRM / email after hits | replay added CRM / email |
|---|---|---|---|
| 1 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 2 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 3 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 4 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 5 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 6 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 7 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 8 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 9 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 10 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 11 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 12 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 13 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 14 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 15 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 16 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 17 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 18 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 19 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 20 | 200x1, 500x9 | 1 / 1 | +0 / +0 |

### Variant c (n8n default concurrency)

Valid runs 20. Runs ending with exactly 1 CRM row and 1 email: 20 of 20 (95% interval 83.9 to 100.0 percent). Runs with more: 0 of 20 (95% interval 0.0 to 16.1 percent).
Final (CRM rows, emails) after the ten: (1, 1) x20.
Posts added by the replay (CRM, emails): (0, 0) x20.

| run | replies (10 hits) | CRM / email after hits | replay added CRM / email |
|---|---|---|---|
| 1 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 2 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 3 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 4 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 5 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 6 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 7 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 8 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 9 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 10 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 11 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 12 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 13 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 14 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 15 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 16 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 17 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 18 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 19 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 20 | 200x1, 500x9 | 1 / 1 | +0 / +0 |

### Variant d (concurrency limit 1)

Valid runs 20. Runs ending with exactly 1 CRM row and 1 email: 20 of 20 (95% interval 83.9 to 100.0 percent). Runs with more: 0 of 20 (95% interval 0.0 to 16.1 percent).
Final (CRM rows, emails) after the ten: (1, 1) x20.
Posts added by the replay (CRM, emails): (0, 0) x20.

| run | replies (10 hits) | CRM / email after hits | replay added CRM / email |
|---|---|---|---|
| 1 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 2 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 3 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 4 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 5 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 6 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 7 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 8 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 9 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 10 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 11 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 12 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 13 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 14 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 15 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 16 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 17 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 18 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 19 | 200x1, 500x9 | 1 / 1 | +0 / +0 |
| 20 | 200x1, 500x9 | 1 / 1 | +0 / +0 |

## Kill runs (variants a to d): n8n killed, restarted, retry pressed once per crashed entry

The letters K1, K2 and K3 in the raw files name different moments for variants a to d (CRM first, then email) and for variant e (email first, then CRM). This file names the moment in words and gives the letter only so that the raw file can be found. For variants a to d: K1 = email request in flight (CRM already stored), K2 = email stored with its reply held for 3 seconds, K3 = CRM request in flight (first step, the email is never sent).

### Variant a

Kill points here: K1 = email request in flight (CRM already stored); K2 = email stored, its reply held; K3 = CRM request in flight (first step, email never sent).

| point | run | CRM / email at kill | after in-flight requests ended | list after restart | retry presses | final CRM / email | crashed entry visible | valid |
|---|---|---|---|---|---|---|---|---|
| K1 | 1 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 2 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 3 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 4 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 5 | 4 / 0 | 4 / 4 | crashed x4, success x6 | 4 | 4 / 4 | yes | yes |
| K2 | 1 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 2 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 3 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 4 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 5 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K3 | 1 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 2 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 3 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 4 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 5 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |

Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).

Email request in flight (CRM already stored) (raw file point K1): 5 valid runs, final (CRM, email) (1, 1) x4, (4, 4) x1; crashed entry visible 5 of 5.

Email stored, its reply held (raw file point K2): 5 valid runs, final (CRM, email) (1, 1) x5; crashed entry visible 5 of 5.

CRM request in flight (first step, email never sent) (raw file point K3): 5 valid runs, final (CRM, email) (1, 0) x5; crashed entry visible 5 of 5.

### Variant b

Kill points here: K1 = email request in flight (CRM already stored); K2 = email stored, its reply held; K3 = CRM request in flight (first step, email never sent).

| point | run | CRM / email at kill | after in-flight requests ended | list after restart | retry presses | final CRM / email | crashed entry visible | valid |
|---|---|---|---|---|---|---|---|---|
| K1 | 1 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 2 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 3 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 4 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 5 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 1 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 2 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 3 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 4 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 5 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K3 | 1 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 2 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 3 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 4 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 5 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |

Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).

Email request in flight (CRM already stored) (raw file point K1): 5 valid runs, final (CRM, email) (1, 1) x5; crashed entry visible 5 of 5.

Email stored, its reply held (raw file point K2): 5 valid runs, final (CRM, email) (1, 1) x5; crashed entry visible 5 of 5.

CRM request in flight (first step, email never sent) (raw file point K3): 5 valid runs, final (CRM, email) (1, 0) x5; crashed entry visible 5 of 5.

### Variant c

Kill points here: K1 = email request in flight (CRM already stored); K2 = email stored, its reply held; K3 = CRM request in flight (first step, email never sent).

| point | run | CRM / email at kill | after in-flight requests ended | list after restart | retry presses | final CRM / email | crashed entry visible | valid |
|---|---|---|---|---|---|---|---|---|
| K1 | 1 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 2 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 3 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 4 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K1 | 5 | 1 / 0 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 1 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 2 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 3 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 4 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K2 | 5 | 1 / 1 | 1 / 1 | crashed x1, success x9 | 1 | 1 / 1 | yes | yes |
| K3 | 1 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 2 | 0 / 0 | 1 / 0 | crashed x10 | 10 | 1 / 0 | yes | yes |
| K3 | 3 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 4 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |
| K3 | 5 | 0 / 0 | 1 / 0 | crashed x1, success x9 | 1 | 1 / 0 | yes | yes |

Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).

Email request in flight (CRM already stored) (raw file point K1): 5 valid runs, final (CRM, email) (1, 1) x5; crashed entry visible 5 of 5.

Email stored, its reply held (raw file point K2): 5 valid runs, final (CRM, email) (1, 1) x5; crashed entry visible 5 of 5.

CRM request in flight (first step, email never sent) (raw file point K3): 5 valid runs, final (CRM, email) (1, 0) x5; crashed entry visible 5 of 5.

### Variant d

Kill points here: K1 = email request in flight (CRM already stored); K2 = email stored, its reply held; K3 = CRM request in flight (first step, email never sent).
The "list after restart" of variant d holds fewer executions than for a to c (it lists 1 to 3, not ten). The author did not investigate why; with the concurrency limit of 1, n8n runs one execution at a time, which may be the reason (a guess, not verified).

| point | run | CRM / email at kill | after in-flight requests ended | list after restart | retry presses | final CRM / email | crashed entry visible | valid |
|---|---|---|---|---|---|---|---|---|
| K1 | 1 | 1 / 0 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K1 | 2 | 1 / 0 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K1 | 3 | 1 / 0 | 1 / 1 | crashed x1, running x1, success x1 | 1 | 1 / 1 | yes | yes |
| K1 | 4 | 1 / 0 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K1 | 5 | 1 / 0 | 1 / 1 | crashed x1, success x2 | 1 | 1 / 1 | yes | yes |
| K2 | 1 | 1 / 1 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K2 | 2 | 1 / 1 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K2 | 3 | 1 / 1 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K2 | 4 | 1 / 1 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K2 | 5 | 1 / 1 | 1 / 1 | crashed x1 | 1 | 1 / 1 | yes | yes |
| K3 | 1 | 0 / 0 | 1 / 0 | crashed x1 | 1 | 1 / 0 | yes | yes |
| K3 | 2 | 0 / 0 | 1 / 0 | crashed x1 | 1 | 1 / 0 | yes | yes |
| K3 | 3 | 0 / 0 | 1 / 0 | crashed x1 | 1 | 1 / 0 | yes | yes |
| K3 | 4 | 0 / 0 | 1 / 0 | crashed x1 | 1 | 1 / 0 | yes | yes |
| K3 | 5 | 0 / 0 | 1 / 0 | crashed x1 | 1 | 1 / 0 | yes | yes |

Kill rows where no client lost its connection: 0 of 15 valid kill rows (the result file holds 15 rows; 2 more attempts kept in invalid/). A kill where no client lost its connection may not have taken effect; 0 means every valid kill row did show a lost connection. Rows with killed_pid not true in the raw row: 0 (such a row is never valid; in the 2026-11-09 Windows run this field only recorded that an n8n process was found).

Email request in flight (CRM already stored) (raw file point K1): 5 valid runs, final (CRM, email) (1, 1) x5; crashed entry visible 5 of 5.

Email stored, its reply held (raw file point K2): 5 valid runs, final (CRM, email) (1, 1) x5; crashed entry visible 5 of 5.

CRM request in flight (first step, email never sent) (raw file point K3): 5 valid runs, final (CRM, email) (1, 0) x5; crashed entry visible 5 of 5.

## Variant e: status column and a re-send job every 15 seconds (blind mail stand-in)

The letters K1, K2 and K3 in the raw files name different moments for variants a to d (CRM first, then email) and for variant e (email first, then CRM). This file names the moment in words and gives the letter only so that the raw file can be found. For variant e: K3 = email request in flight (row still pending), K2 = email stored with its reply held (row pending), K1 = CRM request in flight (last step, email already sent). "Blind mail stand-in" means the fake mail server stores every email it is sent and cannot recognise a repeat.

### Email request in flight (row pending) (raw file point K3)

Kill points of variant e: K1 = CRM request in flight (last step, email already sent); K2 = email stored, its reply held (row pending); K3 = email request in flight (row pending).

Cells are CRM posts / email posts / database status.

| run | at kill | after in-flight settled | +5s | +15s | +25s | +40s | +60s | re-send job posts (email/CRM) | client replies | valid |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0/0/pending | 0/1/pending | 0/1/pending | 2/3/sent | 2/3/sent | 2/3/sent | 2/3/sent | 2/2 | 200x9, ConnectionResetErrorx1 | yes |
| 2 | 0/0/pending | 0/1/pending | 0/1/pending | 1/2/sent | 1/2/sent | 1/2/sent | 1/2/sent | 1/1 | 200x9, ConnectionResetErrorx1 | yes |
| 3 | 0/0/pending | 0/1/pending | 0/2/sent | 1/2/sent | 1/2/sent | 1/2/sent | 1/2/sent | 1/1 | 200x9, ConnectionResetErrorx1 | yes |
| 4 | 0/0/pending | 0/1/pending | 0/1/pending | 1/2/sent | 1/2/sent | 1/2/sent | 1/2/sent | 1/1 | 200x9, ConnectionResetErrorx1 | yes |
| 5 | 0/0/pending | 0/1/pending | 0/1/pending | 2/3/sent | 2/3/sent | 2/3/sent | 2/3/sent | 2/2 | 200x9, ConnectionResetErrorx1 | yes |

### Email stored, its reply held (row pending) (raw file point K2)

Kill points of variant e: K1 = CRM request in flight (last step, email already sent); K2 = email stored, its reply held (row pending); K3 = email request in flight (row pending).

Cells are CRM posts / email posts / database status.

| run | at kill | after in-flight settled | +5s | +15s | +25s | +40s | +60s | re-send job posts (email/CRM) | client replies | valid |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0/1/pending | 0/1/pending | 0/1/pending | 1/2/sent | 1/2/sent | 1/2/sent | 1/2/sent | 1/1 | 200x9, ConnectionResetErrorx1 | yes |
| 2 | 0/1/pending | 0/1/pending | 0/1/pending | 1/2/sent | 1/2/sent | 1/2/sent | 1/2/sent | 1/1 | 200x9, ConnectionResetErrorx1 | yes |
| 3 | 0/1/pending | 0/1/pending | 0/1/pending | 1/2/sent | 1/2/sent | 1/2/sent | 1/2/sent | 1/1 | 200x9, ConnectionResetErrorx1 | yes |
| 4 | 0/1/pending | 0/1/pending | 0/1/pending | 2/3/sent | 2/3/sent | 2/3/sent | 2/3/sent | 2/2 | 200x9, ConnectionResetErrorx1 | yes |
| 5 | 0/1/pending | 0/1/pending | 0/1/pending | 2/3/sent | 2/3/sent | 2/3/sent | 2/3/sent | 2/2 | 200x9, ConnectionResetErrorx1 | yes |

### CRM request in flight (last step, email already sent) (raw file point K1)

Kill points of variant e: K1 = CRM request in flight (last step, email already sent); K2 = email stored, its reply held (row pending); K3 = email request in flight (row pending).

Cells are CRM posts / email posts / database status.

| run | at kill | after in-flight settled | +5s | +15s | +25s | +40s | +60s | re-send job posts (email/CRM) | client replies | valid |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 0/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 0/0 | 200x9, ConnectionResetErrorx1 | yes |
| 2 | 0/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 0/0 | 200x9, ConnectionResetErrorx1 | yes |
| 3 | 0/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 0/0 | 200x9, ConnectionResetErrorx1 | yes |
| 4 | 0/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 0/0 | 200x9, ConnectionResetErrorx1 | yes |
| 5 | 0/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 1/1/sent | 0/0 | 200x9, ConnectionResetErrorx1 | yes |

At the two email points (K3 and K2 together): 10 of 10 (95% interval 72.2 to 100.0 percent) ended with 2 or more emails; 4 of 10 (95% interval 16.8 to 68.7 percent) ended with 2 CRM rows. Status not `sent` at 60 s in 0 of 15 kill runs.

Gaps (ms) between two re-send emails for the same address when less than 1 s apart: [52, 55, 63, 63].

Ten at once for e: 5 runs; final (CRM, email) (1, 1) x5; 35 s later (1, 1) x5; replies 200x10 x5.

## Cross-check against the stand-in's own log

The final counts of each run (ten-at-once after the replay; kill runs after the retry) compared with the posts the fake server logged for that address: 140 runs equal, 0 differ.

