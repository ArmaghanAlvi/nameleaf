"use client"; // Error boundaries must be Client Components.

import Link from "next/link";
import { useEffect } from "react";

// App error boundary (trace-E3). Props per Next 16.4's
// docs/01-app/03-api-reference/03-file-conventions/error.md: `retry`, not
// the older `reset`. Never shows the error's text or stack to the visitor.
export default function Error({
  error,
  retry,
}: {
  error: unknown;
  retry: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="bg-stone-50">
      <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
        <h1 className="text-3xl font-bold tracking-tight">Something went wrong</h1>
        <p className="mt-4 leading-7 text-slate-600">
          Please try again. If it keeps happening, email{" "}
          <a
            href="mailto:contact@nameleaf.app"
            className="underline decoration-slate-300 underline-offset-2 hover:decoration-slate-600"
          >
            contact@nameleaf.app
          </a>
          .
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <button
            type="button"
            onClick={() => retry()}
            className="inline-flex min-h-11 items-center rounded-full bg-slate-900 px-5 font-semibold text-white transition hover:bg-slate-700"
          >
            Try again
          </button>
          <Link
            href="/"
            className="inline-flex min-h-11 items-center rounded-full border border-slate-300 bg-white px-5 font-semibold text-slate-900 transition hover:bg-slate-100"
          >
            Home
          </Link>
        </div>
      </div>
    </main>
  );
}
