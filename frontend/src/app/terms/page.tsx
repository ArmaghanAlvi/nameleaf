import type { Metadata } from "next";
import Link from "next/link";
import { Wordmark } from "@/components/brand/Wordmark";
import { pageMetadata } from "@/lib/site";
import { LEGAL_LAST_UPDATED } from "@/lib/legal";

// Text approved in D2 (D-2.2), verbatim; only the markup is added here.
export const metadata: Metadata = pageMetadata({
  title: "Terms",
  description:
    "The terms for using NameLeaf: a free service whose results are automatic suggestions, with data reusable under its sources' licenses.",
  path: "/terms",
});

const h2 = "mt-10 text-xl font-bold tracking-tight";
const p = "mt-3 leading-7 text-slate-700";
const link =
  "underline decoration-slate-300 underline-offset-2 hover:decoration-slate-600";

export default function TermsPage() {
  return (
    <main className="min-h-screen bg-stone-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-5 sm:px-6">
          <Link href="/" className="inline-flex min-h-11 items-center">
            <Wordmark />
          </Link>
        </div>
      </header>

      <article className="mx-auto max-w-3xl px-4 py-12 sm:px-6">
        <h1 className="text-3xl font-bold tracking-tight">Terms of use</h1>

        <p className="mt-3 text-sm text-slate-500">Last updated: {LEGAL_LAST_UPDATED}</p>

        <p className={p}>
          By using NameLeaf you agree to these terms. If you don&apos;t agree, please
          don&apos;t use the site.
        </p>

        <h2 className={h2}>The service</h2>
        <p className={p}>
          NameLeaf is free and has no accounts. It may change, pause or stop at any
          time.
        </p>

        <h2 className={h2}>Results are suggestions</h2>
        <p className={p}>
          Results are produced automatically from open dictionary data and automated
          matching. Meanings, origins, genders and spellings can be incomplete or
          wrong, and a word&apos;s meaning in one language or culture may not carry over
          to another. Check anything important with a reliable source before relying
          on it, especially before naming a person.
        </p>

        <h2 className={h2}>Data and reuse</h2>
        <p className={p}>
          The words, meanings and names on NameLeaf come from the sources listed on
          the{" "}
          <Link href="/sources" className={link}>
            Sources
          </Link>{" "}
          page, under those sources&apos; licenses. These terms don&apos;t limit anything
          those licenses allow; follow them when you reuse the data. NameLeaf&apos;s code
          is open source under the MIT License.
        </p>

        <h2 className={h2}>Fair use of the service</h2>
        <p className={p}>
          Please don&apos;t send automated traffic that overloads the site, and don&apos;t try
          to get around its rate limits or security.
        </p>

        <h2 className={h2}>No warranty</h2>
        <p className={p}>
          NameLeaf is provided &quot;as is&quot;, without warranties of any kind, to the
          extent the law allows.
        </p>

        <h2 className={h2}>Limitation of liability</h2>
        <p className={p}>
          To the extent the law allows, the operator isn&apos;t liable for any loss or
          damage arising from using NameLeaf or its results.
        </p>

        <h2 className={h2}>Changes</h2>
        <p className={p}>
          These terms may be updated; the date above shows the current version. Using
          the site after a change means you accept the updated terms.
        </p>

        <h2 className={h2}>Contact</h2>
        <p className={p}>
          <a href="mailto:contact@nameleaf.app" className={link}>
            contact@nameleaf.app
          </a>
        </p>
      </article>
    </main>
  );
}
