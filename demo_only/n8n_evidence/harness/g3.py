import lib, api, summarise, json, time, sys, uuid, collections
variant = sys.argv[1]; kp = sys.argv[2]; nruns = int(sys.argv[3]); limit = 1 if variant == "d" else None
KEY = {"K1": ("outbox_pre", {"outbox_pre": 3}), "K2": ("outbox_post", {"outbox_post": 3}), "K3": ("crm_pre", {"crm_pre": 3})}[kp]
ids = json.load(open("ids.json")); wid = ids["wp0b2-g2-" + variant]; path = {"b": "g2-b2", "c": "g2-c2"}.get(variant, "g2-" + variant)
fn = f"g3_{variant}.jsonl"; lib.require_fresh(fn, kill_point=kp)  # the three kill points share one file, each may be written once
out = open(fn, "a"); invalid = 0
slim = lib.slim  # the settle result without the counts (plus a stale-running or unreachable outcome)
def statuses(rows): return dict(collections.Counter(x["status"] for x in rows))
for run in range(1, nruns + 1):
    em = f"g3-{variant}-{kp}-{run}-{uuid.uuid4().hex[:6]}@example.com"; row = {"variant": variant, "kill_point": kp, "run": run, "email": em}
    if lib.port_free(5679):  # n8n is down (works on every OS; was a netstat call that only worked on Windows)
        row["note_start"] = "n8n was down, started"; row["n8n_started_ok"] = lib.start_n8n(limit, paths=(path,))  # the result is recorded, and a failed start stops the script loudly
        if not row["n8n_started_ok"]: out.close(); sys.exit("n8n did not start before run %d (see n8n2.log)" % run)
    api.login(); lib.fpost("/ctl/delays", json.dumps(KEY[1])); t0 = time.time()
    th, rep = lib.fire_async(path, em, rid_prefix="k", timeout=40)
    trig = None
    for _ in range(100):
        if lib.fget("/ctl/state")[KEY[0]] >= 1: trig = round(time.time() - t0, 2); break
        time.sleep(0.1)
    row["trigger_seen_after_s"] = trig; row["fake_state_at_kill"] = lib.fget("/ctl/state"); row["counts_at_kill"] = lib.per_email(em)
    row["killed_pid"] = lib.kill_n8n_confirmed()  # taskkill succeeded AND port 5679 is free again
    th.join(); row["client_replies_during_kill"] = dict(rep)
    lib.fpost("/ctl/delays", "{}")
    w1 = lib.wait_quiet(em, quiet_s=2, timeout=30)  # poll until the stand-in holds no request and the counts stand still (was: sleep 4.5 s)
    row["counts_after_inflight_settled"] = w1["counts"]; row["wait_settled"] = slim(w1)
    row["n8n_restarted_ok"] = lib.start_n8n(limit, paths=(path,)); api.login()
    t_w = time.time(); rows = []
    while time.time() - t_w < 40:  # n8n marks the crashed execution some seconds after startup; wait for it (max 40 s)
        try: rows = lib.execs(wid, lib.iso(t0 - 0.5))
        except RuntimeError: rows = []  # the executions list can fail while n8n is still starting; keep trying until the 40 s are over
        if any(x["status"] in ("crashed", "error", "failed") for x in rows): break
        time.sleep(1)
    row["wait_for_crashed_entry_s"] = round(time.time() - t_w, 1); row["exec_status_after_restart"] = statuses(rows)
    crashed = [x["id"] for x in rows if x["status"] in ("crashed", "error", "failed")]; row["crashed_or_error_ids"] = crashed
    rt = []
    for i in crashed: rt.append(api.call("POST", f"/executions/{i}/retry", {"loadWorkflow": False}))
    row["retry_pressed_n"] = len(crashed); row["retry_responses"] = [str(x)[:160] for x in rt]
    w2 = lib.wait_quiet(em, wid, lib.iso(t0 - 0.5), quiet_s=3, timeout=60, stale_outcome=True)  # a stale "running" entry after the kill is its own outcome (stale_running_after_kill). poll until the retried execution is finished and quiet (was: sleep 7 s)
    row["final"] = w2["counts"]; row["wait_after_retry"] = slim(w2)
    rows2 = lib.execs(wid, lib.iso(t0 - 0.5)); row["exec_status_after_retry"] = statuses(rows2)
    row["exec_list_after_retry"] = [{"id": x["id"], "status": x["status"], "mode": x.get("mode"), "retryOf": x.get("retryOf")} for x in rows2 if x["status"] != "success" or x.get("retryOf") or x.get("mode") == "retry"]
    out.write(json.dumps(row) + "\n"); out.flush(); print(json.dumps(row), flush=True)
    invalid += bool(summarise.kill_reasons(row))
out.close()
sys.exit(lib.EXIT_INVALID_KILL_ROWS if invalid else 0)  # a kill run that does not count is a failure of the script, not a quiet success
