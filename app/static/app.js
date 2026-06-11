const fmtRate = (bps) => {
  if (!bps || bps < 1) return "0 bps";
  const units = ["bps", "Kbps", "Mbps", "Gbps"];
  let i = 0;
  while (bps >= 1000 && i < units.length - 1) { bps /= 1000; i++; }
  return `${bps.toFixed(bps >= 100 ? 0 : bps >= 10 ? 1 : 2)} ${units[i]}`;
};

// Pick one bandwidth unit for an entire chart axis based on the data's
// peak — keeps the Y axis from flickering between Kbps and Mbps as new
// samples come in. Returns {divisor, label}.
const pickAxisUnit = (maxBps) => {
  if (maxBps >= 1e9) return { divisor: 1e9, label: "Gbps" };
  if (maxBps >= 1e6) return { divisor: 1e6, label: "Mbps" };
  if (maxBps >= 1e3) return { divisor: 1e3, label: "Kbps" };
  return { divisor: 1, label: "bps" };
};

const fmtRateInUnit = (bps, unit) => {
  const v = (bps || 0) / unit.divisor;
  const digits = v >= 100 ? 0 : v >= 10 ? 1 : 2;
  return `${v.toFixed(digits)} ${unit.label}`;
};

// Transfer volumes (bytes). Unlike the rates, these are exact — the per-bucket
// sums equal what the router's flow records reported.
const fmtBytes = (b) => {
  if (!b || b < 1) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1000 && i < units.length - 1) { b /= 1000; i++; }
  return `${b.toFixed(i === 0 || b >= 100 ? 0 : b >= 10 ? 1 : 2)} ${units[i]}`;
};

const pickByteUnit = (maxBytes) => {
  if (maxBytes >= 1e12) return { divisor: 1e12, label: "TB" };
  if (maxBytes >= 1e9) return { divisor: 1e9, label: "GB" };
  if (maxBytes >= 1e6) return { divisor: 1e6, label: "MB" };
  if (maxBytes >= 1e3) return { divisor: 1e3, label: "KB" };
  return { divisor: 1, label: "B" };
};

const fmtBytesInUnit = (b, unit) => {
  const v = (b || 0) / unit.divisor;
  const digits = v >= 100 || unit.divisor === 1 ? 0 : v >= 10 ? 1 : 2;
  return `${v.toFixed(digits)} ${unit.label}`;
};

const vol1h = (d) => (d.vol_tx_1h || 0) + (d.vol_rx_1h || 0);
const vol24h = (d) => (d.vol_tx_24h || 0) + (d.vol_rx_24h || 0);

const fmtTime = (ts) => {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
};

const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

let devices = [];
let mainChart = null;
let modalChart = null;
let modalUsageChart = null;
let selectedMac = null;
// Default sort: 24h volume. Volume is the exact measurement (who consumed a
// lot), current rate is the estimate (and reads 0 mid-long-flow).
let sortKey = "vol_24h";
let sortDir = "desc"; // "asc" | "desc"

const SORT_KEYS = new Set(["hostname", "ip", "mac", "tx_bps", "rx_bps", "vol_1h", "vol_24h"]);

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

async function refreshHealth() {
  try {
    const [h, wan] = await Promise.all([
      fetchJSON("/api/health"),
      fetchJSON("/api/wan/current").catch(() => []),
    ]);
    const el = document.getElementById("health");
    const age = h.last_poll_age_s;
    const wanStr = wan.length
      ? wan.map(w => `${w.wan} ↑ ${fmtRate(w.tx_bps)} ↓ ${fmtRate(w.rx_bps)}`).join(" • ")
      : "";
    if (h.last_poll_ok && age !== null && age < h.poll_interval_s * 3) {
      const wanSuffix = wanStr ? `<span class="wan-live">${wanStr}</span>` : "";
      el.innerHTML = `router ${escapeHtml(h.router)} • last poll ${age}s ago${wanSuffix ? " • " + wanSuffix : ""}`;
      el.className = "health ok";
    } else if (h.last_error) {
      el.textContent = `error: ${h.last_error}`;
      el.className = "health bad";
    } else {
      el.textContent = `router ${h.router} • waiting for first poll`;
      el.className = "health";
    }
  } catch (e) {
    document.getElementById("health").textContent = "API unreachable";
  }
}

