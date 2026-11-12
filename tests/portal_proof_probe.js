// Loads demo_only/portal_demo.js against a tiny stand-in Portal and prints what the overview proof panel says for one
// set of numbers: node portal_proof_probe.js <demo js> <json {sent, count, followups?, reminders?}>
const fs = require('fs'), vm = require('vm');
const arg = JSON.parse(process.argv[3]);
function h(tag, attrs) {
  const kids = [];
  for (let i = 2; i < arguments.length; i++) { const k = arguments[i]; if (Array.isArray(k)) kids.push(...k); else if (k !== null && k !== undefined) kids.push(k); }
  return { tag, attrs, kids, appendChild(k) { kids.push(k); return k; } };
}
function text(n) { return typeof n === 'string' ? n : (n && n.kids ? n.kids.map(text).join(' ') : ''); }
let def = null;
const outbox = { count: arg.count, messages: [] };
if ('followups' in arg) outbox.followups = arg.followups;
if ('reminders' in arg) outbox.reminders = arg.reminders;
const P = { h, state: { demo: { fakes: { reachable: true, crm_mode: 'keyed', outbox_mode: 'keyed', outbox, crm: { count: 0, rows: [] } }, crash: {}, desk_started_at: 1 }, online: true },
  ui: { utc: () => 'UTC' }, stale: () => false, registerDemo(d) { def = d; }, on() {}, icon: () => null };
const win = { Portal: P, location: { search: '' } };
win.window = win;
const ctx = vm.createContext(Object.assign(win, { document: { getElementById: () => null, createTextNode: (t) => t }, console }));
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const node = def.overviewProof({ followups: { sent: arg.sent, unknown: 0 } }, P.state.demo);
const json = JSON.stringify(node);
console.log(JSON.stringify({ text: text(node), mismatch: json.includes('proof-mismatch'), reminders: json.includes('proof-reminders') }));
