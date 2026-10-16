const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 3500);
}

const fmtBytes = (b) => {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i > 1 ? 1 : 0)} ${u[i]}`;
};
const fmtSpeed = (b) => (b ? `${fmtBytes(b)}/s` : "—");
const fmtTime = (s) => {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

const JOB_LABEL = {
  saved: "Saved", queued: "Queued", downloading: "Downloading", pausing: "Pausing…", paused: "Paused", complete: "All downloaded",
};
const ITEM_LABEL = {
  new: "Not downloaded", queued: "Queued", downloading: "Downloading", processing: "Processing",
  done: "Downloaded", skipped: "Downloaded", failed: "Failed",
};
const AUDIO = ["mp3", "m4a", "opus"];
const SELECTABLE = (s) => s === "new" || s === "failed";

const TABS = [
  ["all", "All", () => true],
  ["new", "Not downloaded", (s) => s === "new"],
  ["queue", "In queue", (s) => s === "queued" || s === "downloading" || s === "processing"],
  ["done", "Downloaded", (s) => s === "done" || s === "skipped"],
  ["failed", "Failed", (s) => s === "failed"],
];
const TAB_FILTER = Object.fromEntries(TABS.map(([k, , f]) => [k, f]));

let settings = {};

/* ------------------------------------------------------------ open state (remembered per browser) */

const expanded = new Set((() => {
  try { return JSON.parse(localStorage.getItem("expanded") || "[]"); } catch { return []; }
})());
function saveExpanded() {
  try { localStorage.setItem("expanded", JSON.stringify([...expanded])); } catch {}
}

/* ------------------------------------------------------------ add a link */

$("#open-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#url").value.trim();
  const status = $("#open-status");
  const btn = $("#btn-open");
  btn.disabled = true;
  btn.textContent = "Reading…";
  status.classList.remove("error");
  status.textContent = "Reading the playlist… large ones can take 10–30 seconds.";
  try {
    const job = await api("/api/open", { method: "POST", body: { url } });
    $("#url").value = "";
    const isNew = job.added === job.total;
    status.textContent = isNew
      ? `Saved “${job.title}” — ${job.total} video${job.total === 1 ? "" : "s"}${job.counts.done ? `, ${job.counts.done} already downloaded` : ""}.`
      : `Updated “${job.title}” — ${job.added ? `${job.added} new video${job.added === 1 ? "" : "s"} added` : "no new videos"}.`;
    expanded.add(job.id);
    saveExpanded();
    await poll();
    jobViews.get(job.id)?.el.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    status.classList.add("error");
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Add";
  }
});

$("#lib-filter").addEventListener("input", () => {
  const q = $("#lib-filter").value.trim().toLowerCase();
  for (const v of jobViews.values()) v.el.classList.toggle("hidden", !!q && !v.job.title.toLowerCase().includes(q));
});

/* ------------------------------------------------------------ playlist cards */

const jobViews = new Map(); // id -> view

function ensureView(j) {
  let view = jobViews.get(j.id);
  if (view) return view;
  const el = document.createElement("div");
  el.className = "job";
  el.innerHTML = `<div class="job-head">
      <img alt="" loading="lazy"><div class="job-main"></div><div class="job-btns"></div></div>`;
  const img = $("img", el);
  img.onerror = () => { img.style.visibility = "hidden"; };
  if (j.thumbnail) img.src = j.thumbnail; else img.style.visibility = "hidden";
  view = { id: j.id, el, job: j, rev: 0, rows: new Map(), tab: "all", text: "", selected: new Set(), body: null };
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
  return b.map(([a, l, cls = ""]) => `<button class="small ${cls}" data-act="${a}">${l}</button>`).join("") +
    `<button class="small ghost" data-act="toggle">${expanded.has(j.id) ? "Close ▲" : "Open ▼"}</button>`;
}

function renderHead(view) {
  const j = view.job, c = j.counts;
  const parts = [`<span class="badge s-${j.status}">${JOB_LABEL[j.status] || j.status}</span>`,
    `<span><b>${c.done}</b> / ${j.total} downloaded (${Math.floor(j.percent)}%)</span>`];
  if (c.downloading + c.processing) parts.push(`<span class="s-downloading">${c.downloading + c.processing} downloading</span>`);
  if (c.queued) parts.push(`<span>${c.queued} queued</span>`);
  if (c.failed) parts.push(`<span class="s-failed">${c.failed} failed</span>`);
  if (j.speed) parts.push(`<span>${fmtSpeed(j.speed)}</span>`);
  const info = `
    <div class="job-title" title="${esc(j.title)}">${esc(j.title)}</div>
    <div class="bar ${j.status === "complete" ? "ok" : ""}"><div style="width:${j.percent}%"></div></div>
    <div class="job-sub">${parts.join("")}</div>`;
  const btns = headButtons(j);
  // Only touch the DOM when something changed, so clicks are never lost mid-press.
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
  for (const k of ["numbering", "thumbnail", "metadata", "subtitles"]) if (k in o) opt(k).checked = !!o[k];
  if (j.kind !== "playlist") $$("[data-playlist-only]", body).forEach((e) => e.classList.add("hidden"));
  const syncAudio = () => {
    const audio = AUDIO.includes(opt("quality").value);
    $$("[data-video-only]", body).forEach((e) => e.classList.toggle("hidden", audio));
  };
  opt("quality").addEventListener("change", syncAudio);
  syncAudio();

  $('[data-role="tabs"]', body).innerHTML = TABS.map(([k, label]) =>
    `<button data-tab="${k}" class="${k === view.tab ? "on" : ""}">${label} <span class="cnt"></span></button>`).join("");

  // Checkbox on a video row
  $('[data-role="items"]', body).addEventListener("change", (e) => {
    const id = e.target.dataset.sel;
    if (!id) return;
    e.target.checked ? view.selected.add(id) : view.selected.delete(id);
    updateSelection(view);
  });
  $('[data-role="sel-all"]', body).addEventListener("change", (e) => {
    for (const row of view.rows.values()) {
      if (!SELECTABLE(row.item.status) || row.el.classList.contains("hidden")) continue;
      e.target.checked ? view.selected.add(row.item.id) : view.selected.delete(row.item.id);
    }
    syncChecks(view);
  });
  $('[data-role="filter"]', body).addEventListener("input", (e) => {
    view.text = e.target.value.trim().toLowerCase();
    applyVisibility(view);
  });
  view.el.appendChild(body);
  view.body = body;
}

function updateSelection(view) {
  const n = view.selected.size;
  const visible = [...view.rows.values()].filter((r) => SELECTABLE(r.item.status) && !r.el.classList.contains("hidden"));
  const picked = visible.filter((r) => view.selected.has(r.item.id)).length;
  const all = $('[data-role="sel-all"]', view.body);
  all.checked = visible.length > 0 && picked === visible.length;
  all.indeterminate = picked > 0 && picked < visible.length;
  $('[data-role="sel-count"]', view.body).textContent = `${n} selected`;
  const dl = $('[data-role="download"]', view.body);
  dl.disabled = n === 0;
  dl.textContent = n ? `Download ${n} video${n === 1 ? "" : "s"}` : "Download selected";
}

function syncChecks(view) {
  for (const row of view.rows.values()) {
    const cb = $("input[data-sel]", row.el);
    if (cb) cb.checked = view.selected.has(row.item.id);
  }
  updateSelection(view);
}

function applyVisibility(view) {
  const f = TAB_FILTER[view.tab];
  for (const row of view.rows.values()) {
    const show = f(row.item.status) && (!view.text || row.item.title.toLowerCase().includes(view.text));
    row.el.classList.toggle("hidden", !show);
  }
  updateSelection(view);
}

function updateTabCounts(view) {
  const c = view.job.counts;
  const n = { all: view.job.total, new: c.new, queue: c.queued + c.downloading + c.processing, done: c.done, failed: c.failed };
  for (const b of $$("[data-tab]", view.body)) $(".cnt", b).textContent = n[b.dataset.tab] ?? "";
}

function renderItem(view, row) {
  const it = row.item;
  if (!SELECTABLE(it.status)) view.selected.delete(it.id);
  let meta = "";
  if (it.status === "downloading") {
    meta = `${it.progress.toFixed(1)}% · ${fmtBytes(it.downloaded)} / ${fmtBytes(it.total)} · ${fmtSpeed(it.speed)}${it.eta != null ? ` · ${fmtTime(it.eta)} left` : ""}`;
  } else if (it.status === "processing") {
    meta = "Merging / converting…";
  } else if (it.status === "done" && it.filepath) {
    meta = esc(it.filepath.split(/[\\/]/).pop());
  } else if (it.status === "skipped") {
    meta = "Already in the folder";
  }
  let btn = "";
  if (it.status === "failed") btn = `<button class="small" data-item="retry" data-id="${esc(it.id)}">Retry</button>`;
  else if (it.status === "queued" || it.status === "downloading" || it.status === "processing")
    btn = `<button class="small ghost" data-item="cancel" data-id="${esc(it.id)}" title="Remove from queue">✕</button>`;
  const selectable = SELECTABLE(it.status);
  row.el.innerHTML = `
    <input type="checkbox" data-sel="${esc(it.id)}" ${selectable ? "" : "disabled"} ${view.selected.has(it.id) ? "checked" : ""}>
    <span class="num">${esc(it.number || it.index)}</span>
    <div class="mid">
      <div class="t" title="${esc(it.title)}">${esc(it.title)}</div>
      ${meta ? `<div class="meta">${meta}</div>` : ""}
      ${it.status === "downloading" ? `<div class="bar small"><div style="width:${it.progress}%"></div></div>` : ""}
      ${it.error ? `<div class="err">${esc(it.error)}</div>` : ""}
    </div>
    <span class="dur">${fmtTime(it.duration)}</span>
    <div class="right"><span class="badge s-${it.status}">${ITEM_LABEL[it.status] || it.status}</span>${btn}</div>`;
  const show = TAB_FILTER[view.tab](it.status) && (!view.text || it.title.toLowerCase().includes(view.text));
  row.el.classList.toggle("hidden", !show);
}

async function syncItems(view) {
  const data = await api(`/api/jobs/${view.id}/items?since=${view.rev}`);
  const list = $('[data-role="items"]', view.body);
  let appended = false;
  for (const it of data.items) {
    let row = view.rows.get(it.id);
    if (!row) {
      row = { el: document.createElement("label"), item: it };
      row.el.className = "item";
      view.rows.set(it.id, row);
      appended = true;
    }
    row.item = it;
    renderItem(view, row);
  }
  if (appended) {
    // keep playlist order even when new videos are merged in later
    [...view.rows.values()].sort((a, b) => a.item.index - b.item.index).forEach((r) => list.appendChild(r.el));
  }
  view.rev = data.rev;
  updateSelection(view);
}

function selectRange(view, spec) {
  const items = [...view.rows.values()].map((r) => r.item);
  const max = Math.max(0, ...items.map((i) => i.index));
  const picked = new Set();
  for (const part of spec.split(/[\s,]+/).filter(Boolean)) {
    const m = part.match(/^(\d+)(?:-(\d*))?$/);
    if (!m) { toast(`Can't read "${part}". Use e.g. 1-50, 80, 100-`); return; }
    const a = Number(m[1]), b = m[2] === undefined ? a : m[2] === "" ? max : Number(m[2]);
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) picked.add(i);
  }
  let n = 0;
  for (const it of items) if (picked.has(it.index) && SELECTABLE(it.status)) { view.selected.add(it.id); n++; }
  syncChecks(view);
  toast(n ? `Selected ${n} video${n === 1 ? "" : "s"} not yet downloaded` : "Everything in that range is already downloaded or queued");
}

