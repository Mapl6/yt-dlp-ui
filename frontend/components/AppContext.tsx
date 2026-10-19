"use client";
// What every part of the page shares: the latest update from the server and the actions that cross parts
// (refresh now, open a playlist, show a video's details, open Settings, the toast, the "⋯" menu).
import { createContext, useContext } from "react";
import type { Activity, Overview } from "../lib/types";

export interface MenuEntry { label: string; value: string; danger?: boolean; disabled?: boolean }

export interface AppApi {
  overview: Overview | null;
  activity: Activity | null;
  /** Ask for a fresh update right away (after a button press), without waiting for the next push. */
  refresh: () => Promise<void>;
  toast: (msg: string) => void;
  setFilter: (f: { job: string; q: string }) => void;
  openSettings: (section?: "cookies") => void;
  importCookies: () => void;
  showLog: (job: string, id: string) => Promise<void>;
  confirmRemove: (title: string) => Promise<{ deleteFiles: boolean } | null>;
  openMenu: (anchor: HTMLElement, entries: (MenuEntry | "-")[], onPick: (value: string) => void) => void;
  openJob: (id: string, itemId?: string | null) => void;
  markExpanded: (id: string) => void;
  itemAction: (job: string, id: string, action: string, title?: string) => Promise<void>;
  setDashTab: (tab: string) => void;
}

export const AppContext = createContext<AppApi>(null as unknown as AppApi);
export const useApp = () => useContext(AppContext);
