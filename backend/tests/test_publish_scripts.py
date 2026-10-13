"""C4: the publish-build scripts (backend/scripts/publish/).

Nothing here touches a database or a container. A fake-binary harness puts
fake docker / psql / pg_dump / pg_restore / vacuumdb first on PATH; each
records its argv and exits 1. The refusal tests assert that a write-side
script aimed at master exits non-zero BEFORE any fake is called.

The remaining tests are static: syntax (`bash -n`), the projection key list
against sense_embeddings._EMBEDDABLE_SYNONYM_KEYS, a Python model of the
projection and the synonyms digest, and lints for the safety rules
(PGOPTIONS on every in-container client, lock timeouts, no DROP DATABASE, no
literal passwords, check_roles.sql's rolled-back transaction).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
PUBLISH = BACKEND / "scripts" / "publish"
SCRIPTS = sorted(PUBLISH.glob("*.sh"))
SQL_FILES = sorted(PUBLISH.glob("*.sql"))
FAKES = ("docker", "psql", "pg_dump", "pg_restore", "vacuumdb", "pg_isready")


def test_expected_files_exist():
    names = {p.name for p in PUBLISH.iterdir()}
    for name in ("lib.sh", "build_reference.sh", "restore_reference.sh",
                 "check_restore.sh", "projection.sql", "synonyms_digest.sql",
                 "embeddings_digest.sql", "public_counts.sql", "roles.sql",
                 "grants.sql", "check_restore.sql", "check_roles.sql",
                 "README.md"):
        assert name in names, name


# --- syntax -----------------------------------------------------------------

@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None,
                    reason="shellcheck not installed (not installed by policy)")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck(script):
    r = subprocess.run(["shellcheck", "-x", str(script)], cwd=PUBLISH,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# --- fake-binary harness -------------------------------------------------------

@pytest.fixture
def fakes(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in FAKES:
        f = bin_dir / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 1\n')
        f.chmod(0o755)
    return bin_dir, log


def _run(script: str, args: list[str], fakes, cwd: Path):
    bin_dir, log = fakes
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(cwd)}
    r = subprocess.run(["bash", str(PUBLISH / script), *args], cwd=cwd,
                       env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text() if log.exists() else ""
    return r, calls


def _build_args(out: Path, **over) -> list[str]:
    a = {"--source-container": "name_generator_db", "--source-db": "name_generator",
         "--build-container": "nameforge_build", "--port": "55432",
         "--out": str(out)}
    a.update(over)
    return [x for k, v in a.items() if v is not None for x in (k, v)]


def _restore_args(build_dir: Path, **over) -> list[str]:
    a = {"--container": "nameforge-db-1", "--db": "name_generator_20261101",
         "--build-dir": str(build_dir), "--live": "empty"}
    a.update(over)
    out: list[str] = []
    for k, v in a.items():
        if v is None:
            continue
        out.append(k)
        out.extend(v.split(" "))
    return out


@pytest.fixture
def build_dir(tmp_path):
    d = tmp_path / "build"
    d.mkdir()
    (d / "manifest.txt").write_text(
        "alembic=a7c3e91f0b52\n"
        "reference_dump=reference_public.dump\n"
        "reference_dump_sha256=0000\n"
        "live_schema_file=live_schema.sql\n"
        "counts_file=public_counts.tsv\n"
        "digests_file=digests.tsv\n"
        "pgvector_master=0.8.0\npgvector_build=0.8.0\n")
    (d / "reference_public.dump").write_text("not a real dump")
    return d


@pytest.mark.parametrize("over,why", [
    ({"--build-container": "name_generator_db"}, "master container as build target"),
    ({"--port": "5433"}, "master's port"),
    ({"--build-container": None}, "missing --build-container"),
    ({"--port": None}, "missing --port"),
    ({"--source-db": None}, "missing --source-db"),
    ({"--stop-after": "nonsense"}, "unknown step"),
])
def test_build_refuses_before_any_binary(tmp_path, fakes, over, why):
    out = tmp_path / "out"
    r, calls = _run("build_reference.sh", _build_args(out, **over), fakes, tmp_path)
    assert r.returncode != 0, why
    assert calls == "", f"{why}: a binary was called: {calls}"
    assert not out.exists(), why


def test_build_refuses_existing_output(tmp_path, fakes):
    out = tmp_path / "out"
    out.mkdir()
    r, calls = _run("build_reference.sh", _build_args(out), fakes, tmp_path)
    assert r.returncode != 0
    assert calls == ""


@pytest.mark.parametrize("over,why", [
    ({"--container": "name_generator_db"}, "master container"),
    ({"--db": "name_generator"}, "master database name"),
    ({"--container": None}, "missing --container"),
    ({"--db": None}, "missing --db"),
    ({"--live": None}, "missing --live"),
    ({"--live": "carry-from"}, "carry-from without a source"),
    ({"--live": "copy"}, "bad --live mode"),
    ({"--db": "bad;name"}, "invalid database name"),
    ({"--live": "carry-from name_generator_20261101"}, "carry from itself"),
])
def test_restore_refuses_before_any_binary(tmp_path, fakes, build_dir, over, why):
    r, calls = _run("restore_reference.sh", _restore_args(build_dir, **over),
                    fakes, tmp_path)
    assert r.returncode != 0, why
    assert calls == "", f"{why}: a binary was called: {calls}"


def test_restore_refuses_existing_log_dir(tmp_path, fakes, build_dir):
    (build_dir / "restore_name_generator_20261101").mkdir()
    r, calls = _run("restore_reference.sh", _restore_args(build_dir), fakes, tmp_path)
    assert r.returncode != 0
    assert calls == ""


def test_harness_sees_calls_on_a_valid_target(tmp_path, fakes, build_dir):
    """Control: with a non-master target the scripts DO reach the (failing)
    fake binaries -- proof the refusal tests would notice a call."""
    r, calls = _run("restore_reference.sh", _restore_args(build_dir), fakes, tmp_path)
    assert r.returncode != 0
    assert calls.startswith("docker inspect"), (calls, r.stderr)
    r, calls = _run("build_reference.sh", _build_args(tmp_path / "o2"), fakes, tmp_path)
    assert r.returncode != 0
    assert calls.startswith("docker"), (calls, r.stderr)


def test_missing_manifest_key_fails_clearly(tmp_path, fakes, build_dir):
    text = (build_dir / "manifest.txt").read_text()
    (build_dir / "manifest.txt").write_text(
        text.replace("live_schema_file=live_schema.sql\n", ""))
    r, calls = _run("restore_reference.sh", _restore_args(build_dir), fakes, tmp_path)
    assert r.returncode != 0
    assert "lacks 'live_schema_file'" in r.stdout + r.stderr


# --- key list ---------------------------------------------------------------

def _key_lists(path: Path) -> list[tuple[str, ...]]:
    text = path.read_text()
    lists = [tuple(re.findall(r"'([^']*)'", m))
             for m in re.findall(r"e\.key IN \(([^)]*)\)", text)]
    marker = re.findall(r"^-- KEYS:\s*(.+?)\s*(?:\(|$)", text, re.M)
    lists += [tuple(k.strip() for k in m.split(",")) for m in marker]
    return lists


def test_projection_keys_equal_embeddable_synonym_keys():
    from app.services.sense_embeddings import _EMBEDDABLE_SYNONYM_KEYS
    for name in ("projection.sql", "synonyms_digest.sql"):
        lists = _key_lists(PUBLISH / name)
        assert len(lists) >= 2, name
        for keys in lists:
            assert keys == tuple(_EMBEDDABLE_SYNONYM_KEYS), (name, keys)


# --- Python model of the projection and the synonyms digest ---------------------
# Mirrors projection.sql and synonyms_digest.sql row by row, so the DESIGN is
# checked without a database: digest(master) == digest(project(master)), and
# any change to a synonyms value changes the digest. Whether Postgres renders
# json_each values identically before and after json_object_agg is checked
# for real in C5 (the build compares master and build digests and fails if
# they differ).

KEYS = ("synonyms",)


def _project(value):
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if k in KEYS}


def _row_repr(row_id: int, value) -> str:
    if isinstance(value, dict):
        kept = [f"{k}={json.dumps(v)}" for k, v in value.items() if k in KEYS]
        return f"{row_id}\to\t" + "\x1f".join(kept)
    kind = {list: "array", str: "string", type(None): "null"}.get(type(value), "number")
    return f"{row_id}\t{kind}\t{json.dumps(value)}"


def _digest(rows: dict[int, object]) -> str:
    hashes = "".join(hashlib.md5(_row_repr(i, rows[i]).encode()).hexdigest()
                     for i in sorted(rows))
    return hashlib.md5(hashes.encode()).hexdigest()


def _python_synonym_words(value) -> list:
    raw = value or {}
    out = []
    for key in KEYS:
        for item in raw.get(key) or []:
            out.append(item.get("word") if isinstance(item, dict) else item)
    return out


MASTER = {
    1: {"synonyms": [{"word": "luminance", "sense": "visible light"}],
        "glosses": ["a"], "categories": ["x"], "translations": [{"lang": "de"}]},
    2: {"glosses": ["no synonyms here"]},
    3: {"synonyms": None, "examples": ["e"]},
    4: {},
    5: None,
    6: {"head_templates": [], "synonyms": ["radiance", {"word": "lambency"}]},
}


def test_projection_keeps_only_synonyms():
    projected = {i: _project(v) for i, v in MASTER.items()}
    assert projected[1] == {"synonyms": MASTER[1]["synonyms"]}
    assert projected[2] == {} and projected[4] == {}
    assert projected[3] == {"synonyms": None}
    assert projected[5] is None


def test_projection_preserves_what_the_request_path_reads():
    for i, v in MASTER.items():
        assert _python_synonym_words(_project(v)) == _python_synonym_words(v), i


def test_digest_equal_between_master_and_slim():
    assert _digest(MASTER) == _digest({i: _project(v) for i, v in MASTER.items()})


@pytest.mark.parametrize("mutate", [
    lambda m: m[1]["synonyms"][0].update(word="brightness"),
    lambda m: m[1]["synonyms"][0].update(sense="other sense"),
    lambda m: m[2].update(synonyms=[{"word": "new"}]),
    lambda m: m[6].pop("synonyms"),
    lambda m: m.update({7: {"synonyms": []}}),
])
def test_digest_changes_when_synonyms_change(mutate):
    changed = json.loads(json.dumps(MASTER))
    changed = {int(k): v for k, v in changed.items()}
    mutate(changed)
    assert _digest(changed) != _digest(MASTER)


def test_digest_ignores_dropped_keys():
    other = json.loads(json.dumps(MASTER))
    other = {int(k): v for k, v in other.items()}
    other[1]["glosses"] = ["completely different"]
    other[2]["etymology_text"] = "irrelevant"
    assert _digest(other) == _digest(MASTER)


# --- lints --------------------------------------------------------------------

def _logical_lines(path: Path) -> list[str]:
    """Join backslash continuations and pipes split across lines."""
    out, buf = [], ""
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        buf += " " + stripped
        if stripped.endswith("\\") or stripped.endswith("|"):
            buf = buf.rstrip("\\")
            continue
        out.append(buf.strip())
        buf = ""
    if buf:
        out.append(buf.strip())
    return out


CLIENTS = re.compile(r"\b(psql|pg_dump|pg_restore|vacuumdb)\b")
INVOCATION = re.compile(r"(?:^|[|;&(]\s*|\$\(\s*)(?:docker\s+exec\b|psql_ro\b|psql_rw\b)")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_in_container_client_carries_pgoptions(script):
    for line in _logical_lines(script):
        if not re.search(r"\bdocker\s+exec\b", line) or not CLIENTS.search(line):
            continue
        for segment in re.split(r"(?=docker\s+exec\b)", line):
            if not segment.startswith("docker exec") or not CLIENTS.search(segment):
                continue
            ok = "-e PGOPTIONS" in segment
            if re.search(r"\bpg_dump\b", segment) and "--version" not in segment:
                ok = ok and "--lock-wait-timeout" in segment
            assert ok, f"{script.name}: {segment}"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_host_postgres_clients(script):
    """psql / pg_dump / pg_restore / vacuumdb only ever run inside a container
    (docker exec, or the psql_ro/psql_rw helpers): the host's are 18.x."""
    for line in _logical_lines(script):
        code = re.sub(r"(\"[^\"]*\"|'[^']*')", "", line)  # drop quoted text
        if not CLIENTS.search(code):
            continue
        if re.match(r"(note|die|echo|printf|pass|fail|info)\b", code.strip()):
            continue
        if re.search(r"^\s*(\w+\(\)\s*\{|#)", code):
            continue
        assert re.search(r"docker\s+exec\b|psql_ro\b|psql_rw\b", code), \
            f"{script.name}: host client call: {line}"


