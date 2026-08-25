from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import Literal, cast

from app.db.session import get_db
from app.models.generated_name import Language
from app.schemas.explore_v2 import (
    ExpandedSenseResponse,
    ExploreV2Request,
    ExploreV2Response,
    ExploreV2Result,
    GreenCardPayload,
    GreenVariant,
    HopPathStep,
    TreeSummary,
)
from app.services.green_card_retrieval import retrieve_green_cards
from app.services.green_card_view import GreenCardView, build_views
from app.services.parallel_expansion import parallel_expand
from app.services.sense_selection import record_sense_selection
from app.services.sense_display import sense_display_for
from app.services.multi_hop_expansion import multi_hop_expand, HopNode

router = APIRouter(prefix="/explore-v2", tags=["explore-v2"])


def _hopnode_to_result(
    node: HopNode, root_rung: str | None = None
) -> ExploreV2Result:
    sense = node.sense
    lexeme = sense.lexeme
    language = lexeme.language
    is_selected = node.depth == 0
    path = [
        HopPathStep(word=w, senseId=sid, depth=i)
        for i, (w, sid) in enumerate(zip(node.path, node.path_sense_ids))
    ]
    return ExploreV2Result(
        id=f"sense-{sense.id}",
        name=lexeme.lemma,
        category="translation",
        meaning=sense_display_for(sense).definition,
        language=language.name,
        explanation=(
            f"{lexeme.lemma} reached via {'>'.join(node.path)} "
            f"(hop {node.depth}, {node.provenance}, score {node.anchored_score:.3f})."
        ),
        matchType="exact" if is_selected else "expanded",
        matchedSenseId=sense.id,
        relationshipType=node.provenance,
        relationshipWeight=node.anchored_score,
        partOfSpeech=lexeme.part_of_speech,
        depth=node.depth,
        parentSenseId=node.parent_sense_id,
        provenance=node.provenance,
        path=path,
        languageCode=language.code,
        rootRung=root_rung if is_selected else None,
        # No extra query: romanization is a column on the already-loaded
        # Lexeme row that `lexeme.lemma` above came from.
        romanization=lexeme.romanization,
    )


def _variant_payload(v) -> GreenVariant:
    return GreenVariant(
        name=v.lemma,
        romanization=v.romanization,
        relationshipType=v.relation,
        languageCode=v.language_code or None,
        language=v.language_name,
        isCrossLanguage=v.is_cross_language,
        isDirect=v.is_direct,
    )


def _green_payload(view: GreenCardView) -> GreenCardPayload:
    card = view.card
    name = card.name
    return GreenCardPayload(
        nameId=name.id,
        # CHECK constraints (ck_established_names_name_type/_gender) enforce
        # these at the DB level; the Mapped[str] column type just can't say
        # so statically. GreenCardPayload's Literal fields are the actual
        # runtime check -- an out-of-set value raises ValidationError right
        # here rather than needing a redundant assert.
        nameType=cast(Literal["given", "surname", "patronymic"],
                      name.name_type),
        gender=cast(Literal["m", "f", "x", "u"], name.gender),
        isAlsoSurname=name.is_also_surname,
        provenanceLabel=view.provenance,
        meaningChannel=name.meaning_channel,
        homographConfidence=name.homograph_confidence,
        mechanisms=sorted(card.mechanisms),
        matchedTokens=list(card.matched_tokens),
        matchTier=card.tier,
        isGradient=card.is_gradient,
        triggerWord=card.trigger.lemma,
        triggerLanguageCode=card.trigger.language_code,
        triggerVisible=card.trigger.visible,
        clusterId=name.cluster_id,
        variants=[_variant_payload(v) for v in view.variants],
        variantTotal=view.variant_total,
        cognates=[_variant_payload(v) for v in view.cognates],
        cognateTotal=view.cognate_total,
    )


def _green_to_result(view: GreenCardView) -> ExploreV2Result:
    """A STANDALONE green card: its own row in `results`."""
    card = view.card
    name = card.name
    return ExploreV2Result(
        id=f"name-{name.id}",
        name=name.lemma,
        category="established",
        meaning=view.meaning_text or "",
        language=view.language_name,
        explanation=view.explanation,
        matchType="exact" if card.tier == 0 else "expanded",
        # The sense that WON the meaning waterfall -- the name's own row, not
        # the trigger's. `matchedSenseId` means "the sense this result is",
        # and substituting the trigger would make a green card claim to be an
        # English word.
        matchedSenseId=name.source_sense_id,
        relationshipType="+".join(sorted(card.mechanisms)),
        # No similarity score exists for a lexical match (7e).
        relationshipWeight=None,
        # Every established name's source lexeme is a `name` POS row
        # (populate_established_names.py filters on it), so this is a fact,
        # not a placeholder.
        partOfSpeech="name",
        depth=card.trigger.depth,
        # 7d/9e: None means "top level, under the query" -- the trigger was
        # the hidden English pass and is not on screen.
        parentSenseId=card.anchor_sense_id,
        provenance="established_name",
        path=[],
        languageCode=view.card.language_code or None,
        rootRung=None,
        romanization=name.romanization,
        green=_green_payload(view),
    )


