"""
Diff api_current.json against engine_reference.json. Prints only cells where
the displayed word sequence differs. The printed cells are exactly what
Stage 2's route unification must bring into agreement.

--engine / --api (publishing B1) point it at other captures of the same
shape, e.g. two engine_reference captures; the defaults are unchanged.
"""
import argparse
import json


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", default="scripts/eval/engine_reference.json")
    ap.add_argument("--api", default="scripts/eval/api_current.json")
    args = ap.parse_args()

    with open(args.engine) as f:
        engine = json.load(f)
    with open(args.api) as f:
        api = json.load(f)

    total, diffs = 0, 0
    for word, edata in engine.items():
        if "skipped" in edata:
            continue
        for key, ecells in edata["cells"].items():
            total += 1
            ewords = [c["word"] for c in ecells]
            acells = api[word]["cells"].get(key, [])
            awords = [c["word"] for c in acells]
            if ewords != awords:
                diffs += 1
                print(f"{word} {key}:")
                print(f"    engine: {ewords}")
                print(f"    api   : {awords}")

    print(f"\n{diffs}/{total} cells differ")


if __name__ == "__main__":
    main()
