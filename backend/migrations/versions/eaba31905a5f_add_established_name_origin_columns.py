"""add established name origin columns

Revision ID: eaba31905a5f
Revises: 747973b1b940
Create Date: 2026-08-25 19:11:26.600428

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eaba31905a5f'
down_revision: Union[str, Sequence[str], None] = '747973b1b940'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "established_names",
        sa.Column("origin_language_name", sa.String(length=80),
                  nullable=True),
    )
    op.add_column(
        "established_names",
        sa.Column("origin_shape", sa.String(length=12), nullable=True),
    )
    op.add_column(
        "established_names",
        sa.Column("language_header_warning", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_established_names_origin_shape",
        "established_names",
        "origin_shape IS NULL OR origin_shape IN ('from', 'rendering')",
    )
    # Blank-over-wrong, in the schema: a shape with no language names
    # nothing, and a language with no shape cannot be rendered honestly --
    # "from Ukrainian" and "Ukrainian rendering" are different claims.
    # Same reasoning as ck_established_names_meaning_pair.
    op.create_check_constraint(
        "ck_established_names_origin_pair",
        "established_names",
        "(origin_language_name IS NULL AND origin_shape IS NULL) OR "
        "(origin_language_name IS NOT NULL AND origin_shape IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_established_names_origin_pair",
                       "established_names", type_="check")
    op.drop_constraint("ck_established_names_origin_shape",
                       "established_names", type_="check")
    op.drop_column("established_names", "language_header_warning")
    op.drop_column("established_names", "origin_shape")
    op.drop_column("established_names", "origin_language_name")
