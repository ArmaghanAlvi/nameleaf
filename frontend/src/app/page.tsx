import Link from "next/link";
import { Wordmark } from "@/components/brand/Wordmark";
import { RotatingHeroSentence } from "@/components/landing/RotatingHeroSentence";

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
    title: "Generated names",
    description: "New names crafted from your selected meanings.",
    style: "border-blue-200 bg-blue-50",
  },
];

export default function HomePage() {
  return (
    <main className="landing-bg min-h-screen">
      <header className="border-b border-slate-200 bg-white/70 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
          <Link href="/">
            <Wordmark />
          </Link>

          <Link
            href="/generate"
            className="rounded-full bg-slate-900 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-700"
          >
            Explore names
          </Link>
        </div>
      </header>

      <section className="mx-auto max-w-6xl px-6 py-20 text-center sm:py-28">
        <p className="mb-4 text-sm font-semibold uppercase tracking-[0.25em] text-slate-500">
          Names shaped by meaning
        </p>

        <RotatingHeroSentence />

        <p className="mx-auto mt-6 max-w-2xl text-lg leading-8 text-slate-600">
          Explore established names, words across languages, and newly crafted names
          <br />
          inspired by languages from around the world.
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

      <section className="mx-auto max-w-6xl px-6 pb-20">
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