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
function meter(value, label = "", title = "", extraCls = "") {
  if (value === null || value === undefined) return '<span class="faint">—</span>';
  const p = Math.round(value);
  const cls = p >= 90 ? "crit" : p >= 70 ? "warn" : "ok";
  return `<div class="meter ${extraCls}" title="${escapeHtml(title || p + "%")}"><div class="meter-fill ${cls}" style="width:${Math.min(100, p)}%"></div><span class="meter-label">${escapeHtml(label)}${p}%</span></div>`;
}

const gib = (n) => ((n || 0) / 1073741824).toFixed(1) + " GiB";

function diskCell(sys) {
  if (!sys || !sys.disks || sys.disks.length < 2) return meter(sys ? sys.disk_percent : null);
  return '<div class="disk-stack">' + sys.disks.map((dk) => {
    const full = dk.mount.replace(/\\+$/, "") || dk.mount;
    const short = full.includes(":") ? full : (full.replace(/\/+$/, "").split("/").pop() || "/");
    const tip = full + (dk.label ? " " + dk.label : "") +
      (dk.total_bytes ? ` · ${gib(dk.used_bytes)} / ${gib(dk.total_bytes)}` : "");
    return meter(dk.percent, short + " ", tip, "mini");
  }).join("") + "</div>";
}

function statusDot(isOnline) {
  return `<span class="stat-led ${isOnline ? "on" : "off"}" title="${isOnline ? "Online" : "Offline"}"></span>`;
}

// Official Tailscale logomark; colour + glow by state (mirrors _macros.html).
var TS_PATH = "M24 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm-9 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm0-9a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm6-6a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm0-.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM3 24a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm0-.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zm18 .5a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm0-.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM6 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm9-9a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm-3 2.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM6 3a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM3 5.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z";
function tailscaleLogo(ts, offline) {
  let st, txt;
  if (!ts || ts.backend_state === "not_installed") { st = "off"; txt = "Not set up"; }
  else if (offline) { st = "down"; txt = "Offline"; }
  else if (ts.connected) { st = "online"; txt = "Connected"; }
  else if (ts.backend_state === "NeedsLogin" || ts.backend_state === "Stopped") { st = "alert"; txt = ts.backend_state; }
  else { st = "down"; txt = ts.backend_state || "Down"; }
  return `<span class="ts-logo ts-${st}" title="Tailscale: ${escapeHtml(txt)}"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-label="Tailscale ${escapeHtml(txt)}"><path d="${TS_PATH}"/></svg></span>`;
}

function containerSummary(list, offline) {
  if (!list || !list.length) return '<span class="faint">—</span>';
  if (offline) return `<span class="chip bad">0/${list.length} up</span>`;
  const running = list.filter((c) => c.status === "running").length;
  const cls = running === list.length ? "ok" : running === 0 ? "bad" : "warn";
  return `<span class="chip ${cls}">${running}/${list.length} up</span>`;
}

/* ---------- dashboard: fleet cards ----------
   The front page is rendered only here (initial data is embedded in the page,
   then refreshed by polling), so there's no Jinja twin to keep in sync. */
var FLEET = { devices: [], filter: "all", query: "" };

const SEV_RANK = { bad: 0, warn: 1 };

// What's wrong with a device, worst first: [{sev: "bad"|"warn", text}].
function deviceIssues(d) {
  const out = [];
  const sys = d.report && d.report.system;
  if (!d.is_online) return [{ sev: "bad", text: d.last_seen_at ? `Offline ${fmtRelative(d.seconds_since_seen).replace(" ago", "")}` : "Never reported" }];
  if (sys) {
    const disks = sys.disks && sys.disks.length ? sys.disks : [{ mount: "Disk", percent: sys.disk_percent }];
    disks.forEach((dk) => {
      const name = (dk.mount || "").replace(/\\+$/, "") || "Disk";
      if (dk.percent >= 90) out.push({ sev: "bad", text: `${name} ${Math.round(dk.percent)}% full` });
    });
    if (sys.mem_percent >= 90) out.push({ sev: "bad", text: `Memory ${Math.round(sys.mem_percent)}%` });
    if (sys.cpu_percent >= 90) out.push({ sev: "bad", text: `CPU ${Math.round(sys.cpu_percent)}%` });
  }
  const apt = d.report && d.report.apt;
  if (d.apt_needs_update) out.push({ sev: "warn", text: apt && apt.upgradable != null ? `${apt.upgradable} updates` : "Updates pending" });
  if (apt && apt.reboot_required) out.push({ sev: "warn", text: "Reboot pending" });
  const ctrs = (d.report && d.report.docker_containers) || [];
  const down = ctrs.filter((c) => c.status !== "running").length;
  if (down) out.push({ sev: "warn", text: `${down} container${down > 1 ? "s" : ""} stopped` });
  return out.sort((a, b) => SEV_RANK[a.sev] - SEV_RANK[b.sev]);
}

