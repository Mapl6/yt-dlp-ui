// "Your playlists": one card per saved playlist. The card body (options, selection, video list) is built
// when the card is first opened. Rows are drawn by a virtual list and updated from items?since=<rev>.

import { api, itemUrl } from "./api.js";
import { confirmRemove, showLog } from "./dialogs.js";
import { DOWNLOADED, IN_QUEUE, SELECTABLE, TABS, UNAVAILABLE, visibleItems } from "./filters.js";
import { openMenu } from "./menu.js";
import { $, $$, AUDIO, debounce, esc, fmtBytes, fmtSpeed, fmtTime, parseRange, plural, store, toast } from "./util.js";
import { VList } from "./vlist.js";

const JOB_LABEL = {
  saved: "Saved", queued: "Queued", downloading: "Downloading", pausing: "Pausing…", paused: "Paused", complete: "All downloaded",
};
export const ITEM_LABEL = {
  new: "Not downloaded", queued: "Queued", downloading: "Downloading", processing: "Processing",
  done: "Downloaded", skipped: "Downloaded", failed: "Failed",
};
const SYNC_LABEL = { 6: "every 6 hours", 12: "every 12 hours", 24: "daily", 168: "weekly" };
const ROW_HEIGHT = 60;
export const REVEAL_LABEL = /Mac/i.test(navigator.platform) ? "Show in Finder" : "Show in folder";

const ICON = {
  pause: `<svg viewBox="0 0 24 24"><path d="M9 6v12M15 6v12"/></svg>`,
  play: `<svg viewBox="0 0 24 24"><path d="M8 5.5v13l10-6.5z"/></svg>`,
  more: `<svg viewBox="0 0 24 24"><circle cx="5.5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18.5" cy="12" r="1.2"/></svg>`,
  x: `<svg viewBox="0 0 24 24"><path d="m7 7 10 10M17 7 7 17"/></svg>`,
};

const expanded = new Set(store.get("expanded", []));
const saveExpanded = () => store.set("expanded", [...expanded]);
export const jobViews = new Map();             // playlist id -> view
let settings = {};
let update = () => {};                         // ask main.js for a fresh update right away

export function initLibrary({ onChange }) {
  update = onChange;
  $("#lib-filter").addEventListener("input", () => {
    const q = $("#lib-filter").value.trim().toLowerCase();
    for (const v of jobViews.values()) v.el.classList.toggle("hidden", !!q && !v.job.title.toLowerCase().includes(q));
  });
}

/** Open a playlist's card (if needed) and scroll to it; optionally highlight one video. */
export async function openJob(id, itemId = null) {
  const view = jobViews.get(id);
  if (!view) return;
  if (!expanded.has(id)) { expanded.add(id); saveExpanded(); await renderView(view); }
  view.el.scrollIntoView({ behavior: "smooth", block: "start" });
  if (itemId && view.vlist) { await syncItems(view); view.vlist.reveal(itemId); }
}

export function markExpanded(id) { expanded.add(id); saveExpanded(); }

/* ------------------------------------------------------------ cards */

function ensureView(j) {
  let view = jobViews.get(j.id);
  if (view) return view;
  const el = document.createElement("div");
  el.className = "job";
  el.innerHTML = `<div class="job-head"><img alt="" loading="lazy"><div class="job-main"></div><div class="job-btns"></div></div>`;
  const img = $("img", el);
  img.onerror = () => { img.style.visibility = "hidden"; };
  if (j.thumbnail) img.src = j.thumbnail; else img.style.visibility = "hidden";
  view = {
    id: j.id, el, job: j, rev: 0, items: new Map(), list: [], tab: "all", text: "", dur: "any", sort: "index",
    selected: new Set(), body: null, vlist: null, syncing: false,
  };
  el.addEventListener("click", (e) => onClick(e, view));
  jobViews.set(j.id, view);
  return view;
}

