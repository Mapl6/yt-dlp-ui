// The Activity panel: what is downloading, the queue (reorder by dragging), recent results from the
// event log, and History & stats.

import { api } from "./api.js";
import { lineChart } from "./charts.js";
import { detailHtml, fetchDetail, showLog } from "./dialogs.js";
import { ITEM_LABEL, REVEAL_LABEL, itemAction, openJob } from "./library.js";
import { renderStats } from "./stats.js";
import { $, $$, esc, fmtAgo, fmtBytes, fmtLong, fmtSpeed, fmtTime, plural, store, toast } from "./util.js";

const EMPTY = {
  active: "Nothing is downloading right now.",
  queue: "The queue is empty. Open a playlist below, select videos and press Download.",
  recent: "No downloads have finished yet.",
};
const TABS = ["active", "queue", "recent", "stats"];

let tab = store.get("dashTab", "active");
if (!TABS.includes(tab)) tab = "active";
const rows = new Map();                 // key -> { el, html }
const open = new Set();                 // expanded rows (keys)
let data = null, overview = null;
let extra = [], extraTotal = null;      // "Show more" pages of Recently finished
const filter = { job: "", q: "" };
let onFilter = () => {}, update = () => {};

const keyOf = (r) => (tab === "recent" ? `e${r.event}` : `${r.job}:${r.id}`);

export function initDashboard(opts) {
  onFilter = opts.onFilter;
  update = opts.onChange;
  $("#dash-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-dash]");
    if (b) setTab(b.dataset.dash);
  });
  $("#dash-job").addEventListener("change", (e) => { filter.job = e.target.value; onFilter({ ...filter }); });
  let t;
  $("#dash-q").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(() => { filter.q = e.target.value.trim(); onFilter({ ...filter }); }, 300);
  });
  $("#dash-toggle").addEventListener("click", () => {
    store.set("dashOpen", $("#dash").classList.contains("collapsed"));
    render();
  });
  $("#dash-pause-all").addEventListener("click", () => queueAction("pause-all", "Paused every playlist"));
  $("#dash-resume-all").addEventListener("click", () => queueAction("resume-all", "Resumed"));
  $("#dash-retry-all").addEventListener("click", async () => {
    const res = await api("/api/queue/retry-failed", { method: "POST" }).catch((err) => toast(err.message));
    if (res) { toast(res.queued ? `Retrying ${plural(res.queued, "video")}` : "Nothing to retry"); update(); }
  });
  $("#dash-clear").addEventListener("click", async () => {
    if (!confirm("Clear the list of recently finished downloads?\nFiles and History & stats are not affected.")) return;
    await api("/api/history/clear", { method: "POST" }).catch((err) => toast(err.message));
    extra = []; extraTotal = 0;
    update();
  });
  const list = $("#dash-list");
  list.addEventListener("click", onListClick);
  list.addEventListener("dragstart", onDragStart);
  list.addEventListener("dragover", onDragOver);
  list.addEventListener("dragleave", (e) => { if (!list.contains(e.relatedTarget)) clearDrop(); });
  list.addEventListener("drop", onDrop);
  list.addEventListener("dragend", () => { clearDrop(); $$(".dragging", list).forEach((el) => el.classList.remove("dragging")); });
  setInterval(refreshOpenRows, 3000);
}

export function setTab(name) {
  if (!TABS.includes(name)) return;
  tab = name;
  store.set("dashTab", tab);
  $("#dash-list").innerHTML = "";
  rows.clear();
  if (tab === "stats") renderStats(true);
  render();
}

async function queueAction(action, msg) {
  try {
    await api(`/api/queue/${action}`, { method: "POST" });
    toast(msg);
    update();
  } catch (err) { toast(err.message); }
}

/** Called with every update. */
export function renderDashboard(activity, ov) {
  data = activity;
  overview = ov;
  render();
}

function isCollapsed(st) {
  const choice = store.get("dashOpen", null);
  return choice === null ? !st.active && !st.queued : !choice;
}

