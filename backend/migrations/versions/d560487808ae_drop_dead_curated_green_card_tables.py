"""drop dead curated green card tables

Revision ID: d560487808ae
Revises: fcd379f28e51
Create Date: 2026-08-21 17:01:00.550632

Breakdown B Step 2. The curated EstablishedName/NameMeaning/NameRelationship
trio is dead architecture: it hangs off `Concept`, is unreachable from the
live Sense/Lexeme world, and reviving it would reintroduce the review gate
this project abandoned (EXPANSION_FEATURE_COMPLETE_RECORD.md section 4).

It is DROPPED rather than commented because the roadmap's Stage 2a wants the
name `established_names` for the new derived table. Same shape as Phase A's
16ead11a224c.

Original DDL: 847e777b2da9_add_semantic_search_tables.py
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd560487808ae'
down_revision: Union[str, Sequence[str], None] = 'fcd379f28e51'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Children first: both hold FKs into established_names.
    op.drop_table("name_relationships")
    op.drop_table("name_meanings")
    op.drop_table("established_names")


def downgrade() -> None:
    op.create_table(
        "established_names",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("language_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("native_script", sa.String(length=200), nullable=True),
        sa.Column("transliteration", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["language_id"], ["languages.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "name_meanings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("established_name_id", sa.Integer(), nullable=False),
        sa.Column("concept_id", sa.Integer(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("native_form", sa.String(length=200), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["established_name_id"],
                                ["established_names.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "name_relationships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_name_id", sa.Integer(), nullable=False),
        sa.Column("target_name_id", sa.Integer(), nullable=False),
        sa.Column("relationship_type", sa.String(length=50), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_name_id"], ["established_names.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_name_id"], ["established_names.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )