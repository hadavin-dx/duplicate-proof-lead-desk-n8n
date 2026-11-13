"""NEW (not in the 2026-11-09 run): first-start setup of a fresh n8n and Postgres container.
Generates a random owner account and a random Postgres password into <work>/.run/ (never printed, never committed),
creates the owner account, reads the personal project id, creates the Postgres credential, creates the two tables.
Run with N8N_EVD_WORK set and the work folder as the current folder, after n8n and the Postgres container are up."""
import json, os, secrets, subprocess, sys, time
import api, lib

RUN = os.path.join(lib.WORK, ".run")
os.makedirs(RUN, exist_ok=True)
SQL = ("CREATE TABLE IF NOT EXISTS leads(email text primary key);"
       "CREATE TABLE IF NOT EXISTS leads3(email text primary key, status text not null default 'pending', created_at timestamptz not null default now());")


def psql(sql):
    return subprocess.run(["docker", "exec", lib.PG, "psql", "-U", "postgres", "-At", "-c", sql], capture_output=True, text=True)


def main():
    owner_file = os.path.join(RUN, "owner.json")
    if not os.path.exists(owner_file):
        o = {"email": "owner-%s@example.com" % secrets.token_hex(4), "password": secrets.token_urlsafe(24)}
        with open(owner_file, "x") as f: json.dump(o, f)
    o = json.load(open(owner_file))
    r = {"HTTPError": "not answered"}
    for _ in range(60):  # n8n answers its health check before its database migrations are done: retry until it answers JSON
        try:
            r = api.call("POST", "/owner/setup", {"email": o["email"], "firstName": "Test", "lastName": "Runner", "password": o["password"]})
            if "HTTPError" not in r or r["HTTPError"] not in (404, 500, 502, 503):
                break
            time.sleep(2)
        except Exception:  # noqa: BLE001 - empty or non-JSON answer while n8n is still starting
            time.sleep(2)
    if "HTTPError" in r: print("owner setup answered", r.get("HTTPError"), r.get("body", "")[:200]); sys.exit(1)
    for _ in range(30):
        l = api.login()
        if isinstance(l, dict) and "data" in l and "HTTPError" not in l: break
        time.sleep(2)
    else: print("login failed"); sys.exit(1)
    pj = api.call("GET", "/projects/personal")
    pid = pj["data"]["id"]
    pgpw = json.load(open(os.path.join(RUN, "pg.json")))["password"]
    cred = api.call("POST", "/credentials", {"name": "n8n-publish-pg", "type": "postgres", "projectId": pid, "data": {
        "host": "127.0.0.1", "port": 55432, "database": "postgres", "user": "postgres", "password": pgpw, "ssl": "disable",
        "allowUnauthorizedCerts": False, "maxConnections": 100, "sshTunnel": False}})
    if "HTTPError" in cred: print("credential create answered", cred.get("HTTPError"), cred.get("body", "")[:300]); sys.exit(1)
    cid = (cred.get("data") or cred)["id"]
    p = psql(SQL)
    if p.returncode: print("psql failed", p.stderr[:200]); sys.exit(1)
    with open(os.path.join(RUN, "instance.json"), "w") as f: json.dump({"project_id": pid, "pg_credential_id": cid}, f)
    print("setup ok")


if __name__ == "__main__":
    main()
