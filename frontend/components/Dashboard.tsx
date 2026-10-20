"use client";
// The Activity panel: what is downloading, the queue (reorder by dragging), recent results from the
// event log, and History & stats.
import { memo, useEffect, useRef, useState } from "react";
import { api, itemUrl } from "../lib/api";
import { fileName, fmtAgo, fmtBytes, fmtLong, fmtSpeed, fmtTime, mergeEvents, plural, store } from "../lib/util";
import { useApp } from "./AppContext";
import { LineChart } from "./charts";
import { Detail } from "./Detail";
import { BOTTOM, CHEV, FOLDER, PAUSE, PLAY, TOP, X } from "./icons";
import { ITEM_LABEL, REVEAL_LABEL } from "./labels";
import { StatsView } from "./StatsView";

const EMPTY: Record<string, string> = {
  active: "Nothing is downloading right now.",
  queue: "The queue is empty. Open a playlist below, select videos and press Download.",
  recent: "No downloads have finished yet.",
};
export const DASH_TABS = ["active", "queue", "recent", "stats"];
const TAB_LABEL: Record<string, string> = { active: "Downloading now", queue: "Up next", recent: "Recently finished", stats: "History & stats" };

const keyOf = (tab: string, r: any) => (tab === "recent" ? `e${r.event}` : `${r.job}:${r.id}`);
const busyJob = (j: any) => j.counts.queued + j.counts.downloading + j.counts.processing > 0;

function summary(st: any) {
  if (st.active) return `${st.active} downloading · ${fmtSpeed(st.speed)}`;
  if (st.queued) return `${st.queued} in queue`;
  return st.done_today ? `${st.done_today} downloaded in the last 24 hours` : "Nothing downloading";
}