function osFamily(d) { return d.os_key === "windows" ? "windows" : d.os_key ? "linux" : ""; }

const FLEET_FILTERS = [
  ["all", "All", () => true],
  ["online", "Online", (d) => d.is_online],
  ["offline", "Offline", (d) => !d.is_online],
  ["attention", "Needs attention", (d) => d._issues.length > 0],
  ["updates", "Updates", (d) => d.apt_needs_update],
  ["windows", "Windows", (d) => osFamily(d) === "windows"],
  ["linux", "Linux", (d) => osFamily(d) === "linux"],
];

function sparkPoints(values, w = 280, h = 36) {
  if (!values || values.length < 2) return "";
  const step = w / (values.length - 1);
  return values.map((v, i) => `${(i * step).toFixed(1)},${(h - 2 - (Math.min(100, v) / 100) * (h - 4)).toFixed(1)}`).join(" ");
}

function metricBlock(label, value, title) {
  if (value === null || value === undefined) {
    return `<div class="fc-metric"><span class="fc-mlabel">${label}</span><span class="fc-mval faint">—</span><span class="fc-bar"></span></div>`;
  }
  const p = Math.round(value);
  const cls = p >= 90 ? "crit" : p >= 70 ? "warn" : "ok";
  return `<div class="fc-metric" title="${escapeHtml(title || p + "%")}"><span class="fc-mlabel">${label}</span>` +
    `<span class="fc-mval ${cls}">${p}%</span><span class="fc-bar"><span class="${cls}" style="width:${Math.min(100, p)}%"></span></span></div>`;
}

function deviceCard(d) {
  const sys = d.report && d.report.system;
  const sub = (sys && (sys.os_version || (sys.hostname !== d.name ? sys.hostname : ""))) || (d.report ? "" : "Waiting for first report");
  // worst disk, labelled with its drive when there are several
  let disk = sys ? sys.disk_percent : null, diskLabel = "DISK", diskTip = "";
  if (sys && sys.disks && sys.disks.length > 1) {
    const worst = sys.disks.reduce((a, b) => (b.percent > a.percent ? b : a));
    disk = worst.percent;
    diskLabel = "DISK " + (worst.mount.replace(/\\+$/, "").includes(":") ? worst.mount.replace(/\\+$/, "") : "");
    diskTip = sys.disks.map((dk) => `${dk.mount.replace(/\\+$/, "") || dk.mount} ${Math.round(dk.percent)}%`).join(" · ");
  }
  const issues = d._issues;
  const chips = issues.slice(0, 2).map((i) => `<span class="chip ${i.sev}">${escapeHtml(i.text)}</span>`).join("") +
    (issues.length > 2 ? `<span class="chip neutral">+${issues.length - 2}</span>` : "");
  const ctrs = d.report && d.report.docker_containers;
  return `<article class="fleet-card${d.is_online ? "" : " offline"}" onclick="navTo('/devices/${d.id}')">
    <div class="fc-head">
      <span class="fc-os">${d.os_key ? `<span class="os-icon os-${d.os_key}"></span>` : ""}</span>
      <span class="fc-title">
        <a class="fc-name" href="/devices/${d.id}" onclick="event.stopPropagation()">${escapeHtml(d.name)}</a>
        <span class="fc-sub">${escapeHtml(sub)}</span>
      </span>
      <span class="fc-status ${d.is_online ? "on" : "off"}" title="${escapeHtml(fmtAbsolute(d.last_seen_at))}"><span class="dot"></span>${d.is_online ? "Online" : "Offline"}</span>
    </div>
    <div class="fc-metrics">
      ${metricBlock("CPU", sys ? sys.cpu_percent : null, sys && sys.cpu_cores ? `${Math.round(sys.cpu_percent)}% of ${sys.cpu_cores} cores` : "")}
      ${metricBlock("MEM", sys ? sys.mem_percent : null, sys && sys.mem_total_bytes ? `${gib(sys.mem_used_bytes)} / ${gib(sys.mem_total_bytes)}` : "")}
      ${metricBlock(diskLabel.trim(), disk, diskTip)}
    </div>
    <svg class="fc-spark" viewBox="0 0 280 36" preserveAspectRatio="none" aria-hidden="true"><polyline points="${sparkPoints(d.cpu_spark)}"></polyline></svg>
    ${chips ? `<div class="fc-issues">${chips}</div>` : ""}
    <div class="fc-foot">
      ${tailscaleLogo(d.report ? d.report.tailscale : null, !d.is_online)}
      ${ctrs && ctrs.length ? containerSummary(ctrs, !d.is_online) : ""}
      <span class="spacer"></span>
      <span class="fc-seen">${fmtRelative(d.seconds_since_seen)}</span>
      ${d.ssh_enabled ? `<button class="term-btn" title="Open terminal" aria-label="Open terminal on ${escapeHtml(d.name)}" data-term-id="${d.id}" data-term-name="${escapeHtml(d.name)}" onclick="event.stopPropagation(); openTerminalFromBtn(this)">${TERMINAL_ICON}</button>` : ""}
    </div>
  </article>`;
}

