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


class ExploreV2Result(BaseModel):
    id: str
    name: str
    category: Literal[
        "established",
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
    relationshipWeight: float
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