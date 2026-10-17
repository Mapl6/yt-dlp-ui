// The Settings dialog, including the download schedule, speed limits, notifications and yt-dlp updates.

import { TOKEN, api } from "./api.js";
import { notifyEnabled, notifySupported, setNotify } from "./notify.js";
import { $, isNewer, toast } from "./util.js";

let settings = {};
let ytdlpTimer = null;

export const setSettings = (s) => { settings = s || settings; };
export const getSettings = () => settings;

function backupStatus(msg, error = false) {
  const el = $("#backup-status");
  el.textContent = msg;
  el.classList.toggle("err", error);
  el.hidden = !msg;
}

export function openSettings() {
  fillSettings();
  backupStatus("");
  $("#settings-dialog").showModal();
  loadYtdlp();
}

function fillSettings() {
  $("#set-outdir").value = settings.output_dir || "";
  $("#set-concurrency").value = settings.concurrency || 3;
  $("#set-cookies").value = settings.cookies_browser || "";
  $("#set-rate").value = settings.rate_limit_mb || 0;
  $("#set-total-rate").value = settings.total_rate_mb || 0;
  $("#set-sleep").value = settings.sleep_between || 0;
  $("#set-proxy").value = settings.proxy || "";
  $("#set-waitnet").checked = settings.wait_for_network !== false;
  $("#set-sched-on").checked = !!settings.schedule_on;
  $("#set-sched-start").value = settings.schedule_start || "01:00";
  $("#set-sched-end").value = settings.schedule_end || "07:00";
  $("#set-sublangs").value = settings.sub_langs || "en";
  $("#set-notify").checked = notifyEnabled();
  $("#set-notify").disabled = !notifySupported();
  syncSchedule();
}

function syncSchedule() {
  const on = $("#set-sched-on").checked;
  $("#set-sched-start").disabled = $("#set-sched-end").disabled = !on;
}

async function loadYtdlp() {
  clearTimeout(ytdlpTimer);
  if (!$("#settings-dialog").open) return;
  try {
    const y = await api("/api/ytdlp");
    const u = y.update;
    const newer = isNewer(y.latest, y.version);
    $("#ytdlp-version").textContent = `yt-dlp ${y.version}`;
    $("#ytdlp-latest").textContent = u?.running ? "Updating…"
      : u?.restart ? `Version ${u.installed} is installed — restart to use it`
      : u?.ok === false ? "The update failed — see the details below"
      : newer ? `Version ${y.latest} is available` : y.latest ? "Up to date" : "Checking for updates…";
    $("#ytdlp-latest").className = `ytdlp-state ${u?.ok === false ? "s-failed" : newer || u?.restart ? "s-pausing" : "s-done"}`;
    const btn = $("#btn-ytdlp-update");
    btn.disabled = !!u?.running;
    btn.textContent = u?.running ? "Updating…" : "Update now";
    btn.classList.toggle("hidden", !!u?.restart);
    const restart = $("#btn-restart");
    restart.classList.toggle("hidden", !u?.restart);
    restart.disabled = !y.can_restart;
    restart.title = y.can_restart ? "" : "Quit and open the app again to use the new version";
    $("#ytdlp-output").textContent = u?.output || "";
    $("#ytdlp-output").classList.toggle("hidden", !(u?.ok === false && u.output));
    if (u?.running || !y.latest) ytdlpTimer = setTimeout(loadYtdlp, 1500);
  } catch (err) {
    $("#ytdlp-latest").textContent = err.message;
  }
}

async function restartNow() {
  if (!confirm("Restart YT Downloader now? Downloads in progress stop and continue from where they were after the restart.")) return;
  $("#btn-restart").disabled = true;
  $("#btn-restart").textContent = "Restarting…";
  await api("/api/restart", { method: "POST" }).catch((err) => toast(err.message));
  // the page reloads itself once the new process answers with a new session token
  setTimeout(function wait() {
    fetch("/", { cache: "no-store" }).then((r) => (r.ok ? location.reload() : setTimeout(wait, 1000))).catch(() => setTimeout(wait, 1000));
  }, 1500);
}

