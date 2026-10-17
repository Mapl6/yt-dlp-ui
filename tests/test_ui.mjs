// Run: node tests/test_ui.mjs   (no browser needed)
//
// Page logic that doesn't need a browser: the range selector, link parsing, list filters and sorting,
// formatters, the tab title, and the per-day/per-week buckets of History & stats.

import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const root = process.argv[2] || path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const load = (f) => import(pathToFileURL(path.join(root, "static/js", f)).href);
const U = await load("util.js");
const F = await load("filters.js");
const S = await load("stats.js");

let ok = true;
function check(name, cond, extra = "") {
  ok &&= !!cond;
  console.log((cond ? "PASS " : "FAIL ") + name + (cond ? "" : ` ${JSON.stringify(extra)}`));
}
const set = (s) => [...s].sort((a, b) => a - b);

// ---------- range selector
let r = U.parseRange("1-5, 8", 20);
check("range: list and single", JSON.stringify(set(r.picked)) === "[1,2,3,4,5,8]", set(r.picked));
r = U.parseRange("18-", 20);
check("range: open end goes to the last video", JSON.stringify(set(r.picked)) === "[18,19,20]", set(r.picked));
r = U.parseRange("5-3", 20);
check("range: reversed range", JSON.stringify(set(r.picked)) === "[3,4,5]");
r = U.parseRange("1-100000", 30);
check("range: huge range is capped at the playlist length", r.picked.size === 30, r.picked.size);
check("range: bad part reported", U.parseRange("1-5, abc", 20).error === "abc");
check("range: empty is nothing", U.parseRange("  ", 20).picked.size === 0);

// ---------- links
const urls = U.parseUrls("see https://youtu.be/a1, and\nhttps://www.youtube.com/playlist?list=PL1 https://youtu.be/a1 (https://x.test/v).");
check("links: found in text, trailing punctuation dropped, no duplicates",
  JSON.stringify(urls) === JSON.stringify(["https://youtu.be/a1", "https://www.youtube.com/playlist?list=PL1", "https://x.test/v"]), urls);
check("links: none", U.parseUrls("no links here").length === 0);

// ---------- formatters
check("format: bytes", U.fmtBytes(1536) === "2 KB" && U.fmtBytes(5 * 1024 ** 3) === "5.0 GB" && U.fmtBytes(0) === "—");
check("format: clock time", U.fmtTime(75) === "1:15" && U.fmtTime(3725) === "1:02:05" && U.fmtTime(null) === "");
check("format: long waits", U.fmtLong(30) === "under a minute" && U.fmtLong(5400) === "1 h 30 min" && U.fmtLong(200000) === "2 days 7 h",
  [U.fmtLong(30), U.fmtLong(5400), U.fmtLong(200000)]);
check("format: ago", U.fmtAgo(1000, 1030) === "just now" && U.fmtAgo(1000, 1000 + 7200) === "2 h ago");
check("format: plural", U.plural(1, "video") === "1 video" && U.plural(3, "video") === "3 videos");
check("title: idle", U.titleFor([]) === "YT Downloader");
check("title: progress of active downloads", U.titleFor([{ total: 100, downloaded: 50 }, { total: 300, downloaded: 118 }]) === "↓2 · 42% — YT Downloader",
  U.titleFor([{ total: 100, downloaded: 50 }, { total: 300, downloaded: 118 }]));

// ---------- list filters
const items = [
  { id: "a", index: 1, title: "Alpha", status: "done", duration: 300, finished: 50 },
  { id: "b", index: 2, title: "beta", status: "new", duration: 4000 },
  { id: "c", index: 3, title: "Gamma", status: "failed", duration: 1200 },
  { id: "d", index: 4, title: "Delta", status: "new", removed: true, duration: null },
  { id: "e", index: 5, title: "Epsilon", status: "downloading", duration: 60 },
  { id: "f", index: 6, title: "Zeta", status: "skipped", duration: 700, finished: 90 },
];
const ids = (list) => list.map((i) => i.id).join("");
check("filter: selectable = not downloaded and available", ids(items.filter(F.SELECTABLE)) === "bc");
check("filter: unavailable", ids(items.filter(F.UNAVAILABLE)) === "d");
check("tab: not downloaded hides unavailable", ids(F.visibleItems(items, { tab: "new" })) === "b");
check("tab: downloaded includes adopted files", ids(F.visibleItems(items, { tab: "done" })) === "af");
check("tab: in queue", ids(F.visibleItems(items, { tab: "queue" })) === "e");
check("search: case-insensitive", ids(F.visibleItems(items, { text: "BET" })) === "b");
check("length: under 10 minutes", ids(F.visibleItems(items, { dur: "short" })) === "ae");
check("length: over an hour", ids(F.visibleItems(items, { dur: "long" })) === "b");
check("length: unknown length only under 'any'", !ids(F.visibleItems(items, { dur: "medium" })).includes("d"));
check("sort: title ignores case", ids(F.visibleItems(items, { sort: "title" })) === "abdecf", ids(F.visibleItems(items, { sort: "title" })));
check("sort: length, unknown last", ids(F.visibleItems(items, { sort: "duration" })) === "eafcbd", ids(F.visibleItems(items, { sort: "duration" })));
check("sort: status, active first", ids(F.visibleItems(items, { sort: "status" })).startsWith("ec"));
check("sort: recently downloaded first", ids(F.visibleItems(items, { sort: "finished" })).startsWith("fa"));
check("sort: unknown sort falls back to playlist order", ids(F.visibleItems(items, { sort: "nope" })) === "abcdef");

// ---------- history buckets
const today = new Date(2026, 9, 17);         // Saturday 17 October 2026
const stats = { days: [{ day: "2026-10-17", done: 2, failed: 1, bytes: 300 }, { day: "2026-10-12", done: 1, failed: 0, bytes: 100 }] };
const days = S.buckets(7, stats, "day", today);
check("buckets: one per day, oldest first, zeros filled", days.length === 7 && days[0].key === "2026-10-11" && days[6].bytes === 300
  && days[1].bytes === 100 && days[2].bytes === 0, days.map((d) => d.key));
const weeks = S.buckets(14, stats, "week", today);
check("buckets: weeks start on Monday and add up", weeks.map((w) => w.key).join(",") === "2026-09-28,2026-10-05,2026-10-12"
  && weeks[2].bytes === 400 && weeks[2].done === 3, weeks);

console.log(ok ? "\nALL PASSED" : "\nSOME FAILED");
process.exit(ok ? 0 : 1);
