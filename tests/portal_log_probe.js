// Runs the real portal.js and pages/log.js in node against a stand-in page that keeps attributes, children and click handlers.
// node portal_log_probe.js <portal dir>
// The request log gets 4 repeats in a row from one lead, then 2 more requests. It prints what the table shows before and after
// the "Show all N" button of the grouped line is clicked, and after the page re-renders once more (the opened group stays open).
// Made-up data only. The probe waits for a condition (poll-until, with a limit), never for a fixed time.
const fs = require('fs'), vm = require('vm'), path = require('path');
const dir = process.argv[2];

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

// newest first: 4 repeats from one lead through one door (grouped into one line), then a new request and a review
const row = (n, result, lead) => ({ request_id: 'ui-probe-' + n, received_at: 1000000100 - n, door: 'form', result, lead_id: lead ? 'Lprobe' : null,
  lead_name: lead, followup_state: result === 'new' ? 'sent' : null, state_view: result === 'new' ? 'sent' : null, new_details: false });
const log = { items: [row(1, 'duplicate', 'Probe Lead'), row(2, 'duplicate', 'Probe Lead'), row(3, 'duplicate', 'Probe Lead'), row(4, 'duplicate', 'Probe Lead'),
  row(5, 'new', 'Probe Lead'), row(6, 'review', null)], total: 6, next: null };
const summary = { now: 1000000100, desk_started_at: 1, mode: { demo_tools: false, sample_data: false }, counts: { received: 6, new: 1, duplicate: 4, review: 1, open: 0 },
  needs_action: 0, waiting: 0, followups: { pending: 0, sending: 0, sent: 1, failed: 0, unknown: 0, did_not_finish: 0 }, doors: [], series: { bucket_seconds: 5, buckets: [] },
  recent: [], refused_at_door: 0, reconciled: true };
const calls = [];
function fetchStub(p) {
  p = String(p);
  if (p.includes('/api/ui/log')) calls.push(p);
  let body = {}, status = 200;
  if (p.includes('/api/ui/summary')) body = summary;
  else if (p.includes('/api/ui/log')) body = log;
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
function snap(phase) {
  const toggles = byTestid('log-group-toggle');
  return { phase, rows: byTestid('log-row').length, toggles: toggles.map((t) => textOf(t)), grouped: textOf(els.main).includes('4 repeats, same lead') };
}
(async () => {
  try {
    await until(() => byTestid('log-row').length > 0, 'the log table');
    const out = [snap('grouped')];
    const tog = byTestid('log-group-toggle')[0];
    let stopped = 0;
    (tog.listeners.click || []).forEach((f) => f({ stopPropagation() { stopped++; } }));
    out.push(Object.assign(snap('clicked'), { handlers: (tog.listeners.click || []).length, stopped }));
    // the log poll answers again (timers are shortened to 15 ms) and the page renders again: the opened group must stay open
    const before = calls.length;
    await until(() => calls.length >= before + 2, 'two more log polls');
    out.push(snap('later'));
    console.log(JSON.stringify(out));
  } catch (e) { console.log(JSON.stringify({ error: String(e) })); }
  process.exit(0);
})();
