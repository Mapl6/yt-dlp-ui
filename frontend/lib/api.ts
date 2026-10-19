// Talking to the local server. Every /api request needs the session token: the server writes it into the page
// (<meta name="api-token">); under `next dev` the page asks the backend's dev-only /api/dev-token for it.

let token = "";
let tokenPromise: Promise<string> | null = null;
let reloading = false;

function pageToken(): string {
  if (typeof document === "undefined") return "";
  return document.querySelector<HTMLMetaElement>('meta[name="api-token"]')?.content || "";
}

export function getToken(): Promise<string> {
  if (token) return Promise.resolve(token);
  token = pageToken();
  if (token || process.env.NODE_ENV !== "development") return Promise.resolve(token);
  tokenPromise ||= fetch("/api/dev-token").then((r) => r.json()).then((d) => (token = d.token || ""));
  return tokenPromise;
}

function reloadOnce() {
  // The server restarted (new token) or asks for the password: load the page again, at most every 15 s.
  if (reloading) return;
  let last = 0;
  try { last = Number(sessionStorage.getItem("reloadedAt")) || 0; } catch {}
  if (Date.now() - last > 15000) {
    reloading = true;
    try { sessionStorage.setItem("reloadedAt", String(Date.now())); } catch {}
    location.reload();
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-Token": await getToken() },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 403 || res.status === 401) reloadOnce();
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data as T;
}

/** A raw request with the token (for the export download, which needs the response headers). */
export async function apiFetch(path: string): Promise<Response> {
  return fetch(path, { headers: { "X-Token": await getToken() } });
}

/**
 * Live updates: Server-Sent Events from /api/stream. Calls onData({overview, activity}) for each push.
 * After repeated failures it gives up and calls onFail() so the page can fall back to polling.
 */
export function openStream(params: Record<string, string>, onData: (d: any) => void, onFail: () => void) {
  let es: EventSource | null = null, errors = 0, closed = false;
  getToken().then((t) => {
    if (closed) return;
    const qs = new URLSearchParams({ token: t });
    for (const [k, v] of Object.entries(params)) if (v) qs.set(k, v);
    es = new EventSource(`/api/stream?${qs}`);
    es.onmessage = (e) => {
      errors = 0;
      try { onData(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    es.onerror = () => {
      if (closed) return;
      errors++;
      // EventSource hides the status code: ask once the normal way, which reloads on 401/403.
      api("/api/overview").catch(() => {});
      if (errors >= 3) { closed = true; es?.close(); onFail(); }
    };
  });
  return { close() { closed = true; es?.close(); } };
}

export const itemUrl = (job: string, id: string, action = "") =>
  `/api/jobs/${encodeURIComponent(job)}/items/${encodeURIComponent(id)}${action ? `/${action}` : ""}`;
