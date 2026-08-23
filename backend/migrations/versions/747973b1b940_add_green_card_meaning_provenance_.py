"""add green card meaning provenance columns

Revision ID: 747973b1b940
Revises: 4b28bde053c0
Create Date: 2026-08-22 14:08:27.577678

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '747973b1b940'
down_revision: Union[str, Sequence[str], None] = '4b28bde053c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "established_names",
        sa.Column("meaning_source_name_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "established_names",
        sa.Column("homograph_confidence", sa.String(length=16),
                  nullable=True),
    )
    op.create_foreign_key(
        "fk_established_names_meaning_source",
        "established_names", "established_names",
        ["meaning_source_name_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_established_names_meaning_source",
        "established_names", ["meaning_source_name_id"],
    )
    op.create_check_constraint(
        "ck_established_names_homograph_confidence",
        "established_names",
        "homograph_confidence IS NULL OR homograph_confidence IN "
        "('corroborated', 'spelling_only')",
    )
    op.create_check_constraint(
        "ck_established_names_meaning_source_channel",
        "established_names",
        "meaning_source_name_id IS NULL OR "
        "meaning_channel = 'EQUIV_PROPAGATED'",
    )
    op.create_check_constraint(
        "ck_established_names_no_self_propagation",
        "established_names",
        "meaning_source_name_id IS NULL OR meaning_source_name_id <> id",
    )


def downgrade() -> None:
    op.drop_constraint("ck_established_names_no_self_propagation",
                       "established_names", type_="check")
    op.drop_constraint("ck_established_names_meaning_source_channel",
                       "established_names", type_="check")
    op.drop_constraint("ck_established_names_homograph_confidence",
                       "established_names", type_="check")
    op.drop_index("ix_established_names_meaning_source",
                  table_name="established_names")
    op.drop_constraint("fk_established_names_meaning_source",
                       "established_names", type_="foreignkey")
    op.drop_column("established_names", "homograph_confidence")
    op.drop_column("established_names", "meaning_source_name_id")