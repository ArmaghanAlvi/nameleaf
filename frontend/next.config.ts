import type { NextConfig } from "next";

// Local dev only: forward /api/* to the FastAPI server so the browser sees one
// origin, exactly like production (where Caddy does this and Next never sees
// /api). API_PROXY_TARGET overrides the default backend address.
const devApiTarget = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the production image (Stage 3e).
  output: "standalone",
  experimental: {
    // Dev proxy only (the rewrite below): Next cuts proxied requests at 30 s
    // by default (proxy-request.js: `proxyTimeout || 30000`), which fails any
    // local search longer than that. 6 min covers SEARCH_TIMEOUT_SECONDS
    // (300 s) plus queueing. Production never uses this rewrite (Caddy).
    proxyTimeout: 360_000,
  },
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [{ source: "/api/:path*", destination: `${devApiTarget}/:path*` }];
  },
};

export default nextConfig;
