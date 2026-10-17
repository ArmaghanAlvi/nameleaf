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
                 "check_restore.sh", "recall_check.sh", "recall_case.sql",
                 "recall_probe.tsv", "projection.sql", "synonyms_digest.sql",
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
            # -E (errtrace) is allowed on top: recall_check.sh needs its ERR
            # trap to fire inside functions.
            assert re.search(r"^set -E?euo pipefail$", text, re.M), script.name
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


# --- recall_check.sh (C6) ------------------------------------------------------

PROBE = PUBLISH / "recall_probe.tsv"
CASE_SQL = PUBLISH / "recall_case.sql"


def _measure_args(out: Path, **over) -> list[str]:
    a = {"--container": "nameforge-db-1", "--db": "name_generator_20261015",
         "--probe": str(PROBE), "--out": str(out)}
    a.update(over)
    return ["measure", *[x for k, v in a.items() if v is not None for x in (k, v)]]


@pytest.mark.parametrize("over,why", [
    ({"--container": None}, "missing --container"),
    ({"--db": None}, "missing --db"),
    ({"--probe": None}, "missing --probe"),
    ({"--out": None}, "missing --out"),
    ({"--probe": "/nonexistent/probe.tsv"}, "probe file missing"),
    ({"--db": "bad;name"}, "invalid database name"),
])
def test_recall_measure_refuses_before_any_binary(tmp_path, fakes, over, why):
    out = tmp_path / "out"
    r, calls = _run("recall_check.sh", _measure_args(out, **over), fakes, tmp_path)
    assert r.returncode == 2, (why, r.returncode, r.stderr)
    assert calls == "", f"{why}: a binary was called: {calls}"
    assert not out.exists(), why


def test_recall_measure_refuses_existing_output(tmp_path, fakes):
    out = tmp_path / "out"
    out.mkdir()
    r, calls = _run("recall_check.sh", _measure_args(out), fakes, tmp_path)
    assert r.returncode == 2
    assert calls == ""


def test_recall_measure_reaches_the_container_on_valid_args(tmp_path, fakes):
    """Control: valid arguments DO reach the (failing) fake docker, and the
    unexpected failure maps to exit 4, never 1."""
    r, calls = _run("recall_check.sh", _measure_args(tmp_path / "o"), fakes, tmp_path)
    assert calls.startswith("docker exec"), (calls, r.stderr)
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert not (tmp_path / "o" / "summary.txt").exists()


def test_recall_usage_errors_exit_2(tmp_path, fakes):
    for args in ([], ["nonsense"], ["compare", "--baseline", "a", "--candidate", "b"]):
        r, calls = _run("recall_check.sh", args, fakes, tmp_path)
        assert r.returncode == 2, (args, r.returncode, r.stderr)
        assert calls == ""


# Fixture directories for compare. Each case: (sid, lang, list, k, exact_n,
# ann_n, hits, plan). ids.tsv carries the exact distance lists.

def _write_measure_dir(d: Path, cases, exact: dict, complete=True):
    d.mkdir()
    rows = ["sid\tlang\tlist\tk\texact_n\tann_n\thits\tplan\tms"]
    rows += ["\t".join(map(str, c)) + "\t5" for c in cases]
    (d / "cases.tsv").write_text("\n".join(rows) + "\n")
    ids = ["sid\tlang\tlist\trank\tsense_id\tdistance"]
    for (sid, lang), pairs in exact.items():
        for rank, (sense_id, dist) in enumerate(pairs, 1):
            ids.append(f"{sid}\t{lang}\texact\t{rank}\t{sense_id}\t{dist}")
    (d / "ids.tsv").write_text("\n".join(ids) + "\n")
    if complete:
        (d / "summary.txt").write_text("COMPLETE\n")


BASE_CASES = [
    (1, "ru", "ann150", 150, 150, 150, 140, "index"),
    (1, "ru", "ann15", 15, 15, 15, 13, "index"),
    (1, "ru", "ann1", 1, 1, 1, 1, "index"),
    (1, "ga", "ann150", 150, 150, 150, 120, "index"),
    (1, "ga", "ann15", 15, 15, 15, 8, "index"),
    (1, "ga", "ann1", 1, 1, 1, 1, "index"),
]
EXACT = {(1, "ru"): [(10, "0.1"), (11, "0.2")], (1, "ga"): [(20, "0.3"), (21, "0.3")]}


