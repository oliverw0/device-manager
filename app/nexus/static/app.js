// NEXUS page. /api/overview every 30 s draws everything; /api/net every 5 s moves the traffic on the map.
// All the shaping is in nexus.py's build(); this file only draws it and wires up the buttons.
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const LED = { ok: "on", warn: "warn", bad: "off", neutral: "" };
const CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons";
const NET_HISTORY = 48; // samples at 5 s = 4 minutes of sparkline
let view = null;        // last /api/overview
let lastOk = 0;
const hist = new Map(); // key -> [{d, u}]
const localTurns = [];  // things asked from this page, until Willow's log has them
let reqFilter = "all";

// ---------- formatting
function ago(sec) {
  if (!sec) return "";
  const s = Date.now() / 1000 - sec;
  if (s < 0) return "soon";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function until(sec) {
  const s = sec - Date.now() / 1000;
  if (s <= 60) return "any moment";
  if (s < 3600) return `${Math.round(s / 60)} min left`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m left`;
}
function rate(bps) { // bytes/s -> bits/s, Night Panel style: "14 Mbit/s"
  const b = (bps || 0) * 8;
  if (b < 1000) return `${Math.round(b)} bit/s`;
  if (b < 1e6) return `${(b / 1e3).toFixed(b < 1e4 ? 1 : 0)} kbit/s`;
  if (b < 1e9) return `${(b / 1e6).toFixed(b < 1e7 ? 1 : 0)} Mbit/s`;
  return `${(b / 1e9).toFixed(2)} Gbit/s`;
}
const clockTime = (d) => d.toLocaleTimeString("en-AU", { hour: "2-digit", minute: "2-digit", hour12: false });
const dayName = (d) => d.toLocaleDateString("en-AU", { weekday: "short", day: "2-digit", month: "short" }).replace(",", "");
function dueInfo(due) {
  if (!due) return null;
  const d = new Date(`${due}T23:59:59`), days = (d - Date.now()) / 864e5;
  return { text: days < 0 ? `overdue ${dayName(d)}` : days < 1 ? "due today" : days < 2 ? "due tomorrow" : `due ${dayName(d)}`,
           cls: days < 0 ? "due-late" : days < 2 ? "due-soon" : "" };
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

// ---------- icons: dashboard-icons from jsDelivr, ours from static/icons, letters when neither loads
function icon(slug, name, small) {
  const src = !slug ? "" : slug.includes("/") ? `${slug}?v=25` : `${CDN}/svg/${slug}.svg`;
  const letters = esc((name || "?").replace(/[^A-Za-z0-9 ]/g, "").split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase());
  const cors = src.startsWith("http") ? ' crossorigin="anonymous"' : "";
  return `<span class="ico${small ? " sm" : ""}">${src ? `<img src="${esc(src)}"${cors} data-slug="${esc(slug)}" data-letters="${letters}" alt="" loading="lazy">` : `<span class="mono-ico">${letters}</span>`}</span>`;
}
// A few brand icons are black (ASUS, Frigate): measure each once and invert the dark ones so they read on the dark tiles.
const darkness = new Map();
function checkDark(img) {
  const key = img.currentSrc || img.src;
  if (!darkness.has(key)) {
    try {
      const c = document.createElement("canvas"), n = 24;
      c.width = c.height = n;
      const x = c.getContext("2d", { willReadFrequently: true });
      x.drawImage(img, 0, 0, n, n);
      const d = x.getImageData(0, 0, n, n).data;
      let lum = 0, chroma = 0, seen = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 128) {
        lum += 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
        chroma += Math.max(d[i], d[i + 1], d[i + 2]) - Math.min(d[i], d[i + 1], d[i + 2]);
        seen++;
      }
      // only grey/black marks: inverting a coloured logo (Debian's red) would change its colour
      if (seen) darkness.set(key, seen > 20 && lum / seen < 60 && chroma / seen < 40); // nothing drawn yet: measure again next time
    } catch { return; } // a cross-origin image without CORS can't be measured: leave it as it is
  }
  img.classList.toggle("inv", !!darkness.get(key));
}
document.addEventListener("load", (e) => { if (e.target.tagName === "IMG" && e.target.dataset.slug) checkDark(e.target); }, true);
// where something came from: the app's own icon and name in grey. Display only, so no signal colour.
const SOURCES_ICON = { plex: ["plex", "Plex"], jellyfin: ["jellyfin", "Jellyfin"], WILLOW: ["icons/willow.png", "Willow"], PULSE: ["logo.svg", "Health"],
                       SEERR: ["overseerr", "Seerr"], PROXMOX: ["proxmox", "Proxmox"], NEXUS: ["logo.svg", "NEXUS"], LIST: ["icons/willow.png", "Willow list"] };
function srcTag(k) {
  const [ic, name] = SOURCES_ICON[k] || SOURCES_ICON[String(k).split(" ")[0]] || [null, k];
  return `<span class="srctag">${ic ? icon(ic, name, true).replace('class="ico sm"', 'class="ico xs"') : ""}${esc(String(name).toUpperCase())}</span>`;
}
function poster(img, title, big) {
  const l = esc((title || "?").trim()[0] || "?");
  return img ? `<span class="poster${big ? " big" : ""}"><img src="${esc(img)}" alt="" loading="lazy" data-poster="${l}"></span>`
             : `<span class="poster empty${big ? " big" : ""}"><span>${l}</span></span>`;
}
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img.tagName !== "IMG") return;
  if (img.dataset.poster !== undefined) { // a poster that didn't load: the dotted stand-in
    img.parentElement.classList.add("empty");
    img.replaceWith(Object.assign(document.createElement("span"), { textContent: img.dataset.poster }));
    return;
  }
  if (img.dataset.slug && !img.dataset.tried && !img.dataset.slug.includes("/")) {
    img.dataset.tried = "1";
    img.src = `${CDN}/png/${img.dataset.slug}.png`;
    return;
  }
  if (img.dataset.letters !== undefined) img.replaceWith(Object.assign(document.createElement("span"), { className: "mono-ico", textContent: img.dataset.letters }));
}, true);

// ---------- HUD
function tick() {
  const d = new Date();
  $("time").textContent = clockTime(d);
  $("date").textContent = d.toLocaleDateString("en-AU", { weekday: "short", day: "2-digit", month: "short", year: "numeric" }).replace(/,/g, "");
  $("live").classList.toggle("stale", Date.now() - lastOk > 95000);
}
// The top bar is NEXUS, the time, the weather and one status line: everything that needs Oliver (approvals and
// warnings) counted as issues. The detail is in For you.
function hud(v) {
  const n = v.needs.length + v.warnings.length;
  $("brief").textContent = n ? `${plural(n, "issue")} need${n === 1 ? "s" : ""} you` : "Nothing needs you";
  $("brief").classList.toggle("has", n > 0);
  document.title = n ? `(${n}) NEXUS` : "NEXUS";
}

// Melbourne's weather, straight from Open-Meteo (free, no key; the same rough coordinates Willow uses). Every 20 minutes.
const WX = { lat: -37.81, lon: 144.96 };
const SKY = { 0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
  56: "Icy drizzle", 57: "Icy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Icy rain", 67: "Icy rain", 71: "Light snow", 73: "Snow",
  75: "Heavy snow", 77: "Snow", 80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Storms", 96: "Storms, hail", 99: "Storms, hail" };
async function weather() {
  try {
    const q = new URLSearchParams({ latitude: WX.lat, longitude: WX.lon, timezone: "Australia/Melbourne", forecast_days: 1,
      current: "temperature_2m,weather_code", daily: "temperature_2m_max,temperature_2m_min,precipitation_probability_max" });
    const r = await fetch(`https://api.open-meteo.com/v1/forecast?${q}`);
    if (!r.ok) return;
    const d = await r.json(), c = d.current, day = d.daily;
    $("wx-now").innerHTML = `<b>${Math.round(c.temperature_2m)}°</b> ${esc(SKY[c.weather_code] || "")}`;
    const rain = day.precipitation_probability_max?.[0];
    $("wx-day").textContent = `${Math.round(day.temperature_2m_min[0])}–${Math.round(day.temperature_2m_max[0])}°${rain ? ` · RAIN ${rain}%` : ""} · MELBOURNE`;
    $("wx").hidden = false;
  } catch { /* no weather is fine */ }
}
weather();
setInterval(weather, 20 * 60 * 1000);

// ---------- two screens: the board, and apps & media (the switch beside NEXUS in the top bar)
function route() {
  const view = location.hash === "#apps" ? "apps" : "board";
  document.body.dataset.view = view;
  document.querySelectorAll("a[data-view]").forEach((a) => { a.classList.toggle("active", a.dataset.view === view); a.setAttribute("aria-current", a.dataset.view === view ? "page" : "false"); });
  if (view === "board") requestAnimationFrame(() => { mapFitKey = ""; wires(); fitJobs(); });
  scrollTo(0, 0);
}
addEventListener("hashchange", route);
route();

