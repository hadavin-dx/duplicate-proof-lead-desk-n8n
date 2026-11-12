/* Lead Desk portal shell and shared parts. One global: window.Portal. Text nodes only (no HTML parsing).
   Times are epoch SECONDS. */
(function () {
  'use strict';

  var root = document.documentElement;
  var hasOwn = Object.prototype.hasOwnProperty;
  function has(o, k) { return o !== null && o !== undefined && hasOwn.call(o, k); }

  // ---- DOM builder: text nodes only, attributes from an allowlist, hrefs are in-page only
  var ATTR = { 'class': 1, id: 1, role: 1, type: 1, title: 1, tabindex: 1, colspan: 1, scope: 1, 'for': 1, disabled: 1, value: 1, selected: 1, open: 1, placeholder: 1, name: 1, autocomplete: 1, maxlength: 1, spellcheck: 1, hidden: 1, min: 1, max: 1, step: 1, checked: 1, readonly: 1, required: 1, rows: 1, autofocus: 1 };
  var SATTR = { 'class': 1, id: 1, role: 1, viewBox: 1, width: 1, height: 1, x: 1, y: 1, rx: 1, cx: 1, cy: 1, r: 1, d: 1, fill: 1, stroke: 1, points: 1, preserveAspectRatio: 1, focusable: 1, transform: 1, 'stroke-width': 1, 'stroke-dasharray': 1, 'stroke-dashoffset': 1, pathLength: 1 };
  function addKid(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { addKid(el, k); }); return; }
    el.appendChild(kid && kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  function build(el, attrs, allow, kids) {
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'href') { if (String(v).charAt(0) === '#') el.setAttribute('href', String(v)); return; }
        if (has(allow, k) || k.indexOf('aria-') === 0 || k.indexOf('data-') === 0) el.setAttribute(k, v === true ? '' : String(v));
      });
    }
    for (var i = 0; i < kids.length; i++) addKid(el, kids[i]);
    return el;
  }
  function h(tag, attrs) { return build(document.createElement(tag), attrs, ATTR, Array.prototype.slice.call(arguments, 2)); }
  // the SVG namespace is read from the icon sprite in the page, never written as a string here
  var sprite = document.getElementById('icons');
  var SVGNS = sprite.namespaceURI;
  function s(tag, attrs) { return build(document.createElementNS(SVGNS, tag), attrs, SATTR, Array.prototype.slice.call(arguments, 2)); }
  function icon(name, cls) {
    var svg = s('svg', { 'class': 'ico' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true', focusable: 'false' });
    var use = document.createElementNS(SVGNS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }
  function clear(el) { el.textContent = ''; return el; }
  function byId(id) { return document.getElementById(id); }

  // ---- tiny event bus: online, offline, restart, summary, demo
  var handlers = {};
  function on(evt, fn) {
    (handlers[evt] = handlers[evt] || []).push(fn);
    return function () { handlers[evt] = (handlers[evt] || []).filter(function (f) { return f !== fn; }); };
  }
  function emit(evt, arg) { (handlers[evt] || []).slice().forEach(function (f) { try { f(arg); } catch (e) { /* one broken subscriber must not stop the others */ } }); }

  // ---- state and labels
  var bad = {};
  var state = { summary: null, demo: null, online: null, startedAt: null, offlineSince: null, lastOk: null, needs: null };
  var labels = {
    state: { pending: 'Queued', sending: 'Sending', sent: 'Sent', failed: 'Failed', unknown: 'Outcome unknown', did_not_finish: 'Did not finish', crm_unconfirmed: 'CRM not confirmed' },
    verdict: { 'new': 'New', duplicate: 'Repeat', review: 'Request to check', open: 'Not decided yet' },
    brand: { sub: '' }, // empty = the line in index.html stays
    rating: { hot: 'Hot', warm: 'Warm', cold: 'Cold' },
    source: { form: 'Web form', webhook: 'Webhook' },
    result: { 'new': 'New lead', duplicate: 'Repeat, no second lead', review: 'Request to check', open: 'Not decided yet' },
    field: { door: 'Source', times: 'Timestamps', follow_up_state: 'Follow-up state', ai_label: 'AI rating', request_payload: 'Original request text', request_id: 'Request number', lead_id: 'Lead number', email_key: 'Email address (lower case)', name: 'Name', phone: 'Phone', company: 'Company', message: 'Message', lead_owner: 'Lead owner', lead_stage: 'Lead stage', reminder_state: 'Reminder' },
    error: { unknown_needs_person: 'the email may already have been sent. It will not send again until a person says what happened. Use “I checked: it was sent” or “I checked: it was not sent” on the card.' },
    kindHelp: { review: 'The request is incomplete, or the desk could not store or decide it. It is counted under Needs a person: a person has to look', duplicate_with_new_info: 'A repeat of an earlier request that carried new details. The first request stands.' },
    kind: { reminder_to_check: 'Reminder to check', failed: 'Follow-up failed', unknown: 'Outcome unknown', review: 'Request to check', stalled: 'Waiting too long', duplicate_with_new_info: 'Repeat, new details', replay_payload_differs: 'Same request number, different data' },
    // plain words keyed by kind or kind:reason or action id; they win over the desk's own wording, which stays as the fallback
    title: { unknown: 'The email may or may not have been sent', 'unknown:crm_unconfirmed': 'The CRM may or may not have this lead', replay_payload_differs: 'The same request came again with different details. The first one stands.', 'stalled:open receipt': 'The request arrived but has no result yet' },
    reason: { 'stalled:open receipt': 'The desk has this request but has not decided it yet. It tries again by itself. If it stays here, check the disk and the desk log.', 'stalled:pending': 'The desk tries again by itself. Retry starts it now.', 'stalled:sending': 'This follow-up has been in the same step longer than the desk expects. The desk normally retries it by itself. If it stays here, check that the desk and the mail step are running.', 'unknown:ambiguous_prior': 'An earlier attempt may already have sent the email before a stop. The desk will not send again until a person checks.', 'unknown:ambiguous': 'The send ended unclearly, so the email may have been sent. The desk will not send again until a person checks.', 'unknown:mode_mismatch': 'The mail step stored the email in a way the desk did not expect. A person needs to look.', 'unknown': 'The desk cannot tell whether the email was sent. A person needs to look.', 'review:internal_unstorable': 'The request could not be written to the desk’s records, so nothing was sent.', 'review:internal_unstorable:integrity': 'A value broke a storage rule, so the request could not be kept and nothing was sent.' },
    action: { retry: 'Retry', resolve_seen: 'I checked the mailbox' },
    confirmBody: { resolve_not_sent: 'If the email was already sent, a retry will send a second one. Check the mail step first.' },
    // one line under the confirm button: what happens next
    confirmNote: { resolve_sent: 'Next: the desk will never send this follow-up again.', resolve_not_sent: 'Next: Retry is allowed again and may send a second email.', resolve_crm_present: 'Next: Retry goes straight to the email.', resolve_seen: 'Next: the card goes away. The desk never sends this reminder.' },
    // shown on a card that has no button: what a person can do
    next: { 'review:internal_unstorable': 'No button here. Send the form again with a new request number.', 'review:internal_unstorable:integrity': 'No button here. Send the form again with a new request number.', review: 'No button here. Ask the sender to send the request again with the missing or wrong details.', stalled: 'No button here. Sending the same request again changes nothing for this item.', 'stalled:open receipt': 'No button here. Sending the same request number again lets the desk decide this request.', duplicate_with_new_info: 'No action is needed. The first request stands.', replay_payload_differs: 'No action is needed. The first request stands.' }
  };
  function mergeLabels(extra) {
    if (!extra) return;
    Object.keys(extra).forEach(function (grp) {
      labels[grp] = labels[grp] || {};
      Object.keys(extra[grp] || {}).forEach(function (k) { labels[grp][k] = extra[grp][k]; });
    });
  }
  function words(code) { var t = String(code).replace(/_/g, ' '); return t.charAt(0).toUpperCase() + t.slice(1); }

  // ---- network: same-origin JSON; rejects only when the desk cannot be reached
  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' };
    if (opts.body !== undefined && opts.body !== null) {
      init.body = JSON.stringify(opts.body);
      init.headers['Content-Type'] = 'application/json';
    }
    return fetch(path, init).then(function (res) {
      return res.text().then(function (txt) {
        var parsed = null;
        try { parsed = txt ? JSON.parse(txt) : null; } catch (e) { parsed = null; }
        var out = { status: res.status };
        out.data = parsed;
        return out;
      });
    });
  }

  // ---- polling: one timer per key, shared by every subscriber; pauses when the tab is hidden; 2 s back-off when offline
  var polls = {};
  var OFFLINE_RETRY_MS = 2000;
  function poll(key, path, ms, onData, onFail) {
    var p = polls[key];
    var sub = { ok: onData, fail: onFail };
    if (!p) {
      p = polls[key] = { path: path, ms: ms, subs: [sub], timer: null, busy: false, last: null };
      tick(key);
    } else {
      p.subs.push(sub);
      if (ms < p.ms) p.ms = ms;
      if (p.last) setTimeout(function () { if (p.subs.indexOf(sub) >= 0) onData(p.last.data, p.last.status); }, 0);
    }
    return function stop() {
      var q = polls[key];
      if (!q) return;
      q.subs = q.subs.filter(function (x) { return x !== sub; });
      if (!q.subs.length) { clearTimeout(q.timer); delete polls[key]; delete bad[key]; }
    };
  }
  function schedule(key, delay) {
    var p = polls[key];
    if (!p) return;
    clearTimeout(p.timer);
    p.timer = setTimeout(function () { tick(key); }, delay);
  }
  function tick(key) {
    var p = polls[key];
    if (!p || p.busy) return;
    if (document.hidden) { schedule(key, p.ms); return; }
    p.busy = true;
    api(p.path).then(function (r) {
      p.busy = false;
      if (!polls[key]) return;
      p.last = r;
      if (r.status < 500) state.lastOk = Date.now();
      bad[key] = r.status >= 500;
      if (key === 'summary') checkingLine(r.status !== 200);
      setOnline(true);
      paintLive();
      p.subs.slice().forEach(function (x) { try { x.ok(r.data, r.status); } catch (e) { /* one broken subscriber must not stop the others */ } });
      schedule(key, p.ms);
    }, function (err) {
      p.busy = false;
      if (!polls[key]) return;
      if (key === 'summary') checkingLine(true);
      setOnline(false);
      p.subs.slice().forEach(function (x) { try { if (x.fail) x.fail(err); } catch (e) { /* one broken subscriber must not stop the others */ } });
      schedule(key, Math.min(p.ms, OFFLINE_RETRY_MS));
    });
  }
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) Object.keys(polls).forEach(function (k) { if (!polls[k].busy) tick(k); });
  });

  // ---- time
  function pad2(n) { return n < 10 ? '0' + n : String(n); }
  function clock(ms) { var d = new Date(ms); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds()); }
  function utc(epoch, secs) { return new Date(epoch * 1000).toISOString().replace('T', ' ').slice(0, secs ? 19 : 16) + ' UTC'; }
  // one human form for references everywhere: a short request number as it is, a long one as an ellipsis plus its last 6 characters,
  // and a lead number as its first 6 characters (after the L) in capitals; the full value is in the tooltip and behind the copy button
  // a request number is data the sender chose (a form id, an order number, sometimes a name or an address), so it is personal
  // until proven otherwise: while the switch is on it is masked like a lead name, with its own eye (see privacy below)
  function shownRef(id) { return hidePersonal() ? '••••••' : String(id); }
  function shortRef(id) { var t = shownRef(id); return t.length > 14 ? '…' + t.slice(-6) : t; }
  // a request number as the page shows it: plain, or (while the switch is on) masked with an eye
  function refNode(id, prefix) {
    var t = String(id), p = prefix || '', s = t.length > 14 ? '…' + t.slice(-6) : t;
    if (!hidePersonal()) return p + s;
    return h('span', null, p, guarded('ref', s, valueKey('ref', t), '••••••'));
  }
  function requestRef(id) { return 'Request ' + shortRef(id); }
  function leadRef(id) { var t = String(id); return 'Lead ' + t.replace(/^L/, '').slice(0, 6).toUpperCase(); }
  // one rule for every duration on every page: whole seconds, minutes, hours or days, rounded down
  function duration(sec) {
    var n;
    if (sec < 60) return sec + (sec === 1 ? ' second' : ' seconds');
    if (sec < 3600) { n = Math.floor(sec / 60); return n + (n === 1 ? ' minute' : ' minutes'); }
    if (sec < 86400) { n = Math.floor(sec / 3600); return n + (n === 1 ? ' hour' : ' hours'); }
    n = Math.floor(sec / 86400); return n + (n === 1 ? ' day' : ' days');
  }
  // short form for a time stamp cell: 34s, 2m, 3h, 4d; the full time is in the tooltip
  function ago(sec) { return sec < 5 ? 'just now' : sec < 60 ? sec + 's ago' : sec < 3600 ? Math.floor(sec / 60) + 'm ago' : sec < 86400 ? Math.floor(sec / 3600) + 'h ago' : Math.floor(sec / 86400) + 'd ago'; }
  // a later message's place in time, from the desk's own numbers; never names a cause
  function afterFirst(sec, rid, firstRid) {
    if (rid < firstRid) return 'Arrived before the first record was written';
    if (sec < 0) return 'Arrived after the first message; the desk clock gave it an earlier time, so no gap is shown';
    if (sec === 0) return 'In the same second as the first message';
    return duration(sec) + ' after the first message';
  }
  function relTime(epoch, now) {
    if (epoch === null || epoch === undefined) return h('span', { 'class': 'faint tl-hint', title: 'The desk did not save the time of this step.' }, 'time not saved');
    var ref = now === undefined || now === null ? Math.floor(Date.now() / 1000) : now;
    var d = new Date(epoch * 1000);
    var title = d.toLocaleString() + ' local, ' + d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
    var el = h('time', { title: title, 'class': 'num' }, ref - epoch < 0 ? 'in ' + (epoch - ref) + ' seconds' : ago(ref - epoch));
    el.setAttribute('datetime', d.toISOString());
    return el;
  }

  // ---- privacy: lead names, email addresses, phone numbers and messages are masked on screen unless this browser's switch is off.
  // Default = masked (first visit, a new browser session, or storage unavailable). The choice is kept for this browser tab's
  // session only (session storage, never local storage), so a shown state cannot outlive the session; every page and panel
  // goes through mask(), so there is one rule. The desk's JSON still carries the values (a person with devtools sees them).
  var HIDE_KEY = 'lead-desk-hide-personal';
  var hideMem = null; // only used when storage is blocked: the choice then lasts for this visit
  function hidePersonal() {
    if (hideMem !== null) return hideMem;
    try { return window.sessionStorage.getItem(HIDE_KEY) !== '0'; } catch (e) { return true; }
  }
  function setHidePersonal(on) {
    try { window.sessionStorage.setItem(HIDE_KEY, on ? '1' : '0'); hideMem = null; } catch (e) { hideMem = !!on; }
    if (hidePersonal()) clearReveals(true); // the switch ON masks everything again
    emit('personal', hidePersonal());
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') clearReveals(); });
  var DOTS = '••••••';

  // ---- one value shown on purpose: a small eye button beside a masked value reveals THAT value only (memory only, never
  // stored, never in an address, a tooltip or a log). It hides again after REVEAL_MS, on Escape, on a page change, when
  // the panel closes and when the global switch is turned on. Showing one value does not turn on searching by address.
  var REVEAL_MS = 30000;
  var revealed = Object.create(null);
  var REVEAL_WORDS = { name: 'name', email: 'email address', phone: 'phone number', message: 'message', ref: 'request number' };
  // a key for a value that must not sit in the page markup (an address-like request number, the sender address): kind + two hashes
  function valueKey(kind, s) {
    var a = 5381, b = 52711, t = String(s);
    for (var i = 0; i < t.length; i++) { var c = t.charCodeAt(i); a = ((a * 33) ^ c) >>> 0; b = ((b * 31) + c) >>> 0; }
    return kind + ':' + a.toString(36) + b.toString(36);
  }
  function revealOn(key) { return key in revealed; }
  function clearReveals(quiet) {
    var any = false;
    Object.keys(revealed).forEach(function (k) { clearTimeout(revealed[k]); any = true; });
    revealed = Object.create(null);
    if (any && !quiet) emit('reveal');
  }
  function toggleReveal(key) {
    if (revealOn(key)) { clearTimeout(revealed[key]); delete revealed[key]; }
    else revealed[key] = setTimeout(function () { delete revealed[key]; emit('reveal'); }, REVEAL_MS);
    emit('reveal');
    // the button was drawn again: give the keyboard its place back
    var again = document.querySelectorAll('[data-guard-key]');
    for (var i = 0; i < again.length; i++) { if (again[i].getAttribute('data-guard-key') === key) { again[i].focus(); break; } }
  }
  // the value (text) with its eye; the switch OFF shows the plain value with no eye; an empty value stays as it is.
  // key = lead id + field + which record, so a repaint or a poll never re-masks or reveals the wrong cell
  function guarded(field, v, key, maskedText) {
    if (!hidePersonal() || v === null || v === undefined || v === '') return v;
    var on = revealOn(key), word = REVEAL_WORDS[field] || field;
    var val = on ? h('span', { 'class': 'guard-val' }, String(v))
      : h('span', { 'class': 'guard-val is-masked' }, h('span', { 'aria-hidden': 'true' }, maskedText), h('span', { 'class': 'sr-only' }, 'hidden ' + word));
    var label = (on ? 'Hide ' : 'Show ') + word;
    var btn = h('button', { 'class': 'eye-btn', type: 'button', 'aria-pressed': on ? 'true' : 'false', 'aria-label': label, 'data-guard-key': key,
      title: on ? 'Hide this ' + word + ' again' : 'Show this ' + word + ' (hides again after 30 seconds)' }, icon('eye'));
    btn.addEventListener('click', function (e) { e.stopPropagation(); toggleReveal(key); });
    btn.addEventListener('keydown', function (e) { if (e.key === 'Escape') clearReveals(); else e.stopPropagation(); }); // Enter and Space press the button, they do not open the row behind it; Escape hides every single reveal
    return h('span', { 'class': 'guard' }, val, btn);
  }
  function maskEmail(v) { return String(v || '').indexOf('@') >= 0 ? '•••@•••••' : DOTS; } // no character of the address is kept
  function maskPhone(v) { return v ? '••• ••• ••••' : v; }
  function mask(field, v) {
    if (!hidePersonal() || v === null || v === undefined || v === '') return v;
    if (field === 'email' || field === 'email_key') return maskEmail(v);
    if (field === 'phone') return maskPhone(v);
    if (field === 'message' || field === 'name') return DOTS;
    return v;
  }
  // a lead's name: masked with its own eye like an address; a person who works the desk (a lead owner, an approver) is not masked
  function shownName(v) { return guarded('name', v, valueKey('name', v), DOTS); }

  // ---- components
  // ONE colour per follow-up state, used by the badge, the ring and its legend, the timeline dot and the feed icon
  var STATE_TONE = { pending: 'pending', sending: 'info', sent: 'ok', failed: 'bad', did_not_finish: 'warn', unknown: 'hot', crm_unconfirmed: 'teal' };
  function stateTone(st) { return Object.prototype.hasOwnProperty.call(STATE_TONE, st) ? STATE_TONE[st] : 'neutral'; }
  function pillTone(st) {
    if (Object.prototype.hasOwnProperty.call(STATE_TONE, st)) return STATE_TONE[st];
    return { 'new': 'ok', duplicate: 'calm', review: 'warn', open: 'pending', hot: 'hot', warm: 'warm', cold: 'calm' }[st] || 'neutral';
  }
  function pill(st, opts) {
    var text = (opts && opts.text) || (labels.state[st] || labels.verdict[st] || labels.rating[st] || words(st));
    if (labels.rating[st] && !labels.state[st]) return h('span', { 'class': 'pill pill-rating pill-rating-' + st, 'data-status': st }, labels.rating[st]);
    var lead = st === 'failed' || st === 'did_not_finish' ? icon('alert') : st === 'unknown' || st === 'crm_unconfirmed' ? icon('help') : h('span', { 'class': 'dot', 'aria-hidden': 'true' });
    var gloss = { unknown: 'The desk cannot tell whether this email was sent', did_not_finish: 'The desk tried this follow-up and stopped before it finished. The email may already have been sent.', crm_unconfirmed: 'The desk cannot tell whether the CRM holds this lead. The follow-up is waiting for the CRM check, so no email was sent.', pending: 'Waiting in line to be sent', sending: 'The desk is sending it now', open: 'The desk has stored this request but has not decided it yet', review: 'The desk would not decide this request alone: a person has to look' }[st];
    return h('span', { 'class': 'pill pill-' + pillTone(st), 'data-status': st, title: (opts && opts.title) || gloss || null }, lead, text);
  }
  // the Follow-up column of a list shows the EMAIL state; a lead whose CRM entry is unclear shows that on its own chip
  function followupChips(view, st) {
    if (view !== 'crm_unconfirmed') return pill(view || st);
    return h('div', { 'class': 'chip-stack' }, waitingChip(), pill('crm_unconfirmed'));
  }
  // the ONE phrase for a follow-up that waits for the CRM check: the list chip, the lead panel and the Needs card all say it
  function waitingChip() { return pill('pending', { text: 'Waiting for the CRM check', title: 'No email has been sent. The follow-up waits for the CRM check.' }); }
  function card(o) {
    var tip = o.tip ? h('span', { 'class': 'info-tip', tabindex: '0', role: 'img', 'aria-label': o.tip, title: o.tip }, icon('info')) : null;
    var head = h('div', { 'class': 'card-head' }, h('h2', null, o.title, tip), o.why ? h('p', { 'class': 'why' }, o.why) : null);
    return h('section', { 'class': 'card' + (o.cls ? ' ' + o.cls : ''), 'data-testid': o.testid }, o.title ? head : null, o.body);
  }
  function kpiTile(o) {
    var tone = o.tone && o.tone !== 'neutral' ? ' kpi-' + o.tone : '';
    var shown = o.value === null || o.value === undefined ? '–' : (typeof o.value === 'number' ? o.value.toLocaleString() : o.value);
    return h('article', { 'class': 'kpi' + tone, 'data-testid': o.testid },
      h('div', { 'class': 'kpi-label' }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), h('span', null, o.label)),
      h('div', { 'class': 'kpi-value', 'data-testid': o.testid ? o.testid + '-value' : null }, shown),
      o.caption ? h('div', { 'class': 'kpi-caption' }, o.caption) : null);
  }
  function emptyState(o) {
    return h('div', { 'class': 'state', 'data-testid': 'empty-state' }, icon('info'), h('h2', null, o.heading), o.sentence ? h('p', null, o.sentence) : null);
  }
  function lastUpdated() { return state.lastOk ? 'Last updated ' + utc(Math.floor(state.lastOk / 1000)).slice(11) : 'Nothing has loaded yet'; }
  function errorState(o) {
    var offline = !o.status;
    var btn = o.retry ? h('button', { 'class': 'btn btn-primary', type: 'button', 'data-testid': 'error-retry' }, 'Try again') : null;
    if (btn) btn.addEventListener('click', function () { o.retry(); });
    if (offline) {
      // a page that names its own heading leaves the time to the notice bar above, so it is said once
      return h('div', { 'class': 'state state-error state-offline', role: 'alert', 'data-testid': 'error-state' }, icon('alert'), h('h2', null, o.heading || 'The desk is not answering'),
        h('p', null, (o.heading ? 'The desk is not answering. ' : 'It may be stopped or restarting. ') + 'This page reconnects by itself.'), o.heading ? null : h('p', { 'class': 'state-note', 'data-testid': 'last-updated' }, lastUpdated() + '.'), btn);
    }
    return h('div', { 'class': 'state state-error', role: 'alert', 'data-testid': 'error-state' }, icon('alert'), h('h2', null, o.heading || 'Could not load ' + o.what), h('p', null, 'The desk gave no usable answer this time. Try again in a moment.'),
      h('p', { 'class': 'state-note', 'data-testid': 'last-updated' }, state.lastOk ? 'Last answer that worked: ' + utc(Math.floor(state.lastOk / 1000)) + '.' : 'Nothing has loaded yet.'),
      btn, h('details', { 'class': 'details err-detail' }, h('summary', null, 'Technical details (the code the desk answered with)'), h('p', { 'class': 'mono' }, 'The desk answered with code ' + o.status + '.')));
  }
  // label: the word above the stripes ('Loading…' when left out, none when null); blocks: full-width card-shaped blocks instead of stripes
  function skeleton(rows, label, blocks) {
    var out = h('div', { 'class': 'stack', role: 'status', 'data-testid': 'skeleton' }, label === null ? null : h('p', { 'class': 'skel-label muted' }, label || 'Loading…'));
    for (var i = 0; i < (rows || 4); i++) out.appendChild(h('span', { 'class': blocks ? 'skel skel-card' : 'skel' }));
    return out;
  }
  function table(o) {
    var head = h('tr', null, o.columns.map(function (c) { return h('th', { scope: 'col', 'class': c.align ? 'al-' + c.align : null }, c.label); }));
    var tbody = h('tbody');
    (o.rows || []).forEach(function (row) {
      var attrs = o.rowAttrs ? o.rowAttrs(row) : {};
      var tr = h('tr', null);
      Object.keys(attrs).forEach(function (k) { tr.setAttribute(k, attrs[k]); });
      if (o.rowTestid) tr.setAttribute('data-testid', o.rowTestid);
      o.columns.forEach(function (c) {
        var v = c.render ? c.render(row) : row[c.key];
        tr.appendChild(h('td', { 'class': c.align ? 'al-' + c.align : null }, v === null || v === undefined ? '–' : v));
      });
      if (o.onOpen) {
        tr.className = 'is-clickable';
        tr.tabIndex = 0;
        tr.addEventListener('click', function () { o.onOpen(row, tr); });
        tr.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); o.onOpen(row, tr); } });
      }
      tbody.appendChild(tr);
    });
    var wrap = h('div', { 'class': 'table-wrap', 'data-testid': o.testid }, h('table', { 'class': 'table' }, h('thead', null, head), tbody));
    if (!(o.rows || []).length && o.empty) {
      wrap.appendChild(h('div', { 'class': 'table-empty' }, o.empty.nodeType ? o.empty : emptyState(o.empty)));
    }
    return wrap;
  }
  function splitBar(parts, o) {
    o = o || {};
    var total = parts.reduce(function (a, p) { return a + (p.value || 0); }, 0);
    var W = 1000;
    var svg = s('svg', { viewBox: '0 0 ' + W + ' 12', preserveAspectRatio: 'none', role: 'img', 'aria-label': (o.label || 'Split') + ': ' + parts.map(function (p) { return p.label + ' ' + p.value; }).join(', ') });
    var x = 0;
    parts.forEach(function (p) {
      if (!total || !p.value) return;
      var w = Math.round(W * p.value / total);
      svg.appendChild(s('rect', { x: x, y: 0, width: w, height: 12, 'class': 'seg-' + (p.tone || 'neutral') }));
      x += w;
    });
    var legend = h('ul', { 'class': 'legend' }, parts.map(function (p) {
      return h('li', null, h('span', { 'class': 'sw sw-' + (p.tone || 'neutral'), 'aria-hidden': 'true' }), p.label + ' ', h('b', null, p.value));
    }));
    return h('div', { 'class': 'splitbar bar', 'data-testid': o.testid }, svg, legend);
  }

  // ring chart of a real split (same parts as splitBar); the centre number is the total
  function donut(parts, o) {
    o = o || {};
    var total = parts.reduce(function (a, p) { return a + (p.value || 0); }, 0);
    var svg = s('svg', { viewBox: '0 0 120 120', role: 'img', 'class': 'donut-svg', 'aria-label': (o.label || 'Split') + ': ' + parts.map(function (p) { return p.label + ' ' + p.value; }).join(', ') });
    svg.appendChild(s('circle', { cx: 60, cy: 60, r: 48, fill: 'none', 'stroke-width': 14, 'class': 'arc-track' }));
    var used = 0, shown = parts.filter(function (p) { return total && p.value; }).length;
    parts.forEach(function (p) {
      if (!total || !p.value) return;
      var len = 100 * p.value / total;
      var gap = shown > 1 ? Math.min(2, len / 2.5) : 0; // a clear gap, so two small neighbours (red, orange) never touch
      svg.appendChild(s('circle', { cx: 60, cy: 60, r: 48, fill: 'none', 'stroke-width': 14, pathLength: 100, 'stroke-dasharray': (len - gap) + ' ' + (100 - len + gap), 'stroke-dashoffset': -used, transform: 'rotate(-90 60 60)', 'class': 'arc-' + (p.tone || 'neutral') }));
      used += len;
    });
    // whole percentages that add up to 100 (largest remainder), so a split never reads 101%
    var pcts = parts.map(function (p) { return total ? Math.floor(100 * p.value / total) : 0; });
    var left = total ? 100 - pcts.reduce(function (a, x) { return a + x; }, 0) : 0;
    parts.map(function (p, i) { return [i, total ? (100 * p.value / total) % 1 : 0]; }).sort(function (a, b) { return b[1] - a[1]; }).slice(0, left).forEach(function (x) { pcts[x[0]] += 1; });
    var legend = h('ul', { 'class': 'legend legend-col' }, parts.map(function (p, i) {
      var pct = total ? pcts[i] + '%' : '';
      return h('li', null, h('span', { 'class': 'sw sw-' + (p.tone || 'neutral'), 'aria-hidden': 'true' }), h('span', { 'class': 'legend-label' }, p.label), h('b', { 'class': 'legend-n' }, p.value), h('span', { 'class': 'faint legend-pct' }, pct));
    }));
    return h('div', { 'class': 'donut', 'data-testid': o.testid },
      h('div', { 'class': 'donut-ring' }, svg, h('div', { 'class': 'donut-mid' }, h('b', { 'class': 'num' }, String(total)), h('span', null, o.unit || 'total'))), legend);
  }

  // dialogs: native <dialog>, focus handled by the browser (trapped while modal, returned on close)
  function drawer(o) {
    var dlg = h('dialog', { 'class': 'drawer', 'aria-label': o.label || o.title, 'data-testid': o.testid || 'drawer' });
    var closeBtn = h('button', { 'class': 'icon-btn', type: 'button', 'aria-label': 'Close', 'data-testid': 'drawer-close' }, icon('close'));
    var heading = h('h2', null, o.title);
    dlg.appendChild(h('div', { 'class': 'drawer-head' }, heading, o.pill || null, h('span', { 'class': 'spacer' }), closeBtn));
    var body = h('div', { 'class': 'drawer-body' }, o.body);
    dlg.appendChild(body);
    var more = h('div', { 'class': 'drawer-more', 'aria-hidden': 'true', hidden: true }, 'More below');
    dlg.appendChild(more);
    function moreBelow() { var m = body.scrollHeight - body.scrollTop - body.clientHeight > 8; more.hidden = !m; dlg.classList.toggle('has-more', m); }
    body.addEventListener('scroll', moreBelow);
    new MutationObserver(moreBelow).observe(body, { childList: true, subtree: true });
    var closed = false;
    function close() { if (dlg.open) dlg.close(); }
    closeBtn.addEventListener('click', close);
    dlg.addEventListener('click', function (e) { if (e.target === dlg) close(); });
    dlg.addEventListener('close', function () {
      if (closed) return;
      closed = true;
      dlg.remove();
      if (o.onClose) o.onClose();
    });
    document.body.appendChild(dlg);
    dlg.showModal();
    moreBelow();
    function setTitle(node, label) { clear(heading).appendChild(node && node.nodeType ? node : document.createTextNode(String(node))); dlg.setAttribute('aria-label', label); }
    return { el: dlg, body: body, close: close, setTitle: setTitle };
  }
  function confirmDialog(o) {
    return new Promise(function (resolve) {
      clear(byId('toasts')); // an older message must not sit behind the question
      var warn = o.tone === 'warn';
      var dlg = h('dialog', { 'class': 'dialog' + (warn ? ' dialog-warn' : ''), 'aria-labelledby': 'confirm-title', 'data-testid': 'confirm-dialog' });
      var cancel = h('button', { 'class': 'btn', type: 'button', 'data-testid': 'confirm-cancel' }, 'Cancel');
      var ok = h('button', { 'class': 'btn ' + (warn ? 'btn-warn' : 'btn-primary'), type: 'button', 'data-testid': 'confirm-ok' }, o.verb || 'Confirm');
      var result = false;
      dlg.appendChild(h('div', { 'class': 'dialog-head' }, warn ? icon('alert') : null, h('h2', { id: 'confirm-title' }, o.title)));
      dlg.appendChild(h('p', null, o.body));
      if (o.note) dlg.appendChild(h('p', { 'class': 'dialog-note', 'data-testid': 'confirm-note' }, o.note));
      dlg.appendChild(h('div', { 'class': 'dialog-actions' }, cancel, ok));
      cancel.addEventListener('click', function () { dlg.close(); });
      ok.addEventListener('click', function () { result = true; dlg.close(); });
      dlg.addEventListener('close', function () { dlg.remove(); resolve(result); });
      document.body.appendChild(dlg);
      dlg.showModal();
      cancel.focus(); // the safe choice has focus first: these actions cannot be undone
    });
  }
  function toast(text, kind) {
    var k = kind || 'info';
    var ic = { info: 'info', ok: 'check', warn: 'alert', error: 'alert' }[k] || 'info';
    var x = h('button', { 'class': 'toast-x', type: 'button', 'aria-label': 'Dismiss' }, icon('close'));
    var el = h('div', { 'class': 'toast toast-' + k, 'data-testid': 'toast' }, icon(ic), h('div', { 'class': 'toast-text' }, text), x);
    var region = byId('toasts');
    region.appendChild(el);
    while (region.children.length > 4) region.removeChild(region.firstChild);
    function gone() { if (el.parentNode) el.parentNode.removeChild(el); }
    el.addEventListener('click', gone);
    if (k !== 'error') setTimeout(gone, k === 'warn' ? 10000 : 4000); // errors stay until closed (click)
  }

  // ---- pages, demo extension, router
  var pages = Object.create(null); // no inherited names: an id from the address bar can only hit a registered page
  var current = null;
  var demo = null;
  var routeForce = false, routed = false, booted = false; // declared before the functions that read them
  function page(id, def) { pages[id] = def; def.id = id; if (booted) renderNav(); }
  function registerDemo(d) {
    demo = d || {};
    window.Portal.demo = window.Portal.demoExt = demo;
    mergeLabels(demo.labels);
    var line = demo.demoLine || (demo.labels && demo.labels.demoLine);
    if (typeof line === 'string') {
      var strip = byId('demo-line');
      delete strip.dataset.placeholder;
      var lead = /^Demo\W+/i.test(line);
      clear(strip).appendChild(h('span', { 'class': 'tag' }, lead ? 'Demo' : 'DEMO'));
      if (lead) strip.appendChild(h('span', { 'class': 'sr-only' }, ':'));
      var rest = line.replace(/^Demo\W+/i, '');
      strip.appendChild(h('span', null, ' ' + rest.charAt(0).toUpperCase() + rest.slice(1)));
      strip.hidden = false;
      try { window.localStorage.setItem('lead-desk-demo-line', line); } catch (e) { /* storage blocked: the line is fetched again when the desk answers */ }
    }
    if (demo.panel) {
      var nav = demo.navItem || {};
      pages.demo = {
        id: 'demo', title: demo.panel.title || 'Demo controls', purpose: demo.panel.purpose || '',
        nav: { label: nav.label || 'Demo controls', icon: nav.icon || 'demo', order: nav.order || 9, group: 'demo' },
        mount: function (r, ctx) { demo.panel.mount.call(demo.panel, r, ctx); },
        unmount: function () { if (demo.panel.unmount) demo.panel.unmount(); }
      };
    }
    renderNav();
    if (booted) { if (current === 'demo') routeForce = true; route(); }
    emit('demo', state.demo);
  }
  // when the desk is down at page load, the demo line and the Demo nav item still show (from the last visit), so the disclosure is never lost
  function restoreDemo() {
    var line = null;
    try { line = window.localStorage.getItem('lead-desk-demo-line'); } catch (e) { line = null; }
    if (typeof line !== 'string' || !line) return;
    registerDemo({ demoLine: line, navItem: { label: 'Demo controls', icon: 'demo', order: 9 }, panel: { title: 'Demo controls', purpose: '', mount: function (r) { r.appendChild(errorState({ what: 'the demo controls' })); } } });
  }
  function renderNav() {
    var nav = clear(byId('nav'));
    var navDemo = clear(byId('nav-demo'));
    var ids = Object.keys(pages).sort(function (a, b) { return (pages[a].nav.order || 0) - (pages[b].nav.order || 0); });
    ids.forEach(function (id) {
      var p = pages[id];
      var link = h('a', { href: '#/' + id, 'data-testid': 'nav-' + id, 'data-route': id }, icon(p.nav.icon), h('span', null, p.nav.label));
      if (id === 'needs') link.appendChild(h('span', { 'class': 'badge', 'data-testid': 'needs-badge', hidden: true }, ''));
      if (id === current) link.setAttribute('aria-current', 'page');
      (p.nav.group === 'demo' ? navDemo : nav).appendChild(link);
    });
    if (navDemo.children.length) navDemo.insertBefore(h('div', { 'class': 'nav-label' }, 'Demo'), navDemo.firstChild);
    navDemo.hidden = !navDemo.children.length;
    paintBadge();
  }
  // ONE definition of "needs a person": the Needs a person tab total from the review list (falls back to the summary field)
  function needsCount() {
    if (state.needs !== null) return state.needs;
    if (state.summary) return state.summary.needs_action;
    try { return state.online === false ? parseInt(window.localStorage.getItem('lead-desk-needs'), 10) || 0 : 0; } catch (e) { return 0; } // offline at page load: the last known count, so the badge does not vanish
  }
  function onReview(d, status) {
    if (status !== 200 || !d || !d.tabs) return;
    state.needs = d.tabs.action || 0;
    try { window.localStorage.setItem('lead-desk-needs', String(state.needs)); } catch (e) { /* storage blocked */ }
    paintBadge();
    emit('summary', state.summary);
  }
  function paintBadge() {
    var b = document.querySelector('[data-testid="needs-badge"]');
    if (!b) return;
    var n = needsCount();
    b.hidden = !n;
    b.textContent = n ? String(n) : '';
    b.setAttribute('title', n ? n + (n === 1 ? ' item needs a person' : ' items need a person') + ' (Needs a person tab only)' : '');
    b.setAttribute('data-stale', state.online === false || !!bad.summary ? 'true' : 'false'); // the number is the last known one
  }
  var stopPage = null;
  function route() {
    var id = (location.hash.replace(/^#\//, '') || 'overview').split('/')[0];
    if (id === '__proto__' || id === 'constructor' || id === 'prototype' || !Object.prototype.hasOwnProperty.call(pages, id)) id = 'overview';
    if (!Object.prototype.hasOwnProperty.call(pages, id)) return;
    if (id === current && byId('page-body') && !routeForce) return;
    routeForce = false;
    clearReveals(true);
    clear(byId('toasts'));
    if (current && pages[current] && pages[current].unmount) { try { pages[current].unmount(); } catch (e) { /* one broken subscriber must not stop the others */ } }
    var p = pages[id];
    current = id;
    if (id !== 'demo') p.unmount = null; // a page may set this.unmount inside mount()
    var main = clear(byId('main'));
    var h1 = h('h1', { tabindex: '-1', id: 'page-title' }, p.title);
    main.appendChild(h('div', { 'class': 'page-head' }, h1, p.purpose ? h('p', null, p.purpose) : null));
    var body = h('div', { 'class': 'page-body', id: 'page-body', 'data-page': id });
    main.appendChild(body);
    document.title = p.title + ' · Lead Desk';
    clear(byId('crumb')).appendChild(h('span', null, 'Lead Desk / ', h('b', null, p.nav.label)));
    document.querySelectorAll('#nav a, #nav-demo a').forEach(function (a) {
      if (a.getAttribute('data-route') === id) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    root.dataset.rendered = id;
    try { p.mount(body, { route: id, state: state }); } catch (e) {
      body.appendChild(errorState({ what: p.title }));
    }
    if (booted && routed) { h1.focus(); window.scrollTo(0, 0); }
    routed = true;
    pollDemo();
  }
  function go(r) { location.hash = '#/' + r; }

  // ---- connection state: live dot, offline banner, restart detection
  // stale = the desk is offline or answers with a problem: what a page shows is the last known state, so actions wait ("link" event on change)
  var wasStale = false;
  // Only the two polls that every page's numbers come from count: the demo extension's own poll failing must not freeze the buttons.
  var STALE_KEYS = ['summary', 'review'];
  function stale() { return state.online === false || (state.online === true && STALE_KEYS.some(function (k) { return bad[k] && polls[k]; })); }
  // the pill says Live only while the desk answers without a server error
  function paintLive() {
    var st = state.online === false ? 'offline' : stale() ? 'degraded' : 'live';
    byId('live').setAttribute('data-state', st);
    byId('live-text').textContent = { live: 'Live', offline: 'Offline', degraded: 'Problem' }[st];
    paintBanner(st);
    paintBadge();
    var now = stale();
    if (now !== wasStale) { wasStale = now; emit('link', now); }
  }
  // the top notice: the desk is offline, or it answers with a problem; both keep the last values on screen
  function paintBanner(st) {
    var banner = byId('offline-banner');
    if (st === 'degraded' && state.summary) {
      clear(banner).appendChild(icon('alert'));
      banner.appendChild(h('span', null, 'The desk answered with a problem. Showing the last values. Trying again automatically.'));
      banner.appendChild(h('span', { 'class': 'when', 'data-testid': 'last-updated-banner' }, lastUpdated()));
      banner.hidden = false;
    } else if (st !== 'offline') banner.hidden = true;
  }
  function setOnline(v) {
    if (state.online === v) return;
    var was = state.online;
    state.online = v;
    paintLive();
    var banner = byId('offline-banner');
    if (v) {
      banner.hidden = true;
      state.offlineSince = null;
      if (was === false) emit('online');
    } else {
      state.offlineSince = Date.now();
      clear(banner).appendChild(icon('alert'));
      banner.appendChild(h('span', null, 'The desk is offline. ' + (state.summary ? 'Showing the last values. ' : '') + 'Reconnecting automatically.'));
      banner.appendChild(h('span', { 'class': 'when', 'data-testid': 'last-updated-banner' }, lastUpdated()));
      banner.hidden = !state.summary;
      paintBadge();
      emit('offline');
    }
  }
  function onSummary(d, status) {
    if (status !== 200 || !d) return;
    state.summary = d;
    if (state.startedAt !== null && d.desk_started_at !== state.startedAt) emit('restart', d.desk_started_at);
    state.startedAt = d.desk_started_at;
    paintBadge();
    emit('summary', d);
    modeSeen(d.mode);
  }
  // the desk says whether the demo extension is loaded (summary.mode): no request goes to the extension's routes when it is not
  var demoTools = null;
  var demoKnown = false, demoLoaded = false, stopDemo = null, demoMs = 0; // declared before modeSeen, which reads them
  function modeSeen(mode) {
    if (!mode || typeof mode !== 'object') return;
    if (mode.demo_tools === true) { demoTools = true; byId('demo-line').classList.remove('is-real'); pollDemo(); return; }
    demoTools = false;
    if (stopDemo) { stopDemo(); stopDemo = null; }
    demoKnown = true; state.demo = null; sourceChip(false);
    try { window.localStorage.removeItem('lead-desk-demo-line'); } catch (e) { /* storage blocked */ }
    if (demo && !demoLoaded && pages.demo) { delete pages.demo; demo = null; window.Portal.demo = window.Portal.demoExt = null; renderNav(); if (current === 'demo') go('overview'); }
    var strip = byId('demo-line');
    delete strip.dataset.placeholder;
    var REAL_NAMES = { crm: 'CRM', mail: 'mail', ai: 'AI step' };
    var real = Array.isArray(mode.real_services) ? mode.real_services.map(function (k) { return REAL_NAMES[k] || k; }) : [];
    strip.classList.toggle('is-real', mode.sample_data !== true && real.length > 0); // the real-services notice is warn-toned
    if (mode.sample_data === true) {
      clear(strip).appendChild(h('span', { 'class': 'tag' }, 'DEMO'));
      strip.appendChild(h('span', null, ' Every lead is demo data. The demo CRM and demo mail are not connected. Nothing here is sent to real customers or real systems.'));
      strip.hidden = false;
    } else if (real.length) {
      clear(strip).appendChild(h('span', { 'class': 'tag' }, 'REAL'));
      strip.appendChild(h('span', null, ' Real services are connected: ' + real.join(', ') + '. Lead data this desk handles may be sent to them.'));
      strip.hidden = false;
    } else { clear(strip); strip.hidden = true; }
  }

  // ---- demo extension discovery: 200 = load it, 404 = none, anything else = say the source is not confirmed
  function sourceChip(show) {
    var slot = clear(byId('source-slot'));
    if (show) slot.appendChild(h('span', { 'class': 'chip-warn', 'data-testid': 'source-unconfirmed' }, icon('alert'), 'Demo data not confirmed'));
  }
  function loadDemoAssets() {
    if (demoLoaded) return;
    demoLoaded = true;
    var link = document.createElement('link');
    link.setAttribute('rel', 'stylesheet');
    link.setAttribute('href', '/ui/demo/portal_demo.css');
    document.head.appendChild(link);
    var sc = document.createElement('script');
    sc.setAttribute('src', '/ui/demo/portal_demo.js');
    document.head.appendChild(sc);
  }
  // the line kept free for the demo notice: while the first answer is awaited it says it is checking; it never claims demo without an answer
  function checkingLine(failed) {
    var strip = byId('demo-line');
    if (!strip.dataset.placeholder) return;
    clear(strip).appendChild(h('span', { 'class': 'muted', 'data-testid': 'demo-check' }, failed ? 'Could not check whether this desk shows demo data.' : 'Checking whether this desk shows demo data…'));
  }
  function dropPlaceholder() {
    var strip = byId('demo-line');
    if (strip.dataset.placeholder) { delete strip.dataset.placeholder; clear(strip); strip.hidden = true; }
  }
  function pollDemo() {
    if (demoTools !== true) return; // not asked until the desk says the extension is loaded
    var ms = current === 'overview' || current === 'demo' ? 1000 : 5000;
    if (stopDemo && ms === demoMs) return;
    if (stopDemo) stopDemo();
    demoMs = ms;
    stopDemo = poll('demo', '/api/demo/state', ms, function (d, status) {
      if (status === 200) { demoKnown = true; state.demo = d; sourceChip(false); if (d && typeof d.demo_line === 'string' && byId('demo-line').hidden) registerDemo({ demoLine: d.demo_line }); loadDemoAssets(); emit('demo', d); }
      else if (status === 404) { demoKnown = true; state.demo = null; sourceChip(false); dropPlaceholder(); try { window.localStorage.removeItem('lead-desk-demo-line'); } catch (e) { /* storage blocked */ } }
      else { sourceChip(true); dropPlaceholder(); }
    }, function () { dropPlaceholder(); }); // a desk that does not answer is shown by the Offline pill, not by this chip
  }

  // ---- theme
  function effectiveTheme() {
    return root.getAttribute('data-theme') || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function paintTheme() {
    var dark = effectiveTheme() === 'dark';
    var btn = byId('theme-btn');
    btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
    clear(btn).appendChild(icon(dark ? 'sun' : 'moon'));
    btn.title = dark ? 'Switch to light theme' : 'Switch to dark theme';
  }
  function toggleTheme() {
    var next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { window.localStorage.setItem('lead-desk-theme', next); } catch (e) { /* storage blocked: choice lasts for this visit */ }
    paintTheme();
  }

  function boot() {
    byId('brand-mark').appendChild(icon('mark'));
    if (labels.brand.sub) byId('brand-sub').textContent = labels.brand.sub;
    byId('theme-btn').addEventListener('click', toggleTheme);
    paintTheme();
    new MutationObserver(paintTheme).observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', paintTheme);
    booted = true;
    renderNav();
    window.addEventListener('hashchange', route);
    route();
    restoreDemo();
    var strip = byId('demo-line');
    if (strip.hidden) { // first visit, nothing cached: reserve the line's space so the page does not jump when it arrives
      strip.dataset.placeholder = '1';
      strip.hidden = false;
      checkingLine(false);
    }
    poll('summary', '/api/ui/summary', 2000, onSummary, null);
    poll('review', '/api/ui/review', 2000, onReview, null);
  }

  window.Portal = {
    SVGNS: SVGNS, has: has, h: h, s: s, icon: icon, page: page, registerDemo: registerDemo, api: api, poll: poll, on: on,
    stale: stale, privacy: { hidden: hidePersonal, set: setHidePersonal, mask: mask }, shownRef: shownRef, reveal: { key: valueKey, clear: clearReveals, on: revealOn, toggle: toggleReveal, ms: REVEAL_MS }, go: go, router: { go: go, route: route }, labels: labels, state: state, words: words, needsCount: needsCount,
    stateTone: stateTone, ui: { guarded: guarded, name: shownName, refNode: refNode, duration: duration, afterFirst: afterFirst, mask: mask, followupChips: followupChips, waitingChip: waitingChip, shortRef: shortRef, requestRef: requestRef, leadRef: leadRef, card: card, kpiTile: kpiTile, pill: pill, table: table, drawer: drawer, confirm: confirmDialog, toast: toast, relTime: relTime, splitBar: splitBar, donut: donut, utc: utc, emptyState: emptyState, errorState: errorState, skeleton: skeleton, clear: clear }
  };
  document.addEventListener('DOMContentLoaded', boot);
})();
