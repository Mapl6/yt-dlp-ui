// Desktop notifications (opt-in): a playlist finished downloading, or a video failed.
// In the Mac app the native bridge is used; in a browser, the Notification API.

import { api } from "./api.js";
import { store } from "./util.js";

let lastEvent = null;              // newest event id already seen
const jobStatus = new Map();       // playlist id -> status at the previous update

export const notifyEnabled = () => store.get("notify", false) === true;

export async function setNotify(on) {
  if (on && !window.pywebview?.api?.notify && "Notification" in window && Notification.permission !== "granted") {
    const answer = await Notification.requestPermission();
    if (answer !== "granted") { store.set("notify", false); return false; }
  }
  store.set("notify", !!on);
  return !!on;
}

export function notifySupported() {
  return !!window.pywebview?.api?.notify || "Notification" in window;
}

function send(title, body) {
  if (!notifyEnabled()) return;
  if (window.pywebview?.api?.notify) { window.pywebview.api.notify(title, body); return; }
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification(title, { body, tag: `${title}:${body}`.slice(0, 120) }); } catch {}
  }
}

/** Called on every update with the overview. */
export async function checkNotifications(overview) {
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
  if (!notifyEnabled()) return;
  try {
    const { rows } = await api(`/api/history?since_id=${since}&limit=20`);
    const failed = rows.filter((r) => r.status === "failed");
    if (failed.length === 1) send("Download failed", `${failed[0].title}: ${failed[0].error || "unknown error"}`);
    else if (failed.length > 1) send(`${failed.length} downloads failed`, failed.map((r) => r.title).slice(0, 3).join(", "));
  } catch {}
}
