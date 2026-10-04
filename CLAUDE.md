# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Safety rules (read first — these override convenience)

1. **Database changes need confirmation.** Never make irreversible changes to the database without first explaining the change and waiting for confirmation. This includes `DROP`, `TRUNCATE`, unscoped `DELETE`/`UPDATE`, `alembic downgrade`, destructive migrations, `pg_restore` into an existing database, `docker compose down -v`, and `docker volume rm`. Long-running maintenance (`VACUUM FULL`, `REINDEX`, large backfills) also needs confirmation. Prefix any potentially long `psql` statement with `SET lock_timeout = '30s';` so a blocked statement fails fast instead of hanging silently.
2. **Production is unreachable from this machine, by design.** This machine only ever talks to the local Docker database. Never write a non-localhost database URL or production hostname into any file, script default, config, or command. Never ask for production credentials, SSH to a server, or run deploy/publish steps against a server. If a task appears to require production access, stop: that step belongs to the user.
3. **Secrets stay unread.** Do not read `backend/.env` or `frontend/.env*.local`; use `backend/.env.example` for variable names. Never print, log, or commit secrets. Every new setting gets a placeholder line in `.env.example`.
4. **Guardrails mean stop.** If a hook or permission rule blocks a command, do not look for an equivalent command that achieves the same effect. Stop, say what was blocked, and ask.
5. **Git is the user's.** Never stage, commit, push, pull, merge, rebase, or otherwise write to git history (`git add`, `git commit`, `git rm`, `git mv`, `git push`, …), not even on a task branch, and never discard uncommitted work (`git restore`, `git checkout -- <path>`, `git clean`, `git reset --hard`). Make changes in the working tree only, and keep a running list of the files you changed so the user can commit them. Read-only git commands (`status`, `diff`, `log`, `ls-files`, `check-ignore`) are fine. The guard hook (`.claude/hooks/guard.py`) enforces this.
6. **Zero-review constraint.** No solution may require the user to manually review, label, or edit data (imports, embeddings, publish builds). Data changes are produced by scripts from the master database, reproducibly.
7. **No unrequested paid API calls.** Nothing you run should trigger live Gemini calls unless the task explicitly says so (see "Query-time LLM trickle" below).

## What this is

Namecraft — the working name in code (the product has also been called "NameForge"; the final public name is undecided, so do not rename anything unless a task says so). A semantic name generator: users search by *meaning*, and the backend traverses a multilingual wordnet-style graph (synonyms, translations, sense relations) to surface names/words related to that meaning across ~21 languages, ranked and grouped by hop distance from the queried sense.

- `backend/` — FastAPI + SQLAlchemy + Postgres/pgvector. The real engine: sense graph traversal, root selection, vector search, data import pipeline.
- `frontend/` — Next.js 16 (App Router) + React 19 + Tailwind 4. Thin client over the backend API.

The project is being prepared for public hosting — see "Publishing work" below.

## Commands

### Backend

```bash
# from repo root, backend venv already exists at backend/.venv
source backend/.venv/bin/activate

# run the API (from backend/)
cd backend && uvicorn app.main:app --reload

# run the whole test suite (from repo root — pytest.ini lives here)
python -m pytest

# run a single test file / test
python -m pytest backend/tests/test_sense_display.py
python -m pytest backend/tests/test_sense_display.py::test_single_gloss_is_used_verbatim

# migrations (from backend/, where alembic.ini lives)
cd backend && alembic upgrade head
cd backend && alembic revision --autogenerate -m "..."
```

Tests use an in-memory SQLite DB (`backend/tests/conftest.py`), not Postgres — no running DB needed to run `pytest`. Run `pytest` before every commit.

### Database

```bash
docker-compose up -d   # pgvector/pgvector:pg17, exposed on localhost:5433
```

`backend/.env` (see `backend/.env.example`) sets `DATABASE_URL` (`postgresql+psycopg://...`). `backend/app/config.py` loads it via pydantic-settings. The local database is the **master**: ~21 GB, the source of truth for all reference data. Treat it accordingly (Safety rule 1).