// ---------- services: grouped by what they're for
const CATS = [
  ["Media", ["Plex", "Jellyfin", "Seerr", "Sonarr", "Radarr", "Lidarr", "Prowlarr", "qBittorrent", "Tautulli"]],
  ["Home & apps", ["Home Assistant", "Frigate", "Willow", "Galley", "Lobby"]],
  ["Infrastructure", ["Proxmox", "Backup Server", "DSM", "Router", "AdGuard Home", "Proxy Manager", "Authentik", "Portainer"]],
];
// An app's own light: its own problems (a stuck episode is Sonarr's) plus anything machine-wide (offline, a full disk),
// not every problem on the machine it shares (so Seerr isn't amber because Sonarr has a stuck download).
function svcIssues(node, name) { return (node.issues || []).filter((i) => !i.service || i.service === name); }
function svcLed(node, name) {
  const own = svcIssues(node, name);
  if (node.state === "down" || own.some((i) => i.level === "bad")) return "off";
  if (own.length) return "warn";
  return node.led ? "on" : null;
}
function allServices(v) {
  const out = [{ name: "Router", url: v.map.router.url, icon: v.map.router.icon, where: "192.168.50.1", led: null, tip: "router" }];
  for (const h of v.map.hosts) {
    for (const [name, url, ic] of h.services || []) out.push({ name, url, icon: ic, where: where(h.name, url), led: svcLed(h, name), node: `h:${h.id}` });
    for (const g of h.guests || []) for (const [name, url, ic] of g.services || [])
      out.push({ name, url, icon: ic, where: where(g.name, url), led: svcLed(g, name), node: `g:${g.id}` });
  }
  return out;
}
function where(host, url) { // "plex.wiles.au" for a proxied name, else "servarr :8989"
  try { const u = new URL(url); return u.hostname.endsWith("wiles.au") ? u.hostname : `${host}${u.port ? ` :${u.port}` : ""}`; } catch { return host; }
}
function services(v) {
  const all = allServices(v), byName = new Map(all.map((s) => [s.name, s]));
  const seen = new Set();
  let html = "";
  for (const [cat, names] of CATS) {
    const list = names.map((n) => byName.get(n)).filter(Boolean);
    list.forEach((s) => seen.add(s.name));
    html += group(cat, list);
  }
  const rest = all.filter((s) => !seen.has(s.name));
  if (rest.length) html += group("Other", rest);
  $("svc").innerHTML = html;
  $("svc-meta").textContent = `${all.length} LINKS`;
  filterServices();
}
function group(cat, list) {
  return `<div class="svc-group"><div class="svc-group-head">${esc(cat)}</div><div class="svc-grid">${list.map((s) =>
    `<a class="tile" href="${esc(s.url)}" data-q="${esc(`${s.name} ${s.where}`.toLowerCase())}" ${s.node ? `data-node="${s.node}"` : `data-tip="${s.tip}"`} data-open="${esc(s.name)}" data-where="${esc(s.where)}">
       ${icon(s.icon, s.name)}<span class="txt"><span class="nm ellipsis">${esc(s.name)}</span><span class="ad ellipsis">${esc(s.where)}</span></span>
       ${s.led ? `<span class="led ${s.led}"></span>` : ""}</a>`).join("")}</div></div>`;
}

// ---------- watching
function watching(v) {
  const m = v.media, src = m.sources;
  const dot = (k, label) => `<span title="${label}: ${src[k] === "ok" ? "connected" : src[k] === "off" ? "not set up" : "not answering"}">
    <span class="led ${src[k] === "ok" ? "on" : src[k] === "off" ? "" : "off"}" style="width:6px;height:6px"></span> ${label}</span>`;
  $("watch-src").innerHTML = `${dot("plex", "PLEX")}${dot("jellyfin", "JELLYFIN")}`;
  count("watch-meta", m.now.length, m.now.length ? "live" : "");
  $("now").innerHTML = m.now.length ? m.now.map((s) =>
    `<div class="nowcard">${poster(s.img, s.title, true)}
       <div class="body">
         <div class="m mono" style="display:flex;gap:6px;align-items:center">${srcTag(s.src)}<span class="state">${esc(s.state || "")}</span></div>
         <div class="t ellipsis">${esc(s.title)}</div>
         <div class="s ellipsis" style="font-size:12px">${esc(s.sub)}</div>
         <div class="m" style="font:10.5px var(--font-mono);color:var(--muted)">${esc(s.user || "")} · ${esc(s.player || "")}${s.transcode ? ` · ${esc(s.transcode)}` : ""}</div>
         <div class="bar ${s.state === "playing" ? "" : "warn"}"><i style="width:${Math.min(100, s.pct || 0)}%"></i></div>
       </div></div>`).join("")
    : `<p class="empty"><span class="led"></span>Nothing playing right now.</p>`;
  const shown = more.history ? m.recent.slice(0, 12) : m.recent.slice(0, 3);
  $("history").innerHTML = m.recent.length ? shown.map((h) =>
    `<div class="mrow">${poster(h.img, h.title)}<div class="body">
       <div class="t ellipsis">${esc(h.title)}</div><div class="s ellipsis">${esc(h.sub)}</div>
       <div class="m">${srcTag(h.src)}${esc(h.user || "")} · ${ago(h.at)}${h.pct && h.pct < 90 ? ` · ${h.pct}%` : ""}</div>
     </div></div>`).join("") + moreButton("history", m.recent.length, 3, Math.min(12, m.recent.length)) : `<p class="empty">No history yet.</p>`;
}

// ---------- requests (Seerr)
const REQ = { pending: ["warn", "Waiting"], approved: ["", "Approved"], processing: ["", "Searching"], downloading: ["ok", "Downloading"],
              partial: ["", "Partly ready"], available: ["", "Ready"], declined: ["bad", "Declined"], failed: ["bad", "Failed"] };
