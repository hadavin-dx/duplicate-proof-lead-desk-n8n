/* Needs a person: items the desk will not decide alone. Tabs, action cards, confirm dialogs for every resolve decision. */
(function () {
  'use strict';
  var P = window.Portal;
  var h = P.h;
  var has = P.has;

  function ext(name) {
    var d = P.demo;
    return d && typeof d[name] === 'function' ? d[name] : null;
  }

  var TABS = [
    { id: 'action', label: 'Needs a person', lead: 'These need a person. A card with a button can be settled here. A card without a button says what to do outside the desk.', empty: 'Nothing needs a person right now.', sentence: 'When the desk meets something it will not decide alone, it appears here.' },
    { id: 'waiting', label: 'Waiting', lead: 'Nobody has to act yet. The desk retries these by itself; if one stays, check that the desk and the mail step run.', empty: 'Nothing is waiting.', sentence: '“Waiting” means the desk has the item and is still working on it, for longer than it expects. Nobody has to act yet.' },
    { id: 'info', label: 'For information', lead: 'Information only: nobody has to do anything here. “Repeat changed” means the same request came again with other details; the first request stands. There is one card per lead: every note about the same lead sits on that card.', empty: 'Nothing to read.', sentence: 'Repeat requests that carried new details appear here. Nobody has to do anything.' }
  ];
  // what each badge means, in plain words; the Needs a person tab lists only the ones that appear on the page
  var DEFINE = [
    ['failed', '“Failed” means the email was not sent.'],
    ['did_not_finish', '“Did not finish” means the desk tried the follow-up and stopped. The email may already have been sent.'],
    ['unknown', '“Outcome unknown” means the desk cannot tell whether the email was sent.'],
    ['crm_unconfirmed', '“CRM not confirmed” means the desk cannot tell whether the CRM holds the lead. The follow-up is waiting for the CRM check, so no email was sent.'],
    ['review', '“Request to check” means a request with missing details, or one the desk could not store.'],
    ['reminder_to_check', '“Reminder to check” means the desk could not confirm a reminder email, or the mail step refused it. The desk never sends it again.']
  ];
  function definitions(items) {
    var seen = {};
    items.forEach(function (i) { seen[i.state_view || i.kind] = true; });
    var rows = DEFINE.filter(function (d) { return has(seen, d[0]); });
    return rows.length ? h('ul', { 'class': 'needs-define', 'data-testid': 'needs-define' }, rows.map(function (d) { return h('li', null, d[1]); })) : null;
  }
  var KIND_TONE = { reminder_to_check: 'hot', failed: 'bad', unknown: 'hot', review: 'warn', stalled: 'pending', duplicate_with_new_info: 'info', replay_payload_differs: 'info' };

  // the words under a card that has no button: by kind, or by kind:reason when the reason changes what a person can do
  function nextKey(it) { var k = it.kind + ':' + it.reason; return (P.labels.next || {})[k] ? k : it.kind; }

  function kindTag(it) {
    var kind = it.kind, view = it.state_view || kind;
    if (kind === 'failed' || kind === 'unknown') return P.ui.pill(view, { title: view === 'failed' ? 'The follow-up failed and the desk is sure the email was not sent.' : null });
    if (kind === 'stalled') return h('span', { 'class': 'pill pill-stalled' }, P.icon('clock'), P.labels.kind[kind]); // a dashed amber outline with a clock: never read as the filled blue Sending pill
    return h('span', { 'class': 'pill pill-' + (KIND_TONE[kind] || 'neutral'), title: (P.labels.kindHelp || {})[kind] || null }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), P.labels.kind[kind] || P.words(kind));
  }

  // information notes about one lead become one card (a repeat and a replay of the same lead are one story)
  function mergeInfo(items) {
    var out = [], by = {};
    items.forEach(function (i) {
      var k = i.lead_id || i.item_id;
      if (!has(by, k)) { by[k] = Object.assign({}, i, { notes: [] }); out.push(by[k]); }
      var m = by[k];
      m.notes.push(titleOf(i));
      if (i.since < m.since) m.since = i.since;
    });
    // one story, one heading: "Repeat changed: company" (the changed fields of every repeat), plus one plain line when the same request number also came again with other details
    out.forEach(function (m) {
      var fields = [], replay = false;
      m.notes.forEach(function (n) {
        var f = /new details:[ ]*(.+)$/.exec(n);
        if (f) { f[1].split(/,\s*/).forEach(function (x) { if (fields.indexOf(x) < 0) fields.push(x); }); } else replay = true;
      });
      if (m.notes.length > 1 || fields.length) m.merged_title = 'Repeat changed: ' + (fields.length ? fields.join(', ') : 'details');
      m.notes = replay && fields.length ? ['The same request number was also sent again with other details.'] : [];
      m.kind = 'duplicate_with_new_info';
    });
    return out;
  }

  function secondsWord(t) { return t.replace(/(\d+) s\b/g, '$1 seconds'); } // the desk writes "8 s"; the page says seconds
  var RANK = { failed: 0, unknown: 1, reminder_to_check: 2, review: 2, stalled: 3 };
  function lbl(group, it) { var g = P.labels[group] || {}; var r = String(it.reason || ''); return g[it.kind + ':' + r] || g[it.kind + ':' + r.split(':')[0]] || null; }
  function titleOf(it) {
    var g = P.labels.title || {};
    var t = g[it.kind + ':' + it.reason] || g[it.kind] || it.title || '';
    t = t.replace(/^Incomplete request[:]\s*/i, ''); // the chip already says it
    return secondsWord(t).replace(/^./, function (c) { return c.toUpperCase(); });
  }
  function reasonOf(it) {
    var t = lbl('reason', it) || it.reason_text || '';
    if (it.kind === 'unknown' && !lbl('reason', it) && /cannot tell whether the email was sent/.test(t) && P.labels.reason.unknown) t = P.labels.reason.unknown;
    return t.replace(/Send a corrected submission with a NEW request number\./, 'The request cannot be used as it arrived.').replace(/send again blind\./, 'send again until a person checks.').replace(/\bNEW\b/g, 'new');
  }

  function explainer() {
    return h('details', { 'class': 'note explainer', 'data-testid': 'unknown-explainer' },
      h('summary', null, P.icon('help'), 'Why can the desk not tell whether the email was sent?'),
      h('p', null, 'The desk writes down that it is about to act, then acts, then writes down the result. If it stopped between the last two, it cannot tell whether the email or the CRM entry went through.'),
      h('p', null, 'It will not guess and it will not send again blind. Check the real mail step (the part that sends the email) or the CRM, then tell the desk what you found. Until you do, the desk will not send again.'));
  }

  function techDetails(it) {
    var kind = (P.labels.kind || {})[it.kind], sourceName = (P.labels.source || {})[it.source];
    var rows = [['Item number', it.item_id], ['Type of item', kind || (it.kind ? P.words(it.kind) : null)], ['Reason given by the desk', it.reason ? P.words(String(it.reason).replace(/[:,]/g, ', ')) : null], ['Lead number', it.lead_id], ['Source', sourceName || (it.source ? P.words(it.source) : null)]];
    if (it.hint && it.hint !== it.reason_text) rows.push(['Desk note', it.hint]);
    var dl = h('dl', { 'class': 'kv' });
    rows.forEach(function (r) { if (r[1]) { dl.appendChild(h('dt', null, r[0])); dl.appendChild(h('dd', null, String(r[1]))); } });
    return h('details', { 'class': 'details' }, h('summary', null, 'Technical details (record numbers and raw values)'), dl);
  }

  function failureWords(r) {
    var d = r.data || {};
    if (d.hint) return d.hint;
    if (d.error) return P.labels.error[d.error] || P.words(d.error);
    return 'The desk answered ' + r.status + '.';
  }

  function run(a, btn, refresh, who) {
    var cf = a.confirm ? Object.assign({}, a.confirm, { tone: a.id === 'resolve_not_sent' ? 'warn' : null, body: (who ? 'Lead: ' + P.privacy.mask('name', who) + '. ' : '') + ((P.labels.confirmBody || {})[a.id] || a.confirm.body), note: (P.labels.confirmNote || {})[a.id] || null }) : null;
    var go = cf ? P.ui.confirm(cf) : Promise.resolve(true);
    go.then(function (ok) {
      if (!ok) return;
      if (P.stale()) { P.ui.toast('Actions are paused until the desk answers.', 'warn'); return; } // the desk went away while the question was open: nothing is sent
      btn.disabled = true;
      return P.api(a.endpoint, { method: 'POST', body: a.body }).then(function (r) {
        if (r.status === 200) {
          var st = r.data && r.data.state;
          P.ui.toast(a.id === 'resolve_seen' ? 'Done: marked as seen.' : 'Done' + (st ? ': the follow-up is now ' + (P.labels.state[st] || st).toLowerCase() : '') + '.', 'ok');
        } else {
          P.ui.toast(r.status === 409 && r.data && r.data.error === 'unknown_needs_person' ? 'Retry refused: the email may already have been sent. Use \u201cI checked\u201d.' : 'The desk refused: ' + failureWords(r), 'warn');
        }
        refresh();
      }, function () {
        P.ui.toast('Desk stopped. The page will reconnect.', 'warn');
      }).then(function () { btn.disabled = P.stale(); });
    });
  }

  function card(it, now, refresh) {
    // the "I checked" decisions come first and lead; Retry is last and outlined; an unknown item has no Retry button (the desk would refuse it)
    var acts = it.actions.filter(function (a) { return !(a.id === 'retry' && it.kind === 'unknown'); }).sort(function (x, y) { return (x.id === 'retry' ? 1 : 0) - (y.id === 'retry' ? 1 : 0); });
    var buttons = acts.map(function (a) {
      var cls = a.id === 'retry' && (acts.length > 1 || it.kind === 'failed') ? 'btn-outline' : a.id === 'retry' ? 'btn-primary' : a.id === 'resolve_not_sent' || a.id === 'resolve_sent' || a.id === 'resolve_crm_present' ? 'btn-outline' : 'btn-primary';
      var b = h('button', { 'class': 'btn ' + cls, type: 'button', 'data-testid': 'action-' + a.id.replace(/_/g, '-'), 'data-action': a.id, disabled: P.stale(), title: P.stale() ? 'Paused until the desk answers' : null }, (P.labels.action || {})[a.id] || a.label);
      b.addEventListener('click', function () { run(a, b, refresh, it.lead_name); });
      return b;
    });
    var isInfo = it.tab === 'info';
    var why = isInfo ? '' : reasonOf(it);
    var heading = it.merged_title || titleOf(it);
    // a Retry that the card itself says to check first: the plain next step stands beside the button
    var checkFirst = it.kind === 'failed' && acts.some(function (a) { return a.id === 'retry'; }) ? h('span', { 'class': 'need-check', 'data-testid': 'need-check-first' }, P.icon('info'), 'Check the mail step first') : null;
    var pausedCue = P.stale() && acts.length ? h('span', { 'class': 'need-check need-paused-cue', 'data-testid': 'need-paused-cue' }, 'Paused until the desk answers') : null;
    var extraFn = ext('needsCardExtras');
    var extra = null;
    if (extraFn) { try { extra = extraFn(it); } catch (e) { extra = null; } }
    return h('article', { 'class': 'card need', 'data-testid': 'needs-card', 'data-item-id': it.item_id, 'data-kind': it.kind, 'data-view': it.state_view || it.kind },
      h('div', { 'class': 'need-main' },
        h('div', { 'class': 'need-meta' },
          it.lead_name ? h('span', { 'class': 'need-who' }, P.icon('person'), P.ui.name(it.lead_name)) : h('span', { 'class': 'faint' }, 'No lead linked'),
          it.lead_id ? h('span', { 'class': 'faint mono', 'data-testid': 'need-lead-number', title: 'The first six characters of the full lead number ' + it.lead_id }, P.ui.leadRef(it.lead_id)) : null, // tells two masked cards apart
          h('span', { 'class': 'faint' }, 'Source: ' + (P.labels.source[it.source] || it.source)),
          h('span', { 'class': 'need-since' }, P.icon('clock'), 'First seen ', P.ui.relTime(it.since, now))),
        h('div', { 'class': 'need-head' }, kindTag(it), h('h2', null, heading)),
        it.merged_title && it.notes.length ? h('ul', { 'class': 'need-notes' }, it.notes.map(function (n) { return h('li', null, n); })) : null,
        why && why !== titleOf(it) && why.indexOf(titleOf(it)) !== 0 ? h('p', { 'class': 'need-why' }, why) : null,
        !acts.length && (P.labels.next || {})[nextKey(it)] ? h('p', { 'class': 'need-next', 'data-testid': 'need-next' }, P.icon('info'), P.labels.next[nextKey(it)]) : null,
        techDetails(it)),
      buttons.length || extra ? h('div', { 'class': 'need-actions' }, buttons, pausedCue || checkFirst, extra) : null);
  }

  P.page('needs', {
    title: 'Needs a person',
    purpose: 'Items the desk would not settle alone, sorted by what you need to do about them.',
    nav: { label: 'Needs a person', icon: 'person', order: 3 },
    mount: function (root) {
      var tab = 'action', last = null, lastSig = '', stop = null;
      var tabBar = h('div', { 'class': 'tabs', role: 'tablist', 'aria-label': 'Kinds of items', title: 'The number in the sidebar counts the Needs a person tab only' });
      var list = h('div', { 'class': 'needs-list', role: 'tabpanel' }, P.ui.skeleton(3, 'Loading…', true));
      TABS.forEach(function (t) { tabBar.appendChild(h('span', { 'class': 'tab tab-loading', 'aria-hidden': 'true' }, t.label)); }); // the tab names, without counts, until the first answer
      root.appendChild(tabBar);

      root.appendChild(list);

      function draw(d) {
        var counts = Object.assign({}, d.tabs || {});
        var infoAll = d.items.filter(function (i) { return i.tab === 'info'; });
        counts.info = mergeInfo(infoAll).length;
        P.ui.clear(tabBar);
        TABS.forEach(function (t) {
          var b = h('button', { 'class': 'tab', type: 'button', role: 'tab', 'aria-selected': t.id === tab ? 'true' : 'false', 'data-testid': 'needs-tab-' + t.id }, t.label, h('span', { 'class': 'tab-count', 'data-testid': 'needs-tab-count-' + t.id }, String(counts[t.id] || 0)));
          b.addEventListener('click', function () { tab = t.id; lastSig = ''; draw(last); });
          tabBar.appendChild(b);
        });
        // ONE cue for the tab counts, the cards and the sidebar number: they are the last update, not a live count
        if (P.stale()) tabBar.appendChild(h('span', { 'class': 'tabs-asof faint', 'data-testid': 'needs-asof' }, 'as of the last update'));
        var items = d.items.filter(function (i) { return i.tab === tab; }).map(function (i, n) { return { it: i, n: n }; }).sort(function (a, b) {
          var ra = has(RANK, a.it.kind) ? RANK[a.it.kind] : 9, rb = has(RANK, b.it.kind) ? RANK[b.it.kind] : 9;
          return ra - rb || a.n - b.n;
        }).map(function (x) { return x.it; });
        if (tab === 'info') items = mergeInfo(items);
        P.ui.clear(list);
        list.classList.toggle('needs-one', items.length === 1);
        var t = TABS.filter(function (x) { return x.id === tab; })[0];
        if (items.length) list.appendChild(h('div', { 'class': 'needs-lead muted', 'data-testid': 'needs-lead' }, h('p', null, t.lead), tab === 'action' ? definitions(items) : null));
        if (P.stale() && items.some(function (i) { return i.actions.length; })) list.appendChild(h('p', { 'class': 'note note-warn needs-paused', role: 'status', 'data-testid': 'needs-paused' }, P.icon('alert'), h('span', null, 'Actions are paused until the desk answers.')));
        if (tab === 'action' && items.some(function (i) { return i.kind === 'unknown'; })) list.appendChild(explainer());
        if (!items.length) { list.appendChild(P.ui.emptyState({ heading: t.empty, sentence: t.sentence })); return; }
        items.forEach(function (it) { list.appendChild(card(it, d.now, refresh)); });
        var cards = list.querySelectorAll(':scope > .need');
        if (cards.length > 1 && cards.length % 2 === 1) cards[cards.length - 1].classList.add('need-alone'); // an odd last card takes the whole row, no hole beside it
      }
      function show(d, status) {
        if (status !== 200 || !d) {
          lastSig = '';
          if (!last) P.ui.clear(tabBar); // no tab names above an error
          P.ui.clear(list).appendChild(P.ui.errorState({ what: 'the list', status: status, retry: refresh }));
          return;
        }
        var sig = tab + JSON.stringify(d.items) + JSON.stringify(d.tabs);
        if (sig === lastSig) return;
        last = d;
        lastSig = sig;
        draw(d);
      }
      function refresh() { P.api('/api/ui/review').then(function (r) { show(r.data, r.status); }, function () {}); }
      stop = P.poll('review', '/api/ui/review', 2000, show, function () {
        if (!last) { P.ui.clear(tabBar); P.ui.clear(list).appendChild(P.ui.errorState({ what: 'the list', retry: refresh })); }
      });
      var offLink = P.on('link', function () { if (last) { lastSig = ''; draw(last); } }); // actions pause when the desk stops answering and wake when it answers again
      var offMask = [P.on('personal', function () { if (last) { lastSig = ''; draw(last); } }), P.on('reveal', function () { if (last) { lastSig = ''; draw(last); } })]; // lead names follow the switch and their eye buttons
      this.unmount = function () { stop(); offLink(); offMask.forEach(function (f) { f(); }); };
    }
  });
})();
