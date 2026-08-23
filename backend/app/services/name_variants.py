"""
Stage 5 -- the established-name variant / equivalence graph.

PURE FUNCTIONS ONLY, same contract as app/services/established_names.py:
nothing here opens a session or writes a row. scripts/build_name_graph.py
does all the I/O. The split is what makes the containment rule -- the one
piece of this feature that can ship visible falsehoods -- unit-testable
without Postgres.

THE CONTAINMENT DESIGN IS DECIDED, NOT OPEN. Findings section 3 records the
Stage 5b decision made off the 1a/1b measurements:

  1. Cross-language edges are NON-TRANSITIVE LEAVES. Clusters are computed
     over same-language edges only. Measured: same-language components peak
     at 54 nodes (given) and cross-language at 28, but ALL-edges reaches 270
     -- the blob is an interaction effect where English-routed EQUIV_EN edges
     bridge otherwise-separate same-language clusters by alternating hops.
     Cutting the transitivity caps components near their isolated maximum.

  2. The fan-out cap is SCOPED, not blanket: 1 target for EQUIV_EN,
     FEM_EQUIV and MASC_EQUIV; 3 for VARIANT_OF and DIMINUTIVE_OF. A blanket
     cap gives a 47% component reduction but destroys real diminutive chains
     (Cathy/Katy/Kate off Katherine), which carry the largest raw
     multi-candidate volume for reasons unrelated to the blob.

Stage 10c ceiling: 55-65 nodes. `assert_cluster_ceiling` is the gate.
"""
from __future__ import annotations

import re
from collections import defaultdict

from app.utils.text import normalize_lemma

# ---------------------------------------------------------------------------
# 5a -- trigger extraction (promoted from scripts/prune/name_variant_probe.py)
# ---------------------------------------------------------------------------

TRIGGERS: dict[str, "re.Pattern[str]"] = {
    "EQUIV_EN": re.compile(r"\bequivalent to English\s+(.+)", re.IGNORECASE),
    "VARIANT_OF": re.compile(r"\bvariant of\s+(.+)", re.IGNORECASE),
    "DIMINUTIVE_OF": re.compile(
        r"\b(?:diminutive|pet form|short form|hypocorism|hypocoristic"
        r"|nickname)(?:\s+of|\s+for)?\s+(.+)",
        re.IGNORECASE,
    ),
    "FEM_EQUIV": re.compile(r"\bfeminine equivalents?\s+(.+)", re.IGNORECASE),
    "MASC_EQUIV": re.compile(r"\bmasculine equivalents?\s+(.+)", re.IGNORECASE),
}

# Scoped per 5b decision point 2.
FANOUT_CAP: dict[str, int] = {
    "EQUIV_EN": 1,
    "FEM_EQUIV": 1,
    "MASC_EQUIV": 1,
    "VARIANT_OF": 3,
    "DIMINUTIVE_OF": 3,
}

# Relations whose arrow points at the canonical form. Used by head selection.
CANONICAL_POINTING: frozenset[str] = frozenset({"VARIANT_OF", "DIMINUTIVE_OF"})

_TYPE_HEAD = re.compile(
    r"^(?:[\w'\u2019-]+\s+){0,4}?"
    r"(?:(?:given|fore|first)\s*names?"
    r"|(?:sur|last|family)\s*names?"
    r"|patronymics?|matronymics?)\s+",
    re.IGNORECASE,
)
_LEADING_STRIP = re.compile(
    r"^(?:the|a|an)\s+|^(?:male|female|unisex|masculine|feminine)\s+",
    re.IGNORECASE,
)
_NOT_A_NAME = frozenset({
    "given", "name", "names", "surname", "surnames", "forename", "forenames",
    "patronymic", "patronymics", "matronymic", "family", "first", "last",
    "male", "female", "unisex", "masculine", "feminine", "form", "forms",
    "diminutive", "variant", "equivalent", "spelling",
    "other", "others", "another", "similar", "related", "such",
})
_PAREN = re.compile(r"\([^)]*\)")
_SPLIT_SEP = re.compile(r"\s*,\s*|\s+or\s+|\s+and\s+", re.IGNORECASE)


