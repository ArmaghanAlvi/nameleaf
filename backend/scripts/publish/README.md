# Publish build (PARTITION C4)

Scripts that turn master into a slimmed, reproducible reference database for
production. Written and tested in C4; first run end to end in C5 on the Mac
(build, restore into the production stack, gates); `recall_check.sh` added
in C6. `c1_master/`, `c3_master/`, `c5_build/` and `c6_recall/` hold the
records.

The full operator sequence, from master to a switched server, is the
runbook `notes/publish-runbook.md`. It is local only: `notes/` is
gitignored.

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

## Recall check (`recall_check.sh`, C6)

A restored copy has the **same data** as master (counts and digests prove
it) but a **rebuilt HNSW graph**, so filtered vector results shift a little
on every publish (C-5.10). `recall_check.sh` measures how well an index finds
the exact neighbours, so the publish gate can be "recall did not drop"
instead of byte-identity.

- **`measure --container C --db D --probe recall_probe.tsv --out DIR`**
  (read-only). For each case (7 harness-pinned seed senses × 21 languages =
  147 cases) it runs `recall_case.sql`, which mirrors the app's queries,
  with the seed's stored embedding as the query vector:

  | List | Query |
  |---|---|
  | `ann150` | traversal, `relaxed_order`, LIMIT 150 (the width-3 fetch limit: the app's operating point) |
  | `ann15` | traversal, LIMIT 15 (continuity with C-5.10) |
  | `ann1` | the root fallback rung, `strict_order` LIMIT 1, cross-language only |
  | `exact` | index scans off, ordered by (distance, sense_id) |

  - A **hit** is an ANN row whose distance is ≤ the k-th exact distance, so
    a tie swapped at the boundary is not a miss.
  - Each list's **plan type** (HNSW index used or not) is recorded per case:
    a small language may get a sort plan, which reads as perfect recall and
    is a confound, not an index effect.
  - It writes `preflight.txt`, `cases.tsv`, `ids.tsv`, then `summary.txt`
    **last**, as the completion marker.
- **`compare --baseline DIR --candidate DIR --max-drop X`** (files only). It
  gives per-case, per-language and overall deltas; recall with and without
  cases whose plan type changed; and exact *distance* lists compared (a
  difference means the data differs). `--max-drop` has no default. C7 set the publish thresholds (Findings C-7.2): an overall drop of at most 0.02 on `ann15` and `ann150`, and at most 0.07 on `ann1`. Until `compare` takes per-list thresholds (E1), run it with `--max-drop 0.07` and check `ann15` and `ann150` against 0.02 (runbook step 6).
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | ok |
  | 1 | overall recall dropped by more than X on ann150, ann15 or ann1 |
  | 2 | usage or precondition error |
  | 3 | not comparable (no `summary.txt`, different probes, or exact distances differ) |
  | 4 | internal error |

- **Constants tied by tests:** `recall_case.sql`'s `max_scan_tuples`,
  `ef_search`, modes, model, filters and the 150 fetch limit are tied to
  their Python sources by `tests/test_publish_scripts.py`.
- **Intentional differences from the app:**
  - the query vector is a stored embedding, not an embedded query text;
  - languages are filtered by joining on code;
  - only ids and distances are selected;
  - ann1's exact top-1 is the first row of the traversal exact list, which
    is valid while a single embedding model exists (checked; ann1 is
    skipped otherwise).

## What C5 verified, and what remains

Verified on the Mac in C5 (Findings C-5.5–C-5.12):
- the synonyms, lexemes and embeddings digests are equal across master,
  build and restored copy;
- the TOC filter gives a clean `--exit-on-error` restore into a new
  database;
- ownership and grants work: check_restore all PASS, check_roles 10/10;
- build-container readiness detection (after `ecffa12`'s SIGPIPE fix);
- pgvector 0.8.3 on both sides;
- a rebuilt HNSW index moves the filtered gate captures, not `roots`
  (C-5.10). The publish gate is therefore check_restore + `roots` +
  `recall_check`;
- times and sizes: build 7,195 s, restore 4,162 s, reference dump 6.30 GB.

Remaining:
- `--live carry-from` has never run (recommend a Mac rehearsal before the
  second publish);
- server timings (E2);
- per-list thresholds in `compare` (E1);
- an amd64 server needs its in-image gates re-baselined (6h).