def _compare(tmp_path, fakes, cand_cases, cand_exact=EXACT, max_drop="0.03",
             cand_complete=True):
    base, cand = tmp_path / "base", tmp_path / "cand"
    _write_measure_dir(base, BASE_CASES, EXACT)
    _write_measure_dir(cand, cand_cases, cand_exact, complete=cand_complete)
    r, calls = _run("recall_check.sh",
                    ["compare", "--baseline", str(base), "--candidate", str(cand),
                     "--max-drop", max_drop], fakes, tmp_path)
    assert calls == "", "compare must not call any binary"
    return r


def test_recall_compare_identical_passes(tmp_path, fakes):
    r = _compare(tmp_path, fakes, BASE_CASES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: PASS" in r.stdout
    assert "exact_distance_lists_differing=0" in r.stdout


def test_recall_compare_small_drop_within_threshold(tmp_path, fakes):
    cand = list(BASE_CASES)
    cand[1] = (1, "ru", "ann15", 15, 15, 15, 12, "index")  # ru hits 13 -> 12
    r = _compare(tmp_path, fakes, cand, max_drop="0.05")
    # ann15: baseline 21/30 = 0.7000, candidate 20/30 = 0.6667, drop 0.0333.
    assert r.returncode == 0, r.stdout
    assert "ann15\t0.7000\t0.6667\t+0.0333\tok" in r.stdout


def test_recall_compare_drop_beyond_threshold_exits_1(tmp_path, fakes):
    cand = list(BASE_CASES)
    cand[3] = (1, "ga", "ann150", 150, 150, 150, 100, "index")  # 260/300 -> 240/300
    r = _compare(tmp_path, fakes, cand, max_drop="0.03")
    assert r.returncode == 1, r.stdout
    assert "ann150\t0.8667\t0.8000\t+0.0667\tDROP>X" in r.stdout
    assert "RESULT: FAIL" in r.stdout


def test_recall_compare_gates_on_ann1_too(tmp_path, fakes):
    cand = list(BASE_CASES)
    cand[2] = (1, "ru", "ann1", 1, 1, 1, 0, "index")  # 2/2 -> 1/2
    r = _compare(tmp_path, fakes, cand, max_drop="0.03")
    assert r.returncode == 1, r.stdout


def test_recall_compare_reports_plan_type_differences(tmp_path, fakes):
    cand = list(BASE_CASES)
    cand[4] = (1, "ga", "ann15", 15, 15, 15, 15, "noindex")  # sort plan: 'perfect'
    r = _compare(tmp_path, fakes, cand, max_drop="0.03")
    assert "plan_type_differences=1" in r.stdout
    assert "1\tga\tann15\tindex -> noindex" in r.stdout
    # with the case: 21/30 -> 28/30; without it: 13/15 -> 13/15.
    assert "ann15\t0.7000\t0.9333\t-0.2333\tok\t|\t0.8667\t0.8667\t+0.0000" in r.stdout
    assert r.returncode == 0


def test_recall_compare_exact_ids_may_differ_only_inside_ties(tmp_path, fakes):
    swapped = dict(EXACT)
    swapped[(1, "ga")] = [(21, "0.3"), (20, "0.3")]  # tie at 0.3, ids swapped
    r = _compare(tmp_path, fakes, BASE_CASES, cand_exact=swapped)
    assert r.returncode == 0, r.stdout
    assert "exact_id_lists_differing_only_in_ties=1" in r.stdout


def test_recall_compare_exact_distances_differ_exits_3(tmp_path, fakes):
    changed = dict(EXACT)
    changed[(1, "ru")] = [(10, "0.1"), (12, "0.25")]
    r = _compare(tmp_path, fakes, BASE_CASES, cand_exact=changed)
    assert r.returncode == 3, r.stdout
    assert "DIFFERS\t1\tru" in r.stdout


def test_recall_compare_refuses_incomplete_measure(tmp_path, fakes):
    r = _compare(tmp_path, fakes, BASE_CASES, cand_complete=False)
    assert r.returncode == 3
    assert "no summary.txt" in r.stdout


def test_recall_compare_refuses_mismatched_probes(tmp_path, fakes):
    r = _compare(tmp_path, fakes, BASE_CASES[:3])
    assert r.returncode == 3
    assert "probe cases differ" in r.stdout


def test_recall_compare_bad_max_drop_exits_2(tmp_path, fakes):
    r = _compare(tmp_path, fakes, BASE_CASES, max_drop="abc")
    assert r.returncode == 2


# --- the recall SQL mirrors the app -------------------------------------------

def _sql_constants() -> dict[str, str]:
    return dict(re.findall(r"^\\set (\w+) (\S+)$", CASE_SQL.read_text(), re.M))


def test_recall_sql_constants_tie_to_python():
    from app.services import vector_scope
    from app.services.embedding_provider import DEFAULT_EMBEDDING_MODEL

    c = _sql_constants()
    assert int(c["max_scan_tuples"]) == vector_scope.MAX_SCAN_TUPLES
    assert int(c["ef_search"]) == vector_scope.EF_SEARCH
    assert c["model"] == DEFAULT_EMBEDDING_MODEL
    assert c["k_continuity"] == "15"      # C-5.10's top 15
    assert c["k_fallback"] == "1"
    sql = CASE_SQL.read_text()
    for mode in ("relaxed_order", "strict_order"):
        assert mode in vector_scope._VALID_MODES
        assert f"SET LOCAL hnsw.iterative_scan = {mode};" in sql
    assert "SET LOCAL hnsw.max_scan_tuples = :max_scan_tuples;" in sql
    assert "SET LOCAL hnsw.ef_search = :ef_search;" in sql


def test_recall_k_traversal_is_the_apps_width3_fetch_limit():
    src = (BACKEND / "app" / "services" / "vector_sense_search.py").read_text()
    m = re.search(r"min\(\s*max\(expansion_count \* (\d+), (\d+)\),\s*"
                  r"(\d+) if _tree_code == \"en\" else (\d+),\s*\)", src)
    assert m, "candidate_fetch_limit formula changed; update recall_case.sql"
    per, floor, en_cap, other_cap = map(int, m.groups())
    width3 = max(3 * per, floor)
    k = int(_sql_constants()["k_traversal"])
    assert k == min(width3, other_cap) == min(width3, en_cap) == 150


def test_recall_filters_mirror_the_app_queries():
    vss = (BACKEND / "app" / "services" / "vector_sense_search.py").read_text()
    for needle in ("SenseEmbedding.embedding_model == DEFAULT_EMBEDDING_MODEL",
                   'Sense.visibility_status == "visible"',
                   "Lexeme.language_id == tree_language_id",
                   "~SenseEmbedding.sense_id.in_(selected_ids)",
                   'mode="relaxed_order"'):
        assert needle in vss, needle
    rs = (BACKEND / "app" / "services" / "root_selection.py").read_text()
    rung = rs[rs.index("def _vector_rung"):rs.index("def vector_fallback_root")]
    assert 'mode="strict_order"' in rung and ".limit(1)" in rung
    assert 'Sense.visibility_status == "visible"' in rung
    assert "embedding_model" not in rung      # the rung has no model filter
    sql = CASE_SQL.read_text()
    traversal = [b for b in sql.split("\\echo @@begin ")[1:]
                 if b.startswith(("ann150", "ann15", "exact"))]
    assert len(traversal) == 3
    for b in traversal:
        for needle in ("se.embedding_model = :'model'",
                       "s.visibility_status = 'visible'",
                       "g.code = :'lang'",
                       "se.sense_id NOT IN (SELECT x.id FROM senses x WHERE x.lexeme_id ="):
            assert needle in b, needle
    fallback = sql.split("\\echo @@begin ann1\n")[1].split("\\echo @@end")[0]
    assert "s.visibility_status = 'visible'" in fallback
    assert "se.embedding_model = :'model'" not in fallback
    assert "NOT IN" not in fallback
    assert "LIMIT :k_fallback" in fallback
    exact = sql.split("\\echo @@begin exact\n")[1]
    assert "ORDER BY 2, 1" in exact            # distance, then sense_id
    assert "SET LOCAL enable_indexscan = off;" in sql
    assert sql.strip().endswith("ROLLBACK;")


def test_recall_probe_set():
    lines = PROBE.read_text().splitlines()
    assert lines[0].split("\t")[:2] == ["sense_id", "language"]
    rows = [tuple(ln.split("\t")[:2]) for ln in lines[1:]]
    assert len(rows) == len(set(rows))
    seeds = sorted({int(s) for s, _ in rows})
    langs = sorted({lang for _, lang in rows})
    roots = json.loads((BACKEND / "scripts" / "eval" / "publishC_baseline" /
                        "post_cpu" / "roots.json").read_text())
    assert langs == sorted(["en", *roots["languages"]]) and len(langs) == 21
    assert {23466, 7459, 72902, 57055} <= set(seeds)   # C-5.10's four
    assert len(rows) == len(seeds) * 21                 # full cross product
    assert len(rows) * 15 >= 2000                        # recall@15 comparisons