function requests(v) {
  const all = v.requests || [];
  const pend = all.filter((r) => r.state === "pending").length, act = all.filter((r) => ["approved", "processing", "downloading", "partial"].includes(r.state)).length;
  count("req-meta", pend || act, pend ? "warn" : "");
  const list = all.filter((r) => reqFilter === "all" || (reqFilter === "available" ? ["available", "partial"].includes(r.state)
    : ["pending", "approved", "processing", "downloading"].includes(r.state)));
  const order = { pending: 0, downloading: 1, processing: 2, approved: 3, partial: 4 };
  list.sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || (b.at || 0) - (a.at || 0));
  $("reqs").innerHTML = list.length ? list.slice(0, more.requests ? 20 : 4).map((r) => {
    const [cls, label] = REQ[r.state] || ["", r.state];
    const kind = r.type === "tv" ? `TV${r.seasons?.length ? ` · S${r.seasons.join(", S")}` : ""}` : "MOVIE";
    return `<div class="mrow">${poster(r.poster, r.title)}<div class="body">
      <div class="t ellipsis">${esc(r.title)}${r.year ? ` <span class="muted" style="font-weight:400">(${esc(r.year)})</span>` : ""}</div>
      <div class="m"><span class="state ${cls}">${label}</span><span>${esc(kind)}</span></div>
      <div class="m">${esc(r.by || "")} · ${ago(r.at)}</div>
      ${r.state === "downloading" && r.pct != null ? `<div class="bar" style="margin-top:4px"><i style="width:${r.pct}%"></i></div>
        <div class="m">${r.pct}%${r.eta ? ` · ${until(r.eta)}` : ""}</div>` : ""}
      ${r.state === "pending" ? `<div class="row-actions">${btn(["seerr-approve", r.id, "Approve"])}${btn(["seerr-decline", r.id, "Decline"])}</div>` : ""}
    </div></div>`;
  }).join("") + moreButton("requests", list.length, 4, Math.min(20, list.length)) : `<p class="empty">${reqFilter === "all" ? "No requests yet." : "Nothing here."}</p>`;
}
// "Show 9 more" under a short list; it remembers being open until the page reloads
const more = { history: false, requests: false };
function moreButton(key, total, short, long) {
  if (total <= short) return "";
  return `<button type="button" class="show-more" data-more-key="${key}">${more[key] ? "Show fewer" : `Show ${long - short} more`}</button>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-more-key]");
  if (!b || !view) return;
  more[b.dataset.moreKey] = !more[b.dataset.moreKey];
  b.dataset.moreKey === "history" ? watching(view) : requests(view);
});
$("req-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  reqFilter = b.dataset.f;
  for (const x of $("req-filter").children) { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", x === b); }
  if (view) requests(view);
});

// ---------- the network map
const GLOBE = `<svg class="glyph" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/></svg>`;
function rates(n, quiet, short) { // short: "29 Mb/s" for the small map nodes, "29 Mbit/s" everywhere else
  const f = (x) => short ? rate(x).replace("bit/s", "b/s").replace(" b/s", " b/s") : rate(x);
  return `<span class="rates${quiet ? " quiet" : ""}"><span class="dn">↓ ${f(n?.down)}</span><span class="up">↑ ${f(n?.up)}</span></span>`;
}
const NODES = new Map(); // "h:pve" / "g:114" -> the node from /api/overview, for hover cards and the dialog
const ledWord = (l) => ({ on: "UP", warn: "HAS A PROBLEM", off: "DOWN" })[l] || "NO MONITOR";
const ledChip = (l) => ({ on: "ok", warn: "warn", off: "bad" })[l] || "neutral";
const bare = (html) => html.replace(/^<span class="rates[^"]*">|<\/span>$/g, "");
function map(v) {
  const m = v.map, up = m.uplink;
  NODES.clear();
  for (const h of m.hosts) {
    NODES.set(`h:${h.id}`, { ...h, key: `h:${h.id}` });
    for (const g of h.guests || []) NODES.set(`g:${g.id}`, { ...g, key: `g:${g.id}`, host: h.name });
  }
  const live = m.hosts.flatMap((h) => h.guests || []).filter((g) => !g.off);
  $("map-meta").innerHTML = `${live.filter((g) => g.state === "up").length}/${live.length} GUESTS UP`;
  $("tier-top").innerHTML =
    `<div class="node node-inet" data-wire="inet" data-tip="inet">${GLOBE}<div style="min-width:0"><div class="nm">Internet</div>
       ${m.wan ? `<div class="ip-pub">${esc(m.wan.ip)}</div><div class="ad">${esc([m.wan.isp, m.wan.city].filter(Boolean).join(" · "))}</div>`
               : `<div class="ad">Public IP unknown</div>`}</div>
       ${m.wan?.ip ? `<button type="button" class="copy-ip" data-copy="${esc(m.wan.ip)}" aria-label="Copy public IP" title="Copy">
         <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="1"/><path d="M5 15V5a1 1 0 0 1 1-1h10"/></svg></button>` : ""}</div>
     <a class="node" data-wire="router" data-tip="router" href="${esc(m.router.url)}">${icon(m.router.icon, "Router")}
       <div style="min-width:0"><div class="nm">Router</div><div class="ad">${esc(m.router.sub)}</div></div></a>`;
  const head = (h) => `<button type="button" class="hostcard-head" data-node="h:${esc(h.id)}" aria-label="${esc(h.name)}: details">${icon(h.icon, h.name)}
        <span class="txt"><span class="nm">${esc(h.name)}</span><span class="ad ellipsis">${esc(h.sub)}</span>${h.model ? `<span class="model ellipsis">${esc(h.model)}</span>` : ""}</span>
        <span data-rates="h:${esc(h.id)}">${h.net ? rates(h.net) : h.led && h.led !== "off" ? `<span class="rates quiet"><span class="dn">NOT MEASURED</span></span>` : ""}</span>
        <span class="led ${h.led || ""}"></span></button>`;
  const card = (h) => {
    if (h.guests?.length) {
      const inside = h.guests.filter((g) => !g.off).sort((a, b) => a.id - b.id), retired = h.guests.filter((g) => g.off);
      return `<div class="hostcard pve" data-wire="${esc(h.id)}">${head(h)}
        <div class="enclosure">
          <div class="enc-label" data-bridge><b>Inside pve</b> · vmbr0 bridge · ${inside.filter((g) => g.state === "up").length} of ${inside.length} running</div>
          <div class="guests">${inside.map(gnode).join("")}</div>
          ${retired.length ? `<div class="retired-line" title="Stopped, waiting to be deleted">RETIRED · ${retired.map((g) =>
            `<span class="${g.state === "up" ? "on-again" : ""}">${g.id} ${esc(g.name)}${g.state === "up" ? " (running!)" : ""}</span>`).join(" · ")}</div>` : ""}
        </div></div>`;
    }
    return `<div class="hostcard" data-wire="${esc(h.id)}">${head(h)}<div class="svcs">${(h.services || []).map(([n, u, ic]) =>
      `<a class="chiplink" href="${esc(u)}" data-node="h:${esc(h.id)}" data-open="${esc(n)}">${icon(ic, n, true)}<span class="ellipsis">${esc(n)}</span></a>`).join("")}</div></div>`;
  };
  const main = m.hosts.filter((h) => h.guests?.length), side = m.hosts.filter((h) => !h.guests?.length);
  $("tier-hosts").innerHTML = main.map(card).join("") + `<div class="side-hosts">${side.map(card).join("")}</div>`;
  requestAnimationFrame(wires);
  if ($("dlg").open && $("dlg").dataset.key) openNode($("dlg").dataset.key, true);
}
// every guest tile looks the same at every size and every refresh: name, ↓/↑ rates, a small traffic graph. Its apps and
// open issues are in the hover card and the details dialog
function gnode(g) {
  const cls = g.state === "down" || g.led === "off" ? "down" : g.led === "warn" ? "warn" : "";
  return `<div class="gnode ${cls}" role="button" tabindex="0" data-g="${g.id}" data-node="g:${g.id}" aria-label="${esc(g.kind || "LXC")} ${g.id} ${esc(g.name)}: details">
    <span class="top"><span class="led ${g.led || ""}"></span><span class="id">${g.kind === "VM" ? "VM " : ""}${g.id}</span>
      <span class="nm ellipsis">${esc(g.name)}</span></span>
    <span data-rates="${g.id}">${g.state === "up" ? rates(g.net, false, true) : `<span class="rates quiet"><span class="dn">${g.state === "down" ? "NOT RUNNING" : "—"}</span></span>`}</span>
    <svg class="spark" data-spark="${g.id}" viewBox="0 0 100 18" preserveAspectRatio="none"></svg></div>`;
}
function flowStyle(n) { // faster, brighter dashes for busier links; a slow faint trail when it's nearly idle
  const r = n ? n.down + n.up : 0;
  if (r < 200) return { dur: 7, op: 0.3, idle: true };
  const lg = Math.log10(r / 100);
  return { dur: Math.max(0.3, 3.4 - 0.62 * lg).toFixed(2), op: Math.min(1, 0.5 + 0.12 * lg).toFixed(2), rev: n.up > n.down };
}
const sum = (list) => list.reduce((a, n) => ({ down: a.down + (n?.down || 0), up: a.up + (n?.up || 0) }), { down: 0, up: 0 });
// Wide screens: the centre column never scrolls, so the map is scaled to the room Willow leaves it. It's laid out
// wider (1/scale) before scaling, which lets mac-deb and the NAS sit beside pve instead of under it.
// The height doesn't fall steadily as the layout widens (guests jump from 2 to 3 to 4 a row), so it tries scales from
// the top down and keeps the first that fits. Refits only when the room or the set of machines changes.
let mapScale = 1, mapFitKey = "";
function fitMap() {
  const fit = $("topo-fit"), topo = $("topo");
  const key = [innerWidth, fit.clientWidth, fit.clientHeight, topo.querySelectorAll(".gnode, .hostcard").length].join(",");
  if (key === mapFitKey && topo.offsetHeight * mapScale <= fit.clientHeight + 1) return; // same room, still fits
  mapFitKey = key;
  topo.style.width = topo.style.transform = "";
  mapScale = 1;
  if (innerWidth <= 1360) return;
  const W = fit.clientWidth, avail = fit.clientHeight;
  if (!W || !avail || topo.offsetHeight <= avail) return;
  let s = 0.98;
  for (; s > 0.5; s -= 0.02) { topo.style.width = `${W / s}px`; if (topo.offsetHeight * s <= avail) break; }
  s = Math.max(s, 0.5);
  topo.style.width = `${W / s}px`;
  mapScale = s;
  topo.style.transform = `scale(${s})`;
}
function wires() {
  const topo = $("topo"), svg = $("wires");
  if (!view || !topo) return;
  fitMap();
  const T = topo.getBoundingClientRect(), s = mapScale;
  if (!T.width) return;
  // positions in the map's own (unscaled) pixels: the svg is inside the scaled map
  const R = (el) => { const r = el.getBoundingClientRect(); return { l: (r.left - T.left) / s, t: (r.top - T.top) / s, r: (r.right - T.left) / s, b: (r.bottom - T.top) / s, cx: ((r.left + r.right) / 2 - T.left) / s, cy: ((r.top + r.bottom) / 2 - T.top) / s }; };
  svg.setAttribute("viewBox", `0 0 ${topo.offsetWidth} ${topo.offsetHeight}`);
  const base = [], flow = [], dots = [];
  // state: "" a live link (a solid line, with pulses along it paced by its traffic), "quiet" up but nothing measures it
  // (solid, no pulses), "unknown" no monitor says either way (dashed grey), "down" broken (dashed red)
  const wire = (d, n, state = "") => {
    base.push(`<path class="w-base ${state === "down" ? "down" : state === "unknown" ? "unknown" : "up"}" d="${d}"/>`);
    if (state) return;
    const f = flowStyle(n);
    if (!f.idle) flow.push(`<path class="w-flow${f.rev ? " rev" : ""}" d="${d}" style="--dur:${f.dur}s;opacity:${f.op}"/>`);
  };
  const port = (x, y, on) => dots.push(`<circle class="w-port${on ? " on" : ""}" cx="${x}" cy="${y}" r="3.2"/>`);
  const q = (s) => topo.querySelector(s);
  const inet = q('[data-wire="inet"]'), router = q('[data-wire="router"]');
  if (inet && router) {
    const a = R(inet), b = R(router);
    wire(`M${a.r} ${a.cy} H${b.l}`, view.map.uplink);
    port(a.r, a.cy, true); port(b.l, b.cy, true);
    // lowest first, so where stacked hosts share the trunk, each stretch shows the state of the host it reaches first
    const hosts = [...topo.querySelectorAll(".hostcard")].sort((x, y) => R(y).t - R(x).t);
    if (hosts.length) {
      const firstTop = Math.min(...hosts.map((h) => R(h).t)), busY = (b.b + firstTop) / 2;
      dots.push(`<text class="w-tag" x="${b.cx + 8}" y="${busY - 7}">LAN 192.168.50.0/24</text>`);
      for (const h of hosts) {
        const r = R(h), id = h.dataset.wire, info = view.map.hosts.find((x) => x.id === id);
        const lit = info?.led === "on" || info?.led === "warn";
        const state = info?.led === "off" ? "down" : info?.net ? "" : lit ? "quiet" : "unknown";
        if (id === "pve") { // into the lane on pve's left edge; it carries on down to the bridge (below)
          const x = r.l + 14;
          wire(`M${b.cx} ${b.b} V${busY} H${x} V${r.t}`, info?.net, state);
          port(x, r.t, lit);
        } else if (r.t - firstTop < 4) { // top row: straight down from the LAN bus
          const x = Math.max(r.l + 24, Math.min(r.r - 24, r.cx));
          wire(`M${b.cx} ${b.b} V${busY} H${x} V${r.t}`, info?.net, state);
          port(x, r.t, lit);
        } else { // stacked under another host: a riser in the gap on its left, into its side
          const x = Math.max(4, r.l - 12);
          wire(`M${b.cx} ${b.b} V${busY} H${x} V${r.t + 24} H${r.l}`, info?.net, state);
          port(r.l, r.t + 24, lit);
        }
      }
      port(b.cx, b.b, true);
    }
  }
  // inside pve: the lane takes the uplink down to the vmbr0 bridge, the bridge feeds a bus over each row, each guest drops off its row
  const pve = q(".hostcard.pve"), label = pve && pve.querySelector("[data-bridge]");
  if (pve && label) {
    const card = R(pve), lab = R(label), x0 = card.l + 14;
    const guests = new Map(view.map.hosts.flatMap((h) => h.guests || []).map((g) => [String(g.id), g]));
    const nodes = [...pve.querySelectorAll(".gnode")].map((el) => ({ r: R(el), g: guests.get(el.dataset.g) }));
    const rows = new Map();
    for (const n of nodes) { const k = Math.round(n.r.t); rows.set(k, [...(rows.get(k) || []), n]); }
    const keys = [...rows.keys()].sort((a, b) => a - b);
    const bridgeY = lab.cy, total = sum(nodes.map((n) => n.g?.net)), pveNet = view.map.hosts.find((h) => h.id === "pve")?.net;
    wire(`M${x0} ${card.t} V${bridgeY}`, pveNet || total);
    const half = (parseFloat(getComputedStyle(pve.querySelector(".guests")).rowGap) || 32) / 2; // each row's bus runs mid-gap above it
    if (keys.length) wire(`M${x0} ${bridgeY} V${keys[keys.length - 1] - half}`, total);
    dots.push(`<rect class="w-bridge" x="${x0 - 5}" y="${bridgeY - 5}" width="10" height="10"/>`);
    for (const k of keys) {
      const row = rows.get(k).sort((a, b) => a.r.l - b.r.l), y = k - half;
      wire(`M${x0} ${y} H${row[row.length - 1].r.cx}`, sum(row.map((n) => n.g?.net)));
      port(x0, y, true);
      for (const n of row) {
        wire(`M${n.r.cx} ${y} V${n.r.t}`, n.g?.net, n.g?.state === "down" ? "down" : n.g?.state === "up" ? "" : "unknown");
        port(n.r.cx, n.r.t, n.g?.state === "up");
      }
    }
  }
  svg.innerHTML = base.join("") + flow.join("") + dots.join("");
}
new ResizeObserver(() => requestAnimationFrame(wires)).observe(document.documentElement);

// live traffic: rates, sparklines and wire speed every 5 s, without redrawing the map
function remember(key, n) {
  const a = hist.get(key) || [];
  a.push({ d: n?.down || 0, u: n?.up || 0 });
  while (a.length > NET_HISTORY) a.shift();
  hist.set(key, a);
}
function spark(svg, a) {
  if (!a || a.length < 3) { svg.innerHTML = ""; return; }
  const max = Math.max(2000, ...a.map((s) => Math.max(s.d, s.u)));
  const y = (val) => (17 - Math.sqrt(val / max) * 15).toFixed(1);
  const pts = (k) => a.map((s, i) => `${((i / (a.length - 1)) * 100).toFixed(1)},${y(s[k])}`);
  const d = pts("d"), u = pts("u");
  svg.innerHTML = `<path d="M${d[0].split(",")[0]},18 L${d.join(" L")} L${d[d.length - 1].split(",")[0]},18 Z" fill="rgba(55,224,106,0.10)"/>
    <polyline points="${d.join(" ")}" fill="none" stroke="var(--green)" stroke-width="1.2" vector-effect="non-scaling-stroke"/>
    <polyline points="${u.join(" ")}" fill="none" stroke="var(--muted)" stroke-width="1" stroke-dasharray="2 2" vector-effect="non-scaling-stroke"/>`;
}
function applyNet(n) {
  if (!view || !n) return;
  view.map.uplink = n.uplink;
  for (const h of view.map.hosts) {
    const hn = h.id === "pve" ? n.uplink : (n.hosts || {})[h.id];
    if (hn !== undefined || h.id === "pve") {
      h.net = hn || null;
      remember(`h:${h.id}`, h.net);
      const el = document.querySelector(`[data-rates="h:${h.id}"]`);
      if (el && h.net) el.innerHTML = rates(h.net);
    }
    if (NODES.has(`h:${h.id}`)) NODES.get(`h:${h.id}`).net = h.net;
    for (const g of h.guests || []) {
      g.net = n.guests[String(g.id)] || null;
      if (NODES.has(`g:${g.id}`)) NODES.get(`g:${g.id}`).net = g.net;
      if (!g.off) remember(`g:${g.id}`, g.net);
      const el = document.querySelector(`[data-rates="${g.id}"]`);
      if (el && g.state === "up") el.innerHTML = rates(g.net, false, true);
      const sv = document.querySelector(`[data-spark="${g.id}"]`);
      if (sv) spark(sv, hist.get(`g:${g.id}`));
    }
  }
  if ($("dlg").open) dialogTraffic();
  wires();
}
async function pollNet() {
  if (document.hidden || !view) return;
  try { const r = await fetch("api/net", { cache: "no-store" }); if (r.ok) applyNet(await r.json()); } catch { /* the 30 s refresh says if it's down */ }
}

// ---------- hover cards: why a light is the colour it is
const LAMP_HELP = {
  Machines: "Machines Pulse hears from. Red: one stopped reporting.", Checks: "Pulse's health checks (backups, download queues).",
  Downloads: "Downloads stuck in Sonarr or Radarr: fake or unimportable files.", Problems: "Other open problems Pulse found.",
  Disks: "The fullest disk on any machine. Amber from 80%, red from 90%.", Approvals: "Willow jobs and Seerr requests waiting for your OK.",
  Willow: "Willow's background jobs.", Claude: "Claude plan usage this week. Amber from 70%, red from 90%.",
};
function tipHtml(el) {
  const lampName = el.dataset.lamp, nodeKey = el.dataset.node, tipKey = el.dataset.tip;
  if (lampName) {
    const l = view.lamps.find((x) => x.name === lampName);
    if (!l) return "";
    return `<div class="tip-head"><span class="led ${LED[l.level]}"></span><b>${esc(l.name)}</b><span class="chip chip-sm ${ledChip(LED[l.level])}">${esc(l.text)}</span></div>
      <div class="tip-why">${esc(LAMP_HELP[l.name] || "")}</div>
      ${l.items?.length ? `<ul class="tip-list">${l.items.map((i) => `<li class="${l.level}">${esc(i)}</li>`).join("")}</ul>` : ""}
      <div class="tip-foot">Click to jump to it.</div>`;
  }
  if (tipKey === "inet") {
    const w = view.map.wan, u = view.map.uplink;
    return `<div class="tip-head"><span class="led on"></span><b>Internet</b></div>
      <div class="tip-why">${w ? `Public IP ${esc(w.ip)}${w.isp ? ` · ${esc(w.isp)}` : ""}` : "Public IP unknown"}</div>
      ${u ? `<div class="tip-read">${rates(u)}</div>` : ""}<div class="tip-foot">Traffic on this link is pve's uplink, the only port measured.</div>`;
  }
  if (tipKey === "router") {
    return `<div class="tip-head"><b>Router</b><span class="chip chip-sm neutral">NO MONITOR</span></div>
      <div class="tip-why">ASUS RT-AX82U at 192.168.50.1 (rout.wiles.au). No monitoring client, so no light.</div><div class="tip-foot">Click to open its admin page.</div>`;
  }
  const n = NODES.get(nodeKey);
  if (!n) return "";
  const r = n.readings, opening = el.dataset.open;
  const label = nodeKey.startsWith("g:") ? `${n.kind || "LXC"} ${n.id} ${n.name}` : n.name;
  // hovering an app: its own light and problems first, then a line about the machine it runs on
  const issues = opening ? svcIssues(n, opening) : n.issues || [];
  const led = opening ? svcLed(n, opening) : n.led;
  const why = !opening ? n.why : issues.length ? plural(issues.length, "open problem")
    : n.state === "down" ? "Not running" : led ? "Up, nothing wrong" : "No monitor";
  const others = opening ? (n.issues || []).length - issues.length : 0;
  return `<div class="tip-head"><span class="led ${led || ""}"></span><b>${esc(opening || label)}</b><span class="chip chip-sm ${ledChip(led)}">${ledWord(led)}</span></div>
    <div class="tip-why">${esc(why || "")}</div>
    ${issues.length ? `<ul class="tip-list">${issues.slice(0, 4).map((i) => `<li class="${i.level}">${esc(i.title)}</li>`).join("")}${issues.length > 4 ? `<li>+${issues.length - 4} more</li>` : ""}</ul>` : ""}
    ${el.dataset.where ? `<div class="tip-read">${esc(el.dataset.where)}</div>` : ""}
    ${opening ? `<div class="tip-read">ON ${esc(label.toUpperCase())}${others ? ` · ${others} PROBLEM${others === 1 ? "" : "S"} FOR OTHER APPS THERE` : ""}</div>` : ""}
    ${r ? `<div class="tip-read">${[r.cpu != null ? `CPU ${Math.round(r.cpu)}%` : "", r.mem != null ? `MEM ${Math.round(r.mem)}%` : "",
      r.disk ? `DISK ${Math.round(r.disk.percent)}%` : "", r.containers ? `${r.containers[0]}/${r.containers[1]} CONTAINERS` : "", r.updates ? `${r.updates} UPDATES` : ""].filter(Boolean).join(" · ")}</div>` : ""}
    ${n.net ? `<div class="tip-read">${rates(n.net)}</div>` : ""}
    <div class="tip-foot">${opening ? `Click to open ${esc(opening)}.` : "Click for details and actions."}</div>`;
}
let tipTimer, tipFor = "";
function hideTip() { clearTimeout(tipTimer); $("tip").hidden = true; tipFor = ""; }
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-node], [data-lamp], [data-tip]");
  if (!el || !view) { if (!e.target.closest("#tip")) hideTip(); return; }
  const id = `${el.dataset.node || el.dataset.lamp || el.dataset.tip}|${el.dataset.open || ""}`;
  if (id === tipFor) return;
  hideTip();
  tipFor = id;
  tipTimer = setTimeout(() => {
    const tip = $("tip"), html = tipHtml(el);
    if (!html) return;
    tip.innerHTML = html;
    tip.hidden = false;
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    const x = Math.min(Math.max(8, r.left), innerWidth - w - 8);
    let y = r.bottom + 8;
    if (y + h > innerHeight - 8) y = Math.max(8, r.top - h - 8);
    tip.style.left = `${x}px`; tip.style.top = `${y}px`;
  }, 150);
});
document.addEventListener("scroll", hideTip, true);

