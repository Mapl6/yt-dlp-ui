"use client";
// "Your playlists": one card per saved playlist. The card body (options, selection, video list) is built
// when the card is first opened. Rows are drawn by a virtual list and updated from items?since=<rev>.
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Job } from "../lib/types";
import { fmtSpeed, plural, store } from "../lib/util";
import { useApp } from "./AppContext";
import { JobBody, type JobBodyApi } from "./JobBody";
import { JOB_LABEL, SYNC_LABEL } from "./labels";

export interface LibraryApi { openJob: (id: string, itemId?: string | null) => void; markExpanded: (id: string) => void }

export const Library = forwardRef<LibraryApi>(function Library(_, ref) {
  const { overview } = useApp();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [query, setQuery] = useState("");
  const cards = useRef(new Map<string, { el: HTMLDivElement | null; body: JobBodyApi | null }>());
  const [scrollTo, setScrollTo] = useState<{ id: string; itemId: string | null } | null>(null);

  // the open cards are remembered in this browser
  useEffect(() => { setExpanded(new Set(store.get<string[]>("expanded", []))); }, []);
  const save = (s: Set<string>) => { store.set("expanded", [...s]); return s; };
  const markExpanded = (id: string) => setExpanded((s) => save(new Set(s).add(id)));
  const toggle = (id: string) => setExpanded((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return save(n); });
  const forget = (id: string) => setExpanded((s) => { const n = new Set(s); n.delete(id); return save(n); });

  useImperativeHandle(ref, () => ({
    markExpanded,
    /** Open a playlist's card (if needed) and scroll to it; optionally highlight one video. */
    openJob(id, itemId = null) { markExpanded(id); setScrollTo({ id, itemId }); },
  }));

  useEffect(() => {
    if (!scrollTo) return;
    const card = cards.current.get(scrollTo.id);
    if (!card?.el) return;
    card.el.scrollIntoView({ behavior: "smooth", block: "start" });
    if (scrollTo.itemId) card.body?.reveal(scrollTo.itemId);
    setScrollTo(null);
  }, [scrollTo, expanded]);

  const jobs = overview?.jobs || [];
  const q = query.trim().toLowerCase();
  return (
    <section>
      <div className="section-head">
        <h2>Your playlists</h2>
        <input id="lib-filter" type="search" placeholder="Search playlists  /" aria-label="Search playlists" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <div id="jobs">
        {overview && !jobs.length && <p className="empty">Nothing saved yet. Paste a playlist link above.</p>}
        {!overview && <p className="empty">Nothing saved yet. Paste a playlist link above.</p>}
        {jobs.map((j) => (
          <JobCard key={j.id} job={j} open={expanded.has(j.id)} hidden={!!q && !j.title.toLowerCase().includes(q)}
            onToggle={() => toggle(j.id)} onRemoved={() => forget(j.id)}
            register={(el, body) => { if (el || body) cards.current.set(j.id, { el, body }); else cards.current.delete(j.id); }} />
        ))}
      </div>
    </section>
  );
});

interface CardProps {
  job: Job; open: boolean; hidden: boolean;
  onToggle: () => void; onRemoved: () => void;
  register: (el: HTMLDivElement | null, body: JobBodyApi | null) => void;
}

