"""NEW orchestrator (replaces the original master.py, master2.py and master3.py, which assumed a prepared machine).
Order of one full run: Postgres container, fake server, n8n; setup; deploy a, d, c, b; G1+G2 for a, b, c (n8n default
concurrency); G1+G2 and G3 for d (n8n concurrency limit 1); G3 for a, c, b; restart; deploy e; kill runs for e; burst for e.
  python harness/run_all.py --work-dir data/n8n_evidence_run/<name> --node-dir <folder holding node.exe (Windows) or node (Linux)> [--smoke] [--min-idle 80] [--gate-wait-min 10]
--smoke: variant b only, 3 runs per test, plus smoke_readiness.json (raw proof of how long the webhook needs after /healthz/readiness answers).
The work folder must already hold `npm ci` output (node_modules) and be free of results; the script refuses to start otherwise. Stops only what it started (the n8n and stand-in processes whose PIDs
it recorded in <work>/.run/pids.json, and the container it created; it refuses to start if a container with the label n8n-evidence.run=1 or a busy port exists).
Every step's exit code is checked; a failing step stops the run. Before every test configuration the machine must be idle (CPU idle percent >= --min-idle, default 80; it waits up to --gate-wait-min minutes, then records load_gate_failed in load.jsonl and exits with code 3 (code 2 = shortfall of valid runs); --min-idle 0 switches the idle check off).
Machine load (with a time stamp) before, during and after every test configuration goes to load.jsonl; run_manifest.json (versions, git head, OS, load before and after, how the run ended) is written at the start and the end.
Windows x64 and Linux are supported; docker-installed n8n is not (the harness needs node_modules/n8n and a process it can kill)."""
import argparse, json, os, platform, secrets, signal, socket, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
PG_IMAGE = "postgres@sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657"  # postgres:16 on 2026-11-09, RepoDigest read from the local image of a Windows x64 Docker Desktop; expected to be the multi-arch index (so also arm64), UNVERIFIED
HOPELESS_PULL_TEXT = (b"cannot connect to the docker daemon", b"is the docker daemon running", b"permission denied while trying to connect")  # the docker client cannot reach its daemon (not running, or this user may not use its socket): waiting cannot help
PG_NAME, LABEL = "n8n-publish-pg", "n8n-evidence.run=1"
COND = "={{ new Date($json.createdAt).getTime() === new Date($json.updatedAt).getTime() }}"
PORTS = (5679, 5780, 8790, 55432)
PATHS_ABCD = ("g2-a", "g2-d", "g2-b2", "g2-c2")  # webhook paths of the variants a to d (g12.py / g3.py)
EXIT_SHORTFALL, EXIT_LOAD_GATE_FAILED = 2, 3  # exit codes of run_all.py: 2 = fewer valid runs than expected, 3 = the machine never became idle enough (other failures exit with a message)


def port_free(p):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", p)) != 0


KEEP_ENV = ("PATH", "SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH",
            "HOME", "XDG_RUNTIME_DIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")  # HOME and XDG_RUNTIME_DIR for the docker client on Linux (rootless docker keeps its socket there)


def script_env(work, node_dir):
    """the environment of the harness scripts: only what python, the load reading and the docker client need, plus our own three names.
    (Parent secrets and settings are not inherited. On Linux the user must be allowed to use docker: member of the docker group, or rootless docker with XDG_RUNTIME_DIR set.)"""
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    env.update(N8N_EVD_WORK=work, N8N_EVD_NODE=node_dir, N8N_EVD_PG=PG_NAME)
    if os.environ.get("N8N_WEBHOOK_LIVE_TEXT"): env["N8N_WEBHOOK_LIVE_TEXT"] = os.environ["N8N_WEBHOOK_LIVE_TEXT"]  # the override reaches the g*.py scripts
    return env


