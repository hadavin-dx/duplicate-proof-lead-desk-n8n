// Runs the real portal.js and one page file in node against a stand-in page that keeps attributes, classes and children.
// node portal_state_probe.js <portal dir> <scenario>
//   pause:      the Needs page while the desk answers, stops answering, answers with a problem, and answers again;
//               prints, per phase, each action button (its action id and whether it is disabled) and whether the paused line is shown
//   fresh-fail: the Overview page when the very first answer never arrives; prints the text on the page and in the demo notice line
//   loading:    the Overview page while the first answer is still awaited; prints the same
//   cards:      the Needs page with a failed card, a did-not-finish card and a waiting card; prints buttons, hints and the badge list
//   demo-poll:  the Needs page while the demo extension's own poll answers with a server error; the buttons must stay on
//   stale:      the Overview page with data, then without an answer, then with a server error, then answering again; prints what is marked
// The scenarios wait for a condition (poll-until, with a limit), never for a fixed time.
const fs = require('fs'), vm = require('vm'), path = require('path');
const dir = process.argv[2], scenario = process.argv[3];

function classesOf(o) { return (o.attrs['class'] || '').split(/\s+/).filter(Boolean); }
function el(tag) {
  const o = { tag, attrs: {}, dataset: {}, style: {}, children: [], parent: null, nodeType: 1, namespaceURI: 'x', hidden: false,
    classList: { add() {}, remove() {}, contains(c) { return classesOf(o).indexOf(c) >= 0; },
      toggle(c, on) { const cs = classesOf(o).filter((x) => x !== c); if (on === undefined ? classesOf(o).indexOf(c) < 0 : on) cs.push(c); o.attrs['class'] = cs.join(' '); } },
    setAttribute(k, v) { o.attrs[k] = String(v); }, getAttribute(k) { return k in o.attrs ? o.attrs[k] : null; }, removeAttribute(k) { delete o.attrs[k]; },
    addEventListener() {}, appendChild(k) { if (k && k.nodeType === 1) k.parent = o; o.children.push(k); return k; }, insertBefore(k) { return o.appendChild(k); },
    querySelectorAll(sel) { return select(o, sel); }, querySelector(sel) { return select(o, sel)[0] || null; }, focus() {},
    remove() { if (o.parent) o.parent.children = o.parent.children.filter((x) => x !== o); }, closest() { return null; }, contains() { return false; },
    get firstChild() { return o.children[0] || null; }, get lastChild() { return o.children[o.children.length - 1] || null; } };
  let text = '';
  Object.defineProperty(o, 'textContent', { get() { return text; }, set(v) { text = v; if (v === '') o.children = []; } });
  Object.defineProperty(o, 'isConnected', { get() { return true; } });
  return o;
}
// a small selector matcher: comma lists, descendant chains, and compounds of .class, [attr="value"] and tag
function matchOne(n, c) {
  const parts = c.match(/\.[\w-]+|\[[\w-]+="[^"]*"\]|^[a-z0-9]+/g) || [];
  return parts.every((p) => {
    if (p[0] === '.') return classesOf(n).indexOf(p.slice(1)) >= 0;
    if (p[0] === '[') { const m = /^\[([\w-]+)="([^"]*)"\]$/.exec(p); return n.attrs[m[1]] === m[2]; }
    return n.tag === p;
  });
}
function select(root, selector) {
  const out = [];
  selector.split(',').forEach((one) => {
    const chain = one.trim().split(/\s+/);
    walk(root, (n) => {
      if (n === root || !n.attrs || !matchOne(n, chain[chain.length - 1])) return;
      let idx = chain.length - 2, a = n.parent;
      while (idx >= 0 && a) { if (a.attrs && matchOne(a, chain[idx])) idx--; a = a.parent; }
      if (idx < 0 && out.indexOf(n) < 0) out.push(n);
    });
  });
  return out;
}
const els = {};
const doc = { documentElement: el('html'), hidden: false, createElement: el, createElementNS: (ns, t) => el(t), createTextNode: (t) => ({ nodeType: 3, t }),
  getElementById: (id) => (els[id] = els[id] || el(id)), addEventListener(n, f) { if (n === 'DOMContentLoaded') doc.boot = f; },
  querySelectorAll() { return []; }, querySelector() { return null; }, head: el('head'), body: el('body') };
function walk(n, f) { if (!n) return; f(n); (n.children || []).forEach((k) => walk(k, f)); }
function textOf(n) { let out = ''; walk(n, (x) => { if (x.t !== undefined) out += x.t + ' '; }); return out.replace(/\s+/g, ' ').trim(); }

let net = 'online'; // online | offline | degraded | hang
const failedCard = { item_id: 'i1', kind: 'failed', state_view: 'failed', tab: 'action', reason: 'refused', title: 'The follow-up email was not sent', reason_text: 'The mail step refused the email. If you are not sure, check the mail step before you retry.', lead_id: 'L1', lead_name: 'Test Lead', source: 'form', since: 50,
  actions: [{ id: 'retry', label: 'Retry', endpoint: '/review/i1/retry', body: null, confirm: null }] };
const unfinishedCard = Object.assign({}, failedCard, { item_id: 'i2', state_view: 'did_not_finish', reason: 'timeout', title: 'Follow-up did not finish', reason_text: 'The email may have left. Check the mail step (the part that sends the email) first.', endpoint: '/review/i2/retry', lead_name: 'Second Lead' });
const waitingCard = Object.assign({}, failedCard, { item_id: 'i3', kind: 'stalled', state_view: 'stalled', tab: 'waiting', reason: 'pending', title: 'Queued longer than 120 s', reason_text: 'The desk retries by itself.', lead_name: 'Third Lead' });
const review = scenario === 'cards'
  ? { now: 100, tabs: { action: 2, waiting: 1, info: 0 }, items: [failedCard, unfinishedCard, waitingCard] }
  : { now: 100, tabs: { action: 1, waiting: 0, info: 0 }, items: [Object.assign({}, failedCard, { state_view: 'did_not_finish' })] };
const buckets = []; for (let i = 0; i < 60; i++) buckets.push({ t: 1000 + i * 5, new: i % 7 === 0 ? 1 : 0, duplicate: 0, review: 0 });
const summary = { now: 1300, desk_started_at: 1, mode: { demo_tools: scenario === 'demo-poll', sample_data: false }, counts: { received: 3, new: 2, duplicate: 1, review: 0, open: 0 }, needs_action: 1, waiting: 0,
  followups: { pending: 0, sending: 0, sent: 1, failed: 0, unknown: 0, did_not_finish: 1 }, doors: [{ source: 'form', received: 2, last_seen: 1290 }, { source: 'webhook', received: 1, last_seen: 1295 }],
  series: { bucket_seconds: 5, buckets }, recent: [], refused_at_door: 0, reconciled: true };
const calls = [];
function fetchStub(p) {
  p = String(p);
  calls.push(p);
  if (net === 'hang') return new Promise(() => {});
  if (net === 'offline') return Promise.reject(new TypeError('network'));
  let body = {}, status = 200;
  if (p.includes('/api/ui/summary')) { if (net === 'degraded') { body = { error: 'x' }; status = 500; } else body = summary; }
  else if (p.includes('/api/ui/review')) body = review;
  else if (p.includes('/api/demo/state')) { body = { error: 'x' }; status = 500; }
  return Promise.resolve({ status, text: () => Promise.resolve(JSON.stringify(body)) });
}
const onNeeds = scenario === 'pause' || scenario === 'cards' || scenario === 'demo-poll';
const hashFns = [];
const win = { document: doc, location: { hash: onNeeds ? '#/needs' : '#/overview' }, addEventListener(t, f) { if (t === 'hashchange') hashFns.push(f); }, matchMedia: () => ({ matches: false, addEventListener() {} }),
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} }, sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} }, scrollTo() {}, navigator: {} };
win.window = win;
// the poll timers run fast so the whole story takes well under a second
const ctx = vm.createContext(Object.assign(win, { console, setTimeout: (f, ms) => setTimeout(f, Math.min(ms || 0, 15)), clearTimeout, MutationObserver: function () { this.observe = () => {}; }, fetch: fetchStub, Promise }));
const load = (rel) => vm.runInContext(fs.readFileSync(path.join(dir, rel), 'utf8'), ctx);
load('portal.js');
load(onNeeds ? 'pages/needs.js' : 'pages/overview.js');
if (scenario === 'route-proto') load('pages/needs.js');
if (scenario === 'fresh-fail') net = 'offline'; // the very first answer never arrives
if (scenario === 'loading') net = 'hang'; // the first answer is still on its way
els['demo-line'] = el('div'); els['demo-line'].hidden = true; // the page starts with the notice line empty and hidden
doc.boot();

