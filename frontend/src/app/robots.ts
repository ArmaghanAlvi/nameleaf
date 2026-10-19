import type { MetadataRoute } from "next";
import { absoluteUrl } from "@/lib/site";

// Everything may be crawled except the API (Stage 5b).
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: "/api/",
    },
    sitemap: absoluteUrl("/sitemap.xml"),
  };
}
