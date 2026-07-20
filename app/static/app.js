function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- time helpers ---------- */
// Server timestamps are UTC ISO strings without a timezone suffix; append "Z"
// so the browser doesn't misinterpret them as local time.
function parseUtc(iso) {
  if (!iso) return null;
  const hasTz = /[zZ]|[+-]\d\d:?\d\d$/.test(iso);
  const d = new Date(hasTz ? iso : iso + "Z");
  return isNaN(d.getTime()) ? null : d;
}

function fmtAbsolute(iso) {
  const d = parseUtc(iso);
  return d ? d.toLocaleString() : "never";
}

function fmtRelative(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  let s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h " + (m % 60) + "m ago";
  const d = Math.floor(h / 24);
  return d + "d " + (h % 24) + "h ago";
}

/* ---------- component renderers (mirror _macros.html) ---------- */
function meter(value) {
  if (value === null || value === undefined) return '<span class="faint">—</span>';
  const p = Math.round(value);
  const cls = p >= 90 ? "crit" : p >= 70 ? "warn" : "ok";
  return `<div class="meter" title="${p}%"><div class="meter-fill ${cls}" style="width:${Math.min(100, p)}%"></div><span class="meter-label">${p}%</span></div>`;
}

function statusChip(isOnline) {
  return isOnline
    ? '<span class="chip ok"><span class="chip-dot"></span>Online</span>'
    : '<span class="chip bad"><span class="chip-dot"></span>Offline</span>';
}

function tailscaleChip(ts) {
  if (!ts) return '<span class="faint">—</span>';
  if (ts.connected) return '<span class="chip ok">Connected</span>';
  return `<span class="chip bad">${escapeHtml(ts.backend_state || "Down")}</span>`;
}

function containerSummary(list) {
  if (!list || !list.length) return '<span class="faint">—</span>';
  const running = list.filter((c) => c.status === "running").length;
  const cls = running === list.length ? "ok" : running === 0 ? "bad" : "warn";
  return `<span class="chip ${cls}">${running}/${list.length} up</span>`;
}

/* ---------- dashboard ---------- */
function renderDeviceRows(devices) {
  const tbody = document.querySelector("#device-table tbody");
  if (!tbody) return;

  if (devices.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No devices yet. Add one below to get an API key.</td></tr>';
  } else {
    tbody.innerHTML = devices.map((d) => {
      const sys = d.report && d.report.system;
      const hostname = sys && sys.hostname && sys.hostname !== d.name
        ? `<span class="sub">${escapeHtml(sys.hostname)}</span>` : "";
      return `<tr class="clickable" onclick="window.location='/devices/${d.id}'">
        <td>${statusChip(d.is_online)}</td>
        <td class="name-cell"><span class="name">${escapeHtml(d.name)}</span>${hostname}</td>
        <td>${meter(sys ? sys.cpu_percent : null)}</td>
        <td>${meter(sys ? sys.mem_percent : null)}</td>
        <td>${meter(sys ? sys.disk_percent : null)}</td>
        <td>${tailscaleChip(d.report ? d.report.tailscale : null)}</td>
        <td>${containerSummary(d.report ? d.report.docker_containers : null)}</td>
        <td class="muted" title="${escapeHtml(fmtAbsolute(d.last_seen_at))}">${fmtRelative(d.seconds_since_seen)}</td>
      </tr>`;
    }).join("");
  }

  updateTiles(devices);
}

function updateTiles(devices) {
  const total = devices.length;
  const online = devices.filter((d) => d.is_online).length;
  const containers = devices.reduce((sum, d) => {
    const list = d.report && d.report.docker_containers;
    return sum + (list ? list.filter((c) => c.status === "running").length : 0);
  }, 0);
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set("tile-total", total);
  set("tile-online", online);
  set("tile-offline", total - online);
  set("tile-containers", containers);
}

function startDevicePolling(url, intervalMs = 5000) {
  const poll = () => fetch(url).then((r) => r.json()).then(renderDeviceRows).catch(() => {});
  poll();
  setInterval(poll, intervalMs);
}

/* ---------- copy button ---------- */
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (!btn) return;
    const original = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = original; }, 1200);
  }).catch(() => {});
}

/* ---------- history chart ---------- */
function drawSparkline(canvas, series, colors) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  // gridlines at 25/50/75%
  ctx.strokeStyle = "rgba(255,255,255,0.05)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = (i / 4) * h;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  const maxY = 100;
  series.forEach((points, i) => {
    if (points.length < 2) return;
    ctx.beginPath();
    ctx.strokeStyle = colors[i];
    ctx.lineWidth = 1.8;
    points.forEach((v, idx) => {
      const x = (idx / (points.length - 1)) * (w - 8) + 4;
      const y = h - 4 - (v / maxY) * (h - 8);
      if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
}

function loadHistoryChart(url, canvasId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const render = () => fetch(url).then((r) => r.json()).then((rows) => {
    const cpu = rows.map((r) => r.cpu_percent);
    const mem = rows.map((r) => r.mem_percent);
    const disk = rows.map((r) => r.disk_percent);
    drawSparkline(canvas, [cpu, mem, disk], ["#ef5350", "#5b8cff", "#35c66b"]);
  }).catch(() => {});
  render();
  setInterval(render, 10000);
}
