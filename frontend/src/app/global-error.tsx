"use client"; // Error boundaries must be Client Components.

import "./globals.css";

// Errors thrown in the root layout itself. This replaces the root layout when
// active, so it renders its own <html>/<body> and imports the global styles
// (docs/01-app/03-api-reference/03-file-conventions/error.md, global-error).
// Same copy as app/error.tsx; never shows the error's text or stack.
export default function GlobalError({
  error,
  retry,
}: {
  error: unknown;
  retry: () => void;
}) {
  console.error(error);
  return (
    <html lang="en">
      <body className="bg-stone-50 text-slate-900 antialiased">
        <title>NameLeaf</title>
        <main className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
          <h1 className="text-3xl font-bold tracking-tight">Something went wrong</h1>
          <p className="mt-4 leading-7 text-slate-600">
            Please try again. If it keeps happening, email{" "}
            <a
              href="mailto:contact@nameleaf.app"
              className="underline decoration-slate-300 underline-offset-2"
            >
              contact@nameleaf.app
            </a>
            .
          </p>
          <button
            type="button"
            onClick={() => retry()}
            className="mt-8 inline-flex min-h-11 items-center rounded-full bg-slate-900 px-5 font-semibold text-white transition hover:bg-slate-700"
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
