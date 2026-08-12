function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

var TERMINAL_ICON = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>';

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
  return d ? d.toLocaleString("en-AU", { timeZone: "Australia/Sydney" }) : "never";
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
  if (ts.backend_state === "not_installed") return '<span class="chip neutral">No Tailscale client</span>';
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
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No devices yet. Add one below to get an API key.</td></tr>';
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
        <td class="last-seen-cell muted" title="${escapeHtml(fmtAbsolute(d.last_seen_at))}">
          <span>${fmtRelative(d.seconds_since_seen)}</span>
        </td>
        <td class="term-cell">
          ${d.ssh_enabled ? `<button class="term-btn" title="Open SSH terminal" data-term-id="${d.id}" data-term-name="${escapeHtml(d.name)}" onclick="event.stopPropagation(); openTerminalFromBtn(this)">${TERMINAL_ICON}</button>` : ""}
        </td>
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
  { key: "cpu_percent", label: "CPU", color: "#ff5230" },
  { key: "mem_percent", label: "Memory", color: "#37e06a" },
  { key: "disk_percent", label: "Disk", color: "#ffb038" },
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
    const whenStr = when ? when.toLocaleString("en-AU", { timeZone: "Australia/Sydney" }) : "";
    tip.innerHTML = `<div class="tip-time">${whenStr}</div>${rowsHtml}`;
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
var CONTAINER_SPARK_COLORS = { cpu: "255,82,48", mem: "55,224,106" };

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

/* ---------- in-browser SSH terminal ---------- */
var TERM = null, TERM_FIT = null, TERM_WS = null, TERM_DEVICE = null;

function termStatus(text) {
  const el = document.getElementById("term-status");
  if (el) el.textContent = text;
}

function openTerminalFromBtn(btn) {
  openTerminal(btn.dataset.termId, btn.dataset.termName);
}

function showSection(id) {
  document.getElementById(id).hidden = false;
  document.body.classList.add("term-open");
  document.getElementById("terminal-pane").hidden = false;
  refitTerminal();
}

function hideSection(id) {
  document.getElementById(id).hidden = true;
  const bothHidden = document.getElementById("term-section").hidden && document.getElementById("logs-section").hidden;
  if (bothHidden) {
    document.body.classList.remove("term-open");
    document.getElementById("terminal-pane").hidden = true;
  }
  refitTerminal();
}

// Re-fit the xterm after the pane layout changes (logs opening halves its height).
function refitTerminal() {
  if (!TERM || !TERM_FIT) return;
  setTimeout(() => {
    try {
      TERM_FIT.fit();
      if (TERM_WS && TERM_WS.readyState === 1) TERM_WS.send(JSON.stringify({ type: "resize", cols: TERM.cols, rows: TERM.rows }));
    } catch (e) {}
  }, 60);
}

function closeTerminal() {
  if (TERM_WS) { try { TERM_WS.close(); } catch (e) {} TERM_WS = null; }
  if (TERM) { try { TERM.dispose(); } catch (e) {} TERM = null; }
  const body = document.getElementById("term-body");
  if (body) body.innerHTML = "";
  hideSection("term-section");
}

function openTerminal(deviceId, deviceName) {
  if (typeof Terminal === "undefined") { alert("Terminal library failed to load."); return; }
  TERM_DEVICE = deviceId;
  const title = document.getElementById("term-title");
  if (title) title.textContent = "SSH · " + deviceName;
  const userSel = document.getElementById("term-user");
  const connectBtn = document.getElementById("term-connect");
  userSel.innerHTML = "";
  connectBtn.disabled = true;
  showSection("term-section");
  termStatus("Loading users…");

  fetch(`/devices/${deviceId}/ssh-users.json`).then((r) => r.json()).then((info) => {
    if (!info.ssh_enabled) { termStatus("SSH is disabled for this device — enable it on the device page."); return; }
    if (!info.users || !info.users.length) { termStatus("No login users reported yet (waiting for a client report)."); return; }
    info.users.forEach((u) => {
      const opt = document.createElement("option");
      opt.value = u; opt.textContent = u;
      userSel.appendChild(opt);
    });
    if (!info.host) { termStatus("No SSH address available for this device."); return; }
    termStatus("Ready — target " + info.host + ":" + (info.port || 22));
    connectBtn.disabled = false;
  }).catch(() => termStatus("Failed to load users."));
}