async function refreshDevices() {
  devices = await fetchJSON("/api/devices");
  renderDeviceTable();
  refreshTiles();
  if (selectedMac) {
    const d = devices.find(x => x.mac === selectedMac);
    if (d) updateModalHeader(d);
  }
}

function refreshTiles() {
  const nowS = Date.now() / 1000;
  const sumTx = devices.reduce((s, d) => s + (d.tx_bps || 0), 0);
  const sumRx = devices.reduce((s, d) => s + (d.rx_bps || 0), 0);
  const online = devices.filter(d => d.last_seen && nowS - d.last_seen < 300).length;
  const top = devices.slice().sort((a, b) => vol24h(b) - vol24h(a))[0];
  document.getElementById("t-now").textContent = `↓ ${fmtRate(sumRx)}   ↑ ${fmtRate(sumTx)}`;
  document.getElementById("t-vol24").textContent =
    fmtBytes(devices.reduce((s, d) => s + vol24h(d), 0));
  document.getElementById("t-online").textContent = `${online} / ${devices.length}`;
  const tt = document.getElementById("t-top");
  const ts = document.getElementById("t-top-sub");
  if (top && vol24h(top) > 0) {
    tt.textContent = top.hostname || top.ip || top.mac;
    ts.textContent = `${fmtBytes(vol24h(top))} (↓ ${fmtBytes(top.vol_rx_24h)} ↑ ${fmtBytes(top.vol_tx_24h)})`;
  } else {
    tt.textContent = "—";
    ts.innerHTML = "&nbsp;";
  }
}

function sortValue(d, key) {
  if (key === "hostname") return (d.hostname || "").toLowerCase();
  if (key === "ip") {
    // IPv4 numeric sort
    const parts = (d.ip || "").split(".").map(n => parseInt(n, 10) || 0);
    return parts[0] * (1 << 24) + parts[1] * (1 << 16) + parts[2] * 256 + parts[3];
  }
  if (key === "mac") return (d.mac || "").toLowerCase();
  if (key === "vol_1h") return vol1h(d);
  if (key === "vol_24h") return vol24h(d);
  return d[key] || 0;
}

function applySort(rows) {
  const dir = sortDir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = sortValue(a, sortKey);
    const bv = sortValue(b, sortKey);
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return 0;
  });
}

function updateSortIndicators() {
  document.querySelectorAll("#device-table th.sortable").forEach(th => {
    const k = th.dataset.sort;
    const ind = th.querySelector(".sort-ind");
    if (k === sortKey) {
      th.classList.add("sorted");
      ind.textContent = sortDir === "asc" ? "▲" : "▼";
    } else {
      th.classList.remove("sorted");
      ind.textContent = "";
    }
  });
}

function renderDeviceTable() {
  const filter = document.getElementById("filter").value.toLowerCase();
  const tbody = document.querySelector("#device-table tbody");
  const maxVol = Math.max(1, ...devices.map(vol24h));
  const filtered = devices.filter(d => {
    if (!filter) return true;
    return (d.hostname || "").toLowerCase().includes(filter)
        || (d.ip || "").includes(filter)
        || (d.mac || "").toLowerCase().includes(filter)
        || (d.vendor || "").toLowerCase().includes(filter);
  });
  const rows = applySort(filtered)
    .map(d => {
      const name = escapeHtml(d.hostname || "(unknown)");
      const vendor = d.vendor ? `<div class="vendor">${escapeHtml(d.vendor)}</div>` : "";
      // Open NAT sessions but no fresh flow data: the router only exports a
      // long transfer when it finishes, so the rate is unknown — say so
      // instead of showing a false 0 bps.
      const liveDot = d.rate_source === "live"
        ? `<span class="dot-live" title="live reading from the router's Data Flow Monitor"></span>` : "";
      const rateCells = d.rate_pending
        ? `<td class="num" colspan="2"><span class="pending" title="${d.active_sessions} open NAT session(s) but no flow data — the router reports a long transfer only when it finishes">in progress…</span></td>`
        : `<td class="num">${liveDot}<span class="rate">${fmtRate(d.tx_bps)}</span></td>
           <td class="num"><span class="rate">${fmtRate(d.rx_bps)}</span></td>`;
      const barW = Math.round((vol24h(d) / maxVol) * 100);
      return `<tr data-mac="${escapeHtml(d.mac)}" class="${d.mac === selectedMac ? "active" : ""}">
        <td class="device-cell"><div class="hostname">${name}</div>${vendor}</td>
        <td class="ip">${escapeHtml(d.ip || "")}</td>
        <td class="mac">${escapeHtml(d.mac)}</td>
        ${rateCells}
        <td class="num"><span class="vol">${fmtBytes(vol1h(d))}</span></td>
        <td class="num"><span class="vol">${fmtBytes(vol24h(d))}</span><span class="bar" style="width:${barW}%"></span></td>
      </tr>`;
    })
    .join("");
  tbody.innerHTML = rows || `<tr><td colspan="7" style="text-align:center;padding:20px;color:var(--muted)">No devices yet. Waiting for first poll…</td></tr>`;
  tbody.querySelectorAll("tr[data-mac]").forEach(tr => {
    tr.addEventListener("click", () => openDeviceModal(tr.dataset.mac));
  });
  updateSortIndicators();
}

