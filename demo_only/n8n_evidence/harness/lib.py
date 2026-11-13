import json, time, subprocess, os, sys, threading, uuid, collections, signal, socket, urllib.request, urllib.error, urllib.parse
import api
from summarise import overlap, _bad_number, _int, _float  # the time arithmetic lives in summarise.py (it recomputes validity from the stored times)
HERE = os.path.dirname(os.path.abspath(__file__))  # the folder of the harness scripts
WORK = os.environ["N8N_EVD_WORK"]  # CHANGED: work folder (n8n install, n8n data, results); every script runs with it as the current folder
NODE_DIR = os.environ["N8N_EVD_NODE"]  # CHANGED: folder that holds node.exe (Windows) or node (Linux), Node 24.x
IS_NT = os.name == "nt"  # the one place that decides Windows or POSIX (tests switch it to check both branches on any computer)
def node_name(): return "node.exe" if IS_NT else "node"  # the node binary of the OS
N8N_PORTS = (5679, 5780)  # n8n and its task-runner broker: both must be free after a kill
PG = os.environ.get("N8N_EVD_PG", "n8n-publish-pg")  # CHANGED: name of the Postgres container (variants b and e)
FAKE = "http://127.0.0.1:8790"
def _project_id():  # CHANGED: was a literal id of the original run's n8n; now read from the file setup.py writes
    try: return json.load(open(os.path.join(WORK, ".run", "instance.json")))["project_id"]
    except OSError: return ""
P = _project_id()
def fget(path): return json.load(urllib.request.urlopen(FAKE + path, timeout=10))
def fpost(path, body=""): urllib.request.urlopen(urllib.request.Request(FAKE + path, str(body).encode(), method="POST"), timeout=10).read()
def delays(**kw): fpost("/ctl/delays", json.dumps(kw))
def per_email(email):  # counted inside the stand-in server (was: both full lists fetched on every call)
    d = fget("/ctl/count?email=" + urllib.parse.quote(email))
    return {"crm": d["crm"], "outbox": d["outbox"]}
def port_free(port):  # true when nothing listens on the port
    with socket.socket() as s:
        s.settimeout(0.5); return s.connect_ex(("127.0.0.1", port)) != 0
EXIT_INVALID_KILL_ROWS = 4  # exit code of g3.py / g3e.py when a kill row does not count; run_all carries on and lets summarise.shortfalls decide at the end
def _pid_file(): return os.path.join(WORK, ".run", "pids.json")
def record_pid(name, pid):  # remember the process this harness started, so it only ever stops that one
    os.makedirs(os.path.dirname(_pid_file()), exist_ok=True)
    try: d = json.load(open(_pid_file()))
    except (OSError, ValueError): d = {}
    tmp = _pid_file() + ".tmp"  # write a temp file, then replace: a crash can never leave half a file
    with open(tmp, "w") as f: json.dump(dict(d, **{name: pid}), f)
    os.replace(tmp, _pid_file())
def _spawn_kw():  # Windows: no console window, own process group. POSIX: own session, so the whole tree (n8n and its task runner) can be killed as one group
    return {"creationflags": 0x08000000 | 0x00000200} if IS_NT else {"start_new_session": True}
def _kill_tree(pid):  # True only when the OS reported the kill; the caller still needs the ports to be free
    if IS_NT: return subprocess.run(f"taskkill /F /T /PID {pid}", shell=True, capture_output=True).returncode == 0
    try: os.killpg(int(pid), getattr(signal, "SIGKILL", 9)); return True  # the pid is the group id (start_new_session)
    except OSError: return False  # ProcessLookupError (already gone) and PermissionError: nothing of ours was killed
def _recorded_pid(name):
    try: return json.load(open(_pid_file())).get(name)
    except (OSError, ValueError): return None
def _kill_recorded(name):
    pid = _recorded_pid(name)
    if not pid: return None  # never started by this harness (or already stopped): kill nothing, whatever holds the port
    ok = _kill_tree(pid)
    record_pid(name, 0)  # forget the PID at once, so the OS reusing it later can never make us kill a stranger
    return str(pid) if ok else None  # "not found" is not a kill
def _wait_port_free(port, timeout=20):
    for _ in range(timeout * 2):
        if port_free(port): return True
        time.sleep(0.5)
    return False
def _ready(url, timeout):  # poll until the URL answers 200 (replaces fixed sleeps after a start)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if urllib.request.urlopen(url, timeout=2).status == 200: return True
        except Exception: pass
        time.sleep(0.25)
    return False
