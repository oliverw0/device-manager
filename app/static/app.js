// The app's own confirm(): "Title\n\nDetails" -> Promise<boolean>. Esc or Cancel = no.
// With a placeholder it also asks for a note: resolves to the text, or null on cancel.
function ask(msg, okText, danger, placeholder) {
  const d = document.getElementById("confirm-dialog");
  const [title, ...rest] = msg.split("\n\n");
  d.querySelector("#cd-title").textContent = title;
  const body = d.querySelector("#cd-body");
  body.textContent = rest.join("\n\n");
  body.hidden = !rest.length;
  const ok = d.querySelector("#cd-ok");
  ok.textContent = okText || "Confirm";
  ok.className = danger ? "danger" : "";
  const input = d.querySelector("#cd-input");
  input.hidden = placeholder == null;
  input.value = "";
  input.placeholder = placeholder || "";
  d.returnValue = "";
  d.showModal();
  if (!input.hidden) input.focus();
  return new Promise((res) => d.addEventListener("close", () => {
    const yes = d.returnValue === "ok";
    res(input.hidden ? yes : yes ? input.value : null);
  }, { once: true }));
}

// <form data-confirm="..." data-ok="Delete" data-danger> asks before submitting.
document.addEventListener("submit", (e) => {
  const f = e.target;
  if (!f.dataset || !f.dataset.confirm) return;
  e.preventDefault();
  ask(f.dataset.confirm, f.dataset.ok, "danger" in f.dataset).then((yes) => { if (yes) f.submit(); });
});

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Docker whale: stacked container blocks on a hull (fleet card footer).
// viewBox is centred on the drawn whale (x 1.6-23.4, y 2-19.8) so it isn't off-centre or clipped.
var DOCKER_ICON = '<svg class="docker-ico" viewBox="0.5 -1.1 24 24" width="17" height="17" fill="currentColor" aria-hidden="true"><path d="M4 8h2.6v2.4H4zM7.2 8h2.6v2.4H7.2zM10.4 8H13v2.4h-2.6zM7.2 5h2.6v2.4H7.2zM10.4 5H13v2.4h-2.6zM13.6 8h2.6v2.4h-2.6zM10.4 2h2.6v2.4h-2.6z"/><path d="M1.6 11.2h17.6c.5-1.4.3-2.6-.4-3.4l.6-.4c.9.6 1.5 1.5 1.6 2.4.9-.2 1.7 0 2.4.5-.5 1-1.4 1.5-2.6 1.5-1.4 4.6-5 8-10.2 8-4.9 0-8.1-3-9-8.6z"/></svg>';
var TERMINAL_ICON ='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>';

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
var FLEET = { devices: [], query: "" };

const SEV_RANK = { bad: 0, warn: 1 };

// What's wrong with a device, worst first: [{sev: "bad"|"warn", text}].
function deviceIssues(d) {
  const out = [];
  const sys = d.report && d.report.system;
  if (!d.is_online) return [{ id: "offline", sev: "bad", text: d.last_seen_at ? `Offline ${fmtRelative(d.seconds_since_seen).replace(" ago", "")}` : "Never reported" }];
  if (sys) {
    const disks = sys.disks && sys.disks.length ? sys.disks : [{ mount: "Disk", percent: sys.disk_percent }];
    disks.forEach((dk) => {
      const name = (dk.mount || "").replace(/\\+$/, "") || "Disk";
      if (dk.percent >= 90) out.push({ id: "disk:" + name, inspect: dk.mount, sev: "bad", text: `${name} ${Math.round(dk.percent)}% full` });
    });
    if (sys.mem_percent >= 90) out.push({ id: "mem", sev: "bad", text: `Memory ${Math.round(sys.mem_percent)}%` });
    if (sys.cpu_percent >= 90) out.push({ id: "cpu", sev: "bad", text: `CPU ${Math.round(sys.cpu_percent)}%` });
  }
  const apt = d.report && d.report.apt;
  // Flagged with a count of 0 means the apt cache is old, not that packages are pending.
  if (d.apt_needs_update) out.push({ id: "updates", sev: "warn", text: apt && apt.upgradable ? `${apt.upgradable} updates` : apt && apt.upgradable === 0 ? "Package list stale" : "Updates pending" });
  if (apt && apt.reboot_required) out.push({ id: "reboot", sev: "warn", text: "Reboot pending" });
  const ctrs = (d.report && d.report.docker_containers) || [];
  const down = ctrs.filter((c) => c.status !== "running").length;
  if (down) out.push({ id: "containers", sev: "warn", text: `${down} container${down > 1 ? "s" : ""} stopped` });
  return out.sort((a, b) => SEV_RANK[a.sev] - SEV_RANK[b.sev]);
}