function renderFleet() {
  const grid = document.getElementById("device-grid");
  if (!grid) return;
  const devs = FLEET.devices;

  const filters = document.getElementById("device-filters");
  if (filters) {
    filters.innerHTML = FLEET_FILTERS.map(([key, label, fn]) => {
      const n = devs.filter(fn).length;
      if (!n && key !== "all" && key !== FLEET.filter) return "";
      return `<button type="button" class="filter-chip f-${key}${FLEET.filter === key ? " active" : ""}" aria-pressed="${FLEET.filter === key}" data-filter="${key}">${label} <b>${n}</b></button>`;
    }).join("");
  }

  const attention = document.getElementById("attention");
  if (attention) {
    const items = [];
    devs.forEach((d) => d._issues.forEach((i) => items.push({ d, i })));
    items.sort((a, b) => SEV_RANK[a.i.sev] - SEV_RANK[b.i.sev]);
    attention.hidden = items.length === 0;
    attention.innerHTML = items.slice(0, 6).map(({ d, i }) =>
      `<a class="attn ${i.sev}" href="/devices/${d.id}"><span class="attn-what">${escapeHtml(i.text)}</span><span class="attn-dev">${escapeHtml(d.name)}</span></a>`).join("");
  }

  const pred = (FLEET_FILTERS.find(([k]) => k === FLEET.filter) || FLEET_FILTERS[0])[2];
  const q = FLEET.query.trim().toLowerCase();
  const shown = devs.filter(pred).filter((d) => {
    if (!q) return true;
    const sys = d.report && d.report.system;
    return [d.name, d.notes, sys && sys.hostname, sys && sys.os_version, sys && sys.local_ip, d.report && d.report.tailscale && (d.report.tailscale.ips || []).join(" ")]
      .some((s) => s && String(s).toLowerCase().includes(q));
  });
  grid.innerHTML = devs.length === 0
    ? '<p class="muted fleet-empty">No devices yet. Use <b>+ Add device</b> to get an API key.</p>'
    : shown.length === 0 ? '<p class="muted fleet-empty">No devices match.</p>'
    : shown.map(deviceCard).join("");
}

function renderDeviceRows(devices) {
  // worst first: offline / red issues, then amber, then healthy; name within a group
  devices.forEach((d) => { d._issues = deviceIssues(d); });
  const rank = (d) => (d._issues.length ? SEV_RANK[d._issues[0].sev] : 2);
  FLEET.devices = devices.slice().sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  renderFleet();
  renderClientBanner();

  const aptCount = devices.filter((d) => d.apt_needs_update).length;
  const aptAlert = document.getElementById("apt-alert");
  if (aptAlert) {
    aptAlert.hidden = aptCount === 0;
    const n = document.getElementById("apt-alert-count");
    if (n) n.textContent = aptCount;
  }
}

