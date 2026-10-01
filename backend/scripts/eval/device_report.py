"""
Run an eval script and report the embedding device and torch thread count
AS THE RUNNING PROCESS SEES THEM (publishing B1).

WHY: a CPU-vs-MPS comparison is only as good as its labels. Setting
EMBEDDING_DEVICE is an intention; model.device after load is the fact. This
wrapper prints the fact into the same log as the capture, including for
captures taken before get_model() learned to log it.

It never loads the model itself: if the wrapped script didn't load it, the
report says so rather than paying for (and mislabelling) a load.

USAGE (from backend/):
  python3 scripts/eval/device_report.py scripts/eval/phase_timing.py --word light
"""
from __future__ import annotations

import os
import runpy
import sys

sys.path.insert(0, os.getcwd())

import torch                                                          # noqa: E402

import app.services.embedding_provider as ep                          # noqa: E402


def _report(when: str) -> None:
    loaded = ep.get_model.cache_info().currsize > 0
    device = str(ep.get_model().device) if loaded else "model not loaded"
    print(f"[device_report] {when}: device={device} "
          f"torch_num_threads={torch.get_num_threads()} "
          f"env EMBEDDING_DEVICE={os.environ.get('EMBEDDING_DEVICE')!r} "
          f"TORCH_NUM_THREADS={os.environ.get('TORCH_NUM_THREADS')!r}",
          flush=True)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: device_report.py <script.py> [args...]")
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    _report("start")
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        _report("end")


if __name__ == "__main__":
    main()
