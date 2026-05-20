const fmtRate = (bps) => {
  if (!bps || bps < 1) return "0 bps";
  const units = ["bps", "Kbps", "Mbps", "Gbps"];
  let i = 0;
  while (bps >= 1000 && i < units.length - 1) { bps /= 1000; i++; }
  return `${bps.toFixed(bps >= 100 ? 0 : bps >= 10 ? 1 : 2)} ${units[i]}`;
};

const fmtTime = (ts) => {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
};

let devices = [];
let mainChart = null;
let modalChart = null;
let selectedMac = null;
let selectedTopN = null;   // when no specific device selected, show this many

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

async function refreshHealth() {
  try {
    const h = await fetchJSON("/api/health");
    const el = document.getElementById("health");
    const age = h.last_poll_age_s;
    if (h.last_poll_ok && age !== null && age < h.poll_interval_s * 3) {
      el.textContent = `router ${h.router} • last poll ${age}s ago`;
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

function renderDeviceTable() {
  const filter = document.getElementById("filter").value.toLowerCase();
  const tbody = document.querySelector("#device-table tbody");
  const max = Math.max(1, ...devices.map(d => (d.tx_bps || 0) + (d.rx_bps || 0)));
  const rows = devices
    .filter(d => {
      if (!filter) return true;
      return (d.hostname || "").toLowerCase().includes(filter)
          || (d.ip || "").includes(filter)
          || (d.mac || "").toLowerCase().includes(filter)
          || (d.vendor || "").toLowerCase().includes(filter);
    })
    .map(d => {
      const total = (d.tx_bps || 0) + (d.rx_bps || 0);
      const barW = Math.min(80, Math.round((total / max) * 80));
      const name = d.hostname || "(unknown)";
      const vendor = d.vendor ? `<small>${d.vendor}</small>` : "";
      return `<tr data-mac="${d.mac}" class="${d.mac === selectedMac ? "active" : ""}">
        <td><span class="hostname">${name}</span>${vendor}</td>
        <td class="ip">${d.ip || ""}</td>
        <td class="mac">${d.mac}</td>
        <td class="num">${fmtRate(d.tx_bps)}<span class="bar" style="width:${barW}px"></span></td>
        <td class="num">${fmtRate(d.rx_bps)}</td>
      </tr>`;
    })
    .join("");
  tbody.innerHTML = rows || `<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--muted)">No devices yet. Waiting for first poll…</td></tr>`;
  tbody.querySelectorAll("tr[data-mac]").forEach(tr => {
    tr.addEventListener("click", () => openDeviceModal(tr.dataset.mac));
  });
}

function chartConfig(label, points) {
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
        y: { ticks: { color: "#8a96a4", callback: (v) => fmtRate(v) },
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

async function tick() {
  await Promise.all([refreshHealth(), refreshDevices()]);
  if (selectedMac) await refreshModalChart();
  await refreshMainChart();
}

tick();
setInterval(tick, 5000);
