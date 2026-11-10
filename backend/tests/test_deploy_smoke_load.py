"""
E1: deploy/smoke.py (7a) and deploy/load_probe.py (7b) against a fake site.

A ThreadingHTTPServer on 127.0.0.1 plays the production stack: pages, the
sitemap and robots, /api/ready, the sense lookup, /api/explore-v2 and the
rate limit. The server-side checks (log config, access lines, memory and
swap) go through a fake `ssh` on PATH that answers from a JSON file; nothing
reaches a real server.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ALIAS = "example-alias"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "deploy" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look the module up while it loads
    spec.loader.exec_module(mod)
    return mod


smoke = _load("smoke")
load_probe = _load("load_probe")


class Site:
    """Behaviour knobs for the fake site, changed per test."""

    def __init__(self):
        self.status: dict[str, int] = {}
        self.sitemap = "<urlset><url><loc>https://example.invalid/</loc></url></urlset>"
        self.robots = "Sitemap: https://example.invalid/sitemap.xml"
        self.docs_status = 404
        self.search: list[tuple[int, dict]] = []   # cycled per search request
        self.searches = 0
        self.lookup_limit = 5
        self.lookups = 0
        self.lock = threading.Lock()


def handler_for(site: Site):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code: int, body, ctype="text/html", headers=None):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in site.status:
                return self.send(site.status[path], "x")
            if path in ("/", "/generate", "/sources", "/privacy", "/terms"):
                return self.send(200, "<html>ok</html>")
            if path == "/sitemap.xml":
                return self.send(200, site.sitemap, "application/xml")
            if path == "/robots.txt":
                return self.send(200, site.robots, "text/plain")
            if path == "/api/ready":
                return self.send(200, {"status": "ready"}, "application/json")
            if path in ("/api/docs", "/api/redoc", "/api/openapi.json", "/docs"):
                return self.send(site.docs_status, "nope")
            if path == "/api/languages":
                return self.send(200, [{"code": "en"}, {"code": "ru"}], "application/json")
            if path == "/api/senses/lookup":
                with site.lock:
                    site.lookups += 1
                    over = site.lookups > site.lookup_limit
                if over:
                    return self.send(429, {"detail": "slow down"}, "application/json", {"Retry-After": "42"})
                return self.send(200, {"query": "brave", "options": [{"senseId": 7}]}, "application/json")
            return self.send(404, "missing")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path != "/api/explore-v2":
                return self.send(404, "missing")
            with site.lock:
                i = site.searches
                site.searches += 1
            code, body = site.search[i % len(site.search)] if site.search else (200, {"results": []})
            return self.send(code, body, "application/json")

    return Handler


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup
        # that can take tens of seconds; the name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@pytest.fixture
def site():
    s = Site()
    server = LocalServer(("127.0.0.1", 0), handler_for(s))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    s.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield s
    server.shutdown()


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """A fake ssh first on PATH; answers come from answers.json (regex -> stdout)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answers = tmp_path / "answers.json"
    log = tmp_path / "ssh.log"
    script = bin_dir / "ssh"
    script.write_text(f"""#!{sys.executable}
import json, re, sys
args = sys.argv[1:]
while args and args[0] == "-o":
    args = args[2:]
alias, cmd = args[0], " ".join(args[1:])
open({str(log)!r}, "a").write(alias + " " + cmd + "\\n")
if alias != {ALIAS!r}:
    sys.exit(255)
for pattern, out in json.load(open({str(answers)!r})):
    if re.search(pattern, cmd):
        sys.stdout.write(out)
        sys.exit(0)
sys.exit(0)
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    class Fake:
        def set(self, pairs):
            answers.write_text(json.dumps(pairs))

        def calls(self):
            return log.read_text().splitlines() if log.exists() else []

    f = Fake()
    f.set([])
    return f


# --- smoke --------------------------------------------------------------------

def test_smoke_passes_on_a_healthy_site(site, capsys):
    assert smoke.main(["--base-url", site.base, "--site-url", "https://example.invalid"]) == 0
    out = capsys.readouterr().out
    assert "SMOKE PASS" in out
    for name in ("page /privacy", "/sitemap.xml", "/api/ready", "sense lookup 'brave'",
                 "search 0x0 (sense 7)", "absent /api/docs", "absent /docs"):
        assert name in out


@pytest.mark.parametrize("change,failing", [
    (lambda s: s.status.update({"/terms": 500}), "page /terms"),
    (lambda s: s.status.update({"/api/ready": 503}), "/api/ready"),
    (lambda s: setattr(s, "docs_status", 200), "absent /api/docs"),
    (lambda s: setattr(s, "sitemap", "<loc>https://localhost/</loc>"), "/sitemap.xml"),
    (lambda s: setattr(s, "search", [(503, {"detail": {"code": "search_queue_full"}})]), "search 0x0"),
    (lambda s: setattr(s, "lookup_limit", 0), "sense lookup"),
])
def test_smoke_fails_on_each_problem(site, capsys, change, failing):
    change(site)
    assert smoke.main(["--base-url", site.base]) == 1
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if ln.startswith(failing))
    assert " FAIL " in row, row
    assert "SMOKE FAIL" in out


def test_smoke_requires_the_site_url_when_given(site, capsys):
    assert smoke.main(["--base-url", site.base, "--site-url", "https://other.invalid"]) == 1


@pytest.mark.parametrize("argv", [["--base-url", "example.invalid"],
                                  ["--base-url", "https://example.invalid/path"],
                                  ["--base-url", "https://example.invalid", "--alias", "user@host.invalid"]])
def test_smoke_usage_errors(argv):
    with pytest.raises(SystemExit) as e:
        smoke.main(argv)
    assert e.value.code == 2


GOOD_LOGCFG = '{"Type":"json-file","Config":{"max-file":"3","max-size":"10m"}}'
CONTAINERS = ("nameleaf-db-1\tdb\nnameleaf-backend-1\tbackend\n"
              "nameleaf-frontend-1\tfrontend\nnameleaf-caddy-1\tcaddy\n")


def test_smoke_server_checks_pass_with_rotated_logs_and_no_access_lines(site, fake_ssh, capsys):
    fake_ssh.set([["docker ps", CONTAINERS], ["LogConfig", GOOD_LOGCFG],
                  ["docker logs", "INFO: Application startup complete.\n"]])
    assert smoke.main(["--base-url", site.base, "--alias", ALIAS]) == 0, capsys.readouterr().out
    calls = fake_ssh.calls()
    assert sum("LogConfig" in c for c in calls) == 4
    assert {c.split()[-2] for c in calls if "docker logs" in c} == {"nameleaf-backend-1", "nameleaf-caddy-1"}


@pytest.mark.parametrize("answers,failing", [
    ([["LogConfig", '{"Type":"json-file","Config":{}}']], "logging"),
    ([["LogConfig", GOOD_LOGCFG], ["docker logs", 'INFO: client:5 - "GET /ready HTTP/1.1" 200 OK\n']],
     "no access log"),
    ([["LogConfig", GOOD_LOGCFG], ["docker logs", '{"logger":"http.log.access","remote_ip":"x"}\n']],
     "no access log"),
    ([["LogConfig", GOOD_LOGCFG], ["docker logs", "lookup for brave took 3ms\n"]], "no access log"),
])
def test_smoke_server_checks_fail_on_log_problems(site, fake_ssh, capsys, answers, failing):
    fake_ssh.set([["docker ps", CONTAINERS], *answers])
    assert smoke.main(["--base-url", site.base, "--alias", ALIAS]) == 1
    out = capsys.readouterr().out
    assert any(failing in ln and " FAIL " in ln for ln in out.splitlines()), out


# --- load probe -----------------------------------------------------------------

SAMPLE_OK = ("nameleaf-db-1\t4.8GiB / 8GiB\nnameleaf-backend-1\t912MiB / 3GiB\n@@free\n"
             "               total        used        free\nMem:           11700        6100        5600\n"
             "Swap:              0           0           0\n@@swapon\n")
SAMPLE_SWAP = SAMPLE_OK.replace("Swap:              0           0           0",
                                "Swap:           2047          12        2035") + "/swapfile file 2G 12M -2\n"


def run_load(site, *extra):
    return load_probe.main(["--base-url", site.base, "--alias", ALIAS, "--sample-seconds", "0.05",
                            "--rate-probe-delay", "0", *extra])


def test_load_reports_admission_503s_as_expected_and_finds_the_429(site, fake_ssh, capsys):
    fake_ssh.set([["docker stats", SAMPLE_OK]])
    site.search = [(200, {})] * 6 + [(503, {"detail": {"code": "search_queue_full", "message": "busy",
                                                       "retryAfter": 5}})] * 4
    site.lookup_limit = 7
    assert run_load(site) == 0
    out = capsys.readouterr().out
    assert "200 -: 6" in out and "503 search_queue_full: 4" in out
    assert '"first_429_at": 7' in out and '"retry_after": "42"' in out
    assert "peak nameleaf-db-1: 4.80 GiB" in out
    assert "peak host memory used: 6100 MiB" in out
    assert "swap used: no" in out
    assert "WARNING: the rate-limit probe" in out
    assert site.searches == 10


def test_load_flags_swap(site, fake_ssh, capsys):
    fake_ssh.set([["docker stats", SAMPLE_SWAP]])
    assert run_load(site, "--skip-rate-probe") == 1
    out = capsys.readouterr().out
    assert "swap used: YES" in out and "swap was used" in out


def test_load_flags_unexpected_5xx(site, fake_ssh, capsys):
    fake_ssh.set([["docker stats", SAMPLE_OK]])
    site.search = [(500, {"detail": "boom"})]
    assert run_load(site, "--skip-rate-probe") == 1
    assert "unexpected 5xx" in capsys.readouterr().out


def test_load_flags_a_missing_rate_limit(site, fake_ssh, capsys):
    fake_ssh.set([["docker stats", SAMPLE_OK]])
    site.lookup_limit = 10 ** 6
    assert run_load(site, "--rate-probe-max", "5") == 1
    assert "no 429 in 5 requests" in capsys.readouterr().out


def test_load_requires_alias(site):
    with pytest.raises(SystemExit) as e:
        load_probe.main(["--base-url", site.base])
    assert e.value.code == 2


def test_parse_sample_units():
    s = load_probe.parse_sample(SAMPLE_SWAP)
    assert s["containers"]["nameleaf-backend-1"] == 912 * 1024 ** 2
    assert s["swap_used_mb"] == 12 and s["swap_devices"] == ["/swapfile file 2G 12M -2"]