// poll-until: no fixed sleep; fails loudly when the condition never holds
async function until(pred, what) {
  for (let i = 0; i < 600; i++) { if (pred()) return; await new Promise((r) => setTimeout(r, 5)); }
  throw new Error('timed out waiting for ' + what);
}
function testids() { const t = []; walk(els.main, (n) => { if (n.attrs && n.attrs['data-testid']) t.push(n.attrs['data-testid']); }); return t; }
function actionButtons() { const b = []; walk(els.main, (n) => { if (n.tag === 'button' && n.attrs['data-action']) b.push(n); }); return b; }
function snap(phase) {
  const buttons = [], notes = [];
  actionButtons().forEach((n) => buttons.push({ action: n.attrs['data-action'], disabled: 'disabled' in n.attrs, outline: /btn-outline/.test(n.attrs['class'] || '') }));
  walk(els.main, (n) => { if (n.attrs && n.attrs['data-testid'] === 'needs-paused') notes.push(textOf(n)); });
  return { phase, buttons, notes };
}
const allDisabled = () => { const b = actionButtons(); return b.length > 0 && b.every((n) => 'disabled' in n.attrs); };
const allEnabled = () => { const b = actionButtons(); return b.length > 0 && b.every((n) => !('disabled' in n.attrs)); };

