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
let selectedMac = null;
let sortKey = "tx_bps";
let sortDir = "desc"; // "asc" | "desc"

const SORT_KEYS = new Set(["hostname", "ip", "mac", "tx_bps", "rx_bps"]);

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
  if (selectedMac) {
    const d = devices.find(x => x.mac === selectedMac);
    if (d) updateModalHeader(d);
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
  const max = Math.max(1, ...devices.map(d => (d.tx_bps || 0) + (d.rx_bps || 0)));
  const filtered = devices.filter(d => {
    if (!filter) return true;
    return (d.hostname || "").toLowerCase().includes(filter)
        || (d.ip || "").includes(filter)
        || (d.mac || "").toLowerCase().includes(filter)
        || (d.vendor || "").toLowerCase().includes(filter);
  });
  const rows = applySort(filtered)
    .map(d => {
      const total = (d.tx_bps || 0) + (d.rx_bps || 0);
      const barW = Math.min(60, Math.round((total / max) * 60));
      const name = escapeHtml(d.hostname || "(unknown)");
      const vendor = d.vendor ? `<div class="vendor">${escapeHtml(d.vendor)}</div>` : "";
      return `<tr data-mac="${escapeHtml(d.mac)}" class="${d.mac === selectedMac ? "active" : ""}">
        <td class="device-cell"><div class="hostname">${name}</div>${vendor}</td>
        <td class="ip">${escapeHtml(d.ip || "")}</td>
        <td class="mac">${escapeHtml(d.mac)}</td>
        <td class="num"><span class="rate">${fmtRate(d.tx_bps)}</span><span class="bar" style="width:${barW}px"></span></td>
        <td class="num"><span class="rate">${fmtRate(d.rx_bps)}</span></td>
      </tr>`;
    })
    .join("");
  tbody.innerHTML = rows || `<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--muted)">No devices yet. Waiting for first poll…</td></tr>`;
  tbody.querySelectorAll("tr[data-mac]").forEach(tr => {
    tr.addEventListener("click", () => openDeviceModal(tr.dataset.mac));
  });
  updateSortIndicators();
}

function chartConfig(label, points) {
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
        x: { type: "time", time: { tooltipFormat: "MMM d, HH:mm:ss" },
             ticks: { color: "#8a96a4" }, grid: { color: "rgba(255,255,255,0.05)" } },
        y: { ticks: { color: "#8a96a4", callback: (v) => fmtRateInUnit(v, yUnit) },
             title: { display: true, text: yUnit.label, color: "#8a96a4" },
             grid: { color: "rgba(255,255,255,0.05)" }, beginAtZero: true },
      },
      plugins: {
        legend: { labels: { color: "#d7dee6" } },
        title: { display: !!label, text: label, color: "#d7dee6" },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmtRate(ctx.parsed.y)}` } },
      },
    },
  };
}

async function refreshMainChart() {
  const hours = parseInt(document.getElementById("range").value, 10);
  const top = devices
    .filter(d => (d.tx_bps || 0) + (d.rx_bps || 0) > 0)
    .slice(0, 1);

  const ctx = document.getElementById("main-chart").getContext("2d");
  if (top.length === 0) {
    document.getElementById("chart-title").textContent = `Top talker — last ${hours}h (waiting for data)`;
    if (mainChart) mainChart.destroy();
    mainChart = null;
    return;
  }
  const dev = top[0];
  const data = await fetchJSON(`/api/devices/${encodeURIComponent(dev.mac)}/history?hours=${hours}`);
  document.getElementById("chart-title").textContent =
    `Top talker — ${dev.hostname || dev.mac} (last ${hours}h)`;
  if (mainChart) mainChart.destroy();
  mainChart = new Chart(ctx, chartConfig(null, data.points));
}

function updateModalHeader(d) {
  document.getElementById("m-name").textContent = d.hostname || "(unknown device)";
  document.getElementById("m-ip").textContent = `IP ${d.ip || "?"}`;
  document.getElementById("m-mac").textContent = `MAC ${d.mac}`;
  document.getElementById("m-vendor").textContent = d.vendor ? `Vendor ${d.vendor}` : "Vendor unknown";
  document.getElementById("m-tx").textContent = fmtRate(d.tx_bps);
  document.getElementById("m-rx").textContent = fmtRate(d.rx_bps);
  document.getElementById("m-first").textContent = fmtTime(d.first_seen);
  document.getElementById("m-last").textContent = fmtTime(d.last_seen);
  document.getElementById("m-note").value = d.notes || "";
}

async function refreshModalChart() {
  if (!selectedMac) return;
  const hours = parseInt(document.getElementById("m-range").value, 10);
  const data = await fetchJSON(`/api/devices/${encodeURIComponent(selectedMac)}/history?hours=${hours}`);
  const ctx = document.getElementById("m-chart").getContext("2d");
  if (modalChart) modalChart.destroy();
  modalChart = new Chart(ctx, chartConfig(null, data.points));
}

async function openDeviceModal(mac) {
  selectedMac = mac;
  const d = devices.find(x => x.mac === mac);
  if (d) updateModalHeader(d);
  document.getElementById("modal").classList.remove("hidden");
  await refreshModalChart();
  renderDeviceTable();
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  selectedMac = null;
  if (modalChart) { modalChart.destroy(); modalChart = null; }
  renderDeviceTable();
}

document.getElementById("modal-close").addEventListener("click", closeModal);
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") closeModal();
});
document.getElementById("filter").addEventListener("input", renderDeviceTable);
document.getElementById("range").addEventListener("change", refreshMainChart);
document.getElementById("m-range").addEventListener("change", refreshModalChart);
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
      sortDir = (k === "tx_bps" || k === "rx_bps") ? "desc" : "asc";
    }
    renderDeviceTable();
  });
});

async function tick() {
  await Promise.all([refreshHealth(), refreshDevices()]);
  if (selectedMac) await refreshModalChart();
  await refreshMainChart();
}

tick();
setInterval(tick, 1000);
