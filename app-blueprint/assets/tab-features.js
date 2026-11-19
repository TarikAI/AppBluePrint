/* app-blueprint map tab: Features (contract 8.4, 8.5). Plain JavaScript, no libraries, no network.
 * Plugs into the map through BP_TABS. Reads D.features, D.featureStates, D.featureGlyph, D.featureLabel and D.levels, and
 * never throws on a plan without features: it draws one line that names the next step. */
BP_TABS.push({
  id: 'features',
  label: function (D) { return 'Features' + (D.features && D.features.length ? ' (' + D.features.length + ')' : ''); },
  render: function (host, D, H) {
    var feats = Array.isArray(D.features) ? D.features : [];
    var states = Array.isArray(D.featureStates) ? D.featureStates : ['draft', 'planned', 'building', 'built', 'verified', 'accepted', 'failed', 'blocked'];
    var glyphs = D.featureGlyph || {};
    var labels = D.featureLabel || {};
    var el = H.el;
    var COLORS = { draft: 'var(--sub)', planned: 'var(--sub)', building: 'var(--s-ondisk)', built: 'var(--s-built)', verified: 'var(--s-verified)',
      accepted: 'var(--s-verified)', failed: 'var(--s-blocked)', blocked: 'var(--s-blocked)' };

    function g(status) {
      var n = el('span', { cls: 'g', title: labels[status] || status, 'aria-label': labels[status] || status, text: glyphs[status] || '?' });
      n.style.color = COLORS[status] || 'var(--sub)';
      return n;
    }
    function pill(text, kind) { return el('span', { cls: 'pill' + (kind ? ' ' + kind : ''), text: text }); }
    function kindOf(status) { return status === 'accepted' || status === 'verified' ? 'ok' : (status === 'failed' || status === 'blocked' ? 'bad' : (status === 'draft' ? 'warn' : '')); }
    function check(c) {
      if (c.stale) return 'stale';
      if (c.result !== 'pass') return c.result || 'unknown';
      var bits = [];
      if (c.resultLevel) bits.push(c.resultLevel);
      if (c.repeat && typeof c.repeat.n === 'number' && typeof c.repeat.passed === 'number') bits.push(c.repeat.passed + '/' + c.repeat.n);
      return 'pass' + (bits.length ? ' (' + bits.join(', ') + ')' : '');
    }
    function evaluation(c) {
      if (c.verify) return c.evaluation || '-';
      if (c.stale) return 'stale';
      if (c.result === 'pass' && c.via === 'evaluation') return 'pass' + (c.resultLevel ? ' (' + c.resultLevel + ')' : '');
      return c.evaluation && c.evaluation !== 'pass' ? c.evaluation : 'unknown';
    }

    if (!feats.length) {
      host.appendChild(el('p', { cls: 'empty', text: 'No features yet. Add features[] with acceptance criteria to blueprint.json, link files to them with files[].features, then run: python .blueprint/bin/blueprint.py render' }));
      return;
    }

    var counts = {};
    states.forEach(function (s) { counts[s] = 0; });
    feats.forEach(function (f) { counts[f.status] = (counts[f.status] || 0) + 1; });
    var live = feats.filter(function (f) { return !f.draft; }).length;
    var tiles = el('div', { cls: 'grid tiles' });
    tiles.appendChild(el('div', { cls: 'card tile' }, [el('div', { cls: 'n', text: (counts.accepted || 0) + ' / ' + live }), el('div', { cls: 'l', text: 'features accepted' })]));
    states.forEach(function (s) {
      if (counts[s]) tiles.appendChild(el('div', { cls: 'card tile' }, [el('div', { cls: 'n', text: String(counts[s]) }), el('div', { cls: 'l' }, [g(s), ' ' + (labels[s] || s)])]));
    });
    host.appendChild(tiles);
    host.appendChild(el('p', { cls: 'meta', text: 'Status is derived from the files and the checks on every run. Only an independent evaluation (blueprint.py accept) makes a feature accepted; verified means the files and the commands pass, nothing more.' }));

    var filters = el('div', { cls: 'filters' });
    var sel = el('select', { id: 'f-feat-status', 'aria-label': 'Filter features by status' });
    sel.appendChild(el('option', { value: '', text: 'All statuses' }));
    states.forEach(function (s) { if (counts[s]) sel.appendChild(el('option', { value: s, text: labels[s] || s })); });
    filters.appendChild(el('label', { cls: 'meta', 'for': 'f-feat-status', text: 'Status' }));
    filters.appendChild(sel);
    host.appendChild(filters);
    var list = el('div', { cls: 'grid' });
    host.appendChild(list);

    function card(f) {
      var d = el('details', { cls: 'card', id: 'feat-' + f.id });
      var sum = el('summary');
      sum.appendChild(g(f.status));
      sum.appendChild(el('code', { text: ' ' + f.id + ' ' }));
      sum.appendChild(document.createTextNode(f.title + ' '));
      sum.appendChild(pill(f.statusText || f.status, kindOf(f.status)));
      d.appendChild(sum);
      if (f.statement) d.appendChild(el('p', { text: f.statement }));
      d.appendChild(el('p', { cls: 'meta', text: [f.kind, f.milestone || 'no milestone', f.owner, f.level].join(' · ') }));
      if (f.dependsOn && f.dependsOn.length) d.appendChild(el('p', { cls: 'meta', text: 'Depends on: ' + f.dependsOn.join(', ') }));
      if (f.blockedBy && f.blockedBy.length) d.appendChild(el('p', null, [pill('blocked by ' + f.blockedBy.join(', '), 'bad')]));
      if (f.source && f.source.study) d.appendChild(el('p', { cls: 'meta', text: 'Source: study ' + f.source.study }));
      d.appendChild(el('h3', { text: 'Files (' + (f.items || []).length + ')' }));
      if (f.items && f.items.length) {
        var ul = el('ul', { cls: 'plain' });
        f.items.forEach(function (it) {
          var row = [typeof H.glyph === 'function' && D.glyph && D.glyph[it.status] ? H.glyph(it.status) : it.status, ' ', el('code', { text: it.path })];
          if (it.from && it.from !== 'file') row.push(el('span', { cls: 'meta', text: ' (' + it.from.replace('pattern:', 'pattern ') + ')' }));
          ul.appendChild(el('li', null, row));
        });
        d.appendChild(ul);
      } else d.appendChild(el('p', { cls: 'empty', text: 'No file serves this feature. Add its id to files[].features.' }));
      d.appendChild(el('h3', { text: 'Criteria (' + (f.criteria || []).length + ')' }));
      if (f.criteria && f.criteria.length) {
        var wrap = el('div', { cls: 'table-wrap' }), t = el('table');
        t.appendChild(el('thead', null, [el('tr', null, ['ID', 'Given', 'When', 'Then', 'Must not', 'Level', 'Check', 'Evaluation'].map(function (h) { return el('th', { text: h }); }))]));
        var tb = el('tbody');
        f.criteria.forEach(function (c) {
          tb.appendChild(el('tr', null, [c.id, c.scenario, c.action, c.expected, c.mustNot || '', c.level, c.verify ? check(c) : '-', evaluation(c)].map(function (v) { return el('td', { text: String(v) }); })));
        });
        t.appendChild(tb); wrap.appendChild(t); d.appendChild(wrap);
      } else d.appendChild(el('p', { cls: 'empty', text: 'No acceptance criteria yet. Add criteria[] with scenario, action and expected.' }));
      var ev = f.evaluation;
      d.appendChild(el('p', { cls: 'meta', text: 'Latest evaluation: ' + (ev ? [ev.by, ev.mode, ev.verdict, String(ev.at || '').slice(0, 10)].join(' · ') : 'none') }));
      return d;
    }
    function draw() {
      list.textContent = '';
      var want = sel.value;
      var shown = feats.filter(function (f) { return !want || f.status === want; });
      shown.filter(function (f) { return !f.draft; }).concat(shown.filter(function (f) { return f.draft; })).forEach(function (f) { list.appendChild(card(f)); });
      if (!shown.length) list.appendChild(el('p', { cls: 'empty', text: 'No feature has this status.' }));
    }
    sel.addEventListener('change', draw);
    draw();
  }
});
