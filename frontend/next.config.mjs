// Built to static files (frontend/out) that FastAPI serves, so running the app needs only Python.
// `npm run dev` (next dev) proxies /api to the backend started with `python server.py --dev`.
import path from "node:path";

const BACKEND = process.env.YTDL_BACKEND || "http://127.0.0.1:8765";
const dev = process.env.NODE_ENV === "development";

/** @type {import("next").NextConfig} */
const config = {
  output: "export",
  basePath: process.env.NEXT_BASE_PATH ?? "/next",   // served beside the old page until it replaces it
  trailingSlash: !dev,                               // /next/ in the build; in dev it would redirect /api/x to /api/x/
  images: { unoptimized: true },
  // the same source always builds the same files, so CI can check the committed build is current
  // (asset names are content hashes; the page itself is never cached)
  generateBuildId: async () => "ytdl",
  devIndicators: false,
  agentRules: false,                                 // next dev: don't write an AGENTS.md into the project
  turbopack: { root: path.resolve(import.meta.dirname, "..") },   // the repo: one node_modules for tests and build
  ...(dev
    ? { rewrites: async () => [{ source: "/api/:path*", destination: `${BACKEND}/api/:path*`, basePath: false }] }
    : {}),
};

export default config;