def up(timeout=120):
    for _ in range(timeout):
        try:
            if urllib.request.urlopen("http://127.0.0.1:5679/healthz", timeout=2).status == 200: return True
        except Exception: pass
        time.sleep(1)
    return False
def _base_env():  # explicit minimal environment (nothing of the parent's secrets or settings is inherited)
    if not IS_NT:  # POSIX: HOME (node and npm look there), a short PATH; TMPDIR and LANG only when set
        env = {k: os.environ[k] for k in ("HOME", "TMPDIR", "LANG") if k in os.environ}
        env["PATH"] = os.pathsep.join([NODE_DIR, "/".join(("", "usr", "bin")), "/bin"])  # the system folder is built from parts (the public hygiene scan flags path-shaped literals)
        return env
    keep = ("SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "OS", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    root = os.environ.get("SystemRoot") or os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Windows")
    env["PATH"] = os.pathsep.join([NODE_DIR, os.path.join(root, "System32"), root])
    return env
def n8n_env(limit=None):
    env = _base_env()
    if limit: env["N8N_CONCURRENCY_PRODUCTION_LIMIT"] = str(limit)
    # CHANGED: the environment of the old start script, set here; node.exe is started directly (no Git-bash wrapper)
    env.update(N8N_USER_FOLDER=os.path.join(WORK, "n8nhome"), N8N_DIAGNOSTICS_ENABLED="false",
               N8N_VERSION_NOTIFICATIONS_ENABLED="false", N8N_PERSONALIZATION_ENABLED="false", N8N_HOST="127.0.0.1", N8N_LISTEN_ADDRESS="127.0.0.1",
               N8N_PORT="5679", N8N_RUNNERS_BROKER_PORT="5780", N8N_TEMPLATES_ENABLED="false", N8N_HIRING_BANNER_ENABLED="false",
               N8N_RUNNERS_ENABLED="true", N8N_SECURE_COOKIE="false")
    return env
def webhook_status(path):  # (status, first 300 characters of the body) of a GET on the production webhook
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:5679/webhook/{path}", timeout=3) as x: return x.status, x.read(300).decode(errors="replace")
    except urllib.error.HTTPError as e: return e.code, e.read(300).decode(errors="replace")
    except Exception as e: return type(e).__name__, ""
LIVE_TEXT_DEFAULT = "not registered for"
def live_text(): return (os.environ.get("N8N_WEBHOOK_LIVE_TEXT") or LIVE_TEXT_DEFAULT).lower()  # the wording is an override: set N8N_WEBHOOK_LIVE_TEXT to the text the --smoke run recorded in smoke_readiness.json
def webhook_live(status, body):  # a 404 whose text contains live_text() ("not registered for" unless overridden) means: registered, for another method (our webhooks are POST). UNVERIFIED wording of n8n's text: the --smoke run records the raw answers
    if not isinstance(status, int): return False  # n8n not answering (an error class name)
    return status not in (404, 502, 503, 504) or (status == 404 and live_text() in body.lower())  # 502/503/504: n8n or a proxy in front of it is not ready (n8n answers 503 while it starts)
def webhooks_live(paths, timeout):  # readiness only says the database is up: poll the actual webhooks until each is registered
    t0 = time.monotonic(); todo = list(paths)
    while todo and time.monotonic() - t0 < timeout:
        todo = [p for p in todo if not webhook_live(*webhook_status(p))]
        if todo: time.sleep(0.25)
    return not todo
def start_n8n(limit=None, log="n8n2.log", paths=()):
    """paths: webhook paths that must answer (registered) before this returns True; callers pass the workflows they are about to fire at."""
    if not _wait_port_free(5679, 20) or not port_free(5780): raise RuntimeError("port 5679 or 5780 is busy: refusing to start n8n")
    f = open(os.path.join(WORK, log), "ab")
    p = subprocess.Popen([os.path.join(NODE_DIR, node_name()), os.path.join("node_modules", "n8n", "bin", "n8n"), "start"], cwd=WORK, env=n8n_env(limit), stdout=f, stderr=f, **_spawn_kw())
    record_pid("n8n", p.pid)
    f.close()  # the child holds its own copy of the log handle
    # was a fixed 3 s wait after /healthz. /healthz/readiness answers 200 once n8n's database is connected and migrated (UNVERIFIED here: n8n could not be run when this was written; the evidence runner's --smoke run checks it)
    return up() and _ready("http://127.0.0.1:5679/healthz/readiness", 30) and webhooks_live(paths, 60)  # and the webhooks answer