function headButtons(j) {
  const c = j.counts;
  const busy = c.queued + c.downloading + c.processing > 0;
  const b = [];
  if (busy) {
    b.push(j.status === "paused" || j.status === "pausing" ? ["resume", "Resume", "primary"] : ["pause", "Pause"]);
    b.push(["stop", "Stop"]);
  }
  if (c.failed) b.push(["retry", `Retry failed (${c.failed})`]);
  if (j.kind === "playlist") b.push(["refresh", "Check for new videos"]);
  b.push(["open", "Open folder"]);
  b.push(["remove", "Remove"]);
  const open = expanded.has(j.id);
  return b.map(([a, l, cls = ""]) => `<button class="small ${cls}" data-act="${a}">${l}</button>`).join("") +
    `<button class="small ghost chev" data-act="toggle" aria-expanded="${open}" title="${open ? "Close" : "Open"}" aria-label="${open ? "Close" : "Open"}">` +
    `<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button>`;
}

function renderHead(view) {
  const j = view.job, c = j.counts;
  const parts = [`<span class="badge s-${j.status}">${JOB_LABEL[j.status] || j.status}</span>`,
    `<span><b>${c.done}</b> / ${j.total - c.removed} downloaded (${Math.floor(j.percent)}%)</span>`];
  if (c.downloading + c.processing) parts.push(`<span class="s-downloading">${c.downloading + c.processing} downloading</span>`);
  if (c.queued) parts.push(`<span>${c.queued} queued</span>`);
  if (c.failed) parts.push(`<span class="s-failed">${c.failed} failed</span>`);
  if (c.removed) parts.push(`<span class="muted" title="Private, deleted or removed from the playlist">${c.removed} unavailable</span>`);
  if (j.speed) parts.push(`<span>${fmtSpeed(j.speed)}</span>`);
  if (j.check?.running) parts.push(`<span class="s-downloading">Checking files ${j.check.done} / ${j.check.total}…</span>`);
  if (j.sync?.every) parts.push(`<span class="muted" title="${esc(j.sync.error || "")}">↻ ${SYNC_LABEL[j.sync.every] || ""}${j.sync.auto ? ", auto-download" : ""}${j.sync.error ? " (last check failed)" : ""}</span>`);
  const info = `
    <div class="job-title" title="${esc(j.title)}">${esc(j.title)}</div>
    <div class="bar ${j.status === "complete" ? "ok" : ""}"><div style="width:${j.percent}%"></div></div>
    <div class="job-sub">${parts.join("")}</div>`;
  const btns = headButtons(j);
  // Only touch the DOM when something changed, so clicks are never lost mid-update.
  if (view.infoHtml !== info) $(".job-main", view.el).innerHTML = view.infoHtml = info;
  if (view.btnHtml !== btns) $(".job-btns", view.el).innerHTML = view.btnHtml = btns;
}