function render() {
  const d = data;
  if (!d) return;
  const st = d.stats;
  const collapsed = isCollapsed(st);
  const dash = $("#dash");
  dash.classList.toggle("collapsed", collapsed);
  $("#dash-toggle").setAttribute("aria-expanded", String(!collapsed));
  $("#dash-toggle").title = collapsed ? "Show" : "Hide";
  $("#dash-note").textContent = st.waiting ? `⏸ ${st.waiting}` : collapsed ? summary(st) : "";
  $("#dash-note").classList.toggle("warn", !!st.waiting);

  const jobs = overview?.jobs || [];
  const busy = jobs.some((j) => j.counts.queued + j.counts.downloading + j.counts.processing > 0);
  const anyPaused = jobs.some((j) => j.paused && j.counts.queued + j.counts.downloading + j.counts.processing > 0);
  $("#dash-pause-all").classList.toggle("hidden", !busy || jobs.filter((j) => j.counts.queued + j.counts.downloading + j.counts.processing > 0).every((j) => j.paused));
  $("#dash-resume-all").classList.toggle("hidden", !anyPaused);
  if (collapsed) return;

  // filters
  const sel = $("#dash-job");
  const opts = `<option value="">All playlists</option>` + d.jobs.map((j) => `<option value="${esc(j.id)}">${esc(j.title)}</option>`).join("");
  if (sel.dataset.html !== opts) { sel.innerHTML = sel.dataset.html = opts; sel.value = filter.job; }

  // tiles
  $("#dash-active").textContent = st.active;
  $("#dash-active-sub").textContent = st.limit >= st.concurrency ? `of ${st.limit} at once`
    : st.probe ? `of ${st.limit} (one at a time until YouTube allows downloads again)`
    : `of ${st.limit} (reduced: weak connection)`;
  $("#dash-queued").textContent = st.queued;
  $("#dash-queued-sub").textContent = st.queued ? `${st.ready} ready · ${st.queued - st.ready} waiting` : "nothing waiting";
  $("#dash-speed").textContent = st.active ? fmtSpeed(st.speed) : "—";
  $("#dash-speed-sub").textContent = st.avg_speed ? `avg ${fmtSpeed(st.avg_speed)} (2 min)` : "";
  $("#dash-eta").textContent = st.eta ? fmtLong(st.eta) : st.left_bytes ? "—" : "Nothing left";
  $("#dash-eta-sub").textContent = st.left_bytes ? `${st.left_estimated ? "≈ " : ""}${fmtBytes(st.left_bytes)} left` : "";
  $("#dash-done").textContent = `${st.done_today} done`;
  $("#dash-done-sub").textContent = st.failed_today ? `${st.failed_today} failed` : "no failures";
  $("#dash-done-sub").classList.toggle("s-failed", !!st.failed_today);
  renderDisk(st.disks || []);
  // the speed chart only earns its space once something has downloaded in the last two minutes
  const hist = st.speed_history || [];
  $(".dash-chart").classList.toggle("hidden", !st.active && !hist.some((v) => v > 0));
  lineChart($("#dash-speed-chart"), hist, {
    slots: 60, min: 512 * 1024, fmt: fmtSpeed,
    label: (i, len) => { const ago = (len - 1 - i) * 2; return ago ? `${ago < 60 ? `${ago}s` : `${Math.floor(ago / 60)} min ${ago % 60}s`} ago` : "now"; },
  });

  const counts = { active: d.active.length, queue: d.queued, recent: d.recent.length };
  for (const b of $$("[data-dash]")) {
    b.classList.toggle("on", b.dataset.dash === tab);
    const cnt = $(".cnt", b);
    if (cnt) cnt.textContent = counts[b.dataset.dash] || "";
  }
  $("#dash-recent-tools").classList.toggle("hidden", tab !== "recent");
  $("#dash-filters").classList.toggle("hidden", tab === "stats");
  $("#dash-list").classList.toggle("hidden", tab === "stats");
  $("#dash-statsview").classList.toggle("hidden", tab !== "stats");
  if (tab !== "stats") renderList(d);
}

function summary(st) {
  if (st.active) return `${st.active} downloading · ${fmtSpeed(st.speed)}`;
  if (st.queued) return `${st.queued} in queue`;
  return st.done_today ? `${st.done_today} downloaded in the last 24 hours` : "Nothing downloading";
}