// ---------- the details dialog for a machine
function openNode(key, refresh) {
  const n = NODES.get(key), dlg = $("dlg");
  if (!n) return;
  hideTip();
  dlg.dataset.key = key;
  const r = n.readings, isGuest = key.startsWith("g:");
  const meter = (label, p, note) => p == null ? "" : `<div class="meter-row"><div class="top"><span>${label}${note ? ` · ${esc(note)}` : ""}</span><b>${Math.round(p)}%</b></div>
    <div class="bar ${p >= 90 ? "bad" : p >= 70 ? "warn" : ""}"><i style="width:${Math.min(100, p)}%"></i></div></div>`;
  const pveLink = isGuest ? `https://proxmox.wiles.au/#v1:0:=${n.kind === "VM" ? "qemu" : "lxc"}%2F${n.id}` : n.id === "pve" ? "https://proxmox.wiles.au" : null;
  $("dlg-body").innerHTML = `
    <header class="dlg-head">${n.icon ? icon(n.icon, n.name) : `<span class="ico"><span class="mono-ico">${esc(String(n.id))}</span></span>`}
      <div class="txt"><div class="dlg-title">${isGuest ? `${esc(n.kind || "LXC")} ${n.id} · ` : ""}${esc(n.name)}</div>
        <div class="ad">${esc(isGuest ? `192.168.50${n.sub} · inside pve` : n.sub)}</div>${n.model ? `<div class="model">${esc(n.model)}</div>` : ""}</div>
      <span class="chip ${ledChip(n.led)}"><span class="chip-dot"></span>${ledWord(n.led)}</span>
      <button type="button" class="icon-x" data-close aria-label="Close">×</button></header>
    <p class="dlg-why"><span class="led ${n.led || ""}"></span>${esc(n.why || "")}</p>
    ${n.issues?.length ? `<div class="sub-head">What's wrong</div>${n.issues.map((i) => `<div class="warn-row issue"><span class="led ${i.level === "bad" ? "off" : "warn"}"></span><div class="body">
        <div class="t">${esc(i.title)}</div>${i.detail ? `<div class="d">${esc(i.detail)}</div>` : ""}
        ${i.src || i.age ? `<div class="m">${esc([i.src, i.age].filter(Boolean).join(" · "))}</div>` : ""}
        ${i.actions?.length ? `<div class="row-actions">${i.actions.map(btn).join("")}</div>` : ""}</div></div>`).join("")}` : ""}
    <div class="dlg-cols">
      <div><div class="sub-head">Traffic</div><div id="dlg-traffic"></div></div>
      <div><div class="sub-head">Readings${r ? " · from its client" : ""}</div>${r ? `${meter("CPU", r.cpu)}${meter("Memory", r.mem)}${r.disk ? meter("Fullest disk", r.disk.percent, r.disk.mount) : ""}
        <ul class="kv-mini">${r.containers ? `<li><span>Containers</span><b>${r.containers[0]} of ${r.containers[1]} running</b></li>` : ""}
          ${r.updates != null ? `<li><span>Updates</span><b>${r.updates ? `${r.updates} waiting` : "none"}</b></li>` : ""}
          <li><span>Last report</span><b>${esc(r.last_seen || "?")}</b></li>${r.muted ? `<li><span>Alerts</span><b>muted</b></li>` : ""}</ul>`
        : `<p class="empty">No monitoring client on this machine, so no CPU, memory or disk readings.</p>`}</div>
    </div>
    ${n.services?.length ? `<div class="sub-head">Apps</div><div class="svc-grid">${n.services.map(([nm, u, ic]) =>
      `<a class="tile" href="${esc(u)}">${icon(ic, nm)}<span class="txt"><span class="nm ellipsis">${esc(nm)}</span><span class="ad ellipsis">${esc(where(n.name, u))}</span></span></a>`).join("")}</div>` : ""}
    <footer class="dlg-foot">${pveLink ? `<a class="btn small secondary" href="${pveLink}">Open in Proxmox ↗</a>` : ""}
      ${r ? `<a class="btn small secondary" href="/devices" target="_self">Open in Devices</a>` : ""}<span class="spacer"></span><span class="muted mono" style="font-size:10.5px">ESC TO CLOSE</span></footer>`;
  dialogTraffic();
  if (!refresh && !dlg.open) dlg.showModal();
}
function dialogTraffic() {
  const key = $("dlg").dataset.key, n = NODES.get(key), box = $("dlg-traffic");
  if (!n || !box) return;
  const a = hist.get(key);
  box.innerHTML = n.net ? `<div class="big-rates"><div><span class="label">Down</span><b class="dn">${rate(n.net.down)}</b></div><div><span class="label">Up</span><b>${rate(n.net.up)}</b></div></div>
      <svg class="spark big" viewBox="0 0 100 18" preserveAspectRatio="none"></svg><div class="muted mono" style="font-size:10.5px">LAST ${Math.max(1, Math.round(((a?.length || 1) * 5) / 60))} MIN · ↓ SOLID, ↑ DASHED</div>`
    : `<p class="empty">${n.state === "down" ? "Not running." : "Nothing measures this machine's traffic yet."}</p>`;
  const sv = box.querySelector("svg");
  if (sv) spark(sv, a);
}
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]") || e.target === $("dlg")) { $("dlg").close(); return; }
  const n = e.target.closest(".gnode[data-node], .hostcard-head[data-node]");
  if (n) openNode(n.dataset.node);
});
document.addEventListener("keydown", (e) => {
  const n = e.target.closest?.(".gnode[data-node]");
  if (n && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openNode(n.dataset.node); }
});

// ---------- Willow: the conversation, jobs and plan usage
function willow(v) {
  const w = v.willow;
  // drop a local turn once Willow's own log has it
  for (let i = localTurns.length - 1; i >= 0; i--) {
    const t = localTurns[i];
    if (t.done && w.turns.some((s) => s.heard === t.heard && s.at >= t.at - 10)) localTurns.splice(i, 1);
  }
  drawLog(w.turns);
  $("jobs").innerHTML = (w.running.length || w.recent.length) ? [
    ...w.running.map((t) => `<div class="wjob"><span class="led on pulse"></span><div><div class="t">${esc(t.title)}</div>
      <div class="m">RUNNING${t.steps ? ` · ${t.steps} STEPS` : ""} · ${ago(t.at).toUpperCase()}</div></div></div>`),
    ...w.recent.map((t) => `<div class="wjob"><span class="led ${t.state === "done" ? "" : "off"}"></span><div><div class="t">${esc(t.title)}</div>
      <div class="m">${t.state === "done" ? "DONE" : "FAILED"} · ${ago(t.at).toUpperCase()}</div></div></div>`)].join("")
    : `<p class="empty">No jobs running.</p>`;
  const u = v.usage;
  const meter = (label, x) => {
    if (!x || x.used_percent == null) return "";
    const p = Math.round(x.used_percent), cls = p >= 90 ? "bad" : p >= 70 ? "warn" : "";
    const reset = x.resets_at ? new Date(typeof x.resets_at === "number" ? x.resets_at * 1000 : x.resets_at) : null;
    return `<div class="meter-row"><div class="top"><span>${label}</span><b>${p}%</b></div><div class="bar ${cls}"><i style="width:${Math.min(p, 100)}%"></i></div>
      ${reset && !isNaN(reset) ? `<div class="top"><span>Resets ${esc(dayName(reset))} ${clockTime(reset)}</span></div>` : ""}</div>`;
  };
  $("usage").innerHTML = u ? meter("This week", u.seven_day) + meter("Last 5 hours", u.five_hour) : `<p class="empty">Not connected.</p>`;
  fitJobs(); // after the plan is drawn: Jobs gets what the plan leaves
}
// Jobs shows only the ones that fit above the Claude plan; "View N more" opens the whole list (the column then
// scrolls) until "Show fewer"
let jobsOpen = false;
function fitJobs() {
  const box = $("jobs"), items = [...box.querySelectorAll(".wjob")];
  box.querySelector(".jobs-more")?.remove();
  items.forEach((el) => { el.hidden = false; });
  $("willow").classList.toggle("jobs-open", jobsOpen);
  if (!items.length) return;
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "more jobs-more";
  box.append(btn);
  let hidden = 0;
  if (!jobsOpen) while (box.scrollHeight > box.clientHeight + 1 && hidden < items.length) items[items.length - 1 - hidden++].hidden = true;
  if (jobsOpen) btn.textContent = "Show fewer";
  else if (hidden === items.length) btn.textContent = `View ${hidden} job${hidden > 1 ? "s" : ""}`;
  else if (hidden) btn.textContent = `View ${hidden} more`;
  else btn.remove();
}
$("jobs").addEventListener("click", (e) => {
  if (!e.target.closest(".jobs-more")) return;
  jobsOpen = !jobsOpen;
  fitJobs();
});
new ResizeObserver(() => fitJobs()).observe($("jobs").parentElement);
function turnHtml(t, live) {
  const time = t.at ? clockTime(new Date(t.at * 1000)) : "";
  const you = t.heard ? `<div class="who you">YOU${t.device ? ` · ${esc(t.device)}` : ""} · ${time}</div><div class="msg you">${esc(t.heard)}</div>` : "";
  let her;
  if (live && !t.done) {
    her = `<div class="who">WILLOW</div>${t.steps?.length ? `<div class="steps">${t.steps.map((s) => `<span>${esc(s)}</span>`).join("")}</div>` : ""}
      <div class="msg her thinking">${t.say ? `${esc(t.say)} ` : ""}<span class="typing"><i></i><i></i><i></i></span></div>`;
  } else {
    her = `<div class="who">WILLOW${t.ms ? ` · ${(t.ms / 1000).toFixed(1)} s` : ""}${t.route ? ` <span class="route">${esc(t.route)}</span>` : ""}</div>
      ${t.steps?.length ? `<div class="steps">${t.steps.map((s) => `<span>${esc(s)}</span>`).join("")}</div>` : ""}
      <div class="msg her${t.err ? " err" : ""}">${esc(t.say || "…")}</div>
      ${t.show && t.show !== t.say ? `<button type="button" class="more" data-more>Show details</button><div class="show" hidden>${esc(t.show)}</div>` : ""}`;
  }
  return `<div class="turn">${you}${her}</div>`;
}
let lastServerTurns = [];
let speakUntil = 0;
function drawLog(turns) {
  if (turns) lastServerTurns = turns;
  const log = $("log"), nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const html = lastServerTurns.map((t) => turnHtml(t)).join("") + localTurns.map((t) => turnHtml(t, true)).join("");
  log.innerHTML = html || `<p class="empty">Ask Willow something. Answers from every device show here.</p>`;
  if (nearBottom || !log.dataset.scrolled) { log.scrollTop = log.scrollHeight; log.dataset.scrolled = "1"; }
  $("hist-count").textContent = lastServerTurns.length + localTurns.length || "";
  drawTalk();
}
// The talk view: Willow's tree, the last thing said, and the ask box. The strands sway when idle, shimmer while
// Willow thinks, and move like a voice while a fresh answer is "being said".
function drawTalk() {
  const live = localTurns.find((t) => !t.done);
  const last = live || localTurns[localTurns.length - 1] || lastServerTurns[lastServerTurns.length - 1];
  const state = live ? "thinking" : Date.now() < speakUntil ? "speaking" : "idle";
  $("w-tree").dataset.state = state;
  $("w-state").innerHTML = state === "thinking" ? `<span class="typing"><i></i><i></i><i></i></span> THINKING${live.steps?.length ? ` · ${esc(live.steps[live.steps.length - 1])}` : ""}`
    : state === "speaking" ? "ANSWERING" : last?.at ? `READY · LAST SPOKE ${ago(last.at).toUpperCase()}${last.device && last.device !== "nexus" ? ` ON ${esc(last.device).toUpperCase()}` : ""}` : "READY";
  if (!last) { $("w-heard").textContent = ""; $("w-say").textContent = "Ask me anything about the lab, your day or the web."; return; }
  $("w-heard").textContent = last.heard ? `“${last.heard}”` : "";
  // the whole answer, scrolling inside the talk pane if it's long; the phone details are in History
  $("w-say").textContent = live ? (live.say || "") : last.say || "…";
  $("w-say").classList.toggle("err", !!last.err);
}
setInterval(() => { if ($("w-tree").dataset.state === "speaking" && Date.now() >= speakUntil) drawTalk(); }, 500);
$("w-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  for (const x of $("w-tabs").children) { x.classList.toggle("active", x === b); x.setAttribute("aria-selected", x === b); }
  $("pane-talk").hidden = b.dataset.tab !== "talk";
  $("pane-history").hidden = b.dataset.tab !== "history";
  if (b.dataset.tab === "history") { const log = $("log"); log.scrollTop = log.scrollHeight; }
});
$("log").addEventListener("click", (e) => {
  const b = e.target.closest("[data-more]");
  if (!b) return;
  const box = b.nextElementSibling;
  box.hidden = !box.hidden;
  b.textContent = box.hidden ? "Show details" : "Hide details";
});
async function ask(text) {
  text = text.trim();
  if (!text) return;
  const t = { heard: text, at: Date.now() / 1000, device: "nexus", steps: [], done: false };
  localTurns.push(t);
  drawLog();
  $("ask").value = "";
  $("send").disabled = true;
  try {
    let r = await post("api/chat", { text });
    t.say = r.say || "";
    t.route = r.route;
    if (r.pending) {
      drawLog();
      let after = 0;
      for (let i = 0; i < 40; i++) {           // up to ~25 s per wait, so about 15 minutes in all
        const p = await (await fetch(`api/pending/${encodeURIComponent(r.pending)}?after=${after}`, { cache: "no-store" })).json();
        if (p.error) throw new Error(p.error);
        t.steps.push(...(p.steps || []));
        if (p.done) { t.say = p.superseded ? "(answered elsewhere)" : p.say; t.show = p.show; t.route = p.route; break; }
        after = p.next ?? after;
        drawLog();
      }
    } else {
      t.show = r.show;
    }
  } catch (err) {
    t.say = `That didn't reach Willow: ${err.message}`;
    t.err = true;
  }
  t.done = true;
  t.ms = Math.round((Date.now() / 1000 - t.at) * 1000);
  $("send").disabled = false;
  if (t.err) speakUntil = 0; else speak(t.say);
  drawLog();
}
$("composer").addEventListener("submit", (e) => { e.preventDefault(); ask($("ask").value); });
$("quick").addEventListener("click", (e) => { const b = e.target.closest(".qchip"); if (b) ask(b.textContent); });