async function startDownload(view) {
  const body = view.body;
  const opt = (k) => $(`[data-opt="${k}"]`, body);
  const options = {
    quality: opt("quality").value,
    container: opt("container").value,
    output_dir: opt("output_dir").value.trim(),
    numbering: opt("numbering").checked,
    thumbnail: opt("thumbnail").checked,
    metadata: opt("metadata").checked,
    subtitles: opt("subtitles").checked,
    skip_existing: true,
  };
  const ids = [...view.selected];
  const res = await api(`/api/jobs/${view.id}/download`, { method: "POST", body: { ids, options } });
  view.selected.clear();
  syncChecks(view);
  toast(`Queued ${res.queued} video${res.queued === 1 ? "" : "s"}`);
}

async function onClick(e, view) {
  const btn = e.target.closest("button");
  if (!btn) return;
  e.preventDefault(); // buttons inside <label> rows must not toggle the checkbox
  try {
    if (btn.dataset.tab) {
      view.tab = btn.dataset.tab;
      $$("[data-tab]", view.body).forEach((b) => b.classList.toggle("on", b === btn));
      applyVisibility(view);
      return;
    }
    const role = btn.dataset.role;
    if (role === "sel-missing") {
      for (const row of view.rows.values()) if (SELECTABLE(row.item.status)) view.selected.add(row.item.id);
      syncChecks(view);
      return;
    }
    if (role === "apply-range") return selectRange(view, $('[data-role="range"]', view.body).value);
    if (role === "download") { btn.disabled = true; await startDownload(view); return poll(); }

    if (btn.dataset.item) {
      await api(`/api/jobs/${view.id}/items/${encodeURIComponent(btn.dataset.id)}/${btn.dataset.item}`, { method: "POST" });
      return poll();
    }

    const act = btn.dataset.act;
    if (act === "toggle") {
      expanded.has(view.id) ? expanded.delete(view.id) : expanded.add(view.id);
      saveExpanded();
      return poll();
    }
    if (act === "stop" && !confirm("Stop downloading? Videos still in the queue go back to “Not downloaded”.")) return;
    if (act === "remove" && !confirm(`Remove “${view.job.title}” from your library?\nFiles you already downloaded are kept.`)) return;
    if (act === "refresh") { btn.disabled = true; btn.textContent = "Checking…"; }
    const res = await api(`/api/jobs/${view.id}/${act}`, { method: "POST" });
    if (act === "refresh") {
      view.btnHtml = null;
      toast(res.added ? `Added ${res.added} new video${res.added === 1 ? "" : "s"}` : "No new videos in this playlist");
    }
    if (act === "remove") { view.el.remove(); jobViews.delete(view.id); expanded.delete(view.id); saveExpanded(); }
    poll();
  } catch (err) {
    view.btnHtml = null;
    toast(err.message);
  }
}

