"""add gloss_etym to NAME_ORIGIN_SOURCES

Revision ID: 9a2e6b41c8f0
Revises: 492c9602cac4
"""
from typing import Sequence, Union

from alembic import op

revision: str = "9a2e6b41c8f0"
down_revision: Union[str, Sequence[str], None] = "492c9602cac4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = ("category", "llm_native", "llm_foreign", "llm_twin",
       "llm_unknown", "llm_error", "gradient_exempt")
_NEW = _OLD + ("gloss_etym",)


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint("ck_established_names_origin_source",
                       "established_names", type_="check")
    op.create_check_constraint(
        "ck_established_names_origin_source", "established_names",
        f"origin_source IS NULL OR origin_source IN ({_in_list(_NEW)})")


def downgrade() -> None:
    # A row resolved by this channel has no representation in the old
    # constraint. Demote it to NULL -- pending -- rather than deleting the
    # row's display value silently; the next --pass origin will re-resolve
    # it through whichever tiers exist at that revision.
    op.execute("UPDATE established_names "
              "SET origin_source = NULL, display_origin_language = NULL "
              "WHERE origin_source = 'gloss_etym'")
    op.drop_constraint("ck_established_names_origin_source",
                       "established_names", type_="check")
    op.create_check_constraint(
        "ck_established_names_origin_source", "established_names",
        f"origin_source IS NULL OR origin_source IN ({_in_list(_OLD)})")