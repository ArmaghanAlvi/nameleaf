// The one brand source (D1a). Name, tagline, colour and the mark's geometry
// live here; the wordmark, the icon routes and the share image all read from
// this module, so replacing the temporary mark means editing MARK below.
//
// BRAND.color must match `--brand` in src/app/globals.css. Page elements use
// the CSS variable; the generated images (no CSS) use BRAND.color.

export const BRAND = {
  name: "NameLeaf",
  tagline: "Find names by meaning, across 21 languages.",
  color: "#0f172a", // slate-900; keep in sync with --brand in globals.css
} as const;

// A temporary single-leaf glyph on a 32-unit grid: an almond leaf on the
// diagonal with a midrib cut out (even-odd fill), plus a short stem.
export const MARK = {
  viewBox: "0 0 32 32",
  // Leaf outline followed by the midrib sliver; filled with fill-rule evenodd
  // so the sliver reads as a cut-out.
  leaf: "M28 4C14 4 6 12 6 26C20 26 28 18 28 4Z M8.86 21.86L23.2 8.2L23.8 8.8L10.14 23.14Z",
  stem: "M2.3 28.3L6.3 24.3L7.7 25.7L3.7 29.7Z",
} as const;

// Standalone SVG document for the mark (icon route, data URIs in images).
// The xmlns attribute is required for a standalone SVG to render.
export function markSvg(color: string = BRAND.color): string {
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${MARK.viewBox}">` +
    `<path fill="${color}" fill-rule="evenodd" d="${MARK.leaf}"/>` +
    `<path fill="${color}" d="${MARK.stem}"/>` +
    `</svg>`
  );
}

export function markDataUri(color: string = BRAND.color): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(markSvg(color))}`;
}
