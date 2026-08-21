"""
Green-card census N4 — VARIANT GRAPH risk probe (read-only, DB-side).

Extracts "this name is a variant/diminutive/equivalent of that name" edges
from glosses already in the DB (no Kaikki re-import), builds the implied
graph, and measures the one thing that decides whether the variant-dropdown
feature is easy or hard: how big do the connected components get?

Edge sources (all from `senses.definition`, already imported):
  EQUIV_EN        "equivalent to English <n>"            -- cross-language
  VARIANT_OF      "variant of <n>"                        -- same-language
  DIMINUTIVE_OF   "diminutive/pet form/short form/
                    hypocorism/hypocoristic/nickname of <n>" -- same-lang
  FEM_EQUIV       "feminine equivalent <n>[, <n>...]"  -- same-language
  MASC_EQUIV      "masculine equivalent <n>[, <n>...]" -- same-language

An edge only COUNTS (and only gets unioned into a component) if its target
string resolves to an existing name-type lexeme of the SAME requested type
(given or surname) in the target language. Targets that only resolve in the
OTHER type's index are counted separately as cross-type candidates and are
NEVER unioned -- that decision is left for a human to make after seeing the
numbers, not made silently by this probe.

Precision is deliberately loose (first-token-after-trigger extraction) --
this is a risk measurement, not the shipping extractor. Read the "unresolved
target samples" section to judge how much is lost to that looseness.

USAGE (from backend/):
  python3 scripts/prune/name_variant_probe.py --name-type given --lang en --examples 15
  python3 scripts/prune/name_variant_probe.py --name-type given --all
  python3 scripts/prune/name_variant_probe.py --name-type surname --all
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, os.getcwd())

from app.db.session import SessionLocal              # noqa: E402
from app.models.generated_name import Language       # noqa: E402
from app.models.semantic import Lexeme, Sense        # noqa: E402
from app.utils.text import normalize_lemma           # noqa: E402
from scripts.prune.name_inventory_probe import (     # noqa: E402
    classify_name_type,
)

# ---------------------------------------------------------------------------
# Trigger patterns -> raw remainder text following the trigger phrase.
# ---------------------------------------------------------------------------

TRIGGERS = {
    "EQUIV_EN": re.compile(r"\bequivalent to English\s+(.+)", re.IGNORECASE),
    "VARIANT_OF": re.compile(r"\bvariant of\s+(.+)", re.IGNORECASE),
    "DIMINUTIVE_OF": re.compile(
        r"\b(?:diminutive|pet form|short form|hypocorism|hypocoristic"
        r"|nickname)(?:\s+of|\s+for)?\s+(.+)",
        re.IGNORECASE,
    ),
    "FEM_EQUIV": re.compile(
        r"\bfeminine equivalents?\s+(.+)", re.IGNORECASE,
    ),
    "MASC_EQUIV": re.compile(
        r"\bmasculine equivalents?\s+(.+)", re.IGNORECASE,
    ),
}

_LEADING_STRIP = re.compile(
    r"^(?:the|a|an)\s+|^(?:male|female|unisex|masculine|feminine)\s+"
    r"|^(?:given name|surname)\s+",
    re.IGNORECASE,
)
_PAREN = re.compile(r"\([^)]*\)")
_SPLIT_SEP = re.compile(r"\s*,\s*|\s+or\s+|\s+and\s+", re.IGNORECASE)


def extract_target_candidates(remainder: str, cap: int = 3) -> list[str]:
    """
    Turn 'the male given name Dafydd' or 'Alexandra or Sandra, equivalent
    to...' into a short list of single-token candidate name strings. The
    boilerplate strip loops until stable since prefixes nest up to three
    layers deep ('the' + 'male' + 'given name' all before the actual name).
    """
    remainder = re.split(r"[.;]", remainder, maxsplit=1)[0]
    out = []
    for part in _SPLIT_SEP.split(remainder):
        part = _PAREN.sub("", part).strip()
        for _ in range(4):
            stripped = _LEADING_STRIP.sub("", part).strip()
            if stripped == part:
                break
            part = stripped
        if not part:
            continue
        first_tok = part.split()[0].strip(".,;:—-\u2019'\"")
        if first_tok:
            out.append(first_tok)
        if len(out) >= cap:
            break
    return out


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def node_key(lang_code: str, normalized_lemma: str) -> str:
    return f"{lang_code}:{normalized_lemma}"


def load_all_name_records(db):
    """
    ONE pass over every name-POS sense in the DB. Returns:
      records: list of (lang_code, lemma, normalized_lemma, ntype, gloss)
               for senses classified GIVEN or SURNAME (others dropped)
      index:   index[ntype][lang_code] = {normalized_lemma: lemma_display}
    Always scans ALL languages regardless of --lang, because an English
    equivalence target must resolve even when only e.g. `cy` was requested.
    """
    records: list[tuple[str, str, str, str, str]] = []
    index: dict[str, dict[str, dict[str, str]]] = {
        "GIVEN": defaultdict(dict), "SURNAME": defaultdict(dict),
    }

    lang_codes = dict(
        db.execute(select(Language.id, Language.code)).all()
    )

    stmt = (
        select(Sense)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .options(selectinload(Sense.lexeme))
        .where(Lexeme.part_of_speech == "name")
        .order_by(Sense.id)
    )
    for sense in db.scalars(stmt).yield_per(5000):
        lex = sense.lexeme
        code = lang_codes.get(lex.language_id)
        if not code:
            continue
        gloss = (sense.definition or "").strip()
        tags = list(sense.raw_tags or [])
        ntype = classify_name_type(gloss, tags)
        if ntype not in ("GIVEN", "SURNAME"):
            continue
        records.append((code, lex.lemma, lex.normalized_lemma, ntype, gloss))
        index[ntype][code][lex.normalized_lemma] = lex.lemma

    return records, index


def _component_report(uf: UnionFind, lang_of_node: dict[str, str],
                      label: str, top_n: int = 10, show: int = 40) -> int:
    """
    Print one graph's component structure. Returns the largest component size
    so the caller can print a side-by-side verdict block.

    Three graphs are reported by run(): ALL (reproduces the pre-1a behaviour,
    and is the regression check on this edit), SAME-LANGUAGE only, and
    CROSS-LANGUAGE only. Splitting them is the whole point of 1a: it is the
    difference between "cross-language equivalence never chains transitively"
    (a scoping rule) and "fan-out needs a cap" (a different design).
    """
    members: dict[str, list[str]] = defaultdict(list)
    for node in uf.parent:
        members[uf.find(node)].append(node)

    sizes = sorted((len(v) for v in members.values()), reverse=True)
    hist: Counter = Counter(sizes)
    non_trivial = [v for v in members.values() if len(v) > 1]
    largest = sizes[0] if sizes else 0

    print(f"--- COMPONENTS [{label}] ---")
    print(f"  nodes involved in >=1 edge ...... {sum(sizes)}")
    print(f"  components (size>1) ............. {len(non_trivial)}")
    print(f"  LARGEST COMPONENT ............... {largest}")
    for size in sorted(set(sizes), reverse=True)[:15]:
        print(f"      size {size:>4}: {hist[size]:>5} component(s)")
    for comp in sorted(members.values(), key=len, reverse=True)[:top_n]:
        if len(comp) <= 1:
            continue
        langs_in = sorted({lang_of_node.get(n, "?") for n in comp})
        shown = sorted(comp)[:show]
        more = len(comp) - len(shown)
        print(f"  size={len(comp):<5} langs={len(langs_in)} {langs_in}")
        print(f"      {shown}" + (f"  ... +{more} more" if more else ""))
    print()
    return largest


def run(records, index, wanted_type: str, langs: set[str] | None,
        example_cap: int, fanout_cap: int) -> None:
    ntype = "GIVEN" if wanted_type == "given" else "SURNAME"
    other_ntype = "SURNAME" if ntype == "GIVEN" else "GIVEN"

    # THREE graphs, unioned independently from the same edge stream.
    uf_all, uf_same, uf_cross = UnionFind(), UnionFind(), UnionFind()

    same_edges: Counter = Counter()           # by relation, same-language
    cross_edges: Counter = Counter()          # by relation, cross-language
    cross_type_edges: Counter = Counter()
    cross_type_samples: list = []
    unresolved_samples: Counter = Counter()
    fanout_hist: Counter = Counter()          # candidates extracted per match
    fanout_by_rel: dict[str, Counter] = {r: Counter() for r in TRIGGERS}
    edges_seen = 0
    edges_resolved = 0
    edges_cross_type = 0
    resolved_samples: dict[str, list] = {r: [] for r in TRIGGERS}
    lang_of_node: dict[str, str] = {}

    for code, lemma, norm_lemma, rtype, gloss in records:
        if rtype != ntype:
            continue
        if langs and code not in langs:
            continue

        source_node = node_key(code, norm_lemma)
        lang_of_node[source_node] = code

        for rel, rx in TRIGGERS.items():
            m = rx.search(gloss)
            if not m:
                continue
            target_lang = "en" if rel == "EQUIV_EN" else code
            cands = extract_target_candidates(m.group(1), cap=fanout_cap)
            fanout_hist[len(cands)] += 1
            fanout_by_rel[rel][len(cands)] += 1

            for cand in cands:
                edges_seen += 1
                norm_cand = normalize_lemma(cand, target_lang)

                own_idx = index[ntype].get(target_lang, {})
                other_idx = index[other_ntype].get(target_lang, {})

                if norm_cand in own_idx:
                    edges_resolved += 1
                    target_node = node_key(target_lang, norm_cand)
                    lang_of_node[target_node] = target_lang
                    uf_all.union(source_node, target_node)

                    # is_cross_language is decided by the ACTUAL languages,
                    # not by the relation label. An English source carrying
                    # EQUIV_EN points at English and is a SAME-language edge
                    # despite the name -- and Stage 2's schema stores
                    # is_cross_language per edge, so the probe must define it
                    # the way production will.
                    if target_lang == code:
                        same_edges[rel] += 1
                        uf_same.union(source_node, target_node)
                    else:
                        cross_edges[rel] += 1
                        uf_cross.union(source_node, target_node)

                    if len(resolved_samples[rel]) < example_cap:
                        resolved_samples[rel].append(
                            (f"{code}:{lemma}", f"{target_lang}:{cand}")
                        )
                elif norm_cand in other_idx:
                    edges_cross_type += 1
                    cross_type_edges[rel] += 1
                    if len(cross_type_samples) < example_cap * 3:
                        cross_type_samples.append(
                            (rel, f"{code}:{lemma} ({ntype})",
                             f"{target_lang}:{cand} ({other_ntype})")
                        )
                else:
                    unresolved_samples[cand] += 1

    scanned = sum(
        1 for c, _l, _n, r, _g in records
        if r == ntype and (not langs or c in langs)
    )

    print("=" * 72)
    print(f"VARIANT GRAPH   type={ntype}   "
          f"langs={'ALL' if not langs else ','.join(sorted(langs))}   "
          f"fanout_cap={fanout_cap}")
    print("=" * 72)
    print(f"  source senses scanned ........... {scanned}")
    print(f"  candidate edges seen ............ {edges_seen}")
    print(f"  edges resolved (same type) ...... {edges_resolved}")
    print(f"  edges resolved (CROSS type) ..... {edges_cross_type}")
    print(f"  edges unresolved ................ "
          f"{edges_seen - edges_resolved - edges_cross_type}")
    print()
    print("--- resolved edges by relation x language scope ---")
    print(f"  {'relation':<16} {'same-lang':>10} {'cross-lang':>11} {'total':>8}")
    for rel in TRIGGERS:
        tot = same_edges[rel] + cross_edges[rel]
        print(f"  {rel:<16} {same_edges[rel]:>10} {cross_edges[rel]:>11} {tot:>8}")
        for src, tgt in resolved_samples[rel]:
            print(f"      {src} -> {tgt}")
    print()
    print("--- FAN-OUT: candidates extracted per trigger match ---")
    for k in sorted(fanout_hist):
        print(f"  {k} candidate(s): {fanout_hist[k]}")
    print("  by relation (2+ candidate share is what a cap would remove):")
    for rel in TRIGGERS:
        multi = sum(v for k, v in fanout_by_rel[rel].items() if k >= 2)
        tot = sum(fanout_by_rel[rel].values())
        share = f"{100 * multi / tot:.1f}%" if tot else "n/a"
        print(f"      {rel:<16} multi={multi:>6} / {tot:<6} ({share})")
    print()
    print("--- CROSS-TYPE candidate edges (NOT unioned; for your decision) ---")
    for rel in TRIGGERS:
        if cross_type_edges[rel]:
            print(f"  {rel:<16} {cross_type_edges[rel]:>7}")
    for rel, src, tgt in cross_type_samples:
        print(f"      [{rel}] {src} -> {tgt}")
    print()
    print("--- top unresolved target strings (pattern-tuning feed) ---")
    for cand, k in unresolved_samples.most_common(20):
        print(f"    {k:>5}  {cand!r}")
    print()

    largest_all = _component_report(uf_all, lang_of_node, "ALL EDGES")
    largest_same = _component_report(uf_same, lang_of_node, "SAME-LANGUAGE ONLY")
    largest_cross = _component_report(uf_cross, lang_of_node, "CROSS-LANGUAGE ONLY")

    print("=" * 72)
    print(f"1a / 1b VERDICT INPUTS   type={ntype}   fanout_cap={fanout_cap}")
    print(f"  largest ALL ..................... {largest_all}")
    print(f"  largest SAME-LANGUAGE only ...... {largest_same}")
    print(f"  largest CROSS-LANGUAGE only ..... {largest_cross}")
    print("=" * 72)
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--examples", type=int, default=10,
                    help="sample rows printed per relation. NOT the fan-out "
                         "cap -- see --fanout-cap.")
    ap.add_argument("--fanout-cap", type=int, default=3,
                    help="max target candidates extracted per trigger match. "
                         "1b re-runs the whole probe with 1 to test whether "
                         "hub fan-out is what inflates the components.")
    ap.add_argument(
        "--name-type", choices=("given", "surname"), required=True,
        help="Run once per type -- given and surname are never combined "
             "into one component graph by this probe.",
    )
    args = ap.parse_args()

    langs = None if args.all else (set(args.lang) or None)
    if langs is None and not args.all:
        print("Specify --lang CODE (repeatable) or --all.", file=sys.stderr)
        sys.exit(2)

    with SessionLocal() as db:
        print("Loading all name-POS senses (one pass, all languages)...")
        records, index = load_all_name_records(db)
        print(f"Loaded {len(records)} GIVEN+SURNAME senses "
              f"(indices built for both types, all languages).\n")
        run(records, index, args.name_type, langs, args.examples,
            args.fanout_cap)


if __name__ == "__main__":
    main()