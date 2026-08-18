from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.generated_name import Language
from app.models.semantic import Lexeme, Sense, SenseEmbedding
from app.services.embedding_provider import DEFAULT_EMBEDDING_MODEL, embed_query
from app.utils.text import normalize_text
from app.services.sense_selection import get_sense_selection_counts_for_senses
from app.services.sense_reranker import (
    RerankCandidate,
    rerank_candidates,
)
from app.services.vector_scope import scoped_vector_scan

logger = logging.getLogger(__name__)

# Cosine-similarity floor for vector expansions. Candidates whose FINAL
# reranked score falls below this are treated as "not a real synonym" and
# dropped, even if it means returning fewer than expansion_count results.
# 0.0 distance = identical; we keep ~0.70+ similarity. 
MIN_EXPANSION_SCORE = 0.78  # English -- UNCHANGED; the tuned original.

# Per-language floors for the TREE vector fallback. en is the tuned anchor;
# the others transport that operating point by each language's baseline-cone
# offset (its random p50 minus English's, from the within-language
# calibration, /tmp/calibration_auc.txt):
#   la .836-.790=+.046  ru .856-.790=+.066  ja .836-.790=+.046  ar .846-.790=+.056
# APPROXIMATION, consciously: the calibration measured sense-pair cosine,
# while this floor gates reranked query-vector scores -- the offset corrects
# the baseline shift, not the full distribution. These are initial settings
# for the eval harness to refine; they are NOT derived truths.
MIN_EXPANSION_SCORE_BY_LANG: dict[str, float] = {
    "en": MIN_EXPANSION_SCORE,
    "la": 0.826, "ru": 0.846, "ja": 0.826, "ar": 0.836,
    "ga": 0.838,
    "hi": 0.848, "sa": 0.831,
    "he": 0.845, "fa": 0.858,
    "de": 0.817, "pl": 0.823,
    "es": 0.838,
    "zh": 0.824, "ko": 0.839,
    "el": 0.846, "cy": 0.842, "sw": 0.843, "ang": 0.844, "non": 0.852, "is": 0.844,
}


def _min_expansion_score_for(language_code: str | None) -> float:
    return MIN_EXPANSION_SCORE_BY_LANG.get(language_code or "en",
                                           MIN_EXPANSION_SCORE)

MatchType = Literal["selected", "expanded"]


@dataclass(frozen=True)
class SenseSearchHit:
    sense: Sense
    match_type: MatchType
    score: float
    reason: str


# Common inflectional/derivational endings we strip to detect that a
# candidate is "the same word" as the query (light/lighted/lighting/lights).
_MORPH_SUFFIXES = (
    "ing", "edly", "ed", "es", "s",
    "er", "est", "ly", "ness", "ment", "tion", "ion",
)


def _morph_stem(text: str) -> str:
    """
    Crude, dependency-free stem: lowercase, then strip one common suffix.
    Good enough to catch light/lighted/lighting/lights as one family.
    Not linguistically perfect, and that's fine — we only use it to reject
    same-word candidates, never to merge distinct words.
    """
    base = normalize_text(text)

    for suffix in _MORPH_SUFFIXES:
        if len(base) > len(suffix) + 2 and base.endswith(suffix):
            return base[: -len(suffix)]

    return base


def is_morphological_variant_of_query(
    candidate_lemma: str,
    selected_lemmas: set[str],
    selected_stems: set[str],
) -> bool:
    """
    True if the candidate is essentially the same word as a selected lemma:
    an exact match, a shared stem, or a long shared prefix. Used to drop
    'lighted'/'lighting' when the query is 'light'.
    """
    normalized = normalize_text(candidate_lemma)

    if normalized in selected_lemmas:
        return True

    candidate_stem = _morph_stem(normalized)

    if candidate_stem in selected_stems:
        return True

    # Long shared prefix catches cases the suffix list misses
    # (e.g. 'luminous' vs 'luminosity' share 'lumin').
    for stem in selected_stems:
        shared = _shared_prefix_length(candidate_stem, stem)

        if shared >= 5 and shared >= min(len(candidate_stem), len(stem)) - 1:
            return True

    return False


def _shared_prefix_length(a: str, b: str) -> int:
    count = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        count += 1
    return count


def display_word_key_for_sense(sense: Sense) -> str:
    """
    Determines whether two sense results count as the same displayed word.

    For now, this dedupes globally by displayed lemma:
        light/noun and light/adjective collapse to "light"

    Later, if you want to allow same spelling across different languages,
    change this to include language code.
    """
    return normalize_text(sense.lexeme.lemma)


def word_length_allowed(
    sense: Sense,
    *,
    min_length: int,
    max_length: int,
) -> bool:
    length = len(sense.lexeme.lemma)

    return min_length <= length <= max_length