def run_script(work, env, *args, tag=None, ok_codes=(0,)):
    """run one harness script in the work folder; any non-zero exit code stops the whole run (it used to be ignored).
    ok_codes lets g3.py / g3e.py finish with the 'kill rows that do not count' code: the run carries on and the shortfall check at the end decides."""
    print(">>", " ".join(args), time.strftime("%H:%M:%SZ", time.gmtime()), flush=True)
    out = open(os.path.join(work, "master_%s.out" % (tag or "_".join(args[:3]))), "w")
    rc = subprocess.run([sys.executable, os.path.join(HERE, args[0]), *args[1:]], cwd=work, env=env, stdout=out, stderr=subprocess.STDOUT).returncode
    out.close()
    if rc not in ok_codes: sys.exit("step failed with exit code %s: %s (see master_%s.out)" % (rc, " ".join(args), tag or "_".join(args[:3])))
    if rc: print("note: %s exited with code %s (kill rows that do not count); the run goes on, the shortfall check at the end decides" % (" ".join(args), rc), flush=True)
    return rc


def pull_image(image, env, work, tries=4, backoffs=(15, 30, 60), sleep=time.sleep, timeout=300):
    """Pull the pinned image (by digest) before `docker run`, up to `tries` times with 15/30/60 s pauses (Docker Hub answered HTTP 429 on tag pulls; a pull by digest worked on a later try).
    One pull may take at most `timeout` seconds. No retry when docker is not installed (OSError) or its daemon is not running (a retry cannot help). The docker error text goes to <work>/pull.err, never to the console;
    the run stops with the last exit code or error class only."""
    last, i = None, 0
    while i < tries:
        i += 1; err = b""; hopeless = False
        try:
            r = subprocess.run(["docker", "pull", image], env=env, capture_output=True, timeout=timeout)
            last, err = r.returncode, r.stderr or b""
            hopeless = any(x in err.lower() for x in HOPELESS_PULL_TEXT)  # only the three exact "docker is not reachable" texts; a registry failure also starts with "Error response from daemon:" and a retry can help there (HTTP 429)
        except subprocess.TimeoutExpired: last = "timeout"
        except OSError as e: last, hopeless = type(e).__name__, True  # docker is not installed
        if last == 0: return
        with open(os.path.join(work, "pull.err"), "ab") as f: f.write(err)
        print("docker pull failed (%s), try %d of %d" % (last, i, tries), flush=True)
        if hopeless: break
        if i < tries: sleep(backoffs[min(i - 1, len(backoffs) - 1)])
    sys.exit("docker pull of the pinned Postgres image failed after %d tries (last: %s); docker's error text is in pull.err in the work folder" % (i, last))


def finish(work, smoke, lib):
    """the last step of a run: the shortfall check of summarise.py (it used to print RUN ALL DONE whatever the results were)."""
    import summarise
    short = summarise.shortfalls(work, "jsonl", 3 if smoke else 20, 3 if smoke else 5, "b" if smoke else "abcd", not smoke)
    if short:
        print("RUN ALL INCOMPLETE:", *short, sep=chr(10) + "* ", flush=True)
        sys.exit(EXIT_SHORTFALL)
    print("RUN ALL DONE", time.strftime("%H:%M:%SZ", time.gmtime()), flush=True)


def load_gate(lib, setup, min_idle, wait_min, sleep=time.sleep, clock=time.monotonic):
    """wait until the machine is idle enough (CPU idle percent >= min_idle) for at most wait_min minutes; else record load_gate_failed in load.jsonl and exit 3.
    A reading that cannot be taken counts as not idle (idleness is not proven). min_idle <= 0 switches the idle check off. Returns the reading that passed."""
    if min_idle <= 0: return None
    t0 = clock()
    while True:
        m = lib.machine_load()
        if "error" not in m and m["cpu_idle_pct"] >= min_idle: return m
        if clock() - t0 >= wait_min * 60: break
        sleep(15)
    lib.log_load(setup, "load_gate_failed", m)
    print("IDLE CHECK FAILED before %r: %s (needed CPU idle >= %s percent for %s minutes): run it on an idle computer" % (setup, m, min_idle, wait_min), flush=True)
    sys.exit(EXIT_LOAD_GATE_FAILED)


