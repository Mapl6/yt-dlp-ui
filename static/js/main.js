// Starts the page: live updates (Server-Sent Events, with 1-second polling as a fallback), adding links,
// and wiring the parts together.

import { api, openStream } from "./api.js";
import { initDashboard, renderDashboard } from "./dashboard.js";
import { initDialogs } from "./dialogs.js";
import { initLibrary, markExpanded, openJob, renderLibrary } from "./library.js";
import { initNet, renderNet } from "./netpanel.js";
import { checkNotifications } from "./notify.js";
import { initSettings, setSettings } from "./settings.js";
import { initShortcuts } from "./shortcuts.js";
import { $, fmtSpeed, parseUrls, plural, titleFor, toast } from "./util.js";

let filter = { job: "", q: "" };
let stream = null, pollTimer = null, lastOverview = null;

function handle({ overview, activity }) {
  lastOverview = overview;
  setSettings(overview.settings);
  renderNet(overview.net);
  $("#stat-active").textContent = `${overview.active} downloading`;
  $("#stat-speed").textContent = overview.active ? fmtSpeed(overview.speed) : "—";
  document.title = titleFor(activity.active);
  renderWait(overview, activity);
  renderImport(overview.import);
  renderDashboard(activity, overview);
  renderLibrary(overview);
  checkNotifications(overview);
}

function renderWait(overview, activity) {
  const wait = $("#wait-banner");
  const text = overview.waiting ? `⏸ ${overview.waiting}` : "";
  const hold = activity.stats.hold;
  const html = text ? `<span></span>${hold ? `<button class="small" data-act="clear-hold" title="Use this once you've fixed the cause (freed disk space, changed the proxy…)">Try now</button>` : ""}` : "";
  if (wait.dataset.html !== html) { wait.innerHTML = wait.dataset.html = html; }
  if (text) wait.firstChild.textContent = text;
  wait.classList.toggle("hidden", !text);
}

function renderImport(imp) {
  const status = $("#open-status");
  if (!imp) return;
  if (imp.running) {
    status.classList.remove("error");
    status.textContent = `Adding links: ${imp.done} of ${imp.total}${imp.failed.length ? ` (${imp.failed.length} failed)` : ""}…`;
    renderImport.wasRunning = true;
  } else if (renderImport.wasRunning) {
    renderImport.wasRunning = false;
    status.classList.toggle("error", imp.failed.length > 0);
    status.textContent = `Added ${plural(imp.added, "link")}` + (imp.failed.length
      ? `; ${imp.failed.length} couldn't be read: ${imp.failed.slice(0, 3).map((f) => `${f.url} (${f.error})`).join("; ")}` : ".");
  }
}

/* ------------------------------------------------------------ live updates */

async function pollOnce() {
  const qs = new URLSearchParams(Object.entries(filter).filter(([, v]) => v));
  try {
    const [overview, activity] = await Promise.all([api("/api/overview"), api(`/api/activity?${qs}`)]);
    handle({ overview, activity });
  } catch {
    $("#stat-speed").textContent = "—";
    renderNet({ level: "offline", label: "App not running", advice: "Can't reach the downloader. Start it with ./run.sh.", bars: 0 });
  }
}

function startLive() {
  stream?.close();
  clearInterval(pollTimer);
  if (!("EventSource" in window) || startLive.polling) {
    pollTimer = setInterval(pollOnce, 1000);
    return;
  }
  stream = openStream(filter, handle, () => {
    // the stream keeps failing (an old proxy, the server went away…): poll instead, try the stream again later
    startLive.polling = true;
    pollTimer = setInterval(pollOnce, 1000);
    setTimeout(() => { startLive.polling = false; startLive(); }, 60000);
  });
}

/** Ask for a fresh update right away (after a button press), without waiting for the next push. */
const refresh = () => pollOnce();

/* ------------------------------------------------------------ adding links */

async function addLinks(urls) {
  const status = $("#open-status");
  const res = await api("/api/open-many", { method: "POST", body: { urls } });
  status.classList.remove("error");
  status.textContent = `Adding ${plural(res.total, "link")}…`;
  renderImport.wasRunning = true;
}

$("#open-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#url").value.trim();
  const status = $("#open-status");
  const btn = $("#btn-open");
  const urls = parseUrls(text);
  status.classList.remove("error");
  if (!text) return;
  if (urls.length > 1) {
    try { await addLinks(urls); $("#url").value = ""; } catch (err) { status.classList.add("error"); status.textContent = err.message; }
    return;
  }
  btn.disabled = true;
  btn.textContent = "Reading…";
  status.textContent = "Reading the playlist… large ones can take 10–30 seconds.";
  try {
    const job = await api("/api/open", { method: "POST", body: { url: urls[0] || text } });
    $("#url").value = "";
    const isNew = job.added === job.total;
    status.textContent = isNew
      ? `Saved “${job.title}” — ${plural(job.total, "video")}${job.counts.done ? `, ${job.counts.done} already downloaded` : ""}.`
      : `Updated “${job.title}” — ${job.added ? `${plural(job.added, "new video")} added` : "no new videos"}.`;
    markExpanded(job.id);
    await pollOnce();
    openJob(job.id);
  } catch (err) {
    status.classList.add("error");
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Add";
  }
});

$("#url").addEventListener("paste", (e) => {
  const text = e.clipboardData?.getData("text") || "";
  const urls = parseUrls(text);
  if (urls.length > 1) {
    e.preventDefault();
    if (confirm(`Add these ${urls.length} links to your library?`)) addLinks(urls).catch((err) => toast(err.message));
  }
});

$("#btn-import-links").addEventListener("click", () => $("#links-file").click());
$("#links-file").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  e.target.value = "";
  if (!file) return;
  const urls = parseUrls(await file.text());
  if (!urls.length) return toast(`No links found in “${file.name}”`);
  if (confirm(`Add ${plural(urls.length, "link")} from “${file.name}” to your library?`)) addLinks(urls).catch((err) => toast(err.message));
});

$("#wait-banner").addEventListener("click", async (e) => {
  if (!e.target.closest('[data-act="clear-hold"]')) return;
  await api("/api/queue/clear-hold", { method: "POST" }).catch((err) => toast(err.message));
  refresh();
});

/* ------------------------------------------------------------ start */

initNet();
initDialogs();
initLibrary({ onChange: refresh });
initDashboard({ onFilter: (f) => { filter = f; startLive(); refresh(); }, onChange: refresh });
initSettings({ onSaved: refresh });
initShortcuts({
  togglePauseAll: async () => {
    const jobs = lastOverview?.jobs || [];
    const busy = jobs.filter((j) => j.counts.queued + j.counts.downloading + j.counts.processing > 0);
    if (!busy.length) return toast("Nothing is downloading");
    const resume = busy.every((j) => j.paused);
    await api(`/api/queue/${resume ? "resume-all" : "pause-all"}`, { method: "POST" }).catch((err) => toast(err.message));
    toast(resume ? "Resumed" : "Paused every playlist");
    refresh();
  },
});
pollOnce();
startLive();
window.addEventListener("pageshow", (e) => { if (e.persisted) { refresh(); startLive(); } });