def build_query_text_from_selected_senses(
    senses: list[Sense],
) -> str:
    """
    Build a lean query string for E5.

    E5 embeds text literally; it does not follow instructions. So we
    include ONLY meaning-bearing content (lemma + definition + a couple
    of glosses), not prose describing what we want. A tight query of the
    form "lemma: <definition>" matches near-synonyms far better than an
    instruction paragraph, which drags the vector toward meta-vocabulary
    like "poetic" and "equivalent".
    """
    parts: list[str] = []

    from app.services.sense_embeddings import collect_synonyms_for_embedding
    for sense in senses:
        lexeme = sense.lexeme
        extra_glosses = "; ".join(sense.raw_glosses[1:3])
        fragment = f"{lexeme.lemma}: {sense.definition}"
        if extra_glosses:
            fragment = f"{fragment}; {extra_glosses}"
        synonyms = collect_synonyms_for_embedding(sense)
        if synonyms:
            fragment = f"{fragment}; synonyms: {', '.join(synonyms)}"
        parts.append(fragment)

    return " ".join(parts)


def get_selected_senses(
    db: Session,
    *,
    sense_ids: list[int],
) -> list[Sense]:
    if not sense_ids:
        return []

    return list(
        db.scalars(
            select(Sense)
            .options(
                selectinload(Sense.lexeme).selectinload(Lexeme.language),
                selectinload(Sense.relations),
            )
            .where(
                Sense.id.in_(sense_ids),
                Sense.visibility_status == "visible",
            )
        ).all()
    )


def collect_antonym_lemmas(selected_senses: list[Sense]) -> set[str]:
    """
    Normalized lemmas that are antonyms of any selected sense. Used to drop
    candidates like 'dark'/'darkness' that cluster near 'light' in vector
    space precisely because antonyms share context. Requires sense.relations
    to be loaded.
    """
    out: set[str] = set()
    for sense in selected_senses:
        for rel in (getattr(sense, "relations", None) or []):
            if rel.relation_type == "antonym":
                out.add(normalize_text(rel.target_text))
    return out


