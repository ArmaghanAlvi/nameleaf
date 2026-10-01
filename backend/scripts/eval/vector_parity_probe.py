"""
Vector parity probe (publishing B1, Part 7b): does the production image
produce the same query vectors as this Mac, on CPU with the same thread count?

SELF-CONTAINED ON PURPOSE. It imports only torch, sentence_transformers,
numpy and the stdlib -- no app config, no database -- so it runs unchanged
inside the production image with `--network none` and a read-only bind mount.
It builds the model the same way embedding_provider.get_model() does for
EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=N: set_num_threads first, then
SentenceTransformer(MODEL, device="cpu").

TEXTS ARE ENCODED VERBATIM. The input file comes from
`batch_embed_identity.py --export-texts`, whose texts already carry the
"query: " prefix. This probe adds nothing: `encode_inputs()` is the identity,
and the probe checks that on a test string at startup and refuses texts that
lack the prefix (a sign the file came from somewhere else). Each text is
encoded SINGLY, one call per text, normalize_embeddings=True -- the shape
embed_query uses.

USAGE (from backend/):
  # encode; JSON (metadata + base64 float32 vectors) goes to stdout
  python3 scripts/eval/vector_parity_probe.py --texts T.json --threads 2 > A.json
  # compare two encodings of the same texts
  python3 scripts/eval/vector_parity_probe.py --compare A.json B.json
"""
from __future__ import annotations

import argparse
import base64
import json
import platform
import statistics
import sys
import time

MODEL = "intfloat/multilingual-e5-base"
PREFIX = "query: "


def encode_inputs(texts: list[str]) -> list[str]:
    """The exact strings handed to model.encode: the texts, unchanged."""
    return list(texts)


def _check_verbatim(texts: list[str]) -> None:
    probe = ["query: verbatim check"]
    assert encode_inputs(probe) == probe, "encode_inputs must not alter text"
    bad = [t for t in texts if not t.startswith(PREFIX)]
    if bad:
        sys.exit(f"{len(bad)} texts lack the {PREFIX!r} prefix; "
                 f"first: {bad[0][:60]!r}")


def encode(texts_path: str, threads: int) -> None:
    import numpy as np
    import torch
    import sentence_transformers
    import transformers
    from huggingface_hub import try_to_load_from_cache
    from sentence_transformers import SentenceTransformer

    with open(texts_path) as fh:
        texts = json.load(fh)
    _check_verbatim(texts)

    torch.set_num_threads(threads)
    model = SentenceTransformer(MODEL, device="cpu")
    model.encode(PREFIX + "warmup", normalize_embeddings=True)

    vectors, times = [], []
    for t in encode_inputs(texts):
        t0 = time.perf_counter()
        v = model.encode(t, normalize_embeddings=True)
        times.append(time.perf_counter() - t0)
        vectors.append(np.asarray(v, dtype=np.float32))
    arr = np.stack(vectors)

    json.dump({
        "meta": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "torch": torch.__version__,
            "sentence_transformers": sentence_transformers.__version__,
            "transformers": transformers.__version__,
            "device": str(model.device),
            "torch_num_threads": torch.get_num_threads(),
            "config_json": try_to_load_from_cache(MODEL, "config.json"),
            "n_texts": len(texts),
            "median_single_encode_ms": round(statistics.median(times) * 1000, 3),
            "mean_single_encode_ms": round(statistics.fmean(times) * 1000, 3),
        },
        "shape": list(arr.shape),
        "vectors_b64": base64.b64encode(arr.tobytes()).decode("ascii"),
    }, sys.stdout, indent=1)
    print()


def _load(path: str):
    import numpy as np
    with open(path) as fh:
        d = json.load(fh)
    arr = np.frombuffer(base64.b64decode(d["vectors_b64"]), dtype=np.float32)
    return d["meta"], arr.reshape(d["shape"])


def compare(a_path: str, b_path: str) -> None:
    import numpy as np
    meta_a, a = _load(a_path)
    meta_b, b = _load(b_path)
    for label, meta in (("A", meta_a), ("B", meta_b)):
        print(f"{label}: {json.dumps(meta)}")
    if a.shape != b.shape:
        sys.exit(f"shape mismatch {a.shape} vs {b.shape}")
    diff = np.abs(a.astype(np.float64) - b.astype(np.float64))
    identical_rows = int(sum(np.array_equal(a[i], b[i]) for i in range(len(a))))
    per_row_max = diff.max(axis=1)
    order_same = bool(np.array_equal(np.argsort(-(a @ a[0])),
                                     np.argsort(-(b @ b[0]))))
    print(f"vectors={len(a)} dim={a.shape[1]}")
    print(f"all_bitwise_identical={bool(np.array_equal(a, b))}")
    print(f"bitwise_identical_vectors={identical_rows}/{len(a)}")
    print(f"max_abs_diff={float(diff.max()):.3e}")
    print(f"mean_abs_diff={float(diff.mean()):.3e}")
    print(f"per_vector_max_abs_diff: min={float(per_row_max.min()):.3e} "
          f"median={float(np.median(per_row_max)):.3e} "
          f"max={float(per_row_max.max()):.3e}")
    print(f"rank_order_vs_vector0_preserved={order_same}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--texts")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()
    if args.compare:
        compare(*args.compare)
    elif args.texts:
        encode(args.texts, args.threads)
    else:
        ap.error("pass --texts or --compare")


if __name__ == "__main__":
    main()
