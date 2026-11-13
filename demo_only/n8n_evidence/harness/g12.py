import lib, api, summarise, json, time, sys, uuid, collections
variant = sys.argv[1]; port_limit = sys.argv[2] if len(sys.argv) > 2 else "default"; nruns = int(sys.argv[3]) if len(sys.argv) > 3 else 20
# the cap on attempts. It was nruns + 15, but variant d needed 55 attempts for 20 valid runs in the published set (35 invalid), so a rerun of d would have stopped
# early. The cap is a multiple of the valid runs wanted (default 3 x), or the 4th argument (a number of attempts); when it is reached the failure message names the cause.
MAX_ATTEMPTS_FACTOR = 3
max_attempts = int(sys.argv[4]) if len(sys.argv) > 4 else nruns * MAX_ATTEMPTS_FACTOR
fn = f"g12_{variant}_{port_limit}.jsonl"; lib.require_fresh(fn)  # checked first, before any login; never append to old results
for _ in range(30):
    l = api.login()
    if isinstance(l, dict) and "data" in l and "HTTPError" not in l: break
    time.sleep(2)
ids = json.load(open("ids.json")); wid = ids["wp0b2-g2-" + variant]; path = {"b": "g2-b2", "c": "g2-c2"}.get(variant, "g2-" + variant)
out = open(fn, "a"); ok = 0; invalid = 0; attempt = 0; reasons = collections.Counter()
slim = lib.slim  # the settle result without the counts
while ok < nruns and attempt < max_attempts:
    attempt += 1; em = f"g12-{variant}-{attempt}-{uuid.uuid4().hex[:6]}@example.com"
    lib.fpost("/ctl/delays", "{}")
    t0 = time.time(); since = lib.iso(t0 - 0.5); rep1 = lib.fire(path, em, rid_prefix="a1")
    s1 = lib.wait_quiet(em, wid, since, quiet_s=3, timeout=60)  # poll until quiet (was: sleep 1.0 s); a timeout is recorded and makes the run invalid
    c1 = s1["counts"]; er = lib.execs(wid, since); ov = lib.overlap(er)
    sent = lib.SENT_SPREAD_MS
    row = {"variant": variant, "setting": port_limit, "attempt": attempt, "email": em, "valid": None, "client_send_spread_ms": sent, "label": None, "replies": rep1, "after_run1": c1, "overlap": ov, "exec_times": lib.exec_times(er), "settle1": slim(s1)}  # the times too, so validity can be recomputed from the row
    why = summarise.invalid_reason(row, variant)  # the one rule (summarise.py decides; variant d: ten sent together + ten executions listed: the relaxed rule for d)
    row["valid"] = why is None
    if variant == "d" and row["valid"]: row["label"] = "valid under the relaxed rule for d (serialised variant)"
    if row["valid"]:
        rep2 = lib.fire(path, em, rid_prefix="a2")  # the replay follows as soon as the first ten are quiet (was: 4 s later)
        s2 = lib.wait_quiet(em, wid, since, quiet_s=3, timeout=60); c2 = s2["counts"]
        row.update({"replay_replies": rep2, "after_replay": c2, "replay_added": {k: c2[k] - c1[k] for k in c1}, "settle2": slim(s2)})
        why = summarise.invalid_reason(row, variant)
        row["valid"] = why is None
    if row["valid"]: ok += 1
    else: invalid += 1; reasons[why] += 1
    out.write(json.dumps(row) + "\n"); out.flush(); print(json.dumps(row), flush=True)
out.close()
print("DONE valid", ok, "invalid", invalid, flush=True)
if ok < nruns:  # fewer valid runs than asked is a failure, not a quiet success
    print("FAILED: %d valid runs of %d wanted after %d attempts (cap %d = %s); invalid reasons: %s" % (ok, nruns, attempt, max_attempts, "4th argument" if len(sys.argv) > 4 else "%d x the valid runs wanted" % MAX_ATTEMPTS_FACTOR, dict(reasons)), flush=True)
    sys.exit(1)