def expand_from_selected_senses(
    db: Session,
    *,
    selected_sense_ids: list[int],
    expansion_count: int,
    target_language: str | None = None,
    min_length: int = 0,
    max_length: int = 30,
    query_vector: list[float] | None = None,
    selected_senses: list[Sense] | None = None,
) -> list[SenseSearchHit]:
    # Same reuse contract as query_vector below: expand() has already loaded
    # exactly these senses (4 round trips -- base SELECT plus a selectin each
    # for lexeme, language, relations) and passes them down rather than
    # paying for them twice on every one of the ~156 expansion calls per
    # search. Same Session and same ids, so SQLAlchemy's identity map would
    # hand back these very instances anyway; only the SQL is skipped.
    if selected_senses is None:
        selected_senses = get_selected_senses(
            db,
            sense_ids=selected_sense_ids,
        )
    selected_lemmas = {
        normalize_text(sense.lexeme.lemma)
        for sense in selected_senses
    }
    selected_stems = {
        _morph_stem(sense.lexeme.lemma)
        for sense in selected_senses
    }
    antonym_lemmas = collect_antonym_lemmas(selected_senses)

    hits: list[SenseSearchHit] = []
    displayed_word_keys: set[str] = set()

    # 0th expansion:
    # Show the exact selected meaning, but only once per displayed word.
    for sense in selected_senses:
        if not word_length_allowed(
            sense,
            min_length=min_length,
            max_length=max_length,
        ):
            continue

        word_key = display_word_key_for_sense(sense)

        if word_key in displayed_word_keys:
            continue

        displayed_word_keys.add(word_key)

        hits.append(
            SenseSearchHit(
                sense=sense,
                match_type="selected",
                score=1.0,
                reason="user_selected_meaning",
            )
        )

    if expansion_count <= 0 or not selected_senses:
        return hits

    # The tree's language is the ROOT'S language, always — derived, never
    # trusted from the request. Without this, the pgvector scan below runs
    # over the WHOLE embedded pool: the moment any non-English sense is
    # embedded, cross-language hits leak into an English tree mid-traversal —
    # the meshed-graph failure mode (MULTILINGUAL_EXPANSION_MODEL.md §4,
    # Model 2 rejection) arriving by accident. Pulled forward from roadmap
    # Stage 6a as a precondition of the multilingual embedding run.
    tree_language_id = selected_senses[0].lexeme.language_id
    tree_floor = _min_expansion_score_for(selected_senses[0].lexeme.language.code)

    # If the caller already embedded the identical query text (expand() does,
    # for edge scoring), reuse it instead of embedding the same text again.
    # Same senses -> same text -> same vector, so results are unchanged.
    if query_vector is None:
        query_vector = embed_query(
            build_query_text_from_selected_senses(selected_senses)
        )

    selected_lexeme_ids = {sense.lexeme_id for sense in selected_senses}
    # Exclude EVERY sense of the queried lexeme(s), not just the selected
    # sense — otherwise light's ~70 other senses (near-identical embeddings)
    # flood the candidate pool and crowd out genuinely different words.
    selected_ids = set(
        db.scalars(
            select(Sense.id).where(Sense.lexeme_id.in_(selected_lexeme_ids))
        ).all()
    )

    distance = SenseEmbedding.embedding.cosine_distance(query_vector)

    # Important:
    # We fetch more than the requested expansion count because many nearby
    # vector hits may be duplicate senses of the same displayed word.
    # Non-English: iterative scan makes this limit REAL for the first time,
    # and it runs per node (~16 queries/request at width=3, 4 trees).
    # Measured pool ceilings under max_scan_tuples=100K are 240 (la) /
    # 132 (ar) / 404 (ru) rows anyway, so a 1000 limit is pure latency.
    # English keeps the original expression exactly -- byte-identical.
    _tree_code = selected_senses[0].lexeme.language.code
    candidate_fetch_limit = min(
        max(expansion_count * 50, 100),
        1000 if _tree_code == "en" else 200,
    )

    statement = (
        select(SenseEmbedding, distance.label("distance"))
        .join(Sense, Sense.id == SenseEmbedding.sense_id)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .join(Language, Language.id == Lexeme.language_id)
        .options(
            selectinload(SenseEmbedding.sense)
            .selectinload(Sense.lexeme)
            .selectinload(Lexeme.language)
        )
        .where(
            SenseEmbedding.embedding_model == DEFAULT_EMBEDDING_MODEL,
            Sense.visibility_status == "visible",
            Lexeme.language_id == tree_language_id,
            ~SenseEmbedding.sense_id.in_(selected_ids),
        )
    )

    if target_language is not None:
        statement = statement.where(Language.name == target_language)

    # Filtered vector query -> starvation-prone (measured: ar 0 of 15 with
    # iterative scan off, la 1 of 15). relaxed_order because these candidates
    # are reranked downstream anyway and this runs per-node, so cost
    # dominates; ~9x faster than strict on Latin. No-op for English.
    with scoped_vector_scan(
        db, selected_senses[0].lexeme.language.code, mode="relaxed_order",
    ):
        rows = db.execute(
            statement.order_by(distance).limit(candidate_fetch_limit)
        ).all()

    logger.debug("fetched %d candidate rows from pgvector", len(rows))

    candidate_groups: dict[str, list[RerankCandidate]] = {}

    for embedding, raw_distance in rows:
        sense = embedding.sense

        if not word_length_allowed(
            sense,
            min_length=min_length,
            max_length=max_length,
        ):
            continue

        word_key = display_word_key_for_sense(sense)

        # The exact selected word is already shown as the 0th result.
        if word_key in displayed_word_keys:
            continue

        # Drop morphological variants of the query (lighted/lighting/lights).
        if is_morphological_variant_of_query(
            sense.lexeme.lemma,
            selected_lemmas,
            selected_stems,
        ):
            continue
        
        # Drop antonyms (dark/darkness for light) — they cluster by shared
        # context but are the opposite meaning.
        if normalize_text(sense.lexeme.lemma) in antonym_lemmas:
            continue

        distance_value = float(raw_distance)
        vector_score = max(0.0, 1.0 - distance_value)

        candidate_groups.setdefault(word_key, []).append(
            RerankCandidate(
                sense=sense,
                vector_score=vector_score,
            )
        )

    duplicate_candidates = [
        candidate
        for group in candidate_groups.values()
        for candidate in group
    ]

    duplicate_candidate_senses = [
        candidate.sense
        for candidate in duplicate_candidates
    ]

    sense_selection_counts = get_sense_selection_counts_for_senses(
        db,
        senses=duplicate_candidate_senses,
    )

    def duplicate_candidate_sort_key(
        candidate: RerankCandidate,
    ) -> tuple[int, float]:
        # If multiple candidate senses have the same displayed word, choose the
        # exact meaning that has been selected/searched most often.
        # If selection counts tie, keep the strongest vector match.
        return (
            sense_selection_counts.get(candidate.sense.id, 0),
            candidate.vector_score,
        )

    rerank_candidates_input = [
        max(group, key=duplicate_candidate_sort_key)
        for group in candidate_groups.values()
    ]

    reranked = rerank_candidates(
        candidates=rerank_candidates_input,
        sense_selection_counts=sense_selection_counts,
    )

    expanded_added = 0

    logger.debug("%d reranked candidates", len(reranked))
    if logger.isEnabledFor(logging.DEBUG):
        for r in reranked[:15]:
            logger.debug("  %-20s score=%.3f", r.sense.lexeme.lemma, r.final_score)

    for result in reranked:
        if expanded_added >= expansion_count:
            break

        if result.final_score < tree_floor:
            # reranked is sorted desc, so everything after is weaker.
            break

        hits.append(
            SenseSearchHit(
                sense=result.sense,
                match_type="expanded",
                score=result.final_score,
                reason=(
                    "pgvector_similarity_auto_reranked: "
                    + " | ".join(result.explanation_parts)
                ),
            )
        )
        expanded_added += 1

    return hits