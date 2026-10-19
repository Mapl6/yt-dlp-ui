"use client";
// The body of an open playlist card: download options (and the playlist's Save button), automatic checks,
// selection, tabs and filters, the video list, and the file tools.
import { forwardRef, memo, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { DOWNLOADED, IN_QUEUE, SELECTABLE, TABS, UNAVAILABLE, visibleItems } from "../lib/filters";
import type { Item, Job } from "../lib/types";
import { AUDIO, fileName, fmtBytes, fmtSpeed, fmtTime, parseRange, plural } from "../lib/util";
import { useApp, type MenuEntry } from "./AppContext";
import { MORE, PAUSE, PLAY, X } from "./icons";
import { ITEM_LABEL, REVEAL_LABEL, SYNC_LABEL } from "./labels";
import { VirtualList, type VirtualListApi } from "./VirtualList";

const ROW_HEIGHT = 60;
const DEFAULT_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144];

export interface JobBodyApi { reveal: (itemId: string) => void }

interface Form {
  quality: string; container: string; output_dir: string; sponsorblock: string; sec_start: string; sec_end: string;
  sub_langs: string; numbering: boolean; thumbnail: boolean; metadata: boolean; subtitles: boolean; auto_subs: boolean;
}

function initialForm(j: Job, settings: Record<string, any>): Form {
  const o = j.options || {};
  const heights = j.heights?.length ? j.heights : DEFAULT_HEIGHTS;
  const qualities = ["best", ...heights.map(String), ...AUDIO];
  const pick = (k: string, dflt: boolean) => (k in o ? !!o[k] : dflt);
  return {
    quality: o.quality && qualities.includes(String(o.quality)) ? String(o.quality) : heights.includes(1080) ? "1080" : "best",
    container: o.container || "mp4", output_dir: j.outdir, sponsorblock: o.sponsorblock || "", sec_start: "", sec_end: "",
    sub_langs: o.sub_langs || settings.sub_langs || "en",
    numbering: pick("numbering", true), thumbnail: pick("thumbnail", true), metadata: pick("metadata", true),
    subtitles: pick("subtitles", false), auto_subs: pick("auto_subs", false),
  };
}

function collect(f: Form): Record<string, any> {
  const options: Record<string, any> = {
    quality: f.quality, container: f.container, output_dir: f.output_dir.trim(), numbering: f.numbering,
    thumbnail: f.thumbnail, metadata: f.metadata, subtitles: f.subtitles, sub_langs: f.sub_langs.trim(),
    auto_subs: f.auto_subs, sponsorblock: f.sponsorblock, skip_existing: true,
  };
  const start = f.sec_start.trim(), end = f.sec_end.trim();
  if (start || end) options.section = { start, end };
  return options;
}

/** The options as the playlist stores them (a part of a video is only ever for one download). */
const optionsKey = (f: Form) => { const { section, ...o } = collect(f); return JSON.stringify(o); };

function checkSummary(c: NonNullable<Job["check"]>) {
  const bits = [`${c.ok} OK`];
  if (c.found) bits.push(`${c.found} found in the folder`);
  if (c.damaged) bits.push(`${c.damaged} damaged`);
  if (c.missing) bits.push(`${c.missing} missing`);
  return bits.join(", ");
}

