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

/* ---------- copy to clipboard ---------- */
// navigator.clipboard only exists in a secure context (https or localhost).
// This tool is usually served over plain http on a LAN IP, where it's
// undefined — so fall back to a hidden textarea + execCommand.
function fallbackCopy(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true).catch(() => fallbackCopy(text));
  }
  return Promise.resolve(fallbackCopy(text));
}

// Flashes a "Copied" tooltip on the element without destroying its content,
// so it works for both buttons and inline copyable values.
function copyText(text, el) {
  copyToClipboard(text).then((ok) => {
    if (!el || !ok) return;
    el.classList.add("copied");
    setTimeout(() => el.classList.remove("copied"), 1200);
  });
}

/* ---------- history chart ---------- */
var CHART_SERIES = [
  { key: "cpu_percent", label: "CPU", color: "#ef5350" },
  { key: "mem_percent", label: "Memory", color: "#5b8cff" },
  { key: "disk_percent", label: "Disk", color: "#35c66b" },
];

function chartX(i, count, w) { return count < 2 ? 4 : (i / (count - 1)) * (w - 8) + 4; }
function chartY(v, h) { return h - 4 - (v / 100) * (h - 8); }

function drawChart(canvas, rows, hoverIndex) {
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

  if (rows.length < 2) return;

  CHART_SERIES.forEach((s) => {
    ctx.beginPath();
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.8;
    rows.forEach((r, i) => {
      const x = chartX(i, rows.length, w);
      const y = chartY(r[s.key], h);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });

  // hover guide line + highlight dots
  if (hoverIndex != null && rows[hoverIndex]) {
    const x = chartX(hoverIndex, rows.length, w);
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
    CHART_SERIES.forEach((s) => {
      const y = chartY(rows[hoverIndex][s.key], h);
      ctx.beginPath();
      ctx.fillStyle = s.color;
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#0d0f13";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    });
  }
}

function loadHistoryChart(url, canvasId) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const wrap = canvas.parentElement;
  wrap.style.position = wrap.style.position || "relative";

  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.style.display = "none";
  wrap.appendChild(tip);

  const state = { rows: [], hoverIndex: null };
  const draw = () => drawChart(canvas, state.rows, state.hoverIndex);

  function indexFromEvent(e) {
    const rect = canvas.getBoundingClientRect();
    // The canvas renders at a fixed internal width but is displayed scaled
    // (max-width:100%), so map the pointer using the displayed rect width.
    const ratio = (e.clientX - rect.left) / rect.width;
    const idx = Math.round(ratio * (state.rows.length - 1));
    return Math.max(0, Math.min(state.rows.length - 1, idx));
  }

  function showTip(e, idx) {
    const row = state.rows[idx];
    if (!row) return;
    const when = parseUtc(row.timestamp);
    const rowsHtml = CHART_SERIES.map((s) =>
      `<div class="tip-row"><span class="tip-dot" style="background:${s.color}"></span>${s.label}<b>${Math.round(row[s.key])}%</b></div>`
    ).join("");
    tip.innerHTML = `<div class="tip-time">${when ? when.toLocaleString() : ""}</div>${rowsHtml}`;
    tip.style.display = "block";

    const wrapRect = wrap.getBoundingClientRect();
    let left = e.clientX - wrapRect.left + 14;
    if (left + tip.offsetWidth > wrap.clientWidth) {
      left = e.clientX - wrapRect.left - tip.offsetWidth - 14;
    }
    let top = e.clientY - wrapRect.top + 14;
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = top + "px";
  }

  canvas.addEventListener("mousemove", (e) => {
    if (state.rows.length < 2) return;
    state.hoverIndex = indexFromEvent(e);
    draw();
    showTip(e, state.hoverIndex);
  });
  canvas.addEventListener("mouseleave", () => {
    state.hoverIndex = null;
    tip.style.display = "none";
    draw();
  });

  const render = () => fetch(url).then((r) => r.json()).then((rows) => {
    state.rows = rows;
    draw();
  }).catch(() => {});
  render();
  setInterval(render, 10000);
}

/* ---------- per-container background sparklines ---------- */
var CONTAINER_SPARK_COLORS = { cpu: "239,83,80", mem: "91,140,255" };

function drawContainerSpark(canvas, values, metric) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2) return;

  const rgb = CONTAINER_SPARK_COLORS[metric] || CONTAINER_SPARK_COLORS.cpu;
  const xAt = (i) => (i / (values.length - 1)) * w;
  const yAt = (v) => h - (Math.max(0, Math.min(100, v)) / 100) * (h - 3) - 1;

  // filled area
  ctx.beginPath();
  ctx.moveTo(0, h);
  values.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
  ctx.lineTo(w, h);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, `rgba(${rgb},0.38)`);
  grad.addColorStop(1, `rgba(${rgb},0.03)`);
  ctx.fillStyle = grad;
  ctx.fill();

  // line
  ctx.beginPath();
  values.forEach((v, i) => (i === 0 ? ctx.moveTo(xAt(i), yAt(v)) : ctx.lineTo(xAt(i), yAt(v))));
  ctx.strokeStyle = `rgba(${rgb},0.6)`;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function loadContainerSparks(url) {
  const panel = document.getElementById("containers-panel");
  if (!panel) return;
  const toggle = document.getElementById("spark-toggle");
  const state = { data: {}, metric: "cpu" };

  function drawAll() {
    panel.querySelectorAll(".container-item").forEach((item) => {
      const name = item.getAttribute("data-name");
      const canvas = item.querySelector(".container-spark");
      const summary = item.querySelector("summary");
      if (!canvas || !summary) return;
      const w = summary.clientWidth, h = summary.clientHeight;
      if (w === 0 || h === 0) return;
      if (canvas.width !== w) canvas.width = w;
      if (canvas.height !== h) canvas.height = h;
      const series = (state.data[name] || [])
        .map((p) => (state.metric === "cpu" ? p.cpu_percent : p.mem_percent))
        .filter((v) => v != null);
      drawContainerSpark(canvas, series, state.metric);

      // update the inline current-usage figure to match the selected metric
      const usage = item.querySelector(".c-usage");
      if (usage) {
        const val = usage.getAttribute(state.metric === "cpu" ? "data-cpu" : "data-mem");
        usage.textContent = val === "" || val === null ? "" : Math.round(parseFloat(val)) + "%";
      }
    });
  }

  if (toggle) {
    toggle.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-metric]");
      if (!btn) return;
      state.metric = btn.getAttribute("data-metric");
      toggle.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
      drawAll();
    });
  }

  const render = () => fetch(url).then((r) => r.json()).then((data) => {
    state.data = data;
    drawAll();
  }).catch(() => {});
  render();
  setInterval(render, 15000);
  window.addEventListener("resize", drawAll);
}
