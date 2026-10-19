"""
Production build configuration (D3b).

The frontend renders its pages at build time, and every absolute URL on them
(canonical, og:*, sitemap, robots, JSON-LD) comes from SITE_URL. These tests
fail if the production build can run without it: the compose file must pass
SITE_URL as a required build arg, and the frontend Dockerfile must declare it
and refuse to build when it is empty, before `npm run build`.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.prod.yml"
FRONTEND_DOCKERFILE = REPO / "frontend" / "Dockerfile.prod"


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
