"""
No access logs, rotated container logs (D-2.2 L1, D3a).

The privacy page says the server doesn't log IP addresses or searched words.
These tests fail if the production configuration stops matching that:
uvicorn's access log back on, a container without size-capped rotated logs,
or Caddy logging requests (an access `log` directive, or its default
http.log.error logger, which records the request on every 5xx).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "backend" / "Dockerfile.prod"
COMPOSE = REPO / "docker-compose.prod.yml"
CADDYFILE = REPO / "deploy" / "Caddyfile"

SERVICES = {"db", "backend", "frontend", "caddy"}


def _cmd() -> list[str]:
    text = DOCKERFILE.read_text()
    m = re.search(r"^CMD (\[.*?\])\s*$", text.replace("\\\n", " "), re.M | re.S)
    assert m, "no exec-form CMD in Dockerfile.prod"
    return json.loads(m.group(1))


def test_uvicorn_access_log_is_off():
    cmd = _cmd()
    assert cmd[0] == "uvicorn"
    assert "--no-access-log" in cmd
    assert "--access-log" not in cmd


def test_uvicorn_still_trusts_proxy_headers_for_rate_limiting():
    cmd = _cmd()
    assert "--proxy-headers" in cmd
    assert cmd[cmd.index("--forwarded-allow-ips") + 1] == "*"


def test_every_service_has_rotated_json_file_logs():
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]
    assert set(services) == SERVICES, "a service was added or removed: give it logging"
    for name, svc in services.items():
        logging = svc.get("logging")
        assert logging, f"{name}: no logging block"
        assert logging.get("driver") == "json-file", name
        opts = logging.get("options", {})
        assert opts.get("max-size") == "10m", name
        assert str(opts.get("max-file")) == "3", name


def _strip_comments(text: str) -> str:
    return "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())


def test_caddy_excludes_its_error_logger_globally():
    text = _strip_comments(CADDYFILE.read_text())
    m = re.search(r"\{\s*log\s+default\s*\{([^}]*)\}\s*\}", text)
    assert m, "no global `log default { ... }` block"
    assert re.search(r"\bexclude\b[^\n]*\bhttp\.log\.error\b", m.group(1))


def test_caddy_site_block_has_no_log_directive():
    text = _strip_comments(CADDYFILE.read_text())
    site = text[text.index("{$SITE_ADDRESS}"):]
    assert not re.search(r"^\s*log\b", site, re.M)