def _attach_green_cards(
    results: list[ExploreV2Result], views: list[GreenCardView]
) -> list[ExploreV2Result]:
    """7c's gradient merge, done as a MUTATION of the yellow row rather than
    a second row.

    "Merge the two into one gradient card carrying both tags" read literally
    means one card. Emitting a second row would print the same string twice
    and, worse, would leave the yellow node's expansion children hanging off
    a card the user was told is really a name. Flipping the existing row's
    category preserves the tree, the children and the interleave position.

    Merges on `homograph_anchor_sense_id`, never `anchor_sense_id`: when
    English is displayed, a mechanism-1 trigger can win `_better_trigger`
    and leave the latter pointing at an English word.

    At most ONE gradient payload per yellow row: `Martin` the given name and
    `Martin` the surname are two rows at the established_names grain and both
    are gradient-eligible against the same word. The better-ranked one merges
    (views arrive in 7e rank order); the other ships standalone.
    """
    index: dict[int, int] = {}
    for position, result in enumerate(results):
        index.setdefault(result.matchedSenseId, position)

    merged: set[int] = set()
    appended: list[ExploreV2Result] = []
    for view in views:
        anchor = view.card.homograph_anchor_sense_id
        position = index.get(anchor) if (view.card.is_gradient
                                         and anchor is not None) else None
        if position is not None and position not in merged:
            merged.add(position)
            results[position] = results[position].model_copy(update={
                "category": "word-established",
                "green": _green_payload(view),
            })
        else:
            appended.append(_green_to_result(view))
    return results + appended


@router.post("", response_model=ExploreV2Response)
def explore_v2(
    request: ExploreV2Request,
    db: Session = Depends(get_db),
) -> ExploreV2Response:
    for sense_id in request.selectedSenseIds:
        record_sense_selection(
            db,
            sense_id=sense_id,
            query_text=request.queryText,
        )

    results: list[ExploreV2Result] = []
    expanded: list[ExpandedSenseResponse] = []

    width = request.width if request.width is not None else request.expansionCount

    if request.languageCodes is not None:
        # --- Parallel multilingual path (Breakdown 5) -----------------------
        # One tree per requested language; parallel_expansion owns root
        # acquisition (5-rung ladder + rescue + fallback), language scoping,
        # the ru pivot, and the interleave (root band, then round-robin).
        px = parallel_expand(
            db,
            english_sense_id=request.selectedSenseIds[0],
            language_codes=request.languageCodes,
            width=width,
            depth=request.depth,
            min_length=request.minLength,
            max_length=request.maxLength,
            # Stage 8b -- the one-word wiring Breakdown D left ready. Every
            # green-card meaning is English, so mechanism 1 needs an English
            # expansion even when English is not displayed. D-2: ~0% overhead
            # at `all` scope (the pass reuses trees["en"]), ~101% median on
            # ru_only, accepted per 15.2.
            include_english_pass=True,
        )
        lang_names: dict[str, str] = {
            row.code: row.name
            for row in db.execute(select(Language.code, Language.name))
            if row.code is not None
        }
        rung_by_root: dict[int, str] = {}
        summaries: list[TreeSummary] = []
        for code, tree in px.trees.items():
            rung = tree.root.rung if tree.root else (
                "selected" if code == "en" else None)
            if tree.nodes and rung is not None:
                rung_by_root[tree.nodes[0].sense.id] = rung
            summaries.append(TreeSummary(
                languageCode=code,
                language=lang_names.get(code, code),
                rootWord=tree.nodes[0].sense.lexeme.lemma if tree.nodes else None,
                rootRung=rung,
                nodeCount=len(tree.nodes),
                pivotedCount=tree.pivoted_count,
            ))
        for node in px.interleaved:
            if node.depth > 0:
                expanded.append(ExpandedSenseResponse(
                    senseId=node.sense.id,
                    word=node.sense.lexeme.lemma,
                    language=node.sense.lexeme.language.name,
                    definition=sense_display_for(node.sense).definition,
                    relationshipType=node.provenance,
                    weight=node.anchored_score,
                ))
            results.append(_hopnode_to_result(
                node,
                root_rung=rung_by_root.get(node.sense.id)
                if node.depth == 0 else None,
            ))
        # --- Stage 8b: green cards -----------------------------------------
        # ONLY on this path. The legacy branch below is the byte-identity
        # gate (capture_api_current.py sends no languageCodes and lands
        # there), so emitting a single green card into it would make
        # diff_reference.py's 0/160 impossible by construction.
        green_cards = retrieve_green_cards(
            db,
            english_nodes=list(px.english_pass),
            visible_nodes=px.interleaved,
            language_codes=request.languageCodes,
        )
        results = _attach_green_cards(results, build_views(db, green_cards))

        db.commit()
        return ExploreV2Response(
            selectedSenseIds=request.selectedSenseIds,
            expandedSenses=expanded,
            results=results,
            treeSummaries=summaries,
        )

    # --- Legacy single-tree path: BYTE-IDENTICAL, guarded by diff_reference.
    # Every request without languageCodes (harness included) routes here.
    if True:
        nodes = multi_hop_expand(
            db,
            root_sense_id=request.selectedSenseIds[0],
            width=width,
            depth=request.depth,
            target_language=request.language,
            min_length=request.minLength,
            max_length=request.maxLength,
        )
        for node in nodes:
            if node.depth > 0:  # expanded subset -> expandedSenses (lean shape)
                expanded.append(
                    ExpandedSenseResponse(
                        senseId=node.sense.id,
                        word=node.sense.lexeme.lemma,
                        language=node.sense.lexeme.language.name,
                        definition=sense_display_for(node.sense).definition,
                        relationshipType=node.provenance,
                        weight=node.anchored_score,
                    )
                )
            results.append(_hopnode_to_result(node))
            
    db.commit()

    return ExploreV2Response(
        selectedSenseIds=request.selectedSenseIds,
        expandedSenses=expanded,
        results=results,
    )