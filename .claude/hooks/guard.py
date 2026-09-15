#!/usr/bin/env python3
"""
Claude Code PreToolUse guard (publishing roadmap Stage 0b).

A BACKSTOP, never the defense. The defense is structural: production
credentials never exist on this machine (CLAUDE.md safety rule 2). This
script catches the destructive and secret-reading patterns from CLAUDE.md's
safety rules in case an instruction is missed.

Protocol: Claude Code sends the pending tool call as JSON on stdin.
  exit 0 -> allow (normal permission rules still apply)
  exit 2 -> block; stderr is shown to Claude as the reason
Exit 1 does NOT block, so every failure path here exits 2 (fail closed).

Tested by backend/tests/test_claude_guard.py, which runs in CI.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import PurePath

# A .env-style filename anywhere in a command: backend/.env, .env.production,
# frontend/.env.local. The look-arounds keep os.environ and .venv out.
_ENV_NAME = re.compile(r"(?<![\w.])\.env(?:\.[A-Za-z0-9_-]+)*(?![\w.-])")

_LOCAL_HOST = r"(?:localhost|127\.0\.0\.1|db)(?:[:/]|$)"

# `git`, optionally followed by -C <path> / -c <key=value>, then the subcommand.
_GIT = r"\bgit(?:\s+-[cC]\s+\S+)*\s+"

_BASH_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_GIT + r"(?:add|commit|push|pull|rm|mv|merge|rebase|cherry-pick|revert|am)(?![\w-])"),
     "staging, committing, pushing and other git history writes are the user's steps "
     "(CLAUDE.md safety rule 5)"),
    (re.compile(_GIT + r"apply\b[^;&|]*\s--(?:index|cached)\b"),
     "`git apply --index/--cached` stages changes, which is the user's step"),
    (re.compile(_GIT + r"(?:reset\s+--hard|clean|restore)(?![\w-])"),
     "this git command discards uncommitted work irreversibly"),
    (re.compile(_GIT + r"checkout\b[^;&|]*\s--(?:\s|$)"),
     "`git checkout -- <path>` discards uncommitted work irreversibly"),
    (re.compile(r"\bdocker(?:-compose|\s+compose)\b(?=[^;&|]*\bdown\b)(?=[^;&|]*\s(?:-v|--volumes)\b)"),
     "`docker compose down -v` deletes database volumes"),
    (re.compile(r"\bdocker\s+(?:volume\s+(?:rm|prune)|system\s+prune)\b"),
     "removing Docker volumes can delete the master database"),
    (re.compile(r"\bpg_restore\b|\bdropdb\b"),
     "pg_restore / dropdb overwrite or delete a database"),
    (re.compile(r"\balembic\s+downgrade\b"),
     "`alembic downgrade` is a destructive schema change"),
    (re.compile(r"(?:^|[\s;&|(])(?:ssh|scp|sftp)\s"),
     "connecting to remote machines is the user's step (production is unreachable by design)"),
    (re.compile(r"\brsync\b[^;&|]*\s(?:[\w.-]+@)?[\w.-]+:"),
     "copying to a remote machine is the user's step"),
    (re.compile(r"\bdeploy\.sh\b"),
     "deploying is the user's step"),
    (re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s'\"@/]*@(?!" + _LOCAL_HOST + r")[^\s'\"/:]+", re.I),
     "a non-local database URL (production is unreachable from this machine by design)"),
    (re.compile(r"postgres(?:ql)?(?:\+\w+)?://(?![^\s'\"/]*@)(?!" + _LOCAL_HOST + r")[^\s'\"/:]+", re.I),
     "a non-local database URL (production is unreachable from this machine by design)"),
    (re.compile(r"\brm\s+-[A-Za-z]*[rR][A-Za-z]*\s+(?:/|~/?|\$HOME/?)(?:\s|$)"),
     "recursive delete of a root or home directory"),
)

# SQL rules only apply when the command can actually execute SQL, so that
# e.g. `grep -rn truncate backend/app` stays allowed.
_SQL_CONTEXT = re.compile(r"\bpsql\b|\bdocker\s+exec\b|\bpython3?\s+-c\b|\bexecute\s*\(|\btext\s*\(", re.I)
_SQL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdrop\s+(?:table|schema|database|index|extension|view|materialized\s+view|role|user)\b", re.I),
     "DROP destroys database objects"),
    (re.compile(r"\btruncate\b", re.I),
     "TRUNCATE deletes table contents"),
    (re.compile(r"\bdelete\s+from\s+[\w.\"]+\s*(?:;|\"|'|$)", re.I),
     "DELETE without a WHERE clause"),
    (re.compile(r"\balter\s+table\b[^;]*\bdrop\b", re.I),
     "ALTER TABLE ... DROP removes columns or constraints"),
    (re.compile(r"\bvacuum\s+full\b|\breindex\b", re.I),
     "VACUUM FULL / REINDEX lock tables for a long time on a 21 GB database"),
)


def _secret_env_names(text: str) -> list[str]:
    return [m.group(0) for m in _ENV_NAME.finditer(text)
            if not m.group(0).endswith(".example")]


def check_bash(command: str) -> str | None:
    for pattern, reason in _BASH_RULES:
        if pattern.search(command):
            return reason
    if _SQL_CONTEXT.search(command):
        for pattern, reason in _SQL_RULES:
            if pattern.search(command):
                return reason
    secrets = _secret_env_names(command)
    if secrets:
        return f"the command references secret env file(s) {', '.join(sorted(set(secrets)))}"
    return None


def is_secret_env_path(path: str) -> bool:
    name = PurePath(path).name
    return (name == ".env" or name.startswith(".env.")) and not name.endswith(".example")


def check(tool_name: str, tool_input: dict) -> str | None:
    if tool_name == "Bash":
        return check_bash(str(tool_input.get("command", "")))
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and is_secret_env_path(value):
            return (f"reading or editing secret env file {value!r} "
                    "(use .env.example for variable names)")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        reason = check(str(payload.get("tool_name", "")), payload.get("tool_input") or {})
    except Exception as exc:  # fail closed: exit 1 would silently allow
        print(f"BLOCKED by .claude/hooks/guard.py: could not evaluate the tool call ({exc}).",
              file=sys.stderr)
        return 2
    if reason is None:
        return 0
    print(f"BLOCKED by .claude/hooks/guard.py: {reason}.\n"
          "CLAUDE.md safety rule 4: do not try an equivalent command. "
          "Stop and tell the user what was blocked.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())