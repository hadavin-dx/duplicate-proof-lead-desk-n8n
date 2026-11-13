import lib, api, summarise, json, time, sys, uuid, subprocess
kp = sys.argv[1]; nruns = int(sys.argv[2])
KEY = {"K3": ("outbox_pre", {"outbox_pre": 3}), "K2": ("outbox_post", {"outbox_post": 3}), "K1": ("crm_pre", {"crm_pre": 3})}[kp]
fn = "g3e_%s.jsonl" % kp; lib.require_fresh(fn)  # never append to old results
out = open(fn, "a"); invalid = 0
def db(em):
    r = subprocess.run(['docker', 'exec', lib.PG, 'psql', '-U', 'postgres', '-At', '-c', "select status from leads3 where email='%s'" % em], capture_output=True, text=True).stdout.strip()
    return r or "no-row"
for run in range(1, nruns + 1):
    em = f"e3-{kp}-{run}-{uuid.uuid4().hex[:6]}@example.com"; row = {"kill_point": kp, "run": run, "email": em, "mode": "blind"}
    if lib.port_free(5679):  # n8n is down (works on every OS)
        row["note_start"] = "n8n was down, started"; row["n8n_started_ok"] = lib.start_n8n(None, paths=("e3",))  # recorded; a failed start stops the script loudly
        if not row["n8n_started_ok"]: out.close(); sys.exit("n8n did not start before run %d (see n8n2.log)" % run)
    elif run == 1 and not lib.webhooks_live(("e3",), 30): out.close(); sys.exit("n8n is up but the webhook e3 is not registered (see n8n2.log)")  # probe before the first fire
    api.login(); lib.fpost("/ctl/delays", json.dumps(KEY[1])); t0 = time.time()
    th, rep = lib.fire_async("e3", em, rid_prefix="k", timeout=40)
    trig = None
    for _ in range(100):
        if lib.fget("/ctl/state")[KEY[0]] >= 1: trig = round(time.time() - t0, 2); break
        time.sleep(0.1)
    row["trigger_seen_after_s"] = trig; row["fake_state_at_kill"] = lib.fget("/ctl/state")
    row["at_kill"] = dict(lib.per_email(em), db=db(em))
    row["killed_pid"] = lib.kill_n8n_confirmed()  # taskkill succeeded AND port 5679 is free again
    th.join(); row["client_replies_during_kill"] = dict(rep)
    lib.fpost("/ctl/delays", "{}")
    w1 = lib.wait_quiet(em, quiet_s=2, timeout=30)  # poll until the stand-in holds no request and the counts stand still (was: sleep 4.5 s)
    row["after_inflight_settled"] = dict(w1["counts"], db=db(em)); row["wait_settled"] = lib.slim(w1)
    t_up = time.time(); row["n8n_restarted_ok"] = lib.start_n8n(None, paths=("e3",)); t_up = time.time()
    samples = []
    for s in (5, 15, 25, 40, 60):
        time.sleep(max(0, t_up + s - time.time())); samples.append(dict(lib.per_email(em), db=db(em), t_after_restart_s=s))
    row["samples"] = samples  # the samples are observation points of a re-send job that runs every 15 s: fixed times by design, not settle waits
    row["after_restart_5s"] = samples[0]; row["after_60s"] = samples[-1]
    out.write(json.dumps(row) + "\n"); out.flush(); print(json.dumps(row), flush=True)
    invalid += bool(summarise.kill_reasons(row))
out.close()
sys.exit(lib.EXIT_INVALID_KILL_ROWS if invalid else 0)  # a kill run that does not count is a failure of the script, not a quiet success