/* ---------- time axis for every graph ----------
   Samples sit at their real time, not evenly by index, and a pause longer than
   3 typical report intervals (at least 2.5 min) is a gap: the line breaks there
   instead of joining the last reading before an outage to the first after it,
   leaving that stretch blank. */
function toMs(iso) { const d = parseUtc(iso); return d ? d.getTime() : 0; }

// range: optional [start, end] ms, so graphs over the same history share one
// time scale (a series with no data for part of it is blank there).
function timeAxis(times, x0, x1, range) {
  const n = times.length;
  const t0 = range ? range[0] : times[0];
  const span = Math.max(1, (range ? range[1] : times[n - 1]) - t0);
  const xs = times.map((t) => x0 + ((t - t0) / span) * (x1 - x0));
  const d = times.slice(1).map((t, i) => t - times[i]).sort((a, b) => a - b);
  const limit = Math.max((d[Math.floor(d.length / 2)] || 60000) * 3, 150000);
  const gaps = [];  // i where a gap sits between sample i-1 and i
  for (let i = 1; i < n; i++) if (times[i] - times[i - 1] > limit) gaps.push(i);
  // data that starts after / ends before the shared range: edges to mark too
  const edges = [];
  if (range && times[0] - range[0] > limit) edges.push(xs[0]);
  if (range && range[1] - times[n - 1] > limit) edges.push(xs[n - 1]);
  return { xs, gaps, edges, breaks: new Set(gaps) };
}

// Split points into unbroken runs (for polylines / filled areas).
function timeRuns(xs, ys, breaks) {
  const runs = [[]];
  xs.forEach((x, i) => {
    if (breaks.has(i)) runs.push([]);
    runs[runs.length - 1].push([x, ys[i]]);
  });
  return runs.filter((r) => r.length);
}