def test_strict_mode_and_no_drop_database():
    for script in SCRIPTS:
        text = script.read_text()
        if script.name != "lib.sh":
            assert "set -euo pipefail" in text, script.name
    for path in [*SCRIPTS, *SQL_FILES]:
        assert not re.search(r"drop\s+database|dropdb", path.read_text(), re.I), path.name


def test_sql_sessions_set_lock_timeout():
    for name in ("roles.sql", "grants.sql", "check_roles.sql"):
        assert "lock_timeout = '30s'" in (PUBLISH / name).read_text(), name
    assert 'PGOPTS_WRITE="-c lock_timeout=30s"' in (PUBLISH / "lib.sh").read_text()
    assert "default_transaction_read_only=on" in (PUBLISH / "lib.sh").read_text()


def test_no_literal_passwords():
    roles = (PUBLISH / "roles.sql").read_text()
    assert not re.search(r"PASSWORD\s+'", roles)
    assert "\\getenv admin_password NAMEFORGE_ADMIN_PASSWORD" in roles
    assert "\\getenv app_password NAMEFORGE_APP_PASSWORD" in roles
    for path in [*SCRIPTS, *SQL_FILES]:
        assert not re.search(r"PASSWORD\s*=\s*['\"][^$'\"]", path.read_text()), path.name


