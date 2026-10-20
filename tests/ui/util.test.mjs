// util.js: the range selector, link parsing, formatters and the tab title.
import { describe, expect, it } from "vitest";
import * as U from "../../frontend/lib/util";

const sorted = (s) => [...s].sort((a, b) => a - b);

describe("range selector", () => {
  it("range: list and single", () => expect(sorted(U.parseRange("1-5, 8", 20).picked)).toEqual([1, 2, 3, 4, 5, 8]));
  it("range: open end goes to the last video", () => expect(sorted(U.parseRange("18-", 20).picked)).toEqual([18, 19, 20]));
  it("range: reversed range", () => expect(sorted(U.parseRange("5-3", 20).picked)).toEqual([3, 4, 5]));
  it("range: huge range is capped at the playlist length", () => expect(U.parseRange("1-100000", 30).picked.size).toBe(30));
  it("range: bad part reported", () => expect(U.parseRange("1-5, abc", 20).error).toBe("abc"));
  it("range: empty is nothing", () => expect(U.parseRange("  ", 20).picked.size).toBe(0));
});

describe("links", () => {
  it("links: found in text, trailing punctuation dropped, no duplicates", () => {
    const text = "see https://youtu.be/a1, and\nhttps://www.youtube.com/playlist?list=PL1 https://youtu.be/a1 (https://x.test/v).";
    expect(U.parseUrls(text)).toEqual(["https://youtu.be/a1", "https://www.youtube.com/playlist?list=PL1", "https://x.test/v"]);
  });
  it("links: none", () => expect(U.parseUrls("no links here")).toHaveLength(0));
});

describe("recently finished", () => {
  const ev = (...ids) => ids.map((event) => ({ event }));
  it("pages: newest first, each event once even when pages overlap", () =>
    expect(U.mergeEvents(ev(9, 8, 7), ev(7, 6, 6, 5)).map((r) => r.event)).toEqual([9, 8, 7, 6, 5]));
  it("pages: nothing loaded yet", () => expect(U.mergeEvents(ev(3), [])).toEqual(ev(3)));
});

describe("formatters", () => {
  it("format: bytes", () => {
    expect(U.fmtBytes(1536)).toBe("2 KB");
    expect(U.fmtBytes(5 * 1024 ** 3)).toBe("5.0 GB");
    expect(U.fmtBytes(0)).toBe("—");
  });
  it("format: clock time", () => {
    expect(U.fmtTime(75)).toBe("1:15");
    expect(U.fmtTime(3725)).toBe("1:02:05");
    expect(U.fmtTime(null)).toBe("");
  });
  it("format: long waits", () => {
    expect(U.fmtLong(30)).toBe("under a minute");
    expect(U.fmtLong(5400)).toBe("1 h 30 min");
    expect(U.fmtLong(200000)).toBe("2 days 7 h");
  });
  it("format: ago", () => {
    expect(U.fmtAgo(1000, 1030)).toBe("just now");
    expect(U.fmtAgo(1000, 1000 + 7200)).toBe("2 h ago");
  });
  it("format: plural", () => {
    expect(U.plural(1, "video")).toBe("1 video");
    expect(U.plural(3, "video")).toBe("3 videos");
  });
  it("title: idle", () => expect(U.titleFor([])).toBe("YT Downloader"));
  it("title: progress of active downloads", () =>
    expect(U.titleFor([{ total: 100, downloaded: 50 }, { total: 300, downloaded: 118 }])).toBe("↓2 · 42% — YT Downloader"));
});
