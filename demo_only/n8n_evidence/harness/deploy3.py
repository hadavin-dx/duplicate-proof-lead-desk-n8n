import api, json, mkwf3, deploy2
api.login(); ids = {}
for w in (mkwf3.wf_e("e3", "wf3e"), mkwf3.wf_sweeper("wf3sw")):
    json.dump(w, open("wf3_%s.json" % w["name"], "w")); r = api.deploy("wf3_%s.json" % w["name"], w["name"]); print(w["name"], r); ids[w["name"]] = r[0]
json.dump(ids, open("ids3.json", "w"))
