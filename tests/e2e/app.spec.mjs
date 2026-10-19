// The main flows in a real browser, against tests/e2e/fake_app.py (see playwright.config.mjs), on the old page
// and on the Next.js one.
// Elements are found by role, label and visible text, so the same tests can check the Next.js port later.
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

// "Download selected", or "Download 2 videos" once some are selected
const downloadButton = (c) => c.getByRole("button", { name: /^Download (selected|\d+ videos?)$/ });
const card = (page, title) => page.locator(".job").filter({ has: page.locator(".job-title", { hasText: title }) });

async function openCard(page, title) {
  const c = card(page, title);
  const toggle = c.getByRole("button", { name: "Open", exact: true });
  if (await toggle.count()) await toggle.click();
  await expect(downloadButton(c)).toBeVisible();
  return c;
}

async function api(page, path, { method = "GET", data } = {}) {
  const token = await page.locator('meta[name="api-token"]').getAttribute("content");
  const res = await page.request.fetch(path, { method, data, headers: { "X-Token": token } });
  expect(res.ok(), `${method} ${path}: ${res.status()}`).toBeTruthy();
  return res.json();
}

async function jobId(page, title) {
  const { jobs } = await api(page, "/api/overview");
  return jobs.find((j) => j.title === title)?.id;
}

async function throttle(page, on) {
  const port = test.info().project.metadata.mediaPort;
  await page.request.get(`http://127.0.0.1:${port}/__throttle/${on ? "on" : "off"}`);
}

test.beforeEach(async ({ page }) => {
  await page.goto("./");                     // the page under test: / or /next/
  await expect(page.getByRole("heading", { name: "Download from YouTube" })).toBeVisible();
});

test("add a playlist", async ({ page }) => {
  await page.getByLabel("YouTube link").fill("https://e2e.test/list/alpha/5");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.locator("#open-status")).toHaveText("Saved “List alpha” — 5 videos.");
  const c = card(page, "List alpha");
  await expect(c).toContainText("0 / 5 downloaded");
  await expect(downloadButton(c)).toBeVisible();   // opened after adding
  await expect(c.getByRole("checkbox", { name: "Select", exact: true })).toHaveCount(5);
});

test("a link that can't be read shows the error", async ({ page }) => {
  await page.getByLabel("YouTube link").fill("https://e2e.test/nothing-here");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.locator("#open-status")).toContainText("Unsupported URL");
  await expect(page.getByRole("button", { name: "Add", exact: true })).toBeEnabled();
});

test("select a range and download it", async ({ page }) => {
  const c = await openCard(page, "List alpha");
  await c.getByLabel("Range").fill("1-2");
  await c.getByRole("button", { name: "Select", exact: true }).click();
  await expect(c.getByText("2 selected")).toBeVisible();
  await downloadButton(c).click();
  await expect(c).toContainText("2 / 5 downloaded", { timeout: 60_000 });
  await expect(c.locator(".job-sub .badge")).not.toHaveText("Downloading");
});

test("pause and resume a playlist", async ({ page }) => {
  const c = await openCard(page, "List alpha");
  await throttle(page, true);
  try {
    await c.getByRole("button", { name: "Select not downloaded" }).click();
    await expect(c.getByText("3 selected")).toBeVisible();
    await downloadButton(c).click();
    await c.getByRole("button", { name: "Pause", exact: true }).click();
    await expect(c.getByRole("button", { name: "Resume", exact: true })).toBeVisible();
    await expect(c.locator(".job-sub .badge")).toHaveText(/Paus/);
  } finally {
    await throttle(page, false);
  }
  await c.getByRole("button", { name: "Resume", exact: true }).click();
  await expect(c).toContainText("5 / 5 downloaded", { timeout: 60_000 });
  await expect(c.locator(".job-sub .badge")).toHaveText("All downloaded");
});

test("pause all and resume all from the dashboard", async ({ page }) => {
  await page.getByLabel("YouTube link").fill("https://e2e.test/list/gamma/2");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const c = await openCard(page, "List gamma");
  await throttle(page, true);
  try {
    await c.getByRole("button", { name: "Select not downloaded" }).click();
    await downloadButton(c).click();
    await page.getByRole("button", { name: "Pause all" }).click();       // shown only while something downloads
    await expect(page.getByRole("button", { name: "Resume all" })).toBeVisible();
    const { jobs } = await api(page, "/api/overview");
    expect(jobs.filter((j) => j.title === "List gamma").every((j) => j.paused)).toBeTruthy();
  } finally {
    await throttle(page, false);
  }
  await page.getByRole("button", { name: "Resume all" }).click();
  await expect(c).toContainText("2 / 2 downloaded", { timeout: 60_000 });
  await expect(page.getByRole("button", { name: "Resume all" })).toBeHidden();
});

