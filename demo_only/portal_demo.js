/* Demo extension for the Lead Desk portal. Loaded only when the desk answers /api/demo/state.
   It adds the demo line, extra tiles, a connections strip, a cross-check line and the Demo controls page.
   Every name, system and reply here is made up. Deleting this folder leaves the portal working. */
(function () {
  'use strict';
  var P = window.Portal;
  var h = P.h;
  var LINE = 'Demo: Every lead is demo data. The demo CRM (customer records) and the demo mail are stand-ins. AI ratings are pre-written. Nothing here is sent to real customers or real systems.';
  var RESET_COMMAND = 'powershell -NoProfile -File scripts\\reset.ps1';
  var RESET_COMMAND_POSIX = 'scripts/reset.sh';
  var POINT_WORDS = {
    A: 'Stop after the request is saved but before it is decided',
    A2: 'Stop after the decision is saved',
    B: 'Stop just before the answer goes back',
    D1: 'Stop just before the email is sent',
    D2: 'Stop just after the email is sent, before it is noted'
  };
  var restarted = false; // a desk restart was seen while this page was open
  var lastSend = null; // {person, door, prefix, name}

  function dm() { return P.state.demo; }
  function fakes() { var d = dm(); return d && d.fakes && d.fakes.reachable ? d.fakes : null; }
  // a demo person in a result line: always the neutral label (the line stays on screen after the names are hidden again)
  function personName(n) { return 'Lead ' + n; }
  function mode(m) { return m === 'keyed' ? 'Spots repeat emails' : m === 'blind' ? 'Does not spot repeat emails' : '–'; }

  // ---- hooks used by the product pages
  function overviewTiles(summary, d) {
    var f = fakes();
    return [
      { testid: 'kpi-outbox', label: 'Emails in demo mail', value: f ? f.outbox.count : null, caption: f ? (f.outbox_mode === 'keyed' ? 'The demo mail notices repeats.' : 'The demo mail does not notice repeats.') : 'The demo mail is not reachable', tone: f && f.outbox_mode === 'keyed' ? 'ok' : 'neutral' },
      { testid: 'kpi-ai-calls', label: 'AI ratings given', value: d.ai_calls === undefined ? null : d.ai_calls, caption: 'AI ratings the desk has given, kept across restarts.', tone: 'neutral' }
    ];
  }

  function figure(label, value, testid) {
    return h('div', { 'class': 'dm-figure' }, h('div', { 'class': 'dm-figure-value num', 'data-testid': testid }, value === null ? '–' : String(value)), h('div', { 'class': 'dm-figure-label' }, label));
  }
  function overviewProof(summary, d) {
    var f = fakes();
    var sent = summary.followups.sent;
    var head = h('div', { 'class': 'eyebrow' }, 'Check against the demo mail');
    if (!f) {
      return h('div', { 'class': 'dm-proof' }, head, h('p', { 'class': 'note note-warn' }, P.icon('alert'), h('span', null, 'The demo mail cannot be reached, so the two numbers cannot be compared.')));
    }
    var m = typeof f.outbox.followups === 'number' ? f.outbox.followups : f.outbox.count; // follow-ups only: a reminder email is not one
    var r = f.outbox.reminders;
    var same = sent === m;
    var verdict = P.stale() ? h('span', { 'class': 'faint' }, 'Last known values')
      : same
      ? (sent === 0 && m === 0 ? h('span', { 'class': 'faint' }, 'Nothing to compare yet') : h('span', { 'class': 'pill pill-ok' }, P.icon('check'), 'Numbers match'))
      : h('p', { 'class': 'note note-warn', 'data-testid': 'proof-mismatch' }, P.icon('alert'), h('span', null, 'The two numbers differ: the desk marks ' + sent + ' follow-up' + (sent === 1 ? '' : 's') + ' as sent, the demo mail holds ' + m + ' follow-up email' + (m === 1 ? '' : 's') + '. Both are shown as they are.' + (summary.followups.unknown > 0 && m > sent ? ' The follow-up with an unknown outcome may be the extra email: look at it under Needs a person.' : '')));
    return h('div', { 'class': 'dm-proof', 'data-testid': 'proof' }, head,
      h('div', { 'class': 'dm-proof-row' }, figure('Follow-ups sent (desk record)', sent, 'proof-desk'), h('span', { 'class': 'dm-vs' }, sent === 0 && m === 0 ? '' : same ? 'matches' : 'differs from'), figure('Follow-up emails in demo mail', m, 'proof-outbox')),
      verdict,
      typeof r === 'number' && r > 0 ? h('p', { 'class': 'muted', 'data-testid': 'proof-reminders' }, 'Reminder emails in the demo mail: ' + r + '. They are not follow-ups, so they are not compared.') : null);
  }

  function cell(label, main, sub, tone) {
    return h('div', { 'class': 'dm-cell' }, h('div', { 'class': 'eyebrow' }, label), h('div', { 'class': 'dm-cell-main' }, h('span', { 'class': 'dm-dot dm-dot-' + (tone || 'neutral'), 'aria-hidden': 'true' }), main), h('div', { 'class': 'dm-cell-sub' }, sub));
  }
  function aiExtras(dataMap, state) {
    var n = state && state.ai_calls;
    if (typeof n !== 'number') return null;
    return h('div', { 'class': 'dm-ai' }, h('div', { 'class': 'dm-figure dm-figure-row', 'data-testid': 'data-ai-calls' }, h('div', { 'class': 'dm-figure-value num' }, String(n)), h('div', { 'class': 'dm-figure-label' }, 'AI ratings given (kept across restarts)')),
      h('p', { 'class': 'muted dt-foot', 'data-testid': 'data-ai-keyed' }, 'Ratings are looked up by the lead’s lower-case email address: the same address always gets the same rating, and an address not on the list gets Warm.'));
  }
  // the demo CRM may hold more entries than this desk has leads (another desk can write to the same one): say so, do not call it a repeat
  function extraRows(blind, count, summary) { var n = summary && summary.counts ? summary.counts['new'] : null; return !blind && typeof n === 'number' && count > n ? count - n : 0; }
  function crmNote(blind, count, summary) {
    var x = extraRows(blind, count, summary);
    if (x) return 'Checks for repeats. It holds ' + x + ' more than this desk has leads: another desk may write to the same demo CRM.';
    return blind ? 'Does not spot repeats: it counts every entry it receives, even a second one for the same lead.' : 'Checks for repeats: one entry per lead.';
  }
  function crmTone(blind, count, summary) { return extraRows(blind, count, summary) ? 'warn' : 'ok'; }
  function overviewExtras(summary, d) {
    var f = fakes();
    var blindCrm = f && f.crm_mode === 'blind';
    var crash = d.crash || {};
    var cells = [];
    if (f) {
      cells.push(cell('Demo mail', h('span', { 'data-testid': 'conn-outbox-mode' }, mode(f.outbox_mode)),
        f.outbox_mode === 'keyed' ? 'The demo mail spots a repeat of an email it already holds and keeps one.' : 'The demo mail cannot tell it already holds an email, so a repeat is stored again.', f.outbox_mode === 'keyed' ? 'ok' : f.outbox.count > 0 ? 'warn' : 'neutral'));
      cells.push(cell('Demo CRM', h('span', null, h('b', { 'class': 'num', 'data-testid': 'conn-crm-count' }, String(f.crm.count)), ' ' + (f.crm.count === 1 ? 'CRM entry' : 'CRM entries') + (blindCrm ? ' received' : '')),
        crmNote(blindCrm, f.crm.count, summary), crmTone(blindCrm, f.crm.count, summary)));
    } else {
      cells.push(cell('Demo mail', h('span', { 'data-testid': 'conn-outbox-mode' }, 'Not reachable'), 'The demo mail does not answer.', 'bad'));
      cells.push(cell('Demo CRM', h('span', null, h('b', { 'class': 'num', 'data-testid': 'conn-crm-count' }, '–'), ' CRM count'), 'The demo CRM does not answer.', 'bad'));
    }
    var offline = P.stale(); // offline or answering with a problem: the values are the last known ones
    cells.push(cell('Desk', offline ? 'Last known: running' : 'Running', (restarted ? 'Restarted ' : 'Started ') + P.ui.utc(d.desk_started_at), offline ? 'neutral' : 'ok'));
    cells.push(cell('Stop test', crash.available ? (crash.armed ? 'Ready to stop the desk' : 'Off') : 'Not in use', crash.available ? 'Can stop the desk once, on purpose, to test recovery.' : 'Optional. Start the desk with the option that lets it be stopped once on purpose (the stop test) to use it.', crash.armed ? 'warn' : crash.available ? 'neutral' : 'off'));
    return h('section', { 'class': 'dm-strip', 'data-testid': 'connections-strip', 'aria-label': 'Connections' }, cells);
  }

  // the Needs cards stay plain: the double-Retry test lives on the Demo controls page (see retryCard)
  function needsCardExtras() { return null; }

  function leadExtras(detail, d) {
    var f = d && d.fakes && d.fakes.reachable ? d.fakes : null;
    if (!detail || !detail.lead) return null;
    if (!f) return h('div', { 'class': 'dm-lead' }, h('div', { 'class': 'eyebrow' }, 'In the demo mail and the demo CRM'), h('p', { 'class': 'muted' }, 'The demo mail and demo CRM cannot be reached.'));
    var email = String(detail.lead.email || '').toLowerCase();
    var mails = f.outbox.messages.filter(function (m) { return String(m.to).toLowerCase() === email; }).length;
    var note = null;
    var kv = h('dl', { 'class': 'kv' }, h('dt', null, 'Emails to this address'), h('dd', { 'class': 'num', 'data-testid': 'lead-outbox-count' }, String(mails)));
    if (f.crm_mode === 'keyed') {
      var rows = f.crm.rows.filter(function (r) { return r.lead_id === detail.lead.lead_id; }).length;
      kv.appendChild(h('dt', null, 'CRM entries'));
      kv.appendChild(h('dd', { 'class': 'num', 'data-testid': 'lead-crm-count' }, String(rows) + ' for this lead'));
    } else {
      kv.appendChild(h('dt', { title: 'This CRM does not check for repeats, so it cannot say which entries belong to which lead' }, 'CRM entries'));
      kv.appendChild(h('dd', { 'class': 'num', 'data-testid': 'lead-crm-count' }, f.crm.count + ' in the whole demo CRM'));
      note = h('p', { 'class': 'muted dt-foot' }, 'This demo CRM does not record which lead an entry belongs to, so it can only give a total.');
    }
    var flag = detail.followup && detail.followup.state === 'sent' && mails !== 1
      ? h('p', { 'class': 'note note-warn', 'data-testid': 'proof-mismatch' }, P.icon('alert'), h('span', null, 'The desk says one follow-up was sent, but the demo mail holds ' + mails + ' email' + (mails === 1 ? '' : 's') + ' to this address.')) : null;
    return h('div', { 'class': 'dm-lead' }, h('div', { 'class': 'eyebrow' }, 'In the demo mail and the demo CRM'), h('p', { 'class': 'muted dt-foot' }, 'The demo mail is the mail step here. To check whether an email was sent, look at “Emails to this address” first.'), kv, note, flag);
  }

  // ---- Demo controls page
  function describe(r, label) {
    var answers = (r.data && r.data.answers) || [];
    var n = { 'new': 0, duplicate: 0, replayed: 0, refused: 0, noanswer: 0 };
    answers.forEach(function (a) {
      if (typeof a.status !== 'number') n.noanswer += 1; // 'timeout' or 'error': the request never got an HTTP answer
      else if (a.status >= 400) n.refused += 1;
      else if (a.replayed) n.replayed += 1;
      else if (a.verdict === 'new') n['new'] += 1;
      else if (a.verdict === 'duplicate') n.duplicate += 1;
    });
    var pills = h('div', { 'class': 'row' });
    if (n['new']) pills.appendChild(h('span', { 'class': 'pill pill-ok' }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), n['new'] + ' new'));
    if (n.duplicate) pills.appendChild(h('span', { 'class': 'pill pill-neutral' }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), n.duplicate + (n.duplicate === 1 ? ' repeat' : ' repeats')));
    if (n.replayed) pills.appendChild(h('span', { 'class': 'pill pill-info' }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), n.replayed + ' already known: nothing changed'));
    if (n.refused) pills.appendChild(h('span', { 'class': 'pill pill-bad' }, P.icon('alert'), n.refused + ' refused'));
    if (n.noanswer) pills.appendChild(h('span', { 'class': 'pill pill-bad' }, P.icon('alert'), n.noanswer + ' without an answer'));
    var list = h('ul', { 'class': 'dm-answers mono' }, answers.map(function (a) { return h('li', null, P.shownRef(a.ref) + '  ' + a.status + '  ' + (a.replayed ? 'replayed ' : '') + (a.verdict || '–')); }));
    return h('div', { 'class': 'stack' }, h('div', { 'class': 'dm-result-title' }, label), pills,
      answers.length ? h('details', { 'class': 'details' }, h('summary', null, 'Each answer'), list) : null);
  }

  var panel = {
    title: 'Demo controls',
    purpose: 'Send demo leads to the desk, stop it on purpose and watch what it does.',
    mount: function (root) {
      var stops = [];
      var d0 = dm() || {};
      var people = d0.people && d0.people.length ? d0.people : [1, 2, 3, 4, 5, 6].map(function (i) { return { person: i, name: 'Lead ' + i, company: null }; });
      function personText(p) { return P.privacy.hidden() ? 'Lead ' + p.person : p.name + (p.company ? ' (' + p.company + ')' : ''); }
      var personSel = h('select', { 'class': 'select', id: 'dm-person', 'data-testid': 'demo-person' }, people.map(function (p) { return h('option', { value: String(p.person) }, personText(p)); }));
      var doorSel = h('select', { 'class': 'select', id: 'dm-door', 'data-testid': 'demo-door' }, h('option', { value: 'form' }, P.labels.source.form), h('option', { value: 'webhook' }, P.labels.source.webhook));
      var result = h('div', { 'class': 'dm-result', role: 'status', 'data-testid': 'demo-burst-result', hidden: true });
      var bSend = h('button', { 'class': 'btn btn-primary', type: 'button', 'data-testid': 'demo-send' }, P.icon('send'), 'Send one request');
      var bAgain = h('button', { 'class': 'btn', type: 'button', 'data-testid': 'demo-send-again' }, 'Send the same request again');
      var bBurst = h('button', { 'class': 'btn', type: 'button', 'data-testid': 'demo-burst' }, P.icon('bolt'), 'Send 10 at once');
      var busy = false;

      function setBusy(on) { busy = on; [bSend, bAgain, bBurst].forEach(function (b) { b.disabled = on; }); }
      function fire(body, label, remember) {
        if (busy) return;
        setBusy(true);
        P.api('/api/demo/burst', { method: 'POST', body: body }).then(function (r) {
          if (r.status !== 200) { P.ui.toast((r.data && (r.data.hint || r.data.error)) || 'The desk did not accept this request.', 'warn'); return; }
          if (remember) lastSend = { person: body.person, door: body.door, prefix: r.data.ref_prefix };
          result.hidden = false;
          P.ui.clear(result).appendChild(describe(r, label));
        }, function () { P.ui.toast('Desk stopped. The page will reconnect.', 'warn'); }).then(function () { setBusy(false); });
      }
      function pick() { return { person: parseInt(personSel.value, 10), door: doorSel.value }; }
      bSend.addEventListener('click', function () { var p = pick(); fire({ n: 1, person: p.person, door: p.door }, 'Sent one request for ' + personName(p.person) + ' through the ' + P.labels.source[p.door].toLowerCase(), true); });
      bAgain.addEventListener('click', function () {
        if (!lastSend) { P.ui.toast('Send one request first, then send the same one again.', 'warn'); return; }
        fire({ n: 1, person: lastSend.person, door: lastSend.door, ref_prefix: lastSend.prefix }, 'Sent the same request again for ' + personName(lastSend.person), false);
      });
      bBurst.addEventListener('click', function () { var p = pick(); fire({ n: 10, person: p.person, door: p.door }, 'Sent 10 requests at once for ' + personName(p.person) + ', each with its own reference', true); });

      var againNote = h('p', { 'class': 'muted' }, 'The very same request again, after one has been sent here. The desk should say it already knows it and change nothing.');
      function action(button, text) { return h('div', { 'class': 'dm-action' }, button, h('p', { 'class': 'muted' }, text)); }
      var sendCard = P.ui.card({
        title: 'Send a lead', why: 'Demo people, sent straight to the desk.', testid: 'demo-send-card',
        body: [
          h('div', { 'class': 'dm-form' }, h('label', { 'class': 'field', 'for': 'dm-person' }, 'Lead', personSel), h('label', { 'class': 'field', 'for': 'dm-door' }, 'Source', doorSel)),
          h('div', { 'class': 'dm-actions' },
            action(bSend, 'One request for the lead above, through the chosen source.'),
            action(bAgain, againNote),
            action(bBurst, 'Ten requests at the same moment for the same lead, each with its own request number. They go straight to the desk.')),
          result
        ]
      });

      // crash switch
      var available = !!(d0.crash && d0.crash.available);
      var pts = (d0.crash && d0.crash.points) || [];
      var crashSel = h('select', { 'class': 'select', id: 'dm-crash', 'data-testid': 'demo-crash-point', disabled: !available }, pts.map(function (p) { return h('option', { value: p }, POINT_WORDS[p] || 'Stop at point ' + p); }));
      var armBtn = h('button', { 'class': 'btn', type: 'button', 'data-testid': 'demo-crash-arm', disabled: !available }, 'Arm the stop test');
      var offBtn = h('button', { 'class': 'btn btn-sm', type: 'button', hidden: true }, 'Disarm');
      var armed = h('p', { 'class': 'dm-armed', 'data-testid': 'demo-crash-armed' });
      function paintArmed(p) {
        P.ui.clear(armed);
        if (!available) { armed.appendChild(document.createTextNode('Not available: the desk was started without the option that lets it be stopped on purpose (the stop test).')); offBtn.hidden = true; return; }
        offBtn.hidden = !p;
        armed.appendChild(p ? h('span', { 'class': 'pill pill-warn' }, P.icon('alert'), 'Stop is set') : h('span', { 'class': 'pill pill-neutral' }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), 'Not set'));
        if (p) armed.appendChild(h('span', { 'class': 'muted' }, (POINT_WORDS[p] || '') + '. The next request that reaches this moment stops the desk.'));
      }
      function setCrash(point) {
        P.api('/api/demo/crash', { method: 'POST', body: { point: point } }).then(function (r) {
          if (r.status === 200) { paintArmed(r.data.armed || null); P.ui.toast(r.data.armed ? 'Stop is set. The desk will stop at the chosen moment.' : 'Disarmed.', 'ok'); }
          else P.ui.toast((r.data && (r.data.hint || r.data.error)) || 'Could not change the stop test.', 'warn');
        }, function () { P.ui.toast('Desk stopped. The page will reconnect.', 'warn'); });
      }
      armBtn.addEventListener('click', function () { setCrash(crashSel.value); });
      offBtn.addEventListener('click', function () { setCrash('none'); });
      paintArmed(d0.crash ? d0.crash.armed : null);
      var crashCard = P.ui.card({
        title: 'Stop test', why: 'Stops the desk once, on purpose, so you can watch how it recovers.', testid: 'demo-crash-card',
        body: [
          h('label', { 'class': 'field', 'for': 'dm-crash' }, 'When to stop', crashSel),
          h('div', { 'class': 'row' }, armBtn, offBtn),
          armed,
          h('p', { 'class': 'faint' }, 'Choose the moment, arm the test, then send a lead. The desk stops at that moment. Start it again yourself and watch what it does with the half-finished request.')
        ]
      });

      // reset command
      var cmd = h('code', { 'class': 'dm-cmd mono', 'data-testid': 'demo-reset-command' }, RESET_COMMAND);
      var copy = h('button', { 'class': 'btn btn-sm', type: 'button' }, P.icon('copy'), 'Copy');
      copy.addEventListener('click', function () {
        var done = function () { P.ui.toast('Command copied.', 'ok'); };
        var fail = function () { P.ui.toast('Could not copy. Select the command and copy it by hand.', 'warn'); };
        try { navigator.clipboard.writeText(RESET_COMMAND).then(done, fail); } catch (e) { fail(); }
      });
      var resetCard = P.ui.card({
        title: 'Reset the demo', why: 'Run it once from the project folder. The portal cannot do it for you.', testid: 'demo-reset-card',
        body: [h('p', { 'class': 'muted' }, 'It stops what the start script started and clears all stored leads, so every count goes back to zero.'),
          h('details', { 'class': 'details dm-dev' }, h('summary', null, 'The command to run'), h('div', { 'class': 'dm-cmdrow' }, cmd, copy),
            h('p', { 'class': 'faint' }, 'On Linux: ', h('code', { 'class': 'dm-cmd mono', 'data-testid': 'demo-reset-command-posix' }, RESET_COMMAND_POSIX)))]
      });

      // double Retry: two Retry presses in the same instant on one failed or unknown item; the desk should send one email at most
      var itemSel = h('select', { 'class': 'select', id: 'dm-item', 'data-testid': 'demo-retry-item' });
      var dbl = h('button', { 'class': 'btn', type: 'button', 'data-testid': 'double-retry', disabled: true }, 'Press Retry twice');
      var dblOut = h('div', { 'class': 'dm-result', 'data-testid': 'double-retry-result', hidden: true });
      var dblNote = h('p', { 'class': 'faint' }, 'Looking for failed or unknown follow-ups...');
      var items = [];
      var dblBusy = false;
      // the button waits while a test runs, while nothing can be pressed on, and while the desk does not answer (like the buttons on Needs a person)
      function paintDbl() { dbl.disabled = dblBusy || !items.length || P.stale(); dbl.title = P.stale() ? 'Paused until the desk answers' : ''; }
      var lastReview = null;
      function paintItems(d) {
        lastReview = d;
        items = (d && d.items ? d.items : []).filter(function (i) { return i.kind === 'failed' || i.kind === 'unknown'; });
        var keep = itemSel.value;
        P.ui.clear(itemSel);
        items.forEach(function (i, n) {
          itemSel.appendChild(h('option', { value: i.item_id }, (i.lead_name ? (P.privacy.hidden() ? 'Lead ' + (n + 1) : i.lead_name) : 'No lead stored') + ': ' + (i.kind === 'failed' ? 'follow-up failed' : 'outcome unknown')));
          if (i.item_id === keep) itemSel.selectedIndex = n;
        });
        paintDbl();
        itemSel.disabled = !items.length;
        dblNote.textContent = items.length ? 'Choose a follow-up above, then press the button.' : 'No failed or unknown follow-up right now. Create one with the stop test on the left, then start the desk again.';
      }
      function words(r) {
        if (r.status === 200) return 'accepted';
        var d = r.data || {};
        return 'refused (' + (d.hint || (d.error ? (P.labels.error[d.error] || P.words(d.error)).toLowerCase() : r.status)) + ')';
      }
      function dblFail() { P.ui.toast('Desk stopped. The page will reconnect.', 'warn'); dblBusy = false; paintDbl(); } // any request of the press that gets no answer frees the button
      dbl.addEventListener('click', function () {
        var it = items.filter(function (i) { return i.item_id === itemSel.value; })[0];
        if (!it) return;
        var act = (it.actions || []).filter(function (a) { return a.id === 'retry'; })[0];
        var endpoint = act ? act.endpoint : '/review/' + encodeURIComponent(it.item_id) + '/retry';
        dblBusy = true; paintDbl();
        Promise.all([P.api(endpoint, { method: 'POST' }), P.api(endpoint, { method: 'POST' })]).then(function (rs) {
          dblOut.hidden = false;
          function show(tail) { P.ui.clear(dblOut).appendChild(h('div', null, 'Press 1: ' + rs[0].status + ' ' + words(rs[0]), h('br'), 'Press 2: ' + rs[1].status + ' ' + words(rs[1]), h('br'), tail)); }
          show(h('span', { 'class': 'faint' }, 'Waiting for the send to finish...'));
          var tries = 0;
          (function look() {
            P.api('/api/ui/summary').then(function (sm) {
              tries += 1;
              var sending = sm.data && sm.data.followups ? sm.data.followups.sending : 0;
              if (sending > 0 && tries < 30 && root.isConnected) { setTimeout(look, 1000); return; }
              P.api('/api/demo/state').then(function (st) {
                var n = st.data && st.data.fakes && st.data.fakes.reachable ? st.data.fakes.outbox.count : null;
                show(h('b', null, n === null ? 'The demo mail cannot be read.' : 'Emails in demo mail now: ' + n));
                dblBusy = false; paintDbl();
              }, dblFail);
            }, dblFail);
          })();
        }, dblFail);
      });
      var retryCard = P.ui.card({
        title: 'Retry twice test', why: 'Presses Retry twice in the same instant on a failed follow-up. The desk must accept one press and refuse the other, so at most one email is sent.', testid: 'demo-retry-card',
        body: [h('label', { 'class': 'field', 'for': 'dm-item' }, 'Request', itemSel), h('div', { 'class': 'row' }, dbl), dblNote, dblOut]
      });
      stops.push(P.on('link', paintDbl));
      stops.push(P.on('personal', function () { // names shown or hidden again: relabel the pick list, keep the chosen lead
        Array.prototype.forEach.call(personSel.options, function (o, i) { o.textContent = personText(people[i]); });
        if (lastReview) paintItems(lastReview); // the retry list shows lead names too
      }));
      stops.push(P.poll('review', '/api/ui/review', 2000, function (d, st) { if (st === 200) paintItems(d); }, null));

      root.appendChild(h('div', { 'class': 'dm-page dm-flow', 'data-testid': 'demo-panel' }, sendCard, crashCard, retryCard, resetCard));

      stops.push(P.on('demo', function (st) { if (st && st.crash && st.crash.available) paintArmed(st.crash.armed || null); }));
      this.unmount = function () { stops.forEach(function (f) { f(); }); };
    },
    unmount: function () {}
  };
  // the shell calls panel.unmount; mount() above replaces it for the current visit
  var def = {
    labels: { tile: { outbox: 'Emails in demo mail' } },
    demoLine: LINE,
    navItem: { label: 'Demo controls', icon: 'demo', order: 9 },
    panel: panel,
    overviewTiles: overviewTiles,
    overviewProof: overviewProof,
    overviewExtras: overviewExtras,
    aiExtras: aiExtras,
    needsCardExtras: needsCardExtras,
    leadExtras: leadExtras
  };
  P.registerDemo(def);
  P.on('restart', function () { restarted = true; });

  // The shell paints the demo line; here only hide it when the desk reports real (non-simulated) mode.
  function paintLine() {
    var strip = document.getElementById('demo-line');
    var d = dm();
    if (strip) strip.hidden = !!(d && d.simulated === false);
  }
  paintLine();
  P.on('demo', paintLine);
})();
