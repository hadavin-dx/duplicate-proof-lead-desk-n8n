/* Request log: every request the desk received, newest first, read from the desk’s records. No email, phone or message appears here. */
(function () {
  'use strict';
  var P = window.Portal;
  var h = P.h;
  var PAGE = 100;
  var RESULTS = ['new', 'duplicate', 'review', 'open'];
  var TONE = { 'new': 'ok', duplicate: 'calm', review: 'warn', open: 'pending' };

  function lbl(group, code) { return (P.labels[group] && P.labels[group][code]) || P.words(code); }
  function two(n) { return n < 10 ? '0' + n : String(n); }
  // the real time the request arrived (shown in UTC); the relative time and the full date are in the tooltip
  function clockTime(epoch, now) {
    var d = new Date(epoch * 1000);
    var ref = now === undefined ? Math.floor(Date.now() / 1000) : now;
    var el = h('time', { 'class': 'num', title: d.toLocaleString() + ' (' + P.ui.relTime(epoch, now).textContent + ')' }, P.ui.utc(epoch, true).slice(11));
    el.setAttribute('datetime', d.toISOString());
    return ref - epoch > 86400 ? h('span', null, el, ' ', h('span', { 'class': 'faint' }, d.toLocaleDateString())) : h('span', { 'class': 'log-when' }, el, h('span', { 'class': 'faint' }, P.ui.relTime(epoch, now)));
  }
  function resultPill(code, newDetails) {
    return h('span', { 'class': 'pill pill-' + (TONE[code] || 'neutral'), 'data-status': code, title: { review: 'The desk would not decide this request alone: a person has to look', open: 'The desk has stored this request but has not decided it yet' }[code] || (newDetails ? 'A repeat of an earlier request that carried new details. The first request stands.' : null) }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), newDetails ? 'Repeat, new details' : lbl('result', code));
  }
  function copy(id, btn) {
    var done = function () { P.ui.toast('Request number copied', 'ok'); };
    try {
      navigator.clipboard.writeText(id).then(done, function () { P.ui.toast('Could not copy. The full request number is in the tooltip.', 'warn'); });
    } catch (e) { P.ui.toast('Could not copy. The full request number is in the tooltip.', 'warn'); }
    btn.blur();
  }
  function requestCell(r) {
    if (r._n) {
      var tog = h('button', { 'class': 'btn btn-sm', type: 'button', 'data-testid': 'log-group-toggle' }, 'Show all ' + r._n);
      tog.addEventListener('click', function (e) { e.stopPropagation(); r._open(r._key); });
      return h('div', { 'class': 'log-req' }, h('span', { 'class': 'muted log-grp', title: 'Repeats from the same lead, latest first' }, r._n + ' repeats, same lead'), tog);
    }
    var btn = h('button', { 'class': 'icon-btn icon-btn-sm', type: 'button', 'aria-label': 'Copy full request number ' + P.shownRef(r.request_id), title: 'Copy full request number' }, P.icon('copy'));
    btn.addEventListener('click', function (e) { e.stopPropagation(); copy(P.shownRef(r.request_id), btn); }); // while addresses are masked the copy is masked too
    return h('div', { 'class': 'log-req' }, h('span', { 'class': 'mono chip-id', title: 'Full request number ' + P.shownRef(r.request_id) }, P.ui.refNode(r.request_id, '')), btn);
  }

  P.page('log', {
    title: 'Request log',
    purpose: 'Every request the desk received. A request sent again is recognised as a repeat and is not added a second time.',
    nav: { label: 'Request log', icon: 'log', order: 4 },
    mount: function (root) {
      var first = null, extra = [], cursor = null, filter = '', failure = null, loading = false, lastSig = '', alive = true, opened = {};
      var select = h('select', { 'class': 'select', 'aria-label': 'Filter by result', 'data-testid': 'log-result-filter' },
        h('option', { value: '' }, 'All results'), RESULTS.map(function (c) { return h('option', { value: c }, lbl('result', c)); }));
      var count = h('span', { 'class': 'muted leads-count' }, '');
      var bar = h('div', { 'class': 'toolbar' }, select, h('span', { 'class': 'spacer' }), count);
      var note = h('p', { 'class': 'note log-note', 'data-testid': 'log-note' }, P.icon('info'),
        h('span', null, 'Each request with the latest follow-up status of its lead. A repeat is the same request sent again: it makes no second lead and no second email. A dash (–) means there is no lead, so no follow-up. Times are UTC. While “Hide names and email addresses” is on, no name, email address, phone number or message is shown in this table, and every request number is dots until you press its eye button.'));
      var out = h('div', { 'class': 'log-out' }, P.ui.skeleton(6));
      root.appendChild(note);
      root.appendChild(bar);
      root.appendChild(out);

      // three or more repeats in a row from one lead become one line; the button shows them all
      function groupRepeats(list) {
        var out = [], i = 0;
        while (i < list.length) {
          var r = list[i], j = i + 1;
          if (r.result === 'duplicate' && r.lead_name) {
            while (j < list.length && list[j].result === 'duplicate' && list[j].lead_name === r.lead_name && list[j].door === r.door) j++;
          }
          var key = r.request_id + '|' + r.received_at;
          if (j - i >= 3 && !opened[key]) {
            out.push(Object.assign({}, r, { _n: j - i, _key: key, _open: function (k) { opened[k] = 1; render(true); } }));
          } else out.push.apply(out, list.slice(i, j));
          i = j;
        }
        return out;
      }
      function rows() {
        var seen = {}, all = [];
        (first ? first.items : []).concat(extra).forEach(function (r) { var k = r.request_id + '|' + r.received_at; if (!seen[k]) { seen[k] = 1; all.push(r); } });
        return all;
      }
      function render(force) {
        if (!alive) return;
        if (failure && !first) {
          lastSig = '';
          P.ui.clear(out).appendChild(P.ui.errorState({ what: 'the request log', status: failure.status, retry: function () { failure = null; load(); } }));
          return;
        }
        if (!first) return;
        var all = rows();
        var shown = filter ? all.filter(function (r) { return r.result === filter; }) : all;
        var sig = JSON.stringify([all, filter, cursor, loading, opened, P.state.summary ? P.state.summary.now : 0]);
        if (!force && sig === lastSig) return;
        lastSig = sig;
        var grouped = groupRepeats(shown).length;
        count.textContent = first.total + (first.total === 1 ? ' request' : ' requests') + (filter ? ', ' + shown.length + ' shown below' : '') + (grouped < shown.length ? ' (' + grouped + ' rows, repeats grouped)' : '');
        var table = P.ui.table({
          testid: 'log-table', rowTestid: 'log-row',
          rowAttrs: function (r) { return { 'data-request-id': P.shownRef(r.request_id) }; },
          columns: [
            { key: 'received_at', label: 'Received', render: function (r) { return clockTime(r.received_at, P.state.summary ? P.state.summary.now : undefined); } },
            { key: 'request_id', label: 'Request number', render: requestCell },
            { key: 'door', label: 'Source', render: function (r) { return lbl('source', r.door); } },
            { key: 'result', label: 'Result', render: function (r) { return resultPill(r.result, r.new_details); } },
            { key: 'lead_name', label: 'Lead', render: function (r) { return r.lead_name ? h('span', { 'class': 'lead-name' }, P.ui.name(r.lead_name)) : h('span', { 'class': 'faint', title: 'No lead: this request was not stored as a lead' }, '–'); } },
            { key: 'followup_state', label: 'Follow-up', render: function (r) { return r.result === 'duplicate' ? h('span', { 'class': 'pill pill-neutral' }, h('span', { 'class': 'dot', 'aria-hidden': 'true' }), 'No second email') : r.followup_state ? P.ui.followupChips(r.state_view, r.followup_state) : h('span', { 'class': 'faint', title: 'No follow-up: there is no lead' }, '–'); } }
          ],
          rows: groupRepeats(shown),
          empty: all.length ? { heading: 'No requests with this result', sentence: 'Pick another result, or load older requests.' } : { heading: 'No requests yet', sentence: 'When a request reaches the desk it is listed here, newest first.' }
        });
        P.ui.clear(out).appendChild(table);
        if (cursor) {
          var more = h('button', { 'class': 'btn btn-sm', type: 'button', 'data-testid': 'log-more' }, loading ? 'Loading…' : 'Load more');
          more.disabled = loading;
          more.addEventListener('click', loadMore);
          out.appendChild(h('div', { 'class': 'pager' }, h('span', { 'class': 'muted' }, 'Showing ' + all.length + ' of ' + first.total), h('span', { 'class': 'spacer' }), more));
        }
      }
      function onFirst(d, status) {
        if (status === 200 && d && Array.isArray(d.items)) {
          first = d; failure = null;
          if (!extra.length) cursor = d.next;
        } else { failure = { status: status }; }
        render(false);
      }
      function loadMore() {
        if (loading || !cursor) return;
        loading = true; render(true);
        P.api('/api/ui/log?limit=' + PAGE + '&before=' + encodeURIComponent(cursor)).then(function (r) {
          loading = false;
          if (r.status === 200 && r.data && Array.isArray(r.data.items)) { extra = extra.concat(r.data.items); cursor = r.data.next; }
          else P.ui.toast('Could not load older requests. Try again.', 'warn');
          render(true);
        }, function () { loading = false; P.ui.toast('The desk is not answering. Try again in a moment.', 'warn'); render(true); });
      }
      var stop = null;
      function load() {
        if (stop) stop();
        first = null; lastSig = '';
        P.ui.clear(out).appendChild(P.ui.skeleton(6));
        stop = P.poll('log', '/api/ui/log?limit=' + PAGE, 5000, onFirst, function () { failure = { status: 0 }; render(false); });
      }
      select.addEventListener('change', function () { filter = select.value; render(true); });
      var offS = P.on('summary', function () { render(false); });
      var off = P.on('offline', function () { if (!first) { failure = { status: 0 }; render(false); } });
      load();
      var offPersonal = P.on('personal', function () { render(true); }), offReveal = P.on('reveal', function () { render(true); }); // request numbers with an address follow the switch
      this.unmount = function () { alive = false; if (stop) stop(); off(); offS(); offPersonal(); offReveal(); };
    }
  });
})();
