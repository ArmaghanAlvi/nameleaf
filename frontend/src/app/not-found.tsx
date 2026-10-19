import Link from "next/link";
import { Wordmark } from "@/components/brand/Wordmark";

// Root not-found: also serves every unmatched URL (Next 16 docs,
// file-conventions/not-found.md).
export default function NotFound() {
  return (
    <main className="bg-stone-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-5 sm:px-6">
          <Link href="/" className="inline-flex min-h-11 items-center">
            <Wordmark />
          </Link>
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
        <h1 className="text-3xl font-bold tracking-tight">Page not found</h1>
        <p className="mt-4 leading-7 text-slate-600">
          That page doesn&apos;t exist or has moved.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Link
            href="/"
            className="inline-flex min-h-11 items-center rounded-full border border-slate-300 bg-white px-5 font-semibold text-slate-900 transition hover:bg-slate-100"
          >
            Home
          </Link>
          <Link
            href="/generate"
            className="inline-flex min-h-11 items-center rounded-full bg-slate-900 px-5 font-semibold text-white transition hover:bg-slate-700"
          >
            Search names
          </Link>
        </div>
      </div>
    </main>
  );
}
