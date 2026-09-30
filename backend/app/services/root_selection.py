"""
Root selection (MULTILINGUAL_EXPANSION_MODEL.md 2a): English sense -> one
root per target language, through the rungs below (Breakdown 4, Step 1a;
rung 4 `llm` added in Breakdown 4.5 — see the "rung 4" block below;
`pivoted_root` is assigned in parallel_expansion.py):

  corroborated  translation link AND shared ILI
  primary       translation link only
  ili           shared ILI only (no translation link)
  fallback      cross-language vector NN above the PAIR floor (Step 4);
                a pair whose floor is None has no fallback rung.

Tie-break within a rung: embedded+visible display sense required; then
cross-language cosine to the English sense's stored vector (valid WITHIN one
language pair -- constant anisotropy offset; never compared across pairs);
then lowest sense_index, then lemma (determinism).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.generated_name import Language
from app.models.semantic import (
    Lexeme, Sense, SenseEmbedding, SenseSynset, SenseTranslation,
)

from app.services.vector_scope import scoped_vector_scan
from app.utils.text import normalize_text

# Pair floors from scripts/eval/root_link_calibration.py (random p99 rule,
# Breakdown 4 Step 4b). None = no fallback rung for that pair.
# FILL FROM /tmp/root_calibration.txt BEFORE FIRST USE.
ROOT_FALLBACK_FLOORS: dict[str, float | None] = {
    "la": 0.882, "ru": 0.875, "ja": 0.865, "ar": 0.875,
    "ga": 0.873,
    "hi": 0.867, "sa": 0.871,
    "he": 0.877, "fa": 0.871,
    "de": 0.878, "pl": 0.871,
    "es": 0.886,
    "zh": 0.862, "ko": 0.868,
    "el": 0.874, "cy": 0.876, "sw": 0.878, "ang": 0.881, "non": 0.875, "is": 0.878,
}

# Guard for PIVOT-RESCUED roots (Breakdown 4, Step 6 revision). Set at each
# pair's POSITIVE p10 from root_link_calibration.py -- NOT random p99.
# Rationale: a rescued root is already backed by a translation link or shared
# ILI (hard evidence); the similarity check here only catches a synonym that
# drifted somewhere unrelated. Reusing the fallback floor would reject most
# CORRECT rescues -- e.g. la's fallback floor 0.882 sits ABOVE la's positive
# median 0.850, so lux/lumen (max 0.858 vs 'light') would fail it despite
# being right. Strictness is calibrated to the rung's evidence, not shared.
ROOT_RESCUE_FLOORS: dict[str, float | None] = {
    "la": 0.799, "ru": 0.800, "ja": 0.822, "ar": 0.784,
    "ga": 0.801,
    "hi": 0.799, "sa": 0.787,
    "he": 0.785, "fa": 0.787,
    "de": 0.815, "pl": 0.816,
    "es": 0.795,
    "zh": 0.817, "ko": 0.800,
    "el": 0.804, "cy": 0.799, "sw": 0.791, "ang": 0.791, "non": 0.788, "is": 0.796,
}

# Fix B — thin-corroboration override. The ladder returns on the first
# non-empty bucket, so a SINGLE weak translation link masks rung 3 entirely.
# Measured case: en 72902 'brave' -> es returns `bravo` (1 shared ILI) while
# rung 3 holds `valiente` (2 shared: i1475 + i1393) plus intrepido/impavido/
# audaz, all unseen. Override only when corroboration is minimal on one side
# and substantially stronger on the other; the floor guard is the pair's
# positive p10, same evidence-calibrated constant Fix A uses.
_OVERRIDE_MAX_WINNER_ILI = 1    # rung-1/2 winner shares at most this many
_OVERRIDE_MIN_POOL_ILI = 2      # rung-3 candidate must share at least this


@dataclass(frozen=True)
class RootCandidate:
    language_code: str
    sense: Sense
    rung: str           # corroborated | primary | ili | llm | fallback
                        # (+ pivoted_root, assigned in parallel_expansion.py)
    similarity: float   # cross-language cosine to the EN sense (tie-break record)


@dataclass(frozen=True)
class EnglishAnchor:
    """The four English-side values select_root needs, none of which depend on
    `language_code` (roadmap C4b).

    select_roots() previously re-fetched all four once per target language --
    ~80 identical queries per 21-language search -- because it is a bare dict
    comprehension over select_root.

    ⚠ MUST NOT be threaded into _pivot_root_rescue's select_root calls: those
    run on English SYNONYMS, where these values are correctly the synonym's,
    not the original query's. The rescue path passes no anchor and recomputes,
    which is the intended behaviour.
    """
    sense_id: int
    vector: object | None
    lemma: str | None
    definition: str | None
    ilis: set[str]


def build_english_anchor(db: Session, english_sense_id: int) -> EnglishAnchor:
    """Fetch once what select_root would otherwise fetch per language.

    `ilis` stays a plain `set`, deliberately: it feeds
    `SenseSynset.ili.in_(en_ilis)`, and changing the container type would
    change the rendered IN-list order. (That order is ALREADY process-dependent
    -- Python randomizes str hashing -- a pre-existing determinism caveat
    recorded in the Phase C findings, not something introduced here.)
    """
    lemma, definition = db.execute(
        select(Lexeme.lemma, Sense.definition)
        .join(Sense, Sense.lexeme_id == Lexeme.id)
        .where(Sense.id == english_sense_id)
    ).first() or (None, None)
    ilis = {
        ili for (ili,) in db.execute(
            select(SenseSynset.ili).where(SenseSynset.sense_id == english_sense_id)
        )
    }
    return EnglishAnchor(
        sense_id=english_sense_id,
        vector=_en_vector(db, english_sense_id),
        lemma=lemma,
        definition=definition,
        ilis=ilis,
    )


def _display_sense_scored(
    db: Session, lexeme_id: int, en_vector=None,
    en_lemma: str | None = None, en_definition: str | None = None,
) -> tuple[Sense, float, float] | None:
    """Core of _display_sense (see its docstring). Returns
    (sense, effective_score, true_sim) so callers that RANK across multiple
    lexemes (Fix C's homograph race; the llm rung's multi-candidate pick) can
    use the SAME bonus-inclusive score that picked the sense, instead of
    recomputing a bare cosine that discards the bonus and quietly undoes it.

    BUG THIS FIXES (found 8/10/26, testing the lemma-overlap bonus): using
    true_sim alone in the caller after this function used effective_score
    internally meant a correctly-bonus-selected LOW-cosine sense could make
    its own lexeme LOSE Fix C's cross-lexeme margin race to a homograph --
    trading "right lexeme, wrong sense" for "wrong lexeme entirely". Measured
    concretely on ko 'light': lexeme 2298400's best sense moved from idx=2
    (0.8663, wrong) to idx=3 (0.8493 raw / 0.8693 effective, right), and the
    OUTER loop's `sim = _cross_sim(...)` recomputed 0.8493, which lost to the
    dollar-counter lexeme's 0.8436+0.01 margin (0.8536) -- a race it had
    previously won 0.8663-to-0.8536.
    """
    if en_vector is None:
        s = db.scalars(
            select(Sense)
            .options(selectinload(Sense.lexeme).selectinload(Lexeme.language))
            .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
            .where(Sense.lexeme_id == lexeme_id,
                   Sense.visibility_status == "visible")
            .order_by(Sense.sense_index)
            .limit(1)
        ).first()
        return None if s is None else (s, 0.0, 0.0)

    rows = db.execute(
        select(Sense, SenseEmbedding.embedding.cosine_distance(en_vector))
        .options(selectinload(Sense.lexeme).selectinload(Lexeme.language))
        .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
        .where(Sense.lexeme_id == lexeme_id,
               Sense.visibility_status == "visible")
        .order_by(SenseEmbedding.embedding.cosine_distance(en_vector),
                  Sense.sense_index)
        .limit(_DISPLAY_RERANK_K)
    ).all()
    if not rows:
        return None

    key = _gloss_tokens(en_lemma) if en_lemma else set()
    use_bonus = bool(key) and not _en_query_is_hedged(en_definition)

    best_sense, best_true, best_eff = rows[0][0], max(0.0, 1.0 - float(rows[0][1])), None
    best_eff = best_true
    for sense, dist in rows:
        true_sim = max(0.0, 1.0 - float(dist))
        eff = true_sim
        if use_bonus and key <= _gloss_tokens(sense.definition):
            eff += _LEMMA_OVERLAP_BONUS
        if eff > best_eff:
            best_sense, best_true, best_eff = sense, true_sim, eff
    return (best_sense, best_eff, best_true)


def _display_sense(db: Session, lexeme_id: int, en_vector=None,
                   en_lemma: str | None = None,
                   en_definition: str | None = None) -> Sense | None:
    """Thin wrapper over _display_sense_scored for callers that only need the
    Sense. See _display_sense_scored's docstring for the full rationale and
    the bonus-propagation bug it exists to prevent -- any NEW caller that
    ranks this result against another lexeme/candidate should call
    _display_sense_scored directly and use effective_score, not this."""
    result = _display_sense_scored(db, lexeme_id, en_vector, en_lemma, en_definition)
    return None if result is None else result[0]

# Fix C -- widen a translation-linked lexeme id to every sibling sharing its
# normalized key (see _expand_homograph_lexemes), then require a sibling to
# beat every ORIGINALLY-linked candidate's TRUE cosine by more than this
# margin before it can win. Measured 8/7/26 (60-sample probe, ar/zh/ko/es):
# every damaging naive-argmax flip in the sample had a gap under 0.01 --
# es 'betrothed' (correctly linked to the noun/fiancee sense) would flip to
# a verb sense at +0.0057; zh 'ovum' (correctly linked) would flip to the
# SAME lexeme's unrelated 'testicles' sense at +0.0002, with identical POS
# on both sides, so the POS term (Fix C-POS, conditional) cannot catch it
# either. Confirmed-good swaps in the same sample mostly cleared 0.01.
# Single global constant, not per-language: candidates here are SIBLINGS
# within one language pair, so this is a within-pair noise floor, not the
# absolute-threshold problem ROOT_RESCUE_FLOORS exists for.
_HOMOGRAPH_SWAP_MARGIN = 0.01

# Short-gloss under-ranking (three confirmed UI-adjacent cases: 8/4, 8/7/26,
# plus 28 cases surfaced by display_sense_probe.py on 8/9/26, n=336).
# sense_embeddings.build_sense_text() embeds "<lemma>: <definition>" plus
# extra glosses plus synonyms, so a sense whose whole definition is one bare
# word embeds against far less anchoring context than a longer competitor on
# the SAME lexeme -- and systematically loses the cosine comparison against a
# full definitional English query passage. Two motivating cases:
#   ko 'light'  (en 23466): 'light' (5 chars, 0 syn) lost by 0.0170 to
#                           'fire (as a disaster)' (1 syn)
#   ja 'shadow' (en 50303): 'a shadow' scored LOWEST of three candidates
# In both, the CORRECT gloss contains the queried English lemma verbatim and
# the winner does not. Definition-token overlap does not separate them (ko's
# 'light' shares nothing with 'A source of illumination'); the lemma does.
#
# SCOPE, MEASURED NOT ASSUMED: display_sense_probe.py found lemma-overlap
# cases (28) outnumbered ~3.5:1 by cases where the winner is simply the
# shortest gloss on its lexeme WITHOUT sharing the lemma (96) -- e.g. pl
# 'wziąć' idx=27 'to take (to get hit)' beating 'to take (to grab with the
# hands)' on cosine alone, neither containing the token "take". This fix
# closes the LEMMA-OVERLAP slice only. The larger shortest-gloss population
# is NOT addressed here -- see the deferred re-embed option (findings,
# 8/9/26) for what would close it.
#
# THRESHOLD, AND ITS LIMIT: 0.02, read off the clean multi-word fixes in the
# same probe run (de 'violate' 0.0127, es 'capsize' 0.0130, de 'lengthwise'
# 0.0205, pl 'shakedown' 0.0224). NOT a safety margin -- gap size does not
# separate correct swaps from incorrect ones. ru 'brew' (en: "make a hot
# SOUP") flips from the correct 'to cook, to boil' to the wrong 'to brew
# (beer)' at gap 0.0012, an order of magnitude below the genuine fixes; no
# single constant can catch the fixes without also catching this. Measured
# rate: ~1 clear regression in 28 examined cases (~3.6%), against ~11-13
# clear genuine fixes. Accepted as a structural cost of a magnitude-only
# rule, recorded rather than tuned away.
_LEMMA_OVERLAP_BONUS = 0.02

# Hedge guard: skip the bonus when the ENGLISH QUERY definition itself
# signals imprecision. Motivated by es 'friend' (en 15703, "A person with
# whom one is VAGUELY or INDIRECTLY acquainted") -- the lemma match ('friend',
# gap 0.0393) is arguably LESS correct here than the shown 'acquaintance,
# known person', because the query is specifically hedging away from the
# close-friend sense. Targeted, not general-purpose: it does NOT catch
# ru 'brew' (a domain-specificity mismatch, not a hedge), so it is a partial
# mitigation, not a fix for the structural cost above.
_HEDGE_MARKERS = frozenset({
    "vaguely", "loosely", "somewhat", "broadly", "roughly",
    "especially", "chiefly", "particularly", "generally",
})

# Rerank depth. The bonus is small, so a candidate more than a few ranks down
# on raw cosine cannot win; bounding the fetch keeps this off the O(senses)
# path for the pathological lexemes (pl worst 60 senses, ko syllable stubs
# 16+) that Fix C's expansion can now put in front of it.
_DISPLAY_RERANK_K = 5

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _gloss_tokens(value: str | None) -> set[str]:
    """normalize_text() casefolds but does NOT strip punctuation, so a bare
    .split() yields 'fire;' and would miss a query lemma 'fire'."""
    return set(_TOKEN_RE.findall(normalize_text(value or "")))


def _en_query_is_hedged(definition: str | None) -> bool:
    return bool(_gloss_tokens(definition) & _HEDGE_MARKERS)


def _expand_homograph_lexemes(
    db: Session, lexeme_ids, language_id: int
) -> list[int]:
    """Widen a translation-linked lexeme set to every sibling on the same key.

    WHY: `kaikki_translations.py` stores ONE `target_lexeme_id` per
    normalized key, chosen by a map overwrite with no ORDER BY (extractor
    line ~118). It cannot do better -- Phase F runs before Phase G, so no
    embeddings exist at extraction time to score with. Measured exposure:
    48,437 links project-wide on keys carrying >1 visible lexeme (ar 36.5%,
    sa 29.6%, ja 25.2% ... ru 4.4%), with POS agreement on those keys
    running 10-43 points below the 85-99% baseline for unambiguous keys.

    The stored id therefore becomes a HINT, and the choice moves here, where
    `en_vector` exists. Ordering is: originally-linked ids first, then
    remaining siblings by ascending id -- so `max()` on an exact tie returns
    the previously-selected lexeme, i.e. ties preserve current behaviour.

    Note this fixes the LEXEME half of the Stage-4 `(LEXEME, SENSE)` pair;
    `_display_sense(..., en_vector)` fixed the SENSE half on 8/4/26.
    """
    ids = list(dict.fromkeys(lexeme_ids))
    if not ids:
        return []
    keys = [k for (k,) in db.execute(
        select(Lexeme.normalized_lemma)
        .where(Lexeme.id.in_(ids))
        .distinct()
    ) if k]
    if not keys:
        return ids
    seen = set(ids)
    out = list(ids)
    for (i,) in db.execute(
        select(Lexeme.id)
        .where(Lexeme.language_id == language_id,
               Lexeme.normalized_lemma.in_(keys))
        .order_by(Lexeme.id)
    ):
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _en_vector(db: Session, sense_id: int):
    return db.scalar(
        select(SenseEmbedding.embedding).where(SenseEmbedding.sense_id == sense_id)
    )


def _cross_sim(db: Session, en_vector, sense_id: int) -> float:
    if en_vector is None:
        return 0.0
    d = db.scalar(
        select(SenseEmbedding.embedding.cosine_distance(en_vector))
        .where(SenseEmbedding.sense_id == sense_id)
    )
    return max(0.0, 1.0 - float(d)) if d is not None else 0.0


def select_root(
    db: Session, *, english_sense_id: int, language_code: str,
    include_vector_fallback: bool = True,
    anchor: "EnglishAnchor | None" = None,
    lang: Language | None = None,
) -> RootCandidate | None:
    """
    `anchor` and `lang` are pure hoists (roadmap C4b): identical values,
    fetched once by select_roots instead of once per target language. Omitting
    them reproduces the original per-call fetches exactly -- which is what
    _pivot_root_rescue relies on, since it calls this on English SYNONYMS and
    the anchor must be the synonym's, not the original query's.
    """
    if lang is None:
        lang = db.scalars(
            select(Language).where(Language.code == language_code)
        ).first()
    if lang is None:
        return None

    if anchor is None or anchor.sense_id != english_sense_id:
        anchor = build_english_anchor(db, english_sense_id)
    en_vector = anchor.vector
    en_lemma, en_definition = anchor.lemma, anchor.definition
    en_ilis = anchor.ilis

    # --- rungs 1+2: translation links, split by ILI corroboration ----------
    linked_lexeme_ids = [
        lid for (lid,) in db.execute(
            select(SenseTranslation.target_lexeme_id)
            .where(SenseTranslation.sense_id == english_sense_id,
                   SenseTranslation.language_id == lang.id,
                   SenseTranslation.attachment != "llm",
                   SenseTranslation.target_lexeme_id.isnot(None))
            .distinct()
        )
    ]
    # Evidence-weighted ranking (Fix A). The first tuple slot is the count of
    # ILIs this candidate SHARES with the English sense -- corroboration
    # STRENGTH, not merely its presence. Measured motivation: es sense 6360
    # ('love'), where amor (4 shared ILIs, sim 0.8492) lost to querido
    # (1 shared, 0.8711) under cosine-only ranking.
    # A candidate earns its count only if it clears the pair's rescue floor
    # (positive p10); below that it ranks on similarity alone, so a
    # well-corroborated but semantically drifted candidate cannot win on
    # count alone. For the `primary` bucket every count is 0 by
    # construction, so its ordering is UNCHANGED from cosine-only.
    # Fix C -- the stored target_lexeme_id is a HINT, not a decision. Widen to
    # every lexeme on the same normalized key so the loop below can RANK them;
    # the extractor could not (Phase F precedes Phase G, no vectors exist yet).
    # _original_linked_ids MUST be captured before this expansion -- it is
    # what the swap margin below checks against.
    _original_linked_ids = set(linked_lexeme_ids)
    linked_lexeme_ids = _expand_homograph_lexemes(
        db, linked_lexeme_ids, lang.id)
    _floor = ROOT_RESCUE_FLOORS.get(language_code) or 0.0
    # One query for every candidate lexeme's ILIs instead of one per lexeme
    # (roadmap C4b). Byte-identical: `overlap` is a set-intersection SIZE,
    # which is order-independent.
    ilis_by_lexeme: dict[int, set[str]] = {}
    if linked_lexeme_ids:
        for lid_, ili in db.execute(
            select(Sense.lexeme_id, SenseSynset.ili)
            .join(Sense, Sense.id == SenseSynset.sense_id)
            .where(Sense.lexeme_id.in_(linked_lexeme_ids))
        ):
            ilis_by_lexeme.setdefault(lid_, set()).add(ili)

    corroborated: list[tuple[int, float, int, str, Sense, float]] = []
    primary: list[tuple[int, float, int, str, Sense, float]] = []
    for lid in linked_lexeme_ids:
        scored = _display_sense_scored(db, lid, en_vector, en_lemma, en_definition)
        if scored is None:
            continue
        disp, eff_score, sim = scored
        lex_ilis = ilis_by_lexeme.get(lid, set())
        overlap = len(lex_ilis & en_ilis) if en_ilis else 0
        # eff_score (not sim) feeds the cross-lexeme race: it's the SAME
        # bonus-inclusive number that picked disp within its own lexeme, so
        # a lexeme can't lose the homograph race purely because its correct
        # sense scored lower on raw cosine than a wrong one would have.
        _rank_sim = eff_score + (_HOMOGRAPH_SWAP_MARGIN
                                 if lid in _original_linked_ids else 0.0)
        entry = (
            overlap if sim >= _floor else 0,
            _rank_sim, -disp.sense_index, disp.lexeme.lemma, disp, sim,
        )
        (corroborated if overlap else primary).append(entry)
    linked_winner: tuple[str, int, float, Sense] | None = None
    for bucket, rung in ((corroborated, "corroborated"), (primary, "primary")):
        if bucket:
            n_shared, _rank, _, _, disp, sim = max(
                bucket,
                key=lambda e: (e[0], e[1], e[2], [-ord(ch) for ch in e[3]]),
            )
            linked_winner = (rung, n_shared, sim, disp)
            break

    # Fix B: a well-corroborated link wins outright and rung 3 is never
    # queried (unchanged hot path). Only a THIN link defers the decision.
    if linked_winner is not None and (
        not en_ilis or linked_winner[1] > _OVERRIDE_MAX_WINNER_ILI
    ):
        rung, _n, sim, disp = linked_winner
        return RootCandidate(language_code, disp, rung, sim)

    # --- rung 3: shared ILI, no translation link ---------------------------
    if en_ilis:
        # Fetch the shared-ILI COUNT per candidate, not just the id set.
        # es pools are the largest in the project (avg 1.75, 36.8% of senses
        # carry >=2 candidates, max 24) so this tie-break does real work.
        ili_overlap: dict[int, int] = {
            sid: int(n) for (sid, n) in db.execute(
                select(SenseSynset.sense_id,
                       func.count(func.distinct(SenseSynset.ili)))
                .join(Sense, Sense.id == SenseSynset.sense_id)
                .join(Lexeme, Lexeme.id == Sense.lexeme_id)
                .where(SenseSynset.ili.in_(en_ilis),
                       Lexeme.language_id == lang.id)
                .group_by(SenseSynset.sense_id)
            )
        }
        best: tuple[int, float, Sense] | None = None
        for sid, n_shared in ili_overlap.items():
            s = db.scalars(
                select(Sense)
                .options(selectinload(Sense.lexeme).selectinload(Lexeme.language))
                .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
                .where(Sense.id == sid, Sense.visibility_status == "visible")
            ).first()
            if s is None:
                continue
            sim = _cross_sim(db, en_vector, s.id)
            key = (n_shared if sim >= _floor else 0, sim)
            if best is None or key > (best[0], best[1]):
                best = (key[0], sim, s)
        if best is not None:
            n_pool, sim_pool, s_pool = best
            if linked_winner is not None:
                rung, n_win, sim_win, disp = linked_winner
                if (n_pool >= _OVERRIDE_MIN_POOL_ILI
                        and n_pool > n_win
                        and sim_pool >= _floor):
                    return RootCandidate(language_code, s_pool,
                                         "ili_override", sim_pool)
                return RootCandidate(language_code, disp, rung, sim_win)
            return RootCandidate(language_code, s_pool, "ili", sim_pool)

    # Rung 3 found nothing usable -- a thin link is still better than falling
    # through to the LLM/vector rungs.
    if linked_winner is not None:
        rung, _n, sim, disp = linked_winner
        return RootCandidate(language_code, disp, rung, sim)

    # --- rung 4: persisted LLM translation link (Breakdown 4.5) ------------
    # READS ONLY -- live proposing happens in root_llm.resolve_llm_root
    # (backfill, or query-time behind ROOT_LLM_QUERY_TIME). Fenced the other
    # way in rungs 1+2: attachment 'llm' rows never masquerade as curated.
    llm_lids = [
        lid for (lid,) in db.execute(
            select(SenseTranslation.target_lexeme_id)
            .where(SenseTranslation.sense_id == english_sense_id,
                   SenseTranslation.language_id == lang.id,
                   SenseTranslation.attachment == "llm",
                   SenseTranslation.target_lexeme_id.isnot(None))
            .distinct()
        )
    ]
    best_llm: tuple[float, float, Sense] | None = None   # (eff, true_sim, sense)
    for lid in llm_lids:
        scored = _display_sense_scored(db, lid, en_vector, en_lemma, en_definition)
        if scored is None:
            continue
        disp, eff_score, sim = scored
        if best_llm is None or eff_score > best_llm[0]:
            best_llm = (eff_score, sim, disp)
    if best_llm is not None:
        return RootCandidate(language_code, best_llm[2], "llm", best_llm[1])

    if not include_vector_fallback:
        return None
    return _vector_rung(db, lang, language_code, en_vector)

def _vector_rung(db: Session, lang: Language, language_code: str,
                 en_vector) -> RootCandidate | None:
    # (the exact former rung-4 body -- floors, scoped_vector_scan
    #  strict_order, limit 1 -- byte-for-byte)
    floor = ROOT_FALLBACK_FLOORS.get(language_code)
    if floor is None or en_vector is None:
        return None
    # Filtered vector query -> starvation-prone; see vector_scope.
    # strict_order: LIMIT 1 with no downstream rerank, so exact nearest
    # must hold. Measured affordable at this limit (ar 189ms worst case).
    with scoped_vector_scan(db, language_code, mode="strict_order"):
        row = db.execute(
            select(Sense, SenseEmbedding.embedding.cosine_distance(en_vector).label("d"))
            .options(selectinload(Sense.lexeme).selectinload(Lexeme.language))
            .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
            .join(Lexeme, Lexeme.id == Sense.lexeme_id)
            .where(Lexeme.language_id == lang.id,
                   Sense.visibility_status == "visible")
            .order_by("d").limit(1)
        ).first()
    if row is None:
        return None
    sense, dist = row
    sim = max(0.0, 1.0 - float(dist))
    if sim < floor:
        return None
    return RootCandidate(language_code, sense, "fallback", sim)

def vector_fallback_root(
    db: Session, *, english_sense_id: int, language_code: str,
) -> RootCandidate | None:
    """The demoted last-resort rung alone (Breakdown 4.5, decision 1a):
    measured 15-19% precision, now ordered AFTER pivoted_root rescue.
    Kept floored and labeled; candidate for per-language disablement
    (floor -> None) once Step 8's census shows its share."""
    lang = db.scalars(
        select(Language).where(Language.code == language_code)
    ).first()
    if lang is None:
        return None
    return _vector_rung(db, lang, language_code,
                        _en_vector(db, english_sense_id))

def select_roots(
    db: Session, *, english_sense_id: int, language_codes: list[str],
    include_vector_fallback: bool = True,
) -> dict[str, RootCandidate | None]:
    """Roadmap C4b: the four English-side values and the Language rows are
    fetched ONCE here rather than once per target language. Byte-identical:
    same values, fewer fetches. Ordering unchanged.
    """
    if not language_codes:
        return {}
    anchor = build_english_anchor(db, english_sense_id)
    langs = {
        row.code: row for row in db.scalars(
            select(Language).where(Language.code.in_(language_codes))
        )
    }
    return {
        code: select_root(db, english_sense_id=english_sense_id,
                          language_code=code,
                          include_vector_fallback=include_vector_fallback,
                          anchor=anchor, lang=langs.get(code))
        for code in language_codes
    }