/* ------------------------------------------------------------ connection panel */

const LEVELS = ["checking", "excellent", "good", "fair", "weak", "blocked", "offline"];
let lastNetCheck = null;

function fmtMs(p) {
  return p?.ok ? `${p.latency} ms` : p ? (p.error || "No response") : "—";
}

function renderNet(n) {
  if (!n) return;
  const card = $("#net-card"), pill = $("#net-pill");
  for (const el of [card, pill]) {
    LEVELS.forEach((l) => el.classList.remove(`lvl-${l}`));
    el.classList.add(`lvl-${n.level}`);
  }
  $("#net-label").textContent = n.label;
  $("#net-pill-text").textContent = n.label;
  $("#net-advice").textContent = n.advice || "";
  $$("#net-bars i").forEach((b, i) => b.classList.toggle("on", i < (n.bars || 0)));

  const yt = $("#net-yt"), inet = $("#net-inet");
  yt.textContent = fmtMs(n.youtube);
  yt.classList.toggle("bad", !!n.youtube && !n.youtube.ok);
  inet.textContent = fmtMs(n.internet);
  inet.classList.toggle("bad", !!n.internet && !n.internet.ok);
  const y = n.youtube;
  $("#net-stab").textContent = !y?.ok ? "—" : y.loss ? `${y.loss}% lost` : y.jitter < 150 ? "Stable" : `Unsteady (±${y.jitter} ms)`;
  $("#net-speed").textContent = n.speed ? fmtSpeed(n.speed) : n.peak_speed ? `Idle (peak ${fmtSpeed(n.peak_speed)})` : "Idle";
  lastNetCheck = n.checked;
  renderSpark(n.history || []);
}