function connectTerminal() {
  if (!TERM_DEVICE) return;
  const user = document.getElementById("term-user").value;
  if (!user) return;
  const body = document.getElementById("term-body");
  body.innerHTML = "";

  TERM = new Terminal({ cursorBlink: true, fontSize: 13, fontFamily: "ui-monospace, Menlo, Consolas, monospace", theme: { background: "#0b0d11" } });
  TERM_FIT = new FitAddon.FitAddon();
  TERM.loadAddon(TERM_FIT);
  TERM.open(body);
  try { TERM_FIT.fit(); } catch (e) {}

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/devices/${TERM_DEVICE}/terminal?user=${encodeURIComponent(user)}`);
  ws.binaryType = "arraybuffer";
  TERM_WS = ws;
  termStatus("Connecting…");

  const sendResize = () => {
    if (ws.readyState !== 1) return;
    try { TERM_FIT.fit(); } catch (e) {}
    ws.send(JSON.stringify({ type: "resize", cols: TERM.cols, rows: TERM.rows }));
  };

  ws.onopen = () => {
    termStatus("Connected as " + user);
    sendResize();
    TERM.onData((d) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data: d })); });
    TERM.focus();
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") TERM.write(ev.data);
    else TERM.write(new Uint8Array(ev.data));
  };
  ws.onclose = () => termStatus("Disconnected.");
  ws.onerror = () => termStatus("Connection error.");
  window.addEventListener("resize", sendResize);
}

function initTermUpload() {
  const btn = document.getElementById("term-upload");
  const input = document.getElementById("term-file");
  if (!btn || !input) return;
  btn.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const file = input.files[0];
    input.value = "";
    if (!file || !TERM_DEVICE) return;
    const user = document.getElementById("term-user").value;
    if (!user) { termStatus("Select a user first."); return; }
    const fd = new FormData();
    fd.append("file", file);
    termStatus(`Uploading ${file.name}…`);
    fetch(`/devices/${TERM_DEVICE}/upload?user=${encodeURIComponent(user)}`, { method: "POST", body: fd })
      .then((r) => r.json())
      .then((res) => {
        termStatus(res.ok ? `Uploaded ${file.name} to ${res.output}` : `Upload failed: ${res.output}`);
        if (res.ok && TERM) TERM.write(`\r\n\x1b[32m↑ uploaded ${file.name} to ${res.output}\x1b[0m\r\n`);
      })
      .catch(() => termStatus("Upload failed."));
  });
}

function initSshToggle() {
  const toggle = document.getElementById("ssh-toggle");
  if (!toggle) return;
  toggle.addEventListener("change", () => {
    const id = toggle.dataset.deviceId;
    const enabled = toggle.checked;
    toggle.disabled = true;
    fetch(`/devices/${id}/ssh`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "ssh_enabled=" + (enabled ? "true" : "false"),
    }).then((r) => {
      toggle.disabled = false;
      if (!r.ok) { toggle.checked = !enabled; return; }
      const wrap = document.getElementById("ssh-open-wrap");
      if (wrap) wrap.style.display = enabled ? "" : "none";
    }).catch(() => { toggle.disabled = false; toggle.checked = !enabled; });
  });
}

function initInstallCmd() {
  const cmd = document.getElementById("install-cmd");
  const env = document.getElementById("env-snippet");
  if (!cmd && !env) return;
  const key = (cmd || env).dataset.key;
  const host = location.origin;
  if (cmd) cmd.textContent =
    "git clone https://github.com/oliverw0/device-manager-client && cd device-manager-client\n" +
    `sudo HOST_URL=${host} API_KEY=${key} ./install.sh`;
  if (env) env.textContent = `HOST_URL=${host}\nAPI_KEY=${key}`;
}

function initSshProvisionCmd() {
  const el = document.getElementById("ssh-provision-cmd");
  if (!el) return;
  el.textContent =
    "mkdir -p ~/.ssh && curl -fsS " + location.origin +
    "/api/v1/ssh-pubkey >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys";
}

document.addEventListener("DOMContentLoaded", () => {
  const connectBtn = document.getElementById("term-connect");
  const closeBtn = document.getElementById("term-close");
  if (connectBtn) connectBtn.addEventListener("click", connectTerminal);
  if (closeBtn) closeBtn.addEventListener("click", closeTerminal);
  const logsClose = document.getElementById("logs-close");
  if (logsClose) logsClose.addEventListener("click", closeLogs);
  initTermUpload();
  initSshToggle();
  initSshProvisionCmd();
  initInstallCmd();
});

/* ---------- docker container controls (via SSH) ---------- */
var LOGS_TIMER = null;

function containerAction(deviceId, name, action, btn) {
  if (btn) btn.disabled = true;
  fetch(`/devices/${deviceId}/container/${encodeURIComponent(name)}/${action}`, { method: "POST" })
    .then((r) => r.json())
    .then((res) => { if (!res.ok) alert(`${action} failed: ${(res.output || "").slice(0, 400)}`); })
    .catch(() => {})
    .finally(() => {
      if (btn) btn.disabled = false;
      containerLogs(deviceId, name);  // show what happened in the logs pane
    });
}

function containerLogs(deviceId, name) {
  const pre = document.getElementById("logs-body");
  document.getElementById("logs-title").textContent = "Logs · " + name;
  showSection("logs-section");
  const load = () => fetch(`/devices/${deviceId}/container/${encodeURIComponent(name)}/logs`)
    .then((r) => r.text())
    .then((t) => {
      const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
      pre.textContent = t;
      if (atBottom) pre.scrollTop = pre.scrollHeight;  // follow tail unless scrolled up
    })
    .catch(() => { pre.textContent = "failed to fetch logs"; });
  pre.textContent = "loading…";
  load();
  clearInterval(LOGS_TIMER);
  LOGS_TIMER = setInterval(load, 2000);  // ponytail: poll --tail 200; swap for docker logs -f WS if live streaming needed
}

function stackAction(deviceId, names, action, btn) {
  if (btn) btn.disabled = true;
  Promise.all(names.map((n) =>
    fetch(`/devices/${deviceId}/container/${encodeURIComponent(n)}/${action}`, { method: "POST" })
      .then((r) => r.json()).catch(() => ({ ok: false }))
  )).then((results) => {
    const failed = results.filter((r) => !r.ok).length;
    if (failed) alert(`${action}: ${failed}/${names.length} container(s) failed`);
  }).finally(() => { if (btn) btn.disabled = false; });
}

function closeLogs() {
  clearInterval(LOGS_TIMER);
  LOGS_TIMER = null;
  hideSection("logs-section");
}
