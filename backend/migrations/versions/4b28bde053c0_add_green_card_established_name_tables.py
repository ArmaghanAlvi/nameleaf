"""add green card established name tables

Revision ID: 4b28bde053c0
Revises: d560487808ae
Create Date: 2026-08-21 17:05:22.500173


Breakdown B Step 4. The green-card skeleton: attested names, their meaning
tokens, their variant edges and their clusters.

Purely additive -- no engine code reads any of these tables, so
diff_reference.py must show zero movement across this migration. Any movement
means the migration touched something it should not have.

Table order is forced: established_names.cluster_id references
established_name_clusters, and established_name_clusters.head_name_id
references back. Clusters are created WITHOUT that FK and it is added at the
end, which is what use_alter=True on the model expresses.

Edges and clusters land EMPTY. They are Stage 5's to fill; creating them now
means Breakdown C needs no migration of its own.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b28bde053c0'
down_revision: Union[str, Sequence[str], None] = 'd560487808ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "established_name_clusters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name_type", sa.String(length=12), nullable=False),
        sa.Column("head_name_id", sa.Integer(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("is_cross_language_merged", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "name_type IN ('given', 'surname', 'patronymic')",
            name="ck_established_name_clusters_name_type",
        ),
    )
    op.create_index(
        "ix_established_name_clusters_type",
        "established_name_clusters", ["name_type"],
    )

    op.create_table(
        "established_names",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("language_id", sa.Integer(), nullable=False),
        sa.Column("lemma", sa.String(length=300), nullable=False),
        sa.Column("normalized_lemma", sa.String(length=300), nullable=False),
        sa.Column("romanization", sa.String(length=400), nullable=True),
        sa.Column("name_type", sa.String(length=12), nullable=False),
        sa.Column("gender", sa.String(length=1), nullable=False),
        sa.Column("is_also_surname", sa.Boolean(), nullable=False),
        sa.Column("source_lexeme_id", sa.Integer(), nullable=False),
        sa.Column("source_sense_id", sa.Integer(), nullable=False),
        sa.Column("meaning_text", sa.Text(), nullable=True),
        sa.Column("meaning_channel", sa.String(length=20), nullable=True),
        sa.Column("equiv_en_target", sa.String(length=120), nullable=True),
        sa.Column("homograph_lexeme_id", sa.Integer(), nullable=True),
        sa.Column("cluster_id", sa.Integer(), nullable=True),
        sa.Column("popularity_rank", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["language_id"], ["languages.id"]),
        sa.ForeignKeyConstraint(["source_lexeme_id"], ["lexemes.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_sense_id"], ["senses.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["homograph_lexeme_id"], ["lexemes.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cluster_id"],
                                ["established_name_clusters.id"],
                                ondelete="SET NULL"),
        sa.UniqueConstraint("language_id", "normalized_lemma", "name_type",
                            name="uq_established_names_key"),
        sa.CheckConstraint(
            "name_type IN ('given', 'surname', 'patronymic')",
            name="ck_established_names_name_type",
        ),
        sa.CheckConstraint(
            "gender IN ('m', 'f', 'x', 'u')",
            name="ck_established_names_gender",
        ),
        sa.CheckConstraint(
            "meaning_channel IS NULL OR meaning_channel IN "
            "('GLOSS_MEANING', 'ETYM_MARKER', 'ETYM_QUOTED', "
            "'HOMOGRAPH', 'EQUIV_PROPAGATED')",
            name="ck_established_names_meaning_channel",
        ),
        sa.CheckConstraint(
            "(meaning_text IS NULL AND meaning_channel IS NULL) OR "
            "(meaning_text IS NOT NULL AND meaning_channel IS NOT NULL)",
            name="ck_established_names_meaning_pair",
        ),
    )
    op.create_index("ix_established_names_homograph",
                    "established_names", ["homograph_lexeme_id"])
    op.create_index("ix_established_names_cluster",
                    "established_names", ["cluster_id"])
    op.create_index("ix_established_names_source_lexeme",
                    "established_names", ["source_lexeme_id"])

    op.create_table(
        "established_name_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("established_name_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=80), nullable=False),
        sa.Column("token_lexeme_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["established_name_id"],
                                ["established_names.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["token_lexeme_id"], ["lexemes.id"],
                                ondelete="SET NULL"),
        sa.UniqueConstraint("established_name_id", "token",
                            name="uq_established_name_tokens_pair"),
    )
    op.create_index("ix_established_name_tokens_token",
                    "established_name_tokens", ["token"])
    op.create_index("ix_established_name_tokens_lexeme",
                    "established_name_tokens", ["token_lexeme_id"])

    op.create_table(
        "established_name_edges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_name_id", sa.Integer(), nullable=False),
        sa.Column("target_name_id", sa.Integer(), nullable=False),
        sa.Column("relation_type", sa.String(length=20), nullable=False),
        sa.Column("is_cross_language", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["source_name_id"], ["established_names.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_name_id"], ["established_names.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("source_name_id", "target_name_id",
                            "relation_type",
                            name="uq_established_name_edges_edge"),
        sa.CheckConstraint(
            "relation_type IN ('VARIANT_OF', 'DIMINUTIVE_OF', 'FEM_EQUIV', "
            "'MASC_EQUIV', 'EQUIV_EN')",
            name="ck_established_name_edges_relation_type",
        ),
        sa.CheckConstraint(
            "source_name_id <> target_name_id",
            name="ck_established_name_edges_no_self_loop",
        ),
    )
    op.create_index("ix_established_name_edges_target",
                    "established_name_edges", ["target_name_id"])
    op.create_index("ix_established_name_edges_relation",
                    "established_name_edges", ["relation_type"])

    # Last: both tables now exist, so the back-reference can be added.
    op.create_foreign_key(
        "fk_established_name_clusters_head",
        "established_name_clusters", "established_names",
        ["head_name_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_established_name_clusters_head",
        "established_name_clusters", type_="foreignkey",
    )
    op.drop_table("established_name_edges")
    op.drop_table("established_name_tokens")
    op.drop_table("established_names")
    op.drop_table("established_name_clusters")