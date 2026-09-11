"""Fusions manuelles reversibles.

Fusionner deux fiches detruit l'une des deux. Sans garder ce qu'elle contenait,
« annuler » ne pourrait que retirer la regle apprise en laissant le referentiel
fusionne jusqu'au depot du lendemain : l'utilisateur cliquerait et ne verrait
rien changer. Cette table garde l'instantane de la fiche absorbee et les lignes
du magasin deplacees, ce qui suffit a tout remettre en place tout de suite.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "product_merges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("survivor_id", sa.String(length=36), nullable=False),
        sa.Column("survivor_label", sa.String(length=512), nullable=False),
        sa.Column("absorbed_label", sa.String(length=512), nullable=False),
        sa.Column("absorbed", JSONType, nullable=False),
        sa.Column("moved_row_ids", JSONType, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undone_by", sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("product_merges", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_product_merges_store_id"), ["store_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_product_merges_survivor_id"), ["survivor_id"], unique=False
        )
        batch_op.create_index("ix_merges_store", ["store_id", "undone_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("product_merges", schema=None) as batch_op:
        batch_op.drop_index("ix_merges_store")
        batch_op.drop_index(batch_op.f("ix_product_merges_survivor_id"))
        batch_op.drop_index(batch_op.f("ix_product_merges_store_id"))

    op.drop_table("product_merges")
