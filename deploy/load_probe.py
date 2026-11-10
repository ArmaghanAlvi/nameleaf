"""
Load and abuse probe (PARTITION E1; roadmap 7b). Runs on the Mac against the
server, in E4. Report first; E4 makes the call.

    python3 deploy/load_probe.py --base-url https://<site> --alias nameleaf-prod
        [--concurrency 10] [--width 1] [--depth 1] [--languages all|en]
        [--skip-rate-probe] [--insecure]

1. Looks up one sense for a fixed word, and the language list.
2. Fires --concurrency searches at once (default 10, each width x depth =
   1x1, all languages: a normal search, B-4.1). By design (B-4.3) the server
   runs 2 at a time and queues 4, so with 10 at once about 4 get 503
   `search_queue_full` immediately: expected, and counted separately.
3. Meanwhile samples the server every few seconds over SSH:
   `docker stats --no-stream`, `free -m`, `swapon --show`.
4. Rate-limit probe: sense lookups (the 120/min endpoint, not searches) until
   the first 429, at most --rate-probe-max requests. WARNING: this
   rate-limits YOUR OWN IP on /api/senses/lookup for the window (60 s).
5. Reports per-request times, status and error-code counts, peak memory per
   container and for the host, and whether swap was used.

Exit codes: 0 report complete, no swap, no unexpected 5xx; 1 swap was used,
an unexpected 5xx (anything but 503 with an admission code), a request
error, or no 429 within the probe; 2 usage or setup failure (lookup failed).
Python stdlib only (3.9+). This is not scripts/eval/http_concurrency.py: that
one fires 160 searches at fixed levels (it would trip the 20/min search
limit) and samples nothing on the server.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

WORD = "brave"
ADMISSION_CODES = {"search_queue_full", "search_queue_timeout", "large_search_busy"}
SAMPLE_COMMAND = ("docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'; "
                  "echo '@@free'; free -m; echo '@@swapon'; swapon --show --noheadings")
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
BASE_RE = re.compile(r"^https?://[^/\s]+/?$")
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
         "kB": 1000, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}


class Http:
    def __init__(self, base: str, insecure: bool) -> None:
        self.base = base.rstrip("/")
        self.ctx = ssl._create_unverified_context() if insecure else None

    def request(self, path: str, body: dict | None = None, timeout: float = 400.0):
        """-> (status, seconds, text, headers)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if body else "GET",
                                     headers={"Content-Type": "application/json"} if body else {})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                return r.status, time.perf_counter() - t0, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, time.perf_counter() - t0, e.read().decode("utf-8", "replace"), e.headers


def error_code(text: str) -> str:
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        return ""
    return detail.get("code", "") if isinstance(detail, dict) else ""


def to_bytes(value: str) -> float:
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)\s*$", value)
    return float(m.group(1)) * UNITS.get(m.group(2), 1) if m else 0.0


def parse_sample(text: str) -> dict:
    """docker stats + free -m + swapon output -> {"containers": {name: bytes},
    "host_used_mb": int, "swap_used_mb": int, "swap_devices": [..]}."""
    section, out = "stats", {"containers": {}, "host_used_mb": 0, "swap_used_mb": 0, "swap_devices": []}
    for line in text.splitlines():
        if line.strip() == "@@free":
            section = "free"; continue
        if line.strip() == "@@swapon":
            section = "swapon"; continue
        if section == "stats" and "\t" in line:
            name, usage = line.split("\t", 1)
            out["containers"][name] = to_bytes(usage.split("/")[0])
        elif section == "free":
            parts = line.split()
            if parts and parts[0] == "Mem:" and len(parts) > 2:
                out["host_used_mb"] = int(parts[2])
            elif parts and parts[0] == "Swap:" and len(parts) > 2:
                out["swap_used_mb"] = int(parts[2])
        elif section == "swapon" and line.strip():
            out["swap_devices"].append(line.strip())
    return out


class Sampler(threading.Thread):
    def __init__(self, alias: str, every: float) -> None:
        super().__init__(daemon=True)
        self.alias, self.every = alias, every
        self.samples: list[dict] = []
        self.errors: list[str] = []
        self._halt = threading.Event()  # not _stop: Thread uses that name

    def sample(self) -> None:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", self.alias, SAMPLE_COMMAND],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            self.samples.append(parse_sample(r.stdout))
        else:
            self.errors.append(f"ssh exit {r.returncode}: {r.stderr.strip()[:200]}")

    def run(self) -> None:
        while not self._halt.is_set():
            self.sample()
            self._halt.wait(self.every)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=120)
        self.sample()  # one after the load


def run_searches(http: Http, sid: int, codes: list[str], n: int, width: int, depth: int) -> list[tuple]:
    body = {"selectedSenseIds": [sid], "queryText": "", "expansionCount": width,
            "width": width, "depth": depth, "languageCodes": codes, "minLength": 0, "maxLength": 30}

    def one(i: int) -> tuple:
        try:
            status, secs, text, _ = http.request("/api/explore-v2", body)
            return i, status, secs, error_code(text)
        except Exception as exc:
            return i, "error", None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=n) as pool:
        return sorted(pool.map(one, range(n)))