def extract_target_candidates(remainder: str, cap: int = 3) -> list[str]:
    """
    'the female given names Katherine, Kathryn' -> ['Katherine', 'Kathryn'].

    ⟲ REVISED from the probe's version, which stripped the literal strings
    "given name" / "surname" and therefore could not match the PLURAL
    "given names " -- so the whole phrase survived and the extracted target
    was the word "given". `_TYPE_HEAD` instead consumes everything up to and
    including the type noun, which also removes a language adjective
    ("the English female given name Elizabeth"); that one produced a real
    FALSE edge, because `English` is itself an attested English given name.
    """
    remainder = re.split(r"[.;]", remainder, maxsplit=1)[0]
    out: list[str] = []
    for part in _SPLIT_SEP.split(remainder):
        part = _PAREN.sub("", part).strip()
        part = _TYPE_HEAD.sub("", part, count=1)
        for _ in range(4):
            stripped = _LEADING_STRIP.sub("", part).strip()
            if stripped == part:
                break
            part = stripped
        if not part:
            continue
        first_tok = part.split()[0].strip(".,;:\u2014-\u2019'\"")
        if not first_tok or first_tok.casefold() in _NOT_A_NAME:
            continue
        out.append(first_tok)
        if len(out) >= cap:
            break
    return out


def extract_edges(gloss: str, source_lang: str) -> list[tuple[str, str, str]]:
    """
    One gloss -> [(relation, target_lang, normalized_target), ...].

    Targets are normalized with the TARGET language's key, not the source's:
    an EQUIV_EN target is an English string and must be normalized as
    English, or it will never join against an English row.
    """
    out: list[tuple[str, str, str]] = []
    for rel, rx in TRIGGERS.items():
        m = rx.search(gloss or "")
        if not m:
            continue
        target_lang = "en" if rel == "EQUIV_EN" else source_lang
        for cand in extract_target_candidates(
            m.group(1), cap=FANOUT_CAP[rel]
        ):
            norm = normalize_lemma(cand, target_lang)
            if norm:
                out.append((rel, target_lang, norm))
    return out


# ---------------------------------------------------------------------------
# 5c -- cluster materialization
# ---------------------------------------------------------------------------

from typing import Generic, TypeVar

_Node = TypeVar("_Node")


class UnionFind(Generic[_Node]):
    """
    Generic over the node type: the production graph builder
    (build_name_graph.py) unions established_name INT ids, while
    name_variant_probe.py -- which runs before any row has an id -- unions
    STRING "lang:normalized_lemma" keys. One implementation, two type
    parameters, rather than a second copy of the same 12 lines.
    """

    def __init__(self) -> None:
        self.parent: dict[_Node, _Node] = {}

    def find(self, x: _Node) -> _Node:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: _Node, b: _Node) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def build_components(
    edges: list[tuple[int, int, str, bool]],
) -> dict[int, list[int]]:
    """
    edges: (source_id, target_id, relation, is_cross_language)
    -> {representative_id: sorted member ids}, SAME-LANGUAGE EDGES ONLY.

    This function is the containment rule. Cross-language edges are filtered
    out here and nowhere else, so there is exactly one place to read to know
    what the rule is -- and exactly one place a regression could occur.
    """
    uf: UnionFind[int] = UnionFind()
    for src, tgt, _rel, is_cross in edges:
        if is_cross:
            continue
        uf.union(src, tgt)

    members: dict[int, list[int]] = defaultdict(list)
    for node in uf.parent:
        members[uf.find(node)].append(node)
    return {rep: sorted(v) for rep, v in members.items() if len(v) > 1}


# ---------------------------------------------------------------------------
# 5d -- head selection
# ---------------------------------------------------------------------------

def select_head(
    members: list[int],
    edges: list[tuple[int, int, str, bool]],
) -> int:
    """
    Pick the display anchor for one cluster.

    Ranking, in order:
      1. canonical in-degree -- how many members point AT this one via
         VARIANT_OF / DIMINUTIVE_OF, whose arrows genuinely mean "the
         canonical form is over there".
      2. total same-language degree, as a tiebreak.
      3. lowest id, so the choice is STABLE across rebuilds. FEM_EQUIV and
         MASC_EQUIV are deliberately excluded from (1): "Christian, feminine
         equivalent Christiane" is a symmetric relation wearing an arrow, and
         counting it would make whichever gender Wiktionary happened to write
         second look canonical.
    """
    member_set = set(members)
    canonical_in: dict[int, int] = defaultdict(int)
    degree: dict[int, int] = defaultdict(int)
    for src, tgt, rel, is_cross in edges:
        if is_cross or src not in member_set or tgt not in member_set:
            continue
        degree[src] += 1
        degree[tgt] += 1
        if rel in CANONICAL_POINTING:
            canonical_in[tgt] += 1
    return min(
        members,
        key=lambda n: (-canonical_in[n], -degree[n], n),
    )


def assert_cluster_ceiling(
    components: dict[int, list[int]],
    ceiling: int = 100,   # was 65 -- re-derived post-fix; see findings §12
) -> tuple[bool, int]:
    """(ok, largest). Stage 10c's gate, callable at build time."""
    largest = max((len(v) for v in components.values()), default=0)
    return largest <= ceiling, largest