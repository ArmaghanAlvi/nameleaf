"use client";

import { useState } from "react";
import type {
  GreenCardPayload,
  GreenVariant,
} from "@/features/generator/types";

/**
 * 9c: two groupings, never one list.
 *
 * The data distinguishes them at the source and collapsing that would
 * discard information the user wants -- but more importantly the two come
 * from DIFFERENT PLACES. Same-language variants are cluster members;
 * cross-language cognates cannot be, because build_components unions
 * same-language edges only (that filter IS the Stage 5b containment rule).
 * Rendering them as one list would imply a symmetry the data does not have.
 *
 * It also visually contains whatever cross-language chaining survives
 * Stage 5: cognates sit in their own bounded box rather than swelling the
 * variant list.
 */

const RTL_CODES = new Set(["ar", "he", "fa"]);

function dirFor(code?: string | null): "rtl" | undefined {
  return code && RTL_CODES.has(code) ? "rtl" : undefined;
}

function VariantRow({ variant }: { variant: GreenVariant }) {
  return (
    <li className="flex items-baseline justify-between gap-2 py-1">
      <span className="min-w-0">
        <span
          className="font-semibold text-slate-800"
          dir={dirFor(variant.languageCode)}
        >
          {variant.name}
        </span>
        {variant.romanization && variant.romanization !== variant.name && (
          <span dir="ltr" className="ml-2 text-xs italic text-slate-500">
            {variant.romanization}
          </span>
        )}
      </span>
      <span className="shrink-0 text-[11px] font-semibold text-slate-500">
        {variant.relationshipType}
        {variant.isCrossLanguage ? ` \u00b7 ${variant.language}` : ""}
      </span>
    </li>
  );
}

function Group({
  title,
  shown,
  total,
}: {
  title: string;
  shown: GreenVariant[];
  total: number;
}) {
  if (shown.length === 0) return null;
  return (
    <div className="mt-2 first:mt-0">
      <p className="text-[10px] font-bold uppercase tracking-[0.16em] text-slate-400">
        {title}
        {total > 0 ? ` \u00b7 ${total}` : ""}
      </p>
      {/* Scrollable rather than truncated, and as of Step 2 the backend caps
          sit above every census maximum, so `shown` is normally the whole
          family. The count in the header above is what makes the scroll
          depth legible BEFORE scrolling -- a 40-row list inside a fixed
          window otherwise reads as "about six forms". The "not shown" line
          below stays: it is dead in practice now, and that is exactly why
          deleting it would be dangerous -- it is the honest fallback if a
          future census finds a family above the new ceiling. */}
      <ul className="mt-1 max-h-80 overflow-y-auto pr-1">
        {shown.map((variant) => (
          <VariantRow
            key={`${variant.languageCode}-${variant.name}`}
            variant={variant}
          />
        ))}
      </ul>
      {total > shown.length && (
        <p className="mt-1 text-[11px] italic text-slate-400">
          and {total - shown.length} more not shown
        </p>
      )}
    </div>
  );
}

export function VariantDropdown({ green }: { green: GreenCardPayload }) {
  const [open, setOpen] = useState(false);
  const total = green.variantTotal + green.cognateTotal;
  // Renders NOTHING when the name has no family. An empty disclosure that
  // opens onto nothing reads as broken.
  if (total === 0) return null;

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-xs font-bold uppercase tracking-[0.14em] text-emerald-700 transition hover:text-emerald-900"
      >
        {total} related {total === 1 ? "form" : "forms"}
        <span
          aria-hidden
          className={`transition-transform ${open ? "rotate-90" : ""}`}
        >
          {"\u203a"}
        </span>
      </button>

      {open && (
        <div className="mt-2 rounded-2xl border border-emerald-200 bg-white/80 p-3">
          <Group
            title="Variants and diminutives"
            shown={green.variants}
            total={green.variantTotal}
          />
          <Group
            title="Cognates in other languages"
            shown={green.cognates}
            total={green.cognateTotal}
          />
        </div>
      )}
    </div>
  );
}