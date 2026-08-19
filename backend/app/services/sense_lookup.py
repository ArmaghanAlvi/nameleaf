from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import asc, desc, func, nullslast, select
from sqlalchemy.orm import Session, selectinload

from app.models.generated_name import Language
from app.models.semantic import (
    Lexeme,
    Sense,
    SenseAdminOverride,
    SenseSelectionStat,
)
from app.schemas.senses import SenseOptionResponse
from app.services.sense_display import sense_display_for
from app.utils.text import normalize_text

# Safety cap on the candidate set. The largest English lemma (`run`) has 111
# visible senses; headroom for other languages. Ranking happens over the FULL
# set; `limit` slices AFTER ranking -- slicing before it would hide
# high-scoring senses behind the dictionary-order cap (`draw`'s central sense
# sits at dictionary position 87, and at rank 61 even after ranking).
MAX_CANDIDATES = 1000


def effective_definition(
    sense: Sense,
    override: SenseAdminOverride | None,
) -> str:
    if override and override.definition_override:
        return override.definition_override

    return sense.definition


@dataclass(frozen=True)
class SenseCandidate:
    """
    One row of the dropdown's candidate set, before ordering or presentation.

    Shared by the live lookup path and the rank-quality probe so that both
    always score the same population. Do not build this set anywhere else.
    """

    sense: Sense
    lexeme: Lexeme
    language: Language
    selection_stat: SenseSelectionStat | None
    override: SenseAdminOverride | None

    @property
    def selection_count(self) -> int:
        if self.selection_stat is None:
            return 0
        return self.selection_stat.selection_count

    @property
    def pinned_rank(self) -> int | None:
        if self.override is None:
            return None
        return self.override.pinned_rank

    @property
    def is_hidden(self) -> bool:
        return (
            self.sense.visibility_status == "hidden"
            or bool(self.override and self.override.is_hidden)
        )


def fetch_sense_candidates(
    db: Session,
    *,
    query: str,
    language_code: str | None = None,
    include_hidden: bool = False,
    limit: int = 50,
    with_relations: bool = False,
) -> list[SenseCandidate]:
    """
    Fetch the dropdown's candidate senses for an exact lemma match, in the
    current default order (pinned, then popularity, then dictionary order).

    `with_relations` eager-loads Sense.relations. Off in production until a
    ranking signal needs it; the probe turns it on.
    """
    normalized_query = normalize_text(query)

    statement = (
        select(
            Sense,
            Lexeme,
            Language,
            SenseSelectionStat,
            SenseAdminOverride,
        )
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .join(Language, Language.id == Lexeme.language_id)
        .outerjoin(
            SenseSelectionStat,
            SenseSelectionStat.sense_id == Sense.id,
        )
        .outerjoin(
            SenseAdminOverride,
            SenseAdminOverride.sense_id == Sense.id,
        )
        .where(Lexeme.normalized_lemma == normalized_query)
    )

    if language_code is not None:
        statement = statement.where(Language.code == language_code)

    if not include_hidden:
        statement = statement.where(
            Sense.visibility_status == "visible",
            (SenseAdminOverride.is_hidden.is_(None))
            | (SenseAdminOverride.is_hidden.is_(False)),
        )

    if with_relations:
        statement = statement.options(selectinload(Sense.relations))

    statement = (
        statement.order_by(
            nullslast(asc(SenseAdminOverride.pinned_rank)),
            desc(func.coalesce(SenseSelectionStat.selection_count, 0)),
            asc(Sense.source_order),
            asc(Sense.sense_index),
        )
        .limit(limit)
    )

    return [
        SenseCandidate(
            sense=sense,
            lexeme=lexeme,
            language=language,
            selection_stat=stat,
            override=override,
        )
        for sense, lexeme, language, stat, override in db.execute(statement).all()
    ]


def lookup_sense_options(
    db: Session,
    *,
    query: str,
    language_code: str | None = None,
    include_hidden: bool = False,
    limit: int = 50,
) -> list[SenseOptionResponse]:
    # Imported here, not at module top, to break the import cycle:
    # dropdown_ranker imports SenseCandidate from this module, so this module
    # cannot import dropdown_ranker at load time.
    from app.services.dropdown_ranker import (
        CollapsedCandidate,
        RankWeights,
        collapse_ranked,
        rank_candidates,
    )

    candidates = fetch_sense_candidates(
        db,
        query=query,
        language_code=language_code,
        include_hidden=include_hidden,
        limit=MAX_CANDIDATES,
    )

    ranked = rank_candidates(candidates, RankWeights())

    # Collapse is a user-facing presentation feature; the admin view
    # (include_hidden=True) must keep every sense individually visible and
    # actionable (hide/pin per sense), so it bypasses collapse.
    if include_hidden:
        entries = [CollapsedCandidate(representative=c) for c in ranked]
    else:
        entries = collapse_ranked(ranked)

    entries = entries[:limit]

    options: list[SenseOptionResponse] = []

    for entry in entries:
        candidate = entry.representative
        sense = candidate.sense
        override = candidate.override

        display = sense_display_for(sense, override)

        options.append(
            SenseOptionResponse(
                senseId=sense.id,
                word=override.label_override
                if override and override.label_override
                else candidate.lexeme.lemma,
                # Suppressed when an admin label_override replaces the lemma:
                # the romanization is of `lemma`, and pairing it with a
                # different displayed word would be actively misleading.
                romanization=(
                    None
                    if override and override.label_override
                    else candidate.lexeme.romanization
                ),
                language=candidate.language.name,
                languageCode=candidate.language.code,
                partOfSpeech=candidate.lexeme.part_of_speech,
                definition=effective_definition(sense, override),
                displayDefinition=display.definition,
                senseGroup=display.group_label,
                rawGlosses=sense.raw_glosses,
                tags=sense.raw_tags,
                categories=sense.categories,
                selectionCount=candidate.selection_count,
                pinnedRank=candidate.pinned_rank,
                isHidden=candidate.is_hidden,
                sourceLocator=sense.source_locator,
                duplicateCount=entry.duplicate_count,
                collapsedSenseIds=list(entry.collapsed_sense_ids),
            )
        )

    return options