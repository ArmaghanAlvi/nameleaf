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
import { VariantDropdown } from "@/components/generator/VariantDropdown";
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
  | "language"
  // 9d. A SORT, not a filter: CategoryFilter already exists and removes;
  // this groups. Green first, then words, with the third section reserved
  // for generated names.
  | "cardtype";

const categoryOptions: { value: CategoryFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "established", label: "Established names" },
  { value: "translation", label: "Related meanings" },
  { value: "generated", label: "Generated names" },
];

// The Record<ResultCategory, string> type is load-bearing: adding a category
// without a style here is a compile error, which is exactly what should have
// happened when `ili_override` was added to the rung labels and wasn't
// (see ResultDetails.tsx's mirror warning).
const categoryStyles: Record<ResultCategory, string> = {
  established: "border-emerald-300 bg-emerald-50",
  // Half yellow (the WORD), half green (the NAME), with a narrow blend band
  // at the midpoint. Three deliberate choices, each fixing a specific
  // failure of the pre-Breakdown-F string:
  //   * `-r`, not `-br`: the tag row below reads left-to-right as
  //     word-tag-then-name-tag, and the background has to agree with it. A
  //     diagonal ramp agrees with neither axis.
  //   * amber FIRST: the word is the dominant half.
  //   * NO `via-`: a midpoint colour stop is what pinned the old gradient
  //     solid green across the entire first half, which is why the card
  //     never looked 50/50.
  // `from-40% / to-60%` sets the blend band width -- widen to 30/70 for a
  // softer transition, narrow to 45/55 for a harder split.
  // Border is neutral rather than emerald: a green ring around a half-amber
  // card re-asserts "this is a name" and fights the split it sits on.
  "word-established":
    "border-slate-300 bg-gradient-to-r from-amber-50 from-40% to-emerald-50 to-60%",
  related: "border-yellow-200 bg-yellow-50",
  translation: "border-yellow-200 bg-yellow-50",
  generated: "border-blue-200 bg-blue-50",
};

const nameTypeLabels: Record <
  NonNullable<NameResult["green"]>["nameType"],
  string
> = {
  given: "Given name",
  surname: "Surname",
  patronymic: "Patronymic",
};

// "u" (unknown) renders as nothing rather than "unknown" -- blank over wrong,
// the same rule the backend applies to meanings.
const genderLabels: Record <
  NonNullable<NameResult["green"]>["gender"],
  string
> = {
  m: "masculine",
  f: "feminine",
  x: "unisex",
  u: "",
};

// 14e. Two shapes, two sentences. "from Sanskrit" says borrowed;
// "Ukrainian rendering" says this spelling is an English way of writing a
// Ukrainian name. Collapsing them into one string would make the chip
// assert something the categories did not.
function originChipLabel(
  green: NonNullable<NameResult["green"]>
): string | null {
  if (!green.originLanguage) return null;
  return green.originShape === "rendering"
    ? `${green.originLanguage} rendering`
    : `from ${green.originLanguage}`;
}

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

function isGreenish(result: NameResult): boolean {
  return (
    result.category === "established" ||
    result.category === "word-established"
  );
}

/**
 * A GRADIENT card is a yellow row wearing a green tag -- it IS the word's
 * node, with the word's children hanging off it -- so it must never be
 * moved. Only standalone green cards get anchored.
 */
function isStandaloneGreen(result: NameResult): boolean {
  return Boolean(result.green) && result.category === "established";
}

// Stage 21. The bucket every origin that is not a corpus language lands
// in. Prefixed so it can never collide with a real ISO code, the same
// reasoning as 11e's `cardtype-` section keys.
const OTHER_ORIGIN = "__other__";

