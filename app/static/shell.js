/* The NEXUS top bar on the older pages: the clock, Melbourne's weather, the status line (from NEXUS's own count of
   what needs Oliver) and the search, which opens NEXUS's search. It sits outside <main>, so Pulse's boosted
   navigation leaves it running from page to page. The NEXUS page itself does all this in nexus/static/app.js. */
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

  const SKY = { 0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    56: "Icy drizzle", 57: "Icy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Icy rain", 67: "Icy rain", 71: "Light snow", 73: "Snow",
    75: "Heavy snow", 77: "Snow", 80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Storms", 96: "Storms, hail", 99: "Storms, hail" };
  async function weather() {
    try {
      const q = new URLSearchParams({ latitude: -37.81, longitude: 144.96, timezone: "Australia/Melbourne", forecast_days: 1,
        current: "temperature_2m,weather_code", daily: "temperature_2m_max,temperature_2m_min,precipitation_probability_max" });
      const r = await fetch(`https://api.open-meteo.com/v1/forecast?${q}`);
      if (!r.ok) return;
      const d = await r.json(), c = d.current, day = d.daily, rain = day.precipitation_probability_max?.[0];
      $("hud-wx-now").innerHTML = `<b>${Math.round(c.temperature_2m)}°</b> ${esc(SKY[c.weather_code] || "")}`;
      $("hud-wx-day").textContent = `${Math.round(day.temperature_2m_min[0])}–${Math.round(day.temperature_2m_max[0])}°${rain ? ` · RAIN ${rain}%` : ""} · MELBOURNE`;
      $("hud-wx").hidden = false;
    } catch { /* no weather is fine */ }
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

  tick(); weather(); brief();
  setInterval(tick, 1000);
  setInterval(brief, 60 * 1000);
  setInterval(weather, 20 * 60 * 1000);
})();
