function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderDeviceRows(devices) {
  const tbody = document.querySelector("#device-table tbody");
  if (!tbody) return;
  if (devices.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7">No devices yet. Add one below.</td></tr>';
    return;
  }
  tbody.innerHTML = devices.map((d) => {
    const r = d.report;
    const pct = (v) => (v === undefined || v === null ? "-" : Math.round(v) + "%");
    const ts = d.report ? r.tailscale : null;
    return `<tr onclick="window.location='/devices/${d.id}'">
      <td><span class="dot ${d.is_online ? "online" : "offline"}"></span></td>
      <td>${escapeHtml(d.name)}</td>
      <td>${pct(r && r.system && r.system.cpu_percent)}</td>
      <td>${pct(r && r.system && r.system.mem_percent)}</td>
      <td>${pct(r && r.system && r.system.disk_percent)}</td>
      <td>${ts ? (ts.connected ? "up" : "down") : "-"}</td>
      <td>${d.last_seen_at ? escapeHtml(d.last_seen_at) : "never"}</td>
    </tr>`;
  }).join("");
}

function startDevicePolling(url, intervalMs = 5000) {
  const poll = () => fetch(url).then((r) => r.json()).then(renderDeviceRows).catch(() => {});
  poll();
  setInterval(poll, intervalMs);
}

function drawSparkline(canvas, series, colors) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#2a2e38";
  ctx.strokeRect(0, 0, w, h);

  const allPoints = series.flat();
  if (allPoints.length === 0) return;
  const maxY = 100;

  series.forEach((points, i) => {
    if (points.length < 2) return;
    ctx.beginPath();
    ctx.strokeStyle = colors[i];
    ctx.lineWidth = 2;
    points.forEach((v, idx) => {
      const x = (idx / (points.length - 1)) * (w - 10) + 5;
      const y = h - 5 - (v / maxY) * (h - 10);
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
    drawSparkline(canvas, [cpu, mem, disk], ["#ef5350", "#4c8dff", "#3ecf6a"]);
  }).catch(() => {});
  render();
  setInterval(render, 10000);
}