function buildBody(view) {
  const j = view.job;
  const body = $("#tpl-body").content.firstElementChild.cloneNode(true);
  const opt = (k) => $(`[data-opt="${k}"]`, body);
  const heights = j.heights?.length ? j.heights : [2160, 1440, 1080, 720, 480, 360, 240, 144];
  opt("quality").innerHTML =
    `<optgroup label="Video"><option value="best">Best available</option>` +
    heights.map((h) => `<option value="${h}">${h >= 2160 ? "4K (2160p)" : h >= 1440 ? "2K (1440p)" : h + "p"}</option>`).join("") +
    `</optgroup><optgroup label="Audio only"><option value="mp3">MP3</option><option value="m4a">M4A (AAC)</option><option value="opus">Opus</option></optgroup>`;
  const o = j.options || {};
  opt("quality").value = o.quality && $(`option[value="${o.quality}"]`, opt("quality")) ? o.quality : heights.includes(1080) ? "1080" : "best";
  opt("container").value = o.container || "mp4";
  opt("output_dir").value = j.outdir;
  for (const k of ["numbering", "thumbnail", "metadata", "subtitles", "auto_subs"]) if (k in o) opt(k).checked = !!o[k];
  opt("sub_langs").value = o.sub_langs || settings.sub_langs || "en";
  opt("sponsorblock").value = o.sponsorblock || "";
  if (j.kind !== "playlist") $$("[data-playlist-only]", body).forEach((e) => e.classList.add("hidden"));
  const syncOptions = () => {
    const audio = AUDIO.includes(opt("quality").value);
    $$("[data-video-only]", body).forEach((e) => e.classList.toggle("hidden", audio));
    $$("[data-subs-only]", body).forEach((e) => e.classList.toggle("hidden", audio || !opt("subtitles").checked));
  };
  body.addEventListener("change", (e) => {
    if (e.target.matches("[data-opt]")) { syncOptions(); estimate(view); syncSave(view); }
  });
  body.addEventListener("input", (e) => {
    if (e.target.matches('[data-opt="sec_start"], [data-opt="sec_end"]')) estimate(view);
    if (e.target.matches("[data-opt]")) syncSave(view);
  });
  syncOptions();

  // automatic checks for new videos (saved right away)
  const every = $('[data-role="sync-every"]', body), auto = $('[data-role="sync-auto"]', body);
  every.value = String(j.sync?.every || 0);
  auto.checked = !!j.sync?.auto;
  auto.disabled = !Number(every.value);
  const saveSync = async () => {
    auto.disabled = !Number(every.value);
    try {
      await api(`/api/jobs/${view.id}/sync`, { method: "PUT", body: { every_hours: Number(every.value), auto_download: auto.checked } });
      toast(Number(every.value) ? `Checking for new videos ${SYNC_LABEL[every.value]}${auto.checked ? " and downloading them" : ""}` : "Automatic checks turned off");
      update();
    } catch (err) { toast(err.message); }
  };
  every.addEventListener("change", saveSync);
  auto.addEventListener("change", saveSync);

  $('[data-role="tabs"]', body).innerHTML = TABS.map(([k, label]) =>
    `<button data-tab="${k}" class="${k === view.tab ? "on" : ""}">${label} <span class="cnt"></span></button>`).join("");
  $('[data-role="sort"]', body).addEventListener("change", (e) => { view.sort = e.target.value; relist(view); });
  $('[data-role="dur"]', body).addEventListener("change", (e) => { view.dur = e.target.value; relist(view); });

  const box = $('[data-role="items"]', body);
  view.vlist = new VList(box, {
    rowHeight: ROW_HEIGHT, tag: "label", className: "item",
    key: (it) => it.id, html: (it) => rowHtml(view, it),
    decorate: (el, it) => el.classList.toggle("picked", view.selected.has(it.id)),
  });
  box.addEventListener("change", (e) => {
    const id = e.target.dataset.sel;
    if (!id) return;
    e.target.checked ? view.selected.add(id) : view.selected.delete(id);
    view.vlist.draw();
    updateSelection(view);
  });
  $('[data-role="sel-all"]', body).addEventListener("change", (e) => {
    for (const it of view.list) if (SELECTABLE(it)) e.target.checked ? view.selected.add(it.id) : view.selected.delete(it.id);
    view.vlist.draw();
    updateSelection(view);
  });
  $('[data-role="filter"]', body).addEventListener("input", (e) => { view.text = e.target.value; relist(view); });
  view.el.appendChild(body);
  view.body = body;
  view.savedOpts = optionsKey(view);
  syncSave(view);
}

/** The options as the playlist stores them (a part of a video is only ever for one download). */
const optionsKey = (view) => {
  const { section, ...o } = collectOptions(view);
  return JSON.stringify(o);
};

/** The Save button is enabled while the options differ from what the playlist has stored. */
function syncSave(view) {
  const changed = optionsKey(view) !== view.savedOpts;
  $('[data-role="save-opts"]', view.body).disabled = !changed;
  $('[data-role="opts-state"]', view.body).textContent = changed ? "Not saved — press Save options, or Download to use them" : "";
}

