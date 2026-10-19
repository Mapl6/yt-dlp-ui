// End-to-end tests: the real page and server, on a scratch library with a fake YouTube (tests/e2e/fake_app.py).
// Run: npx playwright test   (needs .venv and ffmpeg; no internet)
//
// The same tests run against both pages while the Next.js port replaces the old one, each on its own
// server and library: "static" (the old page at /) and "next" (the Next.js build at /next/).
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

const python = process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python";
const PAGES = [
  { name: "static", port: 8798, media: 8797, base: "/" },        // never 8765: that is the user's own app
  { name: "next", port: 8796, media: 8795, base: "/next/" },
];
// The config is loaded again in each worker: keep the scratch folders the first load made.
for (const p of PAGES) {
  const key = `E2E_DATA_${p.name.toUpperCase()}`;
  process.env[key] ||= mkdtempSync(path.join(tmpdir(), `ytdl-e2e-${p.name}-`));
  p.data = process.env[key];
}

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: 1,                     // one app per page, one library: the tests run in order
  timeout: 90_000,
  expect: { timeout: 30_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    // E2E_CHANNEL=chrome uses the installed Google Chrome instead of Playwright's own (npx playwright install)
    ...(process.env.E2E_CHANNEL ? { channel: process.env.E2E_CHANNEL } : {}),
  },
  projects: PAGES.map((p) => ({
    name: p.name,
    use: { baseURL: `http://127.0.0.1:${p.port}${p.base}` },
    metadata: { mediaPort: p.media },
  })),
  webServer: PAGES.map((p) => ({
    command: `${python} tests/e2e/fake_app.py --port ${p.port} --media-port ${p.media} --data "${p.data}"`,
    url: `http://127.0.0.1:${p.port}/login`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
  })),
});
