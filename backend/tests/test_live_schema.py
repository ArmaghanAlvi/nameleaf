"""C2: the two-zone schema.

The four usage tables are declared in schema `live` (publishing replaces
`public` and never touches `live`); root_llm_attempts stays in `public`
(Findings A-0.1). There are no database-level foreign keys from `live` into
`public`, so a statistics row can outlive its sense -- every reader must
tolerate that. The migration itself is rendered to SQL offline (no database)
and checked statement by statement.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.generated_name import Language
from app.models.semantic import (
    Lexeme,
    RootLlmAttempt,
    Sense,
    SenseSelectionEvent,
    SenseSelectionStat,
    Source,
    WordSearchEvent,
    WordSearchStat,
)
from app.services.sense_lookup import fetch_sense_candidates

LIVE_MODELS = (SenseSelectionStat, SenseSelectionEvent, WordSearchStat,
               WordSearchEvent)
MIGRATION = (Path(__file__).resolve().parent.parent / "migrations" / "versions"
             / "a7c3e91f0b52_move_usage_tables_to_live_schema.py")


def test_usage_tables_are_in_schema_live():
    for model in LIVE_MODELS:
        assert model.__table__.schema == "live", model.__name__


def test_root_llm_attempts_stays_in_public():
    assert RootLlmAttempt.__table__.schema is None


def test_no_foreign_keys_from_live_into_public():
    for model in LIVE_MODELS:
        assert not model.__table__.foreign_keys, model.__name__


def test_relationships_still_join(db):
    src = Source(name="t", source_type="t")
    en = Language(name="English", code="en", script="Latn")
    db.add_all([src, en])
    db.flush()
    lx = Lexeme(language_id=en.id, lemma="brave", normalized_lemma="brave",
                part_of_speech="adj", source_id=src.id,
                source_entry_id="e-brave", raw_entry={})
    db.add(lx)
    db.flush()
    s = Sense(lexeme_id=lx.id, source_id=src.id, source_locator="e-brave:0",
              sense_index=0, definition="showing courage")
    db.add(s)
    db.flush()
    db.add(SenseSelectionStat(sense_id=s.id, selection_count=3))
    db.add(WordSearchStat(language_id=en.id, normalized_lemma="brave",
                          search_count=1))
    db.flush()
    db.expire_all()
    assert db.get(Sense, s.id).selection_stat.selection_count == 3
    assert db.get(SenseSelectionStat, s.id).sense.id == s.id
    assert db.query(WordSearchStat).one().language.code == "en"


def test_orphan_statistics_row_does_not_break_the_dropdown(db):
    src = Source(name="t", source_type="t")
    en = Language(name="English", code="en", script="Latn")
    db.add_all([src, en])
    db.flush()
    lx = Lexeme(language_id=en.id, lemma="light", normalized_lemma="light",
                part_of_speech="noun", source_id=src.id,
                source_entry_id="e-light", raw_entry={})
    db.add(lx)
    db.flush()
    s = Sense(lexeme_id=lx.id, source_id=src.id, source_locator="e-light:0",
              sense_index=0, definition="electromagnetic radiation")
    db.add(s)
    db.flush()
    # A statistics row whose sense is gone (e.g. dropped by a later publish).
    db.add(SenseSelectionStat(sense_id=999_999, selection_count=50))
    db.add(SenseSelectionEvent(sense_id=999_999, query_text="ghost"))
    db.flush()

    candidates = fetch_sense_candidates(db, query="light", language_code="en")
    assert [c.sense.id for c in candidates] == [s.id]
    assert candidates[0].selection_count == 0


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_a7c3e91f0b52", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _render(fn_name: str) -> str:
    """Render upgrade()/downgrade() to Postgres SQL, offline -- no database."""
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf},
    )
    module = _load_migration()
    with Operations.context(ctx):
        getattr(module, fn_name)()
    return buf.getvalue()


def test_migration_chains_onto_the_current_head():
    module = _load_migration()
    assert module.revision == "a7c3e91f0b52"
    assert module.down_revision == "3f7c1a90d5e2"


def test_migration_upgrade_sql():
    sql = _render("upgrade")
    assert "CREATE SCHEMA IF NOT EXISTS live" in sql
    for fk in ("sense_selection_stats_sense_id_fkey",
               "sense_selection_events_sense_id_fkey",
               "word_search_stats_language_id_fkey",
               "word_search_events_language_id_fkey"):
        assert f"DROP CONSTRAINT {fk}" in sql
    for table in ("sense_selection_stats", "sense_selection_events",
                  "word_search_stats", "word_search_events"):
        assert f"ALTER TABLE public.{table} SET SCHEMA live" in sql
    assert "root_llm_attempts" not in sql
    assert "alembic_version" not in sql


def test_migration_downgrade_sql():
    sql = _render("downgrade")
    for table in ("sense_selection_stats", "sense_selection_events",
                  "word_search_stats", "word_search_events"):
        assert f"ALTER TABLE live.{table} SET SCHEMA public" in sql
    assert ("ADD CONSTRAINT sense_selection_stats_sense_id_fkey FOREIGN KEY"
            "(sense_id) REFERENCES public.senses (id) ON DELETE CASCADE") in sql
    assert ("ADD CONSTRAINT word_search_events_language_id_fkey FOREIGN KEY"
            "(language_id) REFERENCES public.languages (id)") in sql
    assert sql.rstrip().rstrip(";").endswith("DROP SCHEMA live")
