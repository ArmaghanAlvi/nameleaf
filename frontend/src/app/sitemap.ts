import type { MetadataRoute } from "next";
import { LEGAL_LAST_UPDATED } from "@/lib/legal";
import { absoluteUrl } from "@/lib/site";

// The public pages only (Stage 5b): no per-name or per-meaning URLs.
// lastModified only where a real date exists (the legal pages' date); no
// invented build dates.
export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: absoluteUrl("/") },
    { url: absoluteUrl("/generate") },
    { url: absoluteUrl("/sources") },
    { url: absoluteUrl("/privacy"), lastModified: LEGAL_LAST_UPDATED },
    { url: absoluteUrl("/terms"), lastModified: LEGAL_LAST_UPDATED },
  ];
}