const vtuple = (v) => ((v || "1.0.0").match(/\d+/g) || []).slice(0, 3).map(Number);
const vOlder = (a, b) => { const x = vtuple(a), y = vtuple(b); for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) < (y[i] || 0); return false; };

// "N clients on an old release · Update all"; the host works out the latest version.
function renderClientBanner() {
  const el = document.getElementById("client-banner");
  if (!el) return;
  if (!FLEET.latest) {
    // GitHub lookup failed: we can't tell who's behind, so offer to update everyone reachable.
    const ready = FLEET.devices.filter((d) => d.report && d.ssh_enabled && d.is_online);
    el.hidden = ready.length === 0;
    el.innerHTML = `<span class="cb-text"><span>Couldn't check for the latest client release</span>` +
      `<span class="cb-names">The host couldn't read it from GitHub; it retries every 5 minutes.</span></span>` +
      `<button type="button" class="secondary" onclick="updateAllClients(${ready.length})">Update all ${ready.length}</button>`;
    return;
  }
  const old = FLEET.devices.filter((d) => d.report && vOlder(d.agent_version, FLEET.latest));
  el.hidden = old.length === 0;
  if (!old.length) return;
  const ready = old.filter((d) => d.ssh_enabled && d.is_online);
  const names = old.map((d) => `${d.name} (${d.agent_version || "1.0.0"})`).join(", ");
  el.innerHTML = `<span class="cb-text"><span><b>${old.length} client${old.length > 1 ? "s" : ""}</b> on an old release · latest is ${escapeHtml(FLEET.latest)}</span>` +
    `<span class="cb-names">${escapeHtml(names)}</span></span>` +
    (ready.length ? `<button type="button" onclick="updateAllClients(${ready.length})">Update ${ready.length === old.length ? "all" : ready.length}</button>`
      : `<span class="muted cb-note">Enable SSH on them to update from here</span>`);
}

function updateAllClients(n) {
  if (!confirm(`Update the client on ${n} device${n > 1 ? "s" : ""}? Each one pulls the latest code and restarts its client.`)) return;
  fetch("/updates/client-all", { method: "POST" }).then((r) => r.json())
    .then((res) => { if (!res.started) alert("No updates started (devices offline or SSH unavailable)."); resumeUpdates(); })
    .catch(() => alert("Couldn't start the updates."));
}

function initFleet(devices, latest) {
  FLEET.latest = latest;
  if (new URLSearchParams(location.search).has("updates")) FLEET.filter = "updates";
  const filters = document.getElementById("device-filters");
  if (filters) filters.addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (b) { FLEET.filter = b.dataset.filter; renderFleet(); }
  });
  const search = document.getElementById("device-search");
  if (search) search.addEventListener("input", () => { FLEET.query = search.value; renderFleet(); });
  renderDeviceRows(devices);
}

function startDevicePolling(url, intervalMs = 5000) {
  const poll = () => fetch(url).then((r) => r.json()).then(renderDeviceRows).catch(() => {});
  PAGE_TIMERS.push(setInterval(poll, intervalMs));
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
  PAGE_TIMERS.push(setInterval(render, 10000));
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
  PAGE_TIMERS.push(setInterval(render, 15000));
  window.addEventListener("resize", drawAll);
}

/* ---------- in-browser SSH terminal ---------- */
var TERM = null, TERM_FIT = null, TERM_WS = null, TERM_DEVICE = null, TERM_CONTAINER = null;

function termStatus(text) {
  const el = document.getElementById("term-status");
  if (el) el.textContent = text;
}

function openTerminalFromBtn(btn) {
  openTerminal(btn.dataset.termId, btn.dataset.termName);
}

// docker exec -it <container> /bin/bash, over the same SSH terminal.
function openContainerShell(deviceId, deviceName, container) {
  openTerminal(deviceId, deviceName, container);
}

/* ---------- device update actions (async background jobs) ----------
   Each update becomes a "tracker": one poller (independent of the DOM) drives
   both the in-panel card on the device page AND a persistent bottom-centre
   overlay (in base.html, so boosted nav can't destroy it). When a job finishes
   it gets a 5s reverse-bar countdown before auto-dismissing from both places. */
