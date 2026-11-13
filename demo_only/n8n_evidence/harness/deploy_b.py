"""NEW (the original deploy2.py had no branch for variant b): deploy variant (b), the Postgres insert, as workflow
"wp0b2-g2-b2" (webhook path g2-b2) and record its id under the key g12.py / g3.py read ("wp0b2-g2-b").
Needs setup.py to have run (credential and table `leads`)."""
import json, os, api, lib, mkwf2, deploy2

api.login()
cred = (json.load(open(os.path.join(lib.WORK, ".run", "instance.json")))["pg_credential_id"], "n8n-publish-pg")
w = mkwf2.wf_pg("g2-b2", "wf2xb", cred)
r = deploy2.dep(w)
ids = json.load(open("ids.json")); ids["wp0b2-g2-b"] = r[0]; json.dump(ids, open("ids.json", "w"))
print("b", r)
