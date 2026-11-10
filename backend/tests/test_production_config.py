"""
Production build configuration (D3b).

The frontend renders its pages at build time, and every absolute URL on them
(canonical, og:*, sitemap, robots, JSON-LD) comes from SITE_URL. These tests
fail if the production build can run without it: the compose file must pass
SITE_URL as a required build arg, and the frontend Dockerfile must declare it
and refuse to build when it is empty, before `npm run build`.

E1 adds the deploy keys: the backend's init and drain settings (E-0.7 6c),
`pull_policy: never` on the local images (D-4.6), and the server override
that removes `build:` so the server can never build (E-0.7 6a).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.prod.yml"
SERVER_OVERRIDE = REPO / "deploy" / "compose.server.yml"
FRONTEND_DOCKERFILE = REPO / "frontend" / "Dockerfile.prod"
BACKEND_DOCKERFILE = REPO / "backend" / "Dockerfile.prod"


def test_compose_frontend_requires_site_url_build_arg():
    compose = yaml.safe_load(COMPOSE.read_text())
    args = compose["services"]["frontend"]["build"].get("args") or {}
    assert args.get("SITE_URL") == "${SITE_URL:?set SITE_URL in .env.production}"


def _build_stage() -> list[str]:
    lines = [ln.strip() for ln in FRONTEND_DOCKERFILE.read_text().splitlines()]
    start = next(i for i, ln in enumerate(lines) if ln.startswith("FROM") and " AS build" in ln)
    end = next((i for i, ln in enumerate(lines[start + 1:], start + 1) if ln.startswith("FROM")),
               len(lines))
    return lines[start:end]


def test_frontend_dockerfile_requires_site_url_before_build():
    stage = _build_stage()
    build = stage.index("RUN npm run build")
    arg = stage.index("ARG SITE_URL")
    guard = next(i for i, ln in enumerate(stage)
                 if ln.startswith("RUN test -n \"$SITE_URL\" ||") and "exit 1" in ln)
    env = stage.index("ENV SITE_URL=$SITE_URL")
    assert arg < guard < env < build


# --- E1: drain, init, pull policy, server override -----------------------------

def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _duration_seconds(value: str) -> int:
    m = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", str(value))
    assert m and value, f"unexpected duration {value!r}"
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _backend_cmd() -> list[str]:
    text = BACKEND_DOCKERFILE.read_text().replace("\\\n", " ")
    m = re.search(r"^CMD (\[.*?\])\s*$", text, re.M | re.S)
    assert m, "no exec-form CMD in backend/Dockerfile.prod"
    return json.loads(m.group(1))


def test_backend_runs_under_an_init():
    assert _services()["backend"].get("init") is True


def test_backend_drains_in_flight_searches_before_sigkill():
    grace = _duration_seconds(_services()["backend"]["stop_grace_period"])
    cmd = _backend_cmd()
    uvicorn_wait = int(cmd[cmd.index("--timeout-graceful-shutdown") + 1])
    # A search runs up to SEARCH_TIMEOUT_SECONDS=300 after up to 30 s queued.
    assert grace == 330
    assert 300 < uvicorn_wait < grace
    assert "--no-access-log" in cmd


def test_local_images_never_come_from_a_registry():
    services = _services()
    for name in ("backend", "frontend"):
        assert services[name].get("pull_policy") == "never", name
        assert services[name]["image"].startswith(f"nameleaf-{name}:${{IMAGE_TAG"), name


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that records Compose's `!reset` tag instead of failing."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: "!reset")


def test_server_override_removes_build_and_nothing_else():
    override = yaml.load(SERVER_OVERRIDE.read_text(), Loader=_ComposeLoader)
    assert override == {"services": {"backend": {"build": "!reset"},
                                     "frontend": {"build": "!reset"}}}