export const JobBody = forwardRef<JobBodyApi, { job: Job; open: boolean }>(function JobBody({ job: j, open }, ref) {
  const { overview, refresh, toast, itemAction, openMenu } = useApp();
  const [form, setForm] = useState<Form>(() => initialForm(j, overview?.settings || {}));
  const [savedOpts, setSavedOpts] = useState(() => optionsKey(form));
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));
  const [syncEvery, setSyncEvery] = useState(String(j.sync?.every || 0));
  const [syncAuto, setSyncAuto] = useState(!!j.sync?.auto);

  // the videos: kept by id, updated from items?since=<rev>
  const items = useRef(new Map<string, Item>());
  const rev = useRef(0);
  const jobRev = useRef(j.rev);
  jobRev.current = j.rev;
  const syncing = useRef(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!open || syncing.current || rev.current >= j.rev) return;
    syncing.current = true;
    (async () => {
      try {
        while (rev.current < jobRev.current) {
          const data = await api(`/api/jobs/${j.id}/items?since=${rev.current}`);
          for (const it of data.items) items.current.set(it.id, it);
          rev.current = data.rev;
          setVersion((v) => v + 1);
        }
      } catch {} finally { syncing.current = false; }
    })();
  }, [open, j.rev, j.id]);

  const [tab, setTab] = useState("all");
  const [text, setText] = useState("");
  const [dur, setDur] = useState("any");
  const [sort, setSort] = useState("index");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const list = useMemo(() => visibleItems([...items.current.values()], { tab, text, dur, sort }),
    [version, tab, text, dur, sort]);   // eslint-disable-line react-hooks/exhaustive-deps

  // a selected video that can no longer be downloaded (it finished, or went away) drops out of the selection
  useEffect(() => {
    let changed = false;
    const next = new Set(selected);
    for (const id of selected) { const it = items.current.get(id); if (!it || !SELECTABLE(it)) { next.delete(id); changed = true; } }
    if (changed) setSelected(next);
  }, [version]);   // eslint-disable-line react-hooks/exhaustive-deps

  const vlist = useRef<VirtualListApi>(null);
  useImperativeHandle(ref, () => ({ reveal: (id) => vlist.current?.reveal(id) }), []);

  const audio = AUDIO.includes(form.quality);
  const changed = optionsKey(form) !== savedOpts;
  const visible = list.filter(SELECTABLE);
  const picked = visible.filter((it) => selected.has(it.id)).length;
  const selAll = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => { if (selAll.current) selAll.current.indeterminate = picked > 0 && picked < visible.length; });

  // estimated size of the selection, next to the Download button
  const [estimate, setEstimate] = useState<{ text: string; warn: boolean; title: string }>({ text: "", warn: false, title: "" });
  useEffect(() => {
    if (!selected.size) { setEstimate({ text: "", warn: false, title: "" }); return; }
    const t = setTimeout(async () => {
      try {
        const e = await api(`/api/jobs/${j.id}/estimate`, { method: "POST", body: { ids: [...selected], options: collect(form) } });
        const size = e.bytes ? `≈ ${fmtBytes(e.bytes)}${e.unknown ? ` + ${e.unknown} of unknown length` : ""}` : "Size unknown";
        setEstimate({
          text: e.problem ? `${size} · ${e.problem}` : e.free != null ? `${size} · ${fmtBytes(e.free)} free` : size,
          warn: !!(e.low || e.problem),
          title: e.low ? "Not enough free space: the queue pauses when less than 1 GB is left" : "Estimated from each video's length and the chosen quality",
        });
      } catch (err: any) { setEstimate({ text: err.message, warn: true, title: "" }); }
    }, 400);
    return () => clearTimeout(t);
  }, [selected, form, j.id]);

  // "Check files": a message when a check started here finishes
  const checking = useRef(false);
  const c = j.check;
  useEffect(() => {
    if (checking.current && !c?.running && c?.finished) {
      toast(c.damaged || c.missing ? `Check done: ${checkSummary(c)}. Broken files are in the Failed tab — press Retry to download them again.`
        : `Check done: all ${c.ok + c.found} files are healthy${c.found ? ` (${c.found} found in the folder)` : ""}.`);
    }
    checking.current = !!c?.running;
  }, [c?.running, c?.finished]);   // eslint-disable-line react-hooks/exhaustive-deps

  const [busy, setBusy] = useState("");
  const run = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    try { await fn(); } catch (err: any) { toast(err.message); } finally { setBusy(""); }
  };

  function selectRange(spec: string) {
    const all = [...items.current.values()];
    const res = parseRange(spec, Math.max(0, ...all.map((i) => i.index)));
    if (res.error !== undefined) return toast(`Can't read "${res.error}". Use e.g. 1-50, 80, 100-`);
    const next = new Set(selected);
    let n = 0;
    for (const it of all) if (res.picked.has(it.index) && SELECTABLE(it)) { next.add(it.id); n++; }
    setSelected(next);
    toast(n ? `Selected ${plural(n, "video")} not yet downloaded` : "Everything in that range is already downloaded or queued");
  }

  const download = () => run("download", async () => {
    const res = await api(`/api/jobs/${j.id}/download`, { method: "POST", body: { ids: [...selected], options: collect(form) } });
    setSelected(new Set());
    setSavedOpts(optionsKey(form));               // Download stores the options too
    toast(`Queued ${plural(res.queued, "video")}`);
    refresh();
  });

  const saveOptions = () => run("save", async () => {
    const { section, ...options } = collect(form);
    await api(`/api/jobs/${j.id}/options`, { method: "PUT", body: { options } });
    setSavedOpts(optionsKey(form));
    toast("Options saved — Download and automatic downloads use them");
    refresh();
  });

  async function saveSync(every: string, auto: boolean) {
    setSyncEvery(every);
    setSyncAuto(auto);
    try {
      await api(`/api/jobs/${j.id}/sync`, { method: "PUT", body: { every_hours: Number(every), auto_download: auto } });
      toast(Number(every) ? `Checking for new videos ${SYNC_LABEL[every]}${auto ? " and downloading them" : ""}` : "Automatic checks turned off");
      refresh();
    } catch (err: any) { toast(err.message); }
  }

  const tools = (role: string) => run(role, async () => {
    if (role === "check") {
      const running = j.check?.running;
      await api(`/api/jobs/${j.id}/check${running ? "/stop" : ""}`, { method: "POST" });
      if (!running) checking.current = true;
      return refresh();
    }
    if (role === "cleanup") {
      const found = await api(`/api/jobs/${j.id}/cleanup`);
      if (!found.files) return toast("No leftover files — the folder is clean.");
      const names = found.names.slice(0, 8).join("\n") + (found.files > 8 ? `\n…and ${found.files - 8} more` : "");
      if (!confirm(`Delete ${plural(found.files, "leftover file")} (${fmtBytes(found.bytes)})?\n\n${names}\n\nVideos in the queue are not touched.`)) return;
      const res = await api(`/api/jobs/${j.id}/cleanup`, { method: "POST" });
      return toast(`Deleted ${plural(res.files, "leftover file")}, freed ${fmtBytes(res.bytes)}`);
    }
    const res = await api(`/api/jobs/${j.id}/m3u`, { method: "POST" });
    toast(`Saved “${res.name}” with ${plural(res.count, "video")} in the playlist folder`);
  });

  const counts = j.counts;
  const tabCount: Record<string, number> = {
    all: j.total, new: counts.new, queue: counts.queued + counts.downloading + counts.processing,
    done: counts.done, failed: counts.failed, removed: counts.removed,
  };
  const heights = j.heights?.length ? j.heights : DEFAULT_HEIGHTS;
  const n = selected.size;
  // stable handlers for the memoised rows (they read the latest state through a ref)
  const latest = useRef({ itemAction, openMenu, toast, jobId: j.id });
  latest.current = { itemAction, openMenu, toast, jobId: j.id };
  const rowHandlers = useMemo(() => {
    const action = (it: Item, a: string) => {
      const l = latest.current;
      l.itemAction(l.jobId, it.id, a, it.title).catch((err) => l.toast(err.message));
    };
    return {
      select: (id: string, on: boolean) => setSelected((s) => { const n = new Set(s); on ? n.add(id) : n.delete(id); return n; }),
      action,
      menu: (anchor: HTMLElement, it: Item) => latest.current.openMenu(anchor, menuFor(it), (a) => action(it, a)),
    };
  }, []);

  return (
    <div className={`job-body ${open ? "" : "hidden"}`}>
      <div className="opts-panel">
        <div className="options">
          <label>Quality <select data-opt="quality" value={form.quality} onChange={(e) => set("quality", e.target.value)}>
            <optgroup label="Video">
              <option value="best">Best available</option>
              {heights.map((h) => <option key={h} value={String(h)}>{h >= 2160 ? "4K (2160p)" : h >= 1440 ? "2K (1440p)" : `${h}p`}</option>)}
            </optgroup>
            <optgroup label="Audio only"><option value="mp3">MP3</option><option value="m4a">M4A (AAC)</option><option value="opus">Opus</option></optgroup>
          </select></label>
          <label data-video-only="" className={audio ? "hidden" : ""}>Format
            <select data-opt="container" value={form.container} onChange={(e) => set("container", e.target.value)}>
              <option value="mp4">MP4 (plays everywhere)</option>
              <option value="mkv">MKV (best quality codecs)</option>
              <option value="webm">WebM</option>
            </select>
          </label>
          <label className="grow">Save to <input data-opt="output_dir" type="text" value={form.output_dir} onChange={(e) => set("output_dir", e.target.value)} /></label>
        </div>
        <div className="options">
          <label>Sponsor segments
            <select data-opt="sponsorblock" title="Uses the community SponsorBlock database" value={form.sponsorblock} onChange={(e) => set("sponsorblock", e.target.value)}>
              <option value="">Keep as they are</option>
              <option value="mark">Mark as chapters</option>
              <option value="remove">Cut them out</option>
            </select>
          </label>
          <label className="part">Only part (optional)
            <span className="part-row"><input data-opt="sec_start" type="text" placeholder="from 0:00" aria-label="Start" value={form.sec_start} onChange={(e) => set("sec_start", e.target.value)} />
              <span aria-hidden="true">–</span><input data-opt="sec_end" type="text" placeholder="to end" aria-label="End" value={form.sec_end} onChange={(e) => set("sec_end", e.target.value)} /></span>
          </label>
          <label data-subs-only="" className={audio || !form.subtitles ? "hidden" : ""}>Subtitle languages{" "}
            <input data-opt="sub_langs" type="text" placeholder="en, fa" title="Language codes separated by commas, or “all”" value={form.sub_langs} onChange={(e) => set("sub_langs", e.target.value)} /></label>
        </div>
        <div className="checks">
          {j.kind === "playlist" && <Check label="Number files by playlist order" on={form.numbering} set={(v) => set("numbering", v)} />}
          <Check label="Embed thumbnail" on={form.thumbnail} set={(v) => set("thumbnail", v)} />
          <Check label="Embed metadata & chapters" on={form.metadata} set={(v) => set("metadata", v)} />
          {!audio && <Check label="Embed subtitles" on={form.subtitles} set={(v) => set("subtitles", v)} />}
          {!audio && form.subtitles && <Check label="Include auto-generated subtitles" on={form.auto_subs} set={(v) => set("auto_subs", v)} />}
        </div>
        <div className="opts-save">
          <span className="muted" aria-live="polite">{changed ? "Not saved — press Save options, or Download to use them" : ""}</span>
          <button className="small" type="button" disabled={!changed || busy === "save"} onClick={saveOptions}
            title="Keep these options for this playlist without downloading anything">Save options</button>
        </div>
        {j.kind === "playlist" && <div className="sync-row">
          <label>Check for new videos
            <select value={syncEvery} onChange={(e) => saveSync(e.target.value, syncAuto)}>
              <option value="0">Never (only when I ask)</option>
              <option value="6">Every 6 hours</option>
              <option value="12">Every 12 hours</option>
              <option value="24">Every day</option>
              <option value="168">Every week</option>
            </select>
          </label>
          <label className="inline-check"><input type="checkbox" checked={syncAuto} disabled={!Number(syncEvery)}
            onChange={(e) => saveSync(syncEvery, e.target.checked)} /> Download new videos automatically</label>
        </div>}
      </div>

      <div className="select-bar">
        <label className="sel-all"><input type="checkbox" ref={selAll} checked={visible.length > 0 && picked === visible.length}
          onChange={(e) => {
            const next = new Set(selected);
            for (const it of list) if (SELECTABLE(it)) e.target.checked ? next.add(it.id) : next.delete(it.id);
            setSelected(next);
          }} /> <span>{n} selected</span></label>
        <button className="small" type="button" onClick={() => {
          const next = new Set(selected);
          for (const it of items.current.values()) if (SELECTABLE(it)) next.add(it.id);
          setSelected(next);
        }}>Select not downloaded</button>
        <RangeGroup onSelect={selectRange} />
        <input type="search" placeholder="Filter titles" aria-label="Filter titles" value={text} onChange={(e) => setText(e.target.value)} />
        <select aria-label="Sort" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="index">Playlist order</option>
          <option value="title">Title</option>
          <option value="duration">Length</option>
          <option value="status">Status</option>
          <option value="finished">Recently downloaded</option>
        </select>
        <select aria-label="Length" value={dur} onChange={(e) => setDur(e.target.value)}>
          <option value="any">Any length</option>
          <option value="short">Under 10 min</option>
          <option value="medium">10–60 min</option>
          <option value="long">Over an hour</option>
        </select>
        <span className={`estimate ${estimate.warn ? "warn" : ""}`} aria-live="polite" title={estimate.title}>{estimate.text}</span>
        <button className="primary" type="button" disabled={n === 0 || busy === "download"} onClick={download}>
          {n ? `Download ${plural(n, "video")}` : "Download selected"}
        </button>
      </div>

      <div className="tabs">
        {TABS.map(([k, label]) => (
          <button key={k} className={`${k === tab ? "on" : ""} ${k === "removed" && !counts.removed ? "hidden" : ""}`} onClick={() => setTab(k)}>
            {label} <span className="cnt">{tabCount[k] ?? ""}</span>
          </button>
        ))}
      </div>
      {open && <VirtualList ref={vlist} className="items" items={list} rowHeight={ROW_HEIGHT} rowKey={(it) => it.id}
        renderRow={(it, top) => <ItemRow key={it.id} it={it} top={top} selected={selected.has(it.id)} h={rowHandlers} />} />}
      <p className={`items-empty ${list.length > 0 || items.current.size === 0 ? "hidden" : ""}`}>No videos match these filters.</p>

      <div className="tools">
        <button className="small ghost" type="button" disabled={busy === "check"} onClick={() => tools("check")}
          title="Verify every downloaded file, and find good files already in the folder">
          {c?.running ? `Stop checking (${c.done} / ${c.total})` : "Check files"}</button>
        <button className="small ghost" type="button" disabled={busy === "cleanup"} onClick={() => tools("cleanup")}
          title="Delete partial downloads and other leftovers of this playlist">Clean up leftovers</button>
        <button className="small ghost" type="button" disabled={busy === "m3u"} onClick={() => tools("m3u")}
          title="Save a playlist file (.m3u8) of the downloaded videos into the folder">Export .m3u</button>
        <span className="muted">{c?.running ? "Checking every file — you can keep using the app."
          : c?.finished ? `Last check ${new Date(c.finished * 1000).toLocaleString()}: ${checkSummary(c)}` : ""}</span>
      </div>
    </div>
  );
});

