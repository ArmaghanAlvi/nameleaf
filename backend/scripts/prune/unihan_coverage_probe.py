#!/usr/bin/env python3
"""
Unihan coverage census (read-only, no DB, no project imports).

Stage 1e: the headline "99.89% covered" is meaningless if a share of those
definitions define nothing. This probe splits covered characters into USABLE
and four category-level exclusion buckets, and validates the extraction rule
("first comma-item of the first semicolon-clause") by reporting what that rule
actually produces -- including how often the first clause is junk while a
later clause is fine.

Every exclusion is a CATEGORY-LEVEL predicate over the definition text. No
per-character decisions: zero-review holds here as everywhere else.

USAGE:
  python3 scripts/prune/unihan_coverage_probe.py \
      --chars chars.txt --unihan-dir Unihan/ --examples 40
"""
from __future__ import annotations

import argparse
import os
import re
from collections import Counter


def load_kdefinition(unihan_dir: str) -> dict[str, str]:
    defs: dict[str, str] = {}
    for fname in ("Unihan_DictionaryLikeData.txt", "Unihan_Readings.txt"):
        path = os.path.join(unihan_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue
                cp, field, value = parts
                if field != "kDefinition" or not cp.startswith("U+"):
                    continue
                try:
                    ch = chr(int(cp[2:], 16))
                except ValueError:
                    continue
                defs[ch] = value
    return defs


# ---------------------------------------------------------------------------
# Category-level exclusion predicates, applied in this ORDER (first match
# wins). Order matters: "used in a person's name" hits both NAME_CIRCULAR and
# PHONETIC, and it is a name-circularity problem, not a phonetic one.
# ---------------------------------------------------------------------------

_UNIHAN_EXCLUSIONS: tuple[tuple[str, re.Pattern], ...] = (
    ("NAME_CIRCULAR", re.compile(
        r"\bused\s+(?:in|for|as)\b[^;]{0,40}\bnames?\b"
        r"|\b(?:girl|boy|female|male|woman|man|person|personal|given)'?s?\s+"
        r"names?\b"
        r"|^\s*(?:a\s+)?surname\b"
        r"|\bname\s+of\s+a\s+(?:person|man|woman|girl|boy)\b",
        re.IGNORECASE)),
    ("CROSS_REF", re.compile(
        r"\bsame\s+as\b"
        r"|\bnon-classical\b"
        r"|\b(?:variant|another|old|ancient|archaic|vulgar|corrupted|"
        r"simplified|standard|erroneous|original|abbreviated)\s+form\s+of\b"
        r"|\bvariant\s+of\b|\babbreviation\s+of\b|\bsee\s+[A-Z\u4e00-\u9fff]",
        re.IGNORECASE)),
    ("PHONETIC", re.compile(
        r"\bused\s+in\s+(?:transliterat\w+|foreign|translation)"
        r"|\bphonetic\b|\btransliterat\w+\b",
        re.IGNORECASE)),
    ("EMPTY", re.compile(
        r"^\s*(?:\(?\s*(?:meaning\s+)?unknown\s*\)?|\?+|[-\u2013\u2014])\s*$",
        re.IGNORECASE)),
)


def classify_clause(text: str) -> str:
    """USABLE, or the name of the first exclusion category that fires."""
    for label, rx in _UNIHAN_EXCLUSIONS:
        if rx.search(text or ""):
            return label
    return "USABLE"


def clauses(definition: str) -> list[str]:
    return [c.strip() for c in (definition or "").split(";") if c.strip()]


def first_item(clause: str) -> str:
    """The extraction rule under test: first comma-item of a clause."""
    return clause.split(",")[0].strip()


def extract_meaning(definition: str) -> tuple[str | None, str]:
    """
    Returns (meaning, outcome). Outcome is one of:
      CLAUSE1   -- first clause was usable; the rule as written worked
      CLAUSE_N  -- first clause was junk, a later clause was usable
                   (this is the 'clause-skipping rescue' 1e-3 measures)
      <LABEL>   -- every clause excluded, named by the FIRST clause's reason
      NO_CLAUSE -- empty definition
    """
    cl = clauses(definition)
    if not cl:
        return None, "NO_CLAUSE"
    if classify_clause(cl[0]) == "USABLE":
        return first_item(cl[0]), "CLAUSE1"
    for c in cl[1:]:
        if classify_clause(c) == "USABLE":
            return first_item(c), "CLAUSE_N"
    return None, classify_clause(cl[0])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chars", required=True,
                    help="file, one character per line (# lines ignored)")
    ap.add_argument("--unihan-dir", required=True,
                    help="directory containing the extracted Unihan_*.txt files")
    ap.add_argument("--examples", type=int, default=25,
                    help="samples printed per bucket. 1e asks for validation "
                         "against a LARGER sample -- use 40+.")
    args = ap.parse_args()

    with open(args.chars, encoding="utf-8") as f:
        chars = sorted({
            line.strip() for line in f
            if line.strip() and not line.startswith("#")
        })

    defs = load_kdefinition(args.unihan_dir)

    covered: list[tuple[str, str]] = []
    missing: list[str] = []
    for ch in chars:
        d = defs.get(ch)
        (covered.append((ch, d)) if d else missing.append(ch))

    whole_class: Counter = Counter()
    outcomes: Counter = Counter()
    tok_hist: Counter = Counter()
    samples: dict[str, list] = {}
    usable_pairs: list[tuple[str, str, str]] = []

    for ch, d in covered:
        whole_class[classify_clause(d)] += 1
        meaning, outcome = extract_meaning(d)
        outcomes[outcome] += 1
        samples.setdefault(outcome, [])
        if len(samples[outcome]) < args.examples:
            samples[outcome].append((ch, meaning, d))
        if meaning is not None:
            tok_hist[min(len(meaning.split()), 5)] += 1
            usable_pairs.append((ch, meaning, d))

    def pct(n, d):
        return f"{100 * n / d:.2f}%" if d else "n/a"

    n_chars = len(chars)
    n_cov = len(covered)
    usable = outcomes["CLAUSE1"] + outcomes["CLAUSE_N"]

    print("=" * 72)
    print("UNIHAN COVERAGE + BOILERPLATE CENSUS (Stage 1e)")
    print("=" * 72)
    print(f"  input characters ................ {n_chars}")
    print(f"  Unihan kDefinition entries ...... {len(defs)}")
    print(f"  covered ......................... {n_cov} ({pct(n_cov, n_chars)})")
    print(f"  missing ......................... {len(missing)} "
          f"({pct(len(missing), n_chars)})")
    print()
    print("--- WHOLE-DEFINITION classification (any clause boilerplate) ---")
    print("    (upper bound on contamination; a definition can be part junk)")
    for label, k in whole_class.most_common():
        print(f"  {label:<16} {k:>7} ({pct(k, n_cov)} of covered)")
    print()
    print("--- EXTRACTION OUTCOME (the rule under test) ---")
    for label in ("CLAUSE1", "CLAUSE_N", "NAME_CIRCULAR", "CROSS_REF",
                  "PHONETIC", "EMPTY", "NO_CLAUSE"):
        if label not in outcomes:
            continue
        print(f"  {label:<16} {outcomes[label]:>7} ({pct(outcomes[label], n_cov)})")
    print()
    print("=" * 72)
    print(f"  HONEST USABLE COVERAGE .......... {usable} "
          f"({pct(usable, n_chars)} of input characters)")
    print(f"  boilerplate share of covered .... "
          f"{pct(n_cov - usable, n_cov)}")
    print(f"  clause-skipping rescue .......... {outcomes['CLAUSE_N']} "
          f"({pct(outcomes['CLAUSE_N'], n_cov)} of covered)")
    print("=" * 72)
    print()
    print("--- extracted-item token count histogram (usable only) ---")
    print("    (1-3 = joins cleanly; 4+ = probably too broad for a name card)")
    for k in sorted(tok_hist):
        label = str(k) if k < 5 else "5+"
        print(f"    {label}: {tok_hist[k]}  ({pct(tok_hist[k], usable)})")
    print()
    for label in ("CLAUSE1", "CLAUSE_N", "NAME_CIRCULAR", "CROSS_REF",
                  "PHONETIC", "EMPTY"):
        if label not in samples:
            continue
        print(f"--- sample [{label}] ---")
        for ch, meaning, d in samples[label]:
            print(f"    {ch}  -> {meaning!r}")
            print(f"         full: {d}")
        print()
    print("--- sample missing (blank-under-zero-review candidates) ---")
    for ch in missing[:args.examples]:
        print(f"    {ch}")


if __name__ == "__main__":
    main()