function renderSpark(hist) {
  const svg = $("#net-spark");
  if (!hist.length) { svg.innerHTML = ""; return; }
  const W = 400, H = 44, slots = 40;
  const max = Math.max(500, ...hist.filter((v) => v != null)) * 1.1;
  const x = (i) => ((i + slots - hist.length) / (slots - 1)) * W;
  const yy = (v) => H - 2 - (v / max) * (H - 6);
  let line = "", area = "", fails = "", run = [];
  const flush = () => {
    if (!run.length) return;
    line += `<polyline class="line" points="${run.map(([a, b]) => `${a},${b}`).join(" ")}"/>`;
    area += `<polygon class="area" points="${run[0][0]},${H} ${run.map(([a, b]) => `${a},${b}`).join(" ")} ${run[run.length - 1][0]},${H}"/>`;
    run = [];
  };
  hist.forEach((v, i) => {
    if (v == null) { flush(); fails += `<rect class="fail" x="${x(i) - 2}" y="${H - 6}" width="4" height="6" rx="1"/>`; }
    else run.push([x(i).toFixed(1), yy(v).toFixed(1)]);
  });
  flush();
  svg.innerHTML = area + line + fails;
}

function tickNetClock() {
  if (!lastNetCheck) return;
  const s = Math.max(0, Math.round(Date.now() / 1000 - lastNetCheck));
  $("#net-checked").textContent = `Checked ${s < 5 ? "just now" : s < 60 ? `${s}s ago` : `${Math.round(s / 60)} min ago`}`;
}
setInterval(tickNetClock, 1000);

