"""
Tiered candidate generator.

expand() is the entry point the API calls. It surfaces candidates in tiers:
  1. selected senses themselves (the 0th result)
  2. Kaikki synonym edges       (curated, highest precision)
  3. OEWN synonym edges         (orphan rescue breadth)
  4. OEWN near_synonym edges    (looser)
  5. vector fallback            (expand_from_selected_senses)

Edge tiers run BEFORE vector search so curated distinct synonyms (radiance,
glow, shine) are surfaced directly, rather than competing in vector ranking
where morphological families (illumin-/lumin-) crowd them out.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.semantic import Lexeme, Sense, SenseEmbedding, SenseRelation
from app.models.generated_name import Language
from app.utils.text import normalize_lemma
from app.services.morphology import same_family
from app.services.vector_sense_search import (
    SenseSearchHit,
    get_selected_senses,
    collect_antonym_lemmas,
    is_morphological_variant_of_query,
    word_length_allowed,
    display_word_key_for_sense,
    expand_from_selected_senses,
    _morph_stem,
)


# Edge tiers in priority order per language: (provenance, relation_type, label).
# ENGLISH TUPLE IS BYTE-IDENTICAL to the original _EDGE_TIERS -- the en path
# must not change. Order encodes trust: human-curated kaikki first; for
# Arabic, omw-arb (human) before awn4 (AI-translated -- the findings doc
# requires awn4 be independently discountable, and tier order IS the
# discount); near_synonym tiers last. ru/la have no wordnet: kaikki only,
# then the vector fallback carries breadth (ru additionally has the pivot,
# which lives in parallel_expansion, not here).
_EDGE_TIERS_EN = (
    ("kaikki", "synonym", "kaikki_synonym"),
    ("oewn", "synonym", "oewn_synonym"),
    ("oewn", "near_synonym", "oewn_near_synonym"),
)
_EDGE_TIERS_BY_LANG: dict[str, tuple[tuple[str, str, str], ...]] = {
    "en": _EDGE_TIERS_EN,
    "ja": (("kaikki", "synonym", "kaikki_synonym"),
           ("omw-ja", "synonym", "omw_ja_synonym")),
    "ar": (("kaikki", "synonym", "kaikki_synonym"),
           ("omw-arb", "synonym", "omw_arb_synonym"),
           ("awn4", "synonym", "awn4_synonym"),
           ("awn4", "near_synonym", "awn4_near_synonym")),
    "ru": (("kaikki", "synonym", "kaikki_synonym"),),
    "la": (("kaikki", "synonym", "kaikki_synonym"),),
    # --- 15-language batch (Stage 8 pre-flight; INERT until each language
    # has rows -- landed early so per-wave Phase I is floors + restart only).
    # Wordnet-bearing (7): kaikki first, wordnet second -- tier order IS the
    # provenance discount (the ja/ar pattern). lsg additionally carries
    # `similar` relations, so ga gets a near_synonym tier (the awn4 pattern;
    # the only batch source that emits any -- OdeNet has no `similar`).
    # Pivot-lane (8): kaikki only (the ru/la pattern). Pivot ELIGIBILITY is
    # derived from the edge inventory, never from this dict.
    "es": (("kaikki", "synonym", "kaikki_synonym"),
           ("omw-es", "synonym", "omw_es_synonym")),
    "de": (("kaikki", "synonym", "kaikki_synonym"),
           ("odenet", "synonym", "odenet_synonym")),
    "pl": (("kaikki", "synonym", "kaikki_synonym"),
           ("omw-pl", "synonym", "omw_pl_synonym")),
    "he": (("kaikki", "synonym", "kaikki_synonym"),
           ("omw-he", "synonym", "omw_he_synonym")),
    "el": (("kaikki", "synonym", "kaikki_synonym"),
           ("omw-el", "synonym", "omw_el_synonym")),
    "zh": (("kaikki", "synonym", "kaikki_synonym"),
           ("omw-cmn", "synonym", "omw_cmn_synonym")),
    "ga": (("kaikki", "synonym", "kaikki_synonym"),
           ("lsg", "synonym", "lsg_synonym"),
           ("lsg", "near_synonym", "lsg_near_synonym")),
    "hi": (("kaikki", "synonym", "kaikki_synonym"),),
    "sa": (("kaikki", "synonym", "kaikki_synonym"),),
    "fa": (("kaikki", "synonym", "kaikki_synonym"),),
    "ko": (("kaikki", "synonym", "kaikki_synonym"),),
    "cy": (("kaikki", "synonym", "kaikki_synonym"),),
    "sw": (("kaikki", "synonym", "kaikki_synonym"),),
    "ang": (("kaikki", "synonym", "kaikki_synonym"),),
    "non": (("kaikki", "synonym", "kaikki_synonym"),),
    "is": (("kaikki", "synonym", "kaikki_synonym"),),  #Added immediately after 15 lang import
}


def _edge_tiers_for(language_code: str | None) -> tuple[tuple[str, str, str], ...]:
    return _EDGE_TIERS_BY_LANG.get(language_code or "en", _EDGE_TIERS_EN)


def _resolve_lemma_to_display_sense(
    db: Session,
    normalized_lemma: str,
    target_language: str | None,
    language_id: int,
) -> Sense | None:
    """
    Pick the sense to DISPLAY for a synonym target lemma.

    Prefers an embedded, visible sense (consistent with what the vector path
    can show), lowest sense_index first (primary sense). Returns None if the
    lemma has no embedded visible sense — we don't surface unembedded junk.
    """
    stmt = (
        select(Sense)
        .options(selectinload(Sense.lexeme).selectinload(Lexeme.language))
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .join(Language, Language.id == Lexeme.language_id)
        .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)  # embedded only
        .where(
            Lexeme.normalized_lemma == normalized_lemma,
            Lexeme.language_id == language_id,   # tree-scoped: an English
            # edge target 'lux' must resolve to English or nothing — never
            # to the Latin lexeme that shares the spelling.
            Sense.visibility_status == "visible",
        )
        .order_by(Sense.sense_index)
        .limit(1)
    )
    if target_language is not None:
        stmt = stmt.where(Language.name == target_language)
    return db.scalars(stmt).first()



_FAMILY_PENALTY_STEP = 0.03


def _apply_family_diversity_penalty(
    hits: list[SenseSearchHit],
) -> list[SenseSearchHit]:
    """
    Soft-throttle morphological families so one root can't monopolize result
    slots. Keeps the strongest member penalty-free; penalizes each successive
    same-family member, then re-sorts. Never demotes the selected hit.
    """
    selected = [h for h in hits if h.match_type == "selected"]
    expanded = [h for h in hits if h.match_type != "selected"]
    expanded.sort(key=lambda h: h.score, reverse=True)

    kept_lemmas: list[str] = []
    rescored: list[SenseSearchHit] = []
    for h in expanded:
        lemma = normalize_lemma(h.sense.lexeme.lemma, h.sense.lexeme.language.code)
        family_count = sum(1 for k in kept_lemmas if same_family(lemma, k))
        penalty = _FAMILY_PENALTY_STEP * family_count
        rescored.append(SenseSearchHit(
            sense=h.sense,
            match_type=h.match_type,
            score=max(0.0, h.score - penalty),
            reason=(h.reason + f" | family-penalty -{penalty:.2f}") if penalty else h.reason,
        ))
        kept_lemmas.append(lemma)

    rescored.sort(key=lambda h: h.score, reverse=True)
    return selected + rescored


def expand(
    db: Session,
    *,
    selected_sense_ids: list[int],
    expansion_count: int,
    target_language: str | None = None,
    min_length: int = 0,
    max_length: int = 30,
    restrict_edges_to_selected: bool = False,
) -> list[SenseSearchHit]:
    selected_senses = get_selected_senses(db, sense_ids=selected_sense_ids)
    if not selected_senses:
        return []

    selected_lemmas = {normalize_lemma(s.lexeme.lemma, s.lexeme.language.code) for s in selected_senses}
    # Tree language derived from the root — see the matching note in
    # vector_sense_search.expand_from_selected_senses.
    tree_language_id = selected_senses[0].lexeme.language_id
    selected_stems = {_morph_stem(s.lexeme.lemma) for s in selected_senses}
    antonym_lemmas = collect_antonym_lemmas(selected_senses)

    # Over-collect so the family penalty has alternatives to promote.
    collect_target = max(expansion_count * 3, expansion_count + 5)

    hits: list[SenseSearchHit] = []
    displayed: set[str] = set()

    # --- Tier 0: the selected senses themselves ---
    for sense in selected_senses:
        if not word_length_allowed(sense, min_length=min_length, max_length=max_length):
            continue
        key = display_word_key_for_sense(sense)
        if key in displayed:
            continue
        displayed.add(key)
        hits.append(SenseSearchHit(
            sense=sense, match_type="selected", score=1.0,
            reason="user_selected_meaning",
        ))

    def expanded_count() -> int:
        return len([h for h in hits if h.match_type == "expanded"])

    # --- Tiers 1-3: collect synonym-edge candidates ---
    if restrict_edges_to_selected:
        # Hop mode: ONLY the passed sense's own edges. Stops a non-primary
        # sense (foul=weather) from inheriting another sense's edges
        # (foul=dirty -> funky/unclean) via whole-lexeme collection.
        edge_source_sense_ids = [s.id for s in selected_senses]
    else:
        # Root mode: all senses of the lexeme. Edges concentrate on the
        # primary sense (OEWN attaches there by design), so we read edges
        # from ALL senses — including hidden Tier-B senses. This is
        # deliberate: post-purge (Stage 4), hidden senses are real meanings
        # (archaic/name/multiword), and their edges are legitimate synonym
        # paths. Do NOT add a visibility filter here; junk-sense edges were
        # deleted with their senses in the Stage 4 purge.
        selected_lexeme_ids = {s.lexeme_id for s in selected_senses}
        edge_source_sense_ids = [
            sid for (sid,) in db.execute(
                select(Sense.id).where(Sense.lexeme_id.in_(selected_lexeme_ids))
            ).all()
        ]

    edge_candidates: list[tuple[Sense, str]] = []
    seen_targets: set[str] = set()
    tree_language_code = selected_senses[0].lexeme.language.code
    for provenance, rel_type, reason in _edge_tiers_for(tree_language_code):
        edges = db.execute(
            select(SenseRelation.target_normalized).where(
                SenseRelation.from_sense_id.in_(edge_source_sense_ids),
                SenseRelation.relation_type == rel_type,
                SenseRelation.provenance == provenance,
            )
        ).all()
        for (norm,) in edges:
            if not norm or norm in seen_targets:
                continue
            seen_targets.add(norm)
            if norm in selected_lemmas or norm in antonym_lemmas:
                continue
            cand = _resolve_lemma_to_display_sense(
                db, norm, target_language, tree_language_id
            )
            if cand is None:
                continue
            if is_morphological_variant_of_query(
                cand.lexeme.lemma, selected_lemmas, selected_stems,
            ):
                continue
            if not word_length_allowed(cand, min_length=min_length, max_length=max_length):
                continue
            key = display_word_key_for_sense(cand)
            if key in displayed:
                continue
            displayed.add(key)
            edge_candidates.append((cand, reason))

    # --- Score edge candidates by cosine similarity to the query ---
    # Compute the query vector once here; reused for the vector fallback below
    # so the same text isn't embedded twice per expand() call.
    from app.services.embedding_provider import embed_query
    from app.services.vector_sense_search import build_query_text_from_selected_senses

    query_vector: list[float] | None = None

    def _get_query_vector() -> list[float]:
        nonlocal query_vector
        if query_vector is None:
            query_vector = embed_query(
                build_query_text_from_selected_senses(selected_senses)
            )
        return query_vector

    if edge_candidates:
        cand_ids = [c.id for c, _ in edge_candidates]
        dist_rows = db.execute(
            select(
                SenseEmbedding.sense_id,
                SenseEmbedding.embedding.cosine_distance(_get_query_vector()).label("d"),
            ).where(SenseEmbedding.sense_id.in_(cand_ids))
        ).all()
        dist_by_id = {sid: float(d) for sid, d in dist_rows}

        scored = sorted(
            (
                (max(0.0, 1.0 - dist_by_id.get(cand.id, 1.0)), cand, reason)
                for cand, reason in edge_candidates
            ),
            key=lambda x: x[0],
            reverse=True,
        )
        for score, cand, reason in scored:
            hits.append(SenseSearchHit(
                sense=cand, match_type="expanded", score=score, reason=reason,
            ))
            if expanded_count() >= collect_target:
                break

    # --- Tier 4: vector fallback for remaining slots ---
    if expanded_count() < collect_target:
        vector_hits = expand_from_selected_senses(
            db,
            selected_sense_ids=selected_sense_ids,
            expansion_count=collect_target - expanded_count() + len(displayed),
            target_language=target_language,
            min_length=min_length,
            max_length=max_length,
            query_vector=_get_query_vector(),
            selected_senses=selected_senses,
        )
        for vh in vector_hits:
            if vh.match_type == "selected":
                continue
            key = display_word_key_for_sense(vh.sense)
            if key in displayed:
                continue
            displayed.add(key)
            hits.append(vh)
            if expanded_count() >= collect_target:
                break

    # --- Family diversity throttle, then truncate to requested count ---
    hits = _apply_family_diversity_penalty(hits)
    selected_hits = [h for h in hits if h.match_type == "selected"]
    expanded_hits = [h for h in hits if h.match_type != "selected"][:expansion_count]
    return selected_hits + expanded_hits