For one-off SQL, use this pattern (long statements get a lock timeout):

```bash
docker exec -i name_generator_db psql -U postgres -d name_generator -c "
SET lock_timeout = '30s';
<statement>;"
```

### Frontend

```bash
cd frontend
npm run dev     # http://localhost:3000
npm run build
npm run lint
```

`npm run lint` and `npm run build` must pass before committing frontend changes.

### Production stack (rehearsal only on this machine)

`docker-compose.prod.yml` + `deploy/Caddyfile` + `.env.production` (template: `.env.production.example`). Always pass `-f docker-compose.prod.yml --env-file .env.production`: without `-f`, compose targets the dev stack, whose volume is the master database. Tear down rehearsal volumes by explicit name (`nameforge_*`), never with `down -v`. Running this stack and creating `.env.production` are the user's steps.

The guard hook at `.claude/hooks/guard.py` enforces the destructive-command, secret-file, and git-write rules; its tests are `backend/tests/test_claude_guard.py`.

## Architecture

### Configuration

All settings live in `app/config.py` (`Settings`, pydantic-settings, loaded from the environment and `backend/.env`). `APP_ENV` (`local` by default, or `production`) derives the security defaults: production turns API docs off, ignores `includeHidden`, enables rate limiting, turns on search limits and admission control, and fences the query-time LLM (unless `ALLOW_QUERY_TIME_LLM_IN_PRODUCTION=1`). Each can be overridden explicitly (`EXPOSE_API_DOCS`, `ALLOW_INCLUDE_HIDDEN`, `RATE_LIMIT_ENABLED`, `SEARCH_LIMITS_ENABLED`, `SEARCH_ADMISSION_ENABLED`).

Exception: `ROOT_LLM_*` stay in `services/root_llm.py`, because the harness fence depends on that module's globals.

The app is built by `create_app(settings)` in `app/main.py`; tests build production-configured apps with it. `GET /health` is liveness. `GET /ready` returns 200 only when the database answers and the startup pre-warm succeeded or was skipped (`PREWARM_ON_STARTUP=0`). A failed pre-warm is retried in the background with backoff, and `/ready` flips to 200 when a retry succeeds.

**Search limits and admission (`app/search_admission.py`, B5).** Both are off locally (settings unset), so the request path is exactly the pre-B5 code. In production:
- `SEARCH_MAX_WIDTH=3` and `SEARCH_MAX_DEPTH=3` (raise width to 5 as a settings change only). Width means *effective* width: `width`, or `expansionCount` when `width` is absent.
- `SEARCH_CONCURRENCY=2` searches run at once, at most `SEARCH_LARGE_CONCURRENCY=1` of them large (width × depth ≥ `SEARCH_LARGE_THRESHOLD=4`). The large lane must stay strictly smaller than the total, so a large search never blocks a normal one.
- Normal searches queue FIFO, up to `SEARCH_QUEUE_SIZE=4` waiting for up to `SEARCH_QUEUE_WAIT_SECONDS=30`. A second large search gets an immediate 503; there is no large-search queue.
- `SEARCH_TIMEOUT_SECONDS=300` is measured from admission, so queue time counts.

Order in `POST /explore-v2`: validate limits (422) → *result-cache seam (not built)* → admission (503) → record the sense selection → search → attach name cards → commit. Record and search share one session and stay in that order, because the search reads the statistics just recorded.

Keep-the-slot: the engine can't be cancelled, so a timed-out search returns 504 but keeps running. Its slot (and large permit) is released by the search thread when it finishes, never by the request. An admitted search runs in its own session, bound to the request session's engine (so test overrides apply), and an abandoned search rolls back instead of committing. That guarantee needs the query-time LLM fenced, as it is in production, because the trickle commits mid-search; `create_app` warns if admission is on while the trickle is live.

