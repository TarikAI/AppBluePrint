/* Roadmap tab of map.html (contract 8.4). Plain JavaScript, no dependencies, no network.
   Reads D.milestones, D.tasks, D.features and D.unknowns (contract 8.5); every one may be missing or empty. */
(function () {
  'use strict';
  var TASK_GLYPH = { todo: '○', doing: '◑', done: '✔', blocked: '▲' };

  function arr(v) { return Array.isArray(v) ? v : []; }
  function objs(v) { return arr(v).filter(function (x) { return x && typeof x === 'object'; }); }
  function str(v) { return typeof v === 'string' ? v : ''; }

  function makeEl(tag, a, kids) {
    var n = document.createElement(tag);
    if (a) for (var k in a) { if (k === 'text') n.textContent = a[k]; else if (k === 'cls') n.className = a[k]; else n.setAttribute(k, a[k]); }
    arr(kids).forEach(function (c) { if (c != null) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return n;
  }

  function featureText(f) {
    var s = str(f.status) || 'planned', n = f.awaitingEvaluation, total = arr(f.criteria).length;
    if (s === 'verified' && typeof n === 'number' && n > 0) {
      var verb = n === 1 ? 'criterion awaits' : 'criteria await';
      return s + ', ' + (total && n === total && n > 1 ? n + ' of ' + total : n) + ' ' + verb + ' evaluation';
    }
    return s;
  }

  function passing(f) {
    var c = objs(f.criteria), ok = 0;
    c.forEach(function (x) { if (x.result === 'pass') ok++; });
    return ok + ' of ' + c.length;
  }

  function render(host, D, H) {
    var el = H && typeof H.el === 'function' ? H.el : makeEl;
    var milestones = objs(D.milestones), tasks = objs(D.tasks), features = objs(D.features), unknowns = objs(D.unknowns);
    var glyphs = D.featureGlyph && typeof D.featureGlyph === 'object' ? D.featureGlyph : {};
    var featById = {}, taskById = {};
    features.forEach(function (f) { if (f && typeof f.id === 'string') featById[f.id] = f; });
    tasks.forEach(function (t) { if (t && typeof t.id === 'string') taskById[t.id] = t; });

    if (!milestones.length && !tasks.length) {
      host.appendChild(el('p', { cls: 'empty', text: 'No milestones or tasks yet. Add milestones[] and tasks[] to .blueprint/blueprint.json (see references/schema.md), then run: blueprint.py validate, then blueprint.py render.' }));
      return;
    }

    function pill(text, kind) { return el('span', { cls: 'pill' + (kind ? ' ' + kind : ''), text: text }); }

    function bar(done, total) {
      var b = el('div', { cls: 'stack', role: 'img', 'aria-label': done + ' of ' + total + ' done' });
      if (total > 0 && done > 0) b.appendChild(el('span', { style: 'width:' + Math.min(100, 100 * done / total) + '%;background:var(--s-verified)' }));
      return b;
    }

    function table(head, rows) {
      var tr = el('tr', null, head.map(function (h) { return el('th', { text: h }); }));
      var body = rows.map(function (r) { return el('tr', null, r.map(function (c) { return el('td', null, [c]); })); });
      return el('div', { cls: 'table-wrap' }, [el('table', null, [el('thead', null, [tr]), el('tbody', null, body)])]);
    }

    function featureTable(list) {
      return table(['Feature', 'Status', 'Criteria passing', 'Owner'], list.map(function (f) {
        var g = glyphs[f.status];
        return [el('span', null, [el('code', { text: f.id }), ' ' + str(f.title)]),
                el('span', null, [g ? el('span', { cls: 'g', text: g }) : null, ' ' + featureText(f)]),
                el('span', { text: passing(f) }), el('span', { text: str(f.owner) })];
      }));
    }

    function taskTable(list) {
      return table(['Task', 'Owner', 'Status'], list.map(function (t) {
        var st = TASK_GLYPH[t.status] ? t.status : 'todo';
        return [el('span', null, [el('code', { text: t.id }), ' ' + str(t.title), t.ownerAction ? pill('you', 'warn') : null]),
                el('span', { text: str(t.owner) }),
                el('span', null, [el('span', { cls: 'g st-' + (st === 'doing' ? 'ondisk' : st === 'done' ? 'verified' : st === 'blocked' ? 'blocked' : 'planned'), text: TASK_GLYPH[st] }),
                                  ' ' + st + (st === 'blocked' && t.note ? ' (' + str(t.note) + ')' : '')])];
      }));
    }

    function counted(list) { return list.filter(function (f) { return f && !f.draft && f.status !== 'draft'; }); }

    // summary tiles
    var allFeatures = counted(features), accepted = allFeatures.filter(function (f) { return f.status === 'accepted'; }).length;
    var doneTasks = tasks.filter(function (t) { return t.status === 'done'; }).length;
    var doneMs = milestones.filter(function (m) { return m.done; }).length;
    var blocking = unknowns.filter(function (u) { return u && u.blocking; }).length;
    var tiles = el('div', { cls: 'grid tiles' });
    [[doneMs + ' / ' + milestones.length, 'milestones done'], [accepted + ' / ' + allFeatures.length, 'features accepted'],
     [doneTasks + ' / ' + tasks.length, 'tasks done'], [String(blocking), 'open blocking questions', blocking > 0]].forEach(function (t) {
      tiles.appendChild(el('div', { cls: 'card tile' + (t[2] ? ' warn' : '') }, [el('div', { cls: 'n', text: String(t[0]) }), el('div', { cls: 'l', text: t[1] })]));
    });
    host.appendChild(tiles);

    // one card per milestone, in plan order
    milestones.forEach(function (m) {
      var p = m.progress && typeof m.progress === 'object' ? m.progress : {}, head = [el('h3', { text: m.id + ' ' + str(m.name) })];
      if (m.due) head.push(el('span', { cls: 'meta', text: 'due ' + m.due }));
      if (m.done) head.push(pill('done', 'ok'));
      if (m.overdue) head.push(pill('overdue', 'bad'));
      var card = el('div', { cls: 'card' }, [el('div', { cls: 'brand' }, head)]);
      var total = (p.features || 0) + (p.tasks || 0), done = (p.accepted || 0) + (p.done || 0);
      card.appendChild(bar(done, total));
      var parts = [];
      if (p.features) parts.push((p.accepted || 0) + ' of ' + p.features + ' features accepted');
      if (p.tasks) parts.push((p.done || 0) + ' of ' + p.tasks + ' tasks done');
      card.appendChild(el('p', { cls: 'meta', text: parts.join(' · ') || 'nothing in it yet' }));
      if (m.goal) card.appendChild(el('p', { text: 'Goal: ' + m.goal }));
      if (m.exit) card.appendChild(el('p', { text: 'Exit: ' + m.exit }));
      var fl = arr(m.features).map(function (id) { return featById[id] || { id: id, title: '', status: 'planned', criteria: [] }; });
      var tl = arr(m.tasks).map(function (id) { return taskById[id]; }).filter(Boolean);
      if (fl.length) card.appendChild(featureTable(fl));
      if (tl.length) card.appendChild(taskTable(tl));
      var blockers = arr(m.blockedBy);
      fl.forEach(function (f) { arr(f.blockedBy).forEach(function (u) { if (blockers.indexOf(u) < 0) blockers.push(u); }); });
      if (blockers.length) card.appendChild(el('p', null, ['Blocked by: '].concat(blockers.map(function (u) { return pill(String(u), 'warn'); }))));
      host.appendChild(card);
    });

    // features and tasks that belong to no milestone
    var known = {}; milestones.forEach(function (m) { known[m.id] = true; });
    var looseF = features.filter(function (f) { return f && !known[f.milestone]; });
    var looseT = tasks.filter(function (t) { return t && !known[t.milestone]; });
    if (looseF.length || looseT.length) {
      var loose = el('div', { cls: 'card' }, [el('h3', { text: 'Not in a milestone' })]);
      if (looseF.length) loose.appendChild(featureTable(looseF));
      if (looseT.length) loose.appendChild(taskTable(looseT));
      host.appendChild(loose);
    }

    // open questions
    var open = unknowns.filter(function (u) { return u && u.status !== 'resolved' && u.status !== 'dropped'; });
    if (open.length) {
      host.appendChild(el('div', { cls: 'card' }, [el('h3', { text: 'Open questions' }), table(['Unknown', 'Severity', 'Status', 'Blocks', 'Experiment', 'Evidence'], open.map(function (u) {
        var x = u.experiment && typeof u.experiment === 'object' ? u.experiment : {};
        return [el('span', null, [el('code', { text: str(u.id) }), ' ' + str(u.question)]), el('span', { text: str(u.severity) }), el('span', { text: str(u.status) }),
                el('span', { text: arr(u.blocks).join(', ') }),
                el('span', { text: [str(x.timebox), x.exit ? 'exit: ' + str(x.exit) : ''].filter(Boolean).join(', ') }),
                el('span', { text: 'for ' + arr(u.evidenceFor).length + ' / against ' + arr(u.evidenceAgainst).length })];
      }))]));
    }

    // timeline: milestones with a due date, in date order
    var dated = milestones.filter(function (m) { return typeof m.due === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(m.due); })
      .map(function (m, i) { return { m: m, i: i }; })
      .sort(function (a, b) { return a.m.due < b.m.due ? -1 : a.m.due > b.m.due ? 1 : a.i - b.i; });
    if (dated.length) {
      host.appendChild(el('div', { cls: 'card' }, [el('h3', { text: 'Timeline' }), table(['Due', 'Milestone', 'Status'], dated.map(function (d) {
        return [el('span', { text: d.m.due }), el('span', { text: d.m.id + ' ' + str(d.m.name) }), el('span', { text: d.m.done ? 'done' : d.m.overdue ? 'overdue' : 'open' })];
      }))]));
    }
  }

  var tabs = typeof BP_TABS !== 'undefined' ? BP_TABS : null;
  if (tabs && typeof tabs.push === 'function') {
    tabs.push({ id: 'roadmap', label: function (D) {
      var n = arr(D && D.milestones).filter(function (m) { return m && m.overdue; }).length;
      return 'Roadmap' + (n ? ' (' + n + ' overdue)' : '');
    }, render: render });
  }
})();
