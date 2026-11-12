const fs = require('fs'), vm = require('vm');
const mode = JSON.parse(process.argv[3]);
const calls = [];
function el(tag) {
  const o = { tag, dataset: {}, style: {}, children: [], nodeType: 1, namespaceURI: 'x', hidden: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {}, addEventListener() {}, appendChild(k) { this.children.push(k); return k; },
    insertBefore(k) { this.children.push(k); return k; }, querySelectorAll() { return []; }, querySelector() { return null; },
    focus() {}, remove() {}, closest() { return null; }, contains() { return false; }, get firstChild() { return this.children[0] || null; } };
  let text = '';
  Object.defineProperty(o, 'textContent', { get() { return text; }, set(v) { text = v; if (v === '') o.children = []; } });
  Object.defineProperty(o, 'isConnected', { get() { return true; } });
  return o;
}
const els = {};
const doc = { documentElement: el('html'), hidden: false, createElement: el, createElementNS: (ns, t) => el(t), createTextNode: (t) => ({ nodeType: 3, t }),
  getElementById: (id) => (els[id] = els[id] || el(id)), addEventListener(n, f) { if (n === 'DOMContentLoaded') doc.boot = f; },
  querySelectorAll() { return []; }, querySelector() { return null; }, head: el('head'), body: el('body') };
const summary = { now: 1, desk_started_at: 1, mode, counts: { received: 0, new: 0, duplicate: 0, review: 0, open: 0 }, needs_action: 0, waiting: 0 };
const win = { document: doc, location: { hash: '#/overview' }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} }, sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} }, scrollTo() {}, navigator: {} };
win.window = win;
const ctx = vm.createContext(Object.assign(win, { console, setTimeout, clearTimeout, MutationObserver: function () { this.observe = () => {}; },
  fetch: (path) => { calls.push(String(path)); const body = String(path).includes('/api/ui/summary') ? summary : (String(path).includes('/api/ui/review') ? { tabs: { action: 0 }, items: [], now: 1 } : {});
    const status = String(path).includes('/api/demo/') ? 404 : 200; return Promise.resolve({ status, text: () => Promise.resolve(JSON.stringify(body)) }); } }));
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const P = ctx.Portal;
// probe modes of the later-messages tests: time words, and the masked-by-default rule with a stubbed sessionStorage
if (process.argv[4] === 'after-first') {
  const secs = [0, 1, 59, 60, 119, 120, 3599, 3600, 7380, 86399, 86400, 172800];
  const words = secs.map((n) => P.ui.afterFirst(n, 9, 5)).concat([P.ui.afterFirst(-10, 9, 5), P.ui.afterFirst(5, 3, 5)]);
  const rel = [0, 4, 5, 60, 3600, 86400].map((k) => P.ui.relTime(1000000 - k, 1000000).children[0].t);
  console.log(JSON.stringify({ words, rel }));
  process.exit(0);
}
if (process.argv[4] === 'reveal') {
  const out = {}, writes = []; let kept = null;
  Object.defineProperty(win, 'sessionStorage', { value: { getItem: () => kept, setItem: (k, v) => { writes.push([k, v]); kept = v; }, removeItem() {} }, configurable: true });
  const events = []; P.on('reveal', () => events.push(1));
  out.start = [P.reveal.on('a'), P.reveal.on('b')];
  P.reveal.toggle('a'); out.oneOn = [P.reveal.on('a'), P.reveal.on('b')];       // two values are independent
  P.reveal.toggle('b'); out.bothOn = [P.reveal.on('a'), P.reveal.on('b')];
  P.reveal.toggle('a'); out.aOff = [P.reveal.on('a'), P.reveal.on('b')];        // a second click hides it again
  out.guardedMasked = typeof P.ui.guarded('email', 'a@example.test', 'b', 'x') === 'object';
  out.guardedEmpty = P.ui.guarded('phone', '', 'p', 'x');
  P.privacy.set(true); out.afterSwitchOn = [P.reveal.on('a'), P.reveal.on('b')]; // the switch ON clears every single reveal
  P.reveal.toggle('c'); P.reveal.clear(); out.cleared = P.reveal.on('c');       // Escape, page change and panel close call clear()
  P.privacy.set(false); out.guardedOff = P.ui.guarded('email', 'a@example.test', 'b', 'x'); // switch OFF: the plain value, no eye
  out.writes = writes.filter((w) => w[0] !== 'lead-desk-hide-personal');         // nothing but the switch is ever stored
  out.ms = P.reveal.ms; out.events = events.length;
  console.log(JSON.stringify(out));
  process.exit(0);
}
if (process.argv[4] === 'hidden') {
  const out = {};
  const store = (v) => Object.defineProperty(win, 'sessionStorage', { value: { getItem: () => v, setItem() {}, removeItem() {} }, configurable: true });
  out.none = (store(null), P.privacy.hidden());
  out.one = (store('1'), P.privacy.hidden());
  out.zero = (store('0'), P.privacy.hidden());
  out.other = (store('x'), P.privacy.hidden());
  Object.defineProperty(win, 'sessionStorage', { get() { throw new Error('blocked'); }, configurable: true });
  out.blocked = P.privacy.hidden();
  // storage that works: a set is read back from storage, so a change made in another tab is not hidden by a stale copy
  let kept = null;
  Object.defineProperty(win, 'sessionStorage', { value: { getItem: () => kept, setItem: (k, v) => { kept = v; }, removeItem() {} }, configurable: true });
  const events = []; P.on('personal', (v) => events.push(v));
  P.privacy.set(false); out.setOff = P.privacy.hidden(); out.keptOff = kept;
  kept = '1'; out.otherTab = P.privacy.hidden();
  P.privacy.set(true); out.events = events;
  // storage whose write throws: the choice lasts for this visit (memory)
  Object.defineProperty(win, 'sessionStorage', { value: { getItem: () => null, setItem() { throw new Error('blocked'); }, removeItem() {} }, configurable: true });
  const longRef = 'x'.repeat(20) + '@b.example';
  P.privacy.set(false); out.memOff = P.privacy.hidden(); out.refShownOff = P.shownRef('a@example.test');
  out.refPlainOff = P.shownRef('ui-1'); out.refShortOff = P.ui.shortRef(longRef); out.refShortPlainOff = P.ui.shortRef('ui-1');
  P.privacy.set(true); out.memOn = P.privacy.hidden(); out.refMasked = P.shownRef('a@example.test'); out.refPlain = P.shownRef('ui-1');
  out.refShort = P.ui.shortRef(longRef); out.refLongMasked = P.shownRef(longRef); out.maskedEmail = P.privacy.mask('email', 'Ada@Example.test'); out.maskedPhone = P.privacy.mask('phone', '555 0100');
  console.log(JSON.stringify(out));
  process.exit(0);
}
P.page('overview', { title: 'Overview', purpose: '', nav: { label: 'Overview', icon: 'overview', order: 1 }, mount() {} });
doc.boot();
setTimeout(() => { console.log(JSON.stringify({ calls, strip: els['demo-line'] ? els['demo-line'].children.map((c) => c.textContent || (c.children[0] && c.children[0].t) || '').join('|') : '', hidden: els['demo-line'].hidden })); process.exit(0); }, 400);
