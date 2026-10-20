// README images: runs the real app on a made-up library (demo_app.py: scratch folders, a local fake YouTube, no
// internet), fills it, starts a few slow downloads, and takes screenshots into docs/images/. Then renders the banner.
//   npm run screenshots          (E2E_CHANNEL=chrome uses the installed Google Chrome)
// Never touches data/ or your download folders, and never uses port 8765.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "@playwright/test";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const OUT = path.join(ROOT, "docs/images");
const PORT = 8790, MEDIA_PORT = 8789;
const BASE = `http://127.0.0.1:${PORT}`;
const DATA = mkdtempSync(path.join(tmpdir(), "ytdl-shots-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
mkdirSync(OUT, { recursive: true });

// a demo app from an earlier run may still be letting go of its port
for (let i = 0; i < 40; i++) {
  try { await fetch(`${BASE}/login`); await sleep(500); } catch { break; }
}
const app = spawn(path.join(ROOT, ".venv/bin/python"),
  ["scripts/screenshots/demo_app.py", "--port", PORT, "--media-port", MEDIA_PORT, "--data", DATA],
  { cwd: ROOT, stdio: ["ignore", "inherit", "inherit"] });
const stop = () => { app.kill(); rmSync(DATA, { recursive: true, force: true }); };
process.on("exit", stop);

async function waitUp() {
  for (let i = 0; i < 240; i++) {
    try { if ((await fetch(`${BASE}/login`)).ok) return; } catch {}
    await sleep(500);
  }
  throw new Error("the demo app didn't start");
}

let token = "";
async function api(p, method = "GET", body) {
  const res = await fetch(BASE + p, { method, headers: { "X-Token": token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) throw new Error(`${method} ${p}: ${res.status} ${await res.text()}`);
  return res.json();
}
const playlist = (key) => `https://www.youtube.com/playlist?list=PLdemo-${key}`;
const ids = (prefix, from, to) => Array.from({ length: to - from + 1 }, (_, i) => `${prefix}${String(from + i).padStart(3, "0")}`);

async function idle() {
  for (let i = 0; i < 600; i++) {
    const { jobs } = await api("/api/overview");
    if (jobs.every((j) => !j.counts.queued && !j.counts.downloading && !j.counts.processing)) return;
    await sleep(1000);
  }
  throw new Error("downloads didn't finish");
}

/** 45 days of history for History & stats (sizes and speeds like real 1080p downloads). */
function history(jobs) {
  const rnd = (() => { let s = 11; return () => (s = (s * 16807) % 2147483647) / 2147483647; })();
  const events = [];
  const now = Date.now() / 1000;
  for (let d = 45; d >= 1; d--) {
    const busy = d % 7 === 2 || d % 7 === 3 ? 0.4 : 1;                      // quieter weekends
    const done = Math.round((3 + rnd() * 12) * busy), failed = rnd() < 0.3 ? 1 + Math.floor(rnd() * 2) : 0;
    for (let k = 0; k < done + failed; k++) {
      const job = jobs[Math.floor(rnd() * jobs.length)];
      const ok = k < done, bytes = Math.round((60 + rnd() * 900) * 2 ** 20);
      events.push({
        time: now - d * 86400 + rnd() * 50000, job_id: job.id, job_title: job.title, item_id: `old${d}-${k}`,
        title: `${job.title} (earlier)`, url: null, status: ok ? "done" : "failed",
        kind: ok ? null : ["network", "ratelimit", "permanent"][Math.floor(rnd() * 3)],
        error: ok ? null : "An earlier failure", bytes: ok ? bytes : null, seconds: ok ? bytes / ((2 + rnd() * 9) * 2 ** 20) : null,
      });
    }
  }
  return events;
}

// The demo saves into a scratch folder: the page is shown the usual folder instead. The live stream is turned off
// (the page then polls), so every update passes through this rewrite.
const SCRATCH_OUT = /[^"]*ytdl-shots-[^/"]*\/out/g;
async function showUsualFolder(page) {
  await page.route("**/api/**", async (route) => {
    const res = await route.fetch();
    route.fulfill({ response: res, body: (await res.text()).replace(SCRATCH_OUT, "~/Downloads/YT Downloader") });
  });
  await page.route("**/api/stream**", (route) => route.abort());     // registered last: checked first
}

async function shoot() {
  await waitUp();
  const browser = await chromium.launch(process.env.E2E_CHANNEL ? { channel: process.env.E2E_CHANNEL } : {});
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await showUsualFolder(page);
  await page.goto(BASE);
  token = await page.locator('meta[name="api-token"]').getAttribute("content");

  // the library: four playlists, part of them downloaded (tiny clips, full speed), two failures
  for (const key of ["linear-algebra", "lofi", "iceland", "cooking"]) await api("/api/open", "POST", { url: playlist(key) });
  const { jobs } = await api("/api/overview");
  const job = (t) => jobs.find((j) => j.title.startsWith(t)).id;
  await api("/api/settings", "PUT", { concurrency: 8 });
  await api(`/api/jobs/${job("Linear")}/download`, "POST", { ids: ids("lin", 1, 16), options: { quality: "1080", container: "mp4" } });
  await api(`/api/jobs/${job("Lo-fi")}/download`, "POST", { ids: ids("lof", 1, 26), options: { quality: "mp3" } });
  await api(`/api/jobs/${job("Home")}/download`, "POST", { ids: ids("coo", 1, 4), options: { quality: "720", container: "mp4" } });
  await idle();
  await api("/api/import", "POST", { data: { app: "yt-downloader", format: 1, jobs: [], events: history(jobs) } });

  // live: big clips served slowly, three at a time, the rest waiting in the queue
  await api("/api/settings", "PUT", { concurrency: 3 });
  await fetch(`http://127.0.0.1:${MEDIA_PORT}/__live/on`);
  await api(`/api/jobs/${job("Linear")}/download`, "POST", { ids: ids("lin", 17, 21), options: { quality: "1080", container: "mp4" } });
  await api(`/api/jobs/${job("Iceland")}/download`, "POST", { ids: ids("ice", 1, 12), options: { quality: "2160", container: "mp4" } });
  console.log("waiting for the speed chart to fill…");
  await sleep(125_000);                                     // the chart shows two minutes

  const view = async (store, scheme = "light") => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.evaluate((s) => { for (const [k, v] of Object.entries(s)) localStorage.setItem(k, JSON.stringify(v)); }, store);
    await page.reload();
    await page.locator("#jobs .job").first().waitFor();
    await page.addStyleTag({ content: ".topbar { position: static !important }" });   // not over element shots
    await sleep(2500);                                      // a couple of live updates
  };
  const save = (name) => path.join(OUT, name);
  const wholePage = async (name) => {                       // down to the last playlist, not the empty space below
    const bottom = await page.locator("#jobs").evaluate((el) => el.getBoundingClientRect().bottom + window.scrollY + 40);
    await page.screenshot({ path: save(name), fullPage: true, clip: { x: 0, y: 0, width: 1440, height: bottom } });
  };

  // 1. the whole page: connection, Activity (downloading now), the library
  await view({ dashOpen: true, dashTab: "active", expanded: [] });
  await wholePage("overview.png");

  // 2. dark mode
  await view({ dashOpen: true, dashTab: "active", expanded: [] }, "dark");
  await wholePage("overview-dark.png");

  // 3. a playlist: options, selection, every state of a video
  await view({ dashOpen: false, dashTab: "active", expanded: [job("Linear")] });
  const card = page.locator(".job", { hasText: "Linear Algebra" });
  await card.getByLabel("Range").fill("22-24");
  await card.getByRole("button", { name: "Select", exact: true }).click();
  await card.locator(".items").evaluate((el) => { el.scrollTop = 60 * 13; });
  await sleep(600);
  await page.locator("#toast").evaluate((el) => el.classList.add("hidden"));
  await card.screenshot({ path: save("playlist.png") });

  // 4. the queue, and 5. history & stats
  await view({ dashOpen: true, dashTab: "queue", expanded: [] });
  await page.locator("#dash").screenshot({ path: save("queue.png") });
  await view({ dashOpen: true, dashTab: "stats", expanded: [] });
  await page.getByRole("combobox", { name: "Period" }).selectOption("30");
  await sleep(1500);
  await page.locator("#dash").screenshot({ path: save("stats.png") });

  // the banner (README) and the social preview (GitHub's 1280×640, under 1 MB)
  const banner = await browser.newPage({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 2 });
  await banner.goto(pathToFileURL(path.join(ROOT, "scripts/screenshots/banner.html")).href);
  await banner.evaluate(() => document.fonts.ready);
  await banner.screenshot({ path: save("banner.png") });
  const social = await browser.newPage({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 1 });
  await social.goto(pathToFileURL(path.join(ROOT, "scripts/screenshots/banner.html")).href);
  await social.screenshot({ path: save("social-preview.png") });

  await browser.close();
  console.log(`saved to ${path.relative(ROOT, OUT)}/`);
}

shoot().then(() => { stop(); process.exit(0); }, (err) => { console.error(err); stop(); process.exit(1); });