function JobCard({ job: j, open, hidden, onToggle, onRemoved, register }: CardProps) {
  const { refresh, toast, confirmRemove } = useApp();
  const el = useRef<HTMLDivElement>(null);
  const body = useRef<JobBodyApi>(null);
  const [built, setBuilt] = useState(open);            // the body is built the first time the card opens
  const [refreshing, setRefreshing] = useState(false);
  const [imgOk, setImgOk] = useState(true);
  useEffect(() => { if (open) setBuilt(true); }, [open]);
  useEffect(() => { register(el.current, body.current); return () => register(null, null); });

  const c = j.counts;
  const busy = c.queued + c.downloading + c.processing > 0;
  const buttons: [string, string, string?][] = [];
  if (busy) {
    buttons.push(j.status === "paused" || j.status === "pausing" ? ["resume", "Resume", "primary"] : ["pause", "Pause"]);
    buttons.push(["stop", "Stop"]);
  }
  if (c.failed) buttons.push(["retry", `Retry failed (${c.failed})`]);
  if (j.kind === "playlist") buttons.push(["refresh", "Check for new videos"]);
  buttons.push(["open", "Open folder"]);
  buttons.push(["remove", "Remove"]);

  async function act(a: string) {
    try {
      if (a === "stop" && !confirm("Stop downloading? Videos still in the queue go back to “Not downloaded”.")) return;
      if (a === "remove") {
        const answer = await confirmRemove(j.title);
        if (!answer) return;
        await api(`/api/jobs/${j.id}/remove`, { method: "POST", body: { delete_files: answer.deleteFiles } });
        onRemoved();
        toast(answer.deleteFiles ? "Removed; its downloaded files are being deleted" : "Removed from your library (files kept)");
        return refresh();
      }
      if (a === "refresh") setRefreshing(true);
      const res = await api(`/api/jobs/${j.id}/${a}`, { method: "POST" });
      if (a === "refresh") toast(res.added ? `Added ${plural(res.added, "new video")}` : "No new videos in this playlist");
      refresh();
    } catch (err: any) {
      toast(err.message);
    } finally {
      if (a === "refresh") setRefreshing(false);
    }
  }

  return (
    <div className={`job ${open ? "open" : ""} ${hidden ? "hidden" : ""}`} ref={el}>
      <div className="job-head" onClick={(e) => {
        // a click anywhere on the header (not a button, not selecting text) opens or closes the card
        if (!(e.target as HTMLElement).closest("button") && !String(window.getSelection())) onToggle();
      }}>
        <img alt="" loading="lazy" src={j.thumbnail || undefined} style={j.thumbnail && imgOk ? undefined : { visibility: "hidden" }}
          onError={() => setImgOk(false)} />
        <div className="job-main"><CardInfo j={j} /></div>
        <div className="job-btns">
          {buttons.map(([a, label, cls = ""]) => (
            <button key={a} className={`small ${cls}`} data-act={a} disabled={a === "refresh" && refreshing} onClick={() => act(a)}>
              {a === "refresh" && refreshing ? "Checking…" : label}
            </button>
          ))}
          <button className="small ghost chev" data-act="toggle" aria-expanded={open} title={open ? "Close" : "Open"} aria-label={open ? "Close" : "Open"} onClick={onToggle}>
            <svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6" /></svg>
          </button>
        </div>
      </div>
      {built && <JobBody ref={body} job={j} open={open} />}
    </div>
  );
}

function CardInfo({ j }: { j: Job }) {
  const c = j.counts;
  return (
    <>
      <div className="job-title" title={j.title}>{j.title}</div>
      <div className={`bar ${j.status === "complete" ? "ok" : ""}`}><div style={{ width: `${j.percent}%` }} /></div>
      <div className="job-sub">
        <span className={`badge s-${j.status}`}>{JOB_LABEL[j.status] || j.status}</span>
        <span><b>{c.done}</b> / {j.total - c.removed} downloaded ({Math.floor(j.percent)}%)</span>
        {c.downloading + c.processing > 0 && <span className="s-downloading">{c.downloading + c.processing} downloading</span>}
        {c.queued > 0 && <span>{c.queued} queued</span>}
        {c.failed > 0 && <span className="s-failed">{c.failed} failed</span>}
        {c.removed > 0 && <span className="muted" title="Private, deleted or removed from the playlist">{c.removed} unavailable</span>}
        {!!j.speed && <span>{fmtSpeed(j.speed)}</span>}
        {j.check?.running && <span className="s-downloading">Checking files {j.check.done} / {j.check.total}…</span>}
        {!!j.sync?.every && <span className="muted" title={j.sync.error || ""}>
          ↻ {SYNC_LABEL[j.sync.every] || ""}{j.sync.auto ? ", auto-download" : ""}{j.sync.error ? " (last check failed)" : ""}</span>}
      </div>
    </>
  );
}