def test_check_roles_runs_in_a_rolled_back_transaction():
    statements = [ln.strip() for ln in (PUBLISH / "check_roles.sql").read_text().splitlines()
                  if ln.strip() and not ln.strip().startswith(("--", "\\"))]
    assert statements[0] == "BEGIN;"
    assert statements[-1] == "ROLLBACK;"
    text = (PUBLISH / "check_roles.sql").read_text()
    assert "current_user <> 'nameforge_app'" in text
    assert text.count("RAISE NOTICE 'PASS") == 10


def test_grants_give_app_exactly_the_planned_privileges():
    text = (PUBLISH / "grants.sql").read_text()
    grants = re.findall(r"^\s*GRANT .*?;", text, re.M | re.S)
    for g in grants:
        assert "DELETE" not in g and "TRUNCATE" not in g and "ALL PRIVILEGES" not in g, g
        assert "nameforge_app" in g, g
    assert "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA live" in text
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public" in text
    assert not re.search(r"GRANT\s+(INSERT|UPDATE)[^;]*SCHEMA public", text)


def test_required_alembic_matches_the_migration_head():
    versions = BACKEND / "migrations" / "versions"
    revs, downs = set(), set()
    for f in versions.glob("*.py"):
        src = f.read_text()
        r = re.search(r'^revision[^=]*=\s*["\']([0-9a-f]+)["\']', src, re.M)
        d = re.search(r'^down_revision[^=]*=\s*(.+)$', src, re.M)
        if r:
            revs.add(r.group(1))
        if d:
            downs.update(re.findall(r"[0-9a-f]{12}", d.group(1)))
    heads = revs - downs
    lib = (PUBLISH / "lib.sh").read_text()
    m = re.search(r'^REQUIRED_ALEMBIC="([0-9a-f]+)"', lib, re.M)
    assert m and heads == {m.group(1)}, (heads, m and m.group(1))