// ---------- right rail
function btn([what, id, label]) {
  const cls = what === "approve" || what === "retry" || what === "seerr-approve" ? "btn small" : what.includes("decline") ? "btn small danger" : "btn small secondary";
  return `<button class="${cls}" type="button" data-do="${esc(what)}" data-id="${esc(JSON.stringify(id))}">${esc(label)}</button>`;
}

function needs(v) {
  const n = v.needs;
  const bad = v.warnings.some((x) => x.level === "bad");
  count("needs-meta", n.length + v.warnings.length, bad ? "bad" : n.length + v.warnings.length ? "warn" : "");
  $("needs-list").innerHTML = n.length ? n.map((x) =>
    `<article class="item ${x.src === "WILLOW" ? "urgent" : ""}">
      <div class="meta">${srcTag(x.src)}<span>${esc(x.label)}</span>
        ${x.with_willow ? `<span class="with-willow">${srcTag("WILLOW")} on it</span>` : ""}
        <span style="margin-left:auto">${esc(x.age || ago(x.at))}</span></div>
      ${x.img !== undefined ? `<div class="with-img">${poster(x.img, x.title)}<div style="min-width:0"><div class="t">${esc(x.title)}</div>${x.detail ? `<div class="d">${esc(x.detail)}</div>` : ""}</div></div>`
        : `<div class="t">${esc(x.title)}</div>${x.detail ? `<div class="d">${esc(x.detail)}</div>` : ""}`}
      ${x.actions?.length ? `<div class="row-actions">${x.actions.map(btn).join("")}</div>` : ""}
    </article>`).join("") : v.warnings.length ? "" : `<p class="empty"><span class="led on"></span>Nothing needs you, and nothing's wrong.</p>`;
}
function warnings(v) {
  const w = v.warnings, bad = w.filter((x) => x.level === "bad").length;
  $("warn-head").hidden = !w.length;
  $("warn-list").innerHTML = w.length ? w.map((x) =>
    `<div class="warn-row"><span class="led ${LED[x.level]}"></span><div class="body">
      <div class="t">${esc(x.title)}</div>${x.detail ? `<div class="d">${esc(x.detail)}</div>` : ""}
      ${x.src || x.age || x.with_willow ? `<div class="m">${esc([x.src, x.age, x.with_willow && "Willow's on it"].filter(Boolean).join(" · "))}</div>` : ""}
      ${x.actions?.length ? `<div class="row-actions">${x.actions.map(btn).join("")}</div>` : ""}
    </div></div>`).join("") : "";
}
const KIND = { decision: ["warn", "Decision"], claude: ["neutral", "Claude brief"], todo: ["neutral", "To do"] };
function todo(v) {
  const t = v.todo;
  count("todo-meta", t.length);
  $("todo-list").innerHTML = t.length ? t.map((x) => {
    const done = x.actions.find((a) => a[0] === "done"), copy = x.actions.find((a) => a[0] === "copy");
    const due = dueInfo(x.due), [kc, kl] = KIND[x.kind] || KIND.todo;
    return `<div class="todo-row">
      <button class="check" type="button" data-do="done" data-id="${esc(JSON.stringify(done[1]))}" aria-label="Mark “${esc(x.title)}” done" title="Done">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></button>
      <div class="body"><div class="t">${esc(x.title.replace(/^Claude:\s*(.)/, (_, c) => c.toUpperCase()))}</div>
        <div class="m"><span class="state ${kc}">${kl}</span>${due ? `<span class="${due.cls}">${esc(due.text)}</span>` : ""}<span>${ago(x.at)}</span></div>
        ${x.detail ? `<div class="d">${esc(x.detail)}</div>` : ""}
        ${copy ? `<div class="row-actions">${btn(copy)}</div>` : ""}
      </div></div>`;
  }).join("") : `<p class="empty"><span class="led on"></span>The list is empty.</p>`;
}
function foot(v) {
  const names = { wan: "Public IP", status: "Health", devices: "Devices", willow: "Willow", log: "Log", usage: "Claude", net: "Glances", plex: "Tautulli", jellyfin: "Jellyfin", seerr: "Seerr" };
  const s = Object.entries(v.sources);
  $("foot").innerHTML = `<div class="srcs">${s.map(([k, ok]) => `<span><span class="led ${ok ? "on" : "off"}"></span>${esc(names[k] || k)}</span>`).join("")}</div>
    <div>${s.filter(([, ok]) => ok).length}/${s.length} SOURCES · UPDATED ${new Date(v.at * 1000).toLocaleTimeString("en-AU", { hour12: false })}</div>
    <div>/ JUMP · A ASK WILLOW · R REFRESH</div>`;
}

