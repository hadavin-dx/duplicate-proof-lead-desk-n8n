import json, sys, urllib.request, http.cookiejar
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
B = "http://127.0.0.1:5679/rest"
import os
def _owner():  # CHANGED: was a literal account; now the generated one that setup.py wrote
    return json.load(open(os.path.join(os.environ["N8N_EVD_WORK"], ".run", "owner.json")))
def call(m, p, body=None):
    r = urllib.request.Request(B + p, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json", "browser-id": "wp0b"}, method=m)
    try:
        return json.load(op.open(r))
    except urllib.error.HTTPError as e:
        return {"HTTPError": e.code, "body": e.read().decode()[:600]}
def login(): o = _owner(); return call("POST", "/login", {"emailOrLdapLoginId": o["email"], "password": o["password"]})
def deploy(fn, name):
    w = json.load(open(fn)); w.pop("id", None); w["name"] = name; w.pop("active", None)
    c = call("POST", "/workflows", w)
    d = c.get("data", c)
    if "id" not in d: return c
    a = call("POST", f"/workflows/{d['id']}/activate", {"versionId": d["versionId"]})
    return d["id"], (a if "HTTPError" in a else "activated")
if __name__ == "__main__":
    l = login(); print(str(l)[:80])
    print(deploy(sys.argv[1], sys.argv[2]))