def _git(*args):
    """the output of a git command, or None when git is missing or the command failed (never the text "unknown": a missing value is recorded as null)."""
    try: r = subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True, timeout=20)
    except Exception: return None
    return r.stdout.strip() if r.returncode == 0 else None


def install_signal_handlers():
    """SIGTERM (timeout, kill) and SIGHUP (terminal closed, ssh drop) end the run through SystemExit(143), so the cleanup in main()'s finally block runs
    (n8n and the stand-in server run in their own sessions and would otherwise outlive this process). SIGHUP is left alone when it is already ignored
    (nohup sets that): then a dropped ssh session does not stop the run at all."""
    def stop(signum, frame):
        for n in ("SIGTERM", "SIGHUP"):  # a second signal during the cleanup must not abort it (docker rm, the end manifest)
            if hasattr(signal, n): signal.signal(getattr(signal, n), signal.SIG_IGN)
        raise SystemExit(143)
    for name in ("SIGTERM", "SIGHUP"):
        if not hasattr(signal, name): continue
        if name == "SIGHUP" and signal.getsignal(signal.SIGHUP) == signal.SIG_IGN: continue  # started under nohup (or with SIGHUP ignored): keep it ignored, a dropped ssh session must not end a 70 to 90 minute run
        signal.signal(getattr(signal, name), stop)