export function initSettings({ onSaved }) {
  $("#btn-settings").addEventListener("click", openSettings);
  $("#set-sched-on").addEventListener("change", syncSchedule);
  $("#btn-ytdlp-update").addEventListener("click", async () => {
    await api("/api/ytdlp/update", { method: "POST" }).catch((err) => toast(err.message));
    loadYtdlp();
  });
  $("#btn-restart").addEventListener("click", restartNow);

  // export / import all data. The dialog is modal and covers the toast, so results are shown inside it.
  $("#btn-export").addEventListener("click", async () => {
    backupStatus("Exporting…");
    try {
      const res = await fetch("/api/export", { headers: { "X-Token": TOKEN } });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Export failed (${res.status})`);
      const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "yt-downloader-export.json";
      const blob = await res.blob();
      const n = (JSON.parse(await blob.text()).jobs || []).length;
      const url = URL.createObjectURL(blob);
      const a = Object.assign(document.createElement("a"), { href: url, download: name });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      backupStatus(`Saved ${name} (${n} playlist${n === 1 ? "" : "s"}, ${(blob.size / 1024).toFixed(0)} KB) to your downloads.`);
    } catch (err) {
      backupStatus(err.message, true);
    }
  });
  $("#btn-import").addEventListener("click", () => $("#import-file").click());
  $("#import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch {
      backupStatus("This file is not a YT Downloader export.", true);
      return;
    }
    const withSettings = !!data?.settings && confirm(
      "Also use the settings from this file (download folder, proxy, speed limits…)?\n\nCancel keeps your current settings and imports only the library and history.");
    backupStatus("Importing…");
    try {
      const r = await api("/api/import", { method: "POST", body: { data, settings: withSettings } });
      const parts = [`${r.playlists_added} playlist${r.playlists_added === 1 ? "" : "s"} added`];
      if (r.playlists_merged) parts.push(`${r.playlists_merged} already here`);
      parts.push(`${r.videos_added} video${r.videos_added === 1 ? "" : "s"} added`);
      if (r.events_added) parts.push(`${r.events_added} history entries`);
      if (r.settings) parts.push("settings restored");
      if (r.skipped) parts.push(`${r.skipped} unreadable entries skipped`);
      let msg = `Imported: ${parts.join(", ")}.`;
      if (r.checking) msg += ` Checking the folders of ${r.checking} playlist${r.checking === 1 ? "" : "s"} for downloaded files — progress shows on each playlist.`;
      backupStatus(msg);
      if (r.settings) {                 // the form still shows the old settings
        settings = (await api("/api/overview")).settings;
        fillSettings();
      }
      onSaved();
    } catch (err) {
      backupStatus(err.message, true);
    }
  });
  $("#settings-dialog").addEventListener("close", async () => {
    clearTimeout(ytdlpTimer);
    if ($("#settings-dialog").returnValue !== "save") return;
    try {
      const wantNotify = $("#set-notify").checked;
      if (wantNotify !== notifyEnabled()) {
        const on = await setNotify(wantNotify);
        if (wantNotify && !on) toast("Notifications are blocked — allow them for this page in the browser settings");
      }
      settings = await api("/api/settings", {
        method: "PUT",
        body: {
          output_dir: $("#set-outdir").value.trim(),
          concurrency: Number($("#set-concurrency").value) || 3,
          cookies_browser: $("#set-cookies").value,
          rate_limit_mb: Number($("#set-rate").value) || 0,
          total_rate_mb: Number($("#set-total-rate").value) || 0,
          sleep_between: Number($("#set-sleep").value) || 0,
          proxy: $("#set-proxy").value.trim(),
          wait_for_network: $("#set-waitnet").checked,
          schedule_on: $("#set-sched-on").checked,
          schedule_start: $("#set-sched-start").value || "01:00",
          schedule_end: $("#set-sched-end").value || "07:00",
          sub_langs: $("#set-sublangs").value.trim() || "en",
        },
      });
      toast("Settings saved");
      onSaved(settings);
    } catch (err) {
      toast(err.message);
    }
  });
}

