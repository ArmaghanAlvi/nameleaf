"""
Production smoke suite (PARTITION E1; roadmap 7a; D-2.2, D-3b). Runs on the
Mac against a base URL, after every deploy (deploy.sh runs it) and in E4.

    python3 deploy/smoke.py --base-url https://<site> [--alias nameleaf-prod]
                            [--site-url https://<site>] [--insecure]

HTTP checks (status code and a time budget each):
  * pages: /, /generate, /sources, /privacy, /terms -> 200;
  * /sitemap.xml and /robots.txt -> 200, containing no `localhost` (D-3b) and,
    with --site-url, containing that URL;
  * /api/ready -> 200;
  * one sense lookup for a fixed word, then one 0x0 search on that sense;
  * API docs absent: /api/docs, /api/redoc, /api/openapi.json and /docs -> 404.
With --alias, also on the server over SSH (D-2.2):
  * every nameleaf container logs with json-file, max-size 10m, max-file 3;
  * the backend's and Caddy's logs since this run started contain no access
    lines (uvicorn's `"GET /... HTTP/1.1"`, Caddy's http.log.access,
    remote_ip / client_ip) and not the smoke word.
--insecure skips TLS verification (E2's bare-IP stage only).

Prints one row per check; exits 0 if every check passed, 1 otherwise, 2 on a
usage error. Python stdlib only (3.9+), so any python3 on the Mac runs it.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

WORD = "brave"
PAGE_BUDGET_S = 10.0
API_BUDGET_S = 15.0
SEARCH_BUDGET_S = 60.0
PAGES = ("/", "/generate", "/sources", "/privacy", "/terms")
FINDABILITY = ("/sitemap.xml", "/robots.txt")
DOCS_ABSENT = ("/api/docs", "/api/redoc", "/api/openapi.json", "/docs")
LOG_SERVICES = ("backend", "caddy")
ACCESS_PATTERNS = (
    re.compile(r'"(?:GET|POST|HEAD|PUT|DELETE|OPTIONS) /[^"]* HTTP/\d'),
    re.compile(r"http\.log\.access"),
    re.compile(r'"(?:remote_ip|client_ip)"'),
)
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
BASE_RE = re.compile(r"^https?://[^/\s]+/?$")


@dataclass
class Result:
    name: str
    ok: bool
    status: str = ""
    seconds: float | None = None
    detail: str = ""


class Http:
    def __init__(self, base: str, insecure: bool) -> None:
        self.base = base.rstrip("/")
        self.ctx = ssl._create_unverified_context() if insecure else None

    def request(self, path: str, body: dict | None = None, timeout: float = 120.0):
        """-> (status, seconds, text). A non-2xx status is returned, not raised."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if body else "GET",
                                     headers={"Content-Type": "application/json"} if body else {})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                text = r.read().decode("utf-8", "replace")
                return r.status, time.perf_counter() - t0, text
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            return e.code, time.perf_counter() - t0, text


def timed_check(results: list[Result], name: str, http: Http, path: str, want: int,
                budget: float, body: dict | None = None, extra=None) -> str | None:
    """Run one HTTP check; return the body on success (for follow-up checks)."""
    try:
        status, secs, text = http.request(path, body)
    except Exception as exc:  # connection refused, TLS, timeout
        results.append(Result(name, False, "-", None, f"{type(exc).__name__}: {exc}"))
        return None
    ok = status == want and secs <= budget
    detail = "" if status == want else f"expected {want}"
    if status == want and secs > budget:
        detail = f"slower than {budget:.0f}s"
    if ok and extra is not None:
        problem = extra(text)
        if problem:
            ok, detail = False, problem
    results.append(Result(name, ok, str(status), secs, detail))
    return text if ok else None


def findability_check(site_url: str | None):
    def check(text: str) -> str | None:
        if "localhost" in text:
            return "contains localhost (frontend built for the wrong SITE_URL, D-3b)"
        if site_url and site_url.rstrip("/") not in text:
            return f"does not contain {site_url}"
        return None
    return check