def rate_probe(http: Http, max_requests: int) -> dict:
    query = urllib.parse.urlencode({"query": WORD, "languageCode": "en", "limit": "1"})
    for i in range(1, max_requests + 1):
        status, _secs, _text, headers = http.request(f"/api/senses/lookup?{query}", timeout=60)
        if status == 429:
            return {"first_429_at": i, "retry_after": headers.get("Retry-After", "")}
        if status != 200:
            return {"first_429_at": None, "stopped": f"status {status} at request {i}"}
    return {"first_429_at": None, "stopped": f"no 429 in {max_requests} requests"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NameLeaf load and abuse probe (7b)")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--alias", required=True, help="SSH alias, for memory and swap sampling")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--width", type=int, default=1)
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--languages", choices=("all", "en"), default="all")
    ap.add_argument("--sample-seconds", type=float, default=5.0)
    ap.add_argument("--skip-rate-probe", action="store_true")
    ap.add_argument("--rate-probe-max", type=int, default=200)
    ap.add_argument("--rate-probe-delay", type=float, default=5.0, help=argparse.SUPPRESS)
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args(argv)
    if not BASE_RE.match(args.base_url):
        ap.error("--base-url must be http(s)://<host> with no path")
    if not ALIAS_RE.match(args.alias):
        ap.error("--alias must be a plain ~/.ssh/config alias (no hostnames, IPs or user@)")

    http = Http(args.base_url, args.insecure)
    query = urllib.parse.urlencode({"query": WORD, "languageCode": "en", "limit": "1"})
    status, _, text, _ = http.request(f"/api/senses/lookup?{query}", timeout=60)
    if status != 200 or not json.loads(text).get("options"):
        print(f"setup failed: sense lookup returned {status}", file=sys.stderr)
        return 2
    sid = json.loads(text)["options"][0]["senseId"]
    if args.languages == "en":
        codes = ["en"]
    else:
        status, _, text, _ = http.request("/api/languages", timeout=60)
        if status != 200:
            print(f"setup failed: /api/languages returned {status}", file=sys.stderr)
            return 2
        codes = [lang["code"] for lang in json.loads(text)]

    print(f"load: {args.concurrency} concurrent searches, {args.width}x{args.depth}, "
          f"{len(codes)} languages, sense {sid} ('{WORD}')")
    sampler = Sampler(args.alias, args.sample_seconds)
    sampler.start()
    t0 = time.perf_counter()
    rows = run_searches(http, sid, codes, args.concurrency, args.width, args.depth)
    wall = time.perf_counter() - t0
    sampler.stop()

    print("\nper request: index status seconds code")
    for i, st, secs, code in rows:
        shown = "-" if secs is None else f"{secs:.2f}"
        print(f"  {i:>3} {st!s:>6} {shown:>8} {code}")
    counts = Counter((st, code) for _, st, _, code in rows)
    print(f"\nwall {wall:.1f}s; status counts:")
    for (st, code), n in sorted(counts.items(), key=lambda kv: str(kv[0])):
        print(f"  {st} {code or '-'}: {n}")

    rate = None
    if not args.skip_rate_probe:
        print(f"\nWARNING: the rate-limit probe now sends up to {args.rate_probe_max} sense lookups and "
              "stops at the first 429. Your own IP stays rate-limited on lookups for the window "
              "(about 60 s): the site's search box may refuse lookups from this network meanwhile.")
        time.sleep(args.rate_probe_delay)
        rate = rate_probe(http, args.rate_probe_max)
        print(f"rate probe: {json.dumps(rate)}")

    peaks: dict[str, float] = {}
    host_peak = swap_peak = 0
    swap_devices: set[str] = set()
    for s in sampler.samples:
        for name, b in s["containers"].items():
            peaks[name] = max(peaks.get(name, 0.0), b)
        host_peak = max(host_peak, s["host_used_mb"])
        swap_peak = max(swap_peak, s["swap_used_mb"])
        swap_devices.update(s["swap_devices"])
    print(f"\nserver samples: {len(sampler.samples)} ok, {len(sampler.errors)} failed")
    for e in sampler.errors[:5]:
        print(f"  sample error: {e}")
    for name in sorted(peaks):
        print(f"  peak {name}: {peaks[name] / 1024 ** 3:.2f} GiB")
    print(f"  peak host memory used: {host_peak} MiB")
    swap_used = swap_peak > 0 or bool(swap_devices)
    print(f"  swap used: {'YES' if swap_used else 'no'} (peak {swap_peak} MiB; devices: {sorted(swap_devices) or 'none'})")

    unexpected = [r for r in rows if r[1] == "error" or
                  (isinstance(r[1], int) and r[1] >= 500 and not (r[1] == 503 and r[3] in ADMISSION_CODES))]
    problems = []
    if swap_used:
        problems.append("swap was used")
    if unexpected:
        problems.append(f"{len(unexpected)} unexpected 5xx or errors")
    if not sampler.samples:
        problems.append("no server samples")
    if rate is not None and rate.get("first_429_at") is None:
        problems.append(f"rate probe: {rate.get('stopped')}")
    print(f"\nLOAD {'OK' if not problems else 'ATTENTION: ' + '; '.join(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
