// Small SVG charts: a line with a crosshair (speed, response time) and columns with a hover tooltip
// (data per day). One data colour (set in CSS); text uses the text colours, never the data colour.

import { esc } from "./util.js";

const NS = "http://www.w3.org/2000/svg";

/** Shared floating tooltip. Labels go in with textContent (they can come from the server). */
function tooltip() {
  let el = document.getElementById("chart-tip");
  if (!el) {
    el = document.createElement("div");
    el.id = "chart-tip";
    el.className = "chart-tip hidden";
    el.setAttribute("role", "status");
    el.innerHTML = "<b></b><span></span>";
    document.body.appendChild(el);
  }
  return el;
}
export function showTip(x, y, value, label) {
  const el = tooltip();
  el.firstChild.textContent = value;
  el.lastChild.textContent = label;
  el.classList.remove("hidden");
  const w = el.offsetWidth, h = el.offsetHeight;
  el.style.left = `${Math.min(window.innerWidth - w - 8, Math.max(8, x - w / 2))}px`;
  el.style.top = `${Math.max(8, y - h - 12)}px`;
}
export const hideTip = () => tooltip().classList.add("hidden");

/**
 * Line + area chart into an <svg viewBox="0 0 W H" preserveAspectRatio="none">.
 * values: numbers or null (a gap, drawn as a small mark at the bottom when opts.gaps).
 * opts: { slots, min, fmt(value) -> text, label(i) -> text, gaps }
 */
export function lineChart(svg, values, opts = {}) {
  const [, , W, H] = svg.getAttribute("viewBox").split(" ").map(Number);
  const slots = Math.max(opts.slots || values.length, 2);
  if (!values.length) { svg.innerHTML = ""; return; }
  const max = Math.max(opts.min || 1, ...values.filter((v) => v != null)) * 1.12;
  const x = (i) => ((i + slots - values.length) / (slots - 1)) * W;
  const y = (v) => H - 2 - (v / max) * (H - 6);
  let line = "", area = "", gaps = "", run = [];
  const flush = () => {
    if (!run.length) return;
    const pts = run.map(([a, b]) => `${a},${b}`).join(" ");
    line += `<polyline class="line" points="${pts}"/>`;
    area += `<polygon class="area" points="${run[0][0]},${H} ${pts} ${run[run.length - 1][0]},${H}"/>`;
    run = [];
  };
  values.forEach((v, i) => {
    if (v == null) { flush(); if (opts.gaps) gaps += `<rect class="fail" x="${x(i) - 2}" y="${H - 6}" width="4" height="6" rx="1"/>`; }
    else run.push([x(i).toFixed(1), y(v).toFixed(1)]);
  });
  flush();
  svg.innerHTML = area + line + gaps + `<line class="cross hidden" x1="0" x2="0" y1="0" y2="${H}"/>`;
  svg._chart = { values, x, slots, W, opts };
  if (!svg._hover) {
    svg._hover = true;
    const move = (e) => {
      const c = svg._chart;
      if (!c?.values.length) return;
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * c.W;
      // snap to the nearest data position
      let best = 0, dist = Infinity;
      c.values.forEach((_, i) => { const d = Math.abs(c.x(i) - px); if (d < dist) { dist = d; best = i; } });
      const cross = svg.querySelector(".cross");
      cross.setAttribute("x1", c.x(best)); cross.setAttribute("x2", c.x(best));
      cross.classList.remove("hidden");
      const v = c.values[best];
      showTip(e.clientX, r.top, v == null ? "No data" : (c.opts.fmt || String)(v), c.opts.label ? c.opts.label(best, c.values.length) : "");
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", () => { svg.querySelector(".cross")?.classList.add("hidden"); hideTip(); });
  }
}

/**
 * Columns (one series) into a container: rows = [{ label, value, sub }], opts = { fmt(value), height }.
 * Columns are ≤ 24px wide with a 4px rounded top, square at the baseline; empty days get a hairline.
 */
export function columnChart(box, rows, opts = {}) {
  const fmt = opts.fmt || String;
  const max = Math.max(1, ...rows.map((r) => r.value));
  const H = opts.height || 140;
  box.innerHTML = `<div class="cols" style="height:${H}px" role="list">${rows.map((r, i) =>
    `<div class="col" role="listitem" tabindex="0" data-i="${i}" aria-label="${esc(r.label)}: ${esc(fmt(r.value))}">` +
    `<i style="height:${r.value ? Math.max(2, (r.value / max) * (H - 4)) : 1}px" class="${r.value ? "" : "zero"}"></i></div>`).join("")}</div>` +
    `<div class="cols-axis"><span>${esc(rows[0]?.label || "")}</span><span>${esc(rows[rows.length - 1]?.label || "")}</span></div>`;
  const show = (el) => {
    const r = rows[Number(el.dataset.i)], rect = el.getBoundingClientRect();
    showTip(rect.left + rect.width / 2, rect.top + rect.height - (el.firstChild.offsetHeight || 0), fmt(r.value),
      r.sub ? `${r.label} · ${r.sub}` : r.label);
  };
  box.onpointerover = (e) => { const el = e.target.closest(".col"); if (el) show(el); };
  box.onpointerleave = hideTip;
  box.onfocusin = (e) => { const el = e.target.closest(".col"); if (el) show(el); };
  box.onfocusout = hideTip;
}