async function saveOptions(view) {
  const { section, ...options } = collectOptions(view);
  try {
    await api(`/api/jobs/${view.id}/options`, { method: "PUT", body: { options } });
    view.savedOpts = optionsKey(view);
    toast("Options saved — Download and automatic downloads use them");
  } finally {
    syncSave(view);
  }
}

function relist(view) {
  view.list = visibleItems([...view.items.values()], view);
  for (const id of view.selected) { const it = view.items.get(id); if (!it || !SELECTABLE(it)) view.selected.delete(id); }
  view.vlist?.setItems(view.list);
  const empty = $('[data-role="empty"]', view.body);
  empty.classList.toggle("hidden", view.list.length > 0 || view.items.size === 0);
  updateSelection(view);
}

function updateSelection(view) {
  const n = view.selected.size;
  const visible = view.list.filter(SELECTABLE);
  const picked = visible.filter((it) => view.selected.has(it.id)).length;
  const all = $('[data-role="sel-all"]', view.body);
  all.checked = visible.length > 0 && picked === visible.length;
  all.indeterminate = picked > 0 && picked < visible.length;
  $('[data-role="sel-count"]', view.body).textContent = `${n} selected`;
  const dl = $('[data-role="download"]', view.body);
  dl.disabled = n === 0;
  dl.textContent = n ? `Download ${plural(n, "video")}` : "Download selected";
  if (view.lastSelCount !== n) { view.lastSelCount = n; estimate(view); }
}

function collectOptions(view) {
  const opt = (k) => $(`[data-opt="${k}"]`, view.body);
  const options = {
    quality: opt("quality").value,
    container: opt("container").value,
    output_dir: opt("output_dir").value.trim(),
    numbering: opt("numbering").checked,
    thumbnail: opt("thumbnail").checked,
    metadata: opt("metadata").checked,
    subtitles: opt("subtitles").checked,
    sub_langs: opt("sub_langs").value.trim(),
    auto_subs: opt("auto_subs").checked,
    sponsorblock: opt("sponsorblock").value,
    skip_existing: true,
  };
  const start = opt("sec_start").value.trim(), end = opt("sec_end").value.trim();
  if (start || end) options.section = { start, end };
  return options;
}

// Estimated size of the selection, next to the Download button.
const estimate = (view) => {
  if (!view.body) return;
  if (!view.estimateFn) {
    view.estimateFn = debounce(async () => {
      const out = $('[data-role="estimate"]', view.body);
      if (!view.selected.size) { out.textContent = ""; out.className = "estimate"; return; }
      try {
        const e = await api(`/api/jobs/${view.id}/estimate`, { method: "POST", body: { ids: [...view.selected], options: collectOptions(view) } });
        const size = e.bytes ? `≈ ${fmtBytes(e.bytes)}${e.unknown ? ` + ${e.unknown} of unknown length` : ""}` : "Size unknown";
        out.textContent = e.problem ? `${size} · ${e.problem}` : e.free != null ? `${size} · ${fmtBytes(e.free)} free` : size;
        out.className = `estimate ${e.low || e.problem ? "warn" : ""}`;
        out.title = e.low ? "Not enough free space: the queue pauses when less than 1 GB is left" : "Estimated from each video's length and the chosen quality";
      } catch (err) { out.textContent = err.message; out.className = "estimate warn"; }
    }, 400);
  }
  view.estimateFn();
};

function checkSummary(c) {
  const bits = [`${c.ok} OK`];
  if (c.found) bits.push(`${c.found} found in the folder`);
  if (c.damaged) bits.push(`${c.damaged} damaged`);
  if (c.missing) bits.push(`${c.missing} missing`);
  return bits.join(", ");
}