// The collector writes a sample bucket only when a device has traffic (flows
// are placed in the buckets they spanned), so idle stretches have no points.
// Without this, Chart.js draws a straight line across the gap — making an idle
// hour look like sustained traffic. Insert explicit zeros at the edges of any
// gap wider than GAP_S so the line drops to 0 between bursts.
const GAP_S = 30;

// Trailing window where data is still incomplete: flow records for anything
// in-flight haven't been exported yet (a long transfer arrives only when it
// ends and is then backfilled). Charts shade this region so the right edge
// isn't read as "zero traffic".
const LIVE_EDGE_S = 120;

function fillGaps(points) {
  if (!points.length) return points;
  const out = [];
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    if (i > 0 && p.ts - points[i - 1].ts > GAP_S) {
      out.push({ ts: points[i - 1].ts + 1, tx_bps: 0, rx_bps: 0 });
      out.push({ ts: p.ts - 1, tx_bps: 0, rx_bps: 0 });
    }
    out.push(p);
  }
  // Trailing edge: short flows export within seconds of ending, so a quiet
  // stretch up to the live-edge window really was (close to) zero. Inside the
  // window nothing is known yet — leave it blank under the shaded band rather
  // than drawing zeros that may be backfilled away.
  const nowS = Date.now() / 1000;
  const last = out[out.length - 1];
  if (nowS - LIVE_EDGE_S - last.ts > GAP_S) {
    out.push({ ts: last.ts + 1, tx_bps: 0, rx_bps: 0 });
    out.push({ ts: nowS - LIVE_EDGE_S, tx_bps: 0, rx_bps: 0 });
  }
  return out;
}

// Time-axis options per selected range: multi-day windows tick by date
// (otherwise Chart.js picks hours and a 7-day axis reads "1PM, 6PM, …" with
// no way to tell the days apart). Tooltips always carry the full date.
const timeScaleFor = (hours) => ({
  unit: hours >= 48 ? "day" : undefined,
  displayFormats: { day: "MMM d" },
  tooltipFormat: "MMM d, HH:mm",
});

// Fixed width for the Y-axis gutter on the stacked modal charts. Without
// this, "6.00 GB" vs "120 Mbps" label widths differ, the plot areas start at
// different x pixels, and the two time axes don't line up vertically.
const Y_AXIS_WIDTH = 68;
const yAxisFit = (scale) => { scale.width = Y_AXIS_WIDTH; };

// Chart.js inline plugin: translucent band over the last LIVE_EDGE_S where
// flow records may not have arrived yet.
const liveEdgePlugin = {
  id: "liveEdge",
  beforeDatasetsDraw(chart) {
    const x = chart.scales.x;
    if (!x) return;
    const area = chart.chartArea;
    const nowMs = Date.now();
    const from = Math.max(x.getPixelForValue(nowMs - LIVE_EDGE_S * 1000), area.left);
    const to = Math.min(x.getPixelForValue(nowMs), area.right);
    if (to <= from) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = "rgba(255, 204, 102, 0.08)";
    ctx.fillRect(from, area.top, to - from, area.bottom - area.top);
    ctx.strokeStyle = "rgba(255, 204, 102, 0.35)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(from, area.top);
    ctx.lineTo(from, area.bottom);
    ctx.stroke();
    if (to - from > 80) {
      ctx.fillStyle = "rgba(255, 204, 102, 0.85)";
      ctx.font = "10px sans-serif";
      ctx.textAlign = "right";
      ctx.fillText("data still arriving", to - 5, area.top + 12);
    }
    ctx.restore();
  },
};

