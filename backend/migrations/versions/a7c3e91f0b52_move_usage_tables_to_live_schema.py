"""move usage tables to schema live (two-zone schema, publishing C2)

Publishing replaces schema `public` (reference data) wholesale and never
touches schema `live` (live-traffic data). This moves the four usage tables
into `live`:

    sense_selection_stats, sense_selection_events,
    word_search_stats, word_search_events

`root_llm_attempts` stays in `public`: it is the resolve-once ledger root
selection reads at query time, and ships with each publish (Findings A-0.1).

ALTER TABLE ... SET SCHEMA moves each table's indexes, constraints and owned
sequences with it:
  * primary keys: sense_selection_stats_pkey, sense_selection_events_pkey,
    word_search_stats_pkey, word_search_events_pkey;
  * uq_word_search_stats_language_lemma;
  * indexes ix_sense_selection_events_sense_id,
    ix_word_search_stats_language_lemma, ix_word_search_stats_count,
    ix_word_search_events_language_query;
  * sequences sense_selection_events_id_seq, word_search_stats_id_seq,
    word_search_events_id_seq (the nextval() defaults reference them by OID,
    so they keep working).

The four foreign keys from these tables into `public` are DROPPED, because a
database-level constraint from `live` into `public` would block, or be
silently dropped by, every publish that replaces `public`. The columns and
their indexes stay; the ORM keeps the relationships with explicit
primaryjoin/foreign() conditions, so metadata and the database agree.
Readers tolerate statistics rows whose sense no longer exists.

No view, function or trigger references these tables (checked on master,
C2). alembic_version stays in `public`.

DOWNGRADE moves the tables back and re-creates the foreign keys with their
original names and ON DELETE rules. Re-creating them FAILS if any
statistics row points at a sense (or language) that no longer exists -- after
a publish, that can be the case; delete such orphans first. `DROP SCHEMA
live` is RESTRICT, so it refuses if anything else has been put in `live`.

Revision ID: a7c3e91f0b52
Revises: 3f7c1a90d5e2
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e91f0b52"
down_revision: Union[str, Sequence[str], None] = "3f7c1a90d5e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "sense_selection_stats",
    "sense_selection_events",
    "word_search_stats",
    "word_search_events",
)

# (table, constraint, column, referred table, ON DELETE) -- as on master.
FOREIGN_KEYS = (
    ("sense_selection_stats", "sense_selection_stats_sense_id_fkey",
     "sense_id", "senses", "CASCADE"),
    ("sense_selection_events", "sense_selection_events_sense_id_fkey",
     "sense_id", "senses", "CASCADE"),
    ("word_search_stats", "word_search_stats_language_id_fkey",
     "language_id", "languages", None),
    ("word_search_events", "word_search_events_language_id_fkey",
     "language_id", "languages", None),
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA IF NOT EXISTS live")
    for table, name, _column, _referred, _ondelete in FOREIGN_KEYS:
        op.drop_constraint(name, table, type_="foreignkey", schema="public")
    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} SET SCHEMA live")


def downgrade() -> None:
    """Downgrade schema."""
    for table in TABLES:
        op.execute(f"ALTER TABLE live.{table} SET SCHEMA public")
    for table, name, column, referred, ondelete in FOREIGN_KEYS:
        op.create_foreign_key(
            name, table, referred, [column], ["id"],
            source_schema="public", referent_schema="public",
            ondelete=ondelete,
        )
    op.execute("DROP SCHEMA live")
