"use client";

import type { NameResult } from "@/features/generator/types";

/**
 * ⚠️ HAND-MAINTAINED MIRROR of the rung strings emitted by
 * backend/app/services/root_selection.py and parallel_expansion.py. Nothing
 * enforces the correspondence: `ili_override` was added to the resolver
 * (root_selection.py:438) and never added here, so it rendered as the bare
 * snake_case string on cards for an entire release cycle.
 *
 * Emission sites, verified 2026-08-14 -- all seven are covered below:
 *   corroborated, primary  root_selection.py:383
 *   ili_override           root_selection.py:438
 *   ili                    root_selection.py:440
 *   llm                    root_selection.py:471
 *   fallback               root_selection.py:503
 *   pivoted_root           parallel_expansion.py:247
 *
 * If a raw snake_case string ever appears in this panel, a rung was added
 * upstream and needs a label here.
 */
export const rootRungLabels: Record<string, string> = {
  corroborated: "corroborated translation",
  primary: "translation link",
  ili: "WordNet synset",
  ili_override: "WordNet synset (override)",
  llm: "LLM translation",
  pivoted_root: "via English synonym",
  fallback: "vector fallback",
};

/**
 * ⚠️ HAND-MAINTAINED MIRROR of MEANING_CHANNELS in
 * backend/app/models/semantic.py (five values as of Stage 6). Same hazard as
 * rootRungLabels above: nothing enforces the correspondence, so a channel
 * added upstream renders as a bare SCREAMING_SNAKE string here.
 */
export const meaningChannelLabels: Record<string, string> = {
  GLOSS_MEANING: "dictionary gloss",
  ETYM_MARKER: "etymology",
  ETYM_QUOTED: "quoted etymology",
  HOMOGRAPH: "identical word",
  EQUIV_PROPAGATED: "inherited from an equivalent name",
};

/**
 * Everything relocated off the card face by B3. Shared by the card view's
 * disclosure and the collapsed view's expansion row, so the two cannot drift.
 *
 * The rung chip is NOT decoration: it is the only surface signal that a root
 * is weak. A `fallback` root (vector nearest-neighbour, no hard evidence) and
 * a `corroborated` root (translation link plus shared ILI) are otherwise
 * visually identical, and under the zero-review constraint this is the only
 * place that difference is legible. One click deep is fine; absent is not.
 */
export function ResultDetails({ result }: { result: NameResult }) {
  const green = result.green;
  const showRung = Boolean(result.rootRung) && result.languageCode !== "en";
  const showPivot = result.provenance === "pivoted";

  return (
    <div className="text-sm text-slate-600">
      {green && (
        <div className="mb-3 flex flex-wrap gap-2">
          <span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-semibold text-emerald-900 shadow-sm">
            {green.homographConfidence === "spelling_only"
              ? "same spelling only"
              : green.meaningChannel
                ? `meaning: ${
                    meaningChannelLabels[green.meaningChannel] ??
                    green.meaningChannel
                  }`
                : "meaning not recorded"}
          </span>
          {green.matchTier === 0 && (
            <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-slate-600 shadow-sm">
              direct match
            </span>
          )}
        </div>
      )}

      {(showRung || showPivot) && (
        <div className="mb-3 flex flex-wrap gap-2">
          {showRung && (
            <span className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-slate-600 shadow-sm">
              root: {rootRungLabels[result.rootRung!] ?? result.rootRung}
            </span>
          )}

          {showPivot && (
            <span className="rounded-full bg-violet-100 px-3 py-1 text-xs font-semibold text-violet-800 shadow-sm">
              via English pivot
            </span>
          )}
        </div>
      )}

      <p className="leading-6">{result.explanation}</p>

      {/* Amendment 7 made visible. A card found through the hidden English
          pass has a REAL trigger that is not on screen; saying so is the
          honest alternative to silently attributing it to some displayed
          word. */}
      {green && !green.triggerVisible && (
        <p className="mt-2 text-xs italic text-slate-500">
          Found through the English word \u201c{green.triggerWord}\u201d, which
          is not among the displayed languages.
        </p>
      )}
    </div>
  );
}