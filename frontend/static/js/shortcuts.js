// Keyboard shortcuts. They never fire while typing in a field.

import { setTab } from "./dashboard.js";
import { openSettings } from "./settings.js";
import { $ } from "./util.js";

export const SHORTCUTS = [
  ["/", "Search your playlists"],
  ["u", "Paste a link (focus the link box)"],
  ["p", "Pause or resume all downloads"],
  ["1 – 4", "Dashboard tabs: downloading, up next, recently finished, history & stats"],
  ["s", "Settings"],
  ["?", "Show these shortcuts"],
  ["Esc", "Close a dialog or menu"],
];

const typing = (el) => el && (el.isContentEditable || /^(input|textarea|select)$/i.test(el.tagName));

export function initShortcuts({ togglePauseAll }) {
  $("#shortcut-list").innerHTML = SHORTCUTS.map(([k, d]) => `<dt><kbd>${k}</kbd></dt><dd>${d}</dd>`).join("");
  $("#btn-shortcuts").addEventListener("click", () => $("#keys-dialog").showModal());
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || typing(e.target) || document.querySelector("dialog[open]")) return;
    const k = e.key;
    if (k === "/") { e.preventDefault(); $("#lib-filter").focus(); }
    else if (k === "u") { e.preventDefault(); $("#url").focus(); }
    else if (k === "p") { e.preventDefault(); togglePauseAll(); }
    else if (k === "s") { e.preventDefault(); openSettings(); }
    else if (k === "?") { e.preventDefault(); $("#keys-dialog").showModal(); }
    else if (/^[1-4]$/.test(k)) {
      e.preventDefault();
      setTab(["active", "queue", "recent", "stats"][Number(k) - 1]);
      $("#dash").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
}