Error body for the 422 (limits), 503 (busy) and 504 (timeout) responses: `{"detail": {"code": str, "message": str, "retryAfter": int}}`, with `retryAfter` and a `Retry-After` header on 503 only. Codes: `search_limit_exceeded`, `search_queue_full`, `search_queue_timeout`, `large_search_busy`, `search_timeout`. FastAPI's own schema-validation 422 keeps `detail` as a list. `message` is written for the user; the frontend shows it as-is.

`GET /search-limits` returns `{maxWidth, maxDepth, largeThreshold, limitsEnforced, admissionEnforced}` (no database). It is the UI's single source for slider maxima and the large-search note.

Rate limiting (`app/middleware/rate_limit.py`) is in-process, assumes one worker, and is off locally so gate captures aren't throttled.

Error reports go to Sentry only when `SENTRY_DSN` is set, and `app/observability.py` scrubs `key=` values, because `root_llm.py` sends the Gemini key in the URL.

The browser always calls a relative `/api`: locally through Next's dev proxy to `127.0.0.1:8000`, in production through Caddy.

### Domain model (`backend/app/models/`)

Two model files, split by role:
- `generated_name.py` — `Language`, `GeneratedName`, `NamePart`, `GenerationFlavorModel`: the curated/generated-name side.
- `semantic.py` — the sense graph: `Source`, `Lexeme`/`Word`, `Sense`, `WordSense`, `Concept`, `SenseRelation`, `SenseEmbedding` (pgvector), `SenseSynset`/ILI bridge rows, `SenseTranslation`, `RootLlmAttempt`, plus the stats/event tables. This is the larger, load-bearing schema. **Any schema change here is a stop-and-ask.**

Large columns that the request path never reads: `lexemes.raw_entry` and `senses.raw_sense` (full import JSON, ~3.9 GB) and `sense_embeddings.embedded_text`. They're used only by importers and backfills. Don't add request-path reads of them — the production copy will have them emptied (Stage 2).

### Request flow

`app/api/routes/*` are thin — they call into `app/services/*`, which holds essentially all logic:

- `POST /generate` → `services/generated_names.py` — simple curated-name search.
- `POST /explore-v2` → the core semantic engine, branching on whether `languageCodes` is present:
  - **Legacy single-tree path** (no `languageCodes`) → `services/multi_hop_expansion.py`. Explicitly commented as "BYTE-IDENTICAL, guarded by `diff_reference.py`" — do not change its output shape casually.
  - **Parallel multilingual path** (`languageCodes` present) → `services/parallel_expansion.py`, which builds one traversal tree per requested language, then interleaves them (root band first, then round-robin) for display.
- `GET /languages` → `services/language_directory.py` (rewritten in Phase C for performance — see below).
- `GET /senses/lookup` → accepts `includeHidden`; production must not honor it (Stage 3c).
- `GET /health` → `routes/health.py`; the production health check.

### Tables written by live traffic

Ordinary searches write `sense_selection_stats` / `sense_selection_events` and `word_search_stats` / `word_search_events`. `SenseSelectionStat` feeds ranking, which is why gate references drift after ordinary API use. In the publishing design these tables form the "live" data zone that publishing never replaces (Stage 2).

### Query-time LLM trickle

With `ROOT_LLM_QUERY_TIME=1` (the local default), root selection can call Gemini at request time. The trickle writes to **reference** tables: `root_llm_attempts` (a resolve-once ledger that root selection *reads* at query time) and `sense_translations` (rows under source `llm-root`). Consequences:
- Every harness or probe in `scripts/eval/` and `scripts/prune/` that can reach `parallel_expand()` must fence the trickle. `tests/test_llm_fence.py` enforces this by grep, with a reasoned exemption list. New scripts follow the same rule; scripts that talk to a separately running server can't fence and go on the exemption list with a reason.
- In production the trickle is **off** (`ROOT_LLM_QUERY_TIME=0`, no key present), because its writes would land in tables that publishing replaces. Re-enabling it in production needs its own design; don't treat it as a flag flip.

