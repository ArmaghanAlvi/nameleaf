"""add lexemes romanization

Revision ID: fcd379f28e51
Revises: a1c7f3e94b28
Create Date: 2026-08-18 15:34:38.046752

Latin-script rendering of Lexeme.lemma, for the ten non-Latn-script languages
(ar, el, fa, he, hi, ja, ko, ru, sa, zh). Purely additive: no engine code
reads this column, so diff_reference.py / capture_parallel_reference.py must
show zero movement across this migration. Any movement means the migration
touched something it should not have.

NULL means "no trustworthy value available" and renders as nothing. Same
convention as languages.display_order (e78656f51d30) and
languages.has_wordnet_edges (a1c7f3e94b28): the column is behaviourally inert
until values are set, so migration and backfill land independently and in
either order.

No index. This column is only ever read as part of an already-fetched Lexeme
row on the display path -- never filtered, sorted, or joined on. An index
would cost write time on 1.68M rows and buy nothing.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fcd379f28e51'
down_revision: Union[str, Sequence[str], None] = 'a1c7f3e94b28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    op.add_column(
        "lexemes",
        sa.Column("romanization", sa.String(length=400), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lexemes", "romanization")