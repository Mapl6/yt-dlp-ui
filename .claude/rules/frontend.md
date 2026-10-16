---
paths:
  - "static/**"
---

# Front-end conventions

- **No framework, no build step, no external scripts.** Plain `app.js`, `index.html` and `style.css`, served by FastAPI.
- **Every request goes through `api()`,** which adds the `X-Token` header (read from `<meta name="api-token">`, filled in by `server.py`). A raw `fetch` to `/api/*` gets a 403.
- **The page polls `/api/overview` every second.** Rows update in place from `/api/jobs/{id}/items?since=<rev>`. Don't re-render whole lists. Head and button HTML is only replaced when it differs (`view.infoHtml`/`view.btnHtml`), otherwise clicks get lost mid-poll.
- **Item filters take the whole item, not the status** (`SELECTABLE(it)`, `UNAVAILABLE(it)`, `TABS` filters), because `removed` matters as much as `status`.
- **Escape anything from the server** with `esc()` before putting it into HTML (titles, errors, file names).
- **Colours come from the CSS variables on `:root`,** with dark-mode overrides in the `prefers-color-scheme` block. Status colours use the `s-<status>` classes.
- **Check after editing:** run `node --check static/app.js`. For UI changes, drive the page in headless Chrome over CDP against a `safe-test-server` instance and read back the DOM and console errors.
