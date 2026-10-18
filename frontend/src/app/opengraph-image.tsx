import { ImageResponse } from "next/og";
import { BRAND, markDataUri } from "@/lib/brand";

export const alt = `${BRAND.name}: ${BRAND.tagline.charAt(0).toLowerCase()}${BRAND.tagline.slice(1)}`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// Share image: the mark, the name and the tagline on the landing page's
// cream background. Uses next/og's bundled default font; no fetches.
export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          background: "#fafaf8",
          color: BRAND.color,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 32 }}>
          <img src={markDataUri()} width={140} height={140} alt="" />
          <div style={{ fontSize: 120, fontWeight: 700, letterSpacing: -2 }}>
            {BRAND.name}
          </div>
        </div>
        <div style={{ marginTop: 40, fontSize: 44, color: "#475569" }}>
          {BRAND.tagline}
        </div>
      </div>
    ),
    { ...size },
  );
}
