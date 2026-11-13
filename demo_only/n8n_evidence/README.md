# What one small test of n8n 2.46.6 measured: ten identical leads at once, and a crash in the middle (fake services, one machine)

> **What to believe, and what not to believe**
> * This is one small test on one Windows laptop, with fake services. Nobody else has verified it. **It has not been run on Linux yet**: the Linux steps below are untested, which is what an independent rerun is for.
> * **The study question:** when ten identical new leads reach an n8n workflow at the same instant, does each lead end with exactly one CRM row and one email, and does that still hold when n8n is killed in the middle, and is the crash visible to a person? Nothing here says n8n is bad, and nothing here shows how real systems behave.
> * "20 of 20 clean runs" does not mean duplicates cannot happen: it means none was seen in 20 tries.
> * The variants a to e are five small workflow designs written for this test, not products. **Variant d runs one request at a time (concurrency limit 1), so its ten requests cannot overlap and cannot race**; its rule for a valid run was relaxed during the run, and 35 attempts were set aside (see "Headline").
> * Some results look alarming (a retry that says `success` but sends no email; HTTP 500 for duplicate callers). **The cause was not investigated; it may come from how these test workflows are set up or from n8n itself.** Do not quote them as findings about n8n.

This folder lets anyone rerun a small test of **n8n** (the workflow tool) and compare the counts with the ones measured on 2026-11-09 (the "2026-11-09 Windows run"). It is not an n8n benchmark and it is not affiliated with n8n; "n8n" is a trademark of its owner; no n8n logo, screenshot or code is in this repository. **The author** is Hadavin (GitHub account `hadavin-dx`). The **harness** is the set of scripts in `harness/` that runs the test. Every label used here (variant, CRM, crashed entry, retry pressed, claim step, test configuration, M, T, R1, R2, kill points K1 to K3) is explained in **`GLOSSARY.md`**.

