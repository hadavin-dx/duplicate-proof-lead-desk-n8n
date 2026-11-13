import json
EM = "={{ $json.body.payload.email }}"
def http(name, path, pos):
    return {"parameters": {"method": "POST", "url": "http://127.0.0.1:8790/fake/" + path, "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($('Webhook').item.json.body) }}", "options": {}},
            "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": pos}
def hook(path, wid): return {"parameters": {"httpMethod": "POST", "path": path, "responseMode": "lastNode", "options": {}}, "name": "Webhook",
            "type": "n8n-nodes-base.webhook", "typeVersion": 2, "position": [0, 0], "webhookId": wid + "-wh"}
def chain(nodes, order):
    c = {}
    for a, b in zip(order, order[1:]): c[a] = {"main": [[{"node": b, "type": "main", "index": 0}]]}
    return c
def wf_dedupe(path, wid, crm_extra=None):
    d = {"parameters": {"operation": "removeItemsSeenInPreviousExecutions", "logic": "removeItemsWithAlreadySeenKeyValues", "dedupeValue": EM, "options": {}},
         "name": "Dedupe", "type": "n8n-nodes-base.removeDuplicates", "typeVersion": 2, "position": [220, 0]}
    nodes = [hook(path, wid), d, http("CRM", "crm", [440, 0]), http("Outbox", "outbox", [660, 0])]
    return {"id": wid, "name": "wp0b2-" + path, "nodes": nodes, "connections": chain(nodes, ["Webhook", "Dedupe", "CRM", "Outbox"]), "active": False, "settings": {}}
def upsert_node(tid, name="Upsert"):
    return {"parameters": {"resource": "row", "operation": "upsert", "dataTableId": {"__rl": True, "mode": "id", "value": tid}, "matchType": "allConditions",
            "filters": {"conditions": [{"keyName": "email", "condition": "eq", "keyValue": EM}]},
            "columns": {"mappingMode": "defineBelow", "value": {"email": EM}, "matchingColumns": [], "schema": [], "attemptToConvertTypes": False, "convertFieldsToString": False}},
            "name": name, "type": "n8n-nodes-base.dataTable", "typeVersion": 1.1, "position": [220, 0]}
def wf_probe(path, wid, tid):
    nodes = [hook(path, wid), upsert_node(tid)]
    return {"id": wid, "name": "wp0b2-" + path, "nodes": nodes, "connections": chain(nodes, ["Webhook", "Upsert"]), "active": False, "settings": {}}
def wf_upsert(path, wid, tid, cond):
    iff = {"parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"}, "conditions": [
        {"id": "c1", "leftValue": cond, "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"}, "options": {}},
        "name": "IsNew", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [440, 0]}
    crm, ob = http("CRM", "crm", [660, 0]), http("Outbox", "outbox", [880, 0])
    nodes = [hook(path, wid), upsert_node(tid), iff, crm, ob]
    cn = chain(nodes, ["Webhook", "Upsert", "IsNew"]); cn["IsNew"] = {"main": [[{"node": "CRM", "type": "main", "index": 0}], []]}; cn["CRM"] = {"main": [[{"node": "Outbox", "type": "main", "index": 0}]]}
    return {"id": wid, "name": "wp0b2-" + path, "nodes": nodes, "connections": cn, "active": False, "settings": {}}
def wf_pg(path, wid, cred):
    pg = {"parameters": {"operation": "executeQuery", "query": "INSERT INTO leads(email) VALUES ($1) ON CONFLICT (email) DO NOTHING RETURNING email;",
          "options": {"queryReplacement": EM}}, "name": "PgInsert", "type": "n8n-nodes-base.postgres", "typeVersion": 2.5, "position": [220, 0],
          "credentials": {"postgres": {"id": cred[0], "name": cred[1]}}}
    iff = {"parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"}, "conditions": [
        {"id": "c1", "leftValue": "={{ $json.email }}", "rightValue": "", "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}], "combinator": "and"}, "options": {}},
        "name": "RowReturned", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [440, 0]}
    nodes = [hook(path, wid), pg, iff, http("CRM", "crm", [660, 0]), http("Outbox", "outbox", [880, 0])]
    cn = chain(nodes, ["Webhook", "PgInsert", "RowReturned"]); cn["RowReturned"] = {"main": [[{"node": "CRM", "type": "main", "index": 0}], []]}; cn["CRM"] = {"main": [[{"node": "Outbox", "type": "main", "index": 0}]]}
    return {"id": wid, "name": "wp0b2-" + path, "nodes": nodes, "connections": cn, "active": False, "settings": {}}