export function Dashboard({ tab, setTab }: { tab: string; setTab: (t: string) => void }) {
  const { activity: d, overview, setFilter, refresh, toast } = useApp();
  const [dashOpen, setDashOpen] = useState<boolean | null>(() => store.get<boolean | null>("dashOpen", null));
  const [job, setJob] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [extra, setExtra] = useState<any[]>([]);
  const [extraTotal, setExtraTotal] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [drag, setDrag] = useState<{ key: string; over: string | null; after: boolean } | null>(null);

  // the search waits for a pause in typing
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    const t = setTimeout(() => setFilter({ job, q: q.trim() }), job ? 0 : 300);
    return () => clearTimeout(t);
  }, [job, q]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (!d) return <section className="card dash collapsed" id="dash" />;
  const st = d.stats;
  const collapsed = dashOpen === null ? !st.active && !st.queued : !dashOpen;
  const jobs = overview?.jobs || [];
  const busy = jobs.filter(busyJob);
  const anyPaused = busy.some((j) => j.paused);

  const queueAction = async (action: string, msg: string) => {
    try { await api(`/api/queue/${action}`, { method: "POST" }); toast(msg); refresh(); } catch (err: any) { toast(err.message); }
  };

  // the rows of the current tab; the event log isn't filtered by the server, so Recently finished is filtered here
  const ql = q.trim().toLowerCase();
  let rows: any[] = tab === "stats" ? [] : d[tab] || [];
  // every event loaded so far, before the filter: the next page starts after these
  const loaded = tab === "recent" ? mergeEvents(d.recent, extra) : [];
  if (tab === "recent") {
    rows = loaded.filter((r: any) => (!job || r.job === job)
      && (!ql || r.title.toLowerCase().includes(ql) || (r.job_title || "").toLowerCase().includes(ql)));
  }
  const counts: Record<string, number> = { active: d.active.length, queue: d.queued, recent: d.recent.length };
  const more = tab === "queue" ? d.queued - d.queue.length : 0;
  const canPage = tab === "recent" && (extraTotal === null ? d.recent.length >= 30 : loaded.length < extraTotal);
  const hist: number[] = st.speed_history || [];

  const toggleRow = (key: string) => setOpen((s) => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n; });

  async function loadMore() {
    setLoadingMore(true);
    try {
      const res = await api(`/api/history?offset=${loaded.length}&limit=30`);
      setExtra((x) => x.concat(res.rows));
      setExtraTotal(res.total);
    } catch (err: any) { toast(err.message); }
    finally { setLoadingMore(false); }
  }

  async function drop() {
    if (!drag) return;
    const keys = rows.map((r) => keyOf(tab, r));
    const moving = rows[keys.indexOf(drag.key)];
    const at = drag.over == null ? -1 : keys.indexOf(drag.over);
    setDrag(null);
    if (!moving || (at >= 0 && rows[at] === moving)) return;
    // "before" the next row when dropped on a row's lower half; at the end when below the last row
    const target = at < 0 ? null : drag.after ? rows[at + 1] || null : rows[at];
    try {
      await api("/api/queue/move", { method: "POST", body: { job: moving.job, id: moving.id, before: target ? { job: target.job, id: target.id } : null } });
      refresh();
    } catch (err: any) { toast(err.message); }
  }

  const disks: any[] = st.disks || [];
  const worst = disks.length ? disks.slice().sort((a, b) => (a.free ?? -1) - a.need - ((b.free ?? -1) - b.need))[0] : null;

  return (
    <section className={`card dash ${collapsed ? "collapsed" : ""}`} id="dash">
      <div className="dash-head">
        <h2>Activity</h2>
        <span className={`muted ${st.waiting ? "warn" : ""}`} id="dash-note">{st.waiting ? `⏸ ${st.waiting}` : collapsed ? summary(st) : ""}</span>
        <div className="dash-head-btns">
          <button className={`small ${!busy.length || busy.every((j) => j.paused) ? "hidden" : ""}`} type="button" id="dash-pause-all"
            title="Pause every playlist (p)" onClick={() => queueAction("pause-all", "Paused every playlist")}>Pause all</button>
          <button className={`small primary ${anyPaused ? "" : "hidden"}`} type="button" id="dash-resume-all"
            title="Resume every playlist (p)" onClick={() => queueAction("resume-all", "Resumed")}>Resume all</button>
          <button className="small ghost chev" type="button" id="dash-toggle" aria-expanded={!collapsed} aria-controls="dash-body"
            title={collapsed ? "Show" : "Hide"} onClick={() => { const v = collapsed; store.set("dashOpen", v); setDashOpen(v); }}>
            <svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6" /></svg>
          </button>
        </div>
      </div>
      {!collapsed && <div className="dash-body" id="dash-body">
        <div className="dash-stats">
          <div><span>Downloading</span><b id="dash-active">{st.active}</b><small id="dash-active-sub">{st.limit >= st.concurrency ? `of ${st.limit} at once`
            : st.probe ? `of ${st.limit} (one at a time until YouTube allows downloads again)` : `of ${st.limit} (reduced: weak connection)`}</small></div>
          <div><span>In queue</span><b id="dash-queued">{st.queued}</b><small id="dash-queued-sub">{st.queued ? `${st.ready} ready · ${st.queued - st.ready} waiting` : "nothing waiting"}</small></div>
          <div><span>Total speed</span><b id="dash-speed">{st.active ? fmtSpeed(st.speed) : "—"}</b><small id="dash-speed-sub">{st.avg_speed ? `avg ${fmtSpeed(st.avg_speed)} (2 min)` : ""}</small></div>
          <div><span>Time left</span><b id="dash-eta">{st.eta ? fmtLong(st.eta) : st.left_bytes ? "—" : "Nothing left"}</b><small id="dash-eta-sub">{st.left_bytes ? `${st.left_estimated ? "≈ " : ""}${fmtBytes(st.left_bytes)} left` : ""}</small></div>
          <div><span>Last 24 hours</span><b id="dash-done">{`${st.done_today} done`}</b><small id="dash-done-sub" className={st.failed_today ? "s-failed" : ""}>{st.failed_today ? `${st.failed_today} failed` : "no failures"}</small></div>
          <div id="dash-disk-tile" className={worst && (worst.low || worst.problem) ? "warn" : ""}
            title={disks.map((x) => `${x.path}: ${x.problem || `${fmtBytes(x.free)} free, queue needs ≈ ${fmtBytes(x.need)}`}`).join("\n")}>
            <span>Disk</span>
            <b id="dash-disk">{!worst ? "—" : worst.problem ? "Not connected" : fmtBytes(worst.free)}</b>
            <small id="dash-disk-sub">{!worst ? "no downloads waiting" : worst.problem ? worst.problem
              : worst.low ? `free · too little for ≈ ${fmtBytes(worst.need)}` : `free · needs ≈ ${fmtBytes(worst.need)}`}</small>
          </div>
        </div>
        {/* the speed chart only earns its space once something has downloaded in the last two minutes */}
        <div className={`chart dash-chart ${!st.active && !hist.some((v) => v > 0) ? "hidden" : ""}`}>
          <LineChart id="dash-speed-chart" width={600} height={48} ariaLabel="Total download speed, last two minutes" values={hist}
            opts={{
              slots: 60, min: 512 * 1024, fmt: fmtSpeed,
              label: (i, len) => { const ago = (len - 1 - i) * 2; return ago ? `${ago < 60 ? `${ago}s` : `${Math.floor(ago / 60)} min ${ago % 60}s`} ago` : "now"; },
            }} />
          <div className="net-foot"><span>Total download speed, last 2 minutes</span></div>
        </div>
        <div className="tabs dash-tabs" id="dash-tabs" role="tablist">
          {DASH_TABS.map((t) => (
            <button key={t} data-dash={t} className={t === tab ? "on" : ""} onClick={() => setTab(t)}>
              {TAB_LABEL[t]} {t !== "stats" && <span className="cnt">{counts[t] || ""}</span>}
            </button>
          ))}
        </div>
        <div className={`dash-filters ${tab === "stats" ? "hidden" : ""}`} id="dash-filters">
          <select id="dash-job" aria-label="Show one playlist" value={job} onChange={(e) => setJob(e.target.value)}>
            <option value="">All playlists</option>
            {d.jobs.map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}
          </select>
          <input id="dash-q" type="search" placeholder="Search titles" aria-label="Search titles" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className={`dash-tools ${tab === "recent" ? "" : "hidden"}`} id="dash-recent-tools">
            <button className="small ghost" type="button" id="dash-retry-all" onClick={async () => {
              const res = await api("/api/queue/retry-failed", { method: "POST" }).catch((err) => { toast(err.message); return null; });
              if (res) { toast(res.queued ? `Retrying ${plural(res.queued, "video")}` : "Nothing to retry"); refresh(); }
            }}>Retry all failed</button>
            <button className="small ghost" type="button" id="dash-clear" onClick={async () => {
              if (!confirm("Clear the list of recently finished downloads?\nFiles and History & stats are not affected.")) return;
              await api("/api/history/clear", { method: "POST" }).catch((err) => toast(err.message));
              setExtra([]); setExtraTotal(0);
              refresh();
            }}>Clear</button>
          </div>
        </div>
        <div className={`dash-list ${tab === "stats" ? "hidden" : ""}`} id="dash-list"
          onDragOver={(e) => {
            if (!drag) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            const row = (e.target as HTMLElement).closest<HTMLElement>(".dash-row");
            if (!row) { if (drag.over !== null) setDrag({ ...drag, over: null, after: true }); return; }
            const r = row.getBoundingClientRect();
            const after = e.clientY > r.top + r.height / 2;
            if (drag.over !== row.dataset.key || drag.after !== after) setDrag({ ...drag, over: row.dataset.key!, after });
          }}
          onDragLeave={(e) => { if (drag && !e.currentTarget.contains(e.relatedTarget as Node)) setDrag({ ...drag, over: null, after: true }); }}
          onDrop={(e) => { e.preventDefault(); drop(); }}>
          {tab !== "stats" && (rows.length === 0
            ? <p className="dash-empty">{EMPTY[tab]}{job || q ? " (with the current filter)" : ""}</p>
            : <>
              {rows.map((r, i) => {
                const key = keyOf(tab, r);
                const dropCls = drag && drag.over === key && drag.key !== key ? (drag.after ? "drop-after" : "drop-before") : "";
                return <DashRow key={key} rowKey={key} r={r} pos={i} tab={tab} expanded={open.has(key)} onToggle={toggleRow}
                  dragging={drag?.key === key} dropCls={dropCls}
                  onDragStart={() => setDrag({ key, over: null, after: true })} onDragEnd={() => setDrag(null)} />;
              })}
              {(more > 0 || canPage) && <div className="dash-more">
                {more > 0 ? `…and ${more} more`
                  : <button className="small ghost" disabled={loadingMore} onClick={loadMore}>Show more</button>}
              </div>}
            </>)}
        </div>
        <StatsView hidden={tab !== "stats"} />
      </div>}
    </section>
  );
}

