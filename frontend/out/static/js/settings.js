// The Settings dialog, including cookies, the download schedule, speed limits, notifications and yt-dlp updates.

import { TOKEN, api } from "./api.js";
import { askPermission, notifySupported, permissionDenied, setNotifyEnabled, takeLegacyNotify } from "./notify.js";
import { $, esc, fmtAgo, isNewer, toast } from "./util.js";

let settings = {};
let cookieFile = null;             // the imported cookies.txt: {imported, count, youtube, signed_in} or null
let sources = [];                  // browsers found on this computer, from /api/cookies/sources
let ytdlpTimer = null;
let legacyChecked = false;

export function setSettings(s) {
  settings = s || settings;
  setNotifyEnabled(settings.notify);
  if (!legacyChecked && s) {
    legacyChecked = true;
    // the notification choice used to live in this browser only: move it to the server once
    if (takeLegacyNotify() && !settings.notify) api("/api/settings", { method: "PUT", body: { notify: true } }).catch(() => {});
  }
}
export const getSettings = () => settings;
export const setCookieFile = (c) => {
  const changed = JSON.stringify(c) !== JSON.stringify(cookieFile);
  cookieFile = c || null;
  if (changed && $("#settings-dialog").open) showCookieFile();
};

function backupStatus(msg, error = false) {
  const el = $("#backup-status");
  el.textContent = msg;
  el.classList.toggle("err", error);
  el.hidden = !msg;
}

export function openSettings(section) {
  fillSettings();
  backupStatus("");
  $("#settings-dialog").showModal();
  loadYtdlp();
  loadCookieSources();
  if (section === "cookies") $("#cookies-section").scrollIntoView({ block: "start" });
}

function fillSettings() {
  $("#set-outdir").value = settings.output_dir || "";
  $("#set-concurrency").value = settings.concurrency || 3;
  $("#set-rate").value = settings.rate_limit_mb || 0;
  $("#set-total-rate").value = settings.total_rate_mb || 0;
  $("#set-sleep").value = settings.sleep_between || 0;
  $("#set-proxy").value = settings.proxy || "";
  $("#set-waitnet").checked = settings.wait_for_network !== false;
  $("#set-sched-on").checked = !!settings.schedule_on;
  $("#set-sched-start").value = settings.schedule_start || "01:00";
  $("#set-sched-end").value = settings.schedule_end || "07:00";
  $("#set-sublangs").value = settings.sub_langs || "en";
  $("#set-notify").checked = !!settings.notify;
  $("#set-notify").disabled = !notifySupported();
  notifyNote();
  fillCookies();
  syncSchedule();
}

function notifyNote() {
  const blocked = $("#set-notify").checked && permissionDenied();
  $("#notify-note").textContent = blocked
    ? "This browser blocks notifications for this page — allow them in the browser’s site settings."
    : "When a playlist finishes downloading or a video fails.";
  $("#notify-note").classList.toggle("err", blocked);
}

/* ---------- cookies */

const STATUS_NOTE = { blocked: " (needs Full Disk Access)", "no-profiles": " (no profiles found)" };

function fillCookies() {
  const sel = $("#set-cookies");
  const want = settings.cookies_browser || "";
  const opts = [`<option value="">None</option>`];
  for (const b of sources) opts.push(`<option value="${esc(b.browser)}">${esc(b.name + (STATUS_NOTE[b.status] || ""))}</option>`);
  if (want && !sources.some((b) => b.browser === want)) opts.push(`<option value="${esc(want)}">${esc(want)} (not found on this computer)</option>`);
  sel.innerHTML = opts.join("");
  sel.value = want;
  fillProfiles(settings.cookies_profile || "");
  cookieTestStatus("");
  showCookieFile();
}

function fillProfiles(want) {
  const b = sources.find((x) => x.browser === $("#set-cookies").value);
  const sel = $("#set-cookies-profile");
  const profiles = b?.profiles || [];
  const opts = [`<option value="">Most recently used</option>`];
  for (const p of profiles) if (p.id) opts.push(`<option value="${esc(p.id)}">${esc(p.name === p.id ? p.id : `${p.name} (${p.id})`)}</option>`);
  if (want && !profiles.some((p) => p.id === want)) opts.push(`<option value="${esc(want)}">${esc(want)} (not found)</option>`);
  sel.innerHTML = opts.join("");
  sel.value = want;
  sel.disabled = !$("#set-cookies").value || $("#set-cookies").value === "safari";
  const note = $("#cookies-note");
  note.textContent = b && b.status !== "ok" ? b.note : "";
  note.hidden = !note.textContent;
}

async function loadCookieSources() {
  try {
    sources = (await api("/api/cookies/sources")).browsers || [];
  } catch (err) {
    sources = [];
    cookieTestStatus(err.message, "err");
  }
  if ($("#settings-dialog").open) {
    const browser = $("#set-cookies").value, profile = $("#set-cookies-profile").value;
    fillCookies();
    $("#set-cookies").value = browser;      // keep what was picked while the list was loading
    fillProfiles(profile);
  }
}

