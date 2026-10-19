"use client";
// One small pop-up menu, shared by every "⋯" button.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { MenuEntry } from "./AppContext";

interface Open { anchor: HTMLElement; entries: (MenuEntry | "-")[]; onPick: (v: string) => void }

export function useMenu() {
  const [open, setOpen] = useState<Open | null>(null);
  const openRef = useRef<Open | null>(null);
  openRef.current = open;

  const close = useCallback(() => {
    openRef.current?.anchor.setAttribute("aria-expanded", "false");
    setOpen(null);
  }, []);
  /** A second click on the same button closes it. */
  const openMenu = useCallback((anchor: HTMLElement, entries: (MenuEntry | "-")[], onPick: (v: string) => void) => {
    const same = openRef.current?.anchor === anchor;
    close();
    if (same) return;
    anchor.setAttribute("aria-expanded", "true");
    setOpen({ anchor, entries, onPick });
  }, [close]);
  return { open, openMenu, close };
}

export function Menu({ open, close }: { open: Open | null; close: () => void }) {
  const el = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    if (!open || !el.current) { setPos(null); return; }
    const r = open.anchor.getBoundingClientRect();
    const w = el.current.offsetWidth, h = el.current.offsetHeight;
    const below = r.bottom + h + 8 < window.innerHeight;
    setPos({
      left: Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + window.scrollX,
      top: (below ? r.bottom + 4 : r.top - h - 4) + window.scrollY,
    });
  }, [open]);

  useEffect(() => {
    if (pos) el.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus({ preventScroll: true });
  }, [pos]);

  useEffect(() => {
    if (!open) return;
    const down = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!el.current?.contains(t) && !open.anchor.contains(t)) close();
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    const scroll = (e: Event) => { if (!el.current?.contains(e.target as Node)) close(); };
    document.addEventListener("pointerdown", down);
    document.addEventListener("keydown", key);
    window.addEventListener("resize", close);
    document.addEventListener("scroll", scroll, true);
    return () => {
      document.removeEventListener("pointerdown", down);
      document.removeEventListener("keydown", key);
      window.removeEventListener("resize", close);
      document.removeEventListener("scroll", scroll, true);
    };
  }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (!open) return null;
  return (
    <div className="menu" role="menu" ref={el} style={pos ? { left: pos.left, top: pos.top } : { visibility: "hidden" }}>
      {open.entries.map((e, i) => e === "-" ? <hr key={i} /> : (
        <button key={i} type="button" role="menuitem" className={e.danger ? "danger" : ""} disabled={e.disabled}
          onClick={(ev) => { ev.stopPropagation(); close(); open.onPick(e.value); }}>
          {e.label}
        </button>
      ))}
    </div>
  );
}
