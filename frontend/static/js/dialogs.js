// Dialogs and the detail view of one video (also used inside expanded dashboard rows).

import { api, itemUrl } from "./api.js";
import { $, esc, fmtAgo, fmtTime } from "./util.js";

const QUALITY = (q) => (q === "best" ? "Best available" : /^\d+$/.test(q) ? `${q}p` : String(q || "").toUpperCase());

export function fetchDetail(job, id) {
  return api(itemUrl(job, id, "log"));
}

/** The facts about one video's download: format, folder, file, attempts and its log lines. */
export function detailHtml(d) {
  const o = d.options || {};
  const chosen = [QUALITY(o.quality || "best"), AUDIO_Q(o.quality) ? "" : (o.container || "mp4").toUpperCase(),
    o.subtitles ? `subtitles ${o.sub_langs || "en"}${o.auto_subs ? " (+auto)" : ""}` : "",
    o.sponsorblock ? `SponsorBlock: ${o.sponsorblock}` : "",
    o.section ? `part ${fmtTime(o.section.start)}–${o.section.end != null ? fmtTime(o.section.end) : "end"}` : ""].filter(Boolean).join(" · ");
  const rows = [
    ["Chosen", chosen],
    ["Got", d.fmt || "—"],
    ["Folder", d.outdir],
    ["File", d.filepath ? d.filepath.split(/[\\/]/).pop() : "—"],
  ];
  if (d.step) rows.push(["Now", d.step]);
  if (d.started) rows.push(["Started", `${new Date(d.started * 1000).toLocaleString()} (${fmtAgo(d.started)})`]);
  const tries = (d.history || []).slice().reverse();
  return `<dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join("")}</dl>` +
    (tries.length ? `<div class="sub-h">Attempts</div><ol class="tries">${tries.map((t) =>
      `<li><span class="kind k-${esc(t.kind)}">${esc(KIND[t.kind] || t.kind)}</span><span class="when">${esc(new Date(t.t * 1000).toLocaleTimeString())}</span><span class="msg">${esc(t.msg)}</span></li>`).join("")}</ol>` : "") +
    `<div class="sub-h">Log</div><pre class="log-lines">${esc(d.lines?.length ? d.lines.join("\n") : "No messages recorded for this video.")}</pre>`;
}
const AUDIO_Q = (q) => ["mp3", "m4a", "opus"].includes(q);
export const KIND = { network: "Connection", ratelimit: "Rate limit", permanent: "Can't download", disk: "Disk", other: "Error" };

export async function showLog(job, id) {
  const d = await fetchDetail(job, id);
  $("#log-title").textContent = d.title;
  $("#log-error").textContent = d.error || "";
  $("#log-error").classList.toggle("hidden", !d.error);
  $("#log-body").innerHTML = detailHtml(d);
  $("#log-dialog").showModal();
}

/** Ask before removing a playlist; resolves to { deleteFiles } or null if cancelled. */
export function confirmRemove(title) {
  const dlg = $("#remove-dialog");
  $("#remove-title").textContent = `Remove “${title}”?`;
  $("#remove-files").checked = false;
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "remove" ? { deleteFiles: $("#remove-files").checked } : null), { once: true });
  });
}

export function initDialogs() {
  $("#remove-files").addEventListener("change", (e) => {
    $("#remove-warn").classList.toggle("hidden", !e.target.checked);
    $("#remove-ok").textContent = e.target.checked ? "Remove and delete files" : "Remove";
  });
}
