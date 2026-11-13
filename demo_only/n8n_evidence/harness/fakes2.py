import json, time, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
calls = {"outbox": [], "crm": []}; lock = threading.Lock()
D = {}  # delays: outbox_pre, outbox_post, crm_pre, crm_post
S = {"outbox_pre": 0, "outbox_post": 0, "crm_pre": 0, "crm_post": 0}  # requests currently inside that wait
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, o):
        b = json.dumps(o).encode()
        try:
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        except Exception: pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n).decode(errors="replace")
        p = self.path.split("?")[0]
        if p == "/ctl/reset":
            with lock: calls["outbox"].clear(); calls["crm"].clear()
            return self._send({"ok": 1})
        if p == "/ctl/delays":
            D.clear(); D.update(json.loads(body or "{}")); return self._send({"ok": 1})
        k = p.rsplit("/", 1)[-1] if p.startswith("/fake/") else None
        if k not in calls: self.send_response(404); self.end_headers(); return
        pre, post = float(D.get(k + "_pre", 0)), float(D.get(k + "_post", 0))
        if pre:
            with lock: S[k + "_pre"] += 1
            time.sleep(pre)
            with lock: S[k + "_pre"] -= 1
        with lock:
            t = time.time()  # one clock read for both logs
            calls[k].append({"t": t, "body": body})
            open("calls2.jsonl", "a").write(json.dumps({"k": k, "t": t, "body": body}) + "\n")
        if post:
            with lock: S[k + "_post"] += 1
            time.sleep(post)
            with lock: S[k + "_post"] -= 1
        self._send({"ok": 1})
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/ctl/state": return self._send(dict(S))
        if p == "/ctl/count":  # counts for one address, so the harness need not fetch the whole lists on every poll
            em = urllib.parse.parse_qs(self.path.partition("?")[2]).get("email", [""])[0]
            with lock: return self._send({kk: sum(1 for c in v if em in c["body"]) for kk, v in calls.items()})
        k = p.rsplit("/", 1)[-1] if p.startswith("/fake/") else None
        self._send(calls[k] if k in calls else {kk: len(v) for kk, v in calls.items()})
ThreadingHTTPServer(("127.0.0.1", 8790), H).serve_forever()
