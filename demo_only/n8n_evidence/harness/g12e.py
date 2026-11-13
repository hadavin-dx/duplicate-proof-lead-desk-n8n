import lib, api, summarise, json, time, uuid, subprocess, sys
lib.require_fresh("g12e.jsonl")
lib.webhooks_live(("e3",), 30) or sys.exit("the webhook e3 is not registered (see n8n2.log)")  # probe before the first fire
out = open("g12e.jsonl", "a"); lib.fpost("/ctl/delays", "{}"); bad = 0
for run in range(1, 6):
    api.login(); em = f"e3-burst-{run}-{uuid.uuid4().hex[:6]}@example.com"
    rep = lib.fire("e3", em, n=10, rid_prefix="b", timeout=60)
    s1 = lib.wait_quiet(em, quiet_s=5, timeout=60)  # poll until quiet for 5 s (was: sleep 5 s)
    st = subprocess.run(['docker', 'exec', lib.PG, 'psql', '-U', 'postgres', '-At', '-c', "select status from leads3 where email='%s'" % em], capture_output=True, text=True).stdout.strip()
    row = {"run": run, "email": em, "replies": rep, "spread_ms": lib.SENT_SPREAD_MS, "counts": s1["counts"], "db_status": st, "settle1": lib.slim(s1)}
    time.sleep(30); row["counts_after_35s"] = lib.per_email(em)  # observation point of the re-send job (it runs every 15 s), not a settle wait
    bad += bool(summarise.burst_reasons(row))  # a timeout or an unreachable stand-in makes the script fail; so does a burst whose ten were not sent within 100 ms
    out.write(json.dumps(row) + "\n"); out.flush(); print(json.dumps(row), flush=True)
sys.exit(1 if bad else 0)
