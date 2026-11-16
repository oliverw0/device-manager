/* @ds-bundle: {"format":4,"namespace":"NightPanel","components":[]} */
/* Night Panel mark engine: the night dial, its tile, the LED matrix and the
   meter bands, plus the chase and rev loaders. Plain script, no dependencies.
   Each function returns markup as a string; NightPanel.mount() fills any
   element carrying data-np="dial|tile|matrix|bands|glyph|signal|chase|rev".
   Colours come from token classes (np-t-*) in bundle.css; pass literal:true
   to write hex values instead (standalone SVG files). */
(function (root) {
  'use strict';

  var HEX = {
    night: { g: '#37e06a', a: '#ffb038', r: '#ff5230', off: '#2c313a', roff: 'rgba(255,82,48,0.28)', bg: '#15171b', edge: '#3a414c' },
    print: { g: '#177a3a', a: '#9a5c00', r: '#c8361a', off: '#d7dbe0', roff: 'rgba(200,54,26,0.25)', bg: '#f4f5f6', edge: '#c9ced4' }
  };
  var uid = 0;

  /* Tick count by rendered size: 12 at 48px and up, 8 from 24px, 5 below. Never fewer than 5. */
  function tickCount(size) { return size >= 48 ? 12 : size >= 24 ? 8 : 5; }

  /* Zones: the last sixth of the sweep is the redline, the sixth before it amber. */
  function zone(i, n) {
    var red = Math.max(1, Math.round(n / 6));
    var amber = Math.max(1, Math.round(n / 6));
    if (i >= n - red) return 'r';
    if (i >= n - red - amber) return 'a';
    return 'g';
  }

  var ROT = { bl: 0, tl: 90, tr: 180, br: 270 };

  function fillAttr(z, lit, o) {
    if (o.literal || o.tone === 'print') {
      var h = HEX[o.tone];
      return 'fill="' + (lit ? h[z] : (z === 'r' ? h.roff : h.off)) + '"';
    }
    return 'class="np-t ' + (lit ? 'np-t-' + z : (z === 'r' ? 'np-t-roff' : 'np-t-off')) + '"';
  }

  function glowFilter(id) {
    return '<filter id="' + id + '" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="2.2" result="b"/>' +
      '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>';
  }

  /* The ticks of one dial inside a box (bx, by, bs) of the 100-unit viewBox. */
  function ticks(o, bx, by, bs) {
    var n = o.ticks || tickCount(o.size);
    var lit = o.progress == null ? n : Math.round(o.progress * n);
    var r1 = 0.58 * bs, r2 = 0.92 * bs, step = 90 / n;
    var w = 0.55 * (0.75 * bs) * step * Math.PI / 180;
    var on = '', offs = '';
    for (var i = 0; i < n; i++) {
      var theta = 90 - (i + 0.5) * step, z = zone(i, n), isLit = i < lit;
      var anim = o.motion ? ' style="--i:' + i + '"' : '';
      var t = '<rect x="' + r1.toFixed(2) + '" y="' + (-w / 2).toFixed(2) + '" width="' + (r2 - r1).toFixed(2) + '" height="' + w.toFixed(2) +
        '" rx="' + (bs / 100).toFixed(2) + '" ' + fillAttr(z, o.motion ? true : isLit, o) + anim +
        ' transform="translate(' + bx + ' ' + (by + bs) + ') rotate(' + (-theta).toFixed(2) + ')"/>';
      if (isLit || o.motion) on += t; else offs += t;
    }
    return { on: on, off: offs, n: n };
  }

  function svgOpen(o, extra) {
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="' + o.size + '" height="' + o.size +
      '" role="img" aria-label="' + (o.label || 'Night Panel') + '"' + (extra || '') + '>';
  }

  /* dial({size, corner, progress, ticks, glow, tone, literal, motion})
     The house mark: rectangular LED ticks on a quarter arc, green to amber to the redline, sweeping clockwise.
     corner: bl (standalone, default) | tr (top-right page corner) | tl | br. */
  function dial(opts) {
    var o = Object.assign({ size: 56, corner: 'bl', tone: 'night', progress: null, glow: null, literal: false, motion: null }, opts || {});
    var glow = o.glow == null ? (o.size >= 28 && o.tone === 'night') : o.glow;
    var id = 'npg' + (++uid);
    var t = ticks(o, 0, 0, 100);
    var cls = o.motion ? ' class="np-dial np-' + o.motion + '" style="--n:' + t.n + '"' : ' class="np-dial"';
    return svgOpen(o, cls) + (glow ? '<defs>' + glowFilter(id) + '</defs>' : '') +
      '<g transform="rotate(' + ROT[o.corner] + ' 50 50)">' + t.off +
      (glow ? '<g filter="url(#' + id + ')">' + t.on + '</g>' : t.on) + '</g></svg>';
  }

  /* Matrix cells for a tile: n x n square LEDs in the box (bx, by, bs), lit along the diagonal from the
     bottom-left, green then amber; the redline corner (top-right) always shows faintly. */
  function cells(o, bx, by, bs, n) {
    var gr = n > 3 ? 6 / 14 : 7 / 14, c = bs / (n + (n - 1) * gr), g = c * gr, on = '', offs = '';
    for (var r = 0; r < n; r++) {
      for (var k = 0; k < n; k++) {
        var d = (k + (n - 1 - r)) / (2 * n - 2);
        var z = d >= 5 / 6 ? 'r' : d >= 4 / 6 ? 'a' : 'g', lit = z !== 'r';
        var t = '<rect x="' + (bx + k * (c + g)).toFixed(2) + '" y="' + (by + r * (c + g)).toFixed(2) + '" width="' + c.toFixed(2) +
          '" height="' + c.toFixed(2) + '" rx="' + (bs / 64).toFixed(2) + '" ' + fillAttr(z, lit, o) + '/>';
        if (lit) on += t; else offs += t;
      }
    }
    return { on: on, off: offs };
  }

  /* tile({size, tone, mask, progress, glyph, literal}): the app icon. A dial on a dark square with a hairline edge.
     mask:true rounds the corners the way iOS and macOS will (22.5%); without it, square for the platform to mask.
     glyph:'matrix' puts the LED matrix in the tile instead (4 x 4, or 3 x 3 below 48px), redline corner dark:
     for an app that is a dashboard or an index of other things. */
  function tile(opts) {
    var o = Object.assign({ size: 128, tone: 'night', mask: false, progress: null, glyph: null, literal: false }, opts || {});
    var h = HEX[o.tone], id = 'npg' + (++uid), glow = o.tone === 'night' && o.size >= 28;
    var rx = o.mask ? 22.5 : 0;
    var inner = Object.assign({}, o, { size: o.size * 0.7, ticks: tickCount(o.size * 0.7) });
    var t = o.glyph === 'matrix' ? (o.size >= 48 ? cells(o, 22, 22, 56, 4) : cells(o, 24, 24, 52, 3)) : ticks(inner, 12, 18, 70);
    var bg = o.literal ? 'fill="' + h.bg + '" stroke="' + h.edge + '"' : 'class="np-tile-bg' + (o.tone === 'print' ? ' np-print' : '') + '"';
    return svgOpen(o) + (glow ? '<defs>' + glowFilter(id) + '</defs>' : '') +
      '<rect x="0.5" y="0.5" width="99" height="99" rx="' + rx + '" ' + bg + ' stroke-width="1"/>' + t.off +
      (glow ? '<g filter="url(#' + id + ')">' + t.on + '</g>' : t.on) + '</svg>';
  }

  /* matrix({cols, rows, cell, gap, level, tone, literal}): the dial unrolled into square LEDs.
     Cells light from the bottom-left corner along the diagonal, green to amber, up to level (0..1);
     the rest stay dark, and the redline corner (top-right) always shows faintly. For cover fields and backgrounds. */
  function matrix(opts) {
    var o = Object.assign({ cols: 12, rows: 8, cell: 14, gap: 4, level: 0.72, tone: 'night', literal: false }, opts || {});
    var W = o.cols * (o.cell + o.gap) - o.gap, H = o.rows * (o.cell + o.gap) - o.gap, out = '';
    for (var r = 0; r < o.rows; r++) {
      for (var c = 0; c < o.cols; c++) {
        var d = (c + (o.rows - 1 - r)) / (o.cols + o.rows - 2);
        var z = d >= 5 / 6 ? 'r' : d >= 4 / 6 ? 'a' : 'g';
        var lit = d <= o.level && z !== 'r';
        out += '<rect x="' + c * (o.cell + o.gap) + '" y="' + r * (o.cell + o.gap) + '" width="' + o.cell + '" height="' + o.cell + '" rx="1" ' + fillAttr(z, lit, o) + '/>';
      }
    }
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + ' ' + H + '" width="100%" preserveAspectRatio="xMidYMid slice" class="np-matrix" role="img" aria-label="' + (o.label || 'Night Panel matrix') + '">' + out + '</svg>';
  }

  /* bands({widths, zones, height, gap, progress, skeleton}): the meter stack. Square-ended bars on dim tracks.
     Default four bars stepping down: green, green, amber, red. progress (0..1) lights whole bars top-down;
     skeleton:true shows dim tracks breathing, for loading placeholders. */
  function bands(opts) {
    var o = Object.assign({ widths: [1, 0.78, 0.55, 0.32], zones: ['g', 'g', 'a', 'r'], height: 8, gap: 6, progress: null, skeleton: false }, opts || {});
    var n = o.widths.length, lit = o.progress == null ? n : Math.round(o.progress * n), out = '';
    for (var i = 0; i < n; i++) {
      var z = o.zones[i] || 'g', on = !o.skeleton && i < lit;
      out += '<span class="np-band" style="height:' + o.height + 'px"><i class="np-band-' + (on ? z : (z === 'r' ? 'roff' : 'off')) +
        '" style="width:' + (o.widths[i] * 100).toFixed(1) + '%"></i></span>';
    }
    return '<span class="np-bands' + (o.skeleton ? ' np-skeleton' : '') + '" style="gap:' + o.gap + 'px" role="presentation">' + out + '</span>';
  }

  /* glyph({rows, cell, gap, tile, label}): a small pixel picture drawn in LEDs, for item icons (a game, a
     device type, a kind of job). rows: equal-length strings, one character per LED: g green, a amber, r red,
     w white (text-strong); anything else is an unlit LED. Keep it 8 to 16 LEDs a side. tile:true sets it on
     the tile background with a one-LED margin, like the matrix tile. Colour carries meaning only through the
     usual zones: draw the picture mostly in white and green, amber and red sparingly. */
  function glyph(opts) {
    var o = Object.assign({ rows: [], cell: 10, gap: 2, tile: false, label: 'icon' }, opts || {});
    var rows = o.rows, R = rows.length, C = 0, pad = o.tile ? o.cell + o.gap : 0, out = '', i, c;
    for (i = 0; i < R; i++) C = Math.max(C, rows[i].length);
    var step = o.cell + o.gap, W = C * step - o.gap + 2 * pad, H = R * step - o.gap + 2 * pad;
    var CLS = { g: 'np-t np-t-g', a: 'np-t np-t-a', r: 'np-t np-t-r', w: 'np-t np-t-w' };
    if (o.tile) out += '<rect class="np-tile-bg" x="0.5" y="0.5" width="' + (W - 1) + '" height="' + (H - 1) + '" rx="2"/>';
    for (i = 0; i < R; i++) {
      for (c = 0; c < C; c++) {
        var cls = CLS[rows[i].charAt(c)] || 'np-t np-t-off';
        out += '<rect x="' + (pad + c * step) + '" y="' + (pad + i * step) + '" width="' + o.cell + '" height="' + o.cell + '" rx="1" class="' + cls + '"/>';
      }
    }
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + ' ' + H + '" width="100%" class="np-glyph" role="img" aria-label="' + o.label + '">' + out + '</svg>';
  }

  /* signal({level, of, tone, label}): ascending bars, like a phone's reception or a game's server list ping.
     level bars of `of` are lit; tone ok (green), warn (amber) or bad (red). Always pair it with the number. */
  function signal(opts) {
    var o = Object.assign({ level: 0, of: 4, tone: 'ok', label: '' }, opts || {}), out = '';
    for (var i = 0; i < o.of; i++) out += '<i' + (i < o.level ? ' class="on"' : '') + ' style="height:' + Math.round(100 * (i + 1) / o.of) + '%"></i>';
    return '<span class="np-signal ' + o.tone + '" role="img" aria-label="' + (o.label || o.level + ' of ' + o.of) + '">' + out + '</span>';
  }

  /* Loaders. chase: one lit tick runs the sweep (ordinary loading, 1.2s). rev: the dial fills and drains like a revving needle (long tasks, syncs, 2.4s). */
  function chase(opts) { return dial(Object.assign({ size: 40 }, opts || {}, { motion: 'chase', progress: null })); }
  function rev(opts) { return dial(Object.assign({ size: 40 }, opts || {}, { motion: 'rev', progress: null })); }

  /* nav(aside): the collapsible sidebar. The hamburger collapses and expands it; collapsed, the logo
     is the way back (click it) and so is the sidebar's right edge (a handle that lights on hover).
     The choice is remembered per app: data-np-nav="key" names the localStorage key. Opt-in, via
     <aside class="sidebar fixed" data-np-nav="myapp-nav">; mount() wires every one it finds. */
  function nav(aside) {
    if (!aside || aside.getAttribute('data-np-nav-ready')) return;
    aside.setAttribute('data-np-nav-ready', '1');
    var key = aside.getAttribute('data-np-nav') || 'np-nav';
    function set(c) {
      aside.classList.toggle('collapsed', c);
      if (aside.classList.contains('fixed')) document.body.classList.toggle('nav-collapsed', c);
      edge.setAttribute('aria-label', c ? 'Expand the menu' : 'Collapse the menu');
      edge.setAttribute('aria-expanded', c ? 'false' : 'true');
      try { localStorage.setItem(key, c ? '1' : ''); } catch (e) { /* private mode */ }
    }
    var edge = document.createElement('button');
    edge.type = 'button';
    edge.className = 'sidebar-edge';
    aside.appendChild(edge);
    var stored = false;
    try { stored = !!localStorage.getItem(key); } catch (e) { /* private mode */ }
    set(stored || aside.classList.contains('collapsed'));
    edge.addEventListener('click', function () { set(!aside.classList.contains('collapsed')); });
    var toggle = aside.querySelector('.nav-toggle');
    if (toggle) toggle.addEventListener('click', function () { set(!aside.classList.contains('collapsed')); });
    var brand = aside.querySelector('.brand');
    if (brand) brand.addEventListener('click', function (e) {
      if (aside.classList.contains('collapsed')) { e.preventDefault(); set(false); }
    });
    return { set: set };
  }

  function num(v) { return v == null || v === '' ? null : Number(v); }
  function mount(scope) {
    var navs = (scope || document).querySelectorAll('[data-np-nav]');
    for (var j = 0; j < navs.length; j++) nav(navs[j]);
    var els = (scope || document).querySelectorAll('[data-np]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i], d = el.dataset, kind = d.np;
      var o = { size: num(d.size) || undefined, corner: d.corner, tone: d.tone, progress: num(d.progress), label: d.label, mask: d.mask === 'true', glyph: d.glyph };
      Object.keys(o).forEach(function (k) { if (o[k] === undefined || o[k] === null) delete o[k]; });
      if (kind === 'dial') el.innerHTML = dial(o);
      else if (kind === 'tile') el.innerHTML = tile(o);
      else if (kind === 'chase') el.innerHTML = chase(o);
      else if (kind === 'rev') el.innerHTML = rev(o);
      else if (kind === 'matrix') el.innerHTML = matrix({ cols: num(d.cols) || 12, rows: num(d.rows) || 8, level: num(d.level) == null ? 0.72 : num(d.level), tone: d.tone || 'night' });
      else if (kind === 'bands') el.innerHTML = bands({ progress: num(d.progress), skeleton: d.skeleton === 'true', height: num(d.height) || 8 });
      else if (kind === 'glyph') el.innerHTML = glyph({ rows: (d.rows || '').split('|'), tile: d.tile === 'true', label: d.label || 'icon' });
      else if (kind === 'signal') el.innerHTML = signal({ level: num(d.level) || 0, of: num(d.of) || 4, tone: d.tone || 'ok', label: d.label });
    }
  }

  root.NightPanel = { dial: dial, tile: tile, matrix: matrix, bands: bands, glyph: glyph, signal: signal, nav: nav, chase: chase, rev: rev, mount: mount, tickCount: tickCount, colors: HEX };
  if (typeof document !== 'undefined' && document.readyState !== 'loading') mount();
  else if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', function () { mount(); });
})(typeof window !== 'undefined' ? window : globalThis);