/**
 * The language a card should DISPLAY and GROUP under.
 *
 * Deliberately NOT `languageCode`. That keeps its existing meaning -- the
 * tree the card was retrieved from -- because `dirFor` reads it for script
 * direction, and `Amal` is Arabic-origin but Latin-script and must render
 * LTR.
 *
 * FALLBACK ORDER, and why each:
 *   displayOrigin present   the resolved answer, whatever tier produced it
 *                           (category, gloss_etym, or the ledger). When it
 *                           names a language outside the 21 -- French and
 *                           Italian lead that tail -- the card keeps the
 *                           REAL language on its badge and groups under
 *                           Other. §23.3 measured that at 12.5% of resolved
 *                           rows, so it is a section, not a curiosity.
 *   llm_unknown             the model was asked and declined, or three
 *                           passes reached no majority. G5 measured this at
 *                           2.3-5.6% per stratum, under the 10% ceiling, so
 *                           it goes to Other as designed rather than
 *                           falling back to English.
 *   anything else           PENDING (origin_source null), llm_error, or
 *                           disagreed. Falls back to the tree label.
 *                           Pending recurs after every repopulate cascade,
 *                           and showing "Unknown" on those rows would make
 *                           an ordinary rebuild look like the English
 *                           section had broken.
 */
function originGroup(
  green: NameResult["green"] | null | undefined,
  codeByName: Map<string, string>
): { code: string; label: string } | null {
  if (!green) return null;
  if (green.displayOrigin) {
    const code = codeByName.get(green.displayOrigin.toLowerCase());
    return code
      ? { code, label: green.displayOrigin }
      : { code: OTHER_ORIGIN, label: green.displayOrigin };
  }
  if (green.originSource === "llm_unknown") {
    return { code: OTHER_ORIGIN, label: "Other" };
  }
  return null;
}

/**
 * The code a result sorts, groups and filters on. A gradient card is a
 * name and a word of the SAME language by construction, so origin is not a
 * separate fact about it and it keeps its tree code.
 */
function effectiveCode(
  result: NameResult,
  codeByName: Map<string, string>
): string | null {
  if (!isStandaloneGreen(result)) return result.languageCode ?? null;
  return (
    originGroup(result.green, codeByName)?.code ??
    result.languageCode ??
    null
  );
}

/**
 * 11e. The card-type sections, expressed as MEMBERSHIP PREDICATES rather
 * than a rank function.
 *
 * A gradient card is genuinely both an established name and a word, so it
 * belongs in both of the first two sections. A rank function returns one
 * number and structurally cannot say that -- which is why `word-established`
 * only ever appeared under "Established names".
 *
 * Array order IS display order. `isGreenish` is retained above and used
 * here; the old `sectionRank` had no other caller.
 */
const cardTypeSections: {
  code: string;
  label: string;
  belongs: (result: NameResult) => boolean;
}[] = [
  {
    code: "cardtype-green",
    label: "Established names",
    belongs: isGreenish,
  },
  {
    code: "cardtype-yellow",
    label: "Words and translations",
    belongs: (result) =>
      result.category === "translation" ||
      result.category === "related" ||
      result.category === "word-established",
  },
  {
    code: "cardtype-blue",
    label: "Generated names",
    belongs: (result) => result.category === "generated",
  },
];

/**
 * 9e's anchoring rule, which the roadmap states as one behaviour but which
 * is really two.
 *
 * `tree`: green cards follow their triggering yellow card, unconditionally.
 * Cards whose trigger came from the HIDDEN English pass (parentSenseId
 * null) sit at top level, immediately after the root band -- they matched
 * the query itself, not any displayed word.
 *
 * `language`: a green card belongs in ITS OWN language group; that is what
 * the sort means. But its trigger is frequently English and sits in a
 * different group entirely, so "follow the trigger" is only well-defined
 * for same-language triggers -- i.e. mechanism-2 and gradient cards.
 * Everything else keeps the position the language sort already gave it,
 * which is at the end of its own group.
 */