def kill_n8n(): return _kill_recorded("n8n")
def kill_n8n_confirmed(timeout=15):
    """True only when the OS reported the kill AND nothing listens on the n8n port afterwards (a kill that took no effect is recorded as such). and the task-runner port is free too."""
    return bool(kill_n8n()) and all(_wait_port_free(p, timeout) for p in N8N_PORTS)
def start_fakes():
    if not port_free(8790): raise RuntimeError("port 8790 is busy: refusing to start the stand-in server")
    f = open(os.path.join(WORK, "fakes2.log"), "ab")  # CHANGED: runs in the work folder, so calls2.jsonl is written there
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "fakes2.py")], cwd=WORK, env=_base_env(), stdout=f, stderr=f, **_spawn_kw())
    record_pid("fakes", p.pid); f.close()
    if not _ready(FAKE + "/ctl/state", 15): raise RuntimeError("the stand-in server did not answer within 15 s")  # was a fixed 1.5 s wait
def kill_fakes(): return _kill_recorded("fakes")
def require_fresh(path, kill_point=None):  # never append to results of an earlier run
    if not os.path.exists(path): return
    if kill_point is None: sys.exit("refusing to append to existing results: " + path)
    try:
        rows = [json.loads(x, parse_constant=_bad_number, parse_int=_int, parse_float=_float) for x in open(path, encoding="utf-8") if x.strip()]
        if not all(isinstance(r, dict) for r in rows): raise ValueError("a row is not an object")
    except (ValueError, OSError, RecursionError): sys.exit("refusing to append: %s is damaged or unreadable (not valid JSON, a row that is not an object, or a number that is not finite or is out of range)" % os.path.basename(path))
    if any(r.get("kill_point") == kill_point for r in rows): sys.exit("refusing to append: %s already holds rows for %s" % (path, kill_point))
def fake_busy():  # true while a request is still held inside the stand-in server; None when the stand-in cannot be reached (never "not busy")
    try: return any(fget("/ctl/state").values())
    except Exception: return None
def slim(w):  # the settle result without the counts, plus any special outcome
    return {k: w[k] for k in ("timed_out", "waited_s", "stale_running", "unreachable", "exec_errors") if k in w}
def wait_quiet(email, wid=None, since=None, quiet_s=3.0, timeout=60.0, poll=0.25, unreachable_s=10.0, stale_outcome=False):
    """poll until the CRM/email post counts for `email` have not changed for quiet_s seconds and (with wid) no execution since `since`
    is unfinished and the stand-in server holds no request. Gives up after `timeout` seconds and says so (timed_out True): the caller must record that, never count it silently.
    a stand-in that cannot be reached for unreachable_s seconds ends the wait with unreachable True. An executions-list error counts as busy (and is counted in exec_errors).
    With stale_outcome, an execution that still shows running at the timeout while the counts stood still and the stand-in was idle is reported as stale_running True (timed_out False): after a kill that is a finding, not a harness timeout.
    Cost: two small GETs per poll (counts of one address, held-request state), plus the executions list when wid is given; one poll every 0.25 s."""
    t0 = time.monotonic(); last = None; stable_since = t0; counts_since = t0; down_since = None; exec_errors = 0
    while True:
        now = time.monotonic(); waited = round(now - t0, 2)
        try: c = per_email(email)
        except Exception: c = None
        fb = fake_busy() if c is not None else None
        if c is None or fb is None:
            down_since = now if down_since is None else down_since
            if now - down_since >= unreachable_s: return {"counts": last or {}, "timed_out": False, "unreachable": True, "waited_s": waited}
            if now - t0 >= timeout: return {"counts": last or {}, "timed_out": True, "waited_s": waited}
            time.sleep(poll); continue
        down_since = None; running = False
        if wid:
            try: running = any(x.get("status") in ("running", "new", "waiting") for x in execs(wid, since))
            except RuntimeError: exec_errors += 1; running = True
        if c != last: counts_since = now
        if fb or running or c != last: last = c; stable_since = now
        elif now - stable_since >= quiet_s: return dict({"counts": c, "timed_out": False, "waited_s": waited}, **({"exec_errors": exec_errors} if exec_errors else {}))
        if now - t0 >= timeout:
            stale = stale_outcome and bool(wid) and running and not fb and not exec_errors and now - counts_since >= quiet_s
            return dict({"counts": c, "timed_out": not stale, "waited_s": waited}, **({"stale_running": True} if stale else {}), **({"exec_errors": exec_errors} if exec_errors else {}))
        time.sleep(poll)
