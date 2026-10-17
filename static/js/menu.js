// One small pop-up menu, shared by every "⋯" button.

import { esc } from "./util.js";

let open = null;

function close() {
  if (!open) return;
  open.el.remove();
  open.anchor.setAttribute("aria-expanded", "false");
  open = null;
}

/** entries: [{ label, value, danger?, disabled? } | "-"]; onPick(value) runs when one is chosen. */
export function openMenu(anchor, entries, onPick) {
  const same = open?.anchor === anchor;
  close();
  if (same) return;                       // a second click on the same button closes it
  const el = document.createElement("div");
  el.className = "menu";
  el.setAttribute("role", "menu");
  el.innerHTML = entries.map((e) => e === "-" ? `<hr>` :
    `<button type="button" role="menuitem" data-v="${esc(e.value)}" class="${e.danger ? "danger" : ""}" ${e.disabled ? "disabled" : ""}>${esc(e.label)}</button>`).join("");
  document.body.appendChild(el);
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth, h = el.offsetHeight;
  const below = r.bottom + h + 8 < window.innerHeight;
  el.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + window.scrollX}px`;
  el.style.top = `${(below ? r.bottom + 4 : r.top - h - 4) + window.scrollY}px`;
  anchor.setAttribute("aria-expanded", "true");
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    e.stopPropagation();
    close();
    onPick(b.dataset.v);
  });
  open = { el, anchor };
  el.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
}

document.addEventListener("pointerdown", (e) => { if (open && !open.el.contains(e.target) && !open.anchor.contains(e.target)) close(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
window.addEventListener("resize", close);
document.addEventListener("scroll", (e) => { if (open && !open.el.contains(e.target)) close(); }, true);