interface RowProps {
  rowKey: string; r: any; pos: number; tab: string; expanded: boolean; dragging: boolean; dropCls: string;
  onToggle: (key: string) => void; onDragStart: () => void; onDragEnd: () => void;
}

const DashRow = memo(function DashRow({ rowKey, r, pos, tab, expanded, dragging, dropCls, onToggle, onDragStart, onDragEnd }: RowProps) {
  const { itemAction, openJob, toast } = useApp();
  const [busy, setBusy] = useState("");

  const act = async (dact: string) => {
    if (dact === "cancel" && tab === "active" && !confirm("Stop this download? It goes back to “Not downloaded” (the partial download is kept).")) return;
    setBusy(dact);
    try { await itemAction(r.job, r.id, dact); } catch (err: any) { toast(err.message); }
    finally { setBusy(""); }
  };
  const icon = (dact: string, label: string, svg: React.ReactNode) => (
    <button className="small ghost icon" data-dact={dact} title={label} aria-label={label} disabled={busy === dact}
      onClick={(e) => { e.stopPropagation(); act(dact); }}>{svg}</button>
  );

  let lead: React.ReactNode = null, meta: React.ReactNode = "", extraEl: React.ReactNode = null, right: React.ReactNode = null;
  if (tab === "active") {
    lead = <span className={`badge s-${r.status}`}>{r.status === "processing" ? r.step || ITEM_LABEL.processing : ITEM_LABEL[r.status]}</span>;
    let m: string;
    if (r.status === "processing") m = `${r.step || "Processing"}…`;
    else {
      const bits = [`${(r.progress || 0).toFixed(1)}%`, `${fmtBytes(r.downloaded)} / ${fmtBytes(r.total)}`, fmtSpeed(r.speed)];
      if (r.eta != null) bits.push(`${fmtTime(r.eta)} left`);
      m = bits.join(" · ");
    }
    if (r.started) m += ` · started ${fmtAgo(r.started)}`;
    if (r.attempts) m += ` · attempt ${r.attempts + 1}`;
    meta = m;
    extraEl = <div className="bar small"><div style={{ width: `${r.status === "processing" ? 100 : r.progress || 0}%` }} /></div>;
    right = <>{icon("hold", "Pause this video", PAUSE)}{icon("cancel", "Stop this video", X)}</>;
  } else if (tab === "queue") {
    lead = <><span className="grip" aria-hidden="true">⋮⋮</span><span className="pos">#{pos + 1}</span></>;
    meta = <>
      <span className={r.retrying ? "s-pausing" : ""}>{r.wait}</span>
      {r.progress > 0 && r.progress < 100 ? ` · ${r.progress.toFixed(0)}% already downloaded` : ""}
      {r.duration ? ` · ${fmtTime(r.duration)}` : ""}
    </>;
    const waitingRetry = !!r.next_try && r.next_try > Date.now() / 1000;   // a bare 0 would be drawn as "0"
    right = <>
      {waitingRetry && <button className="small" data-dact="now" disabled={busy === "now"} onClick={(e) => { e.stopPropagation(); act("now"); }}>Try now</button>}
      {icon("top", "Move to top", TOP)}{icon("bottom", "Move to bottom", BOTTOM)}
      {r.held ? icon("unhold", "Continue", PLAY) : icon("hold", "Pause this video", PAUSE)}{icon("cancel", "Remove from queue", X)}
    </>;
  } else {
    lead = <span className={`badge s-${r.status}`}>{ITEM_LABEL[r.status]}</span>;
    let m = fmtAgo(r.finished);
    if (r.status === "done" && r.filepath) m += ` · ${fileName(r.filepath)}`;
    if (r.bytes) m += ` · ${fmtBytes(r.bytes)}`;
    if (r.status === "done" && r.seconds > 0 && r.bytes) m += ` · ${fmtSpeed(r.bytes / r.seconds)}`;
    if (r.current && r.current !== r.status && !(r.status === "done" && r.current === "skipped"))
      m += ` · now ${(ITEM_LABEL[r.current] || r.current).toLowerCase()}`;
    meta = m;
    const hasFile = r.status === "done" && r.current_file && (r.current === "done" || r.current === "skipped");
    right = <>
      {hasFile && <>{icon("reveal", REVEAL_LABEL, FOLDER)}{icon("play", "Play", PLAY)}</>}
      {r.current === "failed" && <button className="small" data-dact="retry" disabled={busy === "retry"} onClick={(e) => { e.stopPropagation(); act("retry"); }}>Retry</button>}
    </>;
  }
  const err = r.error && (tab !== "recent" || r.status === "failed") ? <div className="err" title={r.error}>{r.error}</div> : null;

  return (
    <div className={`dash-row ${expanded ? "expanded" : ""} ${dragging ? "dragging" : ""} ${dropCls}`} data-key={rowKey}
      draggable={tab === "queue"}
      onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", rowKey); onDragStart(); }}
      onDragEnd={onDragEnd}
      onClick={(e) => {
        const t = e.target as HTMLElement;
        if (t.closest("button") || t.closest("a") || t.closest(".dash-detail") || String(window.getSelection())) return;
        onToggle(rowKey);
      }}>
      <div className="dash-main">
        <div className="lead">{lead}</div>
        <div className="mid">
          <div className="t">{r.url ? <a href={r.url} target="_blank" rel="noopener" title={r.title}>{r.title}</a> : r.title}</div>
          <div className="meta">
            <button className="linkish" title="Show in its playlist" onClick={(e) => { e.stopPropagation(); openJob(r.job, r.id); }}>{r.job_title}</button>
            {" · "}{meta}
          </div>
          {extraEl}{err}
        </div>
        <div className="right">{right}
          <button className="small ghost icon chev-row" title="Details" aria-label="Details" onClick={(e) => { e.stopPropagation(); onToggle(rowKey); }}>{CHEV}</button>
        </div>
      </div>
      {expanded && <RowDetail job={r.job} id={r.id} />}
    </div>
  );
});

/** An expanded row's details, refreshed every 3 s while open. */
function RowDetail({ job, id }: { job: string; id: string }) {
  const [d, setD] = useState<any>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    const load = () => api(itemUrl(job, id, "log")).then((x) => { if (live) { setD(x); setError(""); } })
      .catch((err) => { if (live) setError(err.message); });
    load();
    const t = setInterval(() => { if (!document.hidden) load(); }, 3000);
    return () => { live = false; clearInterval(t); };
  }, [job, id]);
  return (
    <div className="dash-detail">
      {error ? <p className="err">{error}</p> : d ? <Detail d={d} /> : <p className="muted">Loading…</p>}
    </div>
  );
}
