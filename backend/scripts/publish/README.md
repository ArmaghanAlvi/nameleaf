# Publish build (PARTITION C4)

Scripts that turn master into a slimmed, reproducible reference database for
production. Written and tested in C4 **without running anything**; C5 runs
them for the first time. `c1_master/` and `c3_master/` hold the C1/C3
records.

## The publish mechanism

Each publish restores the reference dump into a **new** database
(`name_generator_<stamp>`) in the same Postgres instance, while the current
database keeps serving:

1. **Build** (Mac): `build_reference.sh` dumps master's `public` schema
   read-only, slims it in a throwaway container, verifies it against
   master, and writes a build directory (dump + manifest +
   `live_schema.sql`).
2. **Roles** (once per Postgres cluster): `roles.sql`.
3. **Restore**: `restore_reference.sh` creates the new database, restores
   `public`, runs `ANALYZE`, creates `live` (empty on the first publish, or
   carried from the current database), applies `grants.sql`, and runs both
   checks.
4. **Switch** (by hand; C6's runbook): point the backend's `DATABASE_URL` at
   `nameforge_app@…/<new db>` and restart it. Keep the previous database for
   one publish cycle as the rollback.

`live` and `public` share one database, because the dropdown joins
`live.sense_selection_stats` to `senses`. Carrying `live` needs no writes in
flight. At launch that holds, since `RANKING_STATS_WRITE=0`; once writes are
on, stop the backend for the copy.

## Slimming: projection, not emptying

The request path reads one key of `senses.raw_sense` and
`lexemes.raw_entry`: `"synonyms"`. The chain is
`build_query_text_from_selected_senses` → `collect_synonyms_for_embedding` →
`_kaikki_sense_level_synonyms` / `_kaikki_entry_level_synonyms`, and the key
list is `_EMBEDDABLE_SYNONYM_KEYS` in `app/services/sense_embeddings.py`.

- `projection.sql` keeps exactly those keys, with values verbatim, and drops
  everything else.
- `sense_embeddings.embedded_text` becomes `''`.
- The projection runs before any index exists, so it pays no index
  maintenance.
- There is no `VACUUM FULL`: `pg_dump` writes only live rows.

Rule: request-path code may read only the `_EMBEDDABLE_SYNONYM_KEYS` keys of
`raw_sense` / `raw_entry`. `tests/test_raw_columns_guard.py` enforces it, and
`tests/test_publish_scripts.py` ties the SQL key lists to the Python tuple.

## Verification: counts and digests

| Check | What it covers |
|---|---|
| `public_counts.sql` | exact row counts of every `public` table |
| `synonyms_digest.sql` | per-row md5 of what the projection keeps, for senses and lexemes |
| `embeddings_digest.sql` | per-row md5 of `(sense_id, embedding_model, embedding)` |

The build computes all three on master and on the build database and fails
on any difference. `check_restore.sh` recomputes them on every restored copy
and compares them with the manifest.

**Attribution rule for C5:** if counts and both digests are equal, the data
is identical. A gate diff on the restored copy then comes from the HNSW
index rebuild (the index is rebuilt, never copied, and the search uses an
approximate iterative scan), or from arm64 floats in the in-image run. It
does not come from slimming or restore. Whether a rebuild changes gate
results at all, and by how much, is unverified until C5.

## Safety

- Master (container `name_generator_db`, port 5433, database
  `name_generator`, volume `name-generator_name_generator_pgdata`) is only
  read:
  - a `pg_dump`, plus `psql` with `default_transaction_read_only=on`;
  - the master dump is written to a container path, `docker cp`'d out, and
    removed. That is the C1 method: a custom-format archive written to a
    pipe can't be restored in parallel.
- **Master must be quiet during the build**: no dev server, no searches. The
  build refuses if master has other connections. With the query-time LLM
  unfenced, an ordinary local search can write `public.root_llm_attempts`
  and `public.sense_translations`, so **this build's manifest, not C1's
  counts, is the reference**. `check_restore.sh` reports C1's counts as INFO
  only.
- Write-side scripts take every target as a required argument, with no
  defaults. They refuse master's container, port and database name before
  running anything, and check again at run time with `docker inspect`
  (mounts, ports).
- Every `psql` / `pg_dump` / `pg_restore` / `vacuumdb` runs **inside** a
  Postgres 17 container (`docker exec -e PGOPTIONS=…`), never with the
  host's Homebrew 18 tools. Every session carries `lock_timeout=30s`, and
  `pg_dump` adds `--lock-wait-timeout=30s`.
- `set -euo pipefail`; existing outputs are never overwritten; logs sit next
  to the outputs.
- No script drops a database. A failed restore leaves the new database for
  inspection.
- No passwords in the repo. `roles.sql` reads `NAMEFORGE_ADMIN_PASSWORD` and
  `NAMEFORGE_APP_PASSWORD` with `\getenv`, passed in with `docker exec -e`.

## Roles

| Role | Privileges |
|---|---|
| `nameforge_admin` | Owns every restored object: `CREATE DATABASE … OWNER nameforge_admin`, `pg_restore --no-owner --role=nameforge_admin`, and `live_schema.sql` run after `SET ROLE` |
| `nameforge_app` | The backend's role. `CONNECT`; `USAGE` and `SELECT` on `public`; `USAGE` on `live` with `SELECT`, `INSERT` and `UPDATE` on its tables and `USAGE` on its sequences; default privileges for future `live` objects. Nothing else. |

`CREATE EXTENSION vector` runs as the container superuser.

## Commands C5 runs, in order

Run from `backend/scripts/publish/`. Names, ports and paths are examples.

```bash
# 0. Master quiet (no dev server). Docker Desktop has room (~35–40 GB peak, an estimate).

# 1. Build (Mac). Stop between steps with --stop-after <step> if wanted.
./build_reference.sh --source-container name_generator_db --source-db name_generator \
    --build-container nameforge_build --port 55432 \
    --out ~/Personal-Projects/nameforge_build_20261101 \
    --jobs 4 --maintenance-work-mem 2GB --parallel-workers 2 --shm-size 3g

# 2. Rehearsal target: a clean pgvector/pgvector:pg17 container (C5 starts it;
#    NOT master), e.g. nameforge_rehearsal with its own volume and
#    --shm-size 1g (as production's compose; a parallel HNSW build needs
#    /dev/shm >= maintenance_work_mem, and Docker's default is 64 MB --
#    restore_reference.sh checks this). Then roles, once:
export NAMEFORGE_ADMIN_PASSWORD=... NAMEFORGE_APP_PASSWORD=...
docker exec -i -e PGOPTIONS='-c lock_timeout=30s' \
    -e NAMEFORGE_ADMIN_PASSWORD -e NAMEFORGE_APP_PASSWORD nameforge_rehearsal \
    psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres < roles.sql

# 3. Restore (first publish: empty live), including grants and both checks.
./restore_reference.sh --container nameforge_rehearsal --db name_generator_20261101 \
    --build-dir ~/Personal-Projects/nameforge_build_20261101 --live empty \
    --jobs 4 --maintenance-work-mem 1GB --parallel-workers 1

# 4. (Optional) the checks on their own.
./check_restore.sh --container nameforge_rehearsal --db name_generator_20261101 \
    --build-dir ~/Personal-Projects/nameforge_build_20261101
docker exec -i -e PGOPTIONS='-c lock_timeout=30s' nameforge_rehearsal \
    psql -X -U nameforge_app -d name_generator_20261101 < check_roles.sql

# Later publishes: carry live from the current database.
./restore_reference.sh ... --live carry-from name_generator_20261101 [--allow-connections]
```

Build steps: `preflight dump_master start predata data project postdata
digests dump_slim live_schema manifest`.

## Unverified until C5

- The `json_each` / `json_object_agg` rendering keeps the synonyms digest
  equal. The build fails loudly if it doesn't.
- The TOC filter (`SCHEMA - public`, `COMMENT - SCHEMA public`) makes the
  restore of a `-n public` dump into a new database clean with
  `--exit-on-error`.
- Ownership and grants behave as planned; `check_roles.sql` passes 10/10.
- Build-container readiness detection, and pgvector versions across image
  pulls (recorded in the manifest).
- Whether a rebuilt HNSW index moves gate results.
- Every time and size estimate.
