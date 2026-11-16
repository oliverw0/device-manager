/* The NEXUS top bar on the older pages: the clock, the status line (from NEXUS's own count of
   what needs Oliver) and the search, which opens NEXUS's search. It sits outside <main>, so Pulse's boosted
   navigation leaves it running from page to page. The weather and its card are static/weather.js. The NEXUS page itself does all this in nexus/static/app.js. */
(function () {
  const $ = (id) => document.getElementById(id);
  if (!$("hud-time")) return;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  let lastOk = Date.now();

  function tick() {
    const d = new Date();
    $("hud-time").textContent = d.toLocaleTimeString("en-AU", { hour: "2-digit", minute: "2-digit", hour12: false });
    $("hud-date").textContent = d.toLocaleDateString("en-AU", { weekday: "short", day: "2-digit", month: "short", year: "numeric" }).replace(/,/g, "");
    $("hud-live").classList.toggle("stale", Date.now() - lastOk > 150000);
  }

  async function brief() {
    try {
      const r = await fetch("/nexus/api/brief", { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) return;
      const n = (await r.json()).issues || 0;
      lastOk = Date.now();
      const b = $("hud-brief");
      b.textContent = n ? `${n} issue${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} you` : "Nothing needs you";
      b.classList.toggle("has", n > 0);
    } catch { /* the dot goes amber when it's stale */ }
  }

  // "/" opens NEXUS's search, as it does on NEXUS itself
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    location.href = "/nexus/?jump=1";
  });

  tick(); brief();
  setInterval(tick, 1000);
  setInterval(brief, 60 * 1000);
})();
