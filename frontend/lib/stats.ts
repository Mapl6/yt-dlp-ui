// History & stats: per-day or per-week buckets of the event log. No DOM access, so Node tests can load it.

export interface DayRow { day: string; done: number; failed: number; bytes: number }
export interface Bucket { key: string; label: string; done: number; failed: number; bytes: number }

const dayKey = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const shortDate = (d: Date) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });

/** Every day (or week, starting Monday) of the period, oldest first, with zeros where nothing happened. */
export function buckets(days: number, stats: { days: DayRow[] }, by: "day" | "week" = "day", today = new Date()): Bucket[] {
  const byDay = new Map(stats.days.map((d) => [d.day, d]));
  const out: Bucket[] = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const row = byDay.get(dayKey(d)) || { done: 0, failed: 0, bytes: 0 };
    if (by === "week") {
      const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
      const key = dayKey(monday);
      let w = out[out.length - 1];
      if (!w || w.key !== key) out.push((w = { key, label: `Week of ${shortDate(monday)}`, done: 0, failed: 0, bytes: 0 }));
      w.done += row.done; w.failed += row.failed; w.bytes += row.bytes;
    } else {
      out.push({ key: dayKey(d), label: shortDate(d), done: row.done, failed: row.failed, bytes: row.bytes });
    }
  }
  return out;
}
