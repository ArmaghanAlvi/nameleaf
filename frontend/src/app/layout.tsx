import type { Metadata } from "next";
import { SiteFooter } from "@/components/site/SiteFooter";
import { BRAND } from "@/lib/brand";
import { SITE_URL } from "@/lib/site";
import "./globals.css";

// Site-wide defaults. Each public page adds its own title, description,
// canonical and og/twitter fields through pageMetadata() (lib/site.ts).
export const metadata: Metadata = {
  metadataBase: SITE_URL,
  title: {
    default: BRAND.name,
    template: `%s · ${BRAND.name}`,
  },
  description: BRAND.tagline,
  openGraph: {
    siteName: BRAND.name,
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="flex min-h-screen flex-col bg-stone-50 text-slate-900 antialiased">
        <div className="flex-1">{children}</div>
        <SiteFooter />
      </body>
    </html>
  );
}