// Fleet card CPU sparkline: [{t, v}] -> SVG polylines, broken at gaps.
function sparkSvg(points, w = 280, h = 36) {
  if (!points || points.length < 2) return "";
  const ax = timeAxis(points.map((p) => toMs(p.t)), 0, w);
  const ys = points.map((p) => h - 2 - (Math.min(100, p.v) / 100) * (h - 4));
  return timeRuns(ax.xs, ys, ax.breaks)
    .map((run) => `<polyline points="${run.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}"></polyline>`).join("");
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

// Tailscale state for the card: [chip class, text].
function tailscaleState(ts, offline) {
  if (!ts || ts.backend_state === "not_installed") return ["neutral", "Not set up"];
  if (offline) return ["bad", "Unknown"];
  if (ts.connected) return ["ok", "Connected"];
  return [ts.backend_state === "NeedsLogin" || ts.backend_state === "Stopped" ? "warn" : "bad", ts.backend_state || "Down"];
}

const ICON_LAN = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="6"/><rect x="2" y="16" width="6" height="6"/><rect x="16" y="16" width="6" height="6"/><path d="M12 8v4M5 16v-4h14v4"/></svg>';
const ICON_BOLT = '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true"><path d="M13 2L4 14h7l-1 8 9-12h-7z"/></svg>';
const ICON_OPEN = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17L17 7M8 7h9v9"/></svg>';

function cardLine(icon, label, value) {
  return `<div class="fc-line"><span class="fc-line-ico">${icon}</span><span class="fc-line-label">${label}</span><span class="fc-line-val">${value}</span></div>`;
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
  const offline = !d.is_online;
  const ip = (sys && sys.local_ip) || (d.report ? d.last_report_ip : "") || "";
  const ts = d.report ? d.report.tailscale : null;
  const [tsCls, tsTxt] = tailscaleState(ts, offline);
  const conn = [offline ? "bad" : "ok", tsCls].filter((c) => c === "bad" || c === "warn");
  const connSummary = conn.length ? `<span class="${conn.includes("bad") ? "bad" : "warn"}">${conn.length} issue${conn.length > 1 ? "s" : ""}</span>` : `<span class="ok">All connected</span>`;

  const ctrs = (d.report && d.report.docker_containers) || [];
  const running = ctrs.filter((c) => c.status === "running").length;
  const power = d.report && d.report.power;
  const work = [];
  if (ctrs.length) work.push(cardLine(DOCKER_ICON, "Docker containers",
    `<b class="${offline ? "" : running === ctrs.length ? "" : running ? "warn" : "bad"}">${offline ? "—" : `${running}/${ctrs.length} running`}</b>`));
  if (power) work.push(cardLine(ICON_BOLT, "Power", `<b>${Math.round(power.wall_w)} W</b>`));

  return `<article class="fleet-card${offline ? " offline" : ""}" onclick="navTo('/devices/${d.id}')">
    <div class="fc-head">
      <span class="fc-os">${d.os_key ? `<span class="os-icon os-${d.os_key}"></span>` : ""}</span>
      <span class="fc-title">
        <a class="fc-name" href="/devices/${d.id}" onclick="event.stopPropagation()">${escapeHtml(d.name)}</a>
        <span class="fc-sub">${escapeHtml(sub)}</span>
      </span>
      <span class="chip ${offline ? "bad" : "ok"}" title="${escapeHtml(fmtAbsolute(d.last_seen_at))}">${offline ? "Offline" : "Online"}</span>
    </div>
    <div class="fc-metrics">
      ${metricBlock("CPU", sys ? sys.cpu_percent : null, sys && sys.cpu_cores ? `${Math.round(sys.cpu_percent)}% of ${sys.cpu_cores} cores` : "")}
      ${metricBlock("MEM", sys ? sys.mem_percent : null, sys && sys.mem_total_bytes ? `${gib(sys.mem_used_bytes)} / ${gib(sys.mem_total_bytes)}` : "")}
      ${metricBlock(diskLabel.trim(), disk, diskTip)}
    </div>
    <section class="fc-sec">
      <h3 class="fc-sec-head">Connectivity${connSummary}</h3>
      ${cardLine(ICON_LAN, `LAN${ip ? ` <span class="fc-ip">${escapeHtml(ip)}</span>` : ""}`, `<span class="chip ${offline ? "bad" : "ok"}">${offline ? "Offline" : "Connected"}</span>`)}
      ${cardLine(`<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true"><path d="${TS_PATH}"/></svg>`, "Tailscale", `<span class="chip ${tsCls}">${escapeHtml(tsTxt)}</span>`)}
    </section>
    ${work.length ? `<section class="fc-sec"><h3 class="fc-sec-head">Workloads</h3>${work.join("")}</section>` : ""}
    <div class="fc-foot">
      <span class="fc-seen">Last report ${fmtRelative(d.seconds_since_seen)}</span>
      <span class="fc-foot-r">
        ${d.ssh_enabled ? `<button class="term-btn" title="Open terminal" aria-label="Open terminal on ${escapeHtml(d.name)}" data-term-id="${d.id}" data-term-name="${escapeHtml(d.name)}" onclick="event.stopPropagation(); openTerminalFromBtn(this)">${TERMINAL_ICON}</button>` : ""}
        <a class="term-btn fc-open" href="/devices/${d.id}" aria-label="Open ${escapeHtml(d.name)}" title="Open device" onclick="event.stopPropagation()">${ICON_OPEN}</a>
      </span>
    </div>
  </article>`;
}

function renderFleet() {
  const grid = document.getElementById("device-grid");
  if (!grid) return;
  const devs = FLEET.devices;

  const q = FLEET.query.trim().toLowerCase();
  const shown = devs.filter((d) => {
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
  const bannerKey = "banner:" + (FLEET.latest || "unknown");
  const bannerX = `<button type="button" class="attn-x tip-below tip-end" data-dismiss="${bannerKey}" data-tip="Dismiss until the next release" aria-label="Dismiss">✕</button>`;
  if (DISMISSED[bannerKey]) { el.hidden = true; return; }
  if (!FLEET.latest) {
    // GitHub lookup failed: we can't tell who's behind, so offer to update everyone reachable.
    const ready = FLEET.devices.filter((d) => d.report && d.ssh_enabled && d.is_online);
    el.hidden = ready.length === 0;
    el.innerHTML = `<span class="cb-text"><span>Couldn't check for the latest client release</span>` +
      `<span class="cb-names">The host couldn't read it from GitHub; it retries every 5 minutes.</span></span>` +
      `<button type="button" class="secondary" onclick="updateAllClients(${ready.length})">Update all ${ready.length}</button>` + bannerX;
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
      : `<span class="muted cb-note">Enable SSH on them to update from here</span>`) + bannerX;
}

// force: update every online, SSH-enabled device whatever version it reports.
async function updateAllClients(n, force) {
  const who = force ? "every online device with SSH enabled" : `${n} device${n > 1 ? "s" : ""}`;
  if (!(await ask(`Update the client on ${who}?\n\nEach one pulls the latest code and restarts its client.`, "Update"))) return;
  fetch("/updates/client-all" + (force ? "?force=1" : ""), { method: "POST" }).then((r) => r.json())
    .then((res) => { if (!res.started) alert("No updates started (devices offline or SSH unavailable)."); resumeUpdates(); })
    .catch(() => alert("Couldn't start the updates."));
}

// Dismissed front-page alerts, per browser: { "<deviceId>:<issueId>": 1, "banner:<version>": 1 }.
var DISMISSED = (() => { try { return JSON.parse(localStorage.getItem("dm-dismissed")) || {}; } catch (_) { return {}; } })();
function saveDismissed() { try { localStorage.setItem("dm-dismissed", JSON.stringify(DISMISSED)); } catch (_) {} }

