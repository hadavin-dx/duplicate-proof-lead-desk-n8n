/* Leads page: table, search, one state filter, and the lead drawer (facts, proof ledger, timeline, later messages). */
(function () {
  'use strict';
  var P = window.Portal;
  var h = P.h;

  function ext(name) {
    var d = P.demo;
    return d && typeof d[name] === 'function' ? d[name] : null;
  }

  var PAGE = 50;
  var STATES = ['pending', 'sending', 'sent', 'failed', 'unknown'];

  var mailSender = null; // the mail step's configured From address (current setting), from /api/ui/data-map

  function lbl(group, code) { return (P.labels[group] && P.labels[group][code]) || P.words(code); }

  // display-only switch: hides email and phone on screen; nothing is deleted or changed in the desk
  // masking is Portal.privacy (one rule for every page and panel); the switch below only changes it
  function shownEmail(v, key) { return P.ui.guarded('email', v, key, '•••@•••••'); }
  // one rule for Company on every screen (table, Facts, later-message differences): shown as it is, never masked, and searched
  // while names are masked, because it is a business name; only name, email address, phone and message are masked
  function shownValue(field, v, key) { if (field === 'company') return v; return P.ui.guarded(field, v, key, field === 'phone' ? '••• ••• ••••' : '••••••'); }

  function nameCell(r) {
    return h('div', { 'class': 'lead-cell' }, h('span', { 'class': 'lead-name', title: r.name && !P.privacy.hidden() ? r.name : null }, r.name ? P.ui.name(r.name) : 'No name'), h('span', { 'class': 'lead-mail' }, shownEmail(String(r.email || '').toLowerCase(), 'lead:' + r.lead_id + ':email')));
  }
  function arrivals(r) {
    return r.repeats ? h('span', null, String(r.repeats + 1), ' ', h('span', { 'class': 'faint' }, '(' + r.repeats + (r.repeats === 1 ? ' repeat' : ' repeats') + ')')) : '1';
  }

  // ---- drawer ---
  var REMINDER_WORDS = { pending: 'Queued', sending: 'Sending', sent: 'Sent', failed: 'To check', unknown: 'To check' };
  var CRM_COPY_WORDS = { copied: 'Copied', waiting: 'Waiting', step_only: 'Step copied', not_copied: 'Not copied' };
  var CRM_COPY_TONE = { copied: 'ok', waiting: 'pending', step_only: 'info', not_copied: 'warn' };
  var CRM_COPY_NOTE = { not_copied: 'The desk gave up after several tries.', step_only: 'The owner stays in the desk: the lead flow file gives this owner no CRM identity.' };
  var GAVE_UP_LATER = ' Moving the lead to a later stage starts the copy afresh.', GAVE_UP_LAST = ' A lead already at the last stage is not copied again.';
  // what the note after a give-up may promise depends on the lead: only a lead that can still move has a later stage to start from
  function crmNote(d) {
    var n = CRM_COPY_NOTE[d.crm_copy];
    if (n && d.crm_copy === 'not_copied') { var st = d.steps || []; n += st.length && st[st.length - 1].id === d.step ? GAVE_UP_LAST : GAVE_UP_LATER; }
    return n;
  }
  // a plain chip for a value in the panel's list, so a row is never bold plain text next to chips; tone names the colour
  function chip(text, tone) { return h('span', { 'class': 'pill pill-' + (tone || 'neutral') }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), text); }
  var REMINDER_TONE = { pending: 'pending', sending: 'info', sent: 'ok', failed: 'warn', unknown: 'warn' };
  // the lead stage as a coloured chip, one colour per step; the lead owner as a round initial beside the name
  var STEP_TONE = { 'new': 'info', contacted: 'pending', replied: 'warm', qualified: 'teal', won: 'ok', closed: 'neutral' };
  function stepChip(r) { return h('span', { 'class': 'pill pill-' + (STEP_TONE[r.step] || 'neutral'), 'data-testid': 'step-chip' }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), r.step_label); }
  function ownerCell(r) { return h('span', { 'class': 'lead-owner', title: r.owner }, h('span', { 'class': 'avatar', 'aria-hidden': 'true' }, String(r.owner).trim().charAt(0).toUpperCase()), h('span', { 'class': 'owner-name' }, r.owner)); }
  var inflight = {}; // one request at a time per receipt or lead, so a double click sends one
  var approverChoice = {}; // receipt number -> chosen approver id, kept while the panel is drawn again
  var stepChoice = '';
  var stepArmed = ''; // 'lead:step' once a real-CRM move has been asked for and awaits its second click
  var stepArmedAt = 0, stepTimer = null;
  var ARM_GAP_MS = 400, ARM_LIFE_MS = 8000; // a click inside the gap (a double click) does not confirm; the question lapses after 8 s
  var FORWARD_ONLY = 'A lead only moves forward.', ARM_HINT = 'Click again within ' + (ARM_LIFE_MS / 1000) + ' s.';
  function disarm() { stepArmed = ''; stepArmedAt = 0; clearTimeout(stepTimer); }
  var reload = null; // set by the page: load the open lead again
  var drawerNow = function () { return null; };

  function step(label, value, tone) {
    return h('li', { 'class': 'ledger-step' + (tone ? ' ledger-' + tone : '') }, h('span', { 'class': 'eyebrow' }, label), h('span', { 'class': 'ledger-value' }, value));
  }
  function ledger(d, now) {
    var fr = d.first_receipt, fu = d.followup;
    var verdict = fr ? (d.duplicates_total ? 'New lead, then ' + d.duplicates_total + (d.duplicates_total === 1 ? ' later message' : ' later messages') + ' recognised' : 'New lead') : 'No decision';
    var steps = [
      step('Lead number', h('span', { 'class': 'mono', title: 'The first six characters of the full lead number ' + d.lead.lead_id }, P.ui.leadRef(d.lead.lead_id).replace(/^Lead /, ''))),
      step('Request number', h('span', { 'class': 'mono', title: fr ? 'The number the sender gave this request: ' + P.shownRef(fr.source_ref) : null }, fr ? P.ui.refNode(fr.source_ref, '') : 'not saved')),
      step('Decision', verdict, 'ok'),
      // both rows are chips (the same chips as the Leads table), so a row never switches between a chip and plain text
      step('Follow-up email', !fu ? 'none' : d.state_view === 'crm_unconfirmed' ? P.ui.waitingChip() : P.ui.pill(d.state_view || fu.state)),
      step('CRM', !fu ? 'none' : fu.crm_done === 1 ? P.ui.pill('new', { text: 'CRM entry written' }) : d.state_view === 'crm_unconfirmed' ? P.ui.pill('crm_unconfirmed') : P.ui.pill('pending', { text: 'CRM entry not written yet' })),
      step('AI rating', fu && fu.ai_label ? P.ui.pill(fu.ai_label) : h('span', { 'class': 'faint' }, 'not rated yet'))
    ];
    if (d.step) { // the lead flow is on for this lead
      steps.push(step('Reminder email', REMINDER_WORDS[d.reminder] ? chip(REMINDER_WORDS[d.reminder], REMINDER_TONE[d.reminder]) : chip('No reminder', 'neutral')));
      steps.push(step('CRM copy', CRM_COPY_WORDS[d.crm_copy] ? h('span', { 'class': 'chip-with-note' }, chip(CRM_COPY_WORDS[d.crm_copy], CRM_COPY_TONE[d.crm_copy]), crmNote(d) ? h('span', { 'class': 'faint step-note' }, crmNote(d)) : null) : chip('Not started', 'neutral')));
    }
    if (fu && mailSender) steps.splice(4, 0, step('Sent from (current setting)', P.ui.guarded('email', mailSender, P.reveal.key('sender', mailSender), '•••@•••••')));
    var extra = ext('leadExtras');
    var node = null;
    if (extra) { try { node = extra(d, P.state.demo); } catch (e) { node = null; } }
    if (node) steps.push(h('li', { 'class': 'ledger-step ledger-extra' }, node));
    return h('section', { 'class': 'drawer-section', 'data-testid': 'proof-ledger' },
      h('h3', null, 'What the desk did'),
      h('p', { 'class': 'muted' }, 'One request, followed from arrival to outcome. Each step is read from the desk’s records.'),
      h('ol', { 'class': 'ledger' }, steps));
  }
  function facts(d) {
    var l = d.lead;
    var rows = [['Email', shownValue('email', String(l.email || '').toLowerCase(), 'lead:' + l.lead_id + ':email')], ['Phone', shownValue('phone', l.phone, 'lead:' + l.lead_id + ':phone')], ['Company', l.company], ['Message', shownValue('message', l.message, 'lead:' + l.lead_id + ':message')], ['Source', lbl('source', l.source)]];
    if (d.owner) rows.push(['Lead owner', d.owner]);
    if (d.step_label) rows.push(['Lead stage', stepControl(d)]);
    var dl = h('dl', { 'class': 'kv' });
    rows.forEach(function (r) { if (r[1]) { dl.appendChild(h('dt', null, r[0])); dl.appendChild(h('dd', null, r[1])); } });
    return h('section', { 'class': 'drawer-section' }, h('h3', null, 'Facts'), dl);
  }
  // the lead stage: the label, and (while a later step exists) a choice of later steps. A lead only moves forward.
  function stepControl(d) {
    var steps = d.steps || [], at = -1;
    steps.forEach(function (s, i) { if (s.id === d.step) at = i; });
    var later = steps.slice(at + 1);
    var wrap = h('span', { 'class': 'step-control', 'data-testid': 'step-control' }, h('span', { 'class': 'step-now', 'data-testid': 'step-now' }, d.step_label));
    if (!later.length) return wrap;
    var sel = h('select', { 'class': 'select select-sm', 'aria-label': 'Move this lead to a later step', 'data-testid': 'step-select' }, h('option', { value: '' }, 'Move to\u2026'),
      later.map(function (s) { return h('option', { value: s.id }, s.label); }));
    var btn = h('button', { 'class': 'btn btn-sm', type: 'button', 'data-testid': 'step-move', disabled: true }, 'Move');
    var hint = h('span', { 'class': 'faint step-note step-hint', 'data-testid': 'step-hint' }, FORWARD_ONLY); // its own row, so the text can change without moving anything
    var armKey = function () { return d.lead.lead_id + ':' + sel.value; };
    function sync() {
      btn.disabled = !sel.value || P.stale(); btn.title = P.stale() ? 'Paused until the desk answers' : null;
      var armed = !!stepArmed && stepArmed === armKey();
      btn.textContent = armed ? 'Yes, write to the CRM' : 'Move';
      hint.textContent = armed ? ARM_HINT : FORWARD_ONLY;
      btn.classList.toggle('btn-warn', armed); // the confirming button is filled amber: it cannot be mistaken for Move
    }
    for (var i = 0; i < sel.options.length; i++) if (sel.options[i].value === stepChoice) sel.value = stepChoice;
    sync();
    sel.addEventListener('change', function () { stepChoice = sel.value; disarm(); sync(); });
    var offLink = P.on('link', function () { if (!btn.isConnected) { offLink(); return; } sync(); }); // the desk answers again: the button wakes up
    btn.addEventListener('click', function () {
      // with a real CRM the move is also written to the CRM's lead status field: ask once more, in place
      if (d.stage_writes_real_crm && stepArmed !== armKey()) {
        stepArmed = armKey(); stepArmedAt = Date.now(); sync();
        clearTimeout(stepTimer);
        stepTimer = setTimeout(function () { disarm(); if (btn.isConnected) sync(); }, ARM_LIFE_MS);
        return;
      }
      if (d.stage_writes_real_crm && Date.now() - stepArmedAt < ARM_GAP_MS) return; // the second half of a double click
      disarm();
      moveStep(d, sel.value, btn);
    });
    // the move is copied to the CRM's lead status field: say so here, above the choice, where the person acts and always in view, when the CRM is a real one
    if (d.stage_writes_real_crm) wrap.appendChild(h('p', { 'class': 'note note-warn step-crm-warning', 'data-testid': 'step-crm-warning' }, P.icon('alert'), h('span', null, 'Every change of this stage, including the automatic first step and the step after the follow-up, is also written to the CRM lead status field. Automations watching that field may run.')));
    wrap.appendChild(sel);
    wrap.appendChild(btn);
    wrap.appendChild(hint);
    return wrap;
  }
  function moveStep(d, to, btn) {
    var key = 'step:' + d.lead.lead_id;
    if (!to || inflight[key]) return;
    inflight[key] = true;
    btn.disabled = true;
    P.api('/leads/' + encodeURIComponent(d.lead.lead_id) + '/step', { method: 'POST', body: { step: to } }).then(function (r) {
      inflight[key] = false;
      if (r.status === 200) {
        stepChoice = '';
        var label = (d.steps || []).filter(function (s) { return s.id === to; })[0];
        P.ui.toast('Saved: the lead is now at ' + (label ? label.label : to) + (r.data && r.data.changed === false ? ' (it was already further on, so nothing changed).' : '.') + (d.stage_writes_real_crm && !(r.data && r.data.changed === false) ? ' The copy to the CRM is pending.' : ''), 'ok');
        if (reload) reload();
      } else {
        P.ui.toast('The desk refused: ' + ((r.data && r.data.error) ? P.words(r.data.error).toLowerCase() : 'it answered ' + r.status) + '.', 'warn');
        btn.disabled = false;
      }
    }, function () { inflight[key] = false; btn.disabled = false; P.ui.toast('Desk stopped. The page will reconnect.', 'warn'); });
  }
  // "the web form" but "the Webhook": the name Webhook keeps its capital
  function viaSource(src) { return src === 'webhook' ? 'the ' + lbl('source', src) : 'the ' + lbl('source', src).toLowerCase(); }
  function timeline(d, now) {
    var fr = d.first_receipt, fu = d.followup;
    var items = [];
    function when(at) {
      if (at === false) return h('span', null);
      if (at === undefined || at === null) return P.ui.relTime(null, now);
      var rel = P.ui.relTime(at, now);
      var el = h('time', { 'class': 'num', title: rel.textContent }, P.ui.utc(at, true).slice(11));
      el.setAttribute('datetime', new Date(at * 1000).toISOString());
      return el;
    }
    function add(text, at, note, tone) { items.push(h('li', { 'class': tone ? 'tl-' + tone : null }, h('div', { 'class': 'tl-text' }, text, note ? h('span', { 'class': 'faint' }, ' ' + note) : null), when(at))); }
    if (fr) { add('Request received through ' + viaSource(fr.source), fr.received_at, null, 'info'); add('Desk decided: new lead', fr.decided_at, null, 'ok'); }
    add('Lead stored', d.lead.created_at, null, 'ok');
    if (fu) {
      if (fu.crm_done === 1) add('CRM entry written', false, '(no time kept)', 'ok');
      else add('CRM entry not written yet', false, '(no time kept)', 'warn');
      if (fu.attempts) add('Follow-up tried ' + fu.attempts + (fu.attempts === 1 ? ' time' : ' times'), fu.updated_at);
      add(fu.state === 'sent' ? 'Follow-up email sent' : d.state_view === 'did_not_finish' ? 'Follow-up did not finish (the email may already have been sent)' : d.state_view === 'crm_unconfirmed' ? 'Follow-up email: waiting for the CRM check' : 'Follow-up email: ' + lbl('state', fu.state).toLowerCase(), fu.state === 'sent' ? fu.sent_at : false, null, P.stateTone(d.state_view || fu.state));
    }
    return h('section', { 'class': 'drawer-section', 'data-testid': 'timeline' }, h('h3', null, 'Timeline'), h('p', { 'class': 'muted tl-key' }, 'Each dot has the colour of its status chip: green done, red failed, amber did not finish, orange unknown, teal CRM not confirmed.'), h('ol', { 'class': 'timeline tl' }, items));
  }
  // approval of a later message (only when the desk was started with approvers): pick a name, press Approve or Deny, confirm.
  // The name is picked from a list, not verified: this portal has no sign-in.
  function decisionLine(x) {
    return (x.decision === 'approved' ? 'Approved' : 'Denied') + ' by ' + x.decided_by_name + ' through the desk\u2019s web address at ' + P.ui.utc(x.decided_at, true) + ' (name picked, not verified)';
  }
  function approvalBlock(d, u) {
    var ap = d.approval;
    if (!ap || !(u.differing || []).length) return null;
    var rid = u.receipt_id;
    if (u.decision) return h('p', { 'class': 'decided-line', 'data-testid': 'decided-line' }, P.icon(u.decision.decision === 'approved' ? 'check' : 'close'), decisionLine(u.decision));
    var sel = h('select', { 'class': 'select select-sm', 'aria-label': 'Approver', 'data-testid': 'approver-select' }, h('option', { value: '' }, 'Choose an approver'),
      (ap.approvers || []).map(function (a) { return h('option', { value: a.id }, a.name); }));
    var yes = h('button', { 'class': 'btn btn-sm btn-primary', type: 'button', 'data-testid': 'approve-button' }, 'Approve');
    var no = h('button', { 'class': 'btn btn-sm btn-outline', type: 'button', 'data-testid': 'deny-button' }, 'Deny');
    for (var i = 0; i < sel.options.length; i++) if (sel.options[i].value === approverChoice[rid]) sel.value = approverChoice[rid];
    function sync() {
      var off = !sel.value || P.stale();
      [yes, no].forEach(function (b) { b.disabled = off; b.title = P.stale() ? 'Paused until the desk answers' : !sel.value ? 'Choose an approver first' : null; });
    }
    sync();
    sel.addEventListener('change', function () { approverChoice[rid] = sel.value; sync(); });
    var offLink = P.on('link', function () { if (!yes.isConnected) { offLink(); return; } sync(); }); // the desk answers again: the buttons wake up
    function name() { return sel.options[sel.selectedIndex].textContent; }
    yes.addEventListener('click', function () { decide(d, u, sel.value, name(), 'approve'); });
    no.addEventListener('click', function () { decide(d, u, sel.value, name(), 'deny'); });
    return h('div', { 'class': 'approval', 'data-testid': 'approval' },
      h('div', { 'class': 'approval-row' }, sel, ap.label ? h('span', { 'class': 'faint' }, ap.label) : null, h('span', { 'class': 'spacer' }), yes, no),
      h('p', { 'class': 'faint approval-note' }, 'Approver names come from the list the desk was started with. This portal has no sign-in; a name is picked, not verified.'));
  }
  function decide(d, u, who, whoName, word) {
    var rid = u.receipt_id, key = 'decide:' + rid;
    if (!who || inflight[key]) return;
    var cap = word === 'approve' ? 'Approve' : 'Deny';
    P.ui.confirm({ title: cap + ' this later message?', body: 'Approver: ' + whoName + '. Later message number ' + rid + '.', verb: cap, note: 'This decision is saved in the desk only. It changes no lead and sends nothing.' }).then(function (ok) {
      if (!ok || inflight[key]) return;
      if (P.stale()) { P.ui.toast('Actions are paused until the desk answers.', 'warn'); return; }
      inflight[key] = true;
      P.api('/review/D' + rid + '/resolve', { method: 'POST', body: { decision: word + ':' + who } }).then(function (r) {
        inflight[key] = false;
        var x = r.data;
        if ((r.status === 200 || r.status === 409) && x && x.decision && x.decided_by_name) {
          u.decision = { decision: x.decision, decided_by: x.decided_by, decided_by_name: x.decided_by_name, channel: x.channel, decided_at: x.decided_at };
          if (drawerNow()) fillDrawer(drawerNow().body, d);
          P.ui.toast((r.status === 200 ? 'Saved: ' : 'Already decided: ') + decisionLine(u.decision), r.status === 200 ? 'ok' : 'warn');
        } else {
          P.ui.toast('The desk refused: ' + (x && x.error ? P.words(x.error).toLowerCase() : 'it answered ' + r.status) + '.', 'warn');
        }
      }, function () { inflight[key] = false; P.ui.toast('Desk stopped. The page will reconnect.', 'warn'); });
    });
  }
  // the later messages of a lead (requests with a new request number and the same email address), newest first
  function laterMessages(d) {
    var dups = d.duplicates || [];
    var firstRid = d.first_receipt ? d.first_receipt.receipt_id : 0;
    var kids = [h('h3', null, 'Later messages')];
    if (!dups.length) kids.push(h('p', { 'class': 'muted' }, 'No later messages.'));
    else {
      kids.push(h('p', { 'class': 'muted' }, 'Same email address, new request. The first record never changes.'));
      dups.forEach(function (u) {
        var rows = (u.differing || []).map(function (f) {
          return h('div', { 'class': 'diff-row' },
            h('div', { 'class': 'diff-field' }, P.words(f.field)),
            h('div', { 'class': 'diff-old' }, h('span', { 'class': 'eyebrow' }, 'First record'), h('span', { 'class': 'later-value', tabindex: '0' }, shownValue(f.field, f.first, 'lead:' + d.lead.lead_id + ':' + f.field) || 'empty')),
            h('div', { 'class': 'diff-new' }, h('span', { 'class': 'eyebrow' }, 'This message'), h('span', { 'class': 'later-value', tabindex: '0' }, shownValue(f.field, f['this'], 'later:' + d.lead.lead_id + ':' + u.receipt_id + ':' + f.field))));
        });
        kids.push(h('div', { 'class': 'later-card', 'data-testid': 'later-card', 'data-receipt-id': String(u.receipt_id) },
          h('div', { 'class': 'later-meta' }, 'Received ' + P.ui.utc(u.received_at, true) + ' through ' + viaSource(u.source) + ' · ',
            h('span', { 'class': 'mono', title: 'Request number ' + P.shownRef(u.source_ref) }, P.ui.refNode(u.source_ref, 'Request '))),
          h('div', { 'class': 'later-meta', title: u.seconds_after_first + ' seconds after the first message, by the desk clock' }, P.ui.afterFirst(u.seconds_after_first, u.receipt_id, firstRid)),
          rows.length ? rows : h('p', { 'class': 'muted' }, 'Same details as the first record.'),
          approvalBlock(d, u)));
      });
      if (d.duplicates_total > dups.length) kids.push(h('p', { 'class': 'muted', 'data-testid': 'later-foot' }, 'Showing the newest ' + dups.length + ' of ' + d.duplicates_total + ' later messages.'));
    }
    return h('section', { 'class': 'drawer-section', 'data-testid': 'later-messages' }, kids);
  }
  function tech(d) {
    var fu = d.followup || {};
    var rows = [['Request number', d.first_receipt ? P.ui.refNode(d.first_receipt.source_ref, '') : null], ['Full lead number', d.lead.lead_id], ['Receipt number', d.lead.receipt_id], ['Source', d.lead.source], ['Follow-up state', fu.state], ['AI rating', fu.ai_label], ['Tries', fu.attempts], ['Automatic tries', fu.auto_attempts], ['Last error', fu.last_error], ['CRM step done', fu.crm_done], ['Earlier send unclear', fu.ambiguous_prior], ['Lead owner', d.owner], ['Lead stage', d.step], ['Reminder state', d.reminder], ['CRM copy', d.crm_copy], ['Review items', (d.review_items || []).join(', ')]];
    var dl = h('dl', { 'class': 'kv' });
    rows.forEach(function (r) { if (r[1] !== null && r[1] !== undefined && r[1] !== '') { dl.appendChild(h('dt', null, r[0])); dl.appendChild(h('dd', null, typeof r[1] === 'object' ? r[1] : String(r[1]))); } });
    return h('details', { 'class': 'details', 'data-testid': 'technical-details' }, h('summary', null, 'Technical details (record numbers and raw values)'), dl);
  }
  // the words of the Next step line: they must match the buttons the card on Needs a person really has
  function fillDrawer(body, d) {
    var now = d.now;
    P.ui.clear(body);
    if (d.next_step) body.appendChild(h('p', { 'class': 'note ' + (d.state_view === 'failed' ? 'note-bad' : 'note-warn'), 'data-testid': 'next-step' }, P.icon('alert'), h('span', null, d.next_step)));
    // with the lead flow on, the facts (and the stage control with its CRM warning) come first: the question before a CRM write is always in view
    if (d.step_label) body.appendChild(facts(d));
    body.appendChild(ledger(d, now));
    if (!d.step_label) body.appendChild(facts(d));
    body.appendChild(timeline(d, now));
    body.appendChild(laterMessages(d));
    body.appendChild(tech(d));
  }

  // ---- page ---
  P.page('leads', {
    title: 'Leads',
    purpose: 'Every lead the desk has stored, with what happened to its follow-up. Select a row for the full story. The number beside Needs a person counts requests and follow-ups, so it can be larger than the leads listed here.',
    nav: { label: 'Leads', icon: 'leads', order: 2 },
    mount: function (root, ctx) {
      var drawerRow = null, q = '', stateFilter = '', offset = 0, drawer = null, stopPoll = null, searchFocused = false, drawerData = null, timer = null, lastJson = '', fresh = true;
      var input = h('input', { 'class': 'input', type: 'search', placeholder: 'Company or lead number', 'aria-label': 'Search leads', 'data-testid': 'leads-search', maxlength: 100, autocomplete: 'off', spellcheck: 'false' });
      var select = h('select', { 'class': 'select', 'aria-label': 'Filter by follow-up status', 'data-testid': 'leads-state-filter' },
        h('option', { value: '' }, 'All follow-up statuses'),
        STATES.map(function (s) { return h('option', { value: s }, lbl('state', s)); }));
      var count = h('span', { 'class': 'muted leads-count', 'aria-live': 'off' }, '');
      var hideBox = h('input', { type: 'checkbox', 'data-testid': 'leads-hide-personal' });
      hideBox.checked = P.privacy.hidden();
      var hideSwitch = h('label', { 'class': 'hide-switch', title: 'On by default. Hides lead names, email addresses, phone numbers, messages and request numbers on this screen. Switch off to see them and to search by name or email address. Display only: the data stays stored in the desk.' }, hideBox, h('span', null, 'Hide names and email addresses'), h('span', { 'class': 'faint hide-note' }, 'on screen only'));
      var searchHint = h('span', { 'class': 'faint search-hint', 'data-testid': 'leads-search-hint', title: 'Showing one value with its eye button does not turn name or address search on.' }, 'Names and addresses are searchable after you turn off hiding');
      // one arrangement at every width: search, filter and count on the first row; the switch and its hint on the second
      var bar = h('div', { 'class': 'toolbar-block' },
        h('div', { 'class': 'toolbar' }, h('label', { 'class': 'search' }, h('span', { 'class': 'sr-only' }, 'Search leads'), P.icon('search'), input), select, h('span', { 'class': 'spacer' }), count),
        h('div', { 'class': 'toolbar' }, hideSwitch, searchHint));
      var out = h('div', { 'class': 'leads-out' }, P.ui.skeleton(6));
      var strip = h('div', { 'class': 'sumstrip', 'data-testid': 'leads-summary' });
      function stat(label, value) { return h('div', { 'class': 'sumstat' }, h('div', { 'class': 'sumstat-value num' }, value === null || value === undefined ? '\u2013' : String(value)), h('div', { 'class': 'eyebrow' }, label)); }
      function paintStrip() {
        var sm = P.state.summary;
        strip.classList.toggle('is-stale', P.stale());
        P.ui.clear(strip);
        // without an answer yet the strip keeps its place (dashes), so the table does not jump when the numbers arrive
        strip.appendChild(stat('Leads', sm ? sm.counts['new'] : null));
        strip.appendChild(stat('Follow-ups sent', sm ? sm.followups.sent : null));
        strip.appendChild(stat('Repeats caught', sm ? sm.counts.duplicate : null));
        if (sm && P.stale()) strip.appendChild(h('div', { 'class': 'sumstat-asof faint', 'data-testid': 'leads-asof' }, 'as of the last update'));
      }
      // the search and the filter cannot work while the list cannot load
      function listWorks(ok) {
        input.disabled = !ok; select.disabled = !ok; hideBox.disabled = !ok;
        hideSwitch.classList.toggle('is-disabled', !ok);
        input.title = select.title = ok ? '' : 'Not available while the list cannot load';
      }
      // while addresses are masked the search does not match address text (the desk is asked not to look at it)
      function paintSearch() {
        var masked = P.privacy.hidden();
        input.placeholder = masked ? 'Company or lead number' : 'Name, email, company or request number';
        input.setAttribute('aria-label', input.placeholder);
        searchHint.style.visibility = masked ? 'visible' : 'hidden'; // keeps its place, so the switch and the table do not move
      }
      paintSearch();
      paintStrip();
      var offSummary = P.on('summary', paintStrip), offLink = P.on('link', paintStrip);
      root.appendChild(strip);
      root.appendChild(bar);
      root.appendChild(out);

      function path() {
        var p = '/api/ui/leads?limit=' + PAGE + '&offset=' + offset;
        if (q) p += '&q=' + encodeURIComponent(q);
        if (stateFilter) p += '&state=' + encodeURIComponent(stateFilter);
        if (P.privacy.hidden()) p += '&by_email=0';
        return p;
      }
      var lastData = null;
      function filtered() { return !!(q || stateFilter); }

      function openLead(row) {
        if (drawer) { drawer.close(); P.reveal.clear(); }
        if (drawerRow && drawerRow.lead_id !== row.lead_id) { disarm(); stepChoice = ''; } // another lead's panel replaces this one: its question and choice do not carry over
        var body = h('div', { 'class': 'drawer-body-inner' }, P.ui.skeleton(5));
        drawerRow = row;
        // a panel closed here sends its 'close' event a moment later: it must not clear the state of the panel that replaced it
        var mine = null;
        mine = drawer = P.ui.drawer({ title: row.name ? P.ui.name(row.name) : 'Lead', label: P.privacy.hidden() ? 'Lead' : row.name || 'Lead', pill: P.ui.pill(row.state_view || row.state), body: body, testid: 'lead-drawer', onClose: function () { if (drawer !== mine) return; disarm(); stepChoice = ''; drawer = null; drawerData = null; drawerRow = null; P.reveal.clear(); } });
        drawerNow = function () { return drawer; };
        reload = function () { if (drawerRow) openLead(drawerRow); };
        P.api('/api/ui/leads/' + encodeURIComponent(row.lead_id)).then(function (r) {
          if (drawer !== mine) return; // closed, or replaced by a newer panel: that one fills itself
          var inner = mine.body;
          if (r.status === 200 && r.data) { drawerData = r.data; fillDrawer(inner, r.data); return; }
          P.ui.clear(inner).appendChild(r.status === 404
            ? P.ui.emptyState({ heading: 'This lead is no longer stored', sentence: 'Close this panel and pick another row.' })
            : P.ui.errorState({ what: 'this lead', status: r.status, retry: function () { openLead(row); } }));
        }, function () {
          if (drawer === mine) P.ui.clear(mine.body).appendChild(P.ui.errorState({ what: 'this lead', retry: function () { openLead(row); } }));
        });
      }

      // the list cannot load: keep the last rows, dimmed, when there are some; otherwise say once that there is no list
      function failed(status) {
        lastJson = '';
        listWorks(false);
        if (lastData && out.querySelector('[data-testid="leads-table"]')) { out.classList.add('is-stale'); return; }
        P.ui.clear(out).appendChild(P.ui.errorState({ what: 'leads', status: status, retry: restart, heading: 'The lead list is not available' }));
      }
      function render(d, status, force) {
        if (drawer && !force) return; // paused while the drawer is open (a restart is not paused: it repaints at once)
        if (status !== 200 || !d) {
          failed(status);
          return;
        }
        listWorks(true);
        out.classList.remove('is-stale');
        lastData = d;
        var sig = JSON.stringify(d.rows) + d.total;
        if (sig === lastJson && out.firstChild && !out.querySelector('[data-testid="skeleton"]')) { return; }
        lastJson = sig;
        paint(d);
      }
      // builds the table and pager; not paused by an open panel (the switch repaints the table behind it)
      function paint(d) {
        count.textContent = filtered() ? d.total + (d.total === 1 ? ' lead matches' : ' leads match') : '';
        var table = P.ui.table({
          testid: 'leads-table', rowTestid: 'lead-row',
          rowAttrs: function (r) { return { 'data-lead-id': r.lead_id }; },
          columns: [
            { key: 'name', label: 'Lead', render: nameCell },
            { key: 'company', label: 'Company', render: function (r) { return r.company || h('span', { 'class': 'faint' }, '–'); } },
            { key: 'source', label: 'Source', render: function (r) { return r.source === 'webhook' ? h('span', { title: 'A Webhook is another system sending the request by itself, without a web form.' }, lbl('source', r.source)) : lbl('source', r.source); } },
            { key: 'state', label: 'Follow-up', render: function (r) { return P.ui.followupChips(r.state_view, r.state); } },
            { key: 'ai_label', label: 'AI rating', render: function (r) { return r.ai_label ? P.ui.pill(r.ai_label) : h('span', { 'class': 'faint' }, '–'); } },
            { key: 'arrivals', label: 'Requests', render: arrivals },
            { key: 'owner', label: 'Lead owner', render: function (r) { return r.owner ? ownerCell(r) : h('span', { 'class': 'faint' }, '–'); } },
            { key: 'step', label: 'Step', render: function (r) { return r.step_label ? stepChip(r) : h('span', { 'class': 'faint' }, '–'); } },
            { key: 'updated_at', label: 'Last change', render: function (r) { return P.ui.relTime(r.updated_at, d.now); } }
          ],
          rows: d.rows,
          empty: filtered() ? { heading: 'No leads match', sentence: P.privacy.hidden() && q ? 'Names and email addresses are hidden and are not searched. Turn off hiding to search them.' : 'Try a different search or clear the status filter.' } : { heading: 'No leads yet', sentence: 'When a request arrives and the desk accepts it, the person appears here.' },
          onOpen: openLead
        });
        table.appendChild(h('div', { 'class': 'table-foot' }, d.total + (d.total === 1 ? ' lead' : ' leads') + (filtered() ? (d.total === 1 ? ' matches' : ' match') : '') + ', newest change first'));
        P.ui.clear(out).appendChild(table);
        if (d.total > PAGE) {
          var prev = h('button', { 'class': 'btn btn-sm', type: 'button' }, 'Previous');
          var next = h('button', { 'class': 'btn btn-sm', type: 'button' }, 'Next');
          prev.disabled = offset <= 0;
          next.disabled = offset + PAGE >= d.total;
          prev.addEventListener('click', function () { offset = Math.max(0, offset - PAGE); restart(); });
          next.addEventListener('click', function () { offset += PAGE; restart(); });
          out.appendChild(h('div', { 'class': 'pager' }, h('span', { 'class': 'muted' }, (offset + 1) + ' to ' + Math.min(offset + PAGE, d.total) + ' of ' + d.total), h('span', { 'class': 'spacer' }), prev, next));
        }
      }
      function restart() {
        if (stopPoll) stopPoll();
        lastJson = '';
        fresh = true;
        stopPoll = P.poll('leads', path(), 5000, function (d, st) { if (fresh || !searchFocused) { var first = fresh; fresh = false; render(d, st, first); } }, function () {
          if (!drawer) failed(0);
        });
      }
      input.addEventListener('focus', function () { searchFocused = true; });
      input.addEventListener('blur', function () { searchFocused = false; });
      input.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(function () { q = input.value.trim().slice(0, 100); offset = 0; restart(); }, 250);
      });
      // one function applies the switch: the checkbox and another tab (storage event) both come here
      function applyMask() {
        var nowHidden = P.privacy.hidden();
        hideBox.checked = nowHidden;
        paintSearch();
        if (nowHidden && q) { q = ''; input.value = ''; offset = 0; P.ui.clear(out).appendChild(P.ui.skeleton(6)); } // a typed address never stays on screen, nor the rows it found
        else if (lastData) { lastJson = ''; paint(lastData); }
        paintTitle();
        if (drawer && drawerData) fillDrawer(drawer.body, drawerData); // an open panel is masked or shown at once
        restart(); // the next poll asks without or with the email address in the search
      }
      hideBox.addEventListener('change', function () { P.privacy.set(hideBox.checked); }); // set() tells every listener, this page included
      var offPersonal = P.on('personal', applyMask);
      // one value was shown or hidden (or its time ran out): draw the table and an open panel again, keeping every other choice
      P.api('/api/ui/data-map').then(function (r) { // once: the sender address is a setting, it does not change while the desk runs
        if (r.status === 200 && r.data && typeof r.data.mail_sender === 'string') { mailSender = r.data.mail_sender; if (drawer && drawerData) fillDrawer(drawer.body, drawerData); }
      }, function () { /* the row simply stays out */ });
      function paintTitle() { if (drawer && drawerRow) drawer.setTitle(drawerRow.name ? P.ui.name(drawerRow.name) : 'Lead', P.privacy.hidden() ? 'Lead' : drawerRow.name || 'Lead'); }
      var offReveal = P.on('reveal', function () { paintTitle(); if (lastData) { lastJson = ''; paint(lastData); } if (drawer && drawerData) fillDrawer(drawer.body, drawerData); });
      select.addEventListener('change', function () { stateFilter = select.value; offset = 0; restart(); });
      restart();
      this.unmount = function () { offSummary(); offLink(); offPersonal(); offReveal(); P.reveal.clear(); clearTimeout(timer); if (stopPoll) stopPoll(); if (drawer) drawer.close(); };
    }
  });
})();
