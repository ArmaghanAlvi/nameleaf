"""
Search-size timing grid (publishing B3). READ-ONLY.

Drives `phase_timing.py` -- through `device_report.py`, as SUBPROCESSES -- over
every width x depth cell the UI allows, then tabulates the logs. It imports
nothing from app/ and nothing that writes: each run is a fresh process of the
existing, already-fenced timing harness, so this file only launches and
parses. That is also why COLD is reproducible per run.

Configuration is forced for every child: EMBEDDING_DEVICE=cpu
TORCH_NUM_THREADS=2 HF_HUB_OFFLINE=1. A run whose device_report end line says
anything else, a non-zero exit, or a log without COLD and WARM blocks stops
the grid (grid/ABORTED.txt) -- a mislabelled or partial sample is worse than
none.

  run        pass 1: every cell once, order word -> depth -> width.
             pass 2: cells whose pass-1 WARM wall > 10 s get runs 2 and 3
             (pass 1 is run 1), each in a fresh process.
             --only WORD,W,D runs one cell to a named log (the warm-up).
             --resume skips logs that are already complete.
  tabulate   grid/*.log -> grid/tables.md + grid/cells.json.

USAGE (from backend/):
  python3 scripts/eval/timing_grid.py run --only light,3,3 --log NAME.log
  python3 scripts/eval/timing_grid.py run [--resume]
  python3 scripts/eval/timing_grid.py tabulate
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

GRID = Path("scripts/eval/publishB_baseline/grid")
WORDS = ["brave", "light", "storm"]
SIZES = range(4)
REPEAT_OVER_S = 10.0
SPREAD_FLAG = 0.30
EXPECTED_TREES = 21
CHILD_ENV = {"EMBEDDING_DEVICE": "cpu", "TORCH_NUM_THREADS": "2",
             "HF_HUB_OFFLINE": "1"}
EXPECTED_DEVICE = "device=cpu torch_num_threads=2"

# Target SQL shapes, as phase_timing prints them (first 120 chars).
VECTOR_PREFIX = ("SELECT sense_embeddings.sense_id, "
                 "sense_embeddings.embedding_model")
RELATIONS_PREFIX = "SELECT sense_relations.id AS sense_relations_id"
RESET_PREFIX = "RESET hnsw.iterative_scan"


def log_path(word: str, w: int, d: int, r: int) -> Path:
    return GRID / f"{word}_w{w}_d{d}_r{r}.log"


# --------------------------------------------------------------------- parse

_BLOCK = re.compile(r"^=== (COLD|WARM)\b.*===$", re.M)
_WALL = re.compile(r"^wall\s+([\d.]+)s\s+trees=(\d+) nodes=(\d+)", re.M)
_EMBED = re.compile(r"^embed\s+([\d.]+)s\s+n=\s*(\d+)", re.M)
_SQL = re.compile(r"^sql\s+([\d.]+)s\s+n=\s*(\d+)", re.M)
_REST = re.compile(r"^python \(rest\)\s+([-\d.]+)s", re.M)
_BY_TIME = re.compile(r"^\s+([\d.]+)s\s+n=\s*(\d+)\s+(.*)$")
_BY_COUNT = re.compile(r"^\s+n=\s*(\d+)\s+([\d.]+)s\s+(.*)$")


def _parse_block(text: str) -> dict:
    out: dict = {}
    m = _WALL.search(text)
    if m:
        out.update(wall=float(m[1]), trees=int(m[2]), nodes=int(m[3]))
    m = _EMBED.search(text)
    if m:
        out.update(embed=float(m[1]), embed_n=int(m[2]))
    m = _SQL.search(text)
    if m:
        out.update(sql=float(m[1]), sql_n=int(m[2]))
    m = _REST.search(text)
    if m:
        out["rest"] = float(m[1])
    # Shapes from BOTH tables: by-time (top 12) and by-count (top 6).
    shapes: dict[str, dict] = {}
    section = None
    for line in text.splitlines():
        if line.startswith("-- top 12 SQL"):
            section = "time"
            continue
        if line.startswith("-- top 6 SQL"):
            section = "count"
            continue
        if line.startswith("--") or line.startswith("["):
            section = None
            continue
        if section == "time" and (m := _BY_TIME.match(line)):
            shapes[m[3].strip()] = {"t": float(m[1]), "n": int(m[2])}
        elif section == "count" and (m := _BY_COUNT.match(line)):
            shapes.setdefault(m[3].strip(), {"t": float(m[2]), "n": int(m[1])})
    out["shapes"] = shapes
    return out


def parse_log(path: Path) -> dict | None:
    text = path.read_text(errors="replace")
    heads = list(_BLOCK.finditer(text))
    if len(heads) < 2:
        return None
    blocks = {}
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        blocks[h[1]] = _parse_block(text[h.end():end])
    end_line = next((ln for ln in text.splitlines()
                     if ln.startswith("[device_report] end")), "")
    return {"COLD": blocks.get("COLD"), "WARM": blocks.get("WARM"),
            "device_ok": EXPECTED_DEVICE in end_line, "device_line": end_line}


# ----------------------------------------------------------------------- run

def _run_one(word: str, w: int, d: int, path: Path, seq: int) -> dict:
    env = {**os.environ, **CHILD_ENV}
    cmd = [sys.executable, "scripts/eval/device_report.py",
           "scripts/eval/phase_timing.py",
           "--word", word, "--width", str(w), "--depth", str(d)]
    t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"== {seq:3d} {t0} {path.name}", flush=True)
    with open(path, "w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                            env=env).returncode
    t1 = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(GRID / "order.tsv", "a") as fh:
        fh.write(f"{seq}\t{t0}\t{t1}\t{word}\t{w}\t{d}\t{path.name}\t{rc}\n")
    parsed = parse_log(path)
    problem = None
    if rc != 0:
        problem = f"exit code {rc}"
    elif parsed is None or not parsed["COLD"] or not parsed["WARM"]:
        problem = "missing COLD/WARM block"
    elif not parsed["device_ok"]:
        problem = f"device mismatch: {parsed['device_line']!r}"
    if problem:
        (GRID / "ABORTED.txt").write_text(f"{path.name}: {problem}\n")
        print(f"!! ABORT {path.name}: {problem}", flush=True)
        sys.exit(2)
    print(f"   rc=0 WARM wall={parsed['WARM']['wall']:.2f}s "
          f"trees={parsed['WARM']['trees']} nodes={parsed['WARM']['nodes']}",
          flush=True)
    return parsed


def _complete(path: Path) -> dict | None:
    if not path.exists():
        return None
    p = parse_log(path)
    return p if p and p["COLD"] and p["WARM"] and p["device_ok"] else None


def run(only: str | None, log_name: str | None, resume: bool) -> None:
    GRID.mkdir(parents=True, exist_ok=True)
    order = GRID / "order.tsv"
    seq = sum(1 for _ in open(order)) if order.exists() else 0

    if only:
        word, w, d = only.split(",")
        path = GRID / (log_name or f"{word}_w{w}_d{d}_only.log")
        _run_one(word, int(w), int(d), path, seq + 1)
        return

    cells = [(word, w, d) for word in WORDS for d in SIZES for w in SIZES]
    first: dict[tuple, dict] = {}
    for word, w, d in cells:
        path = log_path(word, w, d, 1)
        done = _complete(path) if resume else None
        if done is None:
            seq += 1
            done = _run_one(word, w, d, path, seq)
        first[(word, w, d)] = done

    heavy = [c for c in cells if first[c]["WARM"]["wall"] > REPEAT_OVER_S]
    print(f"== pass 2: {len(heavy)} cells over {REPEAT_OVER_S:.0f}s WARM: "
          f"{[f'{a}_w{b}_d{c}' for a, b, c in heavy]}", flush=True)
    for r in (2, 3):
        for word, w, d in heavy:
            path = log_path(word, w, d, r)
            if resume and _complete(path):
                continue
            seq += 1
            _run_one(word, w, d, path, seq)
    print("== DONE", flush=True)


# ------------------------------------------------------------------ tabulate

def _find(shapes: dict, prefix: str) -> dict | None:
    hits = [v for k, v in shapes.items() if k.startswith(prefix)]
    return hits[0] if len(hits) == 1 else (
        {"t": sum(h["t"] for h in hits), "n": sum(h["n"] for h in hits),
         "merged": len(hits)} if hits else None)


def _med(xs: list[float]) -> float:
    return statistics.median(xs)


def tabulate() -> None:
    runs: dict[tuple, list[dict]] = {}
    for word in WORDS:
        for d in SIZES:
            for w in SIZES:
                for r in (1, 2, 3):
                    p = log_path(word, w, d, r)
                    if p.exists() and (parsed := parse_log(p)):
                        runs.setdefault((word, w, d), []).append(parsed)

    lines: list[str] = []
    out_json: dict[str, dict] = {}
    say = lines.append

    def med_of(cell, phase, key):
        vals = [r[phase][key] for r in runs[cell] if key in r[phase]]
        return _med(vals) if vals else float("nan")

    say("# B3 timing grid (Mac CPU, EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=2, "
        "21 languages)\n")
    say("Cells: `WARM / COLD` median wall seconds (n = runs). "
        "Rows depth, columns width.\n")
    for word in WORDS:
        say(f"## {word}\n")
        say("| depth \\ width | 0 | 1 | 2 | 3 |")
        say("|---|---|---|---|---|")
        for d in SIZES:
            row = []
            for w in SIZES:
                c = (word, w, d)
                if c not in runs:
                    row.append("—")
                    continue
                row.append(f"{med_of(c, 'WARM', 'wall'):.2f} / "
                           f"{med_of(c, 'COLD', 'wall'):.2f} "
                           f"(n={len(runs[c])})")
            say(f"| **{d}** | " + " | ".join(row) + " |")
        say("")

    say("## Trees and nodes per cell (every run, COLD and WARM)\n")
    say(f"⚑ = trees ≠ {EXPECTED_TREES}.\n")
    say("| cell | runs: COLD trees/nodes → WARM trees/nodes |")
    say("|---|---|")
    for cell in sorted(runs, key=lambda c: (WORDS.index(c[0]), c[2], c[1])):
        parts, flag = [], False
        for r in runs[cell]:
            ct, cn = r["COLD"]["trees"], r["COLD"]["nodes"]
            wt, wn = r["WARM"]["trees"], r["WARM"]["nodes"]
            flag |= ct != EXPECTED_TREES or wt != EXPECTED_TREES
            parts.append(f"{ct}/{cn} → {wt}/{wn}")
        mark = " ⚑" if flag else ""
        say(f"| {cell[0]} w{cell[1]} d{cell[2]}{mark} | " + "; ".join(parts)
            + " |")
    say("")

    say("## Width 0 with depth > 0 vs 0×0 (WARM trees/nodes, run 1)\n")
    for word in WORDS:
        base = runs.get((word, 0, 0))
        if not base:
            continue
        b = (base[0]["WARM"]["trees"], base[0]["WARM"]["nodes"])
        for d in (1, 2, 3):
            c = runs.get((word, 0, d))
            if not c:
                continue
            x = (c[0]["WARM"]["trees"], c[0]["WARM"]["nodes"])
            say(f"- {word} w0 d{d}: {x[0]}/{x[1]} vs 0×0 {b[0]}/{b[1]} → "
                f"{'IDENTICAL' if x == b else 'DIFFERENT'}")
    say("")

    for phase in ("WARM", "COLD"):
        say(f"## {phase} split for cells with median WARM wall > "
            f"{REPEAT_OVER_S:.0f} s (medians across runs)\n")
        say("| cell | n | wall | sql s (%) | embed s (%) | other s (%) | "
            "vector fetch n / mean ms | RESET n | relations N+1 n / total s | "
            "trees/nodes |")
        say("|---|---|---|---|---|---|---|---|---|---|")
        for cell in sorted(runs, key=lambda c: (WORDS.index(c[0]), c[2], c[1])):
            if med_of(cell, "WARM", "wall") <= REPEAT_OVER_S:
                continue
            rs = [r[phase] for r in runs[cell]]
            wall = _med([r["wall"] for r in rs])
            sql = _med([r["sql"] for r in rs])
            emb = _med([r["embed"] for r in rs])
            rest = _med([r["rest"] for r in rs])
            vec = [_find(r["shapes"], VECTOR_PREFIX) for r in rs]
            rel = [_find(r["shapes"], RELATIONS_PREFIX) for r in rs]
            rst = [_find(r["shapes"], RESET_PREFIX) for r in rs]
            if all(vec):
                vn = _med([v["n"] for v in vec])
                vt = _med([v["t"] for v in vec])
                merged = any(v.get("merged") for v in vec)
                vtxt = (f"{vn:.0f} / {vt / vn * 1000:.0f}"
                        + (" (merged shape)" if merged else ""))
            else:
                vtxt = "not in top tables"
            rtxt = (f"{_med([v['n'] for v in rel]):.0f} / "
                    f"{_med([v['t'] for v in rel]):.2f}"
                    if all(rel) else "not in top tables")
            stxt = (f"{_med([v['n'] for v in rst]):.0f}"
                    if all(rst) else "not in top tables")
            say(f"| {cell[0]} w{cell[1]} d{cell[2]} | {len(rs)} | {wall:.2f} | "
                f"{sql:.2f} ({sql / wall * 100:.1f}%) | "
                f"{emb:.2f} ({emb / wall * 100:.1f}%) | "
                f"{rest:.2f} ({rest / wall * 100:.1f}%) | {vtxt} | {stxt} | "
                f"{rtxt} | {rs[0]['trees']}/{rs[0]['nodes']} |")
        say("")

    say(f"## Spread (max − min) / median, repeated cells (⚠ > "
        f"{SPREAD_FLAG:.0%})\n")
    say("| cell | WARM walls | WARM spread | COLD walls | COLD spread |")
    say("|---|---|---|---|---|")
    for cell in sorted(runs, key=lambda c: (WORDS.index(c[0]), c[2], c[1])):
        if len(runs[cell]) < 2:
            continue
        row = [f"{cell[0]} w{cell[1]} d{cell[2]}"]
        for phase in ("WARM", "COLD"):
            ws = [r[phase]["wall"] for r in runs[cell]]
            sp = (max(ws) - min(ws)) / _med(ws)
            row += [" / ".join(f"{x:.2f}" for x in ws),
                    f"{sp:.1%}" + (" ⚠" if sp > SPREAD_FLAG else "")]
        say("| " + " | ".join(row) + " |")
    say("")

    for cell, rs in runs.items():
        out_json[f"{cell[0]}_w{cell[1]}_d{cell[2]}"] = [
            {ph: {k: v for k, v in r[ph].items() if k != "shapes"}
             | {"vector": _find(r[ph]["shapes"], VECTOR_PREFIX),
                "relations": _find(r[ph]["shapes"], RELATIONS_PREFIX),
                "reset": _find(r[ph]["shapes"], RESET_PREFIX)}
             for ph in ("COLD", "WARM")} | {"device": r["device_line"]}
            for r in rs]

    (GRID / "tables.md").write_text("\n".join(lines) + "\n")
    (GRID / "cells.json").write_text(json.dumps(out_json, indent=1))
    print("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--only")
    r.add_argument("--log")
    r.add_argument("--resume", action="store_true")
    sub.add_parser("tabulate")
    args = ap.parse_args()
    if args.cmd == "run":
        run(args.only, args.log, args.resume)
    else:
        tabulate()


if __name__ == "__main__":
    main()
