/* Data and AI: what the AI can read, what the desk stores, how access is limited.
   Built from the fields the AI step is set up to receive and a fixed field list, served by /api/ui/data-map.
   Optional: the extension may offer Portal.demo.aiExtras(dataMap, state) -> Element|null (for example a live count of AI calls). */
(function () {
  'use strict';
  var P = window.Portal;
  var h = P.h;
  var LOOPBACK = { '127.0.0.1': 1, '::1': 1, localhost: 1 };

  function fieldName(code) { return (P.labels.field && P.labels.field[code]) || P.words(code); }
  function chip(text, never) {
    return h('li', { 'class': 'dt-chip' + (never ? ' dt-chip-never' : ''), 'data-testid': never ? 'ai-never-item' : 'ai-reads-item' }, (never ? P.icon('close') : h('span', { 'class': 'dot', 'aria-hidden': 'true' })), text, never ? h('span', { 'class': 'sr-only' }, ' (not given to the AI)') : null);
  }

  function aiCard(m, state) {
    var ai = m.ai_step || {};
    var live = ai.mode === 'live';
    var reads = h('div', { 'class': 'dt-group', 'data-testid': 'data-ai-reads' },
      h('div', { 'class': 'eyebrow' }, 'Given to the AI'),
      h('ul', { 'class': 'dt-chips' }, (ai.receives || []).map(function (k) { return chip(fieldName(k), false); })));
    var never = h('div', { 'class': 'dt-group', 'data-testid': 'data-ai-never' },
      h('div', { 'class': 'eyebrow' }, 'Not given to the AI'),
      h('ul', { 'class': 'dt-chips' }, (ai.never_receives || []).map(function (k) { return chip(fieldName(k), true); })));
    var modeLine = live ? 'A live AI service gives the rating.' : 'The rating comes from a list of pre-written ratings: no live AI is called.';
    var extraNode = null;
    var fn = P.demo && typeof P.demo.aiExtras === 'function' ? P.demo.aiExtras : null;
    if (fn) { try { extraNode = fn(m, state); } catch (e) { extraNode = null; } }
    return P.ui.card({
      title: 'What the AI is given',
      why: 'The rating step asks an AI to rate each new lead hot, warm or cold. Built from the fields the AI step is set up to receive and a fixed field list.',
      testid: 'data-ai',
      body: [h('div', { 'class': 'dt-two' }, reads, never), h('p', { 'class': 'muted dt-foot', 'data-testid': 'data-ai-sent-note' }, 'The email address is sent to the AI.'), h('p', { 'class': 'dt-mode', 'data-testid': 'data-ai-mode' }, P.icon('info'), h('span', null, modeLine)), extraNode]
    });
  }

  function payloadNote(m) {
    var has = (m.stored || []).some(function (r) { return r.field === 'request_payload'; });
    return has ? h('p', { 'class': 'note note-warn dt-payload', 'data-testid': 'data-payload-note' }, P.icon('info'), h('span', null, 'The desk keeps each request exactly as it arrived (\u201cOriginal request text\u201d), including phone, company and message, which the AI never sees.')) : null;
  }
  function storedCard(m) {
    var places = {};
    (m.stored || []).forEach(function (r) { places[r.where] = 1; });
    var onePlace = Object.keys(places).length <= 1;
    var cols = [
        { key: 'field', label: 'Field', render: function (r) { return h('span', { 'class': 'lead-name' }, fieldName(r.field)); } },
        { key: 'purpose', label: 'What it is for', render: function (r) { return P.words(r.purpose); } },
        { key: 'sent_to', label: 'Other systems that get it', render: function (r) { return r.sent_to && r.sent_to.length ? r.sent_to.join(', ') : h('span', { 'class': 'faint' }, 'Nowhere: stays in the desk’s records'); } }
    ];
    if (!onePlace) cols.splice(1, 0, { key: 'where', label: 'Kept in', render: function (r) { return P.words(r.where); } });
    var table = P.ui.table({
      testid: 'data-stored',
      columns: cols,
      rows: m.stored || [],
      empty: { heading: 'Nothing listed', sentence: 'The desk did not list any stored fields.' }
    });
    return P.ui.card({
      title: 'What the desk stores',
      why: 'All kept in the desk’s records on this computer.',
      testid: 'data-stored-card',
      body: [payloadNote(m), table, h('p', { 'class': 'muted dt-foot' }, '“Other systems that get it” names the other systems that receive a field. “Nowhere” means it stays in the desk’s records.')]
    });
  }

  // the Hide switch of the Leads page is a display setting of this browser, so the row follows that switch, not the desk's own masking setting
  function hideSwitchOn() {
    return P.privacy.hidden();
  }

  // plain marks: a line that is on gets a neutral mark and no colour; a line that is off is marked and listed first
  function accessRow(on, text, hint) {
    return { off: !on, node: h('li', { 'class': 'dt-acc' + (on ? '' : ' dt-acc-off'), title: hint || null },
      h('span', { 'class': 'dt-acc-ico' }, (on ? h('span', { 'class': 'dot', 'aria-hidden': 'true' }) : P.icon('close'))),
      h('span', { 'class': 'dt-acc-text' }, text),
      on ? h('span', { 'class': 'pill pill-neutral' }, 'On') : h('span', { 'class': 'pill pill-warn' }, 'Off')) };
  }
  function accessCard(m) {
    var a = m.access || {};
    var local = !!LOOPBACK[a.bind];
    var items = [
      accessRow(local, 'Can only be reached from this computer', 'Address: ' + a.bind),
      accessRow(!!a.portal_pages_read_only, 'Looking at a page changes nothing; only a button can', 'Buttons that change something, through the desk: ' + (a.portal_actions || []).join(', ')),
      accessRow(!!(a.host_check && a.origin_check), 'Checks where each request comes from'),
      accessRow(!!a.login, 'Sign-in required'),
      accessRow(!!a.encryption_at_rest, 'The stored data is encrypted on disk')
    ];
    items.sort(function (x, y) { return (y.off ? 1 : 0) - (x.off ? 1 : 0); }); // stable: the settings that are Off come first
    // the last row is not a setting of the desk but a display choice of this browser: same row, neutral chip either way, always last
    var hideOn = hideSwitchOn();
    var hideRow = h('li', { 'class': 'dt-acc', 'data-testid': 'data-hide-note', title: 'A display choice of this browser (switch on the Leads page). The stored data is not changed.' },
      h('span', { 'class': 'dt-acc-ico' }, hideOn ? h('span', { 'class': 'dot', 'aria-hidden': 'true' }) : P.icon('close')),
      h('span', { 'class': 'dt-acc-text' }, 'Hiding names and email addresses on screen (this also hides phone numbers, messages and request numbers): on by default in this browser; switch it off on the Leads page to see them (the stored data is not changed)'),
      h('span', { 'class': 'pill pill-neutral', 'data-testid': 'data-hide-chip' }, hideOn ? 'On' : 'Off'));
    return P.ui.card({
      title: 'Settings of this desk',
      why: 'Settings that are Off come first; the last row is a choice of this browser. This describes the settings, not a security guarantee.',
      testid: 'data-access',
      body: h('ul', { 'class': 'list dt-acc-list' }, items.map(function (x) { return x.node; }), hideRow)
    });
  }

  P.page('data', {
    title: 'Data and AI',
    purpose: 'What the AI is given, what the desk keeps, and which settings are on or off.',
    nav: { label: 'Data and AI', icon: 'data', order: 5 },
    mount: function (root) {
      var map = null, failure = null, alive = true, sig = '';
      function render(force) {
        if (!alive) return;
        var s = JSON.stringify([map, failure && failure.status, !!(P.demo && P.demo.aiExtras)]);
        if (!force && s === sig && root.firstChild) return;
        sig = s;
        P.ui.clear(root);
        if (!map) {
          root.appendChild(failure ? P.ui.errorState({ what: 'the data map', status: failure.status, retry: function () { failure = null; render(true); } })
            : h('div', { 'class': 'stack' }, P.ui.skeleton(5), P.ui.skeleton(5)));
          return;
        }
        root.appendChild(h('div', { 'class': 'dt' }, aiCard(map, P.state.demo), h('div', { 'class': 'dt-cols' }, accessCard(map), storedCard(map))));
      }
      var stop = P.poll('data-map', '/api/ui/data-map', 15000, function (d, st) {
        if (st === 200 && d && d.ai_step) { map = d; failure = null; } else failure = { status: st };
        render(false);
      }, function () { if (!map) { failure = { status: 0 }; render(false); } });
      var off = P.on('demo', function () { render(true); }), offPersonal = P.on('personal', function () { render(true); });
      render(true);
      this.unmount = function () { alive = false; stop(); off(); offPersonal(); };
    }
  });
})();
