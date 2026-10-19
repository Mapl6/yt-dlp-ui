// End-to-end tests: the real page and server, on a scratch library with a fake YouTube (tests/e2e/fake_app.py).
// Run: npx playwright test   (needs .venv and ffmpeg; no internet)
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

const python = process.platform === "win32" ? ".venv\\Scripts\\python.exe" : ".venv/bin/python";
const PORT = Number(process.env.E2E_PORT || 8798);           // never 8765: that is the user's own app
const MEDIA_PORT = Number(process.env.E2E_MEDIA_PORT || 8797);
// The config is loaded again in each worker: keep the scratch folder the first load made.
process.env.E2E_DATA ||= mkdtempSync(path.join(tmpdir(), "ytdl-e2e-"));

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: 1,                     // one app, one library: the tests run in order
  timeout: 90_000,
  expect: { timeout: 30_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}/`,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    // E2E_CHANNEL=chrome uses the installed Google Chrome instead of Playwright's own (npx playwright install)
    ...(process.env.E2E_CHANNEL ? { channel: process.env.E2E_CHANNEL } : {}),
  },
  metadata: { mediaPort: MEDIA_PORT },
  webServer: {
    command: `${python} tests/e2e/fake_app.py --port ${PORT} --media-port ${MEDIA_PORT} --data "${process.env.E2E_DATA}"`,
    url: `http://127.0.0.1:${PORT}/login`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
  },
});
