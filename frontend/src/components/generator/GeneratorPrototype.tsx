"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  exploreSelectedSenses,
  lookupSenses,
  type SenseOption,
  fetchLanguages,
  type LanguageInfo,
} from "@/lib/api/explore";
import { InfoTip } from "@/components/generator/InfoTip";
import { ResultDetails } from "@/components/generator/ResultDetails";
import {
  languageLabel,
  sortLanguages,
} from "@/features/generator/language-display";
import type {
  GenerationFlavor,
  NamePartKind,
  NameResult,
  ResultCategory,
} from "@/features/generator/types";

type CategoryFilter = ResultCategory | "all";
type SortOption =
  | "az"
  | "za"
  | "shortest"
  | "longest"
  | "relevance"
  | "language";

const categoryOptions: { value: CategoryFilter; label: string }[] = [
  { value: "all", label: "All result types" },
  { value: "established", label: "Established names" },
  { value: "translation", label: "Translations and words" },
  { value: "generated", label: "Generated names" },
];

const categoryStyles: Record<ResultCategory, string> = {
  established: "border-green-200 bg-green-50",
  related: "border-yellow-200 bg-yellow-50",
  translation: "border-yellow-200 bg-yellow-50",
  generated: "border-blue-200 bg-blue-50",
};

const partKindLabels: Record<NamePartKind, string> = {
  root: "Verified root",
  word: "Existing word",
  inspired: "Inspired fragment",
  crafted: "Crafted element",
};

const flavorOptions: {
  value: GenerationFlavor;
  label: string;
}[] = [
  { value: "default", label: "Default" },
  { value: "fantasy", label: "Fantasy" },
  { value: "ancient-inspired", label: "Ancient-inspired" },
  { value: "modern", label: "Modern" },
];

// Phase A6: length filtering removed from the UI. 30 is multi_hop_expand's
// own default; the prototype's 20 was arbitrary. Measured delta recorded in
// IMPORT_PREP_FINDINGS.md (scripts/eval/length_filter_delta.py).
const MIN_LENGTH = 0;
const MAX_LENGTH = 30;

function sortResults(
  results: NameResult[],
  sort: SortOption,
  languageOrder: Map<string, number>
) {
  if (sort === "relevance") {
    // Depth-ascending lineage structure (root first, then each hop level
    // outward) is preserved as the PRIMARY key -- it's already ascending in
    // the server's order, so this is a no-op on that axis. The SECONDARY key
    // is sidebar language rank, which breaks ties across languages at a
    // given depth. Because Array.sort is stable, two same-depth cards from
    // the SAME language keep their original relative order, so the server's
    // parent-grouping within that language's tree is untouched -- only the
    // interleave order across different languages changes.
    return [...results].sort((first, second) => {
      const depthDelta = (first.depth ?? 0) - (second.depth ?? 0);
      if (depthDelta !== 0) return depthDelta;

      const firstIndex =
        languageOrder.get(first.languageCode ?? "") ?? Number.MAX_SAFE_INTEGER;
      const secondIndex =
        languageOrder.get(second.languageCode ?? "") ?? Number.MAX_SAFE_INTEGER;
      return firstIndex - secondIndex;
    });
  }

  if (sort === "language") {
    // Sort on the language index ALONE, with no secondary key. The server
    // returns each tree in lineage order and parallel_expand's interleave
    // preserves within-tree relative order, so a stable sort on language
    // leaves each language's hop-tree ordering intact for free.
    // Array.prototype.sort is stable in every engine since ES2019.
    //
    // What this actually does is un-interleave the parallel expansion back
    // into per-tree groups.
    return [...results].sort((first, second) => {
      const firstIndex =
        languageOrder.get(first.languageCode ?? "") ?? Number.MAX_SAFE_INTEGER;
      const secondIndex =
        languageOrder.get(second.languageCode ?? "") ?? Number.MAX_SAFE_INTEGER;
      return firstIndex - secondIndex;
    });
  }

  return [...results].sort((first, second) => {
    if (sort === "za") {
      return second.name.localeCompare(first.name);
    }

    if (sort === "shortest") {
      return first.name.length - second.name.length;
    }

    if (sort === "longest") {
      return second.name.length - first.name.length;
    }

    return first.name.localeCompare(second.name);
  });
}

