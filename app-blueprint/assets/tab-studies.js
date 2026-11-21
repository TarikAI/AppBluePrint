/* Studies tab of map.html (contract 8.4, 8.5): each study of .blueprint/studies, what it read, how much of its
   evidence it holds and how far its behaviours are reviewed. Freshness is not shown here: it reads and hashes the
   targets, so it is checked on demand by the evidence stale command. Plain JavaScript, no dependencies, no
   network. Reads D.studies only: [{id, kind, target: {name, own}, authorization: {statement}, tool, at, coverage,
   counts, behaviours: {candidate, reviewed, rejected} or null}]. Every value is
   put on the page as text; a value of an unexpected type is shown as empty, never thrown on. */
(function () {
  'use strict';
  if (typeof BP_TABS === 'undefined') return;

  /* lookups by study text use Map, never a plain object: a kind such as __proto__ is just text */
  var KIND = new Map([['source', 'repository'], ['web', 'web'], ['desktop', 'desktop app'], ['mobile', 'mobile app'],
    ['data', 'data files'], ['compare', 'comparison'], ['capture', 'recorded flow'], ['import', 'import'], ['notes', 'notes']]);
  var EMPTY = 'No studies yet. A study reads a repository, an app or data files (only what you may analyse) and ' +
    'records what it shows as evidence: run the study command of this tool (for example study source . on this ' +
    'project), then render again.';

  function list(v) { return Array.isArray(v) ? v : []; }
  function str(v) { return typeof v === 'string' ? v : ''; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
  function num(v) { return typeof v === 'number' && isFinite(v) && v >= 0 ? Math.floor(v) : 0; }
  function entries(D) { return list(D && D.studies).filter(function (s) { return s && typeof s === 'object' && !Array.isArray(s); }); }
  function pill(H, text, kind) { return H.el('span', { cls: 'pill' + (kind ? ' ' + kind : ''), text: text }); }

  function coverageText(c) {
    var skipped = new Map(Object.entries(obj(c.skipped))), parts = [];
    Array.from(skipped.keys()).sort().forEach(function (k) { if (num(skipped.get(k))) parts.push(num(skipped.get(k)) + ' ' + k); });
    return num(c.filesScanned) + ' of ' + num(c.filesSeen) + ' files read' +
      (num(c.filesSkipped) ? ' · ' + num(c.filesSkipped) + ' skipped' + (parts.length ? ' (' + parts.join(', ') + ')' : '') : '');
  }

  function row(H, s) {
    var t = obj(s.target), c = obj(s.coverage), n = obj(s.counts), b = obj(s.behaviours), counted = s.behaviours && typeof s.behaviours === 'object';
    var own = t.own === true;
    var kind = str(s.kind), label = KIND.get(kind);
    var target = [H.el('div', { text: str(t.name) || '(no name)' }),
      own ? pill(H, 'own', 'ok') : pill(H, 'not own: not committed', 'warn')];
    var cov = [H.el('span', { text: coverageText(c) })];
    if (c.truncated === true) cov.push(' ', pill(H, 'stopped by ' + (str(c.stoppedBy) || 'a limit'), 'warn'));
    list(c.missing).filter(function (m) { return typeof m === 'string'; }).forEach(function (m) {
      cov.push(H.el('div', { cls: 'meta', text: 'missing: ' + m }));
    });
    var total = num(b.candidate) + num(b.reviewed) + num(b.rejected);
    var evidence = num(n.evidence) + ' records' + (num(n.secrets) ? ' · ' + num(n.secrets) + ' secrets redacted' : '');
    return H.el('tr', null, [
      H.el('td', null, [H.el('code', { text: str(s.id) || '(no id)' }), H.el('div', { cls: 'meta', text: label || kind })]),
      H.el('td', null, target),
      H.el('td', { text: evidence }),
      H.el('td', { text: !counted ? 'not counted here (study show)' : total ? num(b.reviewed) + ' of ' + total + ' reviewed' + (num(b.rejected) ? ' · ' + num(b.rejected) + ' rejected' : '') : 'none yet' }),
      H.el('td', null, cov),
      H.el('td', null, [H.el('div', { text: str(obj(s.authorization).statement) }),
        H.el('div', { cls: 'meta', text: (str(s.at) ? str(s.at).slice(0, 10) : '') + (str(s.tool) ? ' · tool ' + str(s.tool) : '') })])
    ]);
  }

  BP_TABS.push({
    id: 'studies',
    label: function (D) {
      var n = entries(D).length;
      return 'Studies' + (n ? ' (' + n + ')' : '');
    },
    render: function (host, D, H) {
      var all = entries(D);
      if (!all.length) {
        host.appendChild(H.el('p', { cls: 'empty', text: EMPTY }));
        return;
      }
      var records = 0, reviewed = 0, behaviours = 0;
      all.forEach(function (s) {
        var b = obj(s.behaviours);
        records += num(obj(s.counts).evidence);
        reviewed += num(b.reviewed);
        behaviours += num(b.candidate) + num(b.reviewed) + num(b.rejected);
      });
      var tiles = H.el('div', { cls: 'grid tiles' });
      [[all.length, 'studies', false], [records, 'evidence records', false], [reviewed + ' / ' + behaviours, 'behaviours reviewed', false]].forEach(function (t) {
        tiles.appendChild(H.el('div', { cls: 'card tile' + (t[2] ? ' warn' : '') }, [H.el('div', { cls: 'n', text: String(t[0]) }), H.el('div', { cls: 'l', text: t[1] })]));
      });
      host.appendChild(tiles);
      host.appendChild(H.el('p', { cls: 'meta', text: 'Whether evidence still matches its target is checked on demand: ' +
        'run the evidence stale command of this tool. A study of software that is not your own is kept out of git until ' +
        'you remove its .gitignore.' }));
      var head = H.el('tr', null, ['Study', 'Target', 'Evidence', 'Behaviours', 'Coverage', 'Authorization'].map(function (h) {
        return H.el('th', { scope: 'col', text: h });
      }));
      var body = H.el('tbody');
      all.slice().sort(function (a, b) { return str(a.id) < str(b.id) ? -1 : (str(a.id) > str(b.id) ? 1 : 0); })
        .forEach(function (s) { body.appendChild(row(H, s)); });
      host.appendChild(H.el('div', { cls: 'table-wrap' }, [H.el('table', null, [H.el('thead', null, [head]), body])]));
    }
  });
})();