def http_checks(http: Http, site_url: str | None) -> list[Result]:
    results: list[Result] = []
    for path in PAGES:
        timed_check(results, f"page {path}", http, path, 200, PAGE_BUDGET_S)
    for path in FINDABILITY:
        timed_check(results, path, http, path, 200, PAGE_BUDGET_S, extra=findability_check(site_url))
    timed_check(results, "/api/ready", http, "/api/ready", 200, API_BUDGET_S)

    query = urllib.parse.urlencode({"query": WORD, "languageCode": "en", "limit": "1"})
    text = timed_check(results, f"sense lookup '{WORD}'", http, f"/api/senses/lookup?{query}",
                       200, API_BUDGET_S, extra=lambda t: None if json.loads(t).get("options") else "no options")
    if text is None:
        results.append(Result("search 0x0", False, "-", None, "skipped: no sense from the lookup"))
    else:
        sid = json.loads(text)["options"][0]["senseId"]
        body = {"selectedSenseIds": [sid], "queryText": "", "expansionCount": 0,
                "width": 0, "depth": 0, "languageCodes": ["en"], "minLength": 0, "maxLength": 30}
        timed_check(results, f"search 0x0 (sense {sid})", http, "/api/explore-v2", 200,
                    SEARCH_BUDGET_S, body=body,
                    extra=lambda t: None if isinstance(json.loads(t), dict) else "not a JSON object")
    for path in DOCS_ABSENT:
        timed_check(results, f"absent {path}", http, path, 404, PAGE_BUDGET_S)
    return results


def ssh(alias: str, command: str) -> str:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", alias, command],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ssh exit {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def server_checks(alias: str, since_seconds: int) -> list[Result]:
    results: list[Result] = []
    try:
        listing = ssh(alias, "docker ps -a --filter label=com.docker.compose.project=nameleaf "
                             "--format '{{.Names}}\t{{.Label \"com.docker.compose.service\"}}'")
    except Exception as exc:
        return [Result("server: containers", False, "-", None, str(exc))]
    containers = [ln.split("\t") for ln in listing.splitlines() if "\t" in ln]
    if not containers:
        return [Result("server: containers", False, "-", None, "no nameleaf containers")]
    for name, _service in containers:
        try:
            cfg = json.loads(ssh(alias, f"docker inspect --format '{{{{json .HostConfig.LogConfig}}}}' {name}"))
        except Exception as exc:
            results.append(Result(f"server: {name} logging", False, "-", None, str(exc)))
            continue
        opts = cfg.get("Config") or {}
        ok = cfg.get("Type") == "json-file" and opts.get("max-size") == "10m" and str(opts.get("max-file")) == "3"
        results.append(Result(f"server: {name} logging", ok, "", None,
                              "" if ok else f"got {json.dumps(cfg)}, want json-file 10m x 3"))
    for name, service in containers:
        if service not in LOG_SERVICES:
            continue
        try:
            logs = ssh(alias, f"docker logs --since {since_seconds}s {name} 2>&1")
        except Exception as exc:
            results.append(Result(f"server: {name} no access log", False, "-", None, str(exc)))
            continue
        hits = [p.pattern for p in ACCESS_PATTERNS if p.search(logs)]
        if re.search(rf"\b{WORD}\b", logs):
            hits.append(f"the smoke word '{WORD}'")
        results.append(Result(f"server: {name} no access log", not hits, "", None,
                              f"found {', '.join(hits)} in the last {since_seconds}s" if hits else ""))
    return results


def print_table(results: list[Result]) -> None:
    width = max(len(r.name) for r in results)
    print(f"{'check':<{width}}  result  status  seconds  detail")
    for r in results:
        secs = f"{r.seconds:7.2f}" if r.seconds is not None else "      -"
        print(f"{r.name:<{width}}  {'PASS' if r.ok else 'FAIL':<6}  {r.status:<6}  {secs}  {r.detail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NameLeaf production smoke suite (7a)")
    ap.add_argument("--base-url", required=True, help="e.g. https://<site>; no path")
    ap.add_argument("--alias", help="SSH alias for the server-side log checks")
    ap.add_argument("--site-url", help="the SITE_URL the sitemap and robots must contain")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (bare IP only)")
    args = ap.parse_args(argv)
    if not BASE_RE.match(args.base_url):
        ap.error("--base-url must be http(s)://<host> with no path")
    if args.alias is not None and not ALIAS_RE.match(args.alias):
        ap.error("--alias must be a plain ~/.ssh/config alias (no hostnames, IPs or user@)")

    started = time.time()
    results = http_checks(Http(args.base_url, args.insecure), args.site_url)
    if args.alias:
        results += server_checks(args.alias, int(time.time() - started) + 60)
    print_table(results)
    failed = [r for r in results if not r.ok]
    print(f"\nSMOKE {'PASS' if not failed else 'FAIL'}: {len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
