"""add root_llm_attempts error_detail

Revision ID: 8c18da62fe0f
Revises: eaba31905a5f
Create Date: 2026-08-27 13:40:02.908046

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c18da62fe0f'
down_revision: Union[str, Sequence[str], None] = 'eaba31905a5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "root_llm_attempts",
        sa.Column("error_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("root_llm_attempts", "error_detail")