function updateTools(view) {
  const c = view.job.check;
  const btn = $('[data-role="check"]', view.body), status = $('[data-role="tools-status"]', view.body);
  btn.textContent = c?.running ? `Stop checking (${c.done} / ${c.total})` : "Check files";
  if (c?.running) status.textContent = "Checking every file — you can keep using the app.";
  else if (c?.finished) status.textContent = `Last check ${new Date(c.finished * 1000).toLocaleString()}: ${checkSummary(c)}`;
  else status.textContent = "";
  if (view.checking && !c?.running && c?.finished) {
    toast(c.damaged || c.missing ? `Check done: ${checkSummary(c)}. Broken files are in the Failed tab — press Retry to download them again.`
      : `Check done: all ${c.ok + c.found} files are healthy${c.found ? ` (${c.found} found in the folder)` : ""}.`);
  }
  view.checking = !!c?.running;
}

function updateTabCounts(view) {
  const c = view.job.counts;
  const n = { all: view.job.total, new: c.new, queue: c.queued + c.downloading + c.processing, done: c.done, failed: c.failed, removed: c.removed };
  for (const b of $$("[data-tab]", view.body)) {
    $(".cnt", b).textContent = n[b.dataset.tab] ?? "";
    if (b.dataset.tab === "removed") b.classList.toggle("hidden", !c.removed);
  }
}

/* ------------------------------------------------------------ rows */

const partLabel = (s) => (s ? `part ${fmtTime(s.start)}–${s.end != null ? fmtTime(s.end) : "end"}` : "");

function rowHtml(view, it) {
  const id = esc(it.id);
  let meta = "", metaCls = "meta", full = "";
  if (it.status === "downloading") {
    meta = `${(it.progress || 0).toFixed(1)}% · ${fmtBytes(it.downloaded)} / ${fmtBytes(it.total)} · ${fmtSpeed(it.speed)}${it.eta != null ? ` · ${fmtTime(it.eta)} left` : ""}`;
  } else if (it.status === "processing") {
    meta = `${esc(it.step || "Processing")}…`;
  } else if (DOWNLOADED(it)) {
    meta = it.filepath ? esc(it.filepath.split(/[\\/]/).pop()) : "Already in the folder";
    if (it.section) meta += ` · ${partLabel(it.section)}`;
    if (it.health === "ok") meta += ` · <span class="s-done">✓ verified</span>`;
  } else if (it.status === "queued" && it.held) {
    meta = "Paused — press ▶ to continue";
  }
  if (it.removed && !UNAVAILABLE(it)) meta += `${meta ? " · " : ""}no longer in the playlist`;
  if (it.error && (it.status === "failed" || it.status === "queued")) { metaCls = "meta err"; meta = esc(it.error); full = it.error; }

  let btns = "";
  if (!UNAVAILABLE(it)) {
    if (it.status === "failed") btns += `<button class="small" data-item="retry" data-id="${id}">Retry</button>`;
    if (IN_QUEUE(it)) {
      btns += it.held
        ? `<button class="small ghost icon" data-item="unhold" data-id="${id}" title="Continue" aria-label="Continue">${ICON.play}</button>`
        : `<button class="small ghost icon" data-item="hold" data-id="${id}" title="Pause this video" aria-label="Pause this video">${ICON.pause}</button>`;
      btns += `<button class="small ghost icon" data-item="cancel" data-id="${id}" title="Remove from queue" aria-label="Remove from queue">${ICON.x}</button>`;
    }
    if (DOWNLOADED(it) && it.filepath) btns += `<button class="small ghost icon" data-item="play" data-id="${id}" title="Play" aria-label="Play">${ICON.play}</button>`;
    btns += `<button class="small ghost icon" data-menu="${id}" title="More" aria-label="More actions" aria-haspopup="menu">${ICON.more}</button>`;
  }
  const badge = UNAVAILABLE(it) ? `<span class="badge s-removed" title="Private, deleted or removed from the playlist">Unavailable</span>`
    : it.held && it.status === "queued" ? `<span class="badge s-paused">Paused</span>`
    : `<span class="badge s-${it.status}">${ITEM_LABEL[it.status] || it.status}</span>`;
  const sel = SELECTABLE(it);
  return `<input type="checkbox" data-sel="${id}" ${sel ? "" : "disabled"} ${view.selected.has(it.id) ? "checked" : ""} aria-label="Select">
    <span class="num">${esc(it.index)}</span>
    <div class="mid">
      <div class="t" title="${esc(it.title)}">${esc(it.title)}</div>
      ${meta ? `<div class="${metaCls}" ${full ? `title="${esc(full)}"` : ""}>${meta}</div>` : ""}
      ${it.status === "downloading" ? `<div class="bar small"><div style="width:${it.progress || 0}%"></div></div>` : ""}
    </div>
    <span class="dur">${fmtTime(it.duration)}</span>
    <div class="right">${badge}${btns}</div>`;
}

