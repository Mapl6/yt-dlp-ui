// History & stats (dashboard tab): totals and data downloaded per day or week, from the event log.

import { api } from "./api.js";
import { columnChart } from "./charts.js";
import { KIND } from "./dialogs.js";
import { $, esc, fmtBytes, fmtSpeed, store } from "./util.js";

let period = store.get("statsDays", 30), group = store.get("statsGroup", "day"), table = false;
let last = null, loadedAt = 0;

const dayKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const shortDate = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });

/** Every day (or week, starting Monday) of the period, oldest first, with zeros where nothing happened. */
export function buckets(days, stats, by = "day", today = new Date()) {
  const byDay = new Map(stats.days.map((d) => [d.day, d]));
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const row = byDay.get(dayKey(d)) || { done: 0, failed: 0, bytes: 0 };
    if (by === "week") {
      const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
      const key = dayKey(monday);
      let w = out[out.length - 1];
      if (!w || w.key !== key) out.push((w = { key, label: `Week of ${shortDate(monday)}`, done: 0, failed: 0, bytes: 0 }));
      w.done += row.done; w.failed += row.failed; w.bytes += row.bytes;
    } else {
      out.push({ key: dayKey(d), label: shortDate(d), done: row.done, failed: row.failed, bytes: row.bytes });
    }
  }
  return out;
}

export async function renderStats(force = false) {
  const box = $("#dash-statsview");
  if (!box.dataset.ready) {
    box.dataset.ready = "1";
    box.innerHTML = `
      <div class="stats-controls">
        <select id="stats-days" aria-label="Period">
          <option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="365">Last year</option>
        </select>
        <div class="seg" role="group" aria-label="Group by">
          <button type="button" data-group="day">Per day</button><button type="button" data-group="week">Per week</button>
        </div>
        <button type="button" class="small ghost" id="stats-table">Show table</button>
      </div>
      <div class="stats-tiles" id="stats-tiles"></div>
      <div class="stats-chart">
        <div class="chart-title">Data downloaded</div>
        <div id="stats-cols"></div>
        <div id="stats-tablebox" class="hidden"></div>
      </div>
      <div class="stats-kinds"><div class="chart-title">Why downloads failed</div><div id="stats-kinds"></div></div>`;
    $("#stats-days").value = String(period);
    $("#stats-days").addEventListener("change", (e) => { period = Number(e.target.value); store.set("statsDays", period); renderStats(true); });
    box.addEventListener("click", (e) => {
      const g = e.target.closest("[data-group]");
      if (g) { group = g.dataset.group; store.set("statsGroup", group); draw(); }
      if (e.target.closest("#stats-table")) { table = !table; draw(); }
    });
    setInterval(() => { if (!box.classList.contains("hidden") && !document.hidden) renderStats(); }, 30000);
  }
  if (force || !last || Date.now() - loadedAt > 25000) {
    try {
      last = await api(`/api/stats?days=${period}`);
      loadedAt = Date.now();
    } catch (err) {
      $("#stats-tiles").innerHTML = `<p class="err">${esc(err.message)}</p>`;
      return;
    }
  }
  draw();
}

function draw() {
  const s = last;
  if (!s) return;
  for (const b of document.querySelectorAll("[data-group]")) b.classList.toggle("on", b.dataset.group === group);
  $("#stats-table").textContent = table ? "Show chart" : "Show table";
  const tile = (label, value, sub = "") => `<div><span>${label}</span><b>${value}</b><small>${sub}</small></div>`;
  $("#stats-tiles").innerHTML =
    tile("Downloaded", fmtBytes(s.bytes) === "—" ? "0 B" : fmtBytes(s.bytes), `${s.done} video${s.done === 1 ? "" : "s"}`) +
    tile("Success rate", s.success == null ? "—" : `${s.success}%`, s.failed ? `${s.failed} failed` : "no failures") +
    tile("Average speed", s.avg_speed ? fmtSpeed(s.avg_speed) : "—", "while downloading") +
    tile("Per day", s.bytes ? fmtBytes(s.bytes / s.period) : "—", `average over ${s.period} days`);
  const rows = buckets(s.period, s, group);
  const chartRows = rows.map((r) => ({ label: r.label, value: r.bytes, sub: `${r.done} downloaded${r.failed ? `, ${r.failed} failed` : ""}` }));
  $("#stats-cols").classList.toggle("hidden", table);
  $("#stats-tablebox").classList.toggle("hidden", !table);
  if (table) {
    $("#stats-tablebox").innerHTML = `<table class="data"><thead><tr><th>${group === "week" ? "Week" : "Day"}</th><th>Data</th><th>Downloaded</th><th>Failed</th></tr></thead><tbody>` +
      rows.slice().reverse().map((r) => `<tr><td>${esc(r.label)}</td><td>${r.bytes ? fmtBytes(r.bytes) : "—"}</td><td>${r.done}</td><td>${r.failed}</td></tr>`).join("") + `</tbody></table>`;
  } else {
    columnChart($("#stats-cols"), chartRows, { fmt: (v) => (v ? fmtBytes(v) : "Nothing"), height: 140 });
  }
  const total = s.kinds.reduce((n, k) => n + k.count, 0);
  $("#stats-kinds").innerHTML = total ? s.kinds.map((k) => `
    <div class="kind-row"><span class="kind-name">${esc(KIND[k.kind] || k.kind)}</span>
      <span class="kind-bar"><i style="width:${(100 * k.count) / s.kinds[0].count}%"></i></span>
      <b>${k.count}</b></div>`).join("") : `<p class="muted">No failed downloads in this period.</p>`;
}
