"use client";
// The Settings dialog, including cookies, the download schedule, speed limits, notifications, yt-dlp updates
// and export / import of all data.
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { api, apiFetch } from "../lib/api";
import { askPermission, notifySupported, permissionDenied, setNotifyEnabled, takeLegacyNotify } from "../lib/notify";
import { fmtAgo, isNewer } from "../lib/util";
import { useApp } from "./AppContext";

export interface SettingsApi { open: (section?: "cookies") => void; importCookies: () => void }

interface Form {
  output_dir: string; concurrency: string; rate: string; total_rate: string; sleep: string; proxy: string;
  waitnet: boolean; sched_on: boolean; sched_start: string; sched_end: string; sub_langs: string; notify: boolean;
  cookies: string; profile: string;
}

const formFrom = (s: Record<string, any>): Form => ({
  output_dir: s.output_dir || "", concurrency: String(s.concurrency || 3), rate: String(s.rate_limit_mb || 0),
  total_rate: String(s.total_rate_mb || 0), sleep: String(s.sleep_between || 0), proxy: s.proxy || "",
  waitnet: s.wait_for_network !== false, sched_on: !!s.schedule_on, sched_start: s.schedule_start || "01:00",
  sched_end: s.schedule_end || "07:00", sub_langs: s.sub_langs || "en", notify: !!s.notify,
  cookies: s.cookies_browser || "", profile: s.cookies_profile || "",
});

const STATUS_NOTE: Record<string, string> = { blocked: " (needs Full Disk Access)", "no-profiles": " (no profiles found)" };
const NOTIFY_NOTE = "When a playlist finishes downloading or a video fails.";