function menuFor(it) {
  const m = [];
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

export async function itemAction(job, id, action, title = "") {
  if (action === "log") return showLog(job, id);
  if (action === "top" || action === "bottom") {
    await api("/api/queue/move", { method: "POST", body: { job, id, to: action } });
    toast(action === "top" ? "Moved to the top of the queue" : "Moved to the bottom of the queue");
    return update();
  }
  if (action === "delete-file" && !confirm(`Delete the downloaded file of “${title}”?\nIt goes back to “Not downloaded”.`)) return;
  await api(itemUrl(job, id, action), { method: "POST" });
  if (action === "delete-file") toast("File deleted");
  if (action === "mark-done") toast("Marked as downloaded");
  update();
}

async function syncItems(view) {
  if (view.syncing) return;
  view.syncing = true;
  try {
    const data = await api(`/api/jobs/${view.id}/items?since=${view.rev}`);
    for (const it of data.items) view.items.set(it.id, it);
    view.rev = data.rev;
    relist(view);
  } finally {
    view.syncing = false;
  }
}

function selectRange(view, spec) {
  const items = [...view.items.values()];
  const res = parseRange(spec, Math.max(0, ...items.map((i) => i.index)));
  if (res.error) return toast(`Can't read "${res.error}". Use e.g. 1-50, 80, 100-`);
  let n = 0;
  for (const it of items) if (res.picked.has(it.index) && SELECTABLE(it)) { view.selected.add(it.id); n++; }
  view.vlist.draw();
  updateSelection(view);
  toast(n ? `Selected ${plural(n, "video")} not yet downloaded` : "Everything in that range is already downloaded or queued");
}

async function startDownload(view) {
  const res = await api(`/api/jobs/${view.id}/download`, { method: "POST", body: { ids: [...view.selected], options: collectOptions(view) } });
  view.selected.clear();
  view.vlist.draw();
  updateSelection(view);
  view.savedOpts = optionsKey(view);              // Download stores the options too
  syncSave(view);
  toast(`Queued ${plural(res.queued, "video")}`);
}

async function onClick(e, view) {
  const btn = e.target.closest("button");
  if (!btn) {
    // A click anywhere on the card header (not a button, not selecting text) opens or closes it.
    if (e.target.closest(".job-head") && !String(window.getSelection())) toggleOpen(view);
    return;
  }
  e.preventDefault();                          // buttons inside <label> rows must not toggle the checkbox
  try {
    if (btn.dataset.tab) {
      view.tab = btn.dataset.tab;
      $$("[data-tab]", view.body).forEach((b) => b.classList.toggle("on", b === btn));
      return relist(view);
    }
    const role = btn.dataset.role;
    if (role === "sel-missing") {
      for (const it of view.items.values()) if (SELECTABLE(it)) view.selected.add(it.id);
      view.vlist.draw();
      return updateSelection(view);
    }
    if (role === "check") {
      const running = view.job.check?.running;
      await api(`/api/jobs/${view.id}/check${running ? "/stop" : ""}`, { method: "POST" });
      if (!running) view.checking = true;
      return update();
    }
    if (role === "cleanup") {
      const found = await api(`/api/jobs/${view.id}/cleanup`);
      if (!found.files) return toast("No leftover files — the folder is clean.");
      const list = found.names.slice(0, 8).join("\n") + (found.files > 8 ? `\n…and ${found.files - 8} more` : "");
      if (!confirm(`Delete ${plural(found.files, "leftover file")} (${fmtBytes(found.bytes)})?\n\n${list}\n\nVideos in the queue are not touched.`)) return;
      const res = await api(`/api/jobs/${view.id}/cleanup`, { method: "POST" });
      return toast(`Deleted ${plural(res.files, "leftover file")}, freed ${fmtBytes(res.bytes)}`);
    }
    if (role === "m3u") {
      const res = await api(`/api/jobs/${view.id}/m3u`, { method: "POST" });
      return toast(`Saved “${res.name}” with ${plural(res.count, "video")} in the playlist folder`);
    }
    if (role === "apply-range") return selectRange(view, $('[data-role="range"]', view.body).value);
    if (role === "download") { btn.disabled = true; await startDownload(view); return update(); }
    if (role === "save-opts") { btn.disabled = true; await saveOptions(view); return update(); }

    if (btn.dataset.menu) {
      const it = view.items.get(btn.dataset.menu);
      if (it) openMenu(btn, menuFor(it), (action) => itemAction(view.id, it.id, action, it.title).catch((err) => toast(err.message)));
      return;
    }
    if (btn.dataset.item) {
      const it = view.items.get(btn.dataset.id);
      return itemAction(view.id, btn.dataset.id, btn.dataset.item, it?.title);
    }

    const act = btn.dataset.act;
    if (act === "toggle") return toggleOpen(view);
    if (act === "stop" && !confirm("Stop downloading? Videos still in the queue go back to “Not downloaded”.")) return;
    if (act === "remove") {
      const answer = await confirmRemove(view.job.title);
      if (!answer) return;
      await api(`/api/jobs/${view.id}/remove`, { method: "POST", body: { delete_files: answer.deleteFiles } });
      view.el.remove(); jobViews.delete(view.id); expanded.delete(view.id); saveExpanded();
      toast(answer.deleteFiles ? "Removed; its downloaded files are being deleted" : "Removed from your library (files kept)");
      return update();
    }
    if (act === "refresh") { btn.disabled = true; btn.textContent = "Checking…"; }
    const res = await api(`/api/jobs/${view.id}/${act}`, { method: "POST" });
    if (act === "refresh") {
      view.btnHtml = null;
      toast(res.added ? `Added ${plural(res.added, "new video")}` : "No new videos in this playlist");
    }
    update();
  } catch (err) {
    view.btnHtml = null;
    toast(err.message);
  }
}

function toggleOpen(view) {
  expanded.has(view.id) ? expanded.delete(view.id) : expanded.add(view.id);
  saveExpanded();
  return renderView(view);
}

async function renderView(view) {
  const j = view.job;
  renderHead(view);
  const open = expanded.has(j.id);
  view.el.classList.toggle("open", open);
  if (open && !view.body) buildBody(view);
  if (view.body) {
    view.body.classList.toggle("hidden", !open);
    updateTabCounts(view);
    updateTools(view);
    if (open && view.rev < j.rev) await syncItems(view);
    else if (open) view.vlist.draw();          // the list may have been hidden: draw the rows now in view
  }
}

/** Called with every overview update. */
export function renderLibrary(overview) {
  settings = overview.settings || settings;
  const container = $("#jobs");
  const ids = new Set(overview.jobs.map((j) => j.id));
  for (const [id, view] of jobViews) if (!ids.has(id)) { view.el.remove(); jobViews.delete(id); }
  if (!overview.jobs.length) container.innerHTML = `<p class="empty">Nothing saved yet. Paste a playlist link above.</p>`;
  else $(".empty", container)?.remove();
  for (const j of overview.jobs) {
    const view = ensureView(j);
    view.job = j;
    if (!view.el.parentNode) container.appendChild(view.el);
    renderView(view).catch(() => {});
  }
}
