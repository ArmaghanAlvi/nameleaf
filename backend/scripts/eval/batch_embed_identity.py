"""
Is a batched encode bitwise-identical to a single encode? (roadmap C4c gate)

WHY: batched inference pads sequences to the batch's longest member and runs
the transformer at a different tensor shape; masking makes padded positions
mathematically irrelevant, but the surviving positions are computed by
differently-tiled kernels, so floating-point accumulation order can change.
MPS is the backend least likely to guarantee bitwise equality.

This probe uses REAL query texts pulled from the engine, not toy strings --
length distribution is exactly what drives padding.

USAGE (from backend/): python3 scripts/eval/batch_embed_identity.py
       [--export-texts PATH]   # also write the 64 texts, "query: " prefix
                               # included, as a JSON list (publishing B1)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np                                                    # noqa: E402
from sqlalchemy import select                                         # noqa: E402

from app.db.session import SessionLocal                               # noqa: E402
from app.models.semantic import Lexeme, Sense, SenseEmbedding         # noqa: E402
from app.services.embedding_provider import get_model                 # noqa: E402
from app.services.vector_sense_search import (                        # noqa: E402
    build_query_text_from_selected_senses, get_selected_senses,
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export-texts")
    args = ap.parse_args()

    with SessionLocal() as db:
        sids = [
            sid for (sid,) in db.execute(
                select(Sense.id)
                .join(Lexeme, Lexeme.id == Sense.lexeme_id)
                .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
                .where(Lexeme.language_id == 1,
                       Sense.visibility_status == "visible")
                .order_by(Sense.id).limit(64)
            )
        ]
        senses = get_selected_senses(db, sense_ids=sids)
        texts = [f"query: {build_query_text_from_selected_senses([s])}"
                 for s in senses]

    if args.export_texts:
        with open(args.export_texts, "w") as fh:
            json.dump(texts, fh, ensure_ascii=False, indent=1)
        print(f"wrote {len(texts)} texts to {args.export_texts}")

    lengths = sorted(len(t) for t in texts)
    print(f"{len(texts)} real query texts; char length "
          f"min={lengths[0]} median={lengths[len(lengths)//2]} max={lengths[-1]}")

    model = get_model()
    model.encode("query: warmup", normalize_embeddings=True)

    single = np.stack([
        model.encode(t, normalize_embeddings=True) for t in texts
    ])

    for bs in (8, 32, 64):
        batched = model.encode(texts, normalize_embeddings=True,
                               batch_size=bs, show_progress_bar=False)
        batched = np.asarray(batched)
        exact = bool(np.array_equal(single, batched))
        max_abs = float(np.max(np.abs(single - batched)))
        sims_s = single @ single[0]
        sims_b = batched @ batched[0]
        order_same = bool(np.array_equal(np.argsort(-sims_s), np.argsort(-sims_b)))
        print(f"batch_size={bs:3d}  exact={exact}  "
              f"max_abs_diff={max_abs:.3e}  rank_order_preserved={order_same}")

    one = np.asarray(model.encode([texts[0]], normalize_embeddings=True))[0]
    print(f"batch-of-1 exact: {np.array_equal(single[0], one)}  "
          f"max_abs={float(np.max(np.abs(single[0] - one))):.3e}")


if __name__ == "__main__":
    main()