function getNameLength(name: string) {
  return Array.from(name.replace(/[-\s']/g, "")).length;
}

function languageSectionId(code: string | null) {
  return `language-section-${code ?? "unknown"}`;
}

// Fallback seed only, for when /languages has not resolved (backend down at
// mount). The authoritative source is LanguageInfo.rtl, which the route
// derives from script in {Arab, Hebr} (languages.py:20) -- today that is
// exactly {ar, he, fa}, so this seed is equivalent, not a second opinion.
// Nothing may read this directly; dirFor() is the single consumer of the
// merged set below.
const RTL_FALLBACK_CODES = ["ar", "he", "fa"];

function hopBadgeLabel(result: NameResult, searchedWord: string): string {
  if (result.matchType === "exact") {
    // Roots: the en root IS the searched meaning; every other tree's root
    // is its cross-language semantic equivalent (roadmap 7a label set).
    return result.languageCode && result.languageCode !== "en"
      ? "Semantic equivalent"
      : "Searched meaning";
  }
  const path = result.path ?? [];
  if (path.length >= 3) {
    // depth >= 2: parent is the second-to-last path step
    return `Related through ${path[path.length - 2].word}`;
  }
  // depth 1 (path = [root, this]) or single-hop expanded (empty path)
  return `Related to ${path[0]?.word ?? searchedWord}`;
}

// Human labels for root provenance (the 5-rung ladder + orchestration,
// Breakdown 4/4.5). Shown as a chip on non-English roots so a weak root
// (fallback, pivoted_root) is diagnosable at a glance in the UI itself.
const rootRungLabels: Record<string, string> = {
  corroborated: "corroborated translation",
  primary: "translation link",
  ili: "wordnet synset",
  llm: "LLM translation",
  pivoted_root: "via English synonym",
  fallback: "vector fallback",
};

export function GeneratorPrototype() {
  const [inputValue, setInputValue] = useState("");
  const [activeSearch, setActiveSearch] = useState("");
  const [category, setCategory] = useState<CategoryFilter>("all");
  const [sort, setSort] = useState<SortOption>("relevance");
  const [availableLanguages, setAvailableLanguages] = useState<LanguageInfo[]>([]);
  const [enabledCodes, setEnabledCodes] = useState<string[]>([]);
  const [breadth, setBreadth] = useState(0);
  const [depth, setDepth] = useState(0);
  const [flavor, setFlavor] = useState<GenerationFlavor>("default");

  const [results, setResults] = useState<NameResult[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [senseOptions, setSenseOptions] = useState<SenseOption[]>([]);
  const [selectedSenseIds, setSelectedSenseIds] = useState<number[]>([]);
  const [isLookingUpSenses, setIsLookingUpSenses] = useState(false);
  const [languagesOpen, setLanguagesOpen] = useState(false);
  const [showDropdown, setShowDropdown] = useState(false);

  const [hoveredBuiltFrom, setHoveredBuiltFrom] = useState<
    Record<string, boolean>
  >({});

  // Purely visual. Deliberately NOT wired to enabledCodes: the language
  // checkboxes are a QUERY control (they change what the next search fetches
  // and discard results); collapsing is a NAVIGATION control over results you
  // already have. Different tools, different state.
  const [collapsedLanguages, setCollapsedLanguages] = useState<Set<string>>(
    new Set()
  );

  // Which results have their details panel open. Shared by the card view
  // (Step 7) and the collapsed view (Step 8) so a row stays open across a
  // view-mode toggle.
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  // Card grid vs. collapsed row list. Purely a rendering choice over the
  // same resultGroups -- switching does not refetch or resort.
  const [viewMode, setViewMode] = useState<"cards" | "list">("cards");

  const hoverTimeouts = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const searchContainerRef = useRef<HTMLDivElement>(null);
  // B1 defect 3: two searches in flight race, and the slower one wins if it
  // lands second. requestIdRef makes staleness detectable; inFlightRef makes
  // the superseded request actually stop costing backend time.
  const requestIdRef = useRef(0);
  const inFlightRef = useRef<AbortController | null>(null);

  // Alphabetical, English pinned first. Recomputed only when /languages
  // resolves, which is once per mount.
  const sortedLanguages = useMemo(
    () => sortLanguages(availableLanguages),
    [availableLanguages]
  );

  // Sidebar order IS the sort order. display_order is NULL on all 21 rows
  // today, so without this, "relevance" falls back to raw import order --
  // which is what this fixes. Decoupled from display_order, which drives the
  // backend's parallel interleave -- a different concern with a different
  // correct answer.
  const languageOrder = useMemo(() => {
    const order = new Map<string, number>();
    sortedLanguages.forEach((lang, index) => order.set(lang.code, index));
    return order;
  }, [sortedLanguages]);

  const visibleResults = useMemo(() => {
    const filteredResults = results.filter((result) => {
      const matchesCategory =
        category === "all" || result.category === category;

      const matchesLanguage =
        !result.languageCode || enabledCodes.includes(result.languageCode);

      const resultLength = getNameLength(result.name);

      const matchesLength =
        resultLength >= MIN_LENGTH && resultLength <= MAX_LENGTH;

      const resultFlavors: GenerationFlavor[] =
        result.flavors ?? ["default"];

      const matchesFlavor =
        result.category !== "generated" ||
        flavor === "default" ||
        resultFlavors.includes(flavor);

      return (
        matchesCategory &&
        matchesLanguage &&
        matchesLength &&
        matchesFlavor
      );
    });

    return sortResults(filteredResults, sort, languageOrder);
  }, [
    category,
    enabledCodes,
    flavor,
    sort,
    results,
    languageOrder,
  ]);

  // Grouping is a property of the SORT, not the view -- so card view and
  // (later) collapsed view read the same array and cannot drift. One group
  // with a null label means "render flat, no headers".
  //
  // Counts come from group.items.length, NOT the response's
  // treeSummaries.nodeCount -- that is the unfiltered server count and would
  // disagree with what the sidebar filters are actually showing.
  const resultGroups = useMemo(() => {
    if (sort !== "language") {
      return [
        {
          code: null as string | null,
          label: null as string | null,
          items: visibleResults,
        },
      ];
    }

    const groups: {
      code: string | null;
      label: string | null;
      items: NameResult[];
    }[] = [];

    for (const result of visibleResults) {
      const code = result.languageCode ?? null;
      const last = groups[groups.length - 1];

      if (last && last.code === code) last.items.push(result);
      else groups.push({ code, label: result.language, items: [result] });
    }

    return groups;
  }, [visibleResults, sort]);

  const rtlCodes = useMemo(() => {
    const set = new Set(RTL_FALLBACK_CODES);
    for (const l of availableLanguages) if (l.rtl) set.add(l.code);
    return set;
  }, [availableLanguages]);

  // enabledCodes === [] sends languageCodes: [] and gets zero trees back.
  // Unreachable by accident before "Select none" existed; one click now.
  const noLanguagesSelected =
    availableLanguages.length > 0 && enabledCodes.length === 0;

  function dirFor(code?: string | null): "rtl" | undefined {
    return code && rtlCodes.has(code) ? "rtl" : undefined;
  }

  useEffect(() => {
    const query = inputValue.trim();

    if (query.length === 0) {
      setSenseOptions([]);
      setShowDropdown(false);
      return;
    }

    const timer = setTimeout(async () => {
      setIsLookingUpSenses(true);
      try {
        const response = await lookupSenses(query, "en");
        setSenseOptions(response.options);
        setShowDropdown(response.options.length > 0);
      } catch {
        setSenseOptions([]);
        setShowDropdown(false);
      } finally {
        setIsLookingUpSenses(false);
      }
    }, 350);

    return () => clearTimeout(timer);
  }, [inputValue]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        searchContainerRef.current &&
        !searchContainerRef.current.contains(event.target as Node)
      ) {
        setShowDropdown(false);
      }
    }

    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    fetchLanguages()
      .then((langs) => {
        setAvailableLanguages(langs);
        setEnabledCodes(langs.map((l) => l.code));
      })
      .catch(() => {
        // Backend down at mount: leave empty; runSearch falls back to the
        // legacy en-only path (languageCodes: null) so search still works.
      });
  }, []);

  function toggleLanguage(code: string) {
    setEnabledCodes((prev) =>
      prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]
    );
  }

  // Extracted so per-language sections can render cards without duplicating
  // the article markup. Body is unchanged from the inline version.
  function renderResultCard(result: NameResult) {
    return (
      <article
        key={result.id}
        className={`rounded-3xl border p-5 ${categoryStyles[result.category]}`}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3
              className="text-2xl font-bold"
              dir={dirFor(result.languageCode)}
            >
              {result.name}
            </h3>

            {/* Phase D. dir="ltr" is explicit, not inherited: a Latin string
                inside an RTL card gets its punctuation reordered otherwise.
                Sized up from typical secondary text (text-base, not text-xs)
                because for zh/ja/ko/ar/he this is the only line on the card
                a non-reader of the script can actually pronounce.
                The equality guard is belt-and-braces -- the backfill already
                refuses to store a value identical to the lemma. */}
            {result.romanization &&
              result.romanization !== result.name && (
                <p
                  dir="ltr"
                  className="mt-1 text-base italic text-slate-500"
                >
                  {result.romanization}
                </p>
              )}

            {result.matchType && (
              <span
                className={`mt-3 inline-flex rounded-full px-3 py-1 text-xs font-semibold shadow-sm ${
                  result.matchType === "exact"
                    ? "bg-white/80 text-slate-700"
                    : "bg-amber-100 text-amber-800"
                }`}
              >
                <span dir="auto">{hopBadgeLabel(result, activeSearch)}</span>
              </span>
            )}
          </div>

          <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-semibold text-slate-700">
            {result.language}
          </span>
        </div>

        <p className="mt-5 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Meaning
        </p>

        <p className="mt-1 font-semibold">{result.meaning}</p>

        <div className="mt-4 flex items-center justify-between gap-3">
          <button
            type="button"
            onClick={() => toggleExpanded(result.id)}
            aria-expanded={expandedIds.has(result.id)}
            className="inline-flex items-center gap-1 text-xs font-bold uppercase tracking-[0.14em] text-slate-500 transition hover:text-slate-800"
          >
            Details
            <span
              aria-hidden
              className={`transition-transform ${
                expandedIds.has(result.id) ? "rotate-90" : ""
              }`}
            >
              ›
            </span>
          </button>

          {/* The one rung that stays on the card face: an LLM-sourced
              root is the only rung whose provenance is a model rather
              than a lexical resource, which is worth knowing without
              a click. */}
          {result.rootRung === "llm" &&
            result.languageCode !== "en" && (
              <span className="rounded-full bg-sky-100 px-2.5 py-0.5 text-[11px] font-semibold text-sky-800">
                LLM
              </span>
            )}
        </div>

        {expandedIds.has(result.id) && (
          <div className="mt-3 rounded-2xl bg-white/70 p-4">
            <ResultDetails result={result} />
          </div>
        )}
        {/* UNREACHABLE TODAY: _hopnode_to_result hardcodes
            category="translation", so this never renders. Kept
            deliberately -- it is the only surviving spec of the
            generated-name UI. Revisit when /generate is wired in. */}
        {result.category === "generated" &&
          result.parts &&
          result.parts.length > 0 && (
            <div
              className="relative mt-5"
              onMouseEnter={() => scheduleBuiltFromShow(result.id)}
              onMouseLeave={() => hideBuiltFrom(result.id)}
            >
              <button
                type="button"
                className="w-full rounded-2xl border border-blue-200 bg-white/80 p-3 text-left shadow-sm transition hover:bg-white"
                aria-expanded={Boolean(hoveredBuiltFrom[result.id])}
              >
                <span className="text-[11px] font-bold uppercase tracking-[0.18em] text-slate-500">
                  Generation logic
                </span>
              </button>

              <div
                className={`absolute inset-x-0 top-full z-20 mt-2 rounded-2xl border border-blue-200 bg-white/95 p-3 shadow-xl transition duration-150 ${
                  hoveredBuiltFrom[result.id]
                    ? "pointer-events-auto opacity-100"
                    : "pointer-events-none opacity-0"
                }`}
              >
                <div className="space-y-3">
                  {result.parts.map((part) => (
                    <div
                      key={`${result.id}-${part.text}`}
                      className="rounded-xl border border-slate-200 bg-white p-3"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div>
                          <p className="font-bold text-slate-900">
                            {part.text}
                          </p>

                          <p className="mt-1 text-xs font-semibold text-slate-500">
                            {part.language}
                          </p>
                        </div>

                        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">
                          {partKindLabels[part.kind]}
                        </span>
                      </div>

                      <p className="mt-3 text-sm text-slate-700">
                        {part.meaning}
                      </p>

                      {part.note && (
                        <p className="mt-1 text-xs leading-5 text-slate-500">
                          {part.note}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
      </article>
    );
  }

  // Collapsed view's row. Shares expandedIds and ResultDetails with
  // renderResultCard so a result stays open across a Cards <-> Collapsed
  // toggle -- same state, same detail content, different chrome.
  function renderResultRow(result: NameResult) {
    return (
      <div
        key={result.id}
        className={`border-b border-slate-100 last:border-b-0 ${categoryStyles[result.category]}`}
      >
        <button
          type="button"
          onClick={() => toggleExpanded(result.id)}
          aria-expanded={expandedIds.has(result.id)}
          className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:brightness-95"
        >
          <span className="min-w-0 flex-1">
            <span
              className="block truncate text-base font-bold text-slate-900"
              dir={dirFor(result.languageCode)}
            >
              {result.name}
            </span>
            {result.romanization &&
              result.romanization !== result.name && (
                <span
                  dir="ltr"
                  className="block truncate text-xs italic text-slate-500"
                >
                  {result.romanization}
                </span>
              )}
          </span>

          {result.matchType && (
            <span
              className="hidden shrink-0 text-xs font-semibold text-slate-500 sm:inline"
              dir="auto"
            >
              {hopBadgeLabel(result, activeSearch)}
            </span>
          )}

          {result.rootRung === "llm" && result.languageCode !== "en" && (
            <span className="shrink-0 rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-semibold text-sky-800">
              LLM
            </span>
          )}

          <span className="shrink-0 rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold text-slate-700">
            {result.language}
          </span>

          <span
            aria-hidden
            className={`shrink-0 text-slate-400 transition-transform ${
              expandedIds.has(result.id) ? "rotate-90" : ""
            }`}
          >
            ›
          </span>
        </button>

        {expandedIds.has(result.id) && (
          <div className="bg-slate-50/70 px-4 pb-4 pt-1">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Meaning
            </p>
            <p className="mt-1 font-semibold text-slate-800">
              {result.meaning}
            </p>

            <div className="mt-3">
              <ResultDetails result={result} />
            </div>
          </div>
        )}
      </div>
    );
  }

  function toggleLanguageCollapsed(code: string | null) {
    if (code === null) return;
    setCollapsedLanguages((current) => {
      const next = new Set(current);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  function collapseAllLanguages() {
    setCollapsedLanguages(
      new Set(
        resultGroups
          .map((group) => group.code)
          .filter((code): code is string => code !== null)
      )
    );
  }

  function jumpToLanguage(code: string | null) {
    // Expand first: scrolling to a collapsed section lands you on a header
    // with nothing under it, which reads as a broken link.
    if (code !== null) {
      setCollapsedLanguages((current) => {
        const next = new Set(current);
        next.delete(code);
        return next;
      });
    }

    // getElementById rather than a ref map: the section list is rebuilt on
    // every search and every collapse toggle, and a ref map would need
    // pruning on each. The id is derived and stable.
    document
      .getElementById(languageSectionId(code))
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function toggleExpanded(resultId: string) {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(resultId)) next.delete(resultId);
      else next.add(resultId);
      return next;
    });
  }

  async function runSearch(senseIds: number[]) {
    if (senseIds.length === 0) return;

    // B1 defect 1: the response envelope does NOT echo the query text (see
    // ExploreSelectedSensesResponse in explore.ts), so "read activeSearch off
    // the response" isn't available. Snapshotting at dispatch and applying
    // after the await is the equivalent -- and it also keeps queryText in the
    // request body consistent with the sense ids sitting beside it, which
    // reading live inputValue does not.
    const queryAtDispatch = inputValue;

    const requestId = ++requestIdRef.current;
    inFlightRef.current?.abort();
    const controller = new AbortController();
    inFlightRef.current = controller;

    setIsLoading(true);
    setErrorMessage(null);

    try {
      const response = await exploreSelectedSenses(
        {
          selectedSenseIds: senseIds,
          queryText: queryAtDispatch,
          breadth,
          depth,
          language: null,
          // All-on by default (roadmap 7b v1). Empty availableLanguages means
          // the /languages fetch failed -- degrade to the legacy en-only path
          // rather than sending [] and getting zero trees.
          languageCodes: availableLanguages.length > 0 ? enabledCodes : null,
          minLength: MIN_LENGTH,
          maxLength: MAX_LENGTH,
        },
        controller.signal
      );

      if (requestId !== requestIdRef.current) return;

      setResults(response.results);
      setActiveSearch(queryAtDispatch);
    } catch (error) {
      // A superseded request is not a failure -- don't surface it.
      if (controller.signal.aborted) return;
      if (requestId !== requestIdRef.current) return;

      console.error(error);
      setResults([]);
      setActiveSearch(queryAtDispatch);
      setErrorMessage(
        "The exploration backend is unavailable. Start FastAPI and search again."
      );
    } finally {
      if (requestId === requestIdRef.current) setIsLoading(false);
    }
  }

  async function handleSenseClick(senseId: number) {
    setShowDropdown(false);
    setSelectedSenseIds([senseId]);
    await runSearch([senseId]);
  }

  async function handleSubmit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();

    if (selectedSenseIds.length === 0) {
      setErrorMessage("Select a meaning from the dropdown first.");
      return;
    }

    await runSearch(selectedSenseIds);
  }

  function scheduleBuiltFromShow(resultId: string) {
    clearTimeout(hoverTimeouts.current[resultId]);
    hoverTimeouts.current[resultId] = setTimeout(() => {
      setHoveredBuiltFrom((current) => ({
        ...current,
        [resultId]: true,
      }));
    }, 550);
  }

  function hideBuiltFrom(resultId: string) {
    clearTimeout(hoverTimeouts.current[resultId]);
    setHoveredBuiltFrom((current) => ({
      ...current,
      [resultId]: false,
    }));
  }

  useEffect(() => {
  const timeouts = hoverTimeouts.current;

  return () => {
    Object.values(timeouts).forEach((timeout) => {
      clearTimeout(timeout);
    });
  };
}, []);

  return (
    <main className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-5">
          <Link href="/" className="text-xl font-bold tracking-tight">
            Namecraft
          </Link>

        </div>
      </header>

      <section className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-7xl px-6 py-8">
          <h1 className="text-3xl font-bold tracking-tight">
            Explore names by meaning
          </h1>

          <form onSubmit={handleSubmit} className="mt-6 max-w-3xl">
            <div className="flex gap-3">
              <div className="relative min-w-0 flex-1" ref={searchContainerRef}>
                <input
                  value={inputValue}
                  onChange={(event) => {
                    setInputValue(event.target.value);
                    // B1 defect 2: a sense id belongs to the word it was chosen
                    // for. Typing invalidates it. Without this, Search re-runs
                    // the OLD sense and the results genuinely are for word A
                    // while the header says word B.
                    setSelectedSenseIds([]);
                  }}
                  onFocus={() => {
                    if (senseOptions.length > 0) setShowDropdown(true);
                  }}
                  placeholder="Enter meanings, such as light, freedom, or sky"
                  className="w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 outline-none transition focus:border-slate-900"
                />

                {isLookingUpSenses && (
                  <span className="absolute right-4 top-1/2 -translate-y-1/2 text-sm text-slate-400">
                    Looking up...
                  </span>
                )}

                {showDropdown && senseOptions.length > 0 && (
                  <div className="absolute inset-x-0 top-full z-50 mt-1 max-h-96 overflow-y-auto rounded-2xl border border-slate-200 bg-white shadow-lg">
                    {senseOptions.map((option) => (
                      <button
                        key={option.senseId}
                        type="button"
                        onClick={() => handleSenseClick(option.senseId)}
                        className="w-full px-4 py-3 text-left transition hover:bg-slate-50 not-last:border-b not-last:border-slate-100"
                      >
                        <span className="block font-semibold text-slate-900">
                          <span dir={dirFor(option.languageCode)}>{option.word}</span>
                          {option.romanization &&
                            option.romanization !== option.word && (
                              <span
                                dir="ltr"
                                className="ml-2 font-normal italic text-slate-500"
                              >
                                {option.romanization}
                              </span>
                            )}
                          {" · "}{option.partOfSpeech}
                          {option.duplicateCount > 1 && (
                            <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-500">
                              ×{option.duplicateCount}
                            </span>
                          )}
                        </span>
                        {option.senseGroup && (
                          <span className="mt-0.5 block text-xs italic text-slate-400">
                            {option.senseGroup}
                          </span>
                        )}
                        <span className="mt-0.5 block text-sm text-slate-600">
                          {option.displayDefinition ||
                            option.definition ||
                            "No definition text stored."}
                        </span>
                        <span className="mt-0.5 block text-xs text-slate-400">
                          Chosen {option.selectionCount} times
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              <button
                type="submit"
                disabled={
                  isLoading ||
                  selectedSenseIds.length === 0 ||
                  noLanguagesSelected
                }
                className="rounded-2xl bg-slate-900 px-6 py-3 font-semibold text-white transition hover:bg-slate-700 disabled:opacity-40"
              >
                {isLoading ? "Searching..." : "Search"}
              </button>
            </div>
          </form>

          {errorMessage && (
            <p className="mt-3 text-sm font-semibold text-red-600">
              {errorMessage}
            </p>
          )}
        </div>
      </section>

      <section className="mx-auto grid max-w-7xl gap-8 px-6 py-8 lg:grid-cols-[260px_1fr]">
        <aside className="flex flex-col gap-6 lg:sticky lg:top-6 lg:max-h-[calc(100vh-3rem)] lg:self-start lg:overflow-y-auto">
          <div className="rounded-3xl border border-slate-200 bg-white p-5">
          <h2 className="font-bold">Filters</h2>

          <label className="mt-5 block text-sm font-semibold text-slate-700">
            Result type
          </label>

          <select
            value={category}
            onChange={(event) =>
              setCategory(event.target.value as CategoryFilter)
            }
            className="mt-2 w-full rounded-xl border border-slate-300 bg-white px-3 py-2"
          >
            {categoryOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>

          <div className="mt-5">
            <button
              type="button"
              onClick={() => setLanguagesOpen((open) => !open)}
              aria-expanded={languagesOpen}
              aria-controls="language-panel"
              className="flex w-full items-center justify-between gap-2 text-left text-sm font-semibold text-slate-700"
            >
              <span>
                Languages
                <span className="ml-2 font-normal tabular-nums text-slate-500">
                  {enabledCodes.length} of {availableLanguages.length}
                </span>
              </span>
              <span
                aria-hidden
                className={`text-slate-400 transition-transform ${
                  languagesOpen ? "rotate-90" : ""
                }`}
              >
                ›
              </span>
            </button>

            {languagesOpen && (
              <div id="language-panel" className="mt-3">
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() =>
                      setEnabledCodes(availableLanguages.map((l) => l.code))
                    }
                    className="rounded-lg border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-600 transition hover:bg-slate-50"
                  >
                    Select all
                  </button>
                  <button
                    type="button"
                    onClick={() => setEnabledCodes([])}
                    className="rounded-lg border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-600 transition hover:bg-slate-50"
                  >
                    Select none
                  </button>
                </div>

                {noLanguagesSelected && (
                  <p className="mt-2 rounded-lg bg-amber-50 px-2 py-1.5 text-xs font-semibold text-amber-800">
                    No languages selected — search is disabled.
                  </p>
                )}

                <div className="mt-2 max-h-72 space-y-1 overflow-y-auto pr-1">
                  {sortedLanguages.map((lang) => (
                    <label
                      key={lang.code}
                      className="flex items-center gap-2 text-sm text-slate-700"
                    >
                      <input
                        type="checkbox"
                        checked={enabledCodes.includes(lang.code)}
                        onChange={() => toggleLanguage(lang.code)}
                      />
                      <span dir="auto">{languageLabel(lang)}</span>
                    </label>
                  ))}
                </div>

                <p className="mt-2 text-xs text-slate-400">
                  Unchecking hides results instantly; the next search skips
                  those languages entirely.
                </p>
              </div>
            )}
          </div>

          <div className="mt-5">
            <span className="block text-sm font-semibold text-slate-700">
              Expansion
              <InfoTip label="Expansion">
                <strong>Breadth</strong> is related words per hop.{" "}
                <strong>Depth</strong> is how many hops outward the walk
                travels. Zero on either returns the exact meaning only.
              </InfoTip>
            </span>

            <div className="mt-3">
              <div className="flex items-baseline justify-between">
                <label
                  htmlFor="breadth-slider"
                  className="text-xs font-semibold text-slate-500"
                >
                  Breadth
                </label>
                <span className="text-xs font-bold tabular-nums text-slate-900">
                  {breadth}
                </span>
              </div>
              <input
                id="breadth-slider"
                type="range"
                min={0}
                max={3}
                step={1}
                value={breadth}
                onChange={(event) => setBreadth(Number(event.target.value))}
                list="expansion-ticks"
                className="mt-1 w-full accent-slate-900"
              />
              <div className="flex justify-between px-0.5 text-[10px] tabular-nums text-slate-400">
                <span>0</span>
                <span>1</span>
                <span>2</span>
                <span>3</span>
              </div>
            </div>

            <div className="mt-3">
              <div className="flex items-baseline justify-between">
                <label
                  htmlFor="depth-slider"
                  className="text-xs font-semibold text-slate-500"
                >
                  Depth
                </label>
                <span className="text-xs font-bold tabular-nums text-slate-900">
                  {depth}
                </span>
              </div>
              <input
                id="depth-slider"
                type="range"
                min={0}
                max={3}
                step={1}
                value={depth}
                onChange={(event) => setDepth(Number(event.target.value))}
                list="expansion-ticks"
                className="mt-1 w-full accent-slate-900"
              />
              <div className="flex justify-between px-0.5 text-[10px] tabular-nums text-slate-400">
                <span>0</span>
                <span>1</span>
                <span>2</span>
                <span>3</span>
              </div>
            </div>

            <datalist id="expansion-ticks">
              <option value="0" />
              <option value="1" />
              <option value="2" />
              <option value="3" />
            </datalist>
          </div>

          <span className="mt-5 block text-sm font-semibold text-slate-400">
            Generation flavor
            <InfoTip label="Generation flavor">
              Flavors will shape newly generated names. Generated names are not
              built yet, so this control does nothing today.
            </InfoTip>
          </span>

          <select
            value={flavor}
            onChange={(event) =>
              setFlavor(event.target.value as GenerationFlavor)
            }
            disabled
            aria-disabled="true"
            title="Generated names are not implemented yet"
            className="mt-2 w-full cursor-not-allowed rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-400"
          >
            {flavorOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          </div>

          {sort === "language" && resultGroups.length > 1 && (
            <div className="rounded-3xl border border-slate-200 bg-white p-5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-bold uppercase tracking-[0.16em] text-slate-600">
                  Jump to
                </span>

                <div className="flex shrink-0 gap-1.5">
                  <button
                    type="button"
                    onClick={collapseAllLanguages}
                    className="rounded-lg border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-600 transition hover:bg-slate-50"
                  >
                    Collapse all
                  </button>
                  <button
                    type="button"
                    onClick={() => setCollapsedLanguages(new Set())}
                    className="rounded-lg border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-600 transition hover:bg-slate-50"
                  >
                    Expand all
                  </button>
                </div>
              </div>

              <div className="mt-3 space-y-0.5">
                {resultGroups.map((group, index) => (
                  <button
                    key={group.code ?? `unknown-${index}`}
                    type="button"
                    onClick={() => jumpToLanguage(group.code)}
                    className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 transition hover:bg-slate-50"
                  >
                    <span dir="auto">{group.label ?? "Unknown"}</span>
                    <span className="tabular-nums text-xs font-semibold text-slate-400">
                      {group.items.length}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </aside>

        <div>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p className="text-sm font-semibold uppercase tracking-[0.2em] text-slate-500">
                Results
              </p>

              <h2 className="mt-1 text-2xl font-bold">
                {activeSearch
                  ? `${visibleResults.length} matches for “${activeSearch}”`
                  : "Search to begin"}
              </h2>
            </div>

            <div className="flex items-end gap-3">
              <div>
                <span className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
                  View
                </span>

                <div className="mt-1 inline-flex overflow-hidden rounded-xl border border-slate-300">
                  <button
                    type="button"
                    onClick={() => setViewMode("cards")}
                    aria-pressed={viewMode === "cards"}
                    className={`px-3 py-2 text-sm font-semibold transition ${
                      viewMode === "cards"
                        ? "bg-slate-900 text-white"
                        : "bg-white text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    Cards
                  </button>
                  <button
                    type="button"
                    onClick={() => setViewMode("list")}
                    aria-pressed={viewMode === "list"}
                    className={`border-l border-slate-300 px-3 py-2 text-sm font-semibold transition ${
                      viewMode === "list"
                        ? "bg-slate-900 text-white"
                        : "bg-white text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    Collapsed
                  </button>
                </div>
              </div>

              <div>
                <label
                  htmlFor="sort-select"
                  className="block text-xs font-semibold uppercase tracking-wide text-slate-500"
                >
                  Sort By
                  <InfoTip label="Sort By">
                    <strong>Hop order</strong> walks the tree: the searched
                    word, then each hop outward, grouped under the word it
                    expanded from. Recommended for depth-based expansion.
                  </InfoTip>
                </label>

                <select
                  id="sort-select"
                  value={sort}
                  onChange={(event) =>
                    setSort(event.target.value as SortOption)
                  }
                  className="mt-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm"
                >
                  <option value="relevance">Hop order (tree)</option>
                  <option value="language">Language (grouped)</option>
                  <option value="az">First Letter: A–Z</option>
                  <option value="za">First Letter: Z–A</option>
                  <option value="shortest">Shortest first</option>
                  <option value="longest">Longest first</option>
                </select>
              </div>
            </div>
          </div>

          {visibleResults.length === 0 ? (
            <div className="mt-6 rounded-3xl border border-dashed border-slate-300 bg-white p-10 text-center">
              <h3 className="font-bold">
                {activeSearch ? "No results found" : "Nothing searched yet"}
              </h3>
              <p className="mt-2 text-sm text-slate-600">
                {activeSearch
                  ? "Try raising Breadth or Depth, or enabling more languages in Filters."
                  : "Enter a meaning above and choose a sense from the dropdown."}
              </p>
            </div>
          ) : (
            <div className="mt-6 space-y-6">
              {resultGroups.map((group, index) => {
                const isCollapsed =
                  group.code !== null && collapsedLanguages.has(group.code);

                return (
                  <section
                    key={group.code ?? `group-${index}`}
                    id={languageSectionId(group.code)}
                    // Small top margin so a jumped-to header doesn't land
                    // flush against the viewport edge.
                    className="scroll-mt-6"
                  >
                    {group.label && (
                      <button
                        type="button"
                        onClick={() => toggleLanguageCollapsed(group.code)}
                        aria-expanded={!isCollapsed}
                        className="flex w-full items-center gap-2 border-b border-slate-200 pb-2 text-left"
                      >
                        <span className="text-sm font-bold uppercase tracking-[0.16em] text-slate-600">
                          {group.label}
                        </span>
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold tabular-nums text-slate-600">
                          {group.items.length}
                        </span>
                        <span
                          aria-hidden
                          className={`ml-auto text-slate-400 transition-transform ${
                            isCollapsed ? "" : "rotate-90"
                          }`}
                        >
                          ›
                        </span>
                      </button>
                    )}

                    {!isCollapsed &&
                      (viewMode === "cards" ? (
                        <div
                          className={`grid gap-4 md:grid-cols-2 xl:grid-cols-3 ${
                            group.label ? "mt-4" : ""
                          }`}
                        >
                          {group.items.map((result) =>
                            renderResultCard(result)
                          )}
                        </div>
                      ) : (
                        <div
                          className={`overflow-hidden rounded-3xl border border-slate-200 bg-white ${
                            group.label ? "mt-4" : ""
                          }`}
                        >
                          {group.items.map((result) => renderResultRow(result))}
                        </div>
                      ))}
                  </section>
                );
              })}
            </div>
          )}
        </div>
      </section>
    </main>
  );
}