function Check({ label, on, set }: { label: string; on: boolean; set: (v: boolean) => void }) {
  return <label><input type="checkbox" checked={on} onChange={(e) => set(e.target.checked)} /> {label}</label>;
}

function RangeGroup({ onSelect }: { onSelect: (spec: string) => void }) {
  const [spec, setSpec] = useState("");
  return (
    <div className="range-group">
      <input type="text" placeholder="Range: 1-50, 80, 100-" aria-label="Range" value={spec} onChange={(e) => setSpec(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); onSelect(spec); } }} />
      <button className="small" type="button" onClick={() => onSelect(spec)}>Select</button>
    </div>
  );
}

function menuFor(it: Item): (MenuEntry | "-")[] {
  const m: (MenuEntry | "-")[] = [];
  if (DOWNLOADED(it)) {
    if (it.filepath) m.push({ label: REVEAL_LABEL, value: "reveal" }, { label: "Play", value: "play" });
    m.push({ label: "Details", value: "log" }, "-", { label: "Delete file…", value: "delete-file", danger: true });
  } else if (it.status === "queued") {
    m.push({ label: "Move to top of queue", value: "top" }, { label: "Move to bottom", value: "bottom" },
      { label: "Try now", value: "now" }, { label: it.held ? "Continue" : "Pause this video", value: it.held ? "unhold" : "hold" },
      "-", { label: "Details", value: "log" }, { label: "Remove from queue", value: "cancel", danger: true });
  } else if (IN_QUEUE(it)) {
    m.push({ label: "Pause this video", value: "hold" }, { label: "Details", value: "log" }, "-", { label: "Stop", value: "cancel", danger: true });
  } else {
    m.push({ label: "Mark as downloaded", value: "mark-done" }, { label: "Details", value: "log" });
  }
  return m;
}