var UPDATE_META = {
  apt: {
    label: "Package update",
    confirm: (n, os) => os === "windows"
      ? "Install pending Windows updates on " + n + "?\n\nRuns Windows Update as SYSTEM, in the background — you can leave this page. It never reboots; you'll be told if a restart is needed."
      : "Update all apt packages on " + n + "?\n\nRuns apt-get upgrade as root, in the background — you can leave this page.",
  },
  client: {
    label: "Client update",
    confirm: (n, os) => "Update the DeviceManager client on " + n + "?\n\n" + (os === "windows"
      ? "Downloads the latest client, refreshes its dependencies and restarts the scheduled task, in the background."
      : "Pulls the latest code and rebuilds/restarts the client, in the background."),
  },
};
var UPD_TRACK = {};                 // key "deviceId:kind" -> tracker
var UPD_CD_TIMER = null;            // drives the countdown reverse-bars
var UPD_OVERLAY_MIN = false;        // overlay minimised to a pill
var UPD_DISMISS_MS = 5000;

function fmtEta(s) {
  if (s === null || s === undefined) return "";
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
}

function startUpdate(deviceId, deviceName, kind, os) {
  const meta = UPDATE_META[kind];
  if (!meta || !confirm(meta.confirm(deviceName, os))) return;
  fetch(`/devices/${deviceId}/update/${kind}`, { method: "POST" })
    .then((r) => r.json())
    .then((res) => {
      if (res.job_id) trackUpdate({ device_id: deviceId, device_name: deviceName, kind, id: res.job_id });
      else alert("Couldn't start update: " + (res.detail || "unknown error"));
    })
    .catch(() => alert("Couldn't start update."));
}

function trackUpdate(job) {
  const key = job.device_id + ":" + job.kind;
  if (UPD_TRACK[key]) { UPD_TRACK[key].job = job; renderUpdaters(); return; }
  const t = UPD_TRACK[key] = {
    key, deviceId: job.device_id, deviceName: job.device_name, kind: job.kind,
    jobId: job.id, label: (UPDATE_META[job.kind] || {}).label || "Update",
    job, dismissAt: null,
  };
  pollTracker(t);
  renderUpdaters();
}

function pollTracker(t) {
  const tick = () => fetch(`/devices/${t.deviceId}/update/${t.jobId}.json`)
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((job) => {
      if (!UPD_TRACK[t.key]) return;         // dismissed while in flight
      t.job = job;
      if (job.status === "done" || job.status === "error") { startDismiss(t); return; }
      renderUpdaters();
      setTimeout(tick, 1000);
    })
    .catch(() => { if (UPD_TRACK[t.key]) setTimeout(tick, 2000); });
  tick();
}

function startDismiss(t) {
  if (t.dismissAt == null) t.dismissAt = Date.now() + UPD_DISMISS_MS;
  renderUpdaters();          // paint the finished card once; the timer only nudges widths
  ensureCountdownTimer();
}

// One lightweight timer narrows every countdown bar; it edits widths directly
// rather than re-rendering, so it stays smooth and cheap.
function ensureCountdownTimer() {
  if (UPD_CD_TIMER) return;
  UPD_CD_TIMER = setInterval(() => {
    let any = false;
    Object.values(UPD_TRACK).forEach((t) => {
      if (t.dismissAt == null) return;
      const remain = t.dismissAt - Date.now();
      if (remain <= 0) { removeTracker(t.key); return; }
      any = true;
      const w = (remain / UPD_DISMISS_MS) * 100 + "%";
      document.querySelectorAll(`.upd-cd-fill[data-cd="${t.key}"]`).forEach((f) => { f.style.width = w; });
    });
    if (!any) { clearInterval(UPD_CD_TIMER); UPD_CD_TIMER = null; }
  }, 60);
}

function dismissTracker(key) { removeTracker(key); }

function removeTracker(key) {
  delete UPD_TRACK[key];
  renderUpdaters();
}

/* ----- rendering ----- */
function renderUpdaters() {
  const host = document.getElementById("update-status");
  const onDevice = host ? Number(host.dataset.deviceId) : null;
  const overlayTrackers = [];
  if (host) host.innerHTML = "";
  Object.values(UPD_TRACK).forEach((t) => {
    if (host && onDevice === t.deviceId) {
      const box = document.createElement("div");
      box.dataset.upd = t.kind;
      box.innerHTML = updCardHtml(t, false);
      host.appendChild(box);
    } else {
      overlayTrackers.push(t);
    }
  });
  renderOverlay(overlayTrackers);
}

