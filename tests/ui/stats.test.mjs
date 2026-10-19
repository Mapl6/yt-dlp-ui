// stats.js buckets: History & stats per day and per week.
import { expect, it } from "vitest";
import { buckets } from "../../frontend/static/js/stats.js";

const today = new Date(2026, 9, 17);         // Saturday 17 October 2026
const stats = { days: [{ day: "2026-10-17", done: 2, failed: 1, bytes: 300 }, { day: "2026-10-12", done: 1, failed: 0, bytes: 100 }] };

it("buckets: one per day, oldest first, zeros filled", () => {
  const days = buckets(7, stats, "day", today);
  expect(days).toHaveLength(7);
  expect(days[0].key).toBe("2026-10-11");
  expect([days[6].bytes, days[1].bytes, days[2].bytes]).toEqual([300, 100, 0]);
});

it("buckets: weeks start on Monday and add up", () => {
  const weeks = buckets(14, stats, "week", today);
  expect(weeks.map((w) => w.key)).toEqual(["2026-09-28", "2026-10-05", "2026-10-12"]);
  expect([weeks[2].bytes, weeks[2].done]).toEqual([400, 3]);
});