const partLabel = (s: Item["section"]) => (s ? `part ${fmtTime(s.start)}–${s.end != null ? fmtTime(s.end) : "end"}` : "");

interface RowHandlers {
  select: (id: string, on: boolean) => void;
  action: (it: Item, action: string) => void;
  menu: (anchor: HTMLElement, it: Item) => void;
}
interface RowProps { it: Item; top: number; selected: boolean; h: RowHandlers }

/** One video. Memoised: it only re-renders when the video, its selection or its position changes. */
const ItemRow = memo(function ItemRow({ it, top, selected, h }: RowProps) {
  let meta: React.ReactNode = null, metaCls = "meta", full = "";
  if (it.status === "downloading") {
    meta = `${(it.progress || 0).toFixed(1)}% · ${fmtBytes(it.downloaded)} / ${fmtBytes(it.total)} · ${fmtSpeed(it.speed)}${it.eta != null ? ` · ${fmtTime(it.eta)} left` : ""}`;
  } else if (it.status === "processing") {
    meta = `${it.step || "Processing"}…`;
  } else if (DOWNLOADED(it)) {
    meta = <>{it.filepath ? fileName(it.filepath) : "Already in the folder"}{it.section ? ` · ${partLabel(it.section)}` : ""}
      {it.health === "ok" && <> · <span className="s-done">✓ verified</span></>}</>;
  } else if (it.status === "queued" && it.held) {
    meta = "Paused — press ▶ to continue";
  }
  if (it.removed && !UNAVAILABLE(it)) meta = <>{meta}{meta ? " · " : ""}no longer in the playlist</>;
  if (it.error && (it.status === "failed" || it.status === "queued")) { metaCls = "meta err"; meta = it.error; full = it.error; }

  // buttons inside a <label> row must not toggle its checkbox
  const btn = (action: string, label: string, icon: React.ReactNode) => (
    <button className="small ghost icon" title={label} aria-label={label} onClick={(e) => { e.preventDefault(); h.action(it, action); }}>{icon}</button>
  );
  const sel = SELECTABLE(it);
  return (
    <label className={`item ${selected ? "picked" : ""}`} style={{ transform: `translateY(${top}px)` }} data-key={it.id}>
      <input type="checkbox" disabled={!sel} checked={selected} aria-label="Select" onChange={(e) => h.select(it.id, e.target.checked)} />
      <span className="num">{it.index}</span>
      <div className="mid">
        <div className="t" title={it.title}>{it.title}</div>
        {meta && <div className={metaCls} title={full || undefined}>{meta}</div>}
        {it.status === "downloading" && <div className="bar small"><div style={{ width: `${it.progress || 0}%` }} /></div>}
      </div>
      <span className="dur">{fmtTime(it.duration)}</span>
      <div className="right">
        {UNAVAILABLE(it) ? <span className="badge s-removed" title="Private, deleted or removed from the playlist">Unavailable</span>
          : it.held && it.status === "queued" ? <span className="badge s-paused">Paused</span>
          : <span className={`badge s-${it.status}`}>{ITEM_LABEL[it.status] || it.status}</span>}
        {!UNAVAILABLE(it) && <>
          {it.status === "failed" && <button className="small" onClick={(e) => { e.preventDefault(); h.action(it, "retry"); }}>Retry</button>}
          {IN_QUEUE(it) && <>
            {it.held ? btn("unhold", "Continue", PLAY) : btn("hold", "Pause this video", PAUSE)}
            {btn("cancel", "Remove from queue", X)}
          </>}
          {DOWNLOADED(it) && it.filepath && btn("play", "Play", PLAY)}
          <button className="small ghost icon" title="More" aria-label="More actions" aria-haspopup="menu"
            onClick={(e) => { e.preventDefault(); h.menu(e.currentTarget, it); }}>{MORE}</button>
        </>}
      </div>
    </label>
  );
});
