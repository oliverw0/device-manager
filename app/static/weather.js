/* The top bar's weather, on every page (NEXUS and the older ones): the reading in the bar, and a card when it's
   clicked. The card's header is a sky that matches the weather and the time of day (sun, moon and stars, clouds,
   rain, a storm's flashes, fog, snow; dawn and dusk tints), with Today (the next 24 hours, a temperature curve, the
   details) and 7 days tabs. Open-Meteo: free, no key, the same rough coordinates Willow uses. Every 20 minutes.
   Markup it needs in the top bar: <div class="hud-wx" hidden><span class="wx-now"></span><span class="wx-day"></span></div> */
(function () {
  const bar = document.querySelector(".hud .hud-wx");
  if (!bar) return;
  const PLACE = { name: "Melbourne", lat: -37.81, lon: 144.96, tz: "Australia/Melbourne" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const SKY = { 0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    56: "Icy drizzle", 57: "Icy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Icy rain", 67: "Icy rain", 71: "Light snow", 73: "Snow",
    75: "Heavy snow", 77: "Snow", 80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers", 95: "Storms", 96: "Storms, hail", 99: "Storms, hail" };
  const kind = (c) => c <= 1 ? "clear" : c === 2 ? "partly" : c === 3 ? "cloudy" : c === 45 || c === 48 ? "fog" : c >= 51 && c <= 57 ? "drizzle"
    : (c >= 61 && c <= 67) || (c >= 80 && c <= 82) ? "rain" : (c >= 71 && c <= 77) || c === 85 || c === 86 ? "snow" : c >= 95 ? "storm" : "cloudy";
  const round = (x) => Math.round(x);
  const hour12 = (d) => { const h = d.getHours() % 12 || 12; return `${h} ${d.getHours() < 12 ? "am" : "pm"}`; };
  const hm = (d) => d.toLocaleTimeString("en-AU", { hour: "numeric", minute: "2-digit", hour12: true }).replace(/\s/g, " ").toLowerCase();
  const compass = (deg) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(((deg % 360) + 360) % 360 / 45) % 8];
  const local = (s) => new Date(s);  // Open-Meteo gives local times without an offset, read as this browser's time (Melbourne)

  // ---------- small line icons, one per kind of weather
  function icon(code, day = true, size = 22) {
    const k = kind(code), s = `width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"`;
    const sun = `<circle cx="12" cy="12" r="4.2" class="i-sun"/><path class="i-sun" d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>`;
    const moon = `<path class="i-moon" d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>`;
    const cloud = (y = 0) => `<path class="i-cloud" d="M7 ${18 + y}h10.5a3.5 3.5 0 0 0 .4-7A5.5 5.5 0 0 0 7.3 ${10.6 + y}A3.8 3.8 0 0 0 7 ${18 + y}z"/>`;
    const small = day ? `<g transform="translate(-3.5 -4) scale(.62)">${sun}</g>` : `<g transform="translate(-2 -3) scale(.6)">${moon}</g>`;
    const drops = (n) => [...Array(n)].map((_, i) => `<path class="i-rain" d="M${8.5 + i * 3.5} 20.5l-1 2"/>`).join("");
    if (k === "clear") return `<svg ${s}>${day ? sun : moon}</svg>`;
    if (k === "partly") return `<svg ${s}>${small}${cloud(1)}</svg>`;
    if (k === "cloudy") return `<svg ${s}>${cloud()}</svg>`;
    if (k === "fog") return `<svg ${s}><path class="i-cloud" d="M4 9h16M3 13h18M5 17h14"/></svg>`;
    if (k === "drizzle") return `<svg ${s}>${cloud(-2)}${drops(2)}</svg>`;
    if (k === "rain") return `<svg ${s}>${cloud(-2)}${drops(3)}</svg>`;
    if (k === "snow") return `<svg ${s}>${cloud(-2)}<path class="i-snow" d="M9 21h.01M12.5 21.5h.01M16 21h.01"/></svg>`;
    return `<svg ${s}>${cloud(-2)}<path class="i-bolt" d="M12.5 15.5l-2 3.5h3l-2 3.5"/></svg>`;
  }

  // ---------- data
  let data = null;
  async function load() {
    try {
      const q = new URLSearchParams({
        latitude: PLACE.lat, longitude: PLACE.lon, timezone: PLACE.tz, forecast_days: 7,
        current: "temperature_2m,apparent_temperature,relative_humidity_2m,is_day,weather_code,wind_speed_10m,wind_direction_10m,precipitation",
        hourly: "temperature_2m,weather_code,precipitation_probability,is_day",
        daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset,uv_index_max,wind_speed_10m_max",
      });
      const r = await fetch(`https://api.open-meteo.com/v1/forecast?${q}`);
      if (!r.ok) return;
      data = await r.json();
      data.at = new Date();
      drawBar();
      if (!pop.hidden) drawCard();
    } catch { /* no weather is fine */ }
  }

  function drawBar() {
    const c = data.current, d = data.daily, rain = d.precipitation_probability_max?.[0];
    bar.querySelector(".wx-now").innerHTML = `<b>${round(c.temperature_2m)}°</b> ${esc(SKY[c.weather_code] || "")}`;
    bar.querySelector(".wx-day").textContent = `${round(d.temperature_2m_min[0])}–${round(d.temperature_2m_max[0])}°${rain ? ` · RAIN ${rain}%` : ""} · ${PLACE.name.toUpperCase()}`;
    bar.hidden = false;
  }

  // ---------- the card
  bar.setAttribute("role", "button");
  bar.setAttribute("tabindex", "0");
  bar.setAttribute("aria-haspopup", "dialog");
  bar.setAttribute("aria-expanded", "false");
  bar.title = "Weather: today and the week";
  const pop = document.createElement("div");
  pop.className = "wx-pop";
  pop.id = "wx-pop";
  pop.hidden = true;
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", `Weather in ${PLACE.name}`);
  document.body.appendChild(pop);
  bar.setAttribute("aria-controls", pop.id);
  let tab = "today";

  // the part of the day it is: dawn and dusk are the 45 minutes either side of sunrise and sunset
  function partOfDay(now) {
    const rise = local(data.daily.sunrise[0]), set = local(data.daily.sunset[0]), m = 45 * 60000;
    if (Math.abs(now - rise) < m) return "dawn";
    if (Math.abs(now - set) < m) return "dusk";
    return now > rise && now < set ? "day" : "night";
  }

  // ?wxsky=rain,night shows that sky whatever the weather (kinds: clear partly cloudy fog drizzle rain snow storm;
  // parts: day dawn dusk night), for checking how each one looks
  const PREVIEW = (new URLSearchParams(location.search).get("wxsky") || "").split(",");
  function sky(c, now) {
    const k = PREVIEW[0] || kind(c.weather_code), part = PREVIEW[1] || partOfDay(now);
    const lit = part === "day" || part === "dawn" || part === "dusk";
    const clouds = ["partly", "cloudy", "drizzle", "rain", "storm", "snow"].includes(k);
    const stars = !lit && (k === "clear" || k === "partly");
    return `<div class="wx-sky k-${k} p-${part}" aria-hidden="true">
      ${stars ? `<i class="wx-stars"></i><i class="wx-stars s2"></i>` : ""}
      ${(k === "clear" || k === "partly") ? (lit ? `<i class="wx-sun"></i>` : `<i class="wx-moon"></i>`) : ""}
      ${clouds ? `<i class="wx-cloud c1"></i><i class="wx-cloud c2"></i><i class="wx-cloud c3"></i>` : ""}
      ${k === "fog" ? `<i class="wx-fog f1"></i><i class="wx-fog f2"></i><i class="wx-fog f3"></i>` : ""}
      ${["drizzle", "rain", "storm"].includes(k) ? `<i class="wx-rain${k === "drizzle" ? " light" : ""}"></i>` : ""}
      ${k === "snow" ? `<i class="wx-snow"></i>` : ""}
      ${k === "storm" ? `<i class="wx-flash"></i>` : ""}
      <i class="wx-shade"></i></div>`;
  }

  function today(now) {
    const h = data.hourly, d = data.daily, c = data.current;
    const start = h.time.findIndex((t) => local(t) > new Date(now.getTime() - 3600000));
    const hours = [...Array(24)].map((_, i) => start + i).filter((i) => i >= 0 && i < h.time.length).map((i) => ({
      t: local(h.time[i]), temp: h.temperature_2m[i], code: h.weather_code[i], rain: h.precipitation_probability[i], day: h.is_day[i] === 1 }));
    const strip = hours.map((x, i) => `<div class="wx-hr"><span class="t">${i === 0 ? "Now" : hour12(x.t)}</span>${icon(x.code, x.day)}
      <b>${round(x.temp)}°</b><span class="r">${x.rain >= 10 ? `${x.rain}%` : ""}</span></div>`).join("");
    // the temperature over those hours, as a curve
    const temps = hours.map((x) => x.temp), lo = Math.min(...temps), hi = Math.max(...temps), span = Math.max(1, hi - lo);
    const W = 100, H = 30, pts = temps.map((t, i) => [i / Math.max(1, temps.length - 1) * W, H - 3 - (t - lo) / span * (H - 8)]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
    const rise = local(d.sunrise[0]), set = local(d.sunset[0]);
    const kv = [["Feels like", `${round(c.apparent_temperature)}°`], ["Humidity", `${round(c.relative_humidity_2m)}%`],
      ["Wind", `${round(c.wind_speed_10m)} km/h ${compass(c.wind_direction_10m)}`], ["UV today", d.uv_index_max?.[0] != null ? `${round(d.uv_index_max[0])}${d.uv_index_max[0] >= 8 ? " · very high" : d.uv_index_max[0] >= 6 ? " · high" : d.uv_index_max[0] >= 3 ? " · moderate" : " · low"}` : "–"],
      ["Sunrise", hm(rise)], ["Sunset", hm(set)],
      ["Rain chance", `${d.precipitation_probability_max?.[0] ?? 0}%`], ["Rain today", `${(d.precipitation_sum?.[0] ?? 0).toFixed(1)} mm`]];
    return `<div class="wx-hours" tabindex="0" aria-label="The next 24 hours">${strip}</div>
      <div class="wx-curve"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
        <defs><linearGradient id="wx-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="currentColor" stop-opacity=".28"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>
        <path d="${line} L${W} ${H} L0 ${H} Z" fill="url(#wx-fill)"/><path d="${line}" fill="none" stroke="currentColor" stroke-width="1.4" vector-effect="non-scaling-stroke"/></svg>
        <span class="lo">${round(lo)}°</span><span class="hi">${round(hi)}°</span></div>
      <dl class="wx-kv">${kv.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`;
  }

  function week() {
    const d = data.daily, lo = Math.min(...d.temperature_2m_min), hi = Math.max(...d.temperature_2m_max), span = Math.max(1, hi - lo);
    return `<ol class="wx-week">${d.time.map((t, i) => {
      const day = i === 0 ? "Today" : local(`${t}T12:00`).toLocaleDateString("en-AU", { weekday: "short", day: "numeric" });
      const a = (d.temperature_2m_min[i] - lo) / span * 100, b = (d.temperature_2m_max[i] - lo) / span * 100, rain = d.precipitation_probability_max?.[i] ?? 0;
      return `<li><span class="d">${esc(day)}</span><span class="ic" title="${esc(SKY[d.weather_code[i]] || "")}">${icon(d.weather_code[i], true)}</span>
        <span class="r${rain >= 30 ? " wet" : ""}">${rain >= 10 ? `${rain}%` : ""}</span>
        <span class="lo">${round(d.temperature_2m_min[i])}°</span>
        <span class="range"><i style="left:${a.toFixed(1)}%;right:${(100 - b).toFixed(1)}%"></i></span>
        <span class="hi">${round(d.temperature_2m_max[i])}°</span></li>`;
    }).join("")}</ol>`;
  }

  function drawCard() {
    if (!data) { pop.innerHTML = `<p class="wx-wait">Getting the weather…</p>`; return; }
    const now = new Date(), c = data.current, d = data.daily;
    pop.innerHTML = `
      <header class="wx-hero">${sky(c, now)}
        <div class="wx-hero-in">
          <div class="wx-place">${PLACE.name.toUpperCase()} · ${now.toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short" }).toUpperCase()}</div>
          <div class="wx-big"><b>${round(c.temperature_2m)}°</b><span class="ic">${icon(c.weather_code, c.is_day === 1, 34)}</span></div>
          <div class="wx-cond">${esc(SKY[c.weather_code] || "")}</div>
          <div class="wx-sub">Feels ${round(c.apparent_temperature)}° · H ${round(d.temperature_2m_max[0])}° L ${round(d.temperature_2m_min[0])}°</div>
        </div>
        <button type="button" class="wx-x" aria-label="Close">×</button>
      </header>
      <div class="wx-tabs" role="tablist">
        <button type="button" role="tab" data-wx-tab="today" aria-selected="${tab === "today"}" class="${tab === "today" ? "on" : ""}">Today</button>
        <button type="button" role="tab" data-wx-tab="week" aria-selected="${tab === "week"}" class="${tab === "week" ? "on" : ""}">7 days</button>
        <span class="wx-upd">UPDATED ${data.at.toLocaleTimeString("en-AU", { hour: "2-digit", minute: "2-digit", hour12: false })}</span>
      </div>
      <div class="wx-body">${tab === "today" ? today(now) : week()}</div>`;
  }

  function place() {
    const r = bar.getBoundingClientRect(), w = Math.min(400, window.innerWidth - 24);
    pop.style.width = `${w}px`;
    pop.style.top = `${Math.round(r.bottom + 8)}px`;
    pop.style.left = `${Math.round(Math.max(12, Math.min(r.left - 12, window.innerWidth - w - 12)))}px`;
  }
  function open() {
    drawCard(); place();
    pop.hidden = false;
    bar.setAttribute("aria-expanded", "true");
    bar.classList.add("open");
    requestAnimationFrame(() => pop.classList.add("in"));
  }
  function close() {
    pop.classList.remove("in");
    pop.hidden = true;
    bar.setAttribute("aria-expanded", "false");
    bar.classList.remove("open");
  }
  bar.addEventListener("click", () => (pop.hidden ? open() : close()));
  bar.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pop.hidden ? open() : close(); } });
  pop.addEventListener("click", (e) => {
    const t = e.target.closest("[data-wx-tab]");
    if (t) { tab = t.dataset.wxTab; drawCard(); return; }
    if (e.target.closest(".wx-x")) close();
  });
  // outside the card closes it. The click's path is fixed when it starts, so a tab that redraws the card (and so
  // leaves the page) still counts as inside
  document.addEventListener("click", (e) => { const path = e.composedPath(); if (!pop.hidden && !path.includes(pop) && !path.includes(bar)) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.hidden) { close(); bar.focus(); } });
  window.addEventListener("resize", () => { if (!pop.hidden) place(); });
  window.addEventListener("scroll", () => { if (!pop.hidden) place(); }, { passive: true });

  load();
  setInterval(load, 20 * 60 * 1000);
})();
