import type { NextConfig } from "next";

// Local dev only: forward /api/* to the FastAPI server so the browser sees one
// origin, exactly like production (where Caddy does this and Next never sees
// /api). API_PROXY_TARGET overrides the default backend address.
const devApiTarget = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the production image (Stage 3e).
  output: "standalone",
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [{ source: "/api/:path*", destination: `${devApiTarget}/:path*` }];
  },
};

export default nextConfig;