### Root selection: the multilingual entry point

Before a language's tree can be expanded, it needs a *root* — the equivalent word in that language for the queried English sense. This is its own subsystem, `services/root_selection.py`, described in full in `notes/MULTILINGUAL_EXPANSION_MODEL.md`. It's a six-rung provenance ladder spanning two files, evaluated in this order:

```
corroborated → primary → ili → llm → pivoted_root → fallback (vector NN)
```

Five rungs are assigned in `root_selection.py`: corroborated, primary, ili, llm (reads persisted LLM links only), and the vector fallback, which is exposed separately as `vector_fallback_root`. `pivoted_root` is assigned by `_pivot_root_rescue` in `services/parallel_expansion.py`, which calls root selection with `include_vector_fallback=False`, so the rescue runs after llm and before fallback for pivot-eligible languages. Corroborated and primary share one evidence source (translation links), which is why there are six rungs but five sources below.

Each rung is a different evidence source (translation link, shared interlingual index, LLM-proposed + DB-validated translation, a pivot through Russian, or embedding nearest-neighbor as last resort). Fallback-rung roots are the least trustworthy — a wrong root poisons its entire tree — and are marked with their rung in the API response (`rootRung`) so a bad root is diagnosable from the output. Per-language-pair similarity floors for the fallback and pivot-rescue rungs are hardcoded calibration constants at the top of `root_selection.py`, derived from `scripts/eval/root_link_calibration.py` — don't hand-tune them without re-running that calibration.

**Naming gotcha:** "root" is overloaded. `root_selection.py` / `RootLlmAttempt` / `rootRung` refer to this per-language tree-root concept. A now-deleted "pink-card" `Root`/`RootMeaning` model family (a different, unrelated feature) was fully removed in the Phase A cleanup (see `notes/CLEANUP_AND_TWEAKS_ROADMAP.MD`) — if you see references to it in old notes, it no longer exists in code.

### Vector search and embeddings

