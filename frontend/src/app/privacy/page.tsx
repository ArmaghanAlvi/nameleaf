import type { Metadata } from "next";
import Link from "next/link";
import { Wordmark } from "@/components/brand/Wordmark";
import { LEGAL_LAST_UPDATED } from "@/lib/legal";

// Text approved in D2 (D-2.2), verbatim; only the markup is added here.
export const metadata: Metadata = {
  title: "Privacy · NameLeaf",
};

const h2 = "mt-10 text-xl font-bold tracking-tight";
const p = "mt-3 leading-7 text-slate-700";
const ul = "mt-3 list-disc space-y-2 pl-5 leading-7 text-slate-700";
const mail =
  "underline decoration-slate-300 underline-offset-2 hover:decoration-slate-600";

function Contact() {
  return (
    <a href="mailto:contact@nameleaf.app" className={mail}>
      contact@nameleaf.app
    </a>
  );
}

export default function PrivacyPage() {
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
        <h1 className="text-3xl font-bold tracking-tight">Privacy</h1>

        <p className="mt-3 text-sm text-slate-500">Last updated: {LEGAL_LAST_UPDATED}</p>

        <p className={p}>
          NameLeaf is a free website for finding names by meaning, run by Armaghan
          Alvi. This page explains what happens to information when you use it.
        </p>

        <h2 className={h2}>The short version</h2>
        <ul className={ul}>
          <li>No accounts, no cookies, no ads, no analytics or tracking.</li>
          <li>Your searches aren&apos;t saved, and the server doesn&apos;t log IP addresses.</li>
          <li>Your browser only talks to nameleaf.app.</li>
        </ul>

        <h2 className={h2}>When you search</h2>
        <p className={p}>
          The word you type and the meaning you pick are sent to the NameLeaf server,
          which uses them to build your results. They aren&apos;t saved to the database,
          aren&apos;t written to the server&apos;s logs, and aren&apos;t sent to any AI service or
          other company.
        </p>
        <p className={p}>
          NameLeaf&apos;s code can record which meanings people pick, to order the meaning
          list better. That feature is switched off. If it&apos;s ever switched on, this
          page will be updated first to say what&apos;s stored and for how long.
        </p>

        <h2 className={h2}>Technical information</h2>
        <p className={p}>
          To stop any one visitor from overloading the service, the server keeps
          recent visitors&apos; IP addresses in its memory to apply rate limits.
          They&apos;re never written to disk, and they&apos;re cleared regularly and whenever
          the server restarts.
        </p>
        <p className={p}>
          When something goes wrong, the server&apos;s error logs can record technical
          details of the failed request. These logs are kept for a limited time and
          overwritten automatically.
        </p>
        <p className={p}>
          When an error occurs, a technical report is sent to Sentry, our
          error-monitoring service. A report can include the request that failed,
          such as the word you searched, but not your IP address.
        </p>

        <h2 className={h2}>Cookies and local storage</h2>
        <p className={p}>
          NameLeaf doesn&apos;t set cookies or store anything in your browser. Fonts,
          images and scripts all come from nameleaf.app itself.
        </p>

        <h2 className={h2}>Services we use</h2>
        <ul className={ul}>
          <li>Oracle Cloud hosts the server.</li>
          <li>
            Cloudflare provides the domain registration and DNS, and forwards email
            sent to <Contact />.
          </li>
          <li>Google (Gmail) receives forwarded email.</li>
          <li>Sentry receives error reports.</li>
        </ul>

        <h2 className={h2}>If you email us</h2>
        <p className={p}>
          Your email address and message are used only to reply, and deleted when
          they&apos;re no longer needed.
        </p>

        <h2 className={h2}>Children</h2>
        <p className={p}>
          NameLeaf is for a general audience. It doesn&apos;t collect personal information
          from anyone, including children under 13.
        </p>

        <h2 className={h2}>Your choices</h2>
        <p className={p}>
          Because NameLeaf doesn&apos;t keep personal information about your searches,
          there&apos;s usually nothing to access or delete. Questions: <Contact />.
        </p>

        <h2 className={h2}>Changes</h2>
        <p className={p}>
          If this page changes, the date above changes too. Anything that changes
          what&apos;s collected, such as ads, accounts or usage statistics, will be
          described here before it starts.
        </p>
      </article>
    </main>
  );
}