def _cpu_times():  # Linux: (idle, total) jiffies from the first line of /proc/stat
    v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    return v[3] + (v[4] if len(v) > 4 else 0), sum(v[:8])
def _machine_load_posix():
    a = _cpu_times(); time.sleep(1.0); b = _cpu_times()
    mem = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemAvailable:"))
    return {"cpu_idle_pct": round(100.0 * (b[0] - a[0]) / ((b[1] - a[1]) or 1), 1), "free_ram_mb": round(mem / 1024, 1)}
def machine_load():  # CPU idle percent and free RAM (MB) right now; {"error": ...} when it cannot be read
    # Windows CPU from Win32_Processor.LoadPercentage (100 minus it = idle): the same on every Windows language. Linux reads /proc/stat twice, one second apart, and MemAvailable
    if not IS_NT:
        try: return _machine_load_posix()
        except Exception as e: return {"error": type(e).__name__}
    cmd = ["powershell", "-NoProfile", "-Command", r"$c=100-(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average; $m=(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory; Write-Host ($c.ToString([cultureinfo]::InvariantCulture)) $m"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30); a, b = r.stdout.split()
        return {"cpu_idle_pct": round(float(a), 1), "free_ram_mb": round(float(b) / 1024, 1)}
    except Exception as e: return {"error": type(e).__name__}
def log_load(setup, when, m=None):  # appended to load.jsonl in the work folder; with a time stamp (ts); m: a reading already taken
    with open(os.path.join(WORK, "load.jsonl"), "a") as f: f.write(json.dumps(dict(ts=iso(), setup=setup, when=when, **(m or machine_load()))) + chr(10))
def sample_load(setup, every=30.0):  # also sample while a test configuration runs (when = "during"); returns a function that stops the sampling
    stop = threading.Event()
    def loop():
        while not stop.wait(every): log_load(setup, "during")
    t = threading.Thread(target=loop, daemon=True); t.start()
    def end(): stop.set(); t.join()
    return end
def lead(email, rid): return json.dumps({"source": "webhook", "request_id": rid, "payload": {"name": "Asha Verma", "email": email}}).encode()
def fire(path, email, n=10, rid_prefix="r", timeout=60):
    url = f"http://127.0.0.1:5679/webhook/{path}"; bar = threading.Barrier(n); res = []
    sent = []
    def go(i):
        bar.wait(); sent.append(time.time())
        try:
            r = urllib.request.Request(url, lead(email, f"{rid_prefix}-{i}-{uuid.uuid4().hex[:6]}"), {"Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=timeout) as x: res.append(x.status)
        except urllib.error.HTTPError as e: res.append(e.code)
        except urllib.error.URLError as e: res.append("URLError:" + type(e.reason).__name__)  # urllib wraps errors while the request is being sent; the reason class is kept
        except Exception as e: res.append(type(e).__name__)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    global SENT_SPREAD_MS; SENT_SPREAD_MS = round((max(sent) - min(sent)) * 1000) if sent else None
    return dict(collections.Counter(res))
def fire_async(path, email, **kw):
    res = {}; t = threading.Thread(target=lambda: res.update(fire(path, email, **kw))); t.start(); return t, res
def execs(wid, since_iso=None, limit=60):
    f = json.dumps({"workflowId": wid})
    r = api.call("GET", "/executions?limit=%d&filter=%s" % (limit, urllib.parse.quote(f)))
    if isinstance(r, dict) and "HTTPError" in r: raise RuntimeError("executions list failed: HTTP %s" % r["HTTPError"])  # an error is never "no executions"
    d = r.get("data", r)
    rows = d.get("results", []) if isinstance(d, dict) else d
    if since_iso: rows = [x for x in rows if (x.get("startedAt") or "") >= since_iso]
    return rows
def exec_times(rows):  # per-execution times stored in the raw row, so a reader can recompute whether the ten overlapped
    return [{k: x.get(k) for k in ("createdAt", "startedAt", "stoppedAt")} for x in rows]
def iso(t=None):
    t = t or time.time()  # one clock read (was two)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + (".%03dZ" % int((t % 1) * 1000))
def now_iso(): return iso(time.time() - 0.5)
