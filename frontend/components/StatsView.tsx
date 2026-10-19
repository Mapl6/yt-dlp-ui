"use client";
// History & stats (dashboard tab): totals and data downloaded per day or week, from the event log.
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { buckets } from "../lib/stats";
import { fmtBytes, fmtSpeed, store } from "../lib/util";
import { ColumnChart } from "./charts";
import { KIND } from "./Detail";

export function StatsView({ hidden }: { hidden: boolean }) {
  const [period, setPeriod] = useState(() => store.get("statsDays", 30));
  const [group, setGroup] = useState<"day" | "week">(() => store.get("statsGroup", "day"));
  const [table, setTable] = useState(false);
  const [s, setS] = useState<any>(null);
  const [error, setError] = useState("");

  // loaded when shown or the period changes, then every 30 s while shown
  useEffect(() => {
    if (hidden) return;
    let live = true;
    const load = () => api(`/api/stats?days=${period}`).then((d) => { if (live) { setS(d); setError(""); } })
      .catch((err) => { if (live) setError(err.message); });
    load();
    const t = setInterval(() => { if (!document.hidden) load(); }, 30000);
    return () => { live = false; clearInterval(t); };
  }, [hidden, period]);

  const tile = (label: string, value: string, sub = "") => <div key={label}><span>{label}</span><b>{value}</b><small>{sub}</small></div>;
  const rows = s ? buckets(s.period, s, group) : [];
  const total = s ? s.kinds.reduce((n: number, k: any) => n + k.count, 0) : 0;

  return (
    <div className={`dash-statsview ${hidden ? "hidden" : ""}`} id="dash-statsview">
      <div className="stats-controls">
        <select id="stats-days" aria-label="Period" value={String(period)}
          onChange={(e) => { const p = Number(e.target.value); setPeriod(p); store.set("statsDays", p); }}>
          <option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="365">Last year</option>
        </select>
        <div className="seg" role="group" aria-label="Group by">
          {(["day", "week"] as const).map((g) => (
            <button key={g} type="button" className={group === g ? "on" : ""} onClick={() => { setGroup(g); store.set("statsGroup", g); }}>
              {g === "day" ? "Per day" : "Per week"}
            </button>
          ))}
        </div>
        <button type="button" className="small ghost" id="stats-table" onClick={() => setTable(!table)}>{table ? "Show chart" : "Show table"}</button>
      </div>
      <div className="stats-tiles" id="stats-tiles">
        {error ? <p className="err">{error}</p> : s && <>
          {tile("Downloaded", fmtBytes(s.bytes) === "—" ? "0 B" : fmtBytes(s.bytes), `${s.done} video${s.done === 1 ? "" : "s"}`)}
          {tile("Success rate", s.success == null ? "—" : `${s.success}%`, s.failed ? `${s.failed} failed` : "no failures")}
          {tile("Average speed", s.avg_speed ? fmtSpeed(s.avg_speed) : "—", "while downloading")}
          {tile("Per day", s.bytes ? fmtBytes(s.bytes / s.period) : "—", `average over ${s.period} days`)}
        </>}
      </div>
      <div className="stats-chart">
        <div className="chart-title">Data downloaded</div>
        <div id="stats-cols" className={table ? "hidden" : ""}>
          {s && !table && <ColumnChart height={140} fmt={(v) => (v ? fmtBytes(v) : "Nothing")}
            rows={rows.map((r) => ({ label: r.label, value: r.bytes, sub: `${r.done} downloaded${r.failed ? `, ${r.failed} failed` : ""}` }))} />}
        </div>
        <div id="stats-tablebox" className={table ? "" : "hidden"}>
          {s && table && (
            <table className="data">
              <thead><tr><th>{group === "week" ? "Week" : "Day"}</th><th>Data</th><th>Downloaded</th><th>Failed</th></tr></thead>
              <tbody>{rows.slice().reverse().map((r) => (
                <tr key={r.key}><td>{r.label}</td><td>{r.bytes ? fmtBytes(r.bytes) : "—"}</td><td>{r.done}</td><td>{r.failed}</td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </div>
      <div className="stats-kinds"><div className="chart-title">Why downloads failed</div>
        <div id="stats-kinds">
          {s && (total ? s.kinds.map((k: any) => (
            <div className="kind-row" key={k.kind}><span className="kind-name">{KIND[k.kind] || k.kind}</span>
              <span className="kind-bar"><i style={{ width: `${(100 * k.count) / s.kinds[0].count}%` }} /></span>
              <b>{k.count}</b></div>
          )) : <p className="muted">No failed downloads in this period.</p>)}
        </div>
      </div>
    </div>
  );
}
