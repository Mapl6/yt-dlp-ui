// Small helpers shared by every part of the page. No DOM access at import time, so Node tests can load this.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const AUDIO = ["mp3", "m4a", "opus"];

export function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 3500);
}

export const fmtBytes = (b) => {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i > 1 ? 1 : 0)} ${u[i]}`;
};
export const fmtSpeed = (b) => (b ? `${fmtBytes(b)}/s` : "—");

/** 75 -> "1:15", 3725 -> "1:02:05" */
export const fmtTime = (s) => {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};

/** A long wait in words: 42 -> "under a minute", 5400 -> "1 h 30 min", 200000 -> "2 days 7 h" */
export const fmtLong = (s) => {
  if (s == null || !isFinite(s)) return "";
  s = Math.round(s);
  if (s < 60) return "under a minute";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d} day${d === 1 ? "" : "s"}${h ? ` ${h} h` : ""}`;
  if (h) return `${h} h${m ? ` ${m} min` : ""}`;
  return `${m} min`;
};

export const fmtAgo = (ts, now = Date.now() / 1000) => {
  if (!ts) return "";
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};

export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * Read a range like "1-50, 80, 100-" (max = last position) -> { picked: Set of positions }
 * or { error: "the part that couldn't be read" }.
 */
export function parseRange(spec, max) {
  const picked = new Set();
  for (const part of String(spec).split(/[\s,]+/).filter(Boolean)) {
    const m = part.match(/^(\d+)(?:-(\d*))?$/);
    if (!m) return { error: part };
    const a = Number(m[1]), b = m[2] === undefined ? a : m[2] === "" ? max : Number(m[2]);
    const lo = Math.min(a, b), hi = Math.min(Math.max(a, b), Math.max(max, a));
    for (let i = lo; i <= hi; i++) picked.add(i);
  }
  return { picked };
}

/** Every http(s) link in a block of text (pasted, or a .txt file), in order, without duplicates. */
export function parseUrls(text) {
  const seen = new Set(), out = [];
  for (const m of String(text).matchAll(/https?:\/\/[^\s"'<>]+/g)) {
    const url = m[0].replace(/[),.;]+$/, "");
    if (!seen.has(url)) { seen.add(url); out.push(url); }
  }
  return out;
}

/** "Download progress" for the browser tab title: "↓3 · 42% — YT Downloader". */
export function titleFor(active) {
  if (!active?.length) return "YT Downloader";
  const total = active.reduce((n, a) => n + (a.total || 0), 0);
  const done = active.reduce((n, a) => n + (a.total ? Math.min(a.downloaded || 0, a.total) : 0), 0);
  return `↓${active.length}${total ? ` · ${Math.floor((100 * done) / total)}%` : ""} — YT Downloader`;
}

/** Compare versions by their numbers: "2026.10.3" is newer than "2026.08.19" (PyPI drops the zeros). */
export function isNewer(a, b) {
  if (!a || !b) return false;
  const pa = String(a).split(/[.\-]/).map(Number), pb = String(b).split(/[.\-]/).map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export const store = {
  get(key, fallback = null) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} },
};
