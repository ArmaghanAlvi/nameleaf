import type { Metadata } from "next";
import Link from "next/link";
import { Wordmark } from "@/components/brand/Wordmark";
import { RotatingHeroSentence } from "@/components/landing/RotatingHeroSentence";
import { BRAND } from "@/lib/brand";
import { absoluteUrl, pageMetadata } from "@/lib/site";

// No title: the landing page shares the root layout's segment, so it uses the
// layout's default title ("NameLeaf") without the " · NameLeaf" template.
export const metadata: Metadata = pageMetadata({
  description:
    "Pick a meaning and find real names and related words from 21 languages, from Arabic and Hindi to Old English and Welsh.",
  path: "/",
});

// WebSite structured data (Stage 5c). No potentialAction / SearchAction:
// /generate can't take a search from the URL. `<` is escaped as <, as
// the Next JSON-LD guide recommends. The @context is schema.org's vocabulary
// identifier, not a fetched URL (allowed in D3b planning).
const websiteJsonLd = {
  "@context": "https://schema.org",
  "@type": "WebSite",
  name: BRAND.name,
  url: absoluteUrl("/"),
  description: BRAND.tagline,
};

const categories = [
  {
    title: "Established names",
    description: "Real names with meanings connected to your search.",
    style: "border-green-200 bg-green-50",
  },
  {
    title: "Words across languages",
    description: "Words from supported languages that match your ideas.",
    style: "border-yellow-200 bg-yellow-50",
  },
  {
    title: "Generated names (coming later)",
    description: "New names built from your chosen meanings are planned, not yet available.",
    style: "border-blue-200 bg-blue-50",
  },
];

export default function HomePage() {
  return (
    <main className="landing-bg min-h-screen">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(websiteJsonLd).replace(/</g, "\\u003c"),
        }}
      />
      <header className="border-b border-slate-200 bg-white/70 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-5 sm:px-6">
          <Link href="/" className="inline-flex min-h-11 items-center">
            <Wordmark />
          </Link>

          <Link
            href="/generate"
            className="inline-flex min-h-11 items-center rounded-full bg-slate-900 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-700"
          >
            Explore names
          </Link>
        </div>
      </header>

      <section className="mx-auto max-w-6xl px-4 py-20 text-center sm:px-6 sm:py-28">
        <p className="mb-4 text-sm font-semibold uppercase tracking-[0.25em] text-slate-500">
          Names shaped by meaning
        </p>

        <RotatingHeroSentence />

        <p className="mx-auto mt-6 max-w-2xl text-lg leading-8 text-slate-600">
          Explore real names, related words and word roots from 21 languages that
          share the meaning you choose.
        </p>

        <div className="mt-10">
          <Link
            href="/generate"
            className="inline-flex rounded-full bg-slate-900 px-7 py-3.5 font-semibold text-white transition hover:bg-slate-700"
          >
            Start exploring
          </Link>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-4 pb-20 sm:px-6">
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {categories.map((category) => (
            <article
              key={category.title}
              className={`rounded-3xl border p-6 ${category.style}`}
            >
              <h2 className="text-lg font-bold">{category.title}</h2>
              <p className="mt-2 text-sm leading-6 text-slate-600">
                {category.description}
              </p>
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}