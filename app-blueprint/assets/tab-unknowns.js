/* Unknowns tab of map.html (contract 8.4, 8.5): open questions, what each one blocks and the experiment that ends it.
   Plain JavaScript, no dependencies, no network. Reads D.unknowns only: [{id, question, severity, status, blocks,
   evidenceFor, evidenceAgainst, experiment, decision, blocking}]. A plan without unknowns draws an empty state that
   names the next step; a malformed entry is skipped or shown with what is there, never thrown on. */
(function () {
  'use strict';
  if (typeof BP_TABS === 'undefined') return;

  /* lookups by plan text use Map, never a plain object: an id or status such as __proto__ or constructor is just text */
  var RANK = new Map([['critical', 0], ['high', 1], ['medium', 2], ['low', 3]]);
  var CLOSED = new Map([['resolved', true], ['dropped', true]]);
  function rank(u) { var r = RANK.get(str(u.severity)); return r === undefined ? 4 : r; }

  function list(v) { return Array.isArray(v) ? v : []; }
  function str(v) { return typeof v === 'string' ? v : ''; }
  function entries(D) {
    return list(D && D.unknowns).filter(function (u) { return u && typeof u === 'object' && !Array.isArray(u); });
  }
  function isLive(u) { return CLOSED.get(str(u.status)) !== true; }

  /* blocking first, then open before closed, then the more severe, then the plan's own order */
  function ordered(items) {
    return items.map(function (u, i) { return { u: u, i: i }; }).sort(function (a, b) {
      var ka = [a.u.blocking === true ? 0 : 1, isLive(a.u) ? 0 : 1, rank(a.u), a.i];
      var kb = [b.u.blocking === true ? 0 : 1, isLive(b.u) ? 0 : 1, rank(b.u), b.i];
      for (var k = 0; k < 4; k++) if (ka[k] !== kb[k]) return ka[k] - kb[k];
      return 0;
    }).map(function (x) { return x.u; });
  }

  function pill(H, text, kind) { return H.el('span', { cls: 'pill' + (kind ? ' ' + kind : ''), text: text }); }

  function row(H, u) {
    var sev = str(u.severity), ex = u.experiment && typeof u.experiment === 'object' ? u.experiment : null;
    var blocks = list(u.blocks).filter(function (b) { return typeof b === 'string'; });
    var sevKind = sev === 'critical' || sev === 'high' ? 'bad' : (sev === 'medium' ? 'warn' : (sev === 'low' ? 'ok' : 'warn'));
    var idCell = [H.el('code', { text: str(u.id) || '(no id)' }), H.el('div', { text: str(u.question) || 'No question written.' })];
    var nothing = new Map(list(u.unresolved).filter(function (b) { return typeof b === 'string'; }).map(function (b) { return [b, true]; }));
    var blockCell = blocks.map(function (b) { return H.el('code', { cls: 'chip', text: b + (nothing.get(b) === true ? ' (names nothing)' : '') }); });
    if (u.blocking === true) blockCell.unshift(pill(H, 'blocks work', 'bad'), ' ');
    if (!blockCell.length) blockCell = [H.el('span', { cls: 'meta', text: 'nothing' })];
    var expCell = [H.el('span', { cls: 'meta', text: isLive(u) ? 'No experiment yet.' : '' })];
    if (ex) {
      expCell = [pill(H, str(ex.timebox) || 'no timebox'), ' ', H.el('span', { text: 'ends when: ' + (str(ex.exit) || 'not written') })];
      if (str(ex.plan)) expCell.push(H.el('div', { cls: 'meta', text: str(ex.plan) }));
      if (str(ex.owner)) expCell.push(H.el('div', { cls: 'meta', text: 'owner: ' + str(ex.owner) }));
    }
    var evidence = list(u.evidenceFor).length + ' for · ' + list(u.evidenceAgainst).length + ' against';
    if (str(u.decision)) evidence += ' · decided in ' + str(u.decision);
    return H.el('tr', null, [
      H.el('td', null, idCell),
      H.el('td', null, [pill(H, sev || 'no severity', sevKind)]),
      H.el('td', { text: str(u.status) || 'no status' }),
      H.el('td', null, blockCell),
      H.el('td', null, expCell),
      H.el('td', { cls: 'meta', text: evidence })
    ]);
  }

  BP_TABS.push({
    id: 'unknowns',
    label: function (D) {
      var n = entries(D).filter(isLive).length;
      return 'Unknowns' + (n ? ' (' + n + ')' : '');
    },
    render: function (host, D, H) {
      var all = entries(D);
      if (!all.length) {
        var next = str(D && D.unknownsNext) || 'run the render command of this tool';
        host.appendChild(H.el('p', { cls: 'empty', text: 'No unknowns yet. Add each open question of the plan to unknowns[] in ' +
          '.blueprint/blueprint.json with a severity, what it blocks and an experiment. Next: ' + next }));
        return;
      }
      var live = all.filter(isLive).length, hard = all.filter(function (u) { return u.blocking === true; }).length;
      var tiles = H.el('div', { cls: 'grid tiles' });
      [[live, 'open or investigating', false], [hard, 'blocking work', hard > 0], [all.length - live, 'resolved or dropped', false]].forEach(function (t) {
        tiles.appendChild(H.el('div', { cls: 'card tile' + (t[2] ? ' warn' : '') }, [H.el('div', { cls: 'n', text: String(t[0]) }), H.el('div', { cls: 'l', text: t[1] })]));
      });
      host.appendChild(tiles);
      host.appendChild(H.el('p', { cls: 'meta', text: 'An unknown that is open or investigating and high or critical blocks the phases, milestones ' +
        'and features it names: status --next skips them and claim refuses their files until it is resolved or dropped.' }));

      var showClosed = H.el('input', { type: 'checkbox', id: 'unknowns-closed' });
      var bar = H.el('div', { cls: 'filters' }, [showClosed, H.el('label', { cls: 'meta', 'for': 'unknowns-closed', text: 'Show resolved and dropped' })]);
      var body = H.el('tbody');
      var head = H.el('tr', null, ['Unknown', 'Severity', 'Status', 'Blocks', 'Experiment', 'Evidence'].map(function (h) { return H.el('th', { scope: 'col', text: h }); }));
      host.appendChild(bar);
      host.appendChild(H.el('div', { cls: 'table-wrap' }, [H.el('table', null, [H.el('thead', null, [head]), body])]));

      function draw() {
        while (body.firstChild) body.removeChild(body.firstChild);
        var shown = ordered(all).filter(function (u) { return showClosed.checked || isLive(u); });
        shown.forEach(function (u) { body.appendChild(row(H, u)); });
        if (!shown.length) body.appendChild(H.el('tr', null, [H.el('td', { colspan: '6', cls: 'empty', text: 'Nothing is open. Tick the box to see the resolved and dropped ones.' })]));
      }
      showClosed.addEventListener('change', draw);
      draw();
    }
  });
})();
