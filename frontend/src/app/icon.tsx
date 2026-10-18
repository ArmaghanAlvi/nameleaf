import { ImageResponse } from "next/og";
import { BRAND, markDataUri, markSvg } from "@/lib/brand";

// Two icons from one source: the SVG mark, and a 32x32 PNG fallback for
// browsers that don't take SVG icons.
export function generateImageMetadata() {
  return [
    { id: "svg", contentType: "image/svg+xml", size: { width: 32, height: 32 } },
    { id: "png", contentType: "image/png", size: { width: 32, height: 32 } },
  ];
}

export default async function Icon({ id }: { id: Promise<string | number> }) {
  if ((await id) === "svg") {
    return new Response(markSvg(BRAND.color), {
      headers: { "Content-Type": "image/svg+xml" },
    });
  }
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex" }}>
        <img src={markDataUri()} width={32} height={32} alt="" />
      </div>
    ),
    { width: 32, height: 32 },
  );
}
