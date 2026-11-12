/* Overview page. Everything on it comes from one /api/ui/summary answer, so the numbers add up by construction.
   Optional extensions (extra tiles, a status strip, a cross-check line) arrive through Portal.demoExt hooks; this file knows nothing about them. */
(function () {
  'use strict';
  var P = window.Portal;
  var h = P.h;
  var FEED_ITEMS = 12;
  var restarts = []; // desk restarts seen while the page was open (newest first)

  P.on('restart', function (startedAt) {
    restarts.unshift({ at: startedAt });
    if (restarts.length > 3) restarts.pop();
  });

  function ext(name) { return P.demoExt && typeof P.demoExt[name] === 'function' ? P.demoExt[name] : null; }
  function extra(name, summary) {
    var fn = ext(name);
    if (!fn || !P.state.demo) return null;
    try { return fn(summary, P.state.demo); } catch (e) { return null; }
  }
  function tileLabel(key, dflt) { var t = P.labels.tile; return (t && t[key]) || dflt; }
  function sourceName(src) { return P.labels.source[src] || P.words(src); }
  function clockOf(epoch) { return P.ui.utc(epoch).slice(11); }

  // ---- tiles
  function tileSpecs(d) {
    var c = d.counts;
    var needs = P.needsCount();
    var caption;
    if (needs > 0) caption = h('span', null, 'Requests and follow-ups a person has to decide. ', h('a', { href: '#/needs', 'class': 'nowrap' }, 'Review these \u2192'), d.waiting > 0 ? ' (' + d.waiting + ' more are slow: see the Waiting tab)' : '');
    else caption = d.waiting > 0 ? d.waiting + ' slow (see the Waiting tab), none need a person' : 'Nothing needs a person';
    var list = [
      { testid: 'kpi-leads', label: tileLabel('leads', 'Leads'), value: c['new'], caption: c.received ? 'From ' + c.received.toLocaleString() + (c.received === 1 ? ' request received' : ' requests received') : 'No requests yet', tone: 'info' },
      { testid: 'kpi-outbox', label: tileLabel('outbox', 'Follow-ups sent'), value: d.followups.sent, caption: 'Emails the desk has marked as sent', tone: 'ok' },
      { testid: 'kpi-duplicates', label: tileLabel('duplicates', 'Repeats caught'), value: c.duplicate, caption: 'Repeats that made no second lead', tone: 'neutral' },
      { testid: 'kpi-needs', label: tileLabel('needs', 'Needs a person'), value: needs, caption: caption, tone: needs > 0 ? 'warn' : 'neutral' }
    ];
    var more = extra('overviewTiles', d);
    if (Array.isArray(more)) {
      more.forEach(function (t) {
        var at = -1;
        list.forEach(function (x, i) { if (x.testid === t.testid) at = i; });
        if (at >= 0) list[at] = t; else list.push(t);
      });
    }
    return list;
  }

  // ---- reconcile card
  function bold(v) { return h('b', { 'class': 'num' }, String(v)); }
  function reconcileCard(d) {
    var c = d.counts;
    var parts = c['new'] + c.duplicate + c.review + c.open;
    var sentence;
    if (d.reconciled && parts === c.received) {
      sentence = h('div', { 'class': 'ov-eq', 'data-testid': 'reconcile-sentence' },
        h('div', { 'class': 'ov-eqnum num' }, String(c.received)), h('div', { 'class': 'ov-eqtext' }, h('b', null, c.received === 1 ? 'request received' : 'requests received')));
    } else {
      sentence = h('p', { 'class': 'note note-bad', role: 'alert', 'data-testid': 'reconcile-sentence' }, P.icon('alert'),
        h('span', null, 'The counts do not add up: ' + c.received + ' received, but the four groups add up to ' + parts + '. Check the stored leads before trusting these numbers.'));
    }
    var bar = P.ui.splitBar([
      { label: 'New', value: c['new'], tone: 'ok' },
      { label: 'Repeat', value: c.duplicate, tone: 'neutral' },
      { label: 'Request to check', value: c.review, tone: 'warn' },
      { label: 'Not decided yet', value: c.open, tone: 'pending' }
    ], { label: 'Received requests by group', testid: 'reconcile-bar' });
    var legendNote = h('p', { 'class': 'muted ov-legend-note', 'data-testid': 'open-explained' }, '“Not decided yet” means the desk has stored the request but has not decided it.');
    var bridge = h('p', { 'class': 'muted ov-legend-note', 'data-testid': 'needs-bridge' }, 'Needs a person also counts follow-ups, not only requests to check.');
    return P.ui.card({
      title: 'Requests and what the desk did with them',
      why: 'Every request received is counted once, in one of the four groups below, so the groups add up to the number received.',
      tip: 'These count requests, not follow-up emails. “Request to check” is a request that is missing something or that the desk could not store or decide, so a person has to look; “Not decided yet” is a request it has stored but not yet decided. The Needs a person tile counts these incomplete requests plus failed and outcome-unknown follow-up emails, so it can be larger than this group.',
      testid: 'reconcile', cls: 'card-key',
      body: [sentence, bar, legendNote, bridge]
    });
  }

  // ---- sources, follow-ups, activity
  function sourcesCard(d) {
    var tones = { form: 'info', webhook: 'pending' };
    var bar = P.ui.donut(d.doors.map(function (x) { return { label: sourceName(x.source), value: x.received, tone: tones[x.source] || 'neutral' }; }), { label: 'Requests by source', testid: 'doors-bar', unit: 'requests' });
    var rows = h('ul', { 'class': 'list ov-sources' }, d.doors.map(function (x) {
      return h('li', null, h('span', null, sourceName(x.source)), h('span', { 'class': 'spacer' }),
        h('span', { 'class': 'muted' }, 'last request '), x.last_seen === null ? h('span', { 'class': 'faint' }, 'none yet') : P.ui.relTime(x.last_seen, d.now));
    }));
    // turned away at intake = before the request was stored, so it is counted with the sources, not with the groups above
    var refused = h('p', { 'class': 'ov-refused', 'data-testid': 'refused-count' }, 'Requests turned away before they were stored: ', bold(d.refused_at_door));
    return P.ui.card({ title: 'Where requests come from', why: 'Requests by source: the web form and webhook requests.', testid: 'doors', body: [bar, rows, refused] });
  }
  function followupsCard(d) {
    var f = d.followups;
    // one colour per state (P.stateTone): the ring, its legend, the badges, the timeline and the feed all agree
    var parts = [], any = 0;
    // the same words as every other page; the two groups that split Failed and Outcome unknown show only when they hold something
    ['sent', 'pending', 'sending', 'failed', 'did_not_finish', 'unknown', 'crm_unconfirmed'].forEach(function (k) {
      if ((k === 'did_not_finish' || k === 'crm_unconfirmed') && !f[k]) return;
      any += f[k] || 0;
      parts.push({ label: P.labels.state[k] || P.words(k), value: f[k] || 0, tone: P.stateTone(k) });
    });
    // one line saying how the two amber states differ, shown only when one of them is on the chart
    var apart = f.did_not_finish || f.unknown || f.crm_unconfirmed ? h('p', { 'class': 'muted ov-legend-note', 'data-testid': 'followups-apart' }, [f.did_not_finish ? 'Did not finish: the desk tried and stopped.' : '', f.unknown ? 'Outcome unknown: the desk cannot tell whether the email was sent.' : '', f.crm_unconfirmed ? 'CRM not confirmed: the follow-up is waiting for the CRM check.' : ''].join(' ').trim()) : null;
    var body = any ? [P.ui.donut(parts, { label: 'Follow-ups by state', testid: 'followups-bar', unit: 'follow-ups' }), apart, h('p', { 'class': 'muted ov-foot' }, f.sent + ' of ' + any + (any === 1 ? ' follow-up email' : ' follow-up emails') + ' sent so far.')] : P.ui.emptyState({ heading: 'No follow-up emails yet', sentence: 'Each new lead gets one email; its state is counted here.' });
    return P.ui.card({ title: 'Follow-ups', why: 'The email each new lead gets.', testid: 'followups', body: body, cls: any ? '' : 'card-empty' });
  }
  // the scale tops at the largest bar rounded up to an even number: its middle mark is a whole number and no bar is dwarfed by an odd margin
  function niceMax(top) { return Math.max(2, top + (top % 2)); }
  // one window for the chart and its axis: the last 5 minutes, marked in whole minutes
  var WINDOW = 300;
  // short labels (5m 4m 3m 2m 1m now): the full phrase does not fit between the ticks at 1280 px; the title and the card text say minutes ago
  function agoText(min) { return min + 'm'; }
  function activityCard(d) {
    var all = d.series.buckets, step0 = d.series.bucket_seconds;
    var total = 0, top = 1;
    var b0 = all.slice(Math.max(0, all.length - Math.ceil(WINDOW / step0)));
    // thin bars are hard to read: buckets under 15 seconds are joined into bars of about 15 seconds (an oldest partial group is dropped)
    var g = Math.max(1, Math.round(15 / step0));
    b0 = b0.slice(b0.length % g);
    var b = [], jj, jEnd; // jj and jEnd: the inner loop below (no function inside the loop, and no new var statement)
    for (var j = 0; j < b0.length; j += g) {
      var grp = { t: b0[j].t, 'new': 0, duplicate: 0, review: 0 };
      for (jj = j, jEnd = Math.min(j + g, b0.length); jj < jEnd; jj++) { grp['new'] += b0[jj]['new']; grp.duplicate += b0[jj].duplicate; grp.review += b0[jj].review; }
      b.push(grp);
    }
    var step = step0 * g;
    b.forEach(function (x) { total += x['new'] + x.duplicate + x.review; });
    var tots = b.map(function (x) { var t = x['new'] + x.duplicate + x.review; if (t > top) top = t; return t; });
    var max = niceMax(top);
    var spanSec = b.length * step; // fewer buckets than the window: label what is drawn
    var mins = Math.max(1, Math.round(spanSec / 60));
    var spanText = mins + (mins === 1 ? ' minute' : ' minutes');
    var caption = total ? h('p', { 'class': 'muted' }, h('b', { 'class': 'num' }, String(total)), total === 1 ? ' request decided' : ' requests decided', ' in the last ' + spanText + '. Each bar is ' + step + '\u00a0seconds.')
      : h('p', { 'class': 'chart-empty' }, 'Nothing was decided in the last ' + spanText + '. ', P.demo && !P.stale() ? h('a', { href: '#/demo' }, 'Send a request from Demo controls \u2192') : null);
    if (!total) {
      var none = P.ui.emptyState({ heading: P.stale() ? 'No requests were decided in the ' + spanText + ' before the last update' : 'No requests decided in the last ' + spanText, sentence: 'A bar appears here for each stretch of ' + step + ' seconds in which the desk decides a request.' });
      if (P.demo && !P.stale()) none.appendChild(h('a', { href: '#/demo' }, 'Send a request from Demo controls \u2192'));
      return P.ui.card({ title: 'Activity', why: 'Requests the desk decided, newest on the right.', testid: 'activity', body: none, cls: 'card-empty' });
    }
    var bars = h('div', { 'class': 'ov-bars', role: 'img', 'aria-label': total + ' requests decided in the last ' + spanText + ', one bar per ' + step + ' seconds' });
    tots.forEach(function (t, i) {
      var lvl = t ? Math.max(1, Math.round(20 * t / max)) : 0;
      var tip = (t === 1 ? '1 request' : t + ' requests') + ' at ' + clockOf(b[i].t);
      bars.appendChild(h('span', { 'class': 'ov-bar ov-h' + lvl, 'data-tip': tip, title: tip }));
    });
    var ticks = [];
    for (var k = mins; k >= 1; k--) ticks.push(h('span', { 'class': 'ov-tick', title: k + (k === 1 ? ' minute ago' : ' minutes ago') }, agoText(k)));
    ticks.push(h('span', { 'class': 'ov-tick ov-tick-end' }, 'now'));
    var chart = h('div', { 'class': 'ov-chartbox' }, h('div', { 'class': 'ov-ytitle' }, 'Requests decided per ' + step + ' seconds'), h('div', { 'class': 'ov-chart' },
      h('div', { 'class': 'ov-y' }, h('span', { 'class': 'num' }, String(max)), h('span', { 'class': 'num' }, String(max / 2)), h('span', { 'class': 'num' }, '0')),
      bars,
      h('div', { 'class': 'ov-axis ov-axis-' + Math.min(mins, 5) }, ticks)));
    return P.ui.card({ title: 'Activity', why: 'Requests the desk decided, newest on the right. The marks count minutes back from now.', testid: 'activity', body: [chart, caption] });
  }

  // ---- live feed
  function feedLine(r) {
    var who = r.name ? P.ui.name(r.name) : null; // the lead's name, masked with its own eye like an address
    var icon = 'clock', tone = 'neutral', text, pill = null;
    if (r.kind === 'received') {
      text = ['Request received', who ? [' from ', who] : null];
      pill = P.ui.pill('received');
    } else if (r.kind === 'decided') {
      if (r.verdict === 'new') { icon = 'check'; tone = 'ok'; text = ['New lead: ', who || 'unnamed']; }
      else if (r.verdict === 'duplicate') { icon = 'copy'; tone = 'neutral'; text = r.count > 1 ? [who || 'Someone', ' sent the same request again (repeats shown as one row): no second lead'] : ['Repeat of ', who || 'an earlier lead', ': no second lead']; }
      else { icon = 'alert'; tone = 'warn'; text = ['Could not be decided: sent to a person']; }
      pill = P.ui.pill(r.verdict);
    } else {
      icon = 'send';
      var st = r.state_view || r.state;
      tone = P.stateTone(st);
      var verb = { sent: 'sent to', pending: 'queued for', sending: 'being sent to', failed: 'failed for', did_not_finish: 'did not finish for', unknown: 'outcome unknown for', crm_unconfirmed: 'CRM not confirmed for' }[st] || 'update for';
      text = st === 'crm_unconfirmed' ? ['The CRM is not confirmed for ', who || 'a lead'] : ['Follow-up email ' + verb + ' ', who || 'a lead'];
      pill = P.ui.pill(st);
    }
    return { icon: icon, tone: tone, text: text, pill: pill, door: sourceName(r.source), ref: r.source_ref || '' };
  }
  function feedItem(r, now) {
    var f = feedLine(r);
    return h('li', { 'class': 'ov-feed-item', 'data-testid': 'feed-item' },
      h('span', { 'class': 'ov-when' }, P.ui.relTime(r.at, now)),
      h('span', { 'class': 'ov-ico ov-ico-' + f.tone }, P.icon(f.icon)),
      h('span', { 'class': 'ov-text', title: f.ref ? P.ui.requestRef(f.ref) + ' (full request number ' + P.shownRef(f.ref) + ')' : null }, f.text),
      h('span', { 'class': 'ov-source' }, f.door),
      h('span', { 'class': 'ov-pill' }, f.pill));
  }
  // fewer, calmer rows: a received row is implied by its decision, and consecutive repeats from one person become one row with a count
  function groupFeed(rows) {
    var decided = {};
    rows.forEach(function (r) { if (r.kind === 'decided') decided[r.receipt_id] = true; });
    var out = [];
    rows.forEach(function (r) {
      if (r.kind === 'received' && decided[r.receipt_id]) return;
      var last = out[out.length - 1];
      if (last && r.kind === 'decided' && r.verdict === 'duplicate' && last.kind === 'decided' && last.verdict === 'duplicate' && last.name === r.name && last.source === r.source) { last.count = (last.count || 1) + 1; return; }
      out.push(Object.assign({}, r));
    });
    return out;
  }
  function restartRow(r, now) {
    return h('li', { 'class': 'ov-feed-item', 'data-testid': 'restart-note' },
      h('span', { 'class': 'ov-when' }, P.ui.relTime(r.at, now)),
      h('span', { 'class': 'ov-ico ov-ico-warn' }, P.icon('bolt')),
      h('span', { 'class': 'ov-text' }, 'Desk restarted'),
      h('span', { 'class': 'ov-source' }, 'Restarted at ' + clockOf(r.at)),
      h('span', { 'class': 'ov-pill' }, P.ui.pill('restarted')));
  }
  function feedCard(d) {
    // restarts and requests share one list, newest first (a restart is placed by its own time)
    var entries = restarts.map(function (r) { return { at: r.at, node: restartRow(r, d.now) }; });
    groupFeed(d.recent).forEach(function (r) { entries.push({ at: r.at, node: feedItem(r, d.now) }); });
    entries.sort(function (a, b) { return b.at - a.at; });
    var items = entries.slice(0, FEED_ITEMS).map(function (e) { return e.node; });
    var body = items.length ? h('ul', { 'class': 'list ov-feed', 'data-testid': 'feed' }, items)
      : h('div', { 'data-testid': 'feed' }, P.ui.emptyState({ heading: 'No requests yet', sentence: 'The newest requests, decisions and follow-ups will appear here.' }));
    var why = items.length ? 'The latest ' + items.length + (items.length === 1 ? ' thing' : ' things') + ' the desk did. Repeats from one lead count as one line.' : 'The latest things the desk did.';
    return P.ui.card({ title: 'Live feed', why: why, testid: 'feed-card', body: body });
  }

  // ---- page
  // each placeholder card carries its own title; only the first one says Loading
  function skelCard(title, rows, label) { return P.ui.card({ title: title, body: P.ui.skeleton(rows, label === undefined ? null : label) }); }
  function skeletonPage() {
    var tiles = h('div', { 'class': 'ov-kpis' });
    for (var i = 0; i < 5; i++) tiles.appendChild(h('span', { 'class': 'skel skel-tile' }));
    return h('div', { 'class': 'ov', 'data-testid': 'overview-loading' }, tiles,
      h('div', { 'class': 'grid' }, h('div', { 'class': 'span-8' }, skelCard('Requests and what the desk did with them', 6, 'Loading…')), h('div', { 'class': 'span-4 ov-col' }, skelCard('Where requests come from', 3), skelCard('Follow-ups', 2))),
      skelCard('Activity', 4), skelCard('Live feed', 6));
  }
  // the cross-check of the extension is a full-width strip above the cards, so the card grid below is the same with or without it
  function proofBlock(d) { var p = extra('overviewProof', d); return p ? h('div', { 'class': 'ov-proof' }, p) : null; }
  function build(d) {
    var specs = tileSpecs(d);
    var tiles = h('div', { 'class': 'ov-kpis ov-kpis-' + specs.length }, specs.map(P.ui.kpiTile));
    var strip = extra('overviewExtras', d);
    return h('div', { 'class': 'ov' }, tiles, strip, proofBlock(d),
      h('div', { 'class': 'grid ov-mid' },
        h('div', { 'class': 'span-5' }, reconcileCard(d)),
        h('div', { 'class': 'span-7' }, sourcesCard(d)),
        h('div', { 'class': 'span-7' }, followupsCard(d)),
        h('div', { 'class': 'span-5' }, activityCard(d))),
      feedCard(d));
  }

  P.page('overview', {
    title: 'Overview',
    purpose: 'What the desk has received, decided and sent.',
    nav: { label: 'Overview', icon: 'overview', order: 1 },
    mount: function (root) {
      var summary = null, failure = null, demoSig = '', alive = true;
      function stale() {
        var on = P.stale(); // the one definition of "the desk is not answering well": the shell's own flag
        root.querySelectorAll('.ov').forEach(function (k) { k.classList.toggle('is-stale', on); });
        // every number, chart and list shows the last good answer: say so on each card (nothing is re-drawn from anything newer)
        root.querySelectorAll('.card-head .why').forEach(function (w) {
          var note = w.querySelector('.ov-asof');
          if (on && !note) w.appendChild(h('span', { 'class': 'ov-asof' }, ' (as of the last update)'));
          if (!on && note) note.remove();
        });
        root.querySelectorAll('.kpi').forEach(function (k) {
          var note = k.querySelector('.kpi-asof');
          if (on && !note) k.appendChild(h('div', { 'class': 'kpi-asof' }, 'as of the last update'));
          if (!on && note) note.remove();
        });
      }
      function render() {
        if (!alive) return;
        P.ui.clear(root);
        if (!summary) {
          root.appendChild(failure ? P.ui.errorState({ what: 'the overview', status: failure.status, retry: function () { failure = null; render(); } }) : skeletonPage());
          return;
        }
        root.appendChild(build(summary));
        stale();
      }
      var stop = P.poll('summary', '/api/ui/summary', 2000, function (d, status) {
        if (status === 200 && d) { summary = d; failure = null; } else { failure = { status: status }; }
        render();
      }, function (err) { failure = { status: 0, err: err }; if (summary) stale(); else render(); });
      var offs = [
        P.on('offline', function () { if (summary) render(); else stale(); }),
        P.on('online', render),
        P.on('link', function () { if (summary) render(); }),
        P.on('personal', function () { if (summary) render(); }),
        P.on('reveal', function () { if (summary) render(); }),
        P.on('demo', function (st) {
          var sig = (ext('overviewTiles') ? '1' : '0') + JSON.stringify(st);
          if (sig === demoSig) return;
          demoSig = sig;
          render();
        })
      ];
      render();
      this.unmount = function () { alive = false; stop(); offs.forEach(function (f) { f(); }); };
    }
  });
})();