**Files:** `GLOSSARY.md` (terms) | `DETAILS.md` (versions, how the test was set, results notes, how a run works, Windows steps, licence) | `CHANGES.md` (what is original, what is new, file hashes, history) | `GATES.md` (the author's pass rules) | `REPRODUCTION-RULE.md` (when a rerun counts as "reproduced", where to publish) | `tables.md` (all counts, generated from `published_run/`).

## Headline, in plain words
All numbers are counted in `tables.md`. CRM and mail are fake servers (the stand-in); the test makes no claim about any real system.
* **Ten identical leads sent at the same instant.**
  * Variant a (n8n's Remove Duplicates node, default settings): more than one CRM row and email in 2 of 20 runs (duplicates (3,3) and (8,8)); 18 of 20 ended with exactly one of each.
  * Variants b, c and d ended with exactly one CRM row and one email in 20 of 20 runs each (b: database insert that ignores repeats; c: Data Table upsert; d: variant a with n8n's concurrency limited to 1).
  * Variants c and d needed more than 20 attempts to get 20 valid runs; the others were set aside and counted (`tables.md`, "Attempts").
    * For c: 55 harness faults, not results (20 with a wrong setting in the workflow's IF node, 35 with an empty execution list right after login).
    * For d: 35 runs that the original overlap rule rejected and the relaxed rule rejects as well (34 with an empty execution list, 1 with HTTP 404).
  * **Do not quote "d: 20 of 20" without saying that d cannot race.**
* **The same ten sent again (replay)** added nothing in any run of any variant (0 of 20 each).
* **n8n killed in the middle of a run, restarted, retry pressed once for each crashed entry.**
  * CRM request in flight (first step): variants a to d ended with one CRM row and no email in 5 of 5 runs each. n8n's reply to the retry said `success` and still no email was sent (in the raw `retry_responses`). The cause was not investigated; it may come from how these test workflows are set up or from n8n itself.
  * At the two points where an email was involved: b, c and d ended with one CRM row and one email in 10 of 10 runs each; a in 9 of 10 (the exception already had several posts before the kill).
* **Variant e** (b plus a status column and a job that re-sends pending leads every 15 seconds; no pass/fail rule applies to e): no lead stayed unsent, but at the two email points every run ended with two or more emails (10 of 10), some with two CRM rows (4 of 10); 0 of 15 were not sent at 60 s.
* **Not claimed:** that a is bad, that b, c or d are safe, that n8n cannot do better with another design (a claim step or a sending state was not tested), or that duplicates cannot happen. One small test on one machine is not a proof.
* **"Pass"** means "met the author's own rule" (`GATES.md`; nobody else has checked that rule).
  * By that rule variant a would be marked as not passing G1 (2 of 20 runs had duplicates).
  * Read literally, a also does not pass G3: one kill run already had 4 CRM posts before the kill and ended with 4 CRM rows and 4 emails, and the rule makes no exception for that. This is not a judgement of n8n.

**Status: not independently verified.**
* The numbers come from one automated run on one Windows 11 laptop. If you rerun this, record who ran it (a person or an AI agent), the operating system, `run_manifest.json` and `git rev-parse HEAD`; `REPRODUCTION-RULE.md` says what counts as "reproduced".
* Its lists were chosen after the author had seen an earlier rerun on the same computer (not independent), so "reproduced" is a weak statement; whoever runs the harness scores its own rerun.
* **Rule R1, copied from that file:** "(R1) A count that combines several test configurations (M4 and M5) is scored only if every test configuration it uses is not busy. M1, M2 and M3 are scored per test configuration."

## Quick start: rerun it on Linux (x86-64 or arm64, a shell, Docker; about 1.5 to 2 hours, 2 to 2.5 with the optional smoke run)
**Check first.** `python3 --version` prints 3.12 or higher (Ubuntu 24.04 has it; on 22.04 use `deadsnakes` and write `python3.12` wherever `python3` appears below; commands: `DETAILS.md`, Rerun notes). `docker ps` prints a table **without `sudo`** (else `sudo usermod -aG docker $USER`, log in again; Docker is not installed by this README). `ss -ltn | grep -E ':(5679|5780|8790|55432) '` prints nothing. About 3 GB disk, 4 GB free memory, an otherwise idle computer (a throwaway machine is best); network access to the npm registry, Docker Hub and the Node.js download site (about 1 GB). No account, no key. `python3 -B -I`: `-I` is isolated mode (environment variables and the current folder's modules are ignored), `-B` writes no bytecode files into the folder (a later `pytest` run checks that none exist). All commands run from the repository root. The harness uses only the Python standard library (`pytest` only for step 4).
```
sudo apt-get update && sudo apt-get install -y python3 python3-venv build-essential xz-utils curl git
```
1. **Clone.** The addresses are written in pieces only because the author's secret scanner flags complete web addresses in the files it scans; the lines expand to the usual GitHub and Node.js download addresses. They clone the repository `duplicate-proof-lead-desk-n8n` of `hadavin-dx` (its root is the working directory for every command; this folder is `demo_only/n8n_evidence` inside it). If `git checkout n8n-evidence-rerun` says the branch does not exist, the work was merged: skip that line. Write down `git rev-parse HEAD` when you publish. (If the clone fails: `DETAILS.md`, Rerun notes.)
```
site=github; tld=com; owner=hadavin-dx; name=duplicate-proof-lead-desk-n8n
git clone "https://"$site.$tld/$owner/$name repo && cd repo
git checkout n8n-evidence-rerun
python3 -B -I demo_only/n8n_evidence/harness/sums.py check       # must print: ok
```
`sums.py check` covers the raw results, workflows, `tables.md`, lock and package files and the rule and explanation files; **not** the harness code, this README or `SHA256SUMS` itself. If it does not print `ok`: stop, delete the folder, clone again.

2. **Docker daemon.** `docker info > /dev/null && echo docker ok` must print `docker ok`; if not, `sudo systemctl start docker`.
3. **Node.js 24.22.2** (n8n refuses older Node; used only here, nothing installed system-wide). Download from the official Node.js site, check the SHA-256 against its list, unpack. On another machine type than x86-64 or arm64 it prints one UNSUPPORTED line, downloads nothing and skips the version line: stop. A failed download or checksum stops before `tar`, and your shell never enters `data/node`.
```
v=24.22.2; case "$(uname -m)" in x86_64) a=x64;; aarch64|arm64) a=arm64;; *) a=; echo "UNSUPPORTED machine type $(uname -m): x86-64 or arm64 is needed; stop here" >&2;; esac
site=nodejs; tld=org; dist="https://"$site.$tld"/dist/v$v"      # the official Node.js download site, written in two parts
[ -n "$a" ] && mkdir -p data/node && ( cd data/node && curl -fLO "$dist/node-v$v-linux-$a.tar.xz" && curl -fLO "$dist/SHASUMS256.txt" && grep " node-v$v-linux-$a.tar.xz\$" SHASUMS256.txt | sha256sum -c - && tar -xJf "node-v$v-linux-$a.tar.xz" ) && echo "node unpacked"
[ -n "$a" ] && node="$PWD/data/node/node-v$v-linux-$a/bin" && "$node/node" --version    # must print v24.22.2; anything else: stop
h=demo_only/n8n_evidence/harness        # used by every later step
```
4. **Tests of the harness** (only `pytest`; no Docker, no n8n). `tests/test_n8n_evidence.py` at the repository root needs the repository's dev requirements: ignore it for a rerun.
```
python3 -m venv data/venv && data/venv/bin/pip install pytest
data/venv/bin/python -B -m pytest demo_only/n8n_evidence/harness/tests -q
```
5. **Smoke run, optional** (variant b only, 3 runs per test, a few minutes after `npm ci`; finds Linux problems before the long run; it uses its own work folder).
```
smoke=data/n8n_evidence_run/smoke; mkdir -p "$smoke"
cp demo_only/n8n_evidence/package.json demo_only/n8n_evidence/package-lock.json "$smoke"/
(cd "$smoke" && PATH="$node:$PATH" npm ci --no-audit --no-fund && PATH="$node:$PATH" npm rebuild isolated-vm)
python3 -B -I $h/run_all.py --work-dir "$smoke" --node-dir "$node" --smoke
python3 -B -I $h/summarise.py "$smoke" --ext jsonl --out "$smoke/tables.md" --runs 3 --kills 3 --variants b --no-e
```
6. **Full run** (70 to 90 minutes, after 15 to 20 minutes of `npm ci`) in a **new empty work folder** (`run_all.py` refuses a folder that holds results).
```
work=data/n8n_evidence_run/rerun-1; mkdir -p "$work"
cp demo_only/n8n_evidence/package.json demo_only/n8n_evidence/package-lock.json "$work"/
(cd "$work" && PATH="$node:$PATH" npm ci --no-audit --no-fund && PATH="$node:$PATH" npm rebuild isolated-vm)
nohup sh -c 'python3 -B -I "$1"/run_all.py --work-dir "$2" --node-dir "$3" > "$2/../rerun-1.out" 2>&1; echo "exit $?" > "$2/exit.txt"' sh "$h" "$work" "$node" > /dev/null 2>&1 &
```
   **Is it running or dead?** `pgrep -af run_all.py` lists it if it runs; `tail "$work/../rerun-1.out"` shows its output (a refused start, such as Docker missing, is explained there). When it has ended, `cat "$work/exit.txt"` prints `exit 0`, `exit 2` and so on (codes below). If that file is missing and `pgrep` finds nothing, it was killed hard (SIGKILL, power loss): treat it as incomplete and use step 8. The shell variables `h`, `node`, `work` exist only in the shell that set them: set them again in a new shell (more in `DETAILS.md`, "Rerun notes").
7. **Tables.** When the run has ended with code 0 or 2: `python3 -B -I $h/summarise.py "$work" --ext jsonl --out "$work/tables.md"`, then compare with the committed `tables.md` as `REPRODUCTION-RULE.md` says. `python3 -B -I $h/summarise.py "$work" --ext jsonl --busy` prints `BUSY` or `not busy` for each test configuration (the run is NOT a clean reproduction if any is busy). To check the committed tables against the published raw files (these end in `.ndjson`; its output is byte-identical to `tables.md`):
```
python3 -B -I $h/summarise.py demo_only/n8n_evidence/published_run --ext ndjson --out data/published-check.md
cmp data/published-check.md demo_only/n8n_evidence/tables.md && echo identical
```
8. **Cleanup, only after a hard kill.** `run_all.py` refuses to start while a container or process of an earlier run is left. `cleanup.py` ends a recorded process only if it started in this work folder and leads its own process group; every other recorded process is printed by program name and skipped. In a new shell set `h` and `work` again first (`$work` is the folder of the killed run; after a smoke run use `work="$smoke"`). It also checks the program (node for n8n, python for the stand-in server) and reports, without signalling, a recorded leader that is gone while its group still has members (look with `ps -g <number>`). Exit code 1 means a process could not be stopped or such a group remains, otherwise 0; afterwards check `ss -ltn` for ports 5679 and 5780. The next run needs a new empty work folder. Linux only: on Windows it prints `usage (Linux only)` and exits 1.
```
docker ps -aq --filter label=n8n-evidence.run=1 | xargs -r docker rm -fv    # does nothing when no container matches
python3 -B -I $h/cleanup.py "$work"
```

**Exit codes of `run_all.py`**

| Code | Meaning | What to do |
|---|---|---|
| 0 | Finished, every test has the expected number of valid runs. This does **not** mean the counts match the published ones. | Make the tables (step 7) and compare. |
| 2 | Finished, but some test has fewer valid runs than expected (listed after `RUN ALL INCOMPLETE`). | Rerun in a new empty work folder, or report the run as incomplete (not a reproduction as a whole; counts with the full number of valid runs are still scored one by one). |
| 3 | The computer never became idle enough before a test (idle check). | Quit other programs, rerun in a new empty folder. `--min-idle 0` turns the check off; the run is then NOT a clean reproduction. |
| 143 | Stopped by SIGTERM (128 + 15), or SIGHUP when not ignored: `run_all.py` returns 143 for both after its cleanup. | Partial results: use a new empty folder. |
| 130 | Foreground run only (for example inside `tmux`), Ctrl+C (128 + 2); not tested on Linux. A `nohup` run never receives Ctrl+C. | Use a new empty folder. |
| 4 | Never the exit code of `run_all.py`; it is the code of `g3.py` and `g3e.py` run by hand. | Nothing; see code 2. |
| other | A step failed; the message names the step and its log file (`master_*.out`). | Check for a leftover container (`docker ps`), read `rerun-1.out` and that log; use a new empty folder. |

**Where to publish a rerun.** Anywhere others can read (an issue on the repository, or a page of your own), with `tables.md`, `run_manifest.json` and `load.jsonl` (read them first: they may show details of your machine), who ran it, and the counts scored by `REPRODUCTION-RULE.md`. Nobody needs the author's permission or reply. Windows steps, the licence and everything else: `DETAILS.md`.
