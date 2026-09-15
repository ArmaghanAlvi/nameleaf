"""Wordnet provenance registry — the single source of truth for which edge
provenances come from a wordnet, and what each one is allowed to do.

Three consumers previously kept independent lists and had to agree:
  * importers/wordnet_lmf.py       --provenance argparse choices
  * services/parallel_expansion.py pivot eligibility (zero wordnet edges)
  * services/sense_embeddings.py   which edges feed embedding TEXT
Two of the three failed SILENTLY when a provenance was missing. One list,
two explicit exclusion sets, no silent disagreement.

Adding a wordnet (Stage 5 of the pre-import roadmap): add it to
WORDNET_PROVENANCES, then decide whether it belongs in either exclusion set.
Both decisions are deliberate; neither has a safe default.
"""
from __future__ import annotations

# Every provenance that originates from a wordnet import.
WORDNET_PROVENANCES: frozenset[str] = frozenset({
    "oewn",      # Open English WordNet 2025 (English)
    "omw-ja",    # Open Multilingual Wordnet, Japanese
    "omw-arb",   # OMW Arabic — primary (density-preferred)
    "awn4",      # Arabic WordNet 4, AI-translated from OEWN — supplementary
    # --- 15-language batch, added as licenses cleared (Stage 5, 7/28/26) ---
    "omw-es",    # Spanish, via MCR 3.0 — CC BY 3.0
    "omw-el",    # Greek — license per manifest
    "omw-pl",    # Polish — license per manifest
    "omw-he",    # Hebrew — license per manifest
    "omw-cmn",   # Chinese (Mandarin) — license per manifest
    "odenet",    # German — CC BY-SA 4.0, share-alike
    "lsg",       # Irish (Líonra Séimeantach na Gaeilge) — CC BY-SA 4.0, as declared by the OMW LMF file imported
})

# Non-wordnet provenance, listed because the embedding allowlist needs it.
LEXICAL_PROVENANCES: frozenset[str] = frozenset({"kaikki"})

# EXCLUSION 1 — pivot eligibility.
# A language is pivot-eligible when it has ZERO wordnet synonym edges. English
# is excluded structurally (it is the pivot TARGET and always has oewn edges),
# so counting oewn here would be meaningless at best and, if a non-English
# language ever carried an oewn-provenance edge, wrong.
_NOT_COUNTED_FOR_PIVOT: frozenset[str] = frozenset({"oewn"})

# EXCLUSION 2 — embedding text.
# awn4 is AI-translated from OEWN. It is a legitimate EDGE tier (it is a real
# supplementary signal, deliberately ranked below omw-arb) but its surface
# forms must not enter the vector, which would launder machine translation
# into the semantic space. See MULTILINGUAL_EXPANSION_MODEL.md.
_NOT_EMBEDDABLE: frozenset[str] = frozenset({"awn4"})


def pivot_counting_provenances() -> frozenset[str]:
    """Provenances whose presence makes a language NOT pivot-eligible."""
    return WORDNET_PROVENANCES - _NOT_COUNTED_FOR_PIVOT


def embeddable_provenances() -> frozenset[str]:
    """Provenances whose synonyms may feed embedding TEXT."""
    return (WORDNET_PROVENANCES | LEXICAL_PROVENANCES) - _NOT_EMBEDDABLE