function renderDisk(disks) {
  const box = $("#dash-disk-tile");
  if (!disks.length) {
    $("#dash-disk").textContent = "—";
    $("#dash-disk-sub").textContent = "no downloads waiting";
    box.classList.remove("warn");
    box.title = "";
    return;
  }
  const worst = disks.slice().sort((a, b) => (a.free ?? -1) - a.need - ((b.free ?? -1) - b.need))[0];
  $("#dash-disk").textContent = worst.problem ? "Not connected" : fmtBytes(worst.free);
  $("#dash-disk-sub").textContent = worst.problem ? worst.problem
    : worst.low ? `free · too little for ≈ ${fmtBytes(worst.need)}` : `free · needs ≈ ${fmtBytes(worst.need)}`;
  box.classList.toggle("warn", !!worst.low || !!worst.problem);
  box.title = disks.map((x) => `${x.path}: ${x.problem || `${fmtBytes(x.free)} free, queue needs ≈ ${fmtBytes(x.need)}`}`).join("\n");
}

/* ------------------------------------------------------------ list */

function rowHtml(r, pos) {
  const vid = r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener" title="${esc(r.title)}">${esc(r.title)}</a>` : esc(r.title);
  const from = `<button class="linkish" data-goto="${esc(r.job)}" data-gid="${esc(r.id)}" title="Show in its playlist">${esc(r.job_title)}</button>`;
  const ids = `data-job="${esc(r.job)}" data-id="${esc(r.id)}"`;
  const icon = (act, label, svg) => `<button class="small ghost icon" data-dact="${act}" ${ids} title="${label}" aria-label="${label}">${svg}</button>`;
  let lead = "", meta = "", extraHtml = "", right = "";
  if (tab === "active") {
    lead = `<span class="badge s-${r.status}">${r.status === "processing" ? esc(r.step || ITEM_LABEL.processing) : ITEM_LABEL[r.status]}</span>`;
    if (r.status === "processing") meta = `${esc(r.step || "Processing")}…`;
    else {
      const bits = [`${(r.progress || 0).toFixed(1)}%`, `${fmtBytes(r.downloaded)} / ${fmtBytes(r.total)}`, fmtSpeed(r.speed)];
      if (r.eta != null) bits.push(`${fmtTime(r.eta)} left`);
      meta = bits.join(" · ");
    }
    if (r.started) meta += ` · started ${fmtAgo(r.started)}`;
    if (r.attempts) meta += ` · attempt ${r.attempts + 1}`;
    extraHtml = `<div class="bar small"><div style="width:${r.status === "processing" ? 100 : r.progress || 0}%"></div></div>`;
    right = icon("hold", "Pause this video", PAUSE) + icon("cancel", "Stop this video", X);
  } else if (tab === "queue") {
    lead = `<span class="grip" aria-hidden="true">⋮⋮</span><span class="pos">#${pos + 1}</span>`;
    meta = `<span class="${r.retrying ? "s-pausing" : ""}">${esc(r.wait)}</span>`;
    if (r.progress > 0 && r.progress < 100) meta += ` · ${r.progress.toFixed(0)}% already downloaded`;
    if (r.duration) meta += ` · ${fmtTime(r.duration)}`;
    const waitingRetry = r.next_try && r.next_try > Date.now() / 1000;
    right = (waitingRetry ? `<button class="small" data-dact="now" ${ids}>Try now</button>` : "") +
      icon("top", "Move to top", TOP) + icon("bottom", "Move to bottom", BOTTOM) +
      (r.held ? icon("unhold", "Continue", PLAY) : icon("hold", "Pause this video", PAUSE)) + icon("cancel", "Remove from queue", X);
  } else {
    lead = `<span class="badge s-${r.status}">${ITEM_LABEL[r.status]}</span>`;
    meta = fmtAgo(r.finished);
    if (r.status === "done" && r.filepath) meta += ` · ${esc(r.filepath.split(/[\\/]/).pop())}`;
    if (r.bytes) meta += ` · ${fmtBytes(r.bytes)}`;
    if (r.status === "done" && r.seconds > 0 && r.bytes) meta += ` · ${fmtSpeed(r.bytes / r.seconds)}`;
    if (r.current && r.current !== r.status && !(r.status === "done" && r.current === "skipped"))
      meta += ` · now ${esc((ITEM_LABEL[r.current] || r.current).toLowerCase())}`;
    const hasFile = r.status === "done" && r.current_file && (r.current === "done" || r.current === "skipped");
    right = (hasFile ? icon("reveal", REVEAL_LABEL, FOLDER) + icon("play", "Play", PLAY) : "") +
      (r.current === "failed" ? `<button class="small" data-dact="retry" ${ids}>Retry</button>` : "");
  }
  const err = r.error && (tab !== "recent" || r.status === "failed") ? `<div class="err" title="${esc(r.error)}">${esc(r.error)}</div>` : "";
  return `<div class="lead">${lead}</div>
    <div class="mid"><div class="t">${vid}</div><div class="meta">${from} · ${meta}</div>${extraHtml}${err}</div>
    <div class="right">${right}<button class="small ghost icon chev-row" data-dact="expand" title="Details" aria-label="Details">${CHEV}</button></div>`;
}