function initFleet(devices, latest) {
  FLEET.latest = latest;
  const banner = document.getElementById("client-banner");
  if (banner) banner.addEventListener("click", (e) => {
    const x = e.target.closest("[data-dismiss]");
    if (x) { DISMISSED[x.dataset.dismiss] = 1; saveDismissed(); renderClientBanner(); }
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

function chartY(v, h, max = 100) { return h - 4 - (Math.min(v, max) / max) * (h - 8); }

// A round top for a watts axis, so its quarter lines land on tidy numbers.
function niceMax(v) {
  const step = [5, 10, 20, 25, 50, 100, 200, 250, 500].find((s) => s * 4 >= v * 1.1) || Math.ceil(v / 400) * 100;
  return step * 4;
}

// opts: { series, max } — defaults are the CPU / memory / disk percent chart.
function drawChart(canvas, rows, hoverIndex, opts = {}) {
  const series = opts.series || CHART_SERIES, max = opts.max || 100, range = opts.range || null;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  // reference lines at 1/4, 1/2 and 3/4 of the scale, on the same y as the data
  // (snapped to the pixel grid so they're crisp, not a blurred half-pixel)
  ctx.strokeStyle = "rgba(255,255,255,0.09)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = Math.round(chartY((max * i) / 4, h, max)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  if (rows.length < 2) return null;
  const ax = timeAxis(rows.map((r) => toMs(r.timestamp)), 4, w - 4, range);

  // a gap (no reports) is left empty, edged by dotted grey lines where data
  // stopped and where it resumed
  ctx.save();
  ctx.strokeStyle = "rgba(255,255,255,0.22)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  const marks = ax.gaps.flatMap((i) => [ax.xs[i - 1], ax.xs[i]]).concat(ax.edges);
  marks.forEach((x) => {
    const px = Math.round(x) + 0.5;
    ctx.beginPath();
    ctx.moveTo(px, 0);
    ctx.lineTo(px, h);
    ctx.stroke();
  });
  ctx.restore();

  series.forEach((s) => {
    ctx.beginPath();
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.8;
    rows.forEach((r, i) => {
      const x = ax.xs[i];
      const y = chartY(r[s.key], h, max);
      if (i === 0 || ax.breaks.has(i)) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });

  // hover guide line + highlight dots
  if (hoverIndex != null && rows[hoverIndex]) {
    const x = ax.xs[hoverIndex];
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
    series.forEach((s) => {
      const y = chartY(rows[hoverIndex][s.key], h, max);
      ctx.beginPath();
      ctx.fillStyle = s.color;
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#0d0f13";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    });
  }
  return ax;
}

// opts: { series, unit, scale(rows) -> max, onData(rows, max) }
function loadHistoryChart(url, canvasId, opts = {}) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const series = opts.series || CHART_SERIES, unit = opts.unit || "%";
  const wrap = canvas.parentElement;
  wrap.style.position = wrap.style.position || "relative";

  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.style.display = "none";
  wrap.appendChild(tip);

  const state = { rows: [], hoverIndex: null, ax: null, max: 100, range: null };
  const draw = () => { state.ax = drawChart(canvas, state.rows, state.hoverIndex, { series, max: state.max, range: state.range }); };

  function indexFromEvent(e) {
    const rect = canvas.getBoundingClientRect();
    // The canvas renders at a fixed internal width but is displayed scaled
    // (max-width:100%), so map the pointer using the displayed rect width.
    const px = ((e.clientX - rect.left) / rect.width) * canvas.width;
    if (!state.ax) return 0;
    // nearest sample in time (x is time-based, so not simply ratio * count)
    let best = 0;
    state.ax.xs.forEach((x, i) => { if (Math.abs(x - px) < Math.abs(state.ax.xs[best] - px)) best = i; });
    return best;
  }

  function showTip(e, idx) {
    const row = state.rows[idx];
    if (!row) return;
    const when = parseUtc(row.timestamp);
    const rowsHtml = series.map((s) =>
      `<div class="tip-row"><span class="tip-dot" style="background:${s.color}"></span>${s.label}<b>${Math.round(row[s.key])}${unit}</b></div>`
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
    // the time range comes from the whole history (every chart of this device
    // fetches the same rows), so Hardware usage and Power line up in time
    state.range = rows.length > 1 ? [toMs(rows[0].timestamp), toMs(rows[rows.length - 1].timestamp)] : null;
    state.rows = rows.filter((r) => series.every((s) => r[s.key] != null));
    state.max = opts.scale ? opts.scale(state.rows) : 100;
    if (opts.onData) opts.onData(state.rows, state.max);
    draw();
  }).catch(() => {});
  render();
  PAGE_TIMERS.push(setInterval(render, 10000));
}

/* ---------- per-container background sparklines ---------- */
var CONTAINER_SPARK_COLORS = { cpu: "255,82,48", mem: "55,224,106" };

// points: [{t (ms), v}]; areas and lines break where reports stopped.
function drawContainerSpark(canvas, points, metric) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (points.length < 2) return;

  const rgb = CONTAINER_SPARK_COLORS[metric] || CONTAINER_SPARK_COLORS.cpu;
  const ax = timeAxis(points.map((p) => p.t), 0, w);
  const ys = points.map((p) => h - (Math.max(0, Math.min(100, p.v)) / 100) * (h - 3) - 1);
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, `rgba(${rgb},0.38)`);
  grad.addColorStop(1, `rgba(${rgb},0.03)`);

  timeRuns(ax.xs, ys, ax.breaks).forEach((run) => {
    // filled area
    ctx.beginPath();
    ctx.moveTo(run[0][0], h);
    run.forEach(([x, y]) => ctx.lineTo(x, y));
    ctx.lineTo(run[run.length - 1][0], h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
    // line
    ctx.beginPath();
    run.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
    ctx.strokeStyle = `rgba(${rgb},0.6)`;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });
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
        .map((p) => ({ t: toMs(p.timestamp), v: state.metric === "cpu" ? p.cpu_percent : p.mem_percent }))
        .filter((p) => p.v != null);
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

async function startUpdate(deviceId, deviceName, kind, os) {
  const meta = UPDATE_META[kind];
  if (!meta || !(await ask(meta.confirm(deviceName, os), "Update"))) return;
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
  const existing = UPD_TRACK[key];
  if (existing && existing.jobId === job.id) { if (existing.dismissAt == null) existing.job = job; renderUpdaters(); return; }
  // a NEW job for a device whose last one is still on screen replaces that card
  if (existing) delete UPD_TRACK[key];
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

/* ----- rendering -----
   Cards are patched in place, never rebuilt on a poll: every tracker polls each
   second, and rebuilding all cards each time restarted the sweep animation and
   snapped finished cards' countdown bars back to full ("progress resets"). A
   card is only rebuilt when it switches between running / indeterminate /
   finished. */
function renderUpdaters() {
  const host = document.getElementById("update-status");
  const onDevice = host ? Number(host.dataset.deviceId) : null;
  const all = Object.values(UPD_TRACK);
  if (host) syncCards(host, all.filter((t) => t.deviceId === onDevice), false);
  renderOverlay(host ? all.filter((t) => t.deviceId !== onDevice) : all);
}

function cardMode(t) {
  const job = t.job || {};
  if (job.status === "done" || job.status === "error") return "end";
  return job.percent === null || job.percent === undefined ? "indet" : "run";
}

function syncCards(container, trackers, inOverlay) {
  const want = new Set(trackers.map((t) => t.key));
  [...container.children].forEach((el) => { if (!want.has(el.dataset.key)) el.remove(); });
  trackers.forEach((t) => {
    const mode = cardMode(t);
    const el = [...container.children].find((c) => c.dataset.key === t.key);
    if (el && el.dataset.mode === mode) {
      if (mode !== "end") patchRunningCard(el, t);  // finished cards: the countdown timer owns them
      return;
    }
    const card = document.createElement("div");
    card.dataset.key = t.key;
    card.dataset.upd = t.kind;
    card.dataset.mode = mode;
    card.innerHTML = updCardHtml(t, inOverlay);
    if (el) el.replaceWith(card); else container.appendChild(card);
  });
}

function patchRunningCard(el, t) {
  const job = t.job || {};
  const stage = el.querySelector(".upd-stage");
  const text = (job.stage || "Working…") + (job.eta_seconds != null ? ` · ~${fmtEta(job.eta_seconds)} left` : "");
  if (stage && stage.textContent !== text) stage.textContent = text;
  if (job.percent === null || job.percent === undefined) return;
  const fill = el.querySelector(".meter-fill");
  const label = el.querySelector(".meter-label");
  if (fill) fill.style.width = job.percent + "%";
  if (label) label.textContent = job.percent + "%";
}

function renderOverlay(trackers) {
  const el = document.getElementById("update-overlay");
  if (!el) return;
  if (!trackers.length) { el.hidden = true; el.className = "update-overlay"; el.innerHTML = ""; el.dataset.view = ""; return; }
  el.hidden = false;
  if (UPD_OVERLAY_MIN) {
    const running = trackers.filter((t) => t.dismissAt == null).length;
    const html =
      `<button class="uov-pill" onclick="toggleOverlayMin()"><span class="upd-spin"></span>` +
      `${running ? running + " update" + (running > 1 ? "s" : "") + " running…" : "updates"}` +
      `<span class="uov-caret">▴</span></button>`;
    if (el.dataset.view !== "min" || el.dataset.pill !== html) {  // keep the spinner running between polls
      el.className = "update-overlay min";
      el.innerHTML = html;
      el.dataset.view = "min";
      el.dataset.pill = html;
    }
    return;
  }
  if (el.dataset.view !== "full") {
    el.className = "update-overlay";
    el.innerHTML =
      `<div class="uov-head"><span class="uov-title">Updates</span>` +
      `<button class="term-btn uov-min" title="Minimise" onclick="toggleOverlayMin()">▾</button></div>` +
      `<div class="uov-body"></div>`;
    el.dataset.view = "full";
  }
  syncCards(el.querySelector(".uov-body"), trackers, true);
}

function toggleOverlayMin() { UPD_OVERLAY_MIN = !UPD_OVERLAY_MIN; renderUpdaters(); }

// The card body — identical for in-panel and overlay; overlay adds the device name.
function updCardHtml(t, inOverlay) {
  const job = t.job || { status: "starting", percent: null, stage: "Starting…" };
  const label = escapeHtml(t.label) + (inOverlay ? ` · ${escapeHtml(t.deviceName || "")}` : "");
  if (job.status === "done" || job.status === "error") {
    const ok = job.status === "done";
    // start the countdown bar where it really is, in case the card is (re)drawn mid-countdown
    const left = t.dismissAt ? Math.max(0, Math.min(100, ((t.dismissAt - Date.now()) / UPD_DISMISS_MS) * 100)) : 100;
    const cd = `<div class="upd-cd"><div class="upd-cd-fill ${ok ? "ok" : "bad"}" data-cd="${t.key}" style="width:${left}%"></div></div>`;
    const dismiss = `<button class="term-btn upd-dismiss" title="Dismiss now" onclick="dismissTracker('${t.key}')">✕</button>`;
    const doneText = t.kind === "client" ? "Updated" : "Up to date";
    return `<div class="upd-card"><div class="upd-head">` +
      `<span class="chip ${ok ? "ok" : "bad"}"><span class="chip-dot"></span>${ok ? doneText : "Failed"}</span>` +
      `<span class="upd-label">${label}</span>${dismiss}</div>` +
      (ok ? (job.stage && !["Up to date", "Client updated"].includes(job.stage) ? `<div class="upd-stage">${escapeHtml(job.stage)}</div>` : "")
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

/* ---------- "What's using space?" drawer ----------
   Scans one drive over SSH (POST /devices/<id>/disk-usage) and lists the
   largest folders, expandable one level, plus safe cleanups with their size. */
function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return (n / Math.pow(1024, i)).toFixed(i >= 3 ? 1 : 0) + " " + u[i];
}

var DISK = null;  // { deviceId, deviceName, btn, scan }

function openDiskDrawer(btn) {
  const storage = btn.closest("[data-device-id]");
  DISK = { deviceId: storage.dataset.deviceId, deviceName: storage.dataset.deviceName, btn, scan: null };
  const dlg = document.getElementById("disk-drawer");
  document.getElementById("disk-drawer-title").textContent = `What's using space on ${btn.dataset.name}`;
  dlg.showModal();
  scanDisk();
}

function diskMeterHtml() {
  const b = DISK.btn.dataset, pct = Number(b.pct), used = Number(b.used), total = Number(b.total);
  const cls = pct >= 90 ? "crit" : pct >= 70 ? "warn" : "ok";
  return `<div class="dd-meter"><span class="dd-meter-top"><span class="dd-${cls}">${total ? `${fmtBytes(used)} of ${fmtBytes(total)} · ` : ""}${pct}%</span>` +
    `${total ? `<span class="muted">${fmtBytes(total - used)} free</span>` : ""}</span>` +
    `<span class="fc-bar"><span class="${cls}" style="width:${Math.min(100, pct)}%"></span></span></div>`;
}

function scanDisk() {
  const body = document.getElementById("disk-drawer-body");
  const windows = /^[A-Za-z]:/.test(DISK.btn.dataset.name);
  body.innerHTML = diskMeterHtml() +
    `<div class="dd-scanning"><span class="upd-spin"></span>Scanning ${escapeHtml(DISK.btn.dataset.name)} over SSH…` +
    `<span class="muted">${windows ? "Windows drives can take a minute or two." : "Large drives can take a minute."}</span></div>`;
  const started = DISK;
  fetch(`/devices/${DISK.deviceId}/disk-usage?mount=${encodeURIComponent(DISK.btn.dataset.inspect)}`, { method: "POST" })
    .then((r) => r.json().then((j) => ({ ok: r.ok, j })))
    .then(({ ok, j }) => {
      if (DISK !== started) return;  // closed / reopened meanwhile
      if (!ok) { body.innerHTML = diskMeterHtml() + `<p class="dd-error">${escapeHtml(j.detail || "Scan failed.")}</p><button type="button" class="secondary" onclick="scanDisk()">Try again</button>`; return; }
      DISK.scan = j;
      renderDiskScan();
    })
    .catch(() => { body.innerHTML = diskMeterHtml() + '<p class="dd-error">Couldn\'t reach the host.</p>'; });
}

function renderDiskScan() {
  const s = DISK.scan, max = Math.max(1, ...s.items.map((i) => i.bytes));
  const row = (item, depth, idx) => {
    const kids = depth === 0 && item.children && item.children.length;
    return `<div class="dd-row${depth ? " dd-child" : ""}" ${kids ? `data-expand="${idx}"` : ""}>` +
      `<span class="dd-chev">${kids ? '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 6l6 6-6 6"></path></svg>' : ""}</span>` +
      `<span class="dd-path"><span title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</span>` +
      `<span class="dd-bar"><span style="width:${Math.max(1, Math.round((item.bytes / max) * 100))}%"></span></span></span>` +
      `<span class="dd-size">${fmtBytes(item.bytes)}</span></div>` +
      (kids ? `<div class="dd-kids" id="dd-kids-${idx}" hidden>${item.children.map((c) => row(c, 1)).join("")}</div>` : "");
  };
  const total = s.cleanups.reduce((a, c) => a + c.bytes, 0);
  const when = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  document.getElementById("disk-drawer-body").innerHTML = diskMeterHtml() +
    `<p class="dd-meta">Scanned ${when} over SSH · took ${s.took}s · <button type="button" class="linkish" onclick="scanDisk()">Rescan</button></p>` +
    `<section class="dd-section"><h3>Largest folders</h3>${s.items.map((i, n) => row(i, 0, n)).join("")}</section>` +
    (s.cleanups.length ? `<section class="dd-section dd-clean"><h3>Safe cleanups <span class="dd-free">could free ${fmtBytes(total)}</span></h3>` +
      s.cleanups.map((c) => `<div class="dd-crow"><span class="dd-cwhat"><span>${escapeHtml(c.what)}</span><span class="muted">${escapeHtml(c.how)}</span></span>` +
        `<span class="dd-size">${fmtBytes(c.bytes)}</span>` +
        `<button type="button" class="secondary" data-cleanup="${c.key}">${c.key === "recycle" ? "Empty" : c.key.startsWith("docker") ? "Prune" : "Clean"}</button></div>`).join("") +
      `<p class="dd-note">Runs over SSH. Nothing is deleted until you confirm, and the confirmation says exactly what runs.</p></section>` : "");
}

async function runCleanup(btn) {
  const c = DISK.scan.cleanups.find((x) => x.key === btn.dataset.cleanup);
  if (!c || !(await ask(`Run "${c.how}" on ${DISK.deviceName}?\n\n${c.note}\nFrees about ${fmtBytes(c.bytes)}.`, "Run", true))) return;
  btn.disabled = true;
  btn.textContent = "Running…";
  fetch(`/devices/${DISK.deviceId}/cleanup/${c.key}`, { method: "POST" })
    .then((r) => r.json().then((j) => ({ ok: r.ok, j })))
    .then(({ ok, j }) => {
      if (!ok) { btn.disabled = false; btn.textContent = "Retry"; alert(j.detail || "Cleanup failed."); return; }
      btn.textContent = "Done";
      btn.classList.add("dd-done");
    })
    .catch(() => { btn.disabled = false; btn.textContent = "Retry"; });
}

function initDiskDrawer() {
  const dlg = document.getElementById("disk-drawer");
  if (!dlg || dlg.dataset.bound) return;
  dlg.dataset.bound = "1";
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) { dlg.close(); return; }  // backdrop
    const exp = e.target.closest("[data-expand]");
    if (exp) {
      const kids = document.getElementById("dd-kids-" + exp.dataset.expand);
      kids.hidden = !kids.hidden;
      exp.classList.toggle("open", !kids.hidden);
      return;
    }
    const clean = e.target.closest("[data-cleanup]");
    if (clean && !clean.disabled) runCleanup(clean);
  });
  dlg.addEventListener("close", () => { DISK = null; });
  // /devices/<id>?inspect=<mount> (from a full-disk alert) opens the drawer straight away
  const want = new URLSearchParams(location.search).get("inspect");
  if (want !== null) {
    const btn = [...document.querySelectorAll("[data-inspect]")].find((b) => b.dataset.inspect === want || b.dataset.name === want);
    if (btn) openDiskDrawer(btn);
  }
}

/* ---------- power tracking ---------- */
// Wall watts over time: the same chart as History, on a watts scale.
function loadPowerSpark() {
  const panel = document.querySelector(".panel.power[data-history]");
  if (!panel || !document.getElementById("power-chart")) return;
  loadHistoryChart(panel.dataset.history, "power-chart", {
    series: [{ key: "power_w", label: "Power", color: "#ffb038" }],
    unit: " W",
    scale: (rows) => niceMax(Math.max(1, ...rows.map((r) => r.power_w))),
  });
}

// The Power panel's switch. On installs/starts the exporter on the machine; off
// stops and disables it there. Windows also picks the PSU curve the estimate uses.
function initPowerToggle() {
  const panel = document.querySelector(".panel.power[data-device-id]");
  const sw = document.getElementById("power-toggle");
  if (!panel || !sw || sw.dataset.bound) return;
  sw.dataset.bound = "1";
  const id = panel.dataset.deviceId;
  const psu = document.getElementById("power-psu");
  const status = (html, bad) => {
    const el = document.getElementById("pt-status");
    el.innerHTML = html;
    el.classList.toggle("bad", !!bad);
  };
  const busy = (on) => { sw.disabled = on; if (psu) psu.disabled = on; };
  const post = (action) => fetch(`/devices/${id}/power/${action}?psu=${encodeURIComponent(psu ? psu.value : "generic-gold")}`, { method: "POST" })
    .then((r) => r.json().then((j) => ({ ok: r.ok, j })));

  // after starting, wait for the client's next report to carry a reading, then redraw the page
  const waitForReading = () => {
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      fetch("/devices.json").then((r) => r.json()).then((devs) => {
        const d = devs.find((x) => String(x.id) === id);
        if (d && d.report && d.report.power) { clearInterval(timer); location.reload(); }
        else if (tries >= 36) {
          clearInterval(timer);
          busy(false);
          status("Running, but no reading has arrived. The client may be too old to report power: update it and readings will appear.", true);
        }
      }).catch(() => {});
    }, 5000);
    PAGE_TIMERS.push(timer);
  };

  const turnOn = async (msg) => {
    if (!(await ask(msg, "Turn on"))) return false;
    busy(true);
    status('<span class="upd-spin"></span>Starting power tracking on the machine…');
    post("enable").then(({ ok, j }) => {
      if (!ok) { busy(false); sw.checked = panel.classList.contains("on"); status(escapeHtml(j.detail || "Failed."), true); return; }
      status('<span class="upd-spin"></span>' + escapeHtml(j.message || "Running.") + " Waiting for the next report…");
      waitForReading();
    }).catch(() => { busy(false); sw.checked = panel.classList.contains("on"); status("Couldn't reach the host.", true); });
    return true;
  };

  sw.addEventListener("change", () => {
    if (sw.checked) {
      const windows = !!psu;
      turnOn("Turn on power tracking?\n\n" + (windows
        ? "Installs LibreHardwareMonitor's library and the PawnIO driver (to read CPU power), then starts a small background task that estimates wall power using your PSU's curve."
        : "Installs the pve-power exporter (a small Python service on port 9105) as root and starts it.")
        + " Readings appear within a minute.").then((on) => { if (!on) sw.checked = false; });
      return;
    }
    ask("Turn off power tracking?\n\nStops the exporter on the machine and disables it, so nothing keeps running there. Turning it back on starts it again.", "Turn off", true)
      .then((yes) => { if (yes) turnOff(); else sw.checked = true; });
  });

  const turnOff = () => {
    busy(true);
    status('<span class="upd-spin"></span>Stopping power tracking on the machine…');
    post("disable").then(({ ok, j }) => {
      if (!ok) { busy(false); sw.checked = true; status(escapeHtml(j.detail || "Failed."), true); return; }
      location.reload();
    }).catch(() => { busy(false); sw.checked = true; status("Couldn't reach the host.", true); });
  };

  if (psu) psu.addEventListener("change", () => {
    if (!sw.checked) return;  // just a choice for when it's turned on
    const label = psu.options[psu.selectedIndex].text;
    turnOn(`Use the ${label} curve?\n\nRestarts the power estimate on the machine with it.`).then((on) => { if (!on) location.reload(); });
  });
}

/* ---------- Problems page ---------- */
function problemsRefresh() {
  fetch(location.pathname, { headers: { "X-Boost": "1" } }).then((r) => r.text()).then((html) => {
    const next = new DOMParser().parseFromString(html, "text/html").getElementById("problems-live");
    const cur = document.getElementById("problems-live");
    if (!next || !cur || document.querySelector("dialog[open]") || cur.querySelector("button:disabled")) return;
    cur.replaceWith(next);
  }).catch(() => {});
}

function initProblems() {
  if (document.getElementById("problems-live")) PAGE_TIMERS.push(setInterval(problemsRefresh, 20000));
}

async function ackProblem(id, btn, acked) {
  const note = acked ? "" : await ask("Acknowledge this problem?\n\nIt stays listed, dimmed, until it clears, and stops counting in the menu badge.", "Ack", false, "Note (optional), e.g. after Sunday");
  if (note === null) return;
  btn.disabled = true;
  const body = new FormData();
  body.set("note", note);
  fetch(`/problems/${id}/ack`, { method: "POST", body }).finally(() => { btn.disabled = false; problemsRefresh(); });
}

async function ackAll(btn) {
  if (!(await ask("Acknowledge every new problem?", "Ack all"))) return;
  btn.disabled = true;
  fetch("/problems/ack-all", { method: "POST" }).finally(() => { btn.disabled = false; problemsRefresh(); });
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
