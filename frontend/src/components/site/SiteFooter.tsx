import Link from "next/link";

const item =
  "inline-flex min-h-11 items-center px-2 text-sm text-slate-600 hover:text-slate-900";

// On every page (root layout): sources credit, legal pages, contact.
export function SiteFooter() {
  return (
    <footer className="border-t border-slate-200 bg-white">
      <nav
        aria-label="Footer"
        className="mx-auto flex max-w-7xl flex-wrap items-center justify-center gap-x-4 gap-y-1 px-4 py-4 sm:px-6"
      >
        <Link href="/sources" className={item}>
          Sources
        </Link>
        <Link href="/privacy" className={item}>
          Privacy
        </Link>
        <Link href="/terms" className={item}>
          Terms
        </Link>
        <a href="mailto:contact@nameleaf.app" className={item}>
          contact@nameleaf.app
        </a>
      </nav>
    </footer>
  );
}
