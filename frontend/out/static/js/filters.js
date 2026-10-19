// Which videos show in a playlist's list, and in what order. Filters take the whole item, not only its
// status, because "removed" (unavailable) matters as much as the status.

export const SELECTABLE = (it) => (it.status === "new" || it.status === "failed") && !it.removed;
export const UNAVAILABLE = (it) => it.removed && (it.status === "new" || it.status === "failed");
export const DOWNLOADED = (it) => it.status === "done" || it.status === "skipped";
export const IN_QUEUE = (it) => it.status === "queued" || it.status === "downloading" || it.status === "processing";

export const TABS = [
  ["all", "All", () => true],
  ["new", "Not downloaded", (it) => it.status === "new" && !it.removed],
  ["queue", "In queue", IN_QUEUE],
  ["done", "Downloaded", DOWNLOADED],
  ["failed", "Failed", (it) => it.status === "failed" && !it.removed],
  ["removed", "Unavailable", UNAVAILABLE],
];
export const TAB_FILTER = Object.fromEntries(TABS.map(([k, , f]) => [k, f]));

export const DURATIONS = {
  any: () => true,
  short: (it) => it.duration != null && it.duration < 600,
  medium: (it) => it.duration != null && it.duration >= 600 && it.duration <= 3600,
  long: (it) => it.duration != null && it.duration > 3600,
};

const STATUS_ORDER = { downloading: 0, processing: 1, queued: 2, failed: 3, new: 4, done: 5, skipped: 5 };

export const SORTS = {
  index: (a, b) => a.index - b.index,
  title: (a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base", numeric: true }) || a.index - b.index,
  duration: (a, b) => (a.duration ?? Infinity) - (b.duration ?? Infinity) || a.index - b.index,
  status: (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || a.index - b.index,
  finished: (a, b) => (b.finished || 0) - (a.finished || 0) || a.index - b.index,   // newest download first
};

/** The videos a list shows: tab, title search, length filter, then sort. */
export function visibleItems(items, { tab = "all", text = "", dur = "any", sort = "index" } = {}) {
  const byTab = TAB_FILTER[tab] || TAB_FILTER.all, byDur = DURATIONS[dur] || DURATIONS.any;
  const q = text.trim().toLowerCase();
  return items.filter((it) => byTab(it) && byDur(it) && (!q || it.title.toLowerCase().includes(q)))
    .sort(SORTS[sort] || SORTS.index);
}