// ---------- adding to the list (Willow's list: the same one Willow reads out)
$("todo-add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("todo-new"), text = input.value.trim();
  if (!text) return;
  input.disabled = true;
  try {
    const d = await post("api/act", { do: "add", id: text });
    input.value = "";
    toast(d.say || "Added to your list.");
    load();
  } catch (err) {
    toast(`That didn't work: ${err.message}`, "bad");
  } finally {
    input.disabled = false;
    input.focus();
  }
});

// ---------- buttons
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Nexus": "1" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
function toast(text, level = "ok") {
  const el = $("toast");
  el.innerHTML = `<span class="led ${LED[level]}"></span><span>${esc(text)}</span>`;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 6000);
}
document.addEventListener("click", async (e) => {
  const c = e.target.closest("button[data-copy]");
  if (c) {
    try { await navigator.clipboard.writeText(c.dataset.copy); toast(`Copied ${c.dataset.copy}.`); }
    catch { toast("Couldn't copy here: open NEXUS on nexus.wiles.au.", "warn"); }
    return;
  }
  const b = e.target.closest("button[data-do]");
  if (!b) return;
  const what = b.dataset.do, id = JSON.parse(b.dataset.id);
  if (what === "copy") {
    try { await navigator.clipboard.writeText(id); toast("Brief copied. Paste it into Claude Code."); }
    catch { toast("Couldn't copy here: open NEXUS on nexus.wiles.au.", "warn"); }
    return;
  }
  const card = b.closest(".item, .warn-row, .todo-row, .mrow");
  card?.querySelectorAll("button").forEach((x) => (x.disabled = true));
  if (what === "done") b.classList.add("done");
  try {
    const d = await post("api/act", { do: what, id });
    if (what === "pass") b.textContent = "With Willow"; // the item stays: it isn't fixed yet
    else card?.classList.add(what === "done" ? "done" : "gone");
    toast(d.say || "Done.");
    setTimeout(load, 1200);
  } catch (err) {
    b.classList.remove("done");
    card?.querySelectorAll("button").forEach((x) => (x.disabled = false));
    toast(`That didn't work: ${err.message}`, "bad");
  }
});

