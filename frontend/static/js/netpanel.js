// The connection panel: is the internet / YouTube reachable, how fast it responds, and the history chart.

import { api } from "./api.js";
import { lineChart } from "./charts.js";
import { $, $$, fmtSpeed } from "./util.js";

const LEVELS = ["checking", "excellent", "good", "fair", "weak", "blocked", "offline"];
let lastCheck = null;

const fmtMs = (p) => (p?.ok ? `${p.latency} ms` : p ? (p.error || "No response") : "—");

export function renderNet(n) {
  if (!n) return;
  const card = $("#net-card"), pill = $("#net-pill");
  for (const el of [card, pill]) {
    LEVELS.forEach((l) => el.classList.remove(`lvl-${l}`));
    el.classList.add(`lvl-${n.level}`);
  }
  $("#net-label").textContent = n.label;
  $("#net-pill-text").textContent = n.label;
  $("#net-advice").textContent = n.advice || "";
  $$("#net-bars i").forEach((b, i) => b.classList.toggle("on", i < (n.bars || 0)));
  const yt = $("#net-yt"), inet = $("#net-inet");
  yt.textContent = fmtMs(n.youtube);
  yt.classList.toggle("bad", !!n.youtube && !n.youtube.ok);
  inet.textContent = fmtMs(n.internet);
  inet.classList.toggle("bad", !!n.internet && !n.internet.ok);
  const y = n.youtube;
  $("#net-stab").textContent = !y?.ok ? "—" : y.loss ? `${y.loss}% lost` : y.jitter < 150 ? "Stable" : `Unsteady (±${y.jitter} ms)`;
  $("#net-speed").textContent = n.speed ? fmtSpeed(n.speed) : n.peak_speed ? `Idle (peak ${fmtSpeed(n.peak_speed)})` : "Idle";
  if (n.checked !== lastCheck || !n.checked) {
    lastCheck = n.checked;
    const hist = n.history || [];
    lineChart($("#net-spark"), hist, {
      slots: 40, min: 500, gaps: true, fmt: (v) => `${Math.round(v)} ms`,
      label: (i, len) => { const ago = (len - 1 - i) * 10; return ago ? `${ago < 60 ? `${ago}s` : `${Math.round(ago / 60)} min`} ago` : "latest check"; },
    });
  }
}

function tickClock() {
  if (!lastCheck) return;
  const s = Math.max(0, Math.round(Date.now() / 1000 - lastCheck));
  $("#net-checked").textContent = `Checked ${s < 5 ? "just now" : s < 60 ? `${s}s ago` : `${Math.round(s / 60)} min ago`}`;
}

export function initNet() {
  setInterval(tickClock, 1000);
  $("#btn-net-check").addEventListener("click", async () => {
    const btn = $("#btn-net-check");
    btn.disabled = true;
    btn.textContent = "Testing…";
    const before = lastCheck;
    try {
      await api("/api/net/check", { method: "POST" });
      for (let i = 0; i < 60 && lastCheck === before; i++) await new Promise((r) => setTimeout(r, 500));
    } finally {
      btn.disabled = false;
      btn.textContent = "Test now";
    }
  });
  $("#net-pill").addEventListener("click", () => $("#net-card").scrollIntoView({ behavior: "smooth", block: "center" }));
}
