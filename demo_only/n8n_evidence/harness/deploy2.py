import api, json, sys, mkwf2, lib, time
api.login()
def dt(name): return api.call("POST", f"/projects/{lib.P}/data-tables", {"name": name, "columns": [{"name": "email", "type": "string"}]})["data"]["id"]
def dep(w):
    json.dump(w, open("wf2_%s.json" % w["name"], "w")); r = api.deploy("wf2_%s.json" % w["name"], w["name"])
    ids = json.load(open("ids.json")) if __import__("os").path.exists("ids.json") else {}
    ids[w["name"]] = r[0]; json.dump(ids, open("ids.json", "w")); return r
if __name__ == "__main__":
    what = sys.argv[1]
    if what == "probe":
        tid = dt("probe_" + str(int(time.time()))); print(tid, dep(mkwf2.wf_probe("probe-up", "probewf" + tid[:6], tid)))
    elif what in ("a", "d"):
        print(dep(mkwf2.wf_dedupe("g2-" + what, "wf2x" + what)))
    elif what == "c":
        tid = dt("seen_c_" + str(int(time.time()))); cond = sys.argv[2]
        print(tid, dep(mkwf2.wf_upsert("g2-c2", "wf2cc2", tid, cond)))
