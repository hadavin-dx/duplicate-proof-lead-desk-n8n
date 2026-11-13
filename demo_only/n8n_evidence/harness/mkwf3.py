import json, mkwf2
from mkwf2 import hook, http, chain
import os  # CHANGED: the credential id was a literal id of the original run's n8n; now read from the file setup.py writes
def _cred():
    try: return (json.load(open(os.path.join(os.environ["N8N_EVD_WORK"], ".run", "instance.json")))["pg_credential_id"], "n8n-publish-pg")
    except (OSError, KeyError): return ("", "n8n-publish-pg")
CRED = _cred()
def pg(name, query, pos, repl=None):
    o = {"options": {}}
    if repl: o["options"]["queryReplacement"] = repl
    return {"parameters": {"operation": "executeQuery", "query": query, **o}, "name": name, "type": "n8n-nodes-base.postgres", "typeVersion": 2.5,
            "position": pos, "credentials": {"postgres": {"id": CRED[0], "name": CRED[1]}}}
def notempty(name, pos):
    return {"parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"}, "conditions": [
        {"id": "c1", "leftValue": "={{ $json.email }}", "rightValue": "", "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}], "combinator": "and"}, "options": {}},
        "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": pos}
def jhttp(name, path, pos, body):
    h = http(name, path, pos); h["parameters"]["jsonBody"] = body; return h
def wf_e(path, wid):
    EM = "={{ $('Webhook').item.json.body.payload.email }}"
    nodes = [hook(path, wid),
        pg("PgInsert", "INSERT INTO leads3(email,status,created_at) VALUES ($1,'pending',now()) ON CONFLICT (email) DO NOTHING RETURNING email;", [220, 0], "={{ $json.body.payload.email }}"),
        notempty("RowReturned", [440, 0]),
        http("Outbox", "outbox", [660, 0]),
        pg("MarkSent", "UPDATE leads3 SET status='sent' WHERE email=$1;", [880, 0], EM),
        http("CRM", "crm", [1100, 0]),
        {"parameters": {}, "name": "Duplicate", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": [660, 200]}]
    cn = chain(nodes, ["Webhook", "PgInsert", "RowReturned"])
    cn["RowReturned"] = {"main": [[{"node": "Outbox", "type": "main", "index": 0}], [{"node": "Duplicate", "type": "main", "index": 0}]]}
    cn["Outbox"] = {"main": [[{"node": "MarkSent", "type": "main", "index": 0}]]}
    cn["MarkSent"] = {"main": [[{"node": "CRM", "type": "main", "index": 0}]]}
    return {"id": wid, "name": "wp0b3-" + path, "nodes": nodes, "connections": cn, "active": False, "settings": {}}
def wf_sweeper(wid):
    body = "={{ JSON.stringify({ source: 'sweeper', payload: { email: $('PgPending').item.json.email } }) }}"
    trig = {"parameters": {"rule": {"interval": [{"field": "seconds", "secondsInterval": 15}]}}, "name": "Every15s", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0]}
    nodes = [trig,
        pg("PgPending", "SELECT email FROM leads3 WHERE status='pending' AND created_at < now() - interval '10 seconds';", [220, 0]),
        notempty("HasRow", [440, 0]),
        jhttp("SOutbox", "outbox", [660, 0], body),
        pg("SMarkSent", "UPDATE leads3 SET status='sent' WHERE email=$1 RETURNING email;", [880, 0], "={{ $('PgPending').item.json.email }}"),
        jhttp("SCrm", "crm", [1100, 0], "={{ JSON.stringify({ source: 'sweeper', payload: { email: $json.email } }) }}")]
    cn = chain(nodes, ["Every15s", "PgPending", "HasRow"])
    cn["HasRow"] = {"main": [[{"node": "SOutbox", "type": "main", "index": 0}], []]}
    cn["SOutbox"] = {"main": [[{"node": "SMarkSent", "type": "main", "index": 0}]]}
    cn["SMarkSent"] = {"main": [[{"node": "SCrm", "type": "main", "index": 0}]]}
    return {"id": wid, "name": "wp0b3-sweeper", "nodes": nodes, "connections": cn, "active": False, "settings": {}}
