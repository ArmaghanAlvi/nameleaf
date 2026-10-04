from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.semantic import Sense
from app.utils.text import normalize_text


NO_SYNONYM_PENALTY = -0.05

GENERIC_PREFIXES = (
    "a source of ",
    "a kind of ",
    "a type of ",
    "a form of ",
    "a state of ",
    "a quality of ",
    "the quality of ",
    "a person who ",
    "someone who ",
    "something that ",
    "a thing that ",
    "a place where ",
    "the act of ",
    "the process of ",
)

BROAD_DOMAIN_TERMS = {
    "organ",
    "eye",
    "vision",
    "visual",
    "sight",
    "perception",
    "retina",
    "color",
    "colour",
    "camera",
    "photograph",
    "picture",
    "image",
    "object",
    "device",
    "instrument",
    "tool",
    "machine",
    "glass",
    "lens",
    "skin",
    "exposure",
    "wine",
    "island",
    "electricity",
    "electrical",
    "discharge",
    "cloud",
}

BROAD_DOMAIN_PHRASES = (
    "produced by",
    "resulting from",
    "exposure to",
    "made of",
    "passes through",
)

STOPWORDS = {
    "a",
    "an",
    "the",
    "of",
    "to",
    "in",
    "on",
    "for",
    "from",
    "by",
    "with",
    "and",
    "or",
    "as",
    "at",
    "is",
    "are",
    "was",
    "were",
    "be",
    "being",
    "been",
}


@dataclass(frozen=True)
class RerankCandidate:
    sense: Sense
    vector_score: float


@dataclass(frozen=True)
class RerankResult:
    sense: Sense
    vector_score: float
    final_score: float
    explanation_parts: tuple[str, ...]


def normalize_for_matching(text: str) -> str:
    return normalize_text(text).casefold()


def tokenize(text: str) -> set[str]:
    normalized = normalize_for_matching(text)
    tokens = set(re.findall(r"[a-z][a-z'-]{2,}", normalized))

    return {
        token
        for token in tokens
        if token not in STOPWORDS
    }


def generic_definition_penalty(
    candidate: Sense,
) -> tuple[float, str | None]:
    definition = normalize_for_matching(candidate.definition).strip()

    if not definition:
        return -0.08, "missing definition"

    if definition.startswith(GENERIC_PREFIXES):
        return -0.055, "generic definition pattern"

    if len(tokenize(definition)) <= 2:
        return -0.04, "very short/vague definition"

    return 0.0, None


def broad_domain_penalty(
    candidate: Sense,
) -> tuple[float, str | None]:
    definition = normalize_for_matching(candidate.definition)
    terms = tokenize(definition)

    matched_terms = sorted(terms & BROAD_DOMAIN_TERMS)
    matched_phrases = [
        phrase
        for phrase in BROAD_DOMAIN_PHRASES
        if phrase in definition
    ]

    if not matched_terms and not matched_phrases:
        return 0.0, None

    labels = matched_terms[:4] + matched_phrases[:2]

    return -0.05, f"broad-domain signal: {', '.join(labels)}"


def no_synonym_penalty(candidate: Sense) -> tuple[float, str | None]:
    """
    Penalize senses with no synonym relations. Such senses tend to have
    short, low-content embedded text that collapses toward the generic
    centroid and scores spuriously high against short queries (the
    qasgiq/jigha failure mode). Requires sense.relations to be loaded.
    """
    has_syn = any(
        rel.relation_type in ("synonym", "near_synonym")
        for rel in (getattr(candidate, "relations", None) or [])
    )
    if has_syn:
        return 0.0, None
    return NO_SYNONYM_PENALTY, "no synonym relations"


def rerank_candidates(
    *,
    candidates: list[RerankCandidate],
) -> list[RerankResult]:
    """Score = vector score plus definition-quality penalties, nothing else.

    No usage-popularity bonus (C2, cache plan Part A): search results never
    read usage statistics; they shape the sense dropdown only. That keeps
    results independent of traffic -- stable enough to cache, and not
    steerable by repeated searches.
    """
    results: list[RerankResult] = []

    for candidate in candidates:
        score = candidate.vector_score
        explanation_parts: list[str] = [
            f"vector={candidate.vector_score:.3f}",
        ]

        adjustments = [
            generic_definition_penalty(candidate.sense),
            broad_domain_penalty(candidate.sense),
            no_synonym_penalty(candidate.sense),
        ]

        for adjustment, reason in adjustments:
            score += adjustment

            if reason is not None and adjustment != 0:
                sign = "+" if adjustment > 0 else ""
                explanation_parts.append(f"{sign}{adjustment:.3f} {reason}")

        results.append(
            RerankResult(
                sense=candidate.sense,
                vector_score=candidate.vector_score,
                final_score=score,
                explanation_parts=tuple(explanation_parts),
            )
        )

    return sorted(
        results,
        key=lambda result: result.final_score,
        reverse=True,
    )