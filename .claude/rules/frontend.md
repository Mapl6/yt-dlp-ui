---
paths:
  - "static/**"
---

# Front-end conventions

- **No framework, no build step, no external scripts.** ES modules in `static/js/` (entry `main.js`, loaded with `<script type="module">`), `index.html` and `style.css`, served by FastAPI.
  - `util.js` (helpers, `parseRange`, formatters), `filters.js` (`TABS`, `SELECTABLE`, sort/length filters) and the `buckets` export of `stats.js` must stay importable in Node, without touching the DOM at import time: the Vitest tests in `tests/ui/` load them.
  - `api.js` (token, `api()`, the SSE client), `library.js` (playlist cards), `vlist.js` (virtual list), `dashboard.js`, `stats.js`, `charts.js`, `netpanel.js`, `settings.js`, `dialogs.js`, `menu.js`, `notify.js`, `shortcuts.js`.
- **Every request goes through `api()`,** which adds the `X-Token` header (read from `<meta name="api-token">`, filled in by `server.py`). A raw `fetch` to `/api/*` gets a 403. The live stream is the one exception: `EventSource` can't send headers, so `/api/stream` (only) takes `?token=`.
- **Live updates come from `/api/stream`** (Server-Sent Events: `{overview, activity}` on every change), with 1-second polling of `/api/overview` + `/api/activity` as the fallback; `main.js` `handle()` renders both. After a button press call the `update`/`refresh` callback for an immediate update. Rows update in place from `/api/jobs/{id}/items?since=<rev>`. Don't re-render whole lists. Head and button HTML is only replaced when it differs (`view.infoHtml`/`view.btnHtml`, `row.html`), otherwise clicks get lost mid-update.
- **Playlist rows are drawn by `VList`** (fixed 60 px rows, only the visible ones exist). Selection and counts work on `view.list`/`view.items` (data), never on DOM rows, which may not exist.
- **Charts** (`charts.js`) use one data colour, `--chart` (validated for both themes with the dataviz palette validator); text never takes the data colour. Every chart has a hover/focus tooltip, and History & stats has a table view.
- **Item filters take the whole item, not the status** (`SELECTABLE(it)`, `UNAVAILABLE(it)`, `TABS` filters), because `removed` matters as much as `status`.
- **Escape anything from the server** with `esc()` before putting it into HTML (titles, errors, file names).
- **Colours come from the CSS variables on `:root`,** with dark-mode overrides in the `prefers-color-scheme` block. Status colours use the `s-<status>` classes.
- **Keep controls findable by role and name:** the end-to-end tests (`tests/e2e/`) click buttons by their text or `aria-label` and fill fields by their `<label>`. Renaming a button or label means updating those tests. A new control needs a visible label or an `aria-label`.
- **Check after editing:** `scripts/test.sh` (syntax, Vitest, and the Playwright tests that drive the page). For a closer look, drive the page in headless Chrome over CDP against a `safe-test-server` instance and read back the DOM and console errors.