async function testNow() {
  const btn = $("#btn-net-check");
  btn.disabled = true;
  btn.textContent = "Testing…";
  const before = lastNetCheck;
  try {
    await api("/api/net/check", { method: "POST" });
    for (let i = 0; i < 60 && lastNetCheck === before; i++) await new Promise((r) => setTimeout(r, 500));
  } finally {
    btn.disabled = false;
    btn.textContent = "Test now";
  }
}
$("#btn-net-check").addEventListener("click", testNow);
$("#net-pill").addEventListener("click", () => $("#net-card").scrollIntoView({ behavior: "smooth", block: "center" }));

/* ------------------------------------------------------------ polling */

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const data = await api("/api/overview");
    settings = data.settings;
    renderNet(data.net);
    $("#stat-active").textContent = `${data.active} downloading`;
    $("#stat-speed").textContent = data.active ? fmtSpeed(data.speed) : "—";

    const container = $("#jobs");
    const ids = new Set(data.jobs.map((j) => j.id));
    for (const [id, view] of jobViews) if (!ids.has(id)) { view.el.remove(); jobViews.delete(id); }
    if (!data.jobs.length) container.innerHTML = `<p class="empty">Nothing saved yet. Paste a playlist link above.</p>`;
    else $(".empty", container)?.remove();

    for (const j of data.jobs) {
      const view = ensureView(j);
      view.job = j;
      if (!view.el.parentNode) container.appendChild(view.el);
      renderHead(view);
      const open = expanded.has(j.id);
      if (open && !view.body) buildBody(view);
      if (view.body) {
        view.body.classList.toggle("hidden", !open);
        updateTabCounts(view);
        if (open && view.rev < j.rev) await syncItems(view);
      }
    }
  } catch {
    $("#stat-speed").textContent = "—";
    renderNet({ level: "offline", label: "App not running", advice: "Can't reach the downloader. Start it with ./run.sh.", bars: 0 });
  } finally {
    polling = false;
  }
}
setInterval(poll, 1000);
poll();

/* ------------------------------------------------------------ settings */

$("#btn-settings").addEventListener("click", () => {
  $("#set-outdir").value = settings.output_dir || "";
  $("#set-concurrency").value = settings.concurrency || 3;
  $("#set-cookies").value = settings.cookies_browser || "";
  $("#set-rate").value = settings.rate_limit_mb || 0;
  $("#set-sleep").value = settings.sleep_between || 0;
  $("#set-proxy").value = settings.proxy || "";
  $("#settings-dialog").showModal();
});

$("#settings-dialog").addEventListener("close", async () => {
  if ($("#settings-dialog").returnValue !== "save") return;
  try {
    settings = await api("/api/settings", {
      method: "PUT",
      body: {
        output_dir: $("#set-outdir").value.trim(),
        concurrency: Number($("#set-concurrency").value) || 3,
        cookies_browser: $("#set-cookies").value,
        rate_limit_mb: Number($("#set-rate").value) || 0,
        sleep_between: Number($("#set-sleep").value) || 0,
        proxy: $("#set-proxy").value.trim(),
      },
    });
    toast("Settings saved");
  } catch (err) {
    toast(err.message);
  }
});
