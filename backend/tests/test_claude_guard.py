"""
Stage 0b: the Claude Code guard hook must block what CLAUDE.md forbids and
allow ordinary work. Runs in CI, so a regression in the guard fails a build
instead of silently weakening the backstop.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "guard.py"
_spec = importlib.util.spec_from_file_location("claude_guard", GUARD)
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

BLOCKED = [
    "docker compose down -v",
    "docker-compose down --volumes",
    "docker compose -f docker-compose.prod.yml down -v",
    "docker volume rm name-generator_name_generator_pgdata",
    "pg_restore -d name_generator master.dump",
    'docker exec -i name_generator_db psql -U postgres -c "DROP TABLE senses;"',
    "docker exec -i name_generator_db psql -U postgres -c 'TRUNCATE word_search_events'",
    'docker exec -i name_generator_db psql -U postgres -c "DELETE FROM word_search_events;"',
    'docker exec -i name_generator_db psql -U postgres -c "VACUUM FULL senses;"',
    "cd backend && alembic downgrade -1",
    "git push --force origin publish/a-repo-prep-hardening",
    "git push origin main",
    "git reset --hard HEAD~1",
    "ssh ubuntu@203.0.113.10",
    "rsync -avP slim.dump ubuntu@203.0.113.10:/data/",
    "bash deploy.sh",
    "cat backend/.env",
    "grep KEY frontend/.env.local",
    "cp .env.production /tmp/x",
    "DATABASE_URL=postgresql+psycopg://app:pw@db.example.com:5432/x python x.py",
    "psql postgresql://prod.example.com/name_generator",
]

ALLOWED = [
    "python -m pytest",
    "source backend/.venv/bin/activate && python -m pytest",
    "cat backend/.env.example",
    "grep -rn truncate backend/app",
    "grep -rn 'DROP TABLE' backend/migrations",
    "python -c 'import os; print(os.environ.get(\"HOME\"))'",
    "docker compose up -d",
    "docker compose down",
    "git push -u origin publish/a-repo-prep-hardening",
    "cd backend && alembic upgrade head",
    'docker exec -i name_generator_db psql -U postgres -d name_generator -c "SET lock_timeout = \'30s\'; SELECT count(*) FROM senses;"',
    'docker exec -i name_generator_db psql -U postgres -c "DELETE FROM word_search_events WHERE id = 1;"',
    "DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/name_generator alembic current",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocks(command):
    assert guard.check("Bash", {"command": command}) is not None, command


@pytest.mark.parametrize("command", ALLOWED)
def test_allows(command):
    assert guard.check("Bash", {"command": command}) is None, command


@pytest.mark.parametrize(("tool", "key", "path", "blocked"), [
    ("Read", "file_path", "/Users/x/name-generator/backend/.env", True),
    ("Read", "file_path", "/Users/x/name-generator/frontend/.env.local", True),
    ("Edit", "file_path", "/Users/x/name-generator/.env.production", True),
    ("Grep", "path", "backend/.env", True),
    ("Read", "file_path", "/Users/x/name-generator/backend/.env.example", False),
    ("Read", "file_path", "/Users/x/name-generator/.env.production.example", False),
    ("Read", "file_path", "/Users/x/name-generator/backend/app/config.py", False),
])
def test_file_tools(tool, key, path, blocked):
    assert (guard.check(tool, {key: path}) is not None) is blocked


def _run(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GUARD)], input=payload,
                          capture_output=True, text=True, timeout=10)


def test_protocol_block_exits_2_with_reason():
    out = _run(json.dumps({"tool_name": "Bash",
                           "tool_input": {"command": "docker compose down -v"}}))
    assert out.returncode == 2
    assert "BLOCKED" in out.stderr


def test_protocol_allow_exits_0():
    out = _run(json.dumps({"tool_name": "Bash",
                           "tool_input": {"command": "python -m pytest"}}))
    assert out.returncode == 0


def test_protocol_malformed_input_fails_closed():
    assert _run("not json").returncode == 2