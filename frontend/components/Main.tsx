"use client";
// Starts the page: live updates (Server-Sent Events, with 1-second polling as a fallback), the shared
// actions, the dialogs, the toast and the keyboard shortcuts.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, itemUrl, openStream } from "../lib/api";
import { checkNotifications } from "../lib/notify";
import type { Activity, Overview } from "../lib/types";
import { store, titleFor } from "../lib/util";
import { AppContext, type AppApi } from "./AppContext";
import { ChartTip } from "./charts";
import { DASH_TABS, Dashboard } from "./Dashboard";
import { KeysDialog, LogDialog, type LogDialogApi, RemoveDialog, type RemoveDialogApi } from "./dialogs";
import { Library, type LibraryApi } from "./Library";
import { Menu, useMenu } from "./Menu";
import { NetPanel, OFFLINE } from "./NetPanel";
import { SettingsDialog, type SettingsApi } from "./SettingsDialog";
import { OpenForm, TopBar, WaitBanner } from "./Top";

const typing = (el: Element | null) =>
  !!el && ((el as HTMLElement).isContentEditable || /^(input|textarea|select)$/i.test(el.tagName));

export default function Main() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [activity, setActivity] = useState<Activity | null>(null);
  const [offline, setOffline] = useState(false);
  const [filter, setFilterState] = useState({ job: "", q: "" });
  const filterRef = useRef(filter);
  filterRef.current = filter;
  const [dashTab, setDashTabState] = useState("active");
  useEffect(() => { const t = store.get("dashTab", "active"); if (DASH_TABS.includes(t)) setDashTabState(t); }, []);
  const setDashTab = useCallback((t: string) => { if (DASH_TABS.includes(t)) { setDashTabState(t); store.set("dashTab", t); } }, []);

  const [toastMsg, setToastMsg] = useState<{ msg: string; n: number } | null>(null);
  const toast = useCallback((msg: string) => setToastMsg((t) => ({ msg, n: (t?.n || 0) + 1 })), []);
  useEffect(() => {
    if (!toastMsg) return;
    const t = setTimeout(() => setToastMsg(null), 3500);
    return () => clearTimeout(t);
  }, [toastMsg]);

  const handle = useCallback(({ overview, activity }: { overview: Overview; activity: Activity }) => {
    setOverview(overview);
    setActivity(activity);
    setOffline(false);
    checkNotifications(overview);
  }, []);

  const pollOnce = useCallback(async () => {
    const qs = new URLSearchParams(Object.entries(filterRef.current).filter(([, v]) => v));
    try {
      const [ov, act] = await Promise.all([api("/api/overview"), api(`/api/activity?${qs}`)]);
      handle({ overview: ov, activity: act });
    } catch {
      setOffline(true);
    }
  }, [handle]);

  // live updates: the stream, or polling while the stream keeps failing (an old proxy, the server went away…)
  const [liveGen, setLiveGen] = useState(0);
  useEffect(() => {
    let poll: ReturnType<typeof setInterval> | undefined, retry: ReturnType<typeof setTimeout> | undefined;
    let stream: { close: () => void } | null = null;
    if (!("EventSource" in window)) poll = setInterval(pollOnce, 1000);
    else stream = openStream(filter, handle, () => {
      poll = setInterval(pollOnce, 1000);
      retry = setTimeout(() => setLiveGen((g) => g + 1), 60000);   // try the stream again later
    });
    return () => { stream?.close(); clearInterval(poll); clearTimeout(retry); };
  }, [filter, liveGen, handle, pollOnce]);
  useEffect(() => { pollOnce(); }, [pollOnce]);
  useEffect(() => {
    const show = (e: PageTransitionEvent) => { if (e.persisted) { pollOnce(); setLiveGen((g) => g + 1); } };
    window.addEventListener("pageshow", show);
    return () => window.removeEventListener("pageshow", show);
  }, [pollOnce]);

  useEffect(() => { document.title = titleFor(activity?.active); }, [activity]);

  const settingsRef = useRef<SettingsApi>(null);
  const logRef = useRef<LogDialogApi>(null);
  const removeRef = useRef<RemoveDialogApi>(null);
  const keysRef = useRef<HTMLDialogElement>(null);
  const libraryRef = useRef<LibraryApi>(null);
  const menu = useMenu();

  const showLog = useCallback(async (job: string, id: string) => {
    logRef.current?.show(await api(itemUrl(job, id, "log")));
  }, []);

  const itemAction = useCallback(async (job: string, id: string, action: string, title = "") => {
    if (action === "log") return showLog(job, id);
    if (action === "top" || action === "bottom") {
      await api("/api/queue/move", { method: "POST", body: { job, id, to: action } });
      toast(action === "top" ? "Moved to the top of the queue" : "Moved to the bottom of the queue");
      return pollOnce();
    }
    if (action === "delete-file" && !confirm(`Delete the downloaded file of “${title}”?\nIt goes back to “Not downloaded”.`)) return;
    await api(itemUrl(job, id, action), { method: "POST" });
    if (action === "delete-file") toast("File deleted");
    if (action === "mark-done") toast("Marked as downloaded");
    return pollOnce();
  }, [showLog, toast, pollOnce]);

  const app: AppApi = useMemo(() => ({
    overview, activity, toast, itemAction, showLog,
    refresh: pollOnce,
    setFilter: (f) => setFilterState(f),
    openSettings: (section) => settingsRef.current?.open(section),
    importCookies: () => settingsRef.current?.importCookies(),
    confirmRemove: (title) => removeRef.current?.ask(title) ?? Promise.resolve(null),
    openMenu: menu.openMenu,
    openJob: (id, itemId) => libraryRef.current?.openJob(id, itemId),
    markExpanded: (id) => libraryRef.current?.markExpanded(id),
    setDashTab,
  }), [overview, activity, toast, itemAction, showLog, pollOnce, menu.openMenu, setDashTab]);

  // keyboard shortcuts; they never fire while typing in a field or with a dialog open
  const appRef = useRef(app);
  appRef.current = app;
  useEffect(() => {
    const key = async (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || typing(e.target as Element) || document.querySelector("dialog[open]")) return;
      const k = e.key, a = appRef.current;
      if (k === "/") { e.preventDefault(); document.getElementById("lib-filter")?.focus(); }
      else if (k === "u") { e.preventDefault(); document.getElementById("url")?.focus(); }
      else if (k === "s") { e.preventDefault(); a.openSettings(); }
      else if (k === "?") { e.preventDefault(); keysRef.current?.showModal(); }
      else if (/^[1-4]$/.test(k)) {
        e.preventDefault();
        a.setDashTab(DASH_TABS[Number(k) - 1]);
        document.getElementById("dash")?.scrollIntoView({ behavior: "smooth", block: "start" });
      } else if (k === "p") {
        e.preventDefault();
        const busy = (a.overview?.jobs || []).filter((j) => j.counts.queued + j.counts.downloading + j.counts.processing > 0);
        if (!busy.length) return a.toast("Nothing is downloading");
        const resume = busy.every((j) => j.paused);
        try {
          await api(`/api/queue/${resume ? "resume-all" : "pause-all"}`, { method: "POST" });
          a.toast(resume ? "Resumed" : "Paused every playlist");
        } catch (err: any) { a.toast(err.message); }
        a.refresh();
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, []);

  const net = offline ? OFFLINE : overview?.net;
  return (
    <AppContext.Provider value={app}>
      <TopBar net={net} onShortcuts={() => keysRef.current?.showModal()} />
      <main>
        <OpenForm />
        <WaitBanner />
        <NetPanel net={net} />
        <Dashboard tab={dashTab} setTab={setDashTab} />
        <Library ref={libraryRef} />
      </main>
      <SettingsDialog ref={settingsRef} />
      <LogDialog ref={logRef} />
      <RemoveDialog ref={removeRef} />
      <KeysDialog ref={keysRef} />
      <div id="toast" className={`toast ${toastMsg ? "" : "hidden"}`} role="status">{toastMsg?.msg}</div>
      <Menu open={menu.open} close={menu.close} />
      <ChartTip />
    </AppContext.Provider>
  );
}
