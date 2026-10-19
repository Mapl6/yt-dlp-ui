// Desktop notifications (opt-in): a playlist finished downloading, or a video failed.
// In the Mac app the native bridge is used; in a browser, the Notification API.
// The choice is a setting on the server (settings.notify), so it survives the Mac app's private browser
// storage and is the same in every browser; the browser's own permission is asked for when the box is ticked.
import { api } from "./api";
import type { Overview } from "./types";
import { store } from "./util";

declare global { interface Window { pywebview?: { api?: { notify?: (t: string, b: string) => void } } } }

let lastEvent: number | null = null;          // newest event id already seen
const jobStatus = new Map<string, string>();  // playlist id -> status at the previous update
let enabled = false;

export const setNotifyEnabled = (on: unknown) => { enabled = !!on; };

/** Earlier versions kept the choice in this browser only: returns true once if it should move to the server. */
export function takeLegacyNotify(): boolean {
  const old = store.get<boolean | null>("notify", null);
  if (old === null) return false;
  try { localStorage.removeItem("notify"); } catch {}
  return old === true;
}

/** Ask the browser for permission. Must run from a click: Safari refuses the request otherwise. */
export async function askPermission(): Promise<boolean> {
  if (window.pywebview?.api?.notify || !("Notification" in window)) return true;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  return (await Notification.requestPermission()) === "granted";
}

export const permissionDenied = () =>
  !window.pywebview?.api?.notify && "Notification" in window && Notification.permission === "denied";

export const notifySupported = () => !!window.pywebview?.api?.notify || "Notification" in window;

function send(title: string, body: string) {
  if (!enabled) return;
  if (window.pywebview?.api?.notify) { window.pywebview.api.notify(title, body); return; }
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification(title, { body, tag: `${title}:${body}`.slice(0, 120) }); } catch {}
  }
}

/** Called on every update with the overview. */
export async function checkNotifications(overview: Overview) {
  for (const j of overview.jobs) {
    const before = jobStatus.get(j.id);
    if (before && before !== "complete" && ["downloading", "queued", "pausing"].includes(before) && j.status === "complete")
      send("Playlist downloaded", `“${j.title}” — all ${j.total - j.counts.removed} videos are downloaded.`);
    jobStatus.set(j.id, j.status);
  }
  const last = overview.last_event || 0;
  if (lastEvent === null) { lastEvent = last; return; }      // first update: nothing is new
  if (last <= lastEvent) return;
  const since = lastEvent;
  lastEvent = last;
  if (!enabled) return;
  try {
    const { rows } = await api(`/api/history?since_id=${since}&limit=20`);
    const failed = rows.filter((r: any) => r.status === "failed");
    if (failed.length === 1) send("Download failed", `${failed[0].title}: ${failed[0].error || "unknown error"}`);
    else if (failed.length > 1) send(`${failed.length} downloads failed`, failed.map((r: any) => r.title).slice(0, 3).join(", "));
  } catch {}
}
