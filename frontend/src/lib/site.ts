import type { Metadata } from "next";
import { BRAND } from "@/lib/brand";

// The public site URL, the one source of every absolute URL the site emits:
// metadataBase (canonical, og:url, og:image), the sitemap, robots.txt and the
// JSON-LD block. Read at BUILD time: static pages are rendered during
// `next build`, so the production image passes SITE_URL as a build arg
// (frontend/Dockerfile.prod, docker-compose.prod.yml, .env.production).
// Unset locally, it falls back to the dev server's address so `npm run dev`
// and `npm run build` keep working; production builds refuse to run without it.
function resolveSiteUrl(): URL {
  const raw = process.env.SITE_URL?.trim();
  if (!raw) return new URL("http://localhost:3000");
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`SITE_URL must be an absolute http(s) URL, got "${raw}"`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`SITE_URL must be an absolute http(s) URL, got "${raw}"`);
  }
  return url;
}

export const SITE_URL: URL = resolveSiteUrl();

// An absolute URL for a site path, e.g. absoluteUrl("/sources").
export function absoluteUrl(path: string): string {
  return new URL(path, SITE_URL).toString();
}

// The share image (app/opengraph-image.tsx). A page that sets its own
// `openGraph` no longer inherits the root opengraph-image file (verified in
// the D3b build: og:image appeared on / only), so every page names it here.
const SHARE_IMAGE = {
  url: "/opengraph-image",
  width: 1200,
  height: 630,
  alt: `${BRAND.name}: ${BRAND.tagline.charAt(0).toLowerCase()}${BRAND.tagline.slice(1)}`,
};

// Per-page metadata. Metadata objects merge shallowly, so a page that sets
// `openGraph` or `twitter` replaces the layout's whole object; building them
// here keeps siteName, type, the card type and the share image on every page.
export function pageMetadata({
  title,
  description,
  path,
}: {
  title?: string;
  description: string;
  path: string;
}): Metadata {
  const fullTitle = title ? `${title} · ${BRAND.name}` : BRAND.name;
  return {
    ...(title ? { title } : {}),
    description,
    alternates: { canonical: path },
    openGraph: {
      siteName: BRAND.name,
      type: "website",
      title: fullTitle,
      description,
      url: path,
      images: [SHARE_IMAGE],
    },
    twitter: {
      card: "summary_large_image",
      title: fullTitle,
      description,
      images: [SHARE_IMAGE],
    },
  };
}