// ---------- the search palette: "/" slides the search bar to the middle of the screen, blurs the rest
// and searches services and machines. Enter opens the highlighted one; with no match it searches Google.
function filterServices() { /* the palette searches on its own; kept so services() can still call it */ }
const pal = { open: false, items: [], sel: 0 };
function paletteItems(q) {
  if (!view) return [];
  q = q.trim().toLowerCase();
  const svc = allServices(view).map((s) => ({ kind: "app", name: s.name, sub: s.where, icon: s.icon, led: s.led, url: s.url }));
  const machines = [...NODES.values()].filter((n) => !n.off).map((n) => ({
    kind: "machine", name: n.key.startsWith("g:") ? `${n.kind || "LXC"} ${n.id} ${n.name}` : n.name, sub: n.why || "",
    icon: n.icon || null, led: n.led, key: n.key }));
  const all = [...svc, ...machines];
  if (!q) return svc.slice(0, 8);
  const score = (it) => { const n = it.name.toLowerCase(), s = (it.sub || "").toLowerCase();
    return n.startsWith(q) ? 0 : n.split(/\s+/).some((w) => w.startsWith(q)) ? 1 : n.includes(q) ? 2 : s.includes(q) ? 3 : 9; };
  return all.map((it) => [score(it), it]).filter(([s]) => s < 9).sort((a, b) => a[0] - b[0]).slice(0, 8).map(([, it]) => it);
}
function drawPalette() {
  const q = $("pal-input").value;
  pal.items = paletteItems(q);
  pal.sel = Math.min(pal.sel, Math.max(0, pal.items.length - 1));
  $("pal-results").innerHTML = pal.items.map((it, i) =>
    `<li class="pal-item${i === pal.sel ? " on" : ""}" data-i="${i}" role="option" aria-selected="${i === pal.sel}">
      ${it.icon ? icon(it.icon, it.name) : `<span class="ico"><span class="mono-ico">${esc(it.name.split(" ")[1] || it.name[0])}</span></span>`}
      <span class="txt"><span class="nm">${esc(it.name)}</span><span class="ad ellipsis">${esc(it.sub)}</span></span>
      ${it.led ? `<span class="led ${it.led}"></span>` : ""}<span class="kind">${it.kind === "app" ? "OPEN ↗" : "DETAILS"}</span></li>`).join("")
    || (q.trim() ? `<li class="pal-none"><span class="g">G</span>Nothing in the lab matches. <b>Enter</b> searches Google for “${esc(q.trim())}”.</li>` : "");
  $("pal-hint").innerHTML = pal.items.length ? "↑↓ CHOOSE · ↵ OPEN · ESC CLOSE" : "↵ SEARCH GOOGLE · ESC CLOSE";
}
function openPalette() {
  if (pal.open) return;
  pal.open = true;
  hideTip();
  const box = $("pal-box"), from = $("jump-btn").getBoundingClientRect();
  $("palette").hidden = false;
  $("pal-input").value = "";
  pal.sel = 0;
  drawPalette();
  $("pal-input").focus();
  const to = box.getBoundingClientRect();
  // slide from where the search bar sits in the header to the middle of the screen
  const t = `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / Math.max(to.height, 1)})`;
  const ease = "cubic-bezier(0.2, 0.8, 0.2, 1)";
  box.animate([{ transform: t, opacity: 0.4, transformOrigin: "top left" }, { transform: "none", opacity: 1, transformOrigin: "top left" }], { duration: 260, easing: ease });
  $("palette").animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: "ease-out" });
}
async function closePalette() {
  if (!pal.open) return;
  pal.open = false;
  const box = $("pal-box"), to = $("jump-btn").getBoundingClientRect(), from = box.getBoundingClientRect();
  const t = `translate(${to.left - from.left}px, ${to.top - from.top}px) scale(${to.width / from.width}, ${to.height / Math.max(from.height, 1)})`;
  const a = box.animate([{ transform: "none", opacity: 1, transformOrigin: "top left" }, { transform: t, opacity: 0.3, transformOrigin: "top left" }],
    { duration: 200, easing: "cubic-bezier(0.4, 0, 0.6, 1)" });
  $("palette").animate([{ opacity: 1 }, { opacity: 0 }], { duration: 200, easing: "ease-in" });
  // a hidden tab pauses animations, so don't wait on one forever
  await Promise.race([a.finished.catch(() => {}), new Promise((r) => setTimeout(r, 260))]);
  if (!pal.open) $("palette").hidden = true;
}
function choose(it) {
  const q = $("pal-input").value.trim();
  closePalette();
  if (!it) { if (q) window.open(`https://www.google.com/search?q=${encodeURIComponent(q)}`, "_blank", "noopener"); return; }
  if (it.kind === "app") window.open(it.url, "_blank", "noopener");
  else openNode(it.key);
}
$("jump-btn").addEventListener("click", openPalette);
$("pal-input").addEventListener("input", () => { pal.sel = 0; drawPalette(); });
$("pal-input").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); closePalette(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); pal.sel = (pal.sel + 1) % Math.max(1, pal.items.length); drawPalette(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); pal.sel = (pal.sel - 1 + pal.items.length) % Math.max(1, pal.items.length); drawPalette(); }
  else if (e.key === "Enter") { e.preventDefault(); choose(pal.items[pal.sel]); }
});
$("pal-results").addEventListener("mousemove", (e) => {
  const li = e.target.closest(".pal-item");
  if (li && +li.dataset.i !== pal.sel) { pal.sel = +li.dataset.i; drawPalette(); }
});
$("pal-results").addEventListener("click", (e) => { const li = e.target.closest(".pal-item"); if (li) choose(pal.items[+li.dataset.i]); });
$("palette").addEventListener("mousedown", (e) => { if (!e.target.closest("#pal-box")) closePalette(); }); // clicking off closes it
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (typing || e.ctrlKey || e.metaKey || e.altKey || $("dlg").open) return;
  if (e.key === "/") { e.preventDefault(); openPalette(); }
  if (e.key === "a") { e.preventDefault(); $("ask").focus(); }
  if (e.key === "r") load();
});

