// Runs the real portal.js and pages/log.js in node. Page one holds 3 requests, "Load more" adds 2 older ones, then a new request
// arrives and the next poll answers with a page one that has pushed the oldest of its rows out. Prints the request numbers on screen:
// the pushed-out row must still be there (it is in neither the new page one nor the older rows).
// node portal_log_poll_probe.js <portal dir> [after|inflight]
// after: the poll comes after "Load more" has answered. inflight: the poll comes WHILE "Load more" is still waiting for its answer.
// Made-up data only. The probe waits for a condition (poll-until, with a limit), never for a fixed time.
const fs = require('fs'), vm = require('vm'), path = require('path');
const dir = process.argv[2], scenario = process.argv[3] || 'after';
let releaseOlder = null;
const olderHeld = new Promise((r) => { releaseOlder = r; });

function classesOf(o) { return (o.attrs['class'] || '').split(/\s+/).filter(Boolean); }
function el(tag) {
  const o = { tag, attrs: {}, dataset: {}, style: {}, children: [], parent: null, nodeType: 1, namespaceURI: 'x', hidden: false, listeners: {},
    classList: { add() {}, remove() {}, contains(c) { return classesOf(o).indexOf(c) >= 0; }, toggle() {} },
    setAttribute(k, v) { o.attrs[k] = String(v); }, getAttribute(k) { return k in o.attrs ? o.attrs[k] : null; }, removeAttribute(k) { delete o.attrs[k]; },
    addEventListener(t, f) { (o.listeners[t] = o.listeners[t] || []).push(f); }, appendChild(k) { if (k && k.nodeType === 1) k.parent = o; o.children.push(k); return k; },
    insertBefore(k) { return o.appendChild(k); }, querySelectorAll() { return []; }, querySelector() { return null; }, focus() {}, blur() {},
    remove() { if (o.parent) o.parent.children = o.parent.children.filter((x) => x !== o); }, closest() { return null; }, contains() { return false; },
    get firstChild() { return o.children[0] || null; }, get lastChild() { return o.children[o.children.length - 1] || null; } };
  let text = '';
  Object.defineProperty(o, 'textContent', { get() { return text; }, set(v) { text = v; if (v === '') o.children = []; } });
  Object.defineProperty(o, 'isConnected', { get() { return true; } });
  return o;
}
const els = {};
const doc = { documentElement: el('html'), hidden: false, createElement: el, createElementNS: (ns, t) => el(t), createTextNode: (t) => ({ nodeType: 3, t }),
  getElementById: (id) => (els[id] = els[id] || el(id)), addEventListener(n, f) { if (n === 'DOMContentLoaded') doc.boot = f; },
  querySelectorAll() { return []; }, querySelector() { return null; }, head: el('head'), body: el('body') };
function walk(n, f) { if (!n) return; f(n); (n.children || []).forEach((k) => walk(k, f)); }
function textOf(n) { let out = ''; walk(n, (x) => { if (x.t !== undefined) out += x.t + ' '; }); return out.replace(/\s+/g, ' ').trim(); }
function byTestid(id) { const r = []; walk(els.main, (n) => { if (n.attrs && n.attrs['data-testid'] === id) r.push(n); }); return r; }


const row = (n) => ({ request_id: 'ui-poll-' + n, received_at: 1000000100 - n, door: 'form', result: 'new', lead_id: 'L' + n,
  lead_name: 'Lead ' + n, followup_state: 'sent', state_view: 'sent', new_details: false });
let phase = 'one';
const pages = {
  one: { items: [row(1), row(2), row(3)], total: 5, next: 'c3' },
  older: { items: [row(4), row(5)], total: 5, next: null },
  later: { items: [row(0), row(1), row(2)], total: 6, next: 'c2' }
};
const summary = { now: 1000000100, desk_started_at: 1, mode: { demo_tools: false, sample_data: false }, counts: { received: 5, new: 5, duplicate: 0, review: 0, open: 0 },
  needs_action: 0, waiting: 0, followups: { pending: 0, sending: 0, sent: 5, failed: 0, unknown: 0, did_not_finish: 0 }, doors: [], series: { bucket_seconds: 5, buckets: [] },
  recent: [], refused_at_door: 0, reconciled: true };
let pollsInPhase = 0;
function fetchStub(p) {
  p = String(p);
  let body = {}, status = 200;
  if (p.includes('/api/ui/summary')) body = summary;
  else if (p.includes('before=')) {
    body = pages.older;
    if (scenario === 'inflight') return olderHeld.then(() => ({ status, text: () => Promise.resolve(JSON.stringify(body)) }));
  }
  else if (p.includes('/api/ui/log')) { pollsInPhase++; body = phase === 'later' ? pages.later : pages.one; }
  else if (p.includes('/api/ui/review')) body = { now: 1, tabs: { action: 0, waiting: 0, info: 0 }, items: [] };
  else if (p.includes('/api/demo/')) status = 404;
  return Promise.resolve({ status, text: () => Promise.resolve(JSON.stringify(body)) });
}
const win = { document: doc, location: { hash: '#/log' }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} }, sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} }, scrollTo() {}, navigator: {} };
win.window = win;
const ctx = vm.createContext(Object.assign(win, { console, setTimeout: (f, ms) => setTimeout(f, Math.min(ms || 0, 15)), clearTimeout, MutationObserver: function () { this.observe = () => {}; }, fetch: fetchStub, Promise }));
const load = (rel) => vm.runInContext(fs.readFileSync(path.join(dir, rel), 'utf8'), ctx);
load('portal.js');
load('pages/log.js');
doc.boot();

async function until(pred, what) {
  for (let i = 0; i < 600; i++) { if (pred()) return; await new Promise((r) => setTimeout(r, 5)); }
  throw new Error('timed out waiting for ' + what);
}
const shown = () => byTestid('log-row').map((r) => r.attrs['data-request-id']).sort();
(async () => {
  try {
    await until(() => byTestid('log-row').length === 3, 'page one');
    const more = byTestid('log-more')[0];
    (more.listeners.click || []).forEach((f) => f({}));
    if (scenario === 'inflight') {
      const before = shown();
      phase = 'later'; pollsInPhase = 0;
      await until(() => pollsInPhase >= 2, 'two polls with the new page one while the older rows are still on their way');
      await new Promise((r) => setTimeout(r, 60));
      const during = shown();
      releaseOlder();
      await until(() => byTestid('log-row').length >= 5, 'the older rows');
      await new Promise((r) => setTimeout(r, 60));
      console.log(JSON.stringify({ before, during, after: shown() }));
    } else {
      await until(() => byTestid('log-row').length === 5, 'the older rows');
      const before = shown();
      phase = 'later'; pollsInPhase = 0;
      await until(() => pollsInPhase >= 2, 'two polls with the new page one');
      await new Promise((r) => setTimeout(r, 60));
      console.log(JSON.stringify({ before, after: shown() }));
    }
  } catch (e) { console.log(JSON.stringify({ error: String(e) })); }
  process.exit(0);
})();