test("settings are saved and still there after a reload", async ({ page }) => {
  const before = (await api(page, "/api/overview")).settings;
  await page.getByRole("button", { name: "Settings" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Settings" })).toBeVisible();
  await dialog.getByLabel("Parallel downloads").fill("2");
  await dialog.getByLabel("Subtitle languages").fill("en, fa");
  await dialog.getByLabel("Pause between videos (s)").fill("1");
  await dialog.getByLabel("Speed limit per download (MB/s)").fill("2.5");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect.poll(async () => (await api(page, "/api/overview")).settings.concurrency).toBe(2);

  await page.reload();
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(dialog.getByLabel("Parallel downloads")).toHaveValue("2");
  await expect(dialog.getByLabel("Subtitle languages")).toHaveValue("en, fa");
  await expect(dialog.getByLabel("Pause between videos (s)")).toHaveValue("1");
  await expect(dialog.getByLabel("Speed limit per download (MB/s)")).toHaveValue("2.5");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await api(page, "/api/settings", { method: "PUT", data: before });
});

test("a playlist's Save options keeps them without downloading", async ({ page }) => {
  await page.getByLabel("YouTube link").fill("https://e2e.test/list/beta/2");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const c = await openCard(page, "List beta");
  const save = c.getByRole("button", { name: "Save options" });
  await expect(save).toBeDisabled();
  await c.getByLabel("Sponsor segments").selectOption("mark");
  await expect(c.getByText("Not saved — press Save options")).toBeVisible();
  await save.click();
  await expect(save).toBeDisabled();
  await expect(c.getByText("Not saved")).toBeHidden();
  await expect(c).toContainText("0 / 2 downloaded");

  await page.reload();
  const again = await openCard(page, "List beta");
  await expect(again.getByLabel("Sponsor segments")).toHaveValue("mark");
  const id = await jobId(page, "List beta");
  const { items } = await api(page, `/api/jobs/${id}/items`);
  expect(items.every((it) => it.status === "new")).toBeTruthy();
});

test("YouTube's bot check pauses the queue and offers cookies", async ({ page }) => {
  await page.getByLabel("YouTube link").fill("https://e2e.test/bot/1");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const c = await openCard(page, "List bot");
  await c.getByRole("button", { name: "Select not downloaded" }).click();
  await downloadButton(c).click();
  const banner = page.locator("#wait-banner");
  try {
    await expect(banner).toBeVisible({ timeout: 60_000 });
    await expect(banner).toContainText("YouTube is limiting requests");
    await expect(banner.getByRole("button", { name: "Import cookies…" })).toBeVisible();
    await expect(banner.getByRole("button", { name: "Try now" })).toBeVisible();
    await banner.getByRole("button", { name: "Use cookies from browser…" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel("Use cookies from browser")).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
  } finally {
    const id = await jobId(page, "List bot");
    await api(page, `/api/jobs/${id}/stop`, { method: "POST" });
    await api(page, `/api/jobs/${id}/remove`, { method: "POST" });
    // a new proxy ends a rate-limit pause at once (and so does setting it back)
    const { settings } = await api(page, "/api/overview");
    await api(page, "/api/settings", { method: "PUT", data: { proxy: "http://127.0.0.1:9" } });
    await api(page, "/api/settings", { method: "PUT", data: { proxy: settings.proxy } });
  }
  await expect(banner).toBeHidden();
});

test("export all data, then import it back", async ({ page }, info) => {
  await page.getByRole("button", { name: "Settings" }).click();
  const dialog = page.getByRole("dialog");
  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Export all data" }).click();
  const file = info.outputPath("export.json");
  await (await download).saveAs(file);
  const data = JSON.parse(readFileSync(file, "utf8"));
  expect(data.jobs.map((j) => j.title).sort()).toEqual(["List alpha", "List beta", "List gamma"]);
  await dialog.getByRole("button", { name: "Cancel" }).click();

  const id = await jobId(page, "List alpha");
  await api(page, `/api/jobs/${id}/remove`, { method: "POST" });
  await expect(card(page, "List alpha")).toHaveCount(0);

  await page.getByRole("button", { name: "Settings" }).click();
  await page.locator("#import-file").setInputFiles(file);
  await expect(dialog.getByRole("status").filter({ hasText: /added|Imported/ })).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  const back = card(page, "List alpha");
  await expect(back).toBeVisible();
  await expect(back).toContainText("5 / 5 downloaded", { timeout: 60_000 });
});