// ---------- tabs in the side panels: what matters first, the rest one click away
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window: just don't remember */ } } };
function count(id, n, cls = "") { // Night Panel header-tab counts: plain, or .attention (amber) / .attention.bad (red)
  const el = $(id); el.textContent = n || ""; el.className = cls === "warn" ? "count attention" : cls === "bad" ? "count attention bad" : "count"; }
let rightPicked = false; // once you pick a tab on the right, refreshes leave it alone
function selectTab(group, pane, byHand) {
  const sec = document.querySelector(`[data-tabs="${group}"]`);
  if (!sec || !document.getElementById(pane)) return;
  for (const b of sec.querySelectorAll(".ptabs button")) { const on = b.dataset.pane === pane; b.classList.toggle("active", on); b.setAttribute("aria-selected", on); }
  for (const p of sec.querySelectorAll(".tpane")) p.hidden = p.id !== pane;
  if (byHand) { store.set(`nexus-tab-${group}`, pane); if (group === "right") rightPicked = true; }
}
document.querySelectorAll(".tabbed .ptabs").forEach((t) => t.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-pane]");
  if (b) selectTab(b.closest("[data-tabs]").dataset.tabs, b.dataset.pane, true);
}));
document.addEventListener("click", (e) => { // a lamp or link to #needs, #warnings… opens that tab
  const a = e.target.closest('a[href^="#"]');
  const el = a && document.getElementById(a.getAttribute("href").slice(1));
  const pane = el?.classList.contains("tpane") ? el : el?.closest(".tpane");
  if (!pane) return;
  e.preventDefault();
  selectTab(pane.closest("[data-tabs]").dataset.tabs, pane.id, true);
  el.scrollIntoView({ block: "nearest", behavior: "smooth" });
});
function pickTabs(v) {
  if (!rightPicked) selectTab("right", v.needs.length || v.warnings.length ? "needs" : store.get("nexus-tab-right") || "todo");
}
selectTab("left", store.get("nexus-tab-left") || "watching");

// ---------- load
async function load() {
  try {
    const r = await fetch("api/overview", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    view = await r.json();
    lastOk = Date.now();
    for (const h of view.map.hosts) for (const g of h.guests || []) if (!g.off && !hist.has(String(g.id))) remember(String(g.id), g.net);
    for (const [name, fn] of [["hud", hud], ["services", services], ["watching", watching], ["requests", requests], ["map", map],
                              ["willow", willow], ["needs", needs], ["warnings", warnings], ["todo", todo], ["foot", foot], ["tabs", pickTabs]]) {
      try { fn(view); } catch (err) { console.error(name, err); }
    }
    document.querySelectorAll("img[data-slug]").forEach((i) => i.complete && i.naturalWidth && checkDark(i));
    applyNet({ uplink: view.map.uplink, guests: Object.fromEntries(view.map.hosts.flatMap((h) => h.guests || []).filter((g) => g.net).map((g) => [String(g.id), g.net])) });
  } catch (e) {
    $("brief").textContent = `Couldn't load (${e.message}). Trying again shortly.`;
  }
  tick();
}
["needs-list", "warn-list", "todo-list", "svc", "now", "history", "reqs"].forEach((id) => ($(id).innerHTML = '<div class="skeleton"></div>'));
tick();
load();
setInterval(tick, 5000);
setInterval(() => { if (!document.hidden) load(); }, 30000);
setInterval(pollNet, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - lastOk > 25000) load(); });

// ---------- Willow's voice: answers read aloud (the browser's own speech), and a mic to ask by voice where the
// browser can listen (Chrome/Edge; they send the audio to Google/Microsoft to turn it into text).
const synth = window.speechSynthesis;
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
function voiceOn() { return !!synth && store.get("nexus.voice") !== "off"; }
function speak(text) { // the tree "talks" for as long as the voice does (or about as long as it'd take, when muted)
  speakUntil = Date.now() + Math.min(9000, 1800 + (text || "").length * 45);
  if (!voiceOn() || !text) return;
  synth.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-AU";
  u.onend = u.onerror = () => { speakUntil = 0; drawTalk(); };
  speakUntil = Date.now() + 120000; // ponytail: cap in case the browser never says it finished
  synth.speak(u);
}
if (synth) {
  const b = $("voice");
  const show = () => { const on = voiceOn(); b.setAttribute("aria-pressed", on); b.title = on ? "Reading answers aloud: click to mute" : "Muted: click to read answers aloud"; };
  b.hidden = false;
  show();
  b.addEventListener("click", () => { store.set("nexus.voice", voiceOn() ? "off" : "on"); if (!voiceOn()) { synth.cancel(); speakUntil = 0; drawTalk(); } show(); });
}
if (Recognition) {
  const mic = $("mic");
  let rec = null;
  const listening = (on) => { mic.classList.toggle("active", on); mic.setAttribute("aria-pressed", on); mic.title = on ? "Listening: click to stop" : "Speak to Willow (sends when you stop)"; };
  mic.hidden = false;
  listening(false);
  mic.addEventListener("click", () => {
    if (rec) return rec.stop();
    synth?.cancel();
    let heard = "";
    rec = new Recognition();
    rec.lang = "en-AU";
    rec.interimResults = true;
    rec.onresult = (e) => { heard = [...e.results].map((r) => r[0].transcript).join(""); $("ask").value = heard; };
    rec.onerror = (e) => { if (e.error !== "no-speech" && e.error !== "aborted") toast(e.error === "not-allowed" ? "The browser blocked the microphone" : `Mic: ${e.error}`, "warn"); };
    rec.onend = () => { rec = null; listening(false); if (heard.trim()) ask(heard); };
    rec.start();
    listening(true);
  });
}
