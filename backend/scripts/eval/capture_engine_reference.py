"""
Capture multi_hop_expand's Pure B output across the probe set
at every breadth x depth cell. This is the reference the unified API route

Must reproduce end-to-end. Read-only against the engine.
"""
import argparse
import os
import sys
import json
import contextlib

sys.path.insert(0, os.getcwd())

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.semantic import Sense, Lexeme, SenseEmbedding
from app.services.multi_hop_expansion import multi_hop_expand
from app.services.sense_selection import get_sense_selection_counts_for_senses
from app.utils.text import normalize_text

PROBE_WORDS = [
    "brave", "light", "storm", "river", "calm",
    "joy", "shadow", "fierce", "gold", "whisper",
]

# breadth x depth grid from the result-count model.
GRID = [(b, d) for b in (0, 1, 2, 3) for d in (0, 1, 2, 3)]


def root_id(db, word):
    norm = normalize_text(word)
    r = db.scalars(
        select(Sense).join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
        .where(Lexeme.normalized_lemma == norm, Sense.visibility_status == "visible")
        .order_by((Lexeme.part_of_speech != "noun"), Sense.sense_index)
    ).first()
    return r.id if r else None


def most_used_sense_id(db, word):
    """
    Resolve `word` to the sense users actually pick: highest selection_count
    first, tie-broken by (and fully falling back to) the noun-first root_id
    rule when the word has no selection history. Zero-review: the ranking
    signal is derived from recorded SenseSelectionStat data, no hand-picking.
    """
    norm = normalize_text(word)
    # Candidate senses: embedded + visible (same base filter as root_id).
    candidates = db.scalars(
        select(Sense)
        .join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
        .where(Lexeme.normalized_lemma == norm,
               Sense.visibility_status == "visible")
    ).all()
    if not candidates:
        return None

    counts = get_sense_selection_counts_for_senses(db, senses=candidates)
    # Pick the most-selected candidate; if every count is 0 (no history),
    # fall back to the deterministic noun-first rule.
    best_id = max(candidates, key=lambda s: counts.get(s.id, 0)).id
    if counts.get(best_id, 0) == 0:
        return root_id(db, word)
    return best_id


def capture_cell(db, sid, breadth, depth):
    # Mirror how the engine will be called for a given breadth/depth.
    # width=breadth, depth=depth. The engine returns [root] when either <= 0.
    with open(os.devnull, "w") as dn, contextlib.redirect_stdout(dn):
        nodes = multi_hop_expand(
            db, root_sense_id=sid, width=breadth, depth=depth,
            target_language=None, min_length=0, max_length=30,
        )
    return [
        {
            "word": n.path[-1],
            "sense_id": n.sense.id,
            "depth": n.depth,
            "provenance": n.provenance,
            "anchored_score": round(n.anchored_score, 4),
            "path": list(n.path),
        }
        for n in nodes
    ]


def main():
    # --out (publishing B1) writes elsewhere so a baseline capture never
    # overwrites the tracked reference; the default is unchanged.
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="scripts/eval/engine_reference.json")
    # --reuse-from (publishing C2): take each word's root_sense_id from a
    # prior capture instead of most_used_sense_id, which picks roots from
    # usage statistics. Pinning is what makes a comparison across databases
    # (e.g. production, whose live tables start empty) meaningful.
    ap.add_argument("--reuse-from")
    args = ap.parse_args()

    pinned: dict[str, int | None] | None = None
    if args.reuse_from:
        with open(args.reuse_from) as f:
            prior = json.load(f)
        pinned = {word: data.get("root_sense_id")
                  for word, data in prior.items()}
        print(f"reusing root senses from {args.reuse_from}: {pinned}")

    out = {}
    with SessionLocal() as db:
        for word in PROBE_WORDS:
            sid = (pinned.get(word) if pinned is not None
                   else most_used_sense_id(db, word))
            if sid is None:
                out[word] = {"skipped": "no embedded visible sense"}
                continue
            out[word] = {"root_sense_id": sid, "cells": {}}
            for breadth, depth in GRID:
                key = f"b{breadth}_d{depth}"
                out[word]["cells"][key] = capture_cell(db, sid, breadth, depth)

    path = args.out
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {path}")
    # quick human-readable summary for spot-checking
    for word, data in out.items():
        if "skipped" in data:
            print(f"{word:10s} SKIPPED")
            continue
        b3d3 = data["cells"]["b3_d3"]
        b3d1 = data["cells"]["b3_d1"]
        print(f"{word:10s} b3d1={len(b3d1):2d}  b3d3={len(b3d3):2d}  "
              f"top@b3d1={[c['word'] for c in b3d1 if c['depth']>0][:3]}")


if __name__ == "__main__":
    main()