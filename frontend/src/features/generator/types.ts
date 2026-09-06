export type ResultCategory =
  | "established"
  // Stage 2c's gradient value: the word and the name are the same object in
  // the same language, so they ship as ONE card wearing both tags. A
  // distinct value rather than reusing "established" keeps the category
  // filter coherent (IMPORT_PREP_FINDINGS.md 5.5).
  | "word-established"
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

/**
 * 9a: `RelatedName` was clearly designed for this, so it is extended rather
 * than replaced -- plus the two fields it never had. A language, because the
 * cognate grouping is cross-language by definition; a romanization, because
 * half of that list is in a script the reader cannot pronounce.
 */
export interface GreenVariant extends RelatedName {
  romanization?: string | null;
  languageCode?: string | null;
  language: string;
  isCrossLanguage: boolean;
  isDirect: boolean;
}

/** Everything green about a result. Mirrors backend GreenCardPayload. */
export interface GreenCardPayload {
  nameId: number;
  nameType: "given" | "surname" | "patronymic";
  gender: "m" | "f" | "x" | "u";
  isAlsoSurname: boolean;

  provenanceLabel: string;
  meaningChannel?: string | null;
  homographConfidence?: string | null;
  /** Stage 14e. Null means the source categories stated no origin -- NOT
   *  that the name is native to this language. `Nadia` carries only bare
   *  English categories and is indistinguishable here from any native
   *  English given name; the chip is honest about what Wiktionary says,
   *  which is less than the whole truth. */
  originLanguage?: string | null;
  /** 'from' (borrowed) | 'rendering' (an X-language way of writing a
   *  Y-language name). Different claims, different chip text. */
  originShape?: string | null;
    /** Stage 21. The resolved origin and its provenance. A language NAME,
   *  not a code. Null means pending or unresolvable — see originSource
   *  for which. */
  displayOrigin?: string | null;
  /** 'category' | 'llm_native' | 'llm_foreign' | 'llm_twin' |
   *  'llm_unknown' | 'llm_error' | 'gradient_exempt' | null (pending). */
  originSource?: string | null;
  /** Stage 11d. The NAME's meaning. On a gradient card this is NOT
   *  `NameResult.meaning` -- that is the WORD's definition, from the yellow
   *  row this card merged onto. Mirrors GreenCardPayload.nameMeaning. */
  nameMeaning?: string | null;

  mechanisms: string[];
  matchedTokens: string[];
  matchTier: number;
  isGradient: boolean;

  triggerWord: string;
  triggerLanguageCode: string;
  /** false = the trigger came from the HIDDEN English pass and is not on
   *  screen. The card is real; the word that found it just isn't displayed. */
  triggerVisible: boolean;

  clusterId?: number | null;
  variants: GreenVariant[];
  variantTotal: number;
  cognates: GreenVariant[];
  cognateTotal: number;
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

  // Stage 8. Present on green and gradient cards, absent on yellow ones.
  green?: GreenCardPayload | null;
}

export interface SelectedSense {
  senseId: number;
  word: string;
  language: string;
  partOfSpeech: string;
  definition: string;
}