`services/vector_scope.py` / `vector_sense_search.py` wrap pgvector HNSW queries over `SenseEmbedding` (~1.55M rows; the HNSW index is ~6 GB). `embedding_provider.py` wraps the embedding model (`intfloat/multilingual-e5-base`; cosine similarity is only meaningful *within* one language pair — there's a constant anisotropy offset across pairs, see comments in `root_selection.py`). `embed_query` is LRU-cached (safe — deterministic inference, results copied on return) since it's the largest single cost in a cold search per `phase_timing.py` measurements.

**Production runs on CPU.** Development uses MPS; the production target is a 2-core ARM CPU. Measured locally, the ~290 single `embed_query` calls of a 21-language search cost ~9 s on 2 CPU threads versus ~0.64 s batched. Any embedding change must consider CPU behaviour. `scripts/eval/batch_embed_identity.py` (roadmap C4c) already tests whether batched encodes are bitwise-identical to single encodes — read its recorded outcome in `notes/` before any batching work. Each uvicorn worker loads its own model copy, so worker count is a RAM decision.

### Data pipeline (import-once, not request-path)

`app/importers/` and `app/extractors/` load the sense graph from external sources (Kaikki Wiktionary dumps, Open English WordNet / OMW). `app/review/` and `app/review_ui/` (Streamlit apps — `review_app.py`, `sense_admin_app.py`) are manual-review tooling for the imported/curated data, run standalone (`streamlit run app/review_ui/review_app.py`), not part of the API. `app/seed.py` seeds dev data. None of this runs on the request path, and none of it ships in the production image.

### Byte-identity regression harness — read before touching engine code

This project's standing convention (see `notes/CLEANUP_AND_TWEAKS_ROADMAP.MD` appendix and `notes/MULTILINGUAL_EXPANSION_MODEL.md`) is: **measure before fixing, and verify semantic-engine changes don't alter output** using scripted diff tools in `backend/scripts/eval/`, not just `pytest`:

- `capture_engine_reference.py` / `capture_api_current.py` snapshot expansion output to `engine_reference.json` / `api_current.json`; `diff_reference.py` diffs them (word-sequence identity) — covers the **legacy single-tree path only**.
- `capture_parallel_reference.py` / `root_selection_diff.py` are the equivalent gates for the **parallel multilingual path** (`diff_reference.py` never exercises it, since `capture_api_current.py` issues English-only requests).
- `phase_timing.py` measures where search time goes; `http_concurrency.py` measures throughput against a running server (start that server with `ROOT_LLM_QUERY_TIME=0`).
- These are run after any change touching `multi_hop_expansion.py`, `parallel_expansion.py`, `root_selection.py`, `vector_scope.py`, `sense_reranker.py`, `dropdown_ranker.py`, `embedding_provider.py`, etc. — a 0-diff result is the bar, not a nice-to-have.
- `capture_api_current.py` has a side effect (writes `SenseSelectionStat`) that `capture_engine_reference.py` does not — re-baseline references before comparing if ordinary API usage happened in between.
- The established-names invariant is 106,398 rows; a change that alters it is a finding, not a side effect.

Additional rules for the publishing work:
- **Gates certify production only when run in production's configuration**: CPU device (`EMBEDDING_DEVICE=cpu TORCH_NUM_THREADS=2`), query-time LLM off, and ranking-stats behaviour matching production. Say which configuration a gate ran in when reporting it.
- **Record baselines before changing anything**, and report gate output as-is.
- **Any non-zero diff is the user's decision.** Never regenerate a reference file to make a gate pass without explicit approval in the current session.

If you're asked to change ranking/traversal/root-selection behavior, check `notes/MULTILINGUAL_EXPANSION_MODEL.md` and `notes/CLEANUP_AND_TWEAKS_ROADMAP.MD` first — both are living decision records with a lot of "we measured X, rejected Y because Z" history that will stop you from re-deriving already-rejected approaches.

## Data sources and licensing

- **Live sources (13):** awn4, kaikki, llm-root, lsg, odenet, oewn-2025, omw-arb, omw-cmn, omw-el, omw-es, omw-he, omw-ja, omw-pl. Relation provenances are constrained by `ck_sense_relations_provenance`.
- **Manifests:** Every live source needs `backend/data/source_manifests/<slug>/source_manifest.json`. The public attribution page is generated from these manifests (Stage 4), not written by hand. `kaikki` and `llm-root` manifests are still missing.
- **Share-alike:** Kaikki/Wiktionary, OdeNet, OMW Arabic and LSG are CC BY-SA. Nothing in the product — terms, UI copy, API behaviour — may restrict reuse of the displayed data or imply the data is exclusive. `llm-root` content is machine-generated (Gemini) from Wiktionary-derived text and must be labelled as such.
- **Excluded — never import or reintroduce:** ruwordnet (non-commercial license), latin-wordnet (license ambiguity), ChineseNames (declined; Chinese comes from Kaikki only). icewordnet has a manifest but is not imported; don't import it without an explicit decision.
- **New sources:** Adding any new data source requires an explicit user decision and a license check first.

## Frontend structure

- `src/app/generate/page.tsx` — main search page.
- `src/components/generator/GeneratorPrototype.tsx` — primary search/results UI (a client component).
- `src/lib/api/{generate,explore}.ts` — typed fetch wrappers for the backend endpoints. They currently hardcode `http://127.0.0.1:8000`; Stage 3b replaces this with one shared API-base helper.
- `src/features/generator/types.ts` — shared result/request types mirroring the backend Pydantic schemas (`app/schemas/generate.py`, `app/schemas/explore_v2.py`) — keep them in sync manually, there's no codegen.

**Next.js 16 has breaking changes** — APIs, conventions, and file structure may differ from your training data. Read the relevant guide in `frontend/node_modules/next/dist/docs/` before writing Next.js code (metadata, sitemap, robots, routing), and heed deprecation notices.

The site is used mostly on phones (baby-name browsing), and searches can take seconds on production CPU. Design mobile-first, with honest loading and "busy" states.

## Publishing work

The plan lives in `notes/PUBLISHING_ROADMAP.md` (Stages 0–7), sequenced into Manual and Claude Code sections in `notes/publishing_roadmap_breakdowns/PARTITION.md`. Tasks arrive as a handoff for one Claude Code section. Read that section and the roadmap Stage parts it names before planning, and do only that section.

**Stage labels:**
- **Claude Code:** implement it.
- **Split:** implement and run what the brief says; the user reads gate results and makes the calls.
- **You (the user):** do not execute. You may draft scripts or docs for these stages only when a brief asks.

**Workflow:**
- Plan first, in plan mode. The plan lists the files to touch, the tests and gates to run, and the stop-and-ask points it expects to hit. Wait for approval before implementing.
- Work happens directly on `main`. The user makes every commit and push; you never do.
- Log every finding in the `# Findings Log` section at the bottom of `notes/PUBLISHING_ROADMAP.md`, in the same format as the Findings Log sections of the previous `notes/*_ROADMAP*.md` files. Append only; never edit existing entries or the roadmap's Stages. Log the real output of any test you run, not a verdict.
- Stay in scope: no "while I'm in here" changes. Note them for the user instead.
- Approved plans are often run in auto mode. A handoff may pre-approve specific stop-and-ask items by name. If you reach a stop-and-ask condition the handoff does not name, log it in the Findings Log and end the section there. Do not work around it or continue past it.

**Stop and ask before:**
- any schema change in `app/models/semantic.py`, or a migration touching reference tables;
- changing `embed_query` caching, `build_query_text_from_selected_senses`, or root-selection calibration constants;
- accepting any gate diff;
- adding a dependency the brief doesn't name;
- writing license or attribution wording the manifests don't support;
- anything that touches production (Safety rule 2).

**Production target** (so decisions made in different stages converge):
- `docker-compose.prod.yml`: Caddy reverse proxy (`/` → frontend, `/api/*` → backend, automatic HTTPS), a CPU-only backend image with the model baked in (`HF_HUB_OFFLINE=1`), and Postgres on the internal network only, with no published port.
- Images are built for both `linux/arm64` (free-tier host) and `linux/amd64` (future paid host).
- Postgres schema `public` holds reference data and is replaced wholesale on each publish. Schema `live` holds live-traffic data (and later accounts) and is never touched by publishing.
- The app connects as a least-privilege role: read-only on `public`, writes only to `live`.
- Production defaults: `ROOT_LLM_QUERY_TIME=0`, ranking-stats writes off, API docs off, `includeHidden` ignored, one uvicorn worker with an explicit thread count, rate limits on expensive endpoints.
- No monetization at launch. Ads and user accounts may come later, so don't design anything that blocks them.

**Publishing status** (the user updates this as stages merge):

| Stage | Status |
|---|---|
| 0 Repo prep | done (Breakdown A) |
| 1 CPU readiness | not started |
| 2 Production data build | not started |
| 3 Configuration and hardening | done (Breakdown A) |
| 4 Name, legal pages, UI | not started |
| 5 Findability | not started |
| 6 Hosting and first publish | not started (user) |
| 7 Launch verification | not started |

## Notes directory

`notes/*.md` are living design/decision documents, not historical archives — check them before making non-trivial changes to search ranking, root selection, multilingual traversal, or the search UI, since they record prior measurements and explicitly rejected approaches. `notes/CLEANUP_AND_TWEAKS_ROADMAP.MD` tracks cleanup phases (A–D) against `git log`; `notes/PUBLISHING_ROADMAP.md` is the current publishing plan.

`notes/` is gitignored: it exists only on this machine, so don't assume CI can see it. Runbooks the roadmap mentions (`notes/publish-runbook.md`, `notes/move-host-runbook.md`, `notes/operations.md`) belong there too.