export const SettingsDialog = forwardRef<SettingsApi>(function SettingsDialog(_, ref) {
  const { overview, toast, refresh } = useApp();
  const settings = overview?.settings || {};
  const cookieFile = overview?.cookie_file || null;
  const dlg = useRef<HTMLDialogElement>(null);
  const cookieInput = useRef<HTMLInputElement>(null);
  const importInput = useRef<HTMLInputElement>(null);
  const cookiesSection = useRef<HTMLDivElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [f, setF] = useState<Form>(formFrom(settings));
  const formRef = useRef(f);
  formRef.current = f;
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((x) => ({ ...x, [k]: v }));
  const [sources, setSources] = useState<any[]>([]);
  const [cookieStatus, setCookieStatus] = useState<{ msg: string; cls: string }>({ msg: "", cls: "" });
  const [testing, setTesting] = useState(false);
  const [backup, setBackup] = useState<{ msg: string; error: boolean }>({ msg: "", error: false });
  const [notifyNote, setNotifyNote] = useState({ msg: NOTIFY_NOTE, err: false });
  const [ytdlp, setYtdlp] = useState<any>(null);
  const [restarting, setRestarting] = useState(false);
  const loadYtdlp = useRef<() => void>(() => {});

  // the notification choice: the server's setting, and once, the choice older versions kept in this browser
  const legacyChecked = useRef(false);
  useEffect(() => {
    if (!overview) return;
    setNotifyEnabled(settings.notify);
    if (!legacyChecked.current) {
      legacyChecked.current = true;
      if (takeLegacyNotify() && !settings.notify) api("/api/settings", { method: "PUT", body: { notify: true } }).catch(() => {});
    }
  }, [overview, settings.notify]);

  const syncNotifyNote = (checked: boolean) => {
    const blocked = checked && permissionDenied();
    setNotifyNote({ msg: blocked ? "This browser blocks notifications for this page — allow them in the browser’s site settings." : NOTIFY_NOTE, err: blocked });
  };

  async function loadSources() {
    try { setSources((await api("/api/cookies/sources")).browsers || []); }
    catch (err: any) { setSources([]); setCookieStatus({ msg: err.message, cls: "err" }); }
  }

  // yt-dlp: version, newest version, update progress (polled while it runs or until the newest is known)
  useEffect(() => {
    if (!isOpen) return;
    let timer: ReturnType<typeof setTimeout>;
    let live = true;
    const load = async () => {
      try {
        const y = await api("/api/ytdlp");
        if (!live) return;
        setYtdlp(y);
        if (y.update?.running || y.checking) timer = setTimeout(load, 1500);
      } catch (err: any) { if (live) setYtdlp({ error: err.message }); }
    };
    loadYtdlp.current = load;
    load();
    return () => { live = false; clearTimeout(timer); };
  }, [isOpen]);

  useImperativeHandle(ref, () => ({
    open(section) {
      setF(formFrom(overview?.settings || {}));
      setCookieStatus({ msg: "", cls: "" });
      setBackup({ msg: "", error: false });
      syncNotifyNote(!!overview?.settings?.notify);
      dlg.current?.showModal();
      setIsOpen(true);
      loadSources();
      if (section === "cookies") requestAnimationFrame(() => cookiesSection.current?.scrollIntoView({ block: "start" }));
    },
    importCookies() { cookieInput.current?.click(); },
  }));

  async function onClose() {
    setIsOpen(false);
    if (dlg.current?.returnValue !== "save") return;
    const v = formRef.current;
    try {
      const saved = await api("/api/settings", {
        method: "PUT",
        body: {
          output_dir: v.output_dir.trim(),
          concurrency: Number(v.concurrency) || 3,
          cookies_browser: v.cookies,
          cookies_profile: v.cookies ? v.profile : "",
          rate_limit_mb: Number(v.rate) || 0,
          total_rate_mb: Number(v.total_rate) || 0,
          sleep_between: Number(v.sleep) || 0,
          proxy: v.proxy.trim(),
          wait_for_network: v.waitnet,
          schedule_on: v.sched_on,
          schedule_start: v.sched_start || "01:00",
          schedule_end: v.sched_end || "07:00",
          sub_langs: v.sub_langs.trim() || "en",
          notify: v.notify,
        },
      });
      setNotifyEnabled(saved.notify);
      toast("Settings saved");
      refresh();
    } catch (err: any) { toast(err.message); }
  }

  async function testCookies() {
    if (!f.cookies) return setCookieStatus({ msg: "Choose a browser first.", cls: "warn" });
    setTesting(true);
    setCookieStatus({ msg: "Reading cookies… macOS may ask for your password to let the app read them: choose “Always Allow”.", cls: "" });
    try {
      const r = await api("/api/cookies/test", { method: "POST", body: { browser: f.cookies, profile: f.profile } });
      if (!r.ok) setCookieStatus({ msg: r.error, cls: "err" });
      else if (r.warning) setCookieStatus({ msg: `${r.youtube} YouTube cookies found. ${r.warning}.`, cls: "warn" });
      else setCookieStatus({ msg: `Works: ${r.youtube} YouTube cookies, signed in. Press Save to use them.`, cls: "ok" });
    } catch (err: any) { setCookieStatus({ msg: err.message, cls: "err" }); }
    finally { setTesting(false); }
  }

  async function onCookieFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      const c = await api("/api/cookies/file", { method: "POST", body: { text: await file.text() } });
      toast(`Cookies imported (${c.youtube} for YouTube) — trying again now`);
      refresh();
    } catch (err: any) {
      if (dlg.current?.open) setCookieStatus({ msg: err.message, cls: "err" });
      toast(err.message);
    }
  }

  async function removeCookieFile() {
    if (!confirm("Remove the imported cookies file? Downloads then use the browser’s cookies (if one is chosen).")) return;
    try { await api("/api/cookies/file", { method: "DELETE" }); refresh(); } catch (err: any) { toast(err.message); }
  }

  async function exportAll() {
    setBackup({ msg: "Exporting…", error: false });
    try {
      const res = await apiFetch("/api/export");
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Export failed (${res.status})`);
      const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "yt-downloader-export.json";
      const blob = await res.blob();
      const n = (JSON.parse(await blob.text()).jobs || []).length;
      const url = URL.createObjectURL(blob);
      const a = Object.assign(document.createElement("a"), { href: url, download: name });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      setBackup({ msg: `Saved ${name} (${n} playlist${n === 1 ? "" : "s"}, ${(blob.size / 1024).toFixed(0)} KB) to your downloads.`, error: false });
    } catch (err: any) { setBackup({ msg: err.message, error: true }); }
  }

  async function onImportFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    let data: any;
    try { data = JSON.parse(await file.text()); }
    catch { setBackup({ msg: "This file is not a YT Downloader export.", error: true }); return; }
    const withSettings = !!data?.settings && confirm(
      "Also use the settings from this file (download folder, proxy, speed limits…)?\n\nCancel keeps your current settings and imports only the library and history.");
    setBackup({ msg: "Importing…", error: false });
    try {
      const r = await api("/api/import", { method: "POST", body: { data, settings: withSettings } });
      const parts = [`${r.playlists_added} playlist${r.playlists_added === 1 ? "" : "s"} added`];
      if (r.playlists_merged) parts.push(`${r.playlists_merged} already here`);
      parts.push(`${r.videos_added} video${r.videos_added === 1 ? "" : "s"} added`);
      if (r.events_added) parts.push(`${r.events_added} history entries`);
      if (r.settings) parts.push("settings restored");
      if (r.skipped) parts.push(`${r.skipped} unreadable entries skipped`);
      let msg = `Imported: ${parts.join(", ")}.`;
      if (r.checking) msg += ` Checking the folders of ${r.checking} playlist${r.checking === 1 ? "" : "s"} for downloaded files — progress shows on each playlist.`;
      setBackup({ msg, error: false });
      if (r.settings) setF(formFrom((await api("/api/overview")).settings));   // the form still shows the old settings
      refresh();
    } catch (err: any) { setBackup({ msg: err.message, error: true }); }
  }

  async function restartNow() {
    if (!confirm("Restart YT Downloader now? Downloads in progress stop and continue from where they were after the restart.")) return;
    setRestarting(true);
    await api("/api/restart", { method: "POST" }).catch((err) => toast(err.message));
    // the page reloads itself once the new process answers with a new session token
    setTimeout(function wait() {
      fetch("/", { cache: "no-store" }).then((r) => (r.ok ? location.reload() : setTimeout(wait, 1000))).catch(() => setTimeout(wait, 1000));
    }, 1500);
  }

  // cookie browser and profile lists
  const browser = sources.find((x) => x.browser === f.cookies);
  const profiles: any[] = browser?.profiles || [];
  const cookieNote = browser && browser.status !== "ok" ? browser.note : "";

  // yt-dlp state line
  const u = ytdlp?.update;
  const newer = isNewer(ytdlp?.latest, ytdlp?.version);
  const ytState = ytdlp?.error ? ytdlp.error : u?.running ? "Updating…"
    : u?.restart ? `Version ${u.installed} is installed — restart to use it`
    : u?.ok === false ? "The update failed — see the details below"
    : newer ? `Version ${ytdlp.latest} is available` : ytdlp?.latest ? "Up to date"
    : ytdlp?.checking ? "Checking for updates…" : "Couldn't check for updates (offline?)";
  const ytCls = u?.ok === false ? "s-failed" : newer || u?.restart ? "s-pausing" : "s-done";

  return (
    <dialog id="settings-dialog" ref={dlg} onClose={onClose}>
      <form method="dialog" id="settings-form">
        <h3>Settings</h3>
        <label>Default download folder <input id="set-outdir" type="text" value={f.output_dir} onChange={(e) => set("output_dir", e.target.value)} />
          <small>Each playlist gets its own folder inside this one.</small>
        </label>
        <div className="field-row">
          <label>Parallel downloads <input id="set-concurrency" type="number" min={1} max={8} value={f.concurrency} onChange={(e) => set("concurrency", e.target.value)} /></label>
          <label>Subtitle languages <input id="set-sublangs" type="text" placeholder="en, fa" value={f.sub_langs} onChange={(e) => set("sub_langs", e.target.value)} />
            <small>Default for new downloads, e.g. “en, fa”.</small>
          </label>
        </div>
        <div className="cookies" id="cookies-section" ref={cookiesSection}>
          <div className="cookies-head">Cookies
            <small>Needed for age-restricted or members-only videos, and when YouTube asks you to confirm you’re not a bot. Use the browser you’re signed in to YouTube with.</small>
          </div>
          <div className="field-row">
            <label>Use cookies from browser
              <select id="set-cookies" value={f.cookies} onChange={(e) => { setF((x) => ({ ...x, cookies: e.target.value, profile: "" })); setCookieStatus({ msg: "", cls: "" }); }}>
                <option value="">None</option>
                {sources.map((b) => <option key={b.browser} value={b.browser}>{b.name + (STATUS_NOTE[b.status] || "")}</option>)}
                {f.cookies && !sources.some((b) => b.browser === f.cookies) && <option value={f.cookies}>{`${f.cookies} (not found on this computer)`}</option>}
              </select>
            </label>
            <label>Profile
              <select id="set-cookies-profile" value={f.profile} disabled={!f.cookies || f.cookies === "safari"}
                onChange={(e) => { set("profile", e.target.value); setCookieStatus({ msg: "", cls: "" }); }}>
                <option value="">Most recently used</option>
                {profiles.filter((p) => p.id).map((p) => <option key={p.id} value={p.id}>{p.name === p.id ? p.id : `${p.name} (${p.id})`}</option>)}
                {f.profile && !profiles.some((p) => p.id === f.profile) && <option value={f.profile}>{`${f.profile} (not found)`}</option>}
              </select>
            </label>
          </div>
          <p className="cookie-note" id="cookies-note" hidden={!cookieNote}>{cookieNote}</p>
          <div className="cookie-test">
            <button type="button" className="small" id="btn-cookies-test" disabled={testing} onClick={testCookies}>Test cookies</button>
            <span className={`cookie-status ${cookieStatus.cls}`} id="cookies-test-status" role="status">{cookieStatus.msg}</span>
          </div>
          <div className="cookie-file">
            <div>Cookies file (cookies.txt)
              <small id="cookie-file-state">{cookieFile
                ? `Imported ${fmtAgo(cookieFile.imported)}: ${cookieFile.youtube} YouTube cookies${cookieFile.signed_in ? ", signed in" : ", not signed in"}. Used instead of the browser’s cookies.`
                : "None imported. Export cookies.txt from a signed-in YouTube tab with a browser extension (e.g. “Get cookies.txt LOCALLY”) and import it here. While a file is imported, it is used instead of the browser."}</small>
            </div>
            <div className="backup-btns">
              <button type="button" className="small" id="btn-cookie-import" onClick={() => cookieInput.current?.click()}>Import cookies…</button>
              <button type="button" className={`small ghost ${cookieFile ? "" : "hidden"}`} id="btn-cookie-remove" onClick={removeCookieFile}>Remove</button>
            </div>
            <input type="file" id="cookie-file" accept=".txt,text/plain" hidden ref={cookieInput} onChange={onCookieFile} />
          </div>
        </div>
        <label>Proxy (optional) <input id="set-proxy" type="text" placeholder="socks5://127.0.0.1:1080" value={f.proxy} onChange={(e) => set("proxy", e.target.value)} />
          <small>Use this if YouTube is blocked on your network (VPN / proxy client address).</small>
        </label>
        <label className="inline"><input id="set-waitnet" type="checkbox" checked={f.waitnet} onChange={(e) => set("waitnet", e.target.checked)} />{" "}
          <span>Wait while offline, slow down on a weak connection
            <small>Downloads pause while the internet or YouTube is down and resume by themselves. Turn off only if the connection test is wrong for your network.</small></span>
        </label>
        <div className="field-row">
          <label>Speed limit per download (MB/s) <input id="set-rate" type="number" min={0} step={0.5} value={f.rate} onChange={(e) => set("rate", e.target.value)} />
            <small>0 = unlimited</small>
          </label>
          <label>Total speed limit (MB/s) <input id="set-total-rate" type="number" min={0} step={0.5} value={f.total_rate} onChange={(e) => set("total_rate", e.target.value)} />
            <small>Shared by all downloads. 0 = unlimited</small>
          </label>
        </div>
        <label>Pause between videos (s) <input id="set-sleep" type="number" min={0} step={1} value={f.sleep} onChange={(e) => set("sleep", e.target.value)} />
          <small>Helps avoid rate limiting.</small>
        </label>
        <div className="inline sched">
          <label className="inline-check"><input id="set-sched-on" type="checkbox" checked={f.sched_on} onChange={(e) => set("sched_on", e.target.checked)} /> Only start downloads between</label>
          <span className="sched-times">
            <input id="set-sched-start" type="time" aria-label="From" value={f.sched_start} disabled={!f.sched_on} onChange={(e) => set("sched_start", e.target.value)} />
            {" and "}
            <input id="set-sched-end" type="time" aria-label="Until" value={f.sched_end} disabled={!f.sched_on} onChange={(e) => set("sched_end", e.target.value)} />
          </span>
          <small>Outside these hours videos wait in the queue. A video already downloading finishes.</small>
        </div>
        <label className="inline"><input id="set-notify" type="checkbox" checked={f.notify} disabled={isOpen && !notifySupported()}
          onChange={async (e) => {
            // ask for permission on the click itself: browsers (Safari above all) refuse it later
            let on = e.target.checked;
            if (on && !(await askPermission())) on = false;
            set("notify", on);
            syncNotifyNote(on);
            if (!on && permissionDenied())
              setNotifyNote({ msg: "This browser blocks notifications for this page — allow them in the browser’s site settings, then tick the box again.", err: true });
          }} />{" "}
          <span>Desktop notifications
            <small id="notify-note" className={notifyNote.err ? "err" : ""}>{notifyNote.msg}</small></span>
        </label>
        <div className="ytdlp">
          <div><b id="ytdlp-version">{ytdlp?.version ? `yt-dlp ${ytdlp.version}` : "yt-dlp"}</b> <span className={`ytdlp-state ${ytCls}`} id="ytdlp-latest">{ytdlp ? ytState : ""}</span></div>
          <div className="ytdlp-btns">
            <button type="button" className={`small ${u?.restart ? "hidden" : ""}`} id="btn-ytdlp-update" disabled={!!u?.running}
              onClick={async () => { await api("/api/ytdlp/update", { method: "POST" }).catch((err) => toast(err.message)); loadYtdlp.current(); }}>
              {u?.running ? "Updating…" : "Update now"}
            </button>
            <button type="button" className={`small primary ${u?.restart ? "" : "hidden"}`} id="btn-restart"
              disabled={!ytdlp?.can_restart || restarting} title={ytdlp?.can_restart ? "" : "Quit and open the app again to use the new version"}
              onClick={restartNow}>{restarting ? "Restarting…" : "Restart now"}</button>
          </div>
          <pre className={`log-lines ${u?.ok === false && u.output ? "" : "hidden"}`} id="ytdlp-output">{u?.output || ""}</pre>
        </div>
        <div className="backup">
          <div>Your data
            <small>Settings, every link and playlist with its videos and their state, download folders and history, in one file. Importing adds what is missing (never deletes), continues the queue where it was, and checks the folders to see which videos are already downloaded.</small>
          </div>
          <div className="backup-btns">
            <button type="button" className="small" id="btn-export" onClick={exportAll}>Export all data</button>
            <button type="button" className="small" id="btn-import" onClick={() => importInput.current?.click()}>Import…</button>
          </div>
          <p className={`backup-status ${backup.error ? "err" : ""}`} id="backup-status" role="status" hidden={!backup.msg}>{backup.msg}</p>
          <input type="file" id="import-file" accept=".json,application/json" hidden ref={importInput} onChange={onImportFile} />
        </div>
        <div className="actions">
          <button value="cancel" className="ghost">Cancel</button>
          <button value="save" className="primary">Save</button>
        </div>
      </form>
    </dialog>
  );
});
