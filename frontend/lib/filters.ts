// Which videos show in a playlist's list, and in what order. Filters take the whole item, not only its
// status, because "removed" (unavailable) matters as much as the status.
import type { Item } from "./types";

export const SELECTABLE = (it: Item) => (it.status === "new" || it.status === "failed") && !it.removed;
export const UNAVAILABLE = (it: Item) => !!it.removed && (it.status === "new" || it.status === "failed");
export const DOWNLOADED = (it: Item) => it.status === "done" || it.status === "skipped";
export const IN_QUEUE = (it: Item) => it.status === "queued" || it.status === "downloading" || it.status === "processing";

export const TABS: [string, string, (it: Item) => boolean][] = [
  ["all", "All", () => true],
  ["new", "Not downloaded", (it) => it.status === "new" && !it.removed],
  ["queue", "In queue", IN_QUEUE],
  ["done", "Downloaded", DOWNLOADED],
  ["failed", "Failed", (it) => it.status === "failed" && !it.removed],
  ["removed", "Unavailable", UNAVAILABLE],
];
export const TAB_FILTER: Record<string, (it: Item) => boolean> = Object.fromEntries(TABS.map(([k, , f]) => [k, f]));

export const DURATIONS: Record<string, (it: Item) => boolean> = {
  any: () => true,
  short: (it) => it.duration != null && it.duration < 600,
  medium: (it) => it.duration != null && it.duration >= 600 && it.duration <= 3600,
  long: (it) => it.duration != null && it.duration > 3600,
};

const STATUS_ORDER: Record<string, number> = { downloading: 0, processing: 1, queued: 2, failed: 3, new: 4, done: 5, skipped: 5 };

export const SORTS: Record<string, (a: Item, b: Item) => number> = {
  index: (a, b) => a.index - b.index,
  title: (a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base", numeric: true }) || a.index - b.index,
  duration: (a, b) => (a.duration ?? Infinity) - (b.duration ?? Infinity) || a.index - b.index,
  status: (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || a.index - b.index,
  finished: (a, b) => (b.finished || 0) - (a.finished || 0) || a.index - b.index,   // newest download first
};

/** The videos a list shows: tab, title search, length filter, then sort. */
export function visibleItems(items: Item[], { tab = "all", text = "", dur = "any", sort = "index" } = {}): Item[] {
  const byTab = TAB_FILTER[tab] || TAB_FILTER.all, byDur = DURATIONS[dur] || DURATIONS.any;
  const q = text.trim().toLowerCase();
  return items.filter((it) => byTab(it) && byDur(it) && (!q || it.title.toLowerCase().includes(q)))
    .sort(SORTS[sort] || SORTS.index);
}
