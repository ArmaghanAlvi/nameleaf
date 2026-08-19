from pydantic import BaseModel


class SenseOptionResponse(BaseModel):
    senseId: int
    word: str
    # Phase D. Latin-script rendering of `word`; None renders as nothing.
    romanization: str | None = None
    language: str
    languageCode: str | None
    partOfSpeech: str
    definition: str
    displayDefinition: str
    senseGroup: str | None
    rawGlosses: list[str]
    tags: list[str]
    categories: list[str]
    selectionCount: int
    pinnedRank: int | None
    isHidden: bool
    sourceLocator: str
    duplicateCount: int = 1
    collapsedSenseIds: list[int] = []


class SenseLookupResponse(BaseModel):
    query: str
    options: list[SenseOptionResponse]


class SenseAdminUpdateRequest(BaseModel):
    isHidden: bool | None = None
    pinnedRank: int | None = None
    labelOverride: str | None = None
    definitionOverride: str | None = None
    notes: str | None = None