function anchorGreenCards(
  rows: NameResult[],
  mode: "tree" | "language",
  codeByName: Map<string, string>
): NameResult[] {
  if (!rows.some(isStandaloneGreen)) return rows;

  const anchors = new Map<number, NameResult>();
  for (const row of rows) {
    if (row.matchedSenseId === undefined) continue;
    if (!anchors.has(row.matchedSenseId)) anchors.set(row.matchedSenseId, row);
  }

  function canAnchor(green: NameResult): boolean {
    if (green.parentSenseId === null || green.parentSenseId === undefined) {
      return false;
    }
    const anchor = anchors.get(green.parentSenseId);
    if (!anchor || isStandaloneGreen(anchor)) return false;
    // In language mode a green card whose origin differs from its tree
    // must NOT anchor: anchoring pins it beside a trigger that now lives
    // in a different section, which fights the grouping below. In tree
    // mode the anchor relationship is about the TRIGGER, so origin is
    // irrelevant and the original comparison stands.
    return (
      mode === "tree" ||
      anchor.languageCode === effectiveCode(green, codeByName)
    );
  }

  const byAnchor = new Map<number, NameResult[]>();
  const floating: NameResult[] = [];
  const base: NameResult[] = [];

  for (const row of rows) {
    if (!isStandaloneGreen(row)) {
      base.push(row);
      continue;
    }
    if (canAnchor(row)) {
      const parent = row.parentSenseId as number;
      const list = byAnchor.get(parent) ?? [];
      list.push(row);
      byAnchor.set(parent, list);
      continue;
    }
    if (mode === "language") base.push(row);
    else floating.push(row);
  }

  const out: NameResult[] = [];
  let placed = floating.length === 0;
  for (const row of base) {
    if (!placed && (row.depth ?? 0) > 0) {
      out.push(...floating);
      placed = true;
    }
    out.push(row);
    const children =
      row.matchedSenseId === undefined
        ? undefined
        : byAnchor.get(row.matchedSenseId);
    if (children) out.push(...children);
  }
  if (!placed) out.push(...floating);
  return out;
}

function sortResults(
  results: NameResult[],
  sort: SortOption,
  languageOrder: Map<string, number>,
  codeByName: Map<string, string>
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
    const ordered = [...results].sort((first, second) => {
      const depthDelta = (first.depth ?? 0) - (second.depth ?? 0);
      if (depthDelta !== 0) return depthDelta;

      const firstIndex =
        languageOrder.get(effectiveCode(first, codeByName) ?? "") ??
        Number.MAX_SAFE_INTEGER;
      const secondIndex =
        languageOrder.get(effectiveCode(second, codeByName) ?? "") ??
        Number.MAX_SAFE_INTEGER;
      return firstIndex - secondIndex;
    });
    return anchorGreenCards(ordered, "tree", codeByName);
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
    const ordered = [...results].sort((first, second) => {
      const firstIndex =
        languageOrder.get(effectiveCode(first, codeByName) ?? "") ??
        Number.MAX_SAFE_INTEGER;
      const secondIndex =
        languageOrder.get(effectiveCode(second, codeByName) ?? "") ??
        Number.MAX_SAFE_INTEGER;
      return firstIndex - secondIndex;
    });
    return anchorGreenCards(ordered, "language", codeByName);
  }

  if (sort === "cardtype") {
    // Deliberately a PASS-THROUGH as of 11e. Grouping is done by per-section
    // predicates in `resultGroups`, each of which filters `visibleResults`
    // itself, so pre-sorting into bands buys nothing -- and it costs
    // something: banding would hoist every gradient card to the TOP of the
    // "Words and translations" section rather than leaving it in tree
    // position. The server's interleave order now survives inside each
    // section, matching what the `language` grouped sort already does.
    return [...results];
  }

  // Alphabetical and length: green cards MIX IN, per 9e. No anchoring --
  // "under its trigger" is meaningless in an A-Z list.
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
  // Only ever called for a card whose category is NOT "established" -- see
  // the `result.category !== "established"` guard at both call sites below.
  //
  // A STANDALONE green card carries no badge at all: the four labels here
  // describe how the hop-EXPANSION engine matched the query, and a
  // standalone green card was never touched by that engine -- it was found
  // by established_name_tokens or a homograph key. "Searched meaning" would
  // claim a mechanism that didn't run.
  //
  // A WORD-ESTABLISHED (gradient) card DOES reach here, correctly, with no
  // special-casing needed: _attach_green_cards mutates the existing yellow
  // row rather than emitting a second one, so its matchType, path and depth
  // are the WORD's and are real -- the logic below already describes it.
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

/**
 * 11d. Should a word-name card print a separate "As a name" line?
 *
 * On a gradient card `result.meaning` is the WORD's definition, so the
 * NAME's meaning is a genuinely different fact and belongs under its own
 * label -- except in the two cases where printing it is noise:
 *
 *   * meaningChannel === "HOMOGRAPH". The name's meaning IS the word's
 *     gloss, copied verbatim by inherit_homographs(). Printing the same
 *     string twice under two labels invents a distinction the data does not
 *     have. Per findings 19.2 this is currently the DOMINANT case among
 *     gradient-eligible rows, so expect this branch to fire often.
 *   * The strings match anyway. Catches the same thing structurally, in case
 *     a future channel arrives at an identical string by another route.
 *
 * Returns null rather than "" so the caller's falsiness check is unambiguous.
 */
function nameMeaningToShow(result: NameResult): string | null {
  const green = result.green;
  if (!green) return null;
  if (green.meaningChannel === "HOMOGRAPH") return null;
  const value = green.nameMeaning?.trim() ?? "";
  if (!value) return null;
  if (value === result.meaning.trim()) return null;
  return value;
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
  const [sort, setSort] = useState<SortOption>("cardtype");
  const [availableLanguages, setAvailableLanguages] = useState<LanguageInfo[]>([]);
  const [enabledCodes, setEnabledCodes] = useState<string[]>([]);
  // Stage 21. Defaults ON: §23.3 measured origin='other' on 12.5% of
  // resolved rows, so defaulting it off would hide roughly an eighth of
  // green cards on first load, which reads as missing recall rather than
  // as a filter.
  const [includeOtherOrigins, setIncludeOtherOrigins] = useState(true);
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

  // Language NAME -> code. displayOrigin is a name, not a code, because
  // it is a display column shared by three tiers (category, gloss_etym,
  // ledger) and only the ledger ever knew about codes. Anything that fails
  // to map here is an origin outside the 21 and belongs in Other.
  const languageCodeByName = useMemo(() => {
    const map = new Map<string, string>();
    for (const lang of availableLanguages) {
      map.set(lang.name.toLowerCase(), lang.code);
    }
    return map;
  }, [availableLanguages]);

  const visibleResults = useMemo(() => {
    const filteredResults = results.filter((result) => {
      // A gradient card IS an established name, so the "Established names"
      // filter must keep it. 9d's card-type SORT is what separates the two;
      // CategoryFilter removes, and removing a name because it also happens
      // to be a word would be wrong.
      const matchesCategory =
        category === "all" ||
        result.category === category ||
        (category === "established" && result.category === "word-established");

      // Stage 21. A standalone green card filters on its ORIGIN, not on
      // the tree that produced it: a name whose origin is Arabic belongs
      // to the Arabic selection even though it was retrieved from the
      // English pass. retrieve_green_cards scopes the query the same way,
      // so this is the client half of one rule rather than a second one.
      const cardCode = effectiveCode(result, languageCodeByName);
      const matchesLanguage =
        !cardCode ||
        (cardCode === OTHER_ORIGIN
          ? includeOtherOrigins
          : enabledCodes.includes(cardCode));

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

    return sortResults(filteredResults, sort, languageOrder,
                       languageCodeByName);
  }, [
    category,
    enabledCodes,
    flavor,
    sort,
    results,
    languageOrder,
    languageCodeByName,
    includeOtherOrigins,
  ]);

  // Grouping is a property of the SORT, not the view -- so card view and
  // (later) collapsed view read the same array and cannot drift. One group
  // with a null label means "render flat, no headers".
  //
  // Counts come from group.items.length, NOT the response's
  // treeSummaries.nodeCount -- that is the unfiltered server count and would
  // disagree with what the sidebar filters are actually showing.
  const resultGroups = useMemo(() => {
    if (sort === "cardtype") {
      // Section keys are deliberately NOT language codes: collapsedLanguages
      // is keyed by group.code and would otherwise collide across a sort
      // switch.
      //
      // A gradient card matches two predicates and is therefore rendered
      // TWICE, once per section. That is 11e's intent, not a bug. React keys
      // are safe because the two copies live in different sibling arrays, so
      // `key={result.id}` is still unique within each list.
      //
      // The header count is unaffected: it reads visibleResults.length,
      // which stays the flat truth. Only the per-section badges double-count,
      // which is correct -- the card really is in both sections.
      return cardTypeSections
        .map((section) => ({
          code: section.code as string | null,
          label: section.label as string | null,
          key: (section.code as string | null) ?? section.label ?? "section",
          items: visibleResults.filter(section.belongs),
        }))
        .filter((group) => group.items.length > 0);
    }

    if (sort !== "language") {
      return [
        {
          code: null as string | null,
          label: null as string | null,
          key: "all",
          items: visibleResults,
        },
      ];
    }

    const groups: {
      code: string | null;
      label: string | null;
      key: string;
      items: NameResult[];
    }[] = [];

    for (const result of visibleResults) {
      // Origin cards fold into the EXISTING per-language sections rather
      // than getting their own "Arabic origin" ones. The pill on these
      // cards already reads Arabic (11f), so there is no contradiction to
      // explain -- and separate sections would need prefixed codes, which
      // both collapsedLanguages and languageSectionId are keyed on.
      const code = effectiveCode(result, languageCodeByName) ?? null;
      const group = isStandaloneGreen(result)
        ? originGroup(result.green, languageCodeByName)
        : null;
      const label = group?.label ?? result.language;
      const last = groups[groups.length - 1];

      // Every Other-bucket card shares the same OTHER_ORIGIN code but
      // carries its OWN label (Armenian, Meitei, Hungarian...). Merging on
      // code alone would fold consecutive Other cards from different
      // languages into one section wearing whichever label came first.
      // Checking label too gives each out-of-vocabulary language its own
      // labelled run inside the Other region, while every ordinary
      // section (one code, one label) is completely unaffected.
      if (last && last.code === code && last.label === label) {
        last.items.push(result);
      } else {
        // code alone is not unique for Other-bucket groups: every
        // out-of-vocabulary language shares OTHER_ORIGIN as `code` but
        // carries its own `label` (Armenian, Meitei, Hungarian...), and
        // the merge check above already treats (code, label) as the real
        // identity. `key` makes that same compound identity available to
        // every consumer -- React keys, collapsedLanguages, jumpToLanguage,
        // the DOM anchor id -- so the Other-bucket case only has to be
        // handled once, here, instead of re-derived at each call site
        // (which is how it went missing before).
        const key = code === null
          ? `unknown-${groups.length}`
          : code === OTHER_ORIGIN
            ? `${code}:${label ?? groups.length}`
            : code;
        groups.push({ code, label, key, items: [result] });
      }
    }

    return groups;
  }, [visibleResults, sort, languageCodeByName]);

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
    // Computed once: the JSX below tests it and then renders it.
    const asName = nameMeaningToShow(result);

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
          </div>

          {/* Stage 21. On a standalone green card this reads the ORIGIN,
              not the tree it was retrieved from. A gradient card keeps its
              tree label: it is a name and a word of the same language by
              construction, so origin is not a separate fact about it. */}
          <span className="rounded-full bg-white/70 px-3 py-1 text-xs font-semibold text-slate-700">
            {(isStandaloneGreen(result) &&
              originGroup(result.green, languageCodeByName)?.label) ||
              result.language}
          </span>
        </div>

        {/* ONE tag row, in reading order: the WORD's relationship to the
            search first, the NAME's classification second. Placed below the
            header rather than inside its left column so it can wrap across
            the full card width, and so its left-to-right order matches the
            left-yellow / right-green split behind it.

            `Also a word here` is gone: the two-tone background is now the
            statement, and the tag only ever rendered on cards that already
            have that background. `Also a surname` stays -- nothing else on
            the card carries that fact. */}
        {(result.matchType || result.green) && (
          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            {result.matchType && result.category !== "established" && (
              <span
                className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold shadow-sm ${
                  result.matchType === "exact"
                    ? "bg-white/80 text-slate-700"
                    : "bg-amber-100 text-amber-800"
                }`}
              >
                <span dir="auto">{hopBadgeLabel(result, activeSearch)}</span>
              </span>
            )}

            {result.green && (
              <span className="rounded-full bg-emerald-100 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-900">
                {nameTypeLabels[result.green.nameType]}
                {genderLabels[result.green.gender]
                  ? ` \u00b7 ${genderLabels[result.green.gender]}`
                  : ""}
              </span>
            )}

            {result.green?.isAlsoSurname && (
              <span className="rounded-full bg-white/80 px-2.5 py-0.5 text-[11px] font-semibold text-slate-600">
                Also a surname
              </span>
            )}

            {result.green && originChipLabel(result.green) && (
              <span className="rounded-full bg-sky-100 px-2.5 py-0.5 text-[11px] font-semibold text-sky-900">
                {originChipLabel(result.green)}
              </span>
            )}
          </div>
        )}

        <p className="mt-5 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Meaning
        </p>

        {/* 6d's residue policy on screen: a name with no derived meaning
            still ships, and shows its provenance label instead of a blank.
            A blank card looks broken; a labelled one is informative. */}
        {result.meaning.trim().length > 0 ? (
          <>
            <p className="mt-1 font-semibold">{result.meaning}</p>
            {/* 11d. provenanceLabel describes where the NAME's meaning came
                from. On a gradient card the line above it is the WORD's
                definition, so this caption was attaching name provenance to
                word text -- circular on HOMOGRAPH rows, plainly wrong on any
                other channel. Standalone green cards keep it: there the two
                really do describe the same string. */}
            {result.green && result.category !== "word-established" && (
              <p className="mt-1 text-xs italic text-slate-500">
                {result.green.provenanceLabel}
              </p>
            )}
          </>
        ) : (
          <p className="mt-1 text-sm italic text-slate-500">
            {result.green?.provenanceLabel ?? "Meaning not recorded"}
          </p>
        )}

        {/* The NAME half of a word-name card, deliberately subordinate to the
            word above it: smaller heading, smaller type, and the green
            provenance label finally sitting under the text it actually
            describes. This is what makes "the word part is dominant" a
            structural property of the card rather than a side effect of the
            merge keeping the yellow row's `meaning` field. */}
        {result.category === "word-established" && asName && (
          <>
            <p className="mt-4 text-xs font-semibold uppercase tracking-wide text-emerald-700">
              As a name
            </p>
            <p className="mt-1 text-sm text-slate-700">{asName}</p>
            {result.green && (
              <p className="mt-1 text-xs italic text-slate-500">
                {result.green.provenanceLabel}
              </p>
            )}
          </>
        )}

        {result.green && <VariantDropdown green={result.green} />}

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

          {result.matchType && result.category !== "established" && (
            <span
              className="hidden shrink-0 text-xs font-semibold text-slate-500 sm:inline"
              dir="auto"
            >
              {hopBadgeLabel(result, activeSearch)}
            </span>
          )}

          {result.green && (
            <span className="hidden shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-900 sm:inline">
              {nameTypeLabels[result.green.nameType]}
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
            {result.meaning.trim().length > 0 ? (
              <p className="mt-1 font-semibold text-slate-800">
                {result.meaning}
              </p>
            ) : (
              <p className="mt-1 text-sm italic text-slate-500">
                {result.green?.provenanceLabel ?? "Meaning not recorded"}
              </p>
            )}

            {/* Parity with the card view: same suppression rule, same copy.
                B3's reason for extracting ResultDetails applies here too --
                two views that disagree about what a card contains is worse
                than either view being slightly wrong. */}
            {result.category === "word-established" &&
              nameMeaningToShow(result) && (
                <>
                  <p className="mt-3 text-xs font-semibold uppercase tracking-wide text-emerald-700">
                    As a name
                  </p>
                  <p className="mt-1 text-sm text-slate-700">
                    {nameMeaningToShow(result)}
                  </p>
                </>
              )}

            {result.green && <VariantDropdown green={result.green} />}

            <div className="mt-3">
              <ResultDetails result={result} />
            </div>
          </div>
        )}
      </div>
    );
  }

  function toggleLanguageCollapsed(key: string) {
    setCollapsedLanguages((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function collapseAllLanguages() {
    setCollapsedLanguages(new Set(resultGroups.map((group) => group.key)));
  }

  function jumpToLanguage(key: string) {
    // Expand first: scrolling to a collapsed section lands you on a header
    // with nothing under it, which reads as a broken link.
    setCollapsedLanguages((current) => {
      const next = new Set(current);
      next.delete(key);
      return next;
    });

    // getElementById rather than a ref map: the section list is rebuilt on
    // every search and every collapse toggle, and a ref map would need
    // pruning on each. The id is derived and stable.
    //
    // `key` (not `code`) drives this id -- see resultGroups' `key`
    // computation: `code` alone collides across the Other bucket's
    // per-language sections (Armenian/Meitei/Hungarian all share
    // OTHER_ORIGIN as `code`), and this id must match whatever the
    // sections themselves render at languageSectionId(group.key).
    document
      .getElementById(languageSectionId(key))
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
          includeOtherOrigins,
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
                  <label className="mt-2 flex items-center gap-2 border-t border-slate-200 pt-2 text-sm text-slate-700">
                    <input
                      type="checkbox"
                      checked={includeOtherOrigins}
                      onChange={() => setIncludeOtherOrigins((v) => !v)}
                    />
                    <span>
                      Other origins
                      <InfoTip label="Other origins">
                        Some names match your meaning but come from a
                        language outside this list — French and Italian are
                        the most common. They keep their real language on
                        the card and are grouped under{" "}
                        <strong>Other</strong>, along with the few names we
                        could not place at all. Unlike the boxes above,
                        this one only shows and hides results — it never
                        changes which languages the next search looks at.
                      </InfoTip>
                    </span>
                  </label>
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
                A <strong>hop</strong> refers to the retreiveing of related words.
                For example, searching one hop away from "light" will also retrieve "luminance."
                <br />
                <strong>Breadth</strong> is the amount of words related to 
                your searched meaning that will be retrieved per hop for each language.
                <br />
                <strong>Depth</strong> is how many hops away from the original
                meaning the expansion will stray.
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
              <p className="mt-2 text-sm text-slate-600" style={{ fontSize: '11px' }}><i>*Note that increasing the expansion depth beyond 1 can significantly increase search times (maximum 2-3 minutes)</i></p>
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
                {resultGroups.map((group) => (
                  <button
                    key={group.key}
                    type="button"
                    onClick={() => jumpToLanguage(group.key)}
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
                    Card
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
                </label>

                <select
                  id="sort-select"
                  value={sort}
                  onChange={(event) =>
                    setSort(event.target.value as SortOption)
                  }
                  className="mt-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm"
                >
                  <option value="cardtype">Type</option>
                  <option value="language">Language</option>
                  <option value="relevance">Tree order</option>
                  <option value="az">A to Z</option>
                  <option value="za">Z to A</option>
                  <option value="shortest">Shortest</option>
                  <option value="longest">Longest</option>
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
              {resultGroups.map((group) => {
                const isCollapsed = collapsedLanguages.has(group.key);

                return (
                  <section
                    key={group.key}
                    id={languageSectionId(group.key)}
                    // Small top margin so a jumped-to header doesn't land
                    // flush against the viewport edge.
                    className="scroll-mt-6"
                  >
                    {group.label && (
                      <button
                        type="button"
                        onClick={() => toggleLanguageCollapsed(group.key)}
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