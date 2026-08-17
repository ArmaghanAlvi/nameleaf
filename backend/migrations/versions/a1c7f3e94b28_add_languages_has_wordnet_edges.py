"""add languages has_wordnet_edges

Revision ID: a1c7f3e94b28
Revises: 16ead11a224c
Create Date: 2026-08-17 19:55:00.000000

Pivot eligibility ("this language has ZERO wordnet synonym edges") was
recomputed on every process that served a search: _pivot_eligible_languages
probes one LIMIT 1 existence query per non-English language, and for the 14
languages that genuinely have no wordnet edges the negative cannot
short-circuit -- Postgres must touch every sense of that language to prove
absence. Measured at 1,184ms for a single language (sw, 12,819 senses,
nested-loop Index Only Scan) and ~17s cold for the full set, paid once per
uvicorn restart by whichever request arrived first. See
notes/LATENCY_INVESTIGATION.md finding F11.

This column persists the answer so it is read, not recomputed.

NULL means "not yet computed -- fall back to the live probe", exactly the
convention display_order (e78656f51d30) uses for its own backfill window: the
column is byte-identical to the old behavior until values are set, so the
migration and the backfill can land independently and in either order.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c7f3e94b28'
down_revision: Union[str, Sequence[str], None] = '16ead11a224c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "languages",
        sa.Column("has_wordnet_edges", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("languages", "has_wordnet_edges")