function renderOverlay(trackers) {
  const el = document.getElementById("update-overlay");
  if (!el) return;
  if (!trackers.length) { el.hidden = true; el.className = "update-overlay"; el.innerHTML = ""; return; }
  el.hidden = false;
  if (UPD_OVERLAY_MIN) {
    const running = trackers.filter((t) => t.dismissAt == null).length;
    el.className = "update-overlay min";
    el.innerHTML =
      `<button class="uov-pill" onclick="toggleOverlayMin()"><span class="upd-spin"></span>` +
      `${running ? running + " update" + (running > 1 ? "s" : "") + " running…" : "updates"}` +
      `<span class="uov-caret">▴</span></button>`;
    return;
  }
  el.className = "update-overlay";
  el.innerHTML =
    `<div class="uov-head"><span class="uov-title">Updates</span>` +
    `<button class="term-btn uov-min" title="Minimise" onclick="toggleOverlayMin()">▾</button></div>` +
    `<div class="uov-body">${trackers.map((t) => `<div data-upd="${t.kind}">${updCardHtml(t, true)}</div>`).join("")}</div>`;
}

function toggleOverlayMin() { UPD_OVERLAY_MIN = !UPD_OVERLAY_MIN; renderUpdaters(); }

// The card body — identical for in-panel and overlay; overlay adds the device name.
function updCardHtml(t, inOverlay) {
  const job = t.job || { status: "starting", percent: null, stage: "Starting…" };
  const label = escapeHtml(t.label) + (inOverlay ? ` · ${escapeHtml(t.deviceName || "")}` : "");
  if (job.status === "done" || job.status === "error") {
    const ok = job.status === "done";
    const cd = `<div class="upd-cd"><div class="upd-cd-fill ${ok ? "ok" : "bad"}" data-cd="${t.key}" style="width:100%"></div></div>`;
    const dismiss = `<button class="term-btn upd-dismiss" title="Dismiss now" onclick="dismissTracker('${t.key}')">✕</button>`;
    return `<div class="upd-card"><div class="upd-head">` +
      `<span class="chip ${ok ? "ok" : "bad"}"><span class="chip-dot"></span>${ok ? "Up to date" : "Failed"}</span>` +
      `<span class="upd-label">${label}</span>${dismiss}</div>` +
      (ok ? (job.stage && job.stage !== "Up to date" ? `<div class="upd-stage">${escapeHtml(job.stage)}</div>` : "")
          : `<div class="upd-stage upd-err">${escapeHtml(job.error || "Update failed")}</div>`) + cd + `</div>`;
  }
  const indet = job.percent === null || job.percent === undefined;
  const pct = indet ? 0 : job.percent;
  const eta = job.eta_seconds != null ? ` · ~${fmtEta(job.eta_seconds)} left` : "";
  return `<div class="upd-card"><div class="upd-head"><span class="upd-spin"></span>` +
    `<span class="upd-label">${label}</span></div>` +
    `<div class="upd-stage">${escapeHtml(job.stage || "Working…")}${eta}</div>` +
    `<div class="meter wide upd-meter${indet ? " indet" : ""}">` +
    `<div class="meter-fill ok" style="width:${indet ? 38 : pct}%"></div>` +
    `${indet ? "" : `<span class="meter-label">${pct}%</span>`}</div></div>`;
}

