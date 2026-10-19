"use client";
// The top bar, the "add a link" form and the queue-wide wait banner.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { fmtSpeed, parseUrls, plural } from "../lib/util";
import { useApp } from "./AppContext";
import { NetPill } from "./NetPanel";

export function TopBar({ net, onShortcuts }: { net: any; onShortcuts: () => void }) {
  const { overview, openSettings } = useApp();
  const active = overview?.active || 0;
  return (
    <header className="topbar">
      <div className="topbar-inner">
        <div className="brand">
          <span className="logo" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 5v10m0 0-4-4m4 4 4-4M7 19h10" /></svg></span>
          YT Downloader
        </div>
        <div className="stats">
          <NetPill net={net} />
          <span className="stat" id="stat-active">{active} downloading</span>
          <span className="stat" id="stat-speed">{overview && active ? fmtSpeed(overview.speed) : "—"}</span>
          <button className="icon-btn" id="btn-shortcuts" type="button" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts" onClick={onShortcuts}>
            <svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2" /><path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10" /></svg>
          </button>
          <button className="icon-btn" id="btn-settings" type="button" title="Settings (s)" aria-label="Settings" onClick={() => openSettings()}>
            <svg viewBox="0 0 24 24"><path d="M4 7h9M17 7h3M4 17h3M11 17h9" /><circle cx="15" cy="7" r="2" /><circle cx="9" cy="17" r="2" /></svg>
          </button>
        </div>
      </div>
    </header>
  );
}

const HINT = "The whole playlist is saved to your library. Pick which videos to download whenever you like.";

export function OpenForm() {
  const { overview, refresh, toast, markExpanded, openJob } = useApp();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState({ msg: HINT, error: false });
  const fileInput = useRef<HTMLInputElement>(null);
  const importRunning = useRef(false);

  // progress of "add many links", which runs on the server
  const imp = overview?.import;
  useEffect(() => {
    if (!imp) return;
    if (imp.running) {
      importRunning.current = true;
      setStatus({ msg: `Adding links: ${imp.done} of ${imp.total}${imp.failed.length ? ` (${imp.failed.length} failed)` : ""}…`, error: false });
    } else if (importRunning.current) {
      importRunning.current = false;
      setStatus({
        error: imp.failed.length > 0,
        msg: `Added ${plural(imp.added, "link")}` + (imp.failed.length
          ? `; ${imp.failed.length} couldn't be read: ${imp.failed.slice(0, 3).map((f) => `${f.url} (${f.error})`).join("; ")}` : "."),
      });
    }
  }, [imp]);

  async function addLinks(urls: string[]) {
    const res = await api("/api/open-many", { method: "POST", body: { urls } });
    setStatus({ msg: `Adding ${plural(res.total, "link")}…`, error: false });
    importRunning.current = true;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const value = text.trim();
    const urls = parseUrls(value);
    if (!value) return;
    if (urls.length > 1) {
      try { await addLinks(urls); setText(""); } catch (err: any) { setStatus({ msg: err.message, error: true }); }
      return;
    }
    setBusy(true);
    setStatus({ msg: "Reading the playlist… large ones can take 10–30 seconds.", error: false });
    try {
      const job = await api("/api/open", { method: "POST", body: { url: urls[0] || value } });
      setText("");
      const isNew = job.added === job.total;
      setStatus({
        error: false,
        msg: isNew
          ? `Saved “${job.title}” — ${plural(job.total, "video")}${job.counts.done ? `, ${job.counts.done} already downloaded` : ""}.`
          : `Updated “${job.title}” — ${job.added ? `${plural(job.added, "new video")} added` : "no new videos"}.`,
      });
      markExpanded(job.id);
      await refresh();
      openJob(job.id);
    } catch (err: any) {
      setStatus({ msg: err.message, error: true });
    } finally {
      setBusy(false);
    }
  }

  function paste(e: React.ClipboardEvent) {
    const urls = parseUrls(e.clipboardData?.getData("text") || "");
    if (urls.length > 1) {
      e.preventDefault();
      if (confirm(`Add these ${urls.length} links to your library?`)) addLinks(urls).catch((err) => toast(err.message));
    }
  }

  async function fromFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const urls = parseUrls(await file.text());
    if (!urls.length) return toast(`No links found in “${file.name}”`);
    if (confirm(`Add ${plural(urls.length, "link")} from “${file.name}” to your library?`)) addLinks(urls).catch((err) => toast(err.message));
  }

  return (
    <section className="hero">
      <h1>Download from YouTube</h1>
      <p className="lead">Save a playlist, channel or video to your library, then choose which videos to download.</p>
      <form id="open-form" className="url-row" onSubmit={submit}>
        <svg className="url-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" /></svg>
        <input id="url" type="text" inputMode="url" placeholder="Paste a YouTube link — or several at once…" aria-label="YouTube link"
          autoComplete="off" autoFocus value={text} onChange={(e) => setText(e.target.value)} onPaste={paste} />
        <button type="button" className="ghost" id="btn-import-links" title="Add every link in a .txt file" onClick={() => fileInput.current?.click()}>Import .txt</button>
        <button type="submit" className="primary" id="btn-open" disabled={busy}>{busy ? "Reading…" : "Add"}</button>
        <input type="file" id="links-file" accept=".txt,text/plain" hidden ref={fileInput} onChange={fromFile} />
      </form>
      <p className={`hint ${status.error ? "error" : ""}`} id="open-status">{status.msg}</p>
    </section>
  );
}

export function WaitBanner() {
  const { overview, activity, importCookies, openSettings, refresh, toast } = useApp();
  const waiting = overview?.waiting;
  const hold = activity?.stats?.hold;
  // YouTube's bot check and unreadable cookies are fixed by other cookies: offer them right here
  const cookies = hold && ["ratelimit", "cookies"].includes(hold.kind);
  return (
    <div className={`wait-banner ${waiting ? "" : "hidden"}`} id="wait-banner" role="status">
      {waiting && <>
        <span>{`⏸ ${waiting}`}</span>
        <span className="wait-btns">
          {cookies && <>
            <button className="small" title="Load a cookies.txt exported from a browser where you're signed in to YouTube" onClick={importCookies}>Import cookies…</button>
            <button className="small" onClick={() => openSettings("cookies")}>Use cookies from browser…</button>
          </>}
          {hold && <button className="small" title="Use this once you've fixed the cause (freed disk space, changed the proxy…)"
            onClick={async () => { await api("/api/queue/clear-hold", { method: "POST" }).catch((err) => toast(err.message)); refresh(); }}>Try now</button>}
        </span>
      </>}
    </div>
  );
}