function chartConfig(label, points, hours) {
  points = fillGaps(points);
  const peak = points.reduce((m, p) => Math.max(m, p.tx_bps || 0, p.rx_bps || 0), 0);
  const yUnit = pickAxisUnit(peak);
  return {
    type: "line",
    data: {
      datasets: [
        {
          label: "TX (upload)",
          data: points.map(p => ({ x: p.ts * 1000, y: p.tx_bps })),
          borderColor: "#ffb454",
          backgroundColor: "rgba(255,180,84,0.15)",
          borderWidth: 1.5, pointRadius: 0, tension: 0.25, fill: true,
        },
        {
          label: "RX (download)",
          data: points.map(p => ({ x: p.ts * 1000, y: p.rx_bps })),
          borderColor: "#5cc8ff",
          backgroundColor: "rgba(92,200,255,0.15)",
          borderWidth: 1.5, pointRadius: 0, tension: 0.25, fill: true,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: {
        // Pin the axis to the full selected window (matching the volume bars
        // above it) and to "now" so the shaded not-yet-reported edge shows.
        x: { type: "time",
             min: Date.now() - (hours || 1) * 3600e3, max: Date.now(),
             time: { ...timeScaleFor(hours || 1), tooltipFormat: "MMM d, HH:mm:ss" },
             ticks: { color: "#8a96a4" }, grid: { color: "rgba(255,255,255,0.05)" } },
        y: { afterFit: yAxisFit,
             ticks: { color: "#8a96a4", callback: (v) => fmtRateInUnit(v, yUnit) },
             grid: { color: "rgba(255,255,255,0.05)" }, beginAtZero: true },
      },
      plugins: {
        legend: { labels: { color: "#d7dee6" } },
        title: { display: !!label, text: label, color: "#d7dee6" },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmtRate(ctx.parsed.y)}` } },
      },
    },
    plugins: [liveEdgePlugin],
  };
}

// Fill the selected window with explicit zero bins. Two reasons: (1) honest
// gaps — a quiet half hour shows as empty slots, not absent space; (2) bar
// sizing — Chart.js "flex" thickness sizes bars from the spacing between
// adjacent points, so with only sparse non-zero bins an isolated bar would
// stretch to fill the whole gap. Dense bins = every bar exactly one slot wide.
function fillUsageBins(points, bucketS, sinceTs) {
  const byTs = new Map(points.map(p => [p.ts, p]));
  const start = Math.ceil(sinceTs / bucketS) * bucketS;
  const end = Math.floor(Date.now() / 1000 / bucketS) * bucketS;
  const out = [];
  for (let t = start; t <= end; t += bucketS) {
    out.push(byTs.get(t) || { ts: t, tx_bytes: 0, rx_bytes: 0 });
  }
  return out;
}

// Volume-per-bin bar chart (modal + home page). Bin width per range keeps the
// bar count readable; the current (still-filling) bin is drawn faded.
//
// Bars are positioned at the bin MIDPOINT so each one spans exactly the
// interval it represents (a 7–8 AM bin covers 7:00–8:00, not 6:30–7:30), and
// the axis is pinned to the same [since, now] window the rate chart uses —
// the two charts in the modal must line up when read side by side.
function usageChartConfig(points, bucketS, sinceTs, hours) {
  points = fillUsageBins(points, bucketS, sinceTs);
  const halfBinMs = bucketS * 500;
  const nowBinCenterMs =
    Math.floor(Date.now() / 1000 / bucketS) * bucketS * 1000 + halfBinMs;
  const fade = (base, faded) => (ctx) => (ctx.raw && ctx.raw.x >= nowBinCenterMs ? faded : base);
  const peak = points.reduce((m, p) => Math.max(m, (p.tx_bytes || 0) + (p.rx_bytes || 0)), 0);
  const yUnit = pickByteUnit(peak);
  const binRange = (centerMs) => {
    const s = new Date(centerMs - halfBinMs);
    const e = new Date(centerMs + halfBinMs);
    const sFmt = s.toLocaleString(undefined,
      { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const eFmt = e.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return `${sFmt} – ${eFmt}`;
  };
  return {
    type: "bar",
    data: {
      datasets: [
        {
          label: "TX (upload)",
          data: points.map(p => ({ x: p.ts * 1000 + halfBinMs, y: p.tx_bytes })),
          backgroundColor: fade("rgba(255,180,84,0.8)", "rgba(255,180,84,0.35)"),
          barThickness: "flex", barPercentage: 1.0, categoryPercentage: 0.92,
        },
        {
          label: "RX (download)",
          data: points.map(p => ({ x: p.ts * 1000 + halfBinMs, y: p.rx_bytes })),
          backgroundColor: fade("rgba(92,200,255,0.8)", "rgba(92,200,255,0.35)"),
          barThickness: "flex", barPercentage: 1.0, categoryPercentage: 0.92,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: {
        x: { type: "time", stacked: true,
             min: sinceTs * 1000, max: Date.now(),
             time: timeScaleFor(hours || 1),
             ticks: { color: "#8a96a4" }, grid: { color: "rgba(255,255,255,0.05)" } },
        y: { stacked: true, beginAtZero: true, afterFit: yAxisFit,
             ticks: { color: "#8a96a4", callback: (v) => fmtBytesInUnit(v, yUnit) },
             grid: { color: "rgba(255,255,255,0.05)" } },
      },
      plugins: {
        legend: { labels: { color: "#d7dee6" } },
        tooltip: { callbacks: {
          title: (items) => binRange(items[0].parsed.x),
          label: (ctx) => `${ctx.dataset.label}: ${fmtBytes(ctx.parsed.y)}`
            + (ctx.parsed.x >= nowBinCenterMs ? " (bin still filling)" : ""),
        } },
      },
    },
  };
}

async function refreshMainChart() {
  const hours = parseInt(document.getElementById("range").value, 10);
  // Network-wide volume per bin: exact where NetFlow reported, live-estimated
  // where only DFM readings exist. Per-device rate lines live in the modal.
  const bucketS = USAGE_BUCKET_S[hours] || 3600;
  const data = await fetchJSON(`/api/usage/total?hours=${hours}&bucket_s=${bucketS}`);
  const ctx = document.getElementById("main-chart").getContext("2d");
  const totalBytes = data.points.reduce((s, p) => s + (p.tx_bytes || 0) + (p.rx_bytes || 0), 0);
  document.getElementById("chart-title").textContent =
    `Network usage — ${fmtBytes(totalBytes)} in last ${hours}h (per ${USAGE_BUCKET_LABEL[hours] || "bin"})`;
  if (mainChart) mainChart.destroy();
  mainChart = new Chart(ctx, usageChartConfig(data.points, bucketS, data.since, hours));
}

function updateModalHeader(d) {
  document.getElementById("m-name").textContent = d.hostname || "(unknown device)";
  document.getElementById("m-ip").textContent = `IP ${d.ip || "?"}`;
  document.getElementById("m-mac").textContent = `MAC ${d.mac}`;
  document.getElementById("m-vendor").textContent = d.vendor ? `Vendor ${d.vendor}` : "Vendor unknown";
  document.getElementById("m-tx").textContent = d.rate_pending ? "in progress…" : fmtRate(d.tx_bps);
  document.getElementById("m-rx").textContent = d.rate_pending ? "in progress…" : fmtRate(d.rx_bps);
  document.getElementById("m-first").textContent = fmtTime(d.first_seen);
  document.getElementById("m-last").textContent = fmtTime(d.last_seen);
  document.getElementById("m-note").value = d.notes || "";
}

// Bin width per range for the volume bars — enough bars to see "when"
// without becoming a comb.
const USAGE_BUCKET_S = { 1: 300, 6: 1800, 24: 3600, 168: 21600, 720: 86400 };
const USAGE_BUCKET_LABEL = { 1: "5 min", 6: "30 min", 24: "hour", 168: "6 h", 720: "day" };

async function refreshModalChart() {
  if (!selectedMac) return;
  const hours = parseInt(document.getElementById("m-range").value, 10);
  const data = await fetchJSON(`/api/devices/${encodeURIComponent(selectedMac)}/history?hours=${hours}`);
  const ctx = document.getElementById("m-chart").getContext("2d");
  if (modalChart) modalChart.destroy();
  modalChart = new Chart(ctx, chartConfig(null, data.points, hours));
}

async function refreshModalUsage() {
  if (!selectedMac) return;
  const hours = parseInt(document.getElementById("m-range").value, 10);
  const bucketS = USAGE_BUCKET_S[hours] || 3600;
  const data = await fetchJSON(
    `/api/devices/${encodeURIComponent(selectedMac)}/usage?hours=${hours}&bucket_s=${bucketS}`);
  document.getElementById("m-usage-note").textContent =
    `— bytes moved per ${USAGE_BUCKET_LABEL[hours] || "bin"}; faded bar = bin still filling`;
  const ctx = document.getElementById("m-usage-chart").getContext("2d");
  if (modalUsageChart) modalUsageChart.destroy();
  modalUsageChart = new Chart(ctx, usageChartConfig(data.points, bucketS, data.since, hours));
}

async function openDeviceModal(mac) {
  selectedMac = mac;
  const d = devices.find(x => x.mac === mac);
  if (d) updateModalHeader(d);
  document.getElementById("modal").classList.remove("hidden");
  await Promise.all([refreshModalChart(), refreshModalUsage()]);
  renderDeviceTable();
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  selectedMac = null;
  if (modalChart) { modalChart.destroy(); modalChart = null; }
  if (modalUsageChart) { modalUsageChart.destroy(); modalUsageChart = null; }
  renderDeviceTable();
}

document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") closeModal();
});
document.getElementById("filter").addEventListener("input", renderDeviceTable);
document.getElementById("range").addEventListener("change", refreshMainChart);
document.getElementById("m-range").addEventListener("change", () => {
  refreshModalChart();
  refreshModalUsage();
});
document.getElementById("m-save-note").addEventListener("click", async () => {
  if (!selectedMac) return;
  const note = document.getElementById("m-note").value;
  await fetch(`/api/devices/${encodeURIComponent(selectedMac)}/note`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
  await refreshDevices();
});
// ---- Settings / troubleshooting modal ----
let settingsTimer = null;

const SUMMARY_TEXT = { ok: "All checks passed", warn: "Some warnings", fail: "Problems found" };

async function refreshDiagnostics() {
  const list = document.getElementById("diag-list");
  try {
    const d = await fetchJSON("/api/diagnostics");
    list.innerHTML = d.checks.map(c => `
      <div class="diag-row">
        <span class="diag-dot ${c.status}"></span>
        <div class="diag-body">
          <div class="diag-label">${escapeHtml(c.label)}<span class="diag-badge ${c.status}">${escapeHtml(c.status)}</span></div>
          <div class="diag-detail">${escapeHtml(c.detail)}</div>
          ${c.hint ? `<div class="diag-hint">${escapeHtml(c.hint)}</div>` : ""}
        </div>
      </div>`).join("");
    const sum = document.getElementById("diag-summary");
    sum.textContent = SUMMARY_TEXT[d.summary] || d.summary;
    sum.className = d.summary;
    document.getElementById("diag-updated").textContent = fmtTime(d.generated_ts);
  } catch (e) {
    list.innerHTML = `<div class="diag-row"><span class="diag-dot fail"></span>
      <div class="diag-body"><div class="diag-label">Diagnostics unavailable</div>
      <div class="diag-detail">${escapeHtml(String(e))}</div></div></div>`;
  }
}

function openSettings() {
  document.getElementById("settings-modal").classList.remove("hidden");
  refreshDiagnostics();
  if (settingsTimer) clearInterval(settingsTimer);
  settingsTimer = setInterval(refreshDiagnostics, 2500);
}

function closeSettings() {
  document.getElementById("settings-modal").classList.add("hidden");
  if (settingsTimer) { clearInterval(settingsTimer); settingsTimer = null; }
}

document.getElementById("settings-btn").addEventListener("click", openSettings);
document.getElementById("settings-close").addEventListener("click", closeSettings);
document.getElementById("settings-modal").addEventListener("click", (e) => {
  if (e.target.id === "settings-modal") closeSettings();
});
document.getElementById("diag-refresh").addEventListener("click", refreshDiagnostics);

document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeSettings(); } });

document.querySelectorAll("#device-table th.sortable").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (!SORT_KEYS.has(k)) return;
    if (sortKey === k) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
      sortKey = k;
      sortDir = (k === "tx_bps" || k === "rx_bps" || k === "vol_1h" || k === "vol_24h") ? "desc" : "asc";
    }
    renderDeviceTable();
  });
});

async function tick() {
  await Promise.all([refreshHealth(), refreshDevices()]);
  if (selectedMac) await Promise.all([refreshModalChart(), refreshModalUsage()]);
  await refreshMainChart();
}

tick();
setInterval(tick, 1000);
