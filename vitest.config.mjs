import { defineConfig } from "vitest/config";

// Page logic only (no browser): the end-to-end tests in tests/e2e run with Playwright.
export default defineConfig({
  test: { include: ["tests/ui/**/*.test.mjs"] },
});