// Re-attach to any running job on page load (survives a hard reload too).
function resumeUpdates() {
  fetch("/updates.json").then((r) => r.json()).then((data) => {
    (data.jobs || []).forEach((job) => trackUpdate(job));
    renderUpdaters();
  }).catch(() => {});
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

function openTerminal(deviceId, deviceName, container) {
  if (typeof Terminal === "undefined") { alert("Terminal library failed to load."); return; }
  TERM_DEVICE = deviceId;
  TERM_CONTAINER = container || null;
  const title = document.getElementById("term-title");
  if (title) title.textContent = container ? ("EXEC · " + container + " @ " + deviceName) : ("SSH · " + deviceName);
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
  const containerParam = TERM_CONTAINER ? `&container=${encodeURIComponent(TERM_CONTAINER)}` : "";
  const ws = new WebSocket(`${proto}://${location.host}/devices/${TERM_DEVICE}/terminal?user=${encodeURIComponent(user)}${containerParam}`);
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
  if (env) env.textContent = `HOST_URL=${host}\nAPI_KEY=${key}`;
  if (!cmd) return;
  const clone = "git clone https://github.com/oliverw0/device-manager-client && cd device-manager-client\n";
  const platforms = {
    docker: [clone + `sudo HOST_URL=${host} API_KEY=${key} ./install.sh`,
      "Run on the machine. Writes .env, auto-enables the Tailscale mount if present, and starts the client in Docker."],
    native: [clone + `sudo HOST_URL=${host} API_KEY=${key} ./install-native.sh`,
      "For a Linux box without Docker: installs a venv and a systemd service."],
    // TLS 1.2 line: Windows PowerShell 5.1 on older Server builds can't reach GitHub without it.
    windows: [`$env:HOST_URL='${host}'; $env:API_KEY='${key}'\n` +
      "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'\n" +
      "irm https://raw.githubusercontent.com/oliverw0/device-manager-client/main/install.ps1 | iex",
      "Run in an elevated PowerShell (Run as Administrator). Say yes to the SSH step to enable the console."],
  };
  const hint = document.getElementById("install-hint");
  const tabs = document.querySelectorAll(".platform-tabs [data-platform]");
  const show = (p) => {
    if (!platforms[p]) p = "docker";
    [cmd.textContent, hint.textContent] = platforms[p];
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.platform === p));
  };
  tabs.forEach((t) => t.addEventListener("click", () => show(t.dataset.platform)));
  show(new URLSearchParams(location.search).get("platform") || cmd.dataset.default);
}

/* ---------- device page tabs ----------
   Buttons, not #links: boosted navigation would treat a hash link as a page
   load. The hash is still kept in the URL so reloads and redirects land on a tab. */
function showDeviceTab(name) {
  const panels = document.querySelectorAll(".dtab[data-tab]");
  if (!panels.length) return;
  if (![...panels].some((p) => p.dataset.tab === name)) name = "overview";
  panels.forEach((p) => { p.hidden = p.dataset.tab !== name; });
  document.querySelectorAll(".device-tabs [data-tab]").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on);
  });
  history.replaceState(history.state, "", name === "overview" ? location.pathname + location.search : "#" + name);
  window.dispatchEvent(new Event("resize"));  // container sparklines skip drawing while hidden
}

function initDeviceTabs() {
  const tabs = document.querySelector(".device-tabs");
  if (!tabs) return;
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (b) showDeviceTab(b.dataset.tab);
  });
  showDeviceTab(location.hash.slice(1));
}

function initAddDeviceDialog(openNow) {
  const dlg = document.getElementById("add-device-dialog");
  if (!dlg) return;
  const ssh = document.getElementById("add-ssh");
  const fields = document.getElementById("add-ssh-fields");
  ssh.addEventListener("change", () => { fields.hidden = !ssh.checked; });
  const form = document.getElementById("add-device-form");
  const interval = form.elements.report_interval_seconds;
  const offline = form.elements.offline_after_seconds;
  const check = () => offline.setCustomValidity(
    +offline.value > +interval.value ? "" : "Must be longer than the report interval.");
  interval.addEventListener("input", check);
  offline.addEventListener("input", check);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });  // backdrop click
  if (openNow) dlg.showModal();
}

function initSshProvisionCmd() {
  const el = document.getElementById("ssh-provision-cmd");
  if (!el) return;
  el.textContent =
    "mkdir -p ~/.ssh && curl -fsS " + location.origin +
    "/api/v1/ssh-pubkey >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys";
}

function initNav() {
  document.body.classList.toggle("nav-collapsed", localStorage.getItem("navCollapsed") === "1");
  document.documentElement.classList.remove("nav-preload-collapsed");
  const toggle = document.getElementById("nav-toggle");
  if (toggle) toggle.addEventListener("click", () => {
    const collapsed = document.body.classList.toggle("nav-collapsed");
    localStorage.setItem("navCollapsed", collapsed ? "1" : "0");
  });
}

