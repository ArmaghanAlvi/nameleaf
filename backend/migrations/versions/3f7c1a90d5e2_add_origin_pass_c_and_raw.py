"""add pass_c_raw, origin_raw, attempt_count and the disagreed status

Revision ID: 3f7c1a90d5e2
Revises: 9a2e6b41c8f0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3f7c1a90d5e2"
down_revision: Union[str, Sequence[str], None] = "9a2e6b41c8f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Third pass. A and B are the same question re-ordered; C runs only over
    # rows they disagreed on, so it needs its own slot -- overwriting B
    # would destroy the evidence that the disagreement happened at all.
    op.add_column("name_origin_attempts",
                  sa.Column("pass_c_raw", sa.String(length=80), nullable=True))

    # The badge string, which is NOT the same thing as `origin`. `origin`
    # is the backend marker that drives grouping and filtering ('other');
    # `origin_raw` is what the card shows ('Turkish'). Deriving the badge
    # from pass_a_raw instead would break the moment pass C is the pass
    # that resolved the row.
    op.add_column("name_origin_attempts",
                  sa.Column("origin_raw", sa.String(length=80), nullable=True))

    # Retry ceiling. Transport and omission failures are always re-sendable
    # by policy, so without a ceiling a row the model systematically
    # refuses gets retried every session forever against a daily cap that
    # is already recorded as fragile (§22.13).
    op.add_column("name_origin_attempts",
                  sa.Column("attempt_count", sa.SmallInteger(),
                            server_default="0", nullable=False))

    # 'disagreed' is a real state, not an absence: it means both passes
    # succeeded and contradicted each other, and the row is owed a third
    # call. Deriving it from (pass_a_raw <> pass_b_raw AND pass_c_raw IS
    # NULL) would work, but a ledger that cannot say what state a row is in
    # is the thing that bites three sessions later.
    op.drop_constraint("ck_name_origin_attempts_status",
                       "name_origin_attempts", type_="check")
    op.create_check_constraint(
        "ck_name_origin_attempts_status", "name_origin_attempts",
        "status IN ('resolved', 'unknown', 'error', 'disagreed')")


def downgrade() -> None:
    # Rows in the new state have no representation in the old constraint.
    # Demote them to 'error' -- they are unanswered and re-sendable, which
    # is what 'error' already means.
    op.execute("UPDATE name_origin_attempts SET status = 'error' "
               "WHERE status = 'disagreed'")
    op.drop_constraint("ck_name_origin_attempts_status",
                       "name_origin_attempts", type_="check")
    op.create_check_constraint(
        "ck_name_origin_attempts_status", "name_origin_attempts",
        "status IN ('resolved', 'unknown', 'error')")
    op.drop_column("name_origin_attempts", "attempt_count")
    op.drop_column("name_origin_attempts", "origin_raw")
    op.drop_column("name_origin_attempts", "pass_c_raw")