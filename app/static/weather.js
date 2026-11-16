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
        hourly: "temperature_2m,weather_code,precipitation_probability,precipitation,is_day",
        daily: "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset,uv_index_max,wind_speed_10m_max",
      });
      const r = await fetch(`https://api.open-meteo.com/v1/forecast?${q}`);
      if (!r.ok) return;
      data = await r.json();
      data.at = new Date();
      drawBar();
      if (!ov.hidden) drawCard();
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
  // the card opens in the middle of the page over a darkened, blurred backdrop; clicking the backdrop or × closes it
  const ov = document.createElement("div");
  ov.className = "wx-overlay";
  ov.hidden = true;
  const pop = document.createElement("div");
  pop.className = "wx-pop";
  pop.id = "wx-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-modal", "true");
  pop.tabIndex = -1;   // focus goes to the card itself on open, so Esc and Tab work without lighting up a button
  pop.setAttribute("aria-label", `Weather in ${PLACE.name}`);
  ov.appendChild(pop);
  document.body.appendChild(ov);
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
      t: local(h.time[i]), temp: h.temperature_2m[i], code: h.weather_code[i], rain: h.precipitation_probability[i],
      mm: h.precipitation?.[i] ?? 0, day: h.is_day[i] === 1 }));
    chartHours = hours;
    const strip = hours.map((x, i) => `<div class="wx-hr"><span class="t">${i === 0 ? "Now" : hour12(x.t)}</span>${icon(x.code, x.day)}
      <b>${round(x.temp)}°</b><span class="r">${x.rain >= 10 ? `${x.rain}%` : ""}</span></div>`).join("");
    // the next 24 hours as a chart: the temperature as a line, the chance of rain as bars under it (darker where more
    // rain is expected). Hover it for the hour's reading.
    const n = hours.length, temps = hours.map((x) => x.temp), lo = Math.min(...temps), hi = Math.max(...temps), span = Math.max(1, hi - lo);
    const W = 240, H = 100, TOP = 8, TEMP_H = 46, RAIN_TOP = 62, RAIN_H = 36, step = W / n;
    const x = (i) => (i + 0.5) * step;
    const pts = temps.map((t, i) => [x(i), TOP + (1 - (t - lo) / span) * TEMP_H]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
    const bars = hours.map((hr, i) => {
      const bh = Math.max(hr.rain > 0 ? 1.5 : 0, hr.rain / 100 * RAIN_H);
      const op = hr.mm >= 2 ? 0.95 : hr.mm >= 0.5 ? 0.75 : hr.mm > 0 ? 0.55 : 0.35;
      return bh ? `<rect x="${(i * step + step * 0.18).toFixed(1)}" y="${(RAIN_TOP + RAIN_H - bh).toFixed(1)}" width="${(step * 0.64).toFixed(1)}" height="${bh.toFixed(1)}" rx="1" fill="#5aa0ff" fill-opacity="${op}"/>` : "";
    }).join("");
    const ticks = hours.map((hr, i) => i % 6 === 0 ? `<span style="left:${(x(i) / W * 100).toFixed(2)}%">${i === 0 ? "Now" : hour12(hr.t)}</span>` : "").join("");
    const totalMm = hours.reduce((a, hr) => a + hr.mm, 0);
    const rise = local(d.sunrise[0]), set = local(d.sunset[0]);
    const kv = [["Feels like", `${round(c.apparent_temperature)}°`], ["Humidity", `${round(c.relative_humidity_2m)}%`],
      ["Wind", `${round(c.wind_speed_10m)} km/h ${compass(c.wind_direction_10m)}`], ["UV today", d.uv_index_max?.[0] != null ? `${round(d.uv_index_max[0])}${d.uv_index_max[0] >= 8 ? " · very high" : d.uv_index_max[0] >= 6 ? " · high" : d.uv_index_max[0] >= 3 ? " · moderate" : " · low"}` : "–"],
      ["Sunrise", hm(rise)], ["Sunset", hm(set)],
      ["Rain chance", `${d.precipitation_probability_max?.[0] ?? 0}%`], ["Rain today", `${(d.precipitation_sum?.[0] ?? 0).toFixed(1)} mm`]];
    return `<div class="wx-hours" tabindex="0" aria-label="The next 24 hours">${strip}</div>
      <div class="wx-chart-head"><span><i class="k-temp"></i>Temperature ${round(lo)}–${round(hi)}°</span>
        <span><i class="k-rain"></i>Rain ${totalMm >= 0.1 ? `${totalMm.toFixed(1)} mm expected` : "none expected"}</span></div>
      <div class="wx-chart" data-n="${n}">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
          <defs><linearGradient id="wx-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffb038" stop-opacity=".26"/><stop offset="1" stop-color="#ffb038" stop-opacity="0"/></linearGradient></defs>
          <line x1="0" x2="${W}" y1="${RAIN_TOP + RAIN_H}" y2="${RAIN_TOP + RAIN_H}" class="base"/>
          <line x1="0" x2="${W}" y1="${RAIN_TOP}" y2="${RAIN_TOP}" class="grid"/>
          ${bars}
          <path d="${line} L${x(n - 1)} ${TOP + TEMP_H + 4} L${x(0)} ${TOP + TEMP_H + 4} Z" fill="url(#wx-fill)"/>
          <path d="${line}" fill="none" stroke="#ffb038" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
        </svg>
        <i class="wx-guide" hidden></i><i class="wx-dot" hidden></i>
        <div class="wx-tip" hidden></div>
        <div class="wx-ticks">${ticks}</div>
      </div>
      <dl class="wx-kv">${kv.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`;
  }

  // hovering (or touching) the chart: a guide on the nearest hour and its reading
  let chartHours = [];
  function chartAt(chart, clientX) {
    const r = chart.getBoundingClientRect(), n = chartHours.length;
    if (!n) return;
    const i = Math.max(0, Math.min(n - 1, Math.floor((clientX - r.left) / r.width * n)));
    const hr = chartHours[i], pct = (i + 0.5) / n * 100;
    const temps = chartHours.map((h) => h.temp), lo = Math.min(...temps), span = Math.max(1, Math.max(...temps) - lo);
    const guide = chart.querySelector(".wx-guide"), dot = chart.querySelector(".wx-dot"), tip = chart.querySelector(".wx-tip");
    guide.style.left = dot.style.left = `${pct}%`;
    dot.style.top = `${(8 + (1 - (hr.temp - lo) / span) * 46) / 100 * 100}%`;
    tip.innerHTML = `<b>${i === 0 ? "Now" : hour12(hr.t)}</b> ${icon(hr.code, hr.day, 14)} ${round(hr.temp)}°
      <span class="rain">${hr.rain}% rain${hr.mm > 0 ? ` · ${hr.mm.toFixed(1)} mm` : ""}</span>`;
    tip.style.left = `${Math.min(Math.max(pct, 18), 82)}%`;
    guide.hidden = dot.hidden = tip.hidden = false;
  }
  function chartOff(chart) { chart.querySelectorAll(".wx-guide, .wx-dot, .wx-tip").forEach((e) => (e.hidden = true)); }
  pop.addEventListener("pointermove", (e) => { const c = e.target.closest(".wx-chart"); if (c) chartAt(c, e.clientX); });
  pop.addEventListener("pointerdown", (e) => { const c = e.target.closest(".wx-chart"); if (c) chartAt(c, e.clientX); });
  pop.addEventListener("pointerout", (e) => { const c = e.target.closest(".wx-chart"); if (c && !c.contains(e.relatedTarget)) chartOff(c); });

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

  let lastFocus = null;
  function open() {
    drawCard();
    lastFocus = document.activeElement;
    ov.hidden = false;
    document.documentElement.classList.add("wx-lock");
    bar.setAttribute("aria-expanded", "true");
    bar.classList.add("open");
    requestAnimationFrame(() => { ov.classList.add("in"); pop.focus({ preventScroll: true }); });
  }
  function close() {
    ov.classList.remove("in");
    ov.hidden = true;
    document.documentElement.classList.remove("wx-lock");
    bar.setAttribute("aria-expanded", "false");
    bar.classList.remove("open");
    (lastFocus && lastFocus.focus ? lastFocus : bar).focus({ preventScroll: true });
  }
  bar.addEventListener("click", () => (ov.hidden ? open() : close()));
  bar.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ov.hidden ? open() : close(); } });
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });   // the backdrop, not the card
  pop.addEventListener("click", (e) => {
    const t = e.target.closest("[data-wx-tab]");
    if (t) { tab = t.dataset.wxTab; drawCard(); return; }
    if (e.target.closest(".wx-x")) close();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !ov.hidden) close(); });

  load();
  setInterval(load, 20 * 60 * 1000);
})();
