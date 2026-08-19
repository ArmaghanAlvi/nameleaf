from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.session import get_db
from app.models.generated_name import Language
from app.schemas.explore_v2 import (
    ExpandedSenseResponse,
    ExploreV2Request,
    ExploreV2Response,
    ExploreV2Result,
    HopPathStep,
    TreeSummary,
)
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