/* app-blueprint mind map renderer. Self-contained, no libraries.
 *
 * mindMap(host, root, opts) draws a two-sided mind map into `host` (an element) and returns
 * { expandAll, collapseAll, zoom(f), fit() }.
 *
 * root: { label, title?, children?: [node] }  node: { label, title?, children?, collapsed?, color?, dot? }
 * opts: {
 *   palette:    CSS colours for the first-level branches, used in order,
 *   collapseAt: depth from which nodes with children start collapsed (default: none),
 *   dotColor:   function (node) -> CSS colour for a leaf's dot (default: node.dot or its branch colour),
 *   onSelect:   function (node) called when a node is clicked or activated with Enter/Space
 * }
 * Clicking a node with children also expands or collapses it. Colours should be CSS variables so
 * the map follows the page's light and dark themes.
 */
function mindMap(host, root, opts) {
  opts = opts || {};
  var NS = 'http://www.w3.org/2000/svg', ROW = 24, GX = 30, PAD = 18, GROUP_GAP = 10;
  var palette = opts.palette || ['var(--accent)'];
  var ctx = document.createElement('canvas').getContext('2d');
  var family = getComputedStyle(document.body).fontFamily;
  var st = { scale: 1, selected: null, vbW: 0, focusId: null };
  var seq = 0;

  function s(tag, attrs) { var n = document.createElementNS(NS, tag); for (var k in attrs) n.setAttribute(k, attrs[k]); return n; }
  function measure(t, fs, fw) { ctx.font = fw + ' ' + fs + 'px ' + family; return ctx.measureText(t).width; }
  function has(n) { return !!(n.children && n.children.length); }
  function vis(n) { return has(n) && !n.collapsed ? n.children : []; }
  function leafCount(n) { return has(n) ? n.children.reduce(function (a, c) { return a + leafCount(c); }, 0) : 1; }

  (function init(n, d, color, i) {
    n.depth = d;
    n._id = 'mm' + (seq++);
    if (d === 1) color = n.color || palette[i % palette.length];
    n.branch = color || 'var(--accent)';
    if (n.collapsed == null) n.collapsed = has(n) && d > 0 && opts.collapseAt != null && d >= opts.collapseAt;
    (n.children || []).forEach(function (c, k) { init(c, d + 1, color, k); });
  })(root, 0, null, 0);

  function measureNode(n) {
    n.fs = n.depth === 0 ? 15 : n.depth === 1 ? 13 : 12.5;
    n.fw = n.depth <= 1 ? 600 : 400;
    n.more = has(n) && n.collapsed ? '+' + leafCount(n) : '';
    n.lw = measure(n.label, n.fs, n.fw);
    n.mw = n.more ? measure(n.more, 11, 500) : 0;
    var pad = n.depth === 0 ? 34 : n.depth === 1 ? 24 : 22;
    n.w = Math.ceil(n.lw + (n.more ? n.mw + 8 : 0) + pad);
    n.h = n.depth === 0 ? 36 : n.depth === 1 ? 26 : 20;
  }
  function height(n) { var k = vis(n); return k.length ? k.reduce(function (a, c) { return a + height(c); }, 0) : Math.max(ROW, n.h + 4); }

  function edge(x1, y1, x2, y2, color, width) {
    var dx = (x2 - x1) / 2;
    var p = s('path', { d: 'M' + x1 + ',' + y1 + ' C' + (x1 + dx) + ',' + y1 + ' ' + (x2 - dx) + ',' + y2 + ' ' + x2 + ',' + y2, fill: 'none' });
    p.style.stroke = color; p.style.strokeWidth = width; p.style.opacity = '0.7';
    return p;
  }
  function text(x, y, str, fs, fw, fill, anchor) {
    var t = s('text', { x: x, y: y + fs * 0.35, 'font-size': fs, 'font-weight': fw, 'text-anchor': anchor || 'start' });
    t.textContent = str; t.style.fill = fill;
    return t;
  }

  function act(n) {
    if (has(n) && n.depth > 0) n.collapsed = !n.collapsed;
    st.selected = n; st.focusId = n._id;
    if (opts.onSelect) opts.onSelect(n);
    draw();
  }

  function draw() {
    (function walk(n) { measureNode(n); vis(n).forEach(walk); })(root);
    var top = vis(root), total = top.reduce(function (a, c) { return a + height(c); }, 0), acc = 0, right = [], left = [];
    top.forEach(function (c) { if (acc < total / 2 || right.length === 0) { right.push(c); acc += height(c); } else left.push(c); });
    var sides = [[right, 1], [left, -1]], all = [], sideH = [0, 0];
    sides.forEach(function (sd, si) {
      var nodes = sd[0], dir = sd[1], colW = {}, off = {}, cursor = 0;
      (function cols(list) { list.forEach(function (n) { n.side = dir; colW[n.depth] = Math.max(colW[n.depth] || 0, n.w); cols(vis(n)); }); })(nodes);
      off[1] = root.w / 2 + GX;
      for (var d = 2; colW[d] != null; d++) off[d] = off[d - 1] + colW[d - 1] + GX;
      function place(n) {
        var k = vis(n);
        if (!k.length) { n.y = cursor + height(n) / 2; cursor += height(n); }
        else { k.forEach(place); n.y = (k[0].y + k[k.length - 1].y) / 2; }
        n.x = dir > 0 ? off[n.depth] : -off[n.depth] - n.w;
        all.push(n);
      }
      nodes.forEach(function (n, i) { if (i) cursor += GROUP_GAP; place(n); });
      sideH[si] = cursor;
    });
    var H = Math.max(sideH[0], sideH[1], root.h + 8);
    sides.forEach(function (sd, si) {
      var shift = (H - sideH[si]) / 2;
      (function sh(list) { list.forEach(function (n) { n.y += shift; sh(vis(n)); }); })(sd[0]);
    });
    root.x = -root.w / 2; root.y = H / 2; root.side = 1; all.push(root);

    var minX = Math.min.apply(null, all.map(function (n) { return n.x; }));
    var maxX = Math.max.apply(null, all.map(function (n) { return n.x + n.w; }));
    var vbW = maxX - minX + PAD * 2, vbH = H + PAD * 2;
    st.vbW = vbW;
    var svg = s('svg', { viewBox: (minX - PAD) + ' ' + (-PAD) + ' ' + vbW + ' ' + vbH, width: Math.round(vbW * st.scale), height: Math.round(vbH * st.scale), role: 'tree', 'aria-label': 'Mind map of ' + root.label });
    var edges = s('g', {}), nodesG = s('g', {});
    svg.appendChild(edges); svg.appendChild(nodesG);

    all.forEach(function (n) {
      vis(n).forEach(function (c) {
        var x1 = n.depth === 0 ? (c.side > 0 ? n.x + n.w : n.x) : (c.side > 0 ? n.x + n.w : n.x);
        var x2 = c.side > 0 ? c.x : c.x + c.w;
        edges.appendChild(edge(x1, n.y, x2, c.y, c.branch, c.depth === 1 ? 2.4 : 1.4));
      });
    });

    all.forEach(function (n) {
      var g = s('g', { 'class': 'mm-node', tabindex: '0', role: 'treeitem', 'data-id': n._id,
        'aria-label': n.label + (has(n) ? (n.collapsed ? ', collapsed, ' + leafCount(n) + ' items' : ', expanded') : '') });
      if (has(n) && n.depth > 0) g.setAttribute('aria-expanded', String(!n.collapsed));
      var tt = s('title', {}); tt.textContent = n.title || n.label; g.appendChild(tt);
      var selected = st.selected === n;
      if (n.depth === 0) {
        var r0 = s('rect', { x: n.x, y: n.y - n.h / 2, width: n.w, height: n.h, rx: n.h / 2 }); r0.style.fill = 'var(--accent)';
        g.appendChild(r0);
        g.appendChild(text(n.x + n.w / 2, n.y, n.label, n.fs, n.fw, 'var(--paper)', 'middle'));
      } else if (n.depth === 1) {
        var r1 = s('rect', { x: n.x, y: n.y - n.h / 2, width: n.w, height: n.h, rx: 7 });
        r1.style.fill = selected ? 'var(--accent-soft)' : 'var(--paper)'; r1.style.stroke = n.branch; r1.style.strokeWidth = '1.6';
        g.appendChild(r1);
        g.appendChild(text(n.x + 12, n.y, n.label, n.fs, n.fw, 'var(--ink)'));
        if (n.more) g.appendChild(text(n.x + 12 + n.lw + 8, n.y, n.more, 11, 500, 'var(--sub)'));
      } else {
        if (selected) { var hl = s('rect', { x: n.x + 1, y: n.y - 10, width: n.w - 2, height: 20, rx: 5 }); hl.style.fill = 'var(--accent-soft)'; g.appendChild(hl); }
        var dotX = n.side > 0 ? n.x + 8 : n.x + n.w - 8;
        var dot = s('circle', { cx: dotX, cy: n.y, r: has(n) ? 4.5 : 3.8 });
        dot.style.fill = opts.dotColor ? opts.dotColor(n) : (n.dot || n.branch);
        if (has(n)) { dot.style.stroke = n.branch; dot.style.strokeWidth = '1.5'; }
        g.appendChild(dot);
        if (n.side > 0) {
          g.appendChild(text(n.x + 17, n.y, n.label, n.fs, n.fw, 'var(--ink)'));
          if (n.more) g.appendChild(text(n.x + 17 + n.lw + 6, n.y, n.more, 11, 500, 'var(--sub)'));
        } else {
          g.appendChild(text(n.x + n.w - 17, n.y, n.label, n.fs, n.fw, 'var(--ink)', 'end'));
          if (n.more) g.appendChild(text(n.x + 3, n.y, n.more, 11, 500, 'var(--sub)'));
        }
      }
      if (has(n) || opts.onSelect) g.style.cursor = 'pointer';
      g.addEventListener('click', function () { act(n); });
      g.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); act(n); } });
      nodesG.appendChild(g);
    });

    host.textContent = '';
    host.appendChild(svg);
    if (st.focusId) { var f = svg.querySelector('[data-id="' + st.focusId + '"]'); if (f) f.focus({ preventScroll: true }); }
  }

  function setAll(collapsed) { (function w(n) { if (has(n) && n.depth > 0) n.collapsed = collapsed; (n.children || []).forEach(w); })(root); }
  draw();
  return {
    expandAll: function () { setAll(false); draw(); },
    collapseAll: function () { setAll(true); draw(); },
    zoom: function (f) { st.scale = Math.max(0.3, Math.min(2.5, st.scale * f)); draw(); },
    fit: function () {
      // Measure twice: fitting can make the map tall enough to add a vertical scrollbar,
      // which narrows the box it was fitted to.
      for (var i = 0; i < 2; i++) {
        var cs = getComputedStyle(host);
        var w = host.clientWidth - parseFloat(cs.paddingLeft || 0) - parseFloat(cs.paddingRight || 0) - 2;
        if (w <= 50 || !st.vbW) return;
        st.scale = Math.min(1, w / st.vbW); draw();
        if (host.scrollWidth <= host.clientWidth + 1) return;
      }
    }
  };
}
