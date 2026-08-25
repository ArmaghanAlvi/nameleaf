from typing import Literal

from pydantic import BaseModel, Field


class ExploreV2Request(BaseModel):
    selectedSenseIds: list[int] = Field(min_length=1)
    queryText: str = ""
    expansionCount: int = Field(default=10, ge=0, le=100)
    language: str | None = None
    minLength: int = Field(default=0, ge=0, le=30)
    maxLength: int = Field(default=30, ge=0, le=30)
    # Multi-hop controls. depth=1 => single-hop (existing behavior); width
    # defaults to None so callers that only send expansionCount are unchanged.
    width: int | None = Field(default=None, ge=0, le=10)
    depth: int = Field(default=1, ge=0, le=3)
    # Breakdown 5: which language trees to build. None (absent) preserves the
    # legacy single-tree path BYTE-IDENTICALLY -- the regression harness sends
    # no languageCodes and must keep routing there. A list (e.g. ["en","ru"])
    # routes through parallel_expand. Unknown codes are silently dropped by
    # the orchestrator's order-intersection.
    languageCodes: list[str] | None = None


class HopPathStep(BaseModel):
    word: str
    senseId: int
    depth: int


class GreenVariant(BaseModel):
    """One entry in a green card's variant or cognate dropdown (9c).

    Mirrors the frontend's already-present `RelatedName` (name /
    relationshipType / notes) plus the two fields that interface never had:
    a language, because the cognate grouping is cross-language by
    definition, and a romanization, because half the cognate list is in a
    script the reader cannot pronounce.
    """
    name: str
    romanization: str | None = None
    relationshipType: str
    languageCode: str | None = None
    language: str
    isCrossLanguage: bool
    isDirect: bool


class GreenCardPayload(BaseModel):
    """Everything green about a result. Nested rather than flattened onto
    ExploreV2Result so 8c's additive contract is one optional field, not
    eighteen -- and so `result.green` is a single null check in the UI."""
    nameId: int
    nameType: Literal["given", "surname", "patronymic"]
    gender: Literal["m", "f", "x", "u"]
    isAlsoSurname: bool

    provenanceLabel: str
    meaningChannel: str | None = None
    homographConfidence: str | None = None

    mechanisms: list[str]
    matchedTokens: list[str] = Field(default_factory=list)
    matchTier: int
    isGradient: bool

    triggerWord: str
    triggerLanguageCode: str
    triggerVisible: bool

    clusterId: int | None = None
    variants: list[GreenVariant] = Field(default_factory=list)
    variantTotal: int = 0
    cognates: list[GreenVariant] = Field(default_factory=list)
    cognateTotal: int = 0


class ExploreV2Result(BaseModel):
    id: str
    name: str
    category: Literal[
        "established",
        # Stage 2c's reserved gradient value: the word and the name are the
        # SAME object in the same language, so they ship as one card wearing
        # both tags. A distinct value rather than reusing "established"
        # keeps the category filter coherent (IMPORT_PREP_FINDINGS 5.5).
        "word-established",
        "related",
        "translation",
        "generated",
    ]
    meaning: str
    language: str
    explanation: str
    matchType: Literal["exact", "expanded"]
    matchedSenseId: int
    relationshipType: str
    # NULLABLE as of Stage 8. A lexical green-card match carries no
    # similarity score (roadmap 7e says so explicitly), and 0.0 would be a
    # fabricated number that sorts as "worst". Yellow cards still always
    # populate it; the frontend's NameResult already types it nullable.
    # NOTE: capture_api_current.py does round(r.relationshipWeight, 4) and
    # would raise on None -- which it never sees, because green cards ship
    # only on the parallel path and that script uses the legacy one.
    relationshipWeight: float | None
    partOfSpeech: str
    # Multi-hop metadata. Optional so single-hop results (depth=1) omit them.
    depth: int = 0
    parentSenseId: int | None = None
    provenance: str | None = None
    path: list[HopPathStep] = Field(default_factory=list)
    # Breakdown 5: multilingual surfacing.
    languageCode: str | None = None   # ISO code of THIS result's language (RTL, filtering)
    rootRung: str | None = None       # depth-0 rows only: selected | corroborated |
                                      # primary | ili | llm | pivoted_root | fallback
    # Phase D. Latin-script rendering of `name`, or None where no trustworthy
    # value exists. None MUST render as nothing -- never as a guess.
    romanization: str | None = None
    # Stage 8. Present on green and gradient cards, absent on yellow ones.
    green: GreenCardPayload | None = None


class ExpandedSenseResponse(BaseModel):
    senseId: int
    word: str
    language: str
    definition: str
    relationshipType: str
    weight: float


class TreeSummary(BaseModel):
    """Per-language tree status (Breakdown 5). Lets the UI distinguish
    'language returned nothing' from 'language wasn't requested'."""
    languageCode: str
    language: str
    rootWord: str | None
    rootRung: str | None
    nodeCount: int
    pivotedCount: int


class ExploreV2Response(BaseModel):
    selectedSenseIds: list[int]
    expandedSenses: list[ExpandedSenseResponse]
    results: list[ExploreV2Result]
    treeSummaries: list[TreeSummary] = Field(default_factory=list)