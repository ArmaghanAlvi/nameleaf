"""
Stage 8 -- green-card PRESENTATION.

PURE READ, same contract as green_card_retrieval: nothing here writes or
commits. Retrieval answers "which names"; this module answers "what does a
card need in order to be rendered honestly", which is a different question
with different queries.

THE VARIANT/COGNATE SPLIT IS NOT ONE QUERY.
`name_variants.build_components` unions SAME-LANGUAGE edges only -- that
filter IS the Stage 5b containment rule -- so `cluster_id` is a
same-language structure BY DESIGN. Roadmap 9c's second grouping
("cross-language cognates") therefore cannot come from the cluster at all;
it is a one-hop, non-transitive walk over established_name_edges where
is_cross_language, off ANY member of the card's cluster.

Off the whole cluster, not just the card: the EQUIV_EN edges hang off
whichever member Wiktionary happened to gloss, and 7e deliberately prefers
"the member that actually matched" over the cluster head -- so restricting
the walk to the card itself would empty the cognate list for exactly the
cards 7e prefers to show.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.generated_name import Language
from app.models.semantic import (
    EstablishedName,
    EstablishedNameEdge,
    Lexeme,
)
from app.services.established_names import provenance_label
from app.services.green_card_retrieval import (
    MECH_HOMOGRAPH,
    MECH_TOKEN,
    GreenCard,
)

# Set in Breakdown E Step 6 from the Step-5 emission probe (findings 18.x);
# RAISED in Breakdown G Step 2 (findings §20.2).
#
# Global census (section 1, 1625 clusters): size median=2, p90=5, p99=22,
# max=83. Global census (section 2, 945 names with a cross-language edge):
# in-degree median=1, p90=5, p99=12, max=24 (worst hub: en:John).
#
# WHY THE ORIGINAL VALUES WERE WRONG, and it is a reasoning error rather
# than a measurement error: the caps were sized against the census
# DISTRIBUTION, as if the cost of showing a member were per-member layout.
# It is not. VariantDropdown renders into a scrolling container, so the only
# resource a cap bounds is response BYTES -- and truncating a 41-member
# family at 15 to save ~26 small objects buys nothing, while producing a
# user-visible "and 25 more not shown" on exactly the families a name search
# is most likely to be about (findings §19.1, F-3).
#
# These now sit ABOVE both census maxima with headroom, so no family in the
# corpus as it stands is ever truncated. Measured in Breakdown G Step 3
# against scripts/prune/green_payload_size_probe.py, with a 512 KB
# single-response ceiling agreed before the numbers existed.
VARIANT_CAP = 120   # true max reached: 82 (man/counsel/elf/defend/father/
                     # alexander/helper/defender, matching the corpus census
                     # max of 83 almost exactly). Headroom ~1.5x.
COGNATE_CAP = 80     # true max reached: 53 (father, house) -- ABOVE the
                     # per-name census max of 24, because a cluster's
                     # cognate list pools every member's in-degree, not one
                     # name's. The per-name census answered the wrong
                     # question for this cap; this probe is the correct
                     # measurement. Headroom ~1.5x, same ratio as VARIANT_CAP.

# Display strings for a DIRECT edge, keyed by (relation, card_is_source).
# Edge direction is meaningful: extract_edges reads the SOURCE's gloss, so
# (A, B, DIMINUTIVE_OF) means "A is a diminutive of B" -- and the label
# below describes B as it appears in A's dropdown.
_RELATION_LABELS: dict[tuple[str, bool], str] = {
    ("VARIANT_OF", True): "Canonical form",
    ("VARIANT_OF", False): "Variant",
    ("DIMINUTIVE_OF", True): "Full form",
    ("DIMINUTIVE_OF", False): "Diminutive",
    ("FEM_EQUIV", True): "Masculine counterpart",
    ("FEM_EQUIV", False): "Feminine counterpart",
    ("MASC_EQUIV", True): "Feminine counterpart",
    ("MASC_EQUIV", False): "Masculine counterpart",
    ("EQUIV_EN", True): "English equivalent",
    ("EQUIV_EN", False): "Equivalent name",
}


@dataclass(frozen=True)
class VariantView:
    lemma: str
    romanization: str | None
    language_code: str
    language_name: str
    relation: str
    is_cross_language: bool
    is_direct: bool        # direct edge, vs. same-cluster-only


@dataclass(frozen=True)
class GreenCardView:
    card: GreenCard
    language_name: str
    meaning_text: str | None
    provenance: str
    explanation: str
    variants: tuple[VariantView, ...]
    variant_total: int
    cognates: tuple[VariantView, ...]
    cognate_total: int


# --- batch loaders ---------------------------------------------------------

def _languages(db: Session) -> dict[int, tuple[str, str]]:
    return {
        row.id: (row.code or "", row.name)
        for row in db.execute(select(Language.id, Language.code, Language.name))
    }


def _homograph_lemmas(db: Session, cards) -> dict[int, str]:
    """Only for HOMOGRAPH-channel cards: provenance_label needs the word."""
    ids = {c.name.homograph_lexeme_id for c in cards
           if c.name.meaning_channel == "HOMOGRAPH"
           and c.name.homograph_lexeme_id is not None}
    if not ids:
        return {}
    return {
        row.id: row.lemma
        for row in db.execute(
            select(Lexeme.id, Lexeme.lemma).where(Lexeme.id.in_(sorted(ids)))
        )
    }


def _meaning_source_languages(db: Session, cards) -> dict[int, int]:
    """name_id -> language_id of the row a propagated meaning came from."""
    ids = {c.name.meaning_source_name_id for c in cards
           if c.name.meaning_source_name_id is not None}
    if not ids:
        return {}
    return {
        row.id: row.language_id
        for row in db.execute(
            select(EstablishedName.id, EstablishedName.language_id)
            .where(EstablishedName.id.in_(sorted(ids)))
        )
    }


def _cluster_members(db: Session, cluster_ids) -> dict[int, list[EstablishedName]]:
    if not cluster_ids:
        return {}
    rows = db.scalars(
        select(EstablishedName)
        .where(EstablishedName.cluster_id.in_(sorted(cluster_ids)))
        .order_by(EstablishedName.normalized_lemma, EstablishedName.id)
    ).all()
    out: dict[int, list[EstablishedName]] = {}
    for row in rows:
        # The WHERE clause guarantees cluster_id is non-NULL for every row
        # returned here; this assert converts that runtime guarantee into a
        # static one Pylance can see, rather than silencing the check.
        assert row.cluster_id is not None
        out.setdefault(row.cluster_id, []).append(row)
    return out


def _edges_touching(db: Session, name_ids) -> list[EstablishedNameEdge]:
    """Every edge with an endpoint in `name_ids`.

    One query rather than two (same-language for relation labels,
    cross-language for cognates): both endpoint columns are indexed
    (`source_name_id` via uq_established_name_edges_edge's leading column,
    `target_name_id` via ix_established_name_edges_target), and splitting
    would double the round trips for the same rows. Step 9 EXPLAINs it --
    an OR of two IN-lists is exactly the shape a planner can give up on.
    """
    if not name_ids:
        return []
    ordered = sorted(name_ids)
    return list(db.scalars(
        select(EstablishedNameEdge)
        .where(or_(EstablishedNameEdge.source_name_id.in_(ordered),
                   EstablishedNameEdge.target_name_id.in_(ordered)))
        .order_by(EstablishedNameEdge.id)
    ).all())


def _names_by_id(db: Session, ids) -> dict[int, EstablishedName]:
    if not ids:
        return {}
    return {
        row.id: row
        for row in db.scalars(
            select(EstablishedName)
            .where(EstablishedName.id.in_(sorted(ids)))
        ).all()
    }


# --- assembly --------------------------------------------------------------

def _explanation(card: GreenCard, language_name: str) -> str:
    bits: list[str] = []
    if MECH_TOKEN in card.mechanisms and card.matched_tokens:
        toks = ", ".join(f"\u201c{t}\u201d" for t in card.matched_tokens)
        where = "" if card.trigger.visible else " (not shown in these results)"
        bits.append(
            f"Its recorded meaning contains {toks}, reached from the English "
            f"word \u201c{card.trigger.lemma}\u201d{where}."
        )
    if MECH_HOMOGRAPH in card.mechanisms:
        bits.append(
            f"It is spelled identically to a {language_name} word in these "
            f"results."
        )
    if not bits:
        bits.append("Matched by name lookup.")
    return " ".join(bits)


def _variant_view(
    member: EstablishedName,
    relation: str,
    is_direct: bool,
    is_cross: bool,
    languages,
) -> VariantView:
    code, lang_name = languages.get(member.language_id, ("", ""))
    return VariantView(
        lemma=member.lemma,
        romanization=member.romanization,
        language_code=code,
        language_name=lang_name,
        relation=relation,
        is_cross_language=is_cross,
        is_direct=is_direct,
    )


def build_views(
    db: Session,
    cards: list[GreenCard],
    *,
    variant_cap: int = VARIANT_CAP,
    cognate_cap: int = COGNATE_CAP,
) -> list[GreenCardView]:
    """Ranked GreenCards -> render-ready views.

    AT MOST SIX queries regardless of how many cards came back (fewer when a
    batch has no HOMOGRAPH or EQUIV_PROPAGATED cards): languages, homograph
    lemmas, meaning-source languages, cluster members, edges, far-side names.
    Measured, not asserted -- scripts/prune/green_card_emission_probe.py
    counts them via `before_cursor_execute`.
    """
    if not cards:
        return []

    languages = _languages(db)
    homograph_lemmas = _homograph_lemmas(db, cards)
    meaning_src_lang = _meaning_source_languages(db, cards)

    cluster_ids = {c.name.cluster_id for c in cards
                   if c.name.cluster_id is not None}
    members_by_cluster = _cluster_members(db, cluster_ids)

    # Every id whose edges matter: the cards themselves plus every member of
    # their clusters (amendment 3 -- cognates hang off the family, not the
    # matched spelling).
    family_ids: set[int] = {c.name.id for c in cards}
    for members in members_by_cluster.values():
        family_ids.update(m.id for m in members)

    edges = _edges_touching(db, family_ids)
    far_ids = {e.source_name_id for e in edges} | {e.target_name_id
                                                  for e in edges}
    far_names = _names_by_id(db, far_ids - family_ids)
    for members in members_by_cluster.values():
        for m in members:
            far_names.setdefault(m.id, m)
    for c in cards:
        far_names.setdefault(c.name.id, c.name)

    direct: dict[tuple[int, int], tuple[str, bool]] = {}
    cross_by_source: dict[int, list[EstablishedNameEdge]] = {}
    for e in edges:
        direct.setdefault((e.source_name_id, e.target_name_id),
                          (e.relation_type, True))
        direct.setdefault((e.target_name_id, e.source_name_id),
                          (e.relation_type, False))
        if e.is_cross_language:
            cross_by_source.setdefault(e.source_name_id, []).append(e)
            cross_by_source.setdefault(e.target_name_id, []).append(e)

    views: list[GreenCardView] = []
    for card in cards:
        name = card.name
        code, lang_name = languages.get(name.language_id, ("", ""))

        src_lang_id = meaning_src_lang.get(name.meaning_source_name_id or -1)
        provenance = provenance_label(
            meaning_channel=name.meaning_channel,
            language_name=lang_name,
            homograph_lemma=homograph_lemmas.get(
                name.homograph_lexeme_id or -1),
            homograph_confidence_level=name.homograph_confidence,
            equiv_en_target=name.equiv_en_target,
            source_language_name=(languages.get(src_lang_id, ("", ""))[1]
                                  if src_lang_id else None),
        )

        members = [m for m in members_by_cluster.get(name.cluster_id or -1, [])
                   if m.id != name.id]
        variants: list[VariantView] = []
        for m in members:
            rel = direct.get((name.id, m.id))
            label = (_RELATION_LABELS.get((rel[0], rel[1]), "Related form")
                     if rel else "Related form")
            variants.append(_variant_view(m, label, rel is not None,
                                          False, languages))

        # Cognates: one hop off ANY family member, non-transitive, deduped.
        family = {name.id} | {m.id for m in members}
        seen: set[int] = set()
        cognates: list[VariantView] = []
        for fid in sorted(family):
            for e in cross_by_source.get(fid, []):
                other_id = (e.target_name_id if e.source_name_id == fid
                            else e.source_name_id)
                if other_id in family or other_id in seen:
                    continue
                other = far_names.get(other_id)
                if other is None:
                    continue
                seen.add(other_id)
                card_is_source = e.source_name_id == fid
                label = _RELATION_LABELS.get(
                    (e.relation_type, card_is_source), "Cognate")
                cognates.append(_variant_view(other, label, fid == name.id,
                                              True, languages))

        views.append(GreenCardView(
            card=card,
            language_name=lang_name,
            meaning_text=name.meaning_text,
            provenance=provenance,
            explanation=_explanation(card, lang_name),
            variants=tuple(variants[:variant_cap]),
            variant_total=len(variants),
            cognates=tuple(cognates[:cognate_cap]),
            cognate_total=len(cognates),
        ))
    return views