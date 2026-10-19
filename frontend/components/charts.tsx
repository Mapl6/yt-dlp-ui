"use client";
// Small SVG charts: a line with a crosshair (speed, response time) and columns with a hover tooltip
// (data per day). One data colour (set in CSS); text uses the text colours, never the data colour.
import { useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";

/* ---------- the shared floating tooltip */

interface Tip { x: number; y: number; value: string; label: string }
let tip: Tip | null = null;
const listeners = new Set<() => void>();
const setTip = (t: Tip | null) => { tip = t; listeners.forEach((l) => l()); };
export const showTip = (x: number, y: number, value: string, label: string) => setTip({ x, y, value, label });
export const hideTip = () => setTip(null);

export function ChartTip() {
  const t = useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => tip, () => null);
  const el = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: 0, top: 0 });
  useLayoutEffect(() => {
    if (!t || !el.current) return;
    const w = el.current.offsetWidth, h = el.current.offsetHeight;
    setPos({ left: Math.min(window.innerWidth - w - 8, Math.max(8, t.x - w / 2)), top: Math.max(8, t.y - h - 12) });
  }, [t]);
  return (
    <div id="chart-tip" ref={el} className={`chart-tip ${t ? "" : "hidden"}`} role="status" style={pos}>
      <b>{t?.value}</b><span>{t?.label}</span>
    </div>
  );
}

/* ---------- line + area */

interface LineOpts { slots?: number; min?: number; gaps?: boolean; fmt?: (v: number) => string; label?: (i: number, len: number) => string }

/** values: numbers or null (a gap, drawn as a small mark at the bottom when gaps). */
export function LineChart({ id, values, width, height, ariaLabel, opts }:
  { id?: string; values: (number | null)[]; width: number; height: number; ariaLabel: string; opts: LineOpts }) {
  const svg = useRef<SVGSVGElement>(null);
  const [cross, setCross] = useState<number | null>(null);
  const W = width, H = height;
  const slots = Math.max(opts.slots || values.length, 2);
  const max = Math.max(opts.min || 1, ...values.filter((v): v is number => v != null)) * 1.12;
  const x = (i: number) => ((i + slots - values.length) / (slots - 1)) * W;
  const y = (v: number) => H - 2 - (v / max) * (H - 6);
  const runs: [string, string][][] = [];
  const gaps: number[] = [];
  let run: [string, string][] = [];
  values.forEach((v, i) => {
    if (v == null) { if (run.length) runs.push(run); run = []; if (opts.gaps) gaps.push(i); }
    else run.push([x(i).toFixed(1), y(v).toFixed(1)]);
  });
  if (run.length) runs.push(run);

  const move = (e: React.PointerEvent) => {
    if (!values.length || !svg.current) return;
    const r = svg.current.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    let best = 0, dist = Infinity;           // snap to the nearest data position
    values.forEach((_, i) => { const d = Math.abs(x(i) - px); if (d < dist) { dist = d; best = i; } });
    setCross(best);
    const v = values[best];
    showTip(e.clientX, r.top, v == null ? "No data" : (opts.fmt || String)(v), opts.label ? opts.label(best, values.length) : "");
  };

  return (
    <svg id={id} ref={svg} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={ariaLabel}
      onPointerMove={move} onPointerLeave={() => { setCross(null); hideTip(); }}>
      {values.length > 0 && <>
        {runs.map((pts, i) => {
          const p = pts.map(([a, b]) => `${a},${b}`).join(" ");
          return <polygon key={`a${i}`} className="area" points={`${pts[0][0]},${H} ${p} ${pts[pts.length - 1][0]},${H}`} />;
        })}
        {runs.map((pts, i) => <polyline key={`l${i}`} className="line" points={pts.map(([a, b]) => `${a},${b}`).join(" ")} />)}
        {gaps.map((i) => <rect key={`g${i}`} className="fail" x={x(i) - 2} y={H - 6} width={4} height={6} rx={1} />)}
        <line className={`cross ${cross == null ? "hidden" : ""}`} x1={cross == null ? 0 : x(cross)} x2={cross == null ? 0 : x(cross)} y1={0} y2={H} />
      </>}
    </svg>
  );
}

/* ---------- columns */

export interface ColumnRow { label: string; value: number; sub?: string }

/** One series. Columns are ≤ 24px wide with a 4px rounded top, square at the baseline; empty days get a hairline. */
export function ColumnChart({ rows, fmt = String, height = 140 }: { rows: ColumnRow[]; fmt?: (v: number) => string; height?: number }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  const H = height;
  const show = (el: HTMLElement, i: number) => {
    const r = rows[i], rect = el.getBoundingClientRect();
    const bar = el.firstElementChild as HTMLElement | null;
    showTip(rect.left + rect.width / 2, rect.top + rect.height - (bar?.offsetHeight || 0), fmt(r.value),
      r.sub ? `${r.label} · ${r.sub}` : r.label);
  };
  return (
    <div onPointerLeave={hideTip}>
      <div className="cols" style={{ height: H }} role="list">
        {rows.map((r, i) => (
          <div key={i} className="col" role="listitem" tabIndex={0} aria-label={`${r.label}: ${fmt(r.value)}`}
            onPointerOver={(e) => show(e.currentTarget, i)} onFocus={(e) => show(e.currentTarget, i)} onBlur={hideTip}>
            <i style={{ height: r.value ? Math.max(2, (r.value / max) * (H - 4)) : 1 }} className={r.value ? "" : "zero"} />
          </div>
        ))}
      </div>
      <div className="cols-axis"><span>{rows[0]?.label || ""}</span><span>{rows[rows.length - 1]?.label || ""}</span></div>
    </div>
  );
}
