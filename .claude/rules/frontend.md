---
paths:
  - "frontend/**"
---

# Front-end conventions

- **Next.js (App Router, TypeScript, React), built to static files.**
  - `next.config.mjs` sets `output: "export"`, so `npm run build` writes `frontend/out/`. That build is **committed**: users run only Python, and FastAPI serves `out/index.html` at `/` and `out/_next/` at `/_next/`.
  - Every front-end change needs `npm run build` and the updated `frontend/out` in the same commit. CI fails when the committed build is out of date. The build is reproducible (fixed `generateBuildId`).
  - No server features of Next.js (no API routes, server actions, `next/image` optimisation, middleware): there is no Node server at run time.
- **Layout:**
  - `app/layout.tsx` (shell; imports `app/globals.css`) and `app/page.tsx` → `components/Main.tsx`, the client root;
  - `components/`: `Top` (top bar, link form, wait banner), `NetPanel`, `Dashboard` + `StatsView`, `Library` + `JobBody` + `VirtualList`, `SettingsDialog`, `dialogs`, `Detail`, `Menu`, `charts`;
  - `lib/`: `api` (token, `api()`, the SSE client), `util`, `filters`, `stats`, `notify`, `types`. `lib/util.ts`, `lib/filters.ts` and `lib/stats.ts` must stay free of DOM and React at import time: the Vitest tests in `tests/ui/` load them.
- **The session token:**
  - the server adds `<meta name="api-token">` to `<head>` when it serves the page. Don't render that tag from React: React 19 would add a second, empty copy when it hydrates;
  - under `next dev` there is no tag, and `getToken()` asks the backend's `/api/dev-token`, which exists only with `--dev`;
  - every request goes through `api()` (or `apiFetch()` when you need the response headers). A raw `fetch` to `/api/*` gets a 403. `/api/stream` is the one exception, because `EventSource` can't send headers: it takes `?token=`.
- **State and live updates:**
  - `Main` holds the latest `{overview, activity}` from `/api/stream`, with 1-second polling as the fallback, and shares it and the cross-part actions through `AppContext` (`useApp()`). After a button press call `refresh()` for an immediate update.
  - A playlist's videos come from `/api/jobs/{id}/items?since=<rev>` into a map in `JobBody`; never refetch the whole list.
- **Clicks must never be lost mid-update:**
  - keep keys stable (video ids, playlist ids, event ids);
  - memoise rows (`ItemRow`, `DashRow`) and give them stable props (handlers through a ref, the row position as a number, not a new style object).
- **Playlist rows are drawn by `VirtualList`** (fixed 60 px rows, only the visible ones exist). Selection and counts work on the data, never on DOM rows.
- **Markup and classes follow `globals.css`:**
  - keep the existing class names and ids, which the stylesheet and the end-to-end tests use;
  - colours come from the CSS variables on `:root`, with dark-mode overrides in the `prefers-color-scheme` block; status colours use the `s-<status>` classes;
  - charts (`components/charts.tsx`) use one data colour, `--chart` (validated for both themes); text never takes the data colour. Every chart has a hover/focus tooltip, and History & stats has a table view.
- **Item filters take the whole item, not the status** (`SELECTABLE(it)`, `UNAVAILABLE(it)`, `TABS`), because `removed` matters as much as `status`.
- **Text from the server is rendered as text** (JSX escapes it). Never use `dangerouslySetInnerHTML`.
- **Keep controls findable by role and name:**
  - the end-to-end tests (`tests/e2e/`) click buttons by their text or `aria-label` and fill fields by their `<label>`;
  - renaming a button or label means updating those tests;
  - a new control needs a visible label or an `aria-label`.
- **Check after editing:**
  - `npm run dev` (through `./run.sh --dev`) while working;
  - then `scripts/test.sh`: the build (with the TypeScript check), Vitest, and the Playwright tests that drive the page.
  - For a closer look, drive the page in headless Chrome against a `safe-test-server` instance and read back the DOM and console errors.
