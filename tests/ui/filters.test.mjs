// filters.js: which videos can be selected, the tabs, search, length filters and sorting.
import { describe, expect, it } from "vitest";
import * as F from "../../static/js/filters.js";

const items = [
  { id: "a", index: 1, title: "Alpha", status: "done", duration: 300, finished: 50 },
  { id: "b", index: 2, title: "beta", status: "new", duration: 4000 },
  { id: "c", index: 3, title: "Gamma", status: "failed", duration: 1200 },
  { id: "d", index: 4, title: "Delta", status: "new", removed: true, duration: null },
  { id: "e", index: 5, title: "Epsilon", status: "downloading", duration: 60 },
  { id: "f", index: 6, title: "Zeta", status: "skipped", duration: 700, finished: 90 },
];
const ids = (list) => list.map((i) => i.id).join("");
const visible = (opts) => ids(F.visibleItems(items, opts));

describe("filters", () => {
  it("filter: selectable = not downloaded and available", () => expect(ids(items.filter(F.SELECTABLE))).toBe("bc"));
  it("filter: unavailable", () => expect(ids(items.filter(F.UNAVAILABLE))).toBe("d"));
  it("tab: not downloaded hides unavailable", () => expect(visible({ tab: "new" })).toBe("b"));
  it("tab: downloaded includes adopted files", () => expect(visible({ tab: "done" })).toBe("af"));
  it("tab: in queue", () => expect(visible({ tab: "queue" })).toBe("e"));
  it("search: case-insensitive", () => expect(visible({ text: "BET" })).toBe("b"));
  it("length: under 10 minutes", () => expect(visible({ dur: "short" })).toBe("ae"));
  it("length: over an hour", () => expect(visible({ dur: "long" })).toBe("b"));
  it("length: unknown length only under 'any'", () => expect(visible({ dur: "medium" })).not.toContain("d"));
});

describe("sorting", () => {
  it("sort: title ignores case", () => expect(visible({ sort: "title" })).toBe("abdecf"));
  it("sort: length, unknown last", () => expect(visible({ sort: "duration" })).toBe("eafcbd"));
  it("sort: status, active first", () => expect(visible({ sort: "status" })).toMatch(/^ec/));
  it("sort: recently downloaded first", () => expect(visible({ sort: "finished" })).toMatch(/^fa/));
  it("sort: unknown sort falls back to playlist order", () => expect(visible({ sort: "nope" })).toBe("abcdef"));
});
