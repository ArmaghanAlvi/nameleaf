export type ResultCategory =
  | "established"
  | "related"
  | "translation"
  | "generated";

export type MatchType = "exact" | "expanded";

export type NamePartKind =
  | "root"
  | "word"
  | "inspired"
  | "crafted";

export type GenerationFlavor =
  | "default"
  | "fantasy"
  | "ancient-inspired"
  | "modern";

export interface NamePart {
  text: string;
  meaning: string;
  language: string;
  kind: NamePartKind;
  note?: string | null;
}

export interface AlternateMeaning {
  meaning: string;
  explanation: string;
  nativeForm?: string | null;
  isPrimary?: boolean;
}

export interface RelatedName {
  name: string;
  relationshipType: string;
  notes?: string | null;
}

export interface HopPathStep {
  word: string;
  senseId: number;
  depth: number;
}

export interface NameResult {
  id: string;
  name: string;
  category: ResultCategory;
  meaning: string;
  language: string;
  explanation: string;

  matchType?: MatchType;
  matchedConcept?: string;

  relationshipType?: string | null;
  relationshipWeight?: number | null;
  equivalenceType?: string | null;
  senseRank?: number | null;
  source?: string | null;
  sourceLocator?: string | null;
  confidence?: string | null;

  sourceLanguages?: string[];
  flavors?: GenerationFlavor[];
  parts?: NamePart[];

  alternateMeanings?: AlternateMeaning[];
  relatedNames?: RelatedName[];

  matchedSenseId?: number;
  partOfSpeech?: string;

  // Multi-hop fields (real, from explore-v2; see explore.ts adapter)
  depth?: number;
  parentSenseId?: number | null;
  provenance?: string | null;
  path?: HopPathStep[];

  // Multilingual fields (Breakdown 5, real backend output)
  languageCode?: string | null;
  rootRung?: string | null;

  // Phase D: Latin-script rendering of `name`. null/undefined renders as
  // nothing -- the backend sends null wherever no trustworthy value exists,
  // and a guess is worse than a blank.
  romanization?: string | null;
}

export interface SelectedSense {
  senseId: number;
  word: string;
  language: string;
  partOfSpeech: string;
  definition: string;
}