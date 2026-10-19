// Talking to the local server. Every /api request needs the session token from the page.

export const TOKEN = (typeof document !== "undefined" && document.querySelector('meta[name="api-token"]')?.content) || "";

function reloadOnce() {
  // The server restarted (new token) or asks for the password: load the page again, at most every 15 s.
  if (api.reloading) return;
  let last = 0;
  try { last = Number(sessionStorage.getItem("reloadedAt")) || 0; } catch {}
  if (Date.now() - last > 15000) {
    api.reloading = true;
    try { sessionStorage.setItem("reloadedAt", String(Date.now())); } catch {}
    location.reload();
  }
}

export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", "X-Token": TOKEN },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 403 || res.status === 401) reloadOnce();
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

/**
 * Live updates: Server-Sent Events from /api/stream. Calls onData({overview, activity}) for each push.
 * After repeated failures it gives up and calls onFail() so the page can fall back to polling.
 */
export function openStream(params, onData, onFail) {
  const qs = new URLSearchParams({ token: TOKEN });
  for (const [k, v] of Object.entries(params)) if (v) qs.set(k, v);
  let errors = 0, closed = false;
  const es = new EventSource(`/api/stream?${qs}`);
  es.onmessage = (e) => {
    errors = 0;
    try { onData(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
  es.onerror = () => {
    if (closed) return;
    errors++;
    // EventSource hides the status code: ask once the normal way, which reloads on 401/403.
    api("/api/overview").catch(() => {});
    if (errors >= 3) { closed = true; es.close(); onFail(); }
  };
  return { close() { closed = true; es.close(); } };
}

export const itemUrl = (job, id, action = "") => `/api/jobs/${encodeURIComponent(job)}/items/${encodeURIComponent(id)}${action ? `/${action}` : ""}`;
