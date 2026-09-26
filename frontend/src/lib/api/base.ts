// Single source of the backend base URL (publishing Stage 3b).
// Default "/api": production routes it through Caddy, and local dev through
// the Next.js rewrite in next.config.ts. Set NEXT_PUBLIC_API_BASE_URL only to
// point the browser somewhere else (it is baked in at build time).
const RAW_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api";

export const API_BASE = RAW_BASE.replace(/\/+$/, "");

export function apiUrl(path: string): string {
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}