def manifest(work, node_dir, lib, stage, extra):
    """run_manifest.json in the work folder (written at the start and again at the end): what ran, on what. No host name, user name or path is recorded."""
    path = os.path.join(work, "run_manifest.json")
    try: m = json.load(open(path))
    except (OSError, ValueError): m = {}
    if stage == "start":
        try: n8n = json.load(open(os.path.join(work, "node_modules", "n8n", "package.json")))["version"]
        except (OSError, ValueError, KeyError): n8n = "unknown"
        try: node = subprocess.run([os.path.join(node_dir, lib.node_name()), "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or "unknown"
        except Exception: node = "unknown"
        dirty = _git("status", "--porcelain", "--untracked-files=no", "--", HERE)  # tracked files only (an untracked __pycache__ is not a change)
        m.update(n8n=n8n, node=node, python=platform.python_version(), os=platform.platform(), machine=platform.machine(), webhook_live_text=lib.live_text(), git_head=_git("rev-parse", "HEAD"),
                 harness_dirty=None if dirty is None else bool(dirty), started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    m.update({stage + "_load": lib.machine_load(), stage + "_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, **extra)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(m, f, indent=1)
    os.replace(tmp, path)


def smoke_readiness(work, lib, path="g2-b2"):
    """raw evidence for the independent run (UNVERIFIED until run): restart n8n, and when /healthz/readiness has answered, probe the real webhook until it is registered.
    The probes (time since readiness, status, first 200 characters of the body) go to smoke_readiness.json and are printed; the first probes show whether readiness came before the webhook."""
    ready = lib.start_n8n(None)  # no paths: returns when readiness answered
    t0 = time.monotonic(); probes = []
    while time.monotonic() - t0 < 60:
        st, body = lib.webhook_status(path)
        probes.append({"after_readiness_s": round(time.monotonic() - t0, 2), "status": st, "body": body[:200]})
        if lib.webhook_live(st, body): break
        time.sleep(0.25)
    live = lib.webhook_live(probes[-1]["status"], probes[-1]["body"])
    ev = {"readiness_answered": ready, "webhook_path": path, "webhook_registered": live, "probes": probes}
    with open(os.path.join(work, "smoke_readiness.json"), "w") as f: json.dump(ev, f, indent=1)
    print("smoke readiness:", json.dumps(ev), flush=True)
    if not (ready and live): sys.exit("smoke: n8n readiness or the webhook never answered (see smoke_readiness.json)")


def _number(lo, hi=None):
    """argparse type: a finite number from lo up to hi (no upper limit when hi is None); anything else is an argparse error."""
    def conv(text):
        v = float(text)  # ValueError becomes "invalid value" by argparse
        if v - v != 0 or v < lo or (hi is not None and v > hi): raise argparse.ArgumentTypeError("must be a number from %g%s" % (lo, " to %g" % hi if hi is not None else " up"))
        return v
    return conv


def main():
    install_signal_handlers()
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", required=True); ap.add_argument("--node-dir", required=True); ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--min-idle", type=_number(0, 100), default=80.0, help="CPU idle percent needed before every test configuration (0 = no idle check)")
    ap.add_argument("--gate-wait-min", type=_number(0), default=10.0, help="minutes to wait for the machine to become idle before giving up (exit 3)")
    a = ap.parse_args()
    work = os.path.abspath(a.work_dir)
    if not os.path.isdir(os.path.join(work, "node_modules", "n8n")): sys.exit("work folder has no node_modules/n8n: run npm ci there first")
    stale = [f for f in os.listdir(work) if f.startswith(("g12", "g3", "calls2", "ids", "master_", "load", "n8n2", "fakes2", "run_manifest", "smoke_")) or f in (".run", "n8nhome")]  # load.jsonl and the two logs added; manifest and smoke file
    if stale: sys.exit("work folder is not fresh (refusing to append to old results): %s" % stale[:5])
    try: old = subprocess.run(["docker", "ps", "-aq", "--filter", "label=" + LABEL], capture_output=True, text=True).stdout.split()
    except OSError as e: sys.exit("docker could not be started (%s): install Docker and make sure the docker command is on PATH" % type(e).__name__)
    if old: sys.exit("a container with the label %s already exists (%d): not starting, so that cleanup removes only what this run started" % (LABEL, len(old)))
    busy = [p for p in PORTS if not port_free(p)]
    if busy: sys.exit("ports in use, not starting: %s" % busy)
    env = script_env(work, os.path.abspath(a.node_dir))
    os.environ.update(env)  # this process imports lib, which reads the three names
    sys.path.insert(0, HERE)
    os.makedirs(os.path.join(work, ".run"))
    pgpw = secrets.token_urlsafe(24)
    with open(os.path.join(work, ".run", "pg.json"), "x") as f: json.dump({"password": pgpw}, f)

    def py(*args, tag=None, ok_codes=(0,)): return run_script(work, env, *args, tag=tag, ok_codes=ok_codes)  # a failing step stops the run

    def phase(setup, *args, tag=None):  # machine load before and after every test configuration (load.jsonl); and every 30 s during it; the idle check first
        load_gate(lib, setup, a.min_idle, a.gate_wait_min)
        lib.log_load(setup, "before"); end = lib.sample_load(setup)
        try: py(*args, tag=tag, ok_codes=(0, lib.EXIT_INVALID_KILL_ROWS) if args[0] in ("g3.py", "g3e.py") else (0,))
        finally: end()
        lib.log_load(setup, "after")

    def body():
        pull_image(PG_IMAGE, env, work)
        subprocess.run(["docker", "run", "-d", "--name", PG_NAME, "--label", LABEL, "-p", "127.0.0.1:55432:5432", "-e", "POSTGRES_PASSWORD", PG_IMAGE],
                       env=dict(env, POSTGRES_PASSWORD=pgpw), check=True, capture_output=True)
        for _ in range(60):
            if subprocess.run(["docker", "exec", PG_NAME, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], capture_output=True).returncode == 0: break  # over TCP, which the image's temporary start-up server does not offer (was: socket check plus sleep 3)
            time.sleep(1)
        else: sys.exit("Postgres did not accept connections within 60 s")  # was: carried on silently
        os.chdir(work)
        lib.start_fakes()
        print("n8n up (limit 1):", lib.start_n8n(1), flush=True)
        py("setup.py", tag="setup")
        for what in (("a",), ("d",), ("c", COND), ("b",)):
            py("deploy_b.py", tag="deploy_b") if what == ("b",) else py("deploy2.py", *what, tag="deploy_" + what[0])
        ids = json.load(open("ids.json"))  # g12.py and g3.py look variant c up under "wp0b2-g2-c"; its workflow is named wp0b2-g2-c2
        ids["wp0b2-g2-c"] = ids["wp0b2-g2-c2"]
        json.dump(ids, open("ids.json", "w"))
        n = "3" if a.smoke else "20"
        k = "3" if a.smoke else "5"
        if a.smoke:  # variant b only, n8n at default concurrency
            lib.kill_n8n_confirmed() or sys.exit("smoke: the first n8n could not be killed")
            smoke_readiness(work, lib)  # proof that the webhook answers after readiness (raw evidence file)
            phase("b default", "g12.py", "b", "default", n, tag="g12_b_default")
            phase("b kill runs", "g3.py", "b", "K1", k, tag="g3_b_K1"); phase("b kill runs K2", "g3.py", "b", "K2", k, tag="g3_b_K2"); phase("b kill runs K3", "g3.py", "b", "K3", k, tag="g3_b_K3")
            print("client reply names seen (smoke):", json.dumps(summarise.reply_keys(work, "jsonl")), flush=True)  # the real names of the errors the clients saw while n8n was killed
            return finish(work, True, lib)
        def restart(limit, what):  # kill proven (process and both ports), then start, and the four webhooks must answer; a failed restart stops the run
            lib.kill_n8n_confirmed() or sys.exit("n8n could not be killed before the restart (%s)" % what)
            ok = lib.start_n8n(limit, paths=PATHS_ABCD); print("n8n %s up:" % what, ok, flush=True)
            ok or sys.exit("n8n did not come up (%s): see n8n2.log" % what)
        restart(None, "default")
        for v in ("a", "b", "c"): phase("%s default" % v, "g12.py", v, "default", n, tag="g12_%s_default" % v)
        restart(1, "limit 1")
        phase("d limit1", "g12.py", "d", "limit1", n, tag="g12_d_limit1")
        for kp in ("K1", "K2", "K3"): phase("d kill runs " + kp, "g3.py", "d", kp, k, tag="g3_d_" + kp)
        restart(None, "default")
        for v in ("a", "c", "b"):
            for kp in ("K1", "K2", "K3"): phase("%s kill runs %s" % (v, kp), "g3.py", v, kp, k, tag="g3_%s_%s" % (v, kp))
        py("deploy3.py", tag="deploy3")
        for kp in ("K3", "K2", "K1"): phase("e kill runs " + kp, "g3e.py", kp, "5", tag="g3e_" + kp)
        phase("e ten at once", "g12e.py", tag="g12e")
        finish(work, False, lib)

    import lib, summarise
    status = "aborted"
    try:
        manifest(work, os.path.abspath(a.node_dir), lib, "start", {"smoke": a.smoke, "min_idle": a.min_idle, "gate_wait_min": a.gate_wait_min})
        body()
        status = "finished"
    except SystemExit as e:
        status = "exit %s" % (e.code,)
        raise
    finally:
        try:
            lib.kill_n8n(); lib.kill_fakes()
        finally:
            try:
                ids = subprocess.run(["docker", "ps", "-aq", "--filter", "label=" + LABEL], capture_output=True, text=True).stdout.split()
                if ids: subprocess.run(["docker", "rm", "-fv", *ids], capture_output=True)
            finally:
                manifest(work, os.path.abspath(a.node_dir), lib, "end", {"status": status})


if __name__ == "__main__":
    main()