(async () => {
  try {
    if (scenario === 'fresh-fail') {
      await until(() => textOf(els.main).includes('The desk is not answering') && textOf(els['demo-line']).includes('Could not check'), 'the not-answering page');
      console.log(JSON.stringify({ text: textOf(els.main), line: textOf(els['demo-line']), testids: testids() }));
    } else if (scenario === 'route-proto') {
      // route ids come from the address bar: names every object has must not reach the page table or Object.prototype
      const out = [];
      await until(() => textOf(els.main).length > 0, 'the first page');
      for (const h of ['#/__proto__', '#/constructor', '#/toString', '#/prototype', '#/hasOwnProperty/x', '#/needs']) {
        win.location.hash = h; hashFns.forEach((f) => f());
        out.push({ hash: h, rendered: doc.documentElement.dataset.rendered });
      }
      const polluted = Object.keys(Object.prototype).concat(Object.keys(vm.runInContext('Object.prototype', ctx)));
      console.log(JSON.stringify({ out, polluted, unmountOnProto: vm.runInContext('Object.prototype.unmount === undefined', ctx) }));
    } else if (scenario === 'loading') {
      console.log(JSON.stringify({ text: textOf(els.main), line: textOf(els['demo-line']), testids: testids() }));
    } else if (scenario === 'cards') {
      await until(() => actionButtons().length === 2, 'two cards');
      const cards = [];
      walk(els.main, (n) => {
        if (n.attrs && n.attrs['data-testid'] === 'needs-card') {
          const b = []; walk(n, (x) => { if (x.tag === 'button' && x.attrs['data-action']) b.push(/btn-outline/.test(x.attrs['class'] || '') ? 'outline' : 'primary'); });
          cards.push({ view: n.attrs['data-view'], buttons: b, checkFirst: textOf(n).includes('Check the mail step first') });
        }
      });
      let define = ''; walk(els.main, (n) => { if (n.attrs && n.attrs['data-testid'] === 'needs-define') define = textOf(n); });
      console.log(JSON.stringify({ cards, define, text: textOf(els.main) }));
    } else if (scenario === 'demo-poll') {
      await until(() => calls.filter((c) => c.includes('/api/demo/state')).length >= 3, 'three demo polls that fail');
      await until(() => actionButtons().length > 0, 'the card');
      console.log(JSON.stringify(snap('demo-poll')));
    } else if (scenario === 'stale') {
      const out = [];
      const kpis = () => select(els.main, '.kpi');
      const marked = () => select(els.main, '.kpi-asof').length;
      const cardNotes = () => select(els.main, '.card-head .ov-asof').length;
      await until(() => kpis().length >= 4, 'the Overview numbers');
      const ticks = select(els.main, '.ov-tick').map((n) => textOf(n));
      out.push({ phase: 'answering', kpis: kpis().length, marked: marked(), cardNotes: cardNotes(), ticks, link: textOf(els.main).includes('Send a request') });
      net = 'offline'; await until(() => marked() === kpis().length, 'every number marked while offline');
      out.push({ phase: 'offline', kpis: kpis().length, marked: marked(), cardNotes: cardNotes(), cards: select(els.main, '.card').length });
      net = 'online'; await until(() => marked() === 0, 'marks removed when it answers');
      net = 'degraded'; await until(() => marked() === kpis().length, 'every number marked on a server error');
      out.push({ phase: 'degraded', kpis: kpis().length, marked: marked(), cardNotes: cardNotes() });
      net = 'online'; await until(() => marked() === 0 && cardNotes() === 0, 'marks removed after the error');
      out.push({ phase: 'recovered', kpis: kpis().length, marked: marked(), cardNotes: cardNotes() });
      console.log(JSON.stringify(out));
    } else {
      const out = [];
      await until(() => allEnabled(), 'the answering page'); out.push(snap('answering'));
      net = 'offline'; await until(allDisabled, 'buttons off while offline'); out.push(snap('offline'));
      net = 'online'; await until(allEnabled, 'buttons back'); out.push(snap('back'));
      net = 'degraded'; await until(allDisabled, 'buttons off on a server error'); out.push(snap('degraded'));
      net = 'online'; await until(allEnabled, 'buttons recovered'); out.push(snap('recovered'));
      console.log(JSON.stringify(out));
    }
  } catch (e) { console.log(JSON.stringify({ error: String(e) })); }
  process.exit(0);
})();
