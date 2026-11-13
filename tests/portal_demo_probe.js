// Loads demo_only/portal_demo.js against a tiny stand-in Portal and prints the text of the overview connection strip
// for one set of numbers: node portal_demo_probe.js <demo js> <json {count, new}>
const fs = require('fs'), vm = require('vm');
const arg = JSON.parse(process.argv[3]);
function h(tag, attrs) {
  const kids = [];
  for (let i = 2; i < arguments.length; i++) { const k = arguments[i]; if (Array.isArray(k)) kids.push(...k); else if (k !== null && k !== undefined) kids.push(k); }
  return { tag, attrs, kids, appendChild(k) { kids.push(k); return k; } };
}
function text(n) { return typeof n === 'string' ? n : (n && n.kids ? n.kids.map(text).join(' ') : ''); }
let def = null;
const P = { h, state: { demo: { fakes: { reachable: true, crm_mode: 'keyed', outbox_mode: 'keyed', outbox: { count: 1, messages: [] }, crm: { count: arg.count, rows: [] } }, crash: {}, desk_started_at: 1 }, online: true },
  ui: { utc: () => 'UTC' }, stale: () => false, registerDemo(d) { def = d; }, on() {}, icon: () => null };
const win = { Portal: P, location: { search: '' } };
win.window = win;
const ctx = vm.createContext(Object.assign(win, { document: { getElementById: () => null, createTextNode: (t) => t }, console }));
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const strip = def.overviewExtras({ counts: { new: arg.new } }, P.state.demo);
console.log(JSON.stringify({ text: text(strip), warn: JSON.stringify(strip).includes('dm-dot-warn') }));
