"""add name origin ledger, twins, and display columns

Revision ID: 492c9602cac4
Revises: 8c18da62fe0f
Create Date: 2026-08-31 15:11:09.897677

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '492c9602cac4'
down_revision: Union[str, Sequence[str], None] = '8c18da62fe0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- established_names: the display pair (Stage 18c) -------------------
    op.add_column(
        "established_names",
        sa.Column("display_origin_language", sa.String(length=80),
                  nullable=True),
    )
    op.add_column(
        "established_names",
        sa.Column("origin_source", sa.String(length=24), nullable=True),
    )
    op.create_check_constraint(
        "ck_established_names_origin_source",
        "established_names",
        "origin_source IS NULL OR origin_source IN "
        "('category', 'llm_native', 'llm_foreign', 'llm_twin', "
        "'llm_unknown', 'llm_error', 'gradient_exempt')",
    )
    # A displayed origin must name its provenance. No inverse constraint:
    # gradient_exempt / llm_unknown / llm_error are legal states WITH a
    # provenance and WITHOUT a display value.
    op.create_check_constraint(
        "ck_established_names_display_origin_pair",
        "established_names",
        "display_origin_language IS NULL OR origin_source IS NOT NULL",
    )

    # --- the ledger (Stage 18b) -------------------------------------------
    op.create_table(
        "name_origin_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("language_id", sa.Integer(),
                  sa.ForeignKey("languages.id"), nullable=False),
        sa.Column("normalized_lemma", sa.String(length=300), nullable=False),
        sa.Column("name_type", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("pass_a_raw", sa.String(length=80), nullable=True),
        sa.Column("pass_b_raw", sa.String(length=80), nullable=True),
        sa.Column("origin", sa.String(length=80), nullable=True),
        sa.Column("confidence", sa.String(length=8), nullable=True),
        sa.Column("is_coined", sa.Boolean(), nullable=True),
        sa.Column("in_vocabulary", sa.Boolean(), nullable=True),
        sa.Column("twin_language", sa.String(length=16), nullable=True),
        sa.Column("source_sense_id", sa.Integer(),
                  sa.ForeignKey("senses.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("language_id", "normalized_lemma", "name_type",
                            name="uq_name_origin_attempts_key"),
        sa.CheckConstraint(
            "status IN ('resolved', 'unknown', 'error')",
            name="ck_name_origin_attempts_status"),
        sa.CheckConstraint(
            "name_type IN ('given', 'surname', 'patronymic')",
            name="ck_name_origin_attempts_name_type"),
    )

    # --- the twin lookup (Stage 19a) --------------------------------------
    op.create_table(
        "name_origin_twins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("language_id", sa.Integer(),
                  sa.ForeignKey("languages.id"), nullable=False),
        sa.Column("normalized_lemma", sa.String(length=300), nullable=False),
        sa.Column("name_type", sa.String(length=12), nullable=False),
        sa.Column("twin_language_id", sa.Integer(),
                  sa.ForeignKey("languages.id"), nullable=False),
        sa.Column("twin_lemma", sa.String(length=300), nullable=False),
        sa.Column("match_key", sa.String(length=300), nullable=False),
        sa.UniqueConstraint("language_id", "normalized_lemma", "name_type",
                            "twin_language_id",
                            name="uq_name_origin_twins_key"),
        sa.CheckConstraint(
            "name_type IN ('given', 'surname', 'patronymic')",
            name="ck_name_origin_twins_name_type"),
    )


def downgrade() -> None:
    op.drop_table("name_origin_twins")
    op.drop_table("name_origin_attempts")
    op.drop_constraint("ck_established_names_display_origin_pair",
                       "established_names", type_="check")
    op.drop_constraint("ck_established_names_origin_source",
                       "established_names", type_="check")
    op.drop_column("established_names", "origin_source")
    op.drop_column("established_names", "display_origin_language")
