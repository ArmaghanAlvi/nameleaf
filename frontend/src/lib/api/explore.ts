import type { HopPathStep, NameResult } from "@/features/generator/types";

export interface SenseOption {
  senseId: number;
  word: string;
  romanization: string | null;
  language: string;
  languageCode: string | null;
  partOfSpeech: string;
  definition: string;
  displayDefinition: string;
  senseGroup: string | null;
  rawGlosses: string[];
  tags: string[];
  categories: string[];
  selectionCount: number;
  pinnedRank: number | null;
  isHidden: boolean;
  sourceLocator: string;
  duplicateCount: number;
  collapsedSenseIds: number[];
}

export interface SenseLookupResponse {
  query: string;
  options: SenseOption[];
}

export async function lookupSenses(
  query: string,
  languageCode = "en"
): Promise<SenseLookupResponse> {
  const params = new URLSearchParams({
    query,
    languageCode,
    // Full ranked candidate set. The route caps at le=200; the largest
    // observed lemma is `run` (111 senses, fewer post-collapse). Without
    // this, the route default (50) reintroduces a truncation the ranker
    // was specifically fixed to avoid -- draw's central sense ranks 61st.
    limit: "200",
  });

  const response = await fetch(
    `http://127.0.0.1:8000/senses/lookup?${params.toString()}`
  );

  if (!response.ok) {
    throw new Error(`Backend returned status ${response.status}`);
  }

  return response.json();
}

export interface ExploreSelectedSensesRequest {
  selectedSenseIds: number[];
  queryText: string;
  breadth: number;   // expansions per node (0-3); 0 = exact meaning only
  depth: number;     // hops (0-3); 0 = exact meaning only
  language: string | null;         // legacy field; unused on the parallel path
  languageCodes: string[] | null;  // which trees to build; null = legacy en-only path
  minLength: number;
  maxLength: number;
}

// Mirrors the backend's ExploreV2Result exactly (app/schemas/explore_v2.py).
export interface ExploreV2Result {
  id: string;
  name: string;
  category: "established" | "related" | "translation" | "generated";
  meaning: string;
  language: string;
  explanation: string;
  matchType: "exact" | "expanded";
  matchedSenseId: number;
  relationshipType: string;
  relationshipWeight: number;
  partOfSpeech: string;
  depth: number;
  parentSenseId: number | null;
  provenance: string | null;
  path: HopPathStep[];
  languageCode: string | null;
  rootRung: string | null;
  romanization: string | null;
}

export interface ExploreSelectedSensesResponse {
  selectedSenseIds: number[];
  expandedSenses: {
    senseId: number;
    word: string;
    language: string;
    definition: string;
    relationshipType: string;
    weight: number;
  }[];
  results: ExploreV2Result[];
  treeSummaries: {
    languageCode: string;
    language: string;
    rootWord: string | null;
    rootRung: string | null;
    nodeCount: number;
    pivotedCount: number;
  }[];
}

// The mapped shape the UI consumes: same envelope, results as NameResult[].
export interface ExploreSelectedSensesResult {
  selectedSenseIds: number[];
  expandedSenses: ExploreSelectedSensesResponse["expandedSenses"];
  results: NameResult[];
}

// D3 adapter: explore-v2 sends ExploreV2Result; the UI consumes NameResult.
// Every field here is real backend output; NameResult's other optional fields
// (parts, flavors, matchedConcept, ...) are mock/generator-era and never come
// from this endpoint.
export function toNameResult(r: ExploreV2Result): NameResult {
  return {
    id: r.id,
    name: r.name,
    category: r.category,
    meaning: r.meaning,
    language: r.language,
    explanation: r.explanation,
    matchType: r.matchType,
    matchedSenseId: r.matchedSenseId,
    relationshipType: r.relationshipType,
    relationshipWeight: r.relationshipWeight,
    partOfSpeech: r.partOfSpeech,
    depth: r.depth,
    parentSenseId: r.parentSenseId,
    provenance: r.provenance,
    path: r.path,
    languageCode: r.languageCode,
    rootRung: r.rootRung,
    romanization: r.romanization,
  };
}

export async function exploreSelectedSenses(
  request: ExploreSelectedSensesRequest,
  signal?: AbortSignal
): Promise<ExploreSelectedSensesResult> {
// Unified contract: every request routes through multi_hop_expand.
  //   width  = breadth (expansions per node)
  //   depth  = depth   (hops; 0 = exact meaning only, allowed by schema now)
  // The backend returns just the searched word when width<=0 or depth<=0,
  // so the zero-case needs no special disguising — it's passed through.
  const body = {
    selectedSenseIds: request.selectedSenseIds,
    queryText: request.queryText,
    expansionCount: request.breadth,
    width: request.breadth,
    depth: request.depth,
    language: request.language,
    languageCodes: request.languageCodes,
    minLength: request.minLength,
    maxLength: request.maxLength,
  };

  const response = await fetch("http://127.0.0.1:8000/explore-v2", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Backend returned status ${response.status}`);
  }

  const data: ExploreSelectedSensesResponse = await response.json();
  return { ...data, results: data.results.map(toNameResult) };
}


export interface LanguageInfo {
  code: string;
  name: string;
  nativeName: string | null;
  script: string | null;
  rtl: boolean;
}


export async function fetchLanguages(): Promise<LanguageInfo[]> {
  const response = await fetch("http://127.0.0.1:8000/languages");
  if (!response.ok) {
    throw new Error(`Backend returned status ${response.status}`);
  }
  return response.json();
}