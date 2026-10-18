import { BRAND, MARK } from "@/lib/brand";

// The mark plus the product name, shared by every page header. Colour comes
// from the --brand CSS variable (globals.css); the geometry from lib/brand.ts.
export function Wordmark() {
  return (
    <span
      className="inline-flex items-center gap-2 text-xl font-bold tracking-tight"
      style={{ color: "var(--brand)" }}
    >
      <svg
        viewBox={MARK.viewBox}
        className="h-6 w-6 shrink-0"
        fill="currentColor"
        aria-hidden="true"
      >
        <path fillRule="evenodd" d={MARK.leaf} />
        <path d={MARK.stem} />
      </svg>
      {BRAND.name}
    </span>
  );
}
