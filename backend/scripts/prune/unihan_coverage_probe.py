#!/usr/bin/env python3
"""
Unihan coverage census (read-only, no DB, no project imports).

For a list of characters (one per line -- e.g. the output of
extract_cjk_chars.py), reports how many have an English kDefinition in the
Unihan database, and how "usable" those definitions look as a green-card
meaning: a definition like "one; a, single; whole; entire" is measured by
its FIRST clause only ("one; a, single" is not one meaning, it's four
options), since that first clause is what the join would actually use.

Get the Unihan files first (see the accompanying instructions for the
download command) -- this script only parses what's already on disk.

USAGE:
  python3 unihan_coverage_probe.py --chars chars.txt --unihan-dir Unihan/
"""
from __future__ import annotations

import argparse
import os
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


def first_clause_tokens(definition: str) -> list[str]:
    first = definition.split(";")[0]
    return [t.strip() for t in first.split(",") if t.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chars", required=True,
                    help="file, one character per line (# lines ignored)")
    ap.add_argument("--unihan-dir", required=True,
                    help="directory containing the extracted Unihan_*.txt files")
    ap.add_argument("--examples", type=int, default=15)
    args = ap.parse_args()

    with open(args.chars, encoding="utf-8") as f:
        chars = sorted({
            line.strip() for line in f
            if line.strip() and not line.startswith("#")
        })

    defs = load_kdefinition(args.unihan_dir)
    print(f"input characters ................ {len(chars)}")
    print(f"Unihan kDefinition entries ....... {len(defs)}")
    print()

    covered: list[tuple[str, str]] = []
    missing: list[str] = []
    tok_hist: Counter = Counter()

    for ch in chars:
        d = defs.get(ch)
        if d:
            covered.append((ch, d))
            toks = first_clause_tokens(d)
            tok_hist[min(len(toks), 5)] += 1
        else:
            missing.append(ch)

    def pct(n, d):
        return f"{100 * n / d:.2f}%" if d else "n/a"

    print(f"covered .......................... {len(covered)} "
          f"({pct(len(covered), len(chars))})")
    print(f"missing .......................... {len(missing)} "
          f"({pct(len(missing), len(chars))})")
    print()
    print("--- sample covered ---")
    for ch, d in covered[:args.examples]:
        print(f"    {ch}  {d}")
    print()
    print("--- sample missing (blank-under-zero-review candidates) ---")
    for ch in missing[:args.examples]:
        print(f"    {ch}")
    print()
    print("--- first-clause token-count histogram ---")
    print("    (1 = clean single-word meaning; 5 = 5-or-more, likely too")
    print("     broad to join cleanly -- inspect the covered samples above)")
    for k in sorted(tok_hist):
        label = str(k) if k < 5 else "5+"
        print(f"    {label}: {tok_hist[k]}")


if __name__ == "__main__":
    main()