const PAUSE = `<svg viewBox="0 0 24 24"><path d="M9 6v12M15 6v12"/></svg>`;
const PLAY = `<svg viewBox="0 0 24 24"><path d="M8 5.5v13l10-6.5z"/></svg>`;
const X = `<svg viewBox="0 0 24 24"><path d="m7 7 10 10M17 7 7 17"/></svg>`;
const TOP = `<svg viewBox="0 0 24 24"><path d="M6 5h12M12 19V9m0 0-4 4m4-4 4 4"/></svg>`;
const BOTTOM = `<svg viewBox="0 0 24 24"><path d="M6 19h12M12 5v10m0 0-4-4m4 4 4-4"/></svg>`;
const FOLDER = `<svg viewBox="0 0 24 24"><path d="M4 7.5A1.5 1.5 0 0 1 5.5 6H10l2 2h6.5A1.5 1.5 0 0 1 20 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/></svg>`;
const CHEV = `<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>`;

function currentRows(d) {
  if (tab !== "recent") return d[tab] || [];
  const seen = new Set(d.recent.map((r) => r.event));
  const q = filter.q.toLowerCase();
  // the event log isn't filtered by the server: apply the playlist and search filters here
  return d.recent.concat(extra.filter((r) => !seen.has(r.event))).filter((r) => (!filter.job || r.job === filter.job)
    && (!q || r.title.toLowerCase().includes(q) || (r.job_title || "").toLowerCase().includes(q)));
}

function renderList(d) {
  const list = $("#dash-list");
  const items = currentRows(d);
  if (!items.length) {
    rows.clear();
    const html = `<p class="dash-empty">${EMPTY[tab]}${filter.job || filter.q ? " (with the current filter)" : ""}</p>`;
    if (list.innerHTML !== html) list.innerHTML = html;
    return;
  }
  $(".dash-empty", list)?.remove();
  const keep = new Set();
  let prev = null;
  items.forEach((r, i) => {
    const key = keyOf(r);
    keep.add(key);
    let row = rows.get(key);
    if (!row) {
      row = { el: document.createElement("div"), html: "", data: r };
      row.el.className = "dash-row";
      row.el.dataset.key = key;
      row.el.innerHTML = `<div class="dash-main"></div><div class="dash-detail hidden"></div>`;
      rows.set(key, row);
    }
    row.data = r;
    row.el.draggable = tab === "queue";
    row.el.classList.toggle("expanded", open.has(key));
    // Only touch rows that changed, so clicks are never lost mid-update.
    const html = rowHtml(r, i);
    if (row.html !== html) row.el.firstChild.innerHTML = row.html = html;
    const want = prev ? prev.nextSibling : list.firstChild;
    if (want !== row.el) list.insertBefore(row.el, want);
    prev = row.el;
  });
  for (const [key, row] of rows) if (!keep.has(key)) { row.el.remove(); rows.delete(key); }
  // footer: "and N more" for the queue, "Show more" for recent results
  let foot = $(".dash-more", list);
  const more = tab === "queue" ? d.queued - d.queue.length : 0;
  const canPage = tab === "recent" && (extraTotal === null ? d.recent.length >= 30 : items.length < extraTotal);
  if (more > 0 || canPage) {
    if (!foot) foot = list.appendChild(Object.assign(document.createElement("div"), { className: "dash-more" }));
    else if (foot !== list.lastChild) list.appendChild(foot);
    const html = more > 0 ? `…and ${more} more` : `<button class="small ghost" data-dact="more">Show more</button>`;
    if (foot.innerHTML !== html) foot.innerHTML = html;
  } else foot?.remove();
}

async function onListClick(e) {
  const btn = e.target.closest("button");
  const rowEl = e.target.closest(".dash-row");
  try {
    if (!btn) {
      if (rowEl && !e.target.closest("a") && !e.target.closest(".dash-detail") && !String(window.getSelection())) toggleRow(rowEl.dataset.key);
      return;
    }
    if (btn.dataset.goto) return openJob(btn.dataset.goto, btn.dataset.gid);
    const { dact, job, id } = btn.dataset;
    if (dact === "expand") return toggleRow(rowEl.dataset.key);
    if (dact === "more") {
      btn.disabled = true;
      const res = await api(`/api/history?offset=${currentRows(data).length}&limit=30`);
      extra = extra.concat(res.rows);
      extraTotal = res.total;
      return render();
    }
    if (dact === "log") return showLog(job, id);
    btn.disabled = true;
    if (dact === "cancel" && tab === "active" && !confirm("Stop this download? It goes back to “Not downloaded” (the partial download is kept).")) {
      btn.disabled = false;
      return;
    }
    await itemAction(job, id, dact);
  } catch (err) {
    if (btn) btn.disabled = false;
    toast(err.message);
  }
}

/* ------------------------------------------------------------ expanded rows */

async function toggleRow(key) {
  const row = rows.get(key);
  if (!row) return;
  if (open.has(key)) {
    open.delete(key);
    row.el.classList.remove("expanded");
    row.el.lastChild.classList.add("hidden");
    return;
  }
  open.add(key);
  row.el.classList.add("expanded");
  row.el.lastChild.classList.remove("hidden");
  row.el.lastChild.innerHTML = `<p class="muted">Loading…</p>`;
  await fillDetail(key);
}

async function fillDetail(key) {
  const row = rows.get(key);
  if (!row || !open.has(key)) return;
  try {
    const d = await fetchDetail(row.data.job, row.data.id);
    const html = detailHtml(d);
    const box = row.el.lastChild;
    if (box.dataset.html !== html) {
      const pre = box.querySelector(".log-lines");
      const atEnd = !pre || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
      box.innerHTML = box.dataset.html = html;
      const npre = box.querySelector(".log-lines");
      if (npre && atEnd) npre.scrollTop = npre.scrollHeight;
    }
  } catch (err) {
    row.el.lastChild.innerHTML = `<p class="err">${esc(err.message)}</p>`;
  }
}

function refreshOpenRows() {
  if (document.hidden) return;
  for (const key of open) if (rows.has(key)) fillDetail(key);
}

/* ------------------------------------------------------------ drag to reorder the queue */

let dragKey = null;

function onDragStart(e) {
  const row = e.target.closest(".dash-row");
  if (tab !== "queue" || !row) return;
  dragKey = row.dataset.key;
  row.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragKey);
}

function clearDrop() {
  $$(".drop-before, .drop-after", $("#dash-list")).forEach((el) => el.classList.remove("drop-before", "drop-after"));
}

function dropTarget(e) {
  const row = e.target.closest(".dash-row");
  if (!row) return { row: null, after: true };
  const r = row.getBoundingClientRect();
  return { row, after: e.clientY > r.top + r.height / 2 };
}

function onDragOver(e) {
  if (!dragKey) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  clearDrop();
  const { row, after } = dropTarget(e);
  if (row && row.dataset.key !== dragKey) row.classList.add(after ? "drop-after" : "drop-before");
}

async function onDrop(e) {
  if (!dragKey) return;
  e.preventDefault();
  const moving = rows.get(dragKey)?.data;
  const { row, after } = dropTarget(e);
  clearDrop();
  dragKey = null;
  if (!moving || (row && rows.get(row.dataset.key)?.data === moving)) return;
  // "before" the next row when dropped on a row's lower half; at the end when below the last row
  let target = row;
  if (row && after) {
    target = row.nextElementSibling;
    while (target && !target.classList.contains("dash-row")) target = target.nextElementSibling;
  }
  const before = target ? rows.get(target.dataset.key)?.data : null;
  try {
    await api("/api/queue/move", { method: "POST", body: { job: moving.job, id: moving.id, before: before ? { job: before.job, id: before.id } : null } });
    update();
  } catch (err) { toast(err.message); }
}