function cookieTestStatus(msg, cls = "") {
  const el = $("#cookies-test-status");
  el.textContent = msg;
  el.className = `cookie-status ${cls}`;
}

async function testCookies() {
  const browser = $("#set-cookies").value;
  if (!browser) return cookieTestStatus("Choose a browser first.", "warn");
  const btn = $("#btn-cookies-test");
  btn.disabled = true;
  cookieTestStatus("Reading cookies… macOS may ask for your password to let the app read them: choose “Always Allow”.");
  try {
    const r = await api("/api/cookies/test", { method: "POST", body: { browser, profile: $("#set-cookies-profile").value } });
    if (!r.ok) cookieTestStatus(r.error, "err");
    else if (r.warning) cookieTestStatus(`${r.youtube} YouTube cookies found. ${r.warning}.`, "warn");
    else cookieTestStatus(`Works: ${r.youtube} YouTube cookies, signed in. Press Save to use them.`, "ok");
  } catch (err) {
    cookieTestStatus(err.message, "err");
  } finally {
    btn.disabled = false;
  }
}

function showCookieFile() {
  const c = cookieFile;
  $("#cookie-file-state").textContent = c
    ? `Imported ${fmtAgo(c.imported)}: ${c.youtube} YouTube cookies${c.signed_in ? ", signed in" : ", not signed in"}. Used instead of the browser’s cookies.`
    : "None imported. Export cookies.txt from a signed-in YouTube tab with a browser extension (e.g. “Get cookies.txt LOCALLY”) and import it here. While a file is imported, it is used instead of the browser.";
  $("#btn-cookie-remove").classList.toggle("hidden", !c);
}

/** Pick a cookies.txt and send it to the server. Also used by the banner when YouTube asks for cookies. */
export function importCookies() {
  $("#cookie-file").click();
}

async function onCookieFile(e) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    cookieFile = await api("/api/cookies/file", { method: "POST", body: { text: await file.text() } });
    showCookieFile();
    toast(`Cookies imported (${cookieFile.youtube} for YouTube) — trying again now`);
    onSavedCb();
  } catch (err) {
    if ($("#settings-dialog").open) cookieTestStatus(err.message, "err");
    toast(err.message);
  }
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

let onSavedCb = () => {};

export function initSettings({ onSaved }) {
  onSavedCb = onSaved;
  $("#btn-settings").addEventListener("click", () => openSettings());
  $("#set-sched-on").addEventListener("change", syncSchedule);
  $("#set-cookies").addEventListener("change", () => { fillProfiles(""); cookieTestStatus(""); });
  $("#set-cookies-profile").addEventListener("change", () => cookieTestStatus(""));
  $("#btn-cookies-test").addEventListener("click", testCookies);
  $("#btn-cookie-import").addEventListener("click", importCookies);
  $("#cookie-file").addEventListener("change", onCookieFile);
  $("#btn-cookie-remove").addEventListener("click", async () => {
    if (!confirm("Remove the imported cookies file? Downloads then use the browser’s cookies (if one is chosen).")) return;
    try {
      await api("/api/cookies/file", { method: "DELETE" });
      cookieFile = null;
      showCookieFile();
      onSaved();
    } catch (err) { toast(err.message); }
  });
  // Ask for notification permission on the click itself: browsers (Safari above all) refuse it later.
  $("#set-notify").addEventListener("change", async (e) => {
    if (e.target.checked && !(await askPermission())) e.target.checked = false;
    notifyNote();
    if (!e.target.checked && permissionDenied()) {
      $("#notify-note").textContent = "This browser blocks notifications for this page — allow them in the browser’s site settings, then tick the box again.";
      $("#notify-note").classList.add("err");
    }
  });
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
      settings = await api("/api/settings", {
        method: "PUT",
        body: {
          output_dir: $("#set-outdir").value.trim(),
          concurrency: Number($("#set-concurrency").value) || 3,
          cookies_browser: $("#set-cookies").value,
          cookies_profile: $("#set-cookies").value ? $("#set-cookies-profile").value : "",
          rate_limit_mb: Number($("#set-rate").value) || 0,
          total_rate_mb: Number($("#set-total-rate").value) || 0,
          sleep_between: Number($("#set-sleep").value) || 0,
          proxy: $("#set-proxy").value.trim(),
          wait_for_network: $("#set-waitnet").checked,
          schedule_on: $("#set-sched-on").checked,
          schedule_start: $("#set-sched-start").value || "01:00",
          schedule_end: $("#set-sched-end").value || "07:00",
          sub_langs: $("#set-sublangs").value.trim() || "en",
          notify: $("#set-notify").checked,
        },
      });
      setNotifyEnabled(settings.notify);
      toast("Settings saved");
      onSaved(settings);
    } catch (err) {
      toast(err.message);
    }
  });
}