/* ---------- boosted navigation ----------
   The app is a plain MPA, so a normal link click reloads the page and destroys
   the live xterm + WebSocket. To keep the terminal pane alive while navigating,
   we intercept same-origin link clicks on desktop and swap only <main> + the
   sidebar, leaving #terminal-pane untouched. Desktop only: on phones the
   terminal is fullscreen and normal navigation is fine. Falls back to a real
   navigation on any hiccup (cross-origin, non-GET target, fetch error). */
var PAGE_TIMERS = [];  // per-page intervals, cleared before each swap so they don't pile up

function isDesktop() { return window.matchMedia("(min-width: 721px)").matches; }

// For rows/elements that navigate via JS (not an <a>): boost on desktop.
function navTo(url) { if (isDesktop()) boostTo(url, true); else window.location = url; }

function runPageScripts(container) {
  container.querySelectorAll("script").forEach((old) => {
    const s = document.createElement("script");
    if (old.src) s.src = old.src; else s.textContent = old.textContent;
    document.body.appendChild(s);  // appending executes it
    document.body.removeChild(s);
  });
}

function boostTo(url, push) {
  fetch(url, { headers: { "X-Boost": "1" }, credentials: "same-origin" })
    .then((r) => { if (!r.ok || r.redirected) throw new Error("full-load"); return r.text(); })
    .then((html) => {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const newMain = doc.querySelector("main.container");
      const curMain = document.querySelector("main.container");
      if (!newMain || !curMain) throw new Error("no-main");

      PAGE_TIMERS.forEach(clearInterval);
      PAGE_TIMERS = [];

      curMain.replaceWith(newMain);
      const newNav = doc.querySelector("nav.sidebar");
      const curNav = document.querySelector("nav.sidebar");
      if (newNav && curNav) { curNav.replaceWith(newNav); initNav(); }
      document.title = doc.title;

      // Re-bind handlers that target the freshly swapped <main> content.
      initSshToggle();
      initSshProvisionCmd();
      initInstallCmd();
      const ps = doc.getElementById("page-scripts");
      if (ps) runPageScripts(ps);
      renderUpdaters();  // re-place in-flight update cards into the new page / overlay

      if (push) history.pushState({}, "", url);
      window.scrollTo(0, 0);
    })
    .catch(() => { window.location.href = url; });
}

function initBoost() {
  document.addEventListener("click", (e) => {
    if (!isDesktop()) return;
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    let url;
    try { url = new URL(a.href, location.href); } catch (_) { return; }
    if (url.origin !== location.origin) return;
    e.preventDefault();
    boostTo(url.pathname + url.search, true);
  });
  window.addEventListener("popstate", () => {
    if (isDesktop()) boostTo(location.pathname + location.search, false);
    else window.location.reload();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initNav();
  initBoost();
  resumeUpdates();
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

/* ---------- mobile container action sheet ----------
   On phones the container row is just a dot + name; tapping it slides up a bar
   with the action buttons, cloned from that row's (hidden) .c-actions. */
function openActionSheet(item) {
  const sheet = document.getElementById("action-sheet");
  if (!sheet) return;
  const nameEl = item.querySelector(".c-name");
  const actions = item.querySelector(":scope > summary .c-actions");
  document.getElementById("sheet-title").textContent = nameEl ? nameEl.textContent : "Container";
  const dst = document.getElementById("sheet-actions");
  dst.innerHTML = actions
    ? actions.innerHTML
    : '<p class="muted" style="margin:0">Enable SSH for this device to control containers.</p>';
  sheet.hidden = false;
  document.body.classList.add("sheet-open");
}

function closeActionSheet() {
  const sheet = document.getElementById("action-sheet");
  if (sheet) sheet.hidden = true;
  document.body.classList.remove("sheet-open");
}

function initContainerActionSheet() {
  // Tapping an action button (which opens the terminal/logs pane) dismisses the sheet.
  const dst = document.getElementById("sheet-actions");
  if (dst) dst.addEventListener("click", (e) => { if (e.target.closest("button")) closeActionSheet(); });
  // Intercept container-row taps on mobile so the <details> info doesn't toggle.
  document.addEventListener("click", (e) => {
    if (!window.matchMedia("(max-width: 720px)").matches) return;
    const summary = e.target.closest(".container-item > summary");
    if (!summary) return;
    e.preventDefault();
    openActionSheet(summary.parentElement);
  });
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
