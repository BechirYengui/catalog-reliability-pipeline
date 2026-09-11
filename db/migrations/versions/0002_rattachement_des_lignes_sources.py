"""Rattachement des lignes sources, et identite stable des fiches produit.

Deux changements qui n'en font qu'un.

`product_source_rows` garde, pour chacune des 10 000 lignes du fichier, la
fiche qui la represente. Fusionner sans cela reviendrait a couper le lien avec
le systeme du magasin : sa caisse continue de parler avec SES identifiants.

`products.product_key` donne a une fiche une identite stable d'un depot a
l'autre. Le referentiel etait auparavant efface puis reinsere a chaque depot,
donc chaque fiche recevait un nouvel identifiant chaque matin. Un rattachement
qui pointe vers une fiche disparue ne vaut rien : les deux changements doivent
arriver ensemble.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_source_rows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("store_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("row_id", sa.String(length=64), nullable=False),
        sa.Column("source_label", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("product_source_rows", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_product_source_rows_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_product_source_rows_store_id"), ["store_id"], unique=False
        )
        batch_op.create_index("ix_source_rows_product", ["product_id"], unique=False)
        batch_op.create_index("ix_source_rows_store_row", ["store_id", "row_id"], unique=False)

    # `server_default` est indispensable : la table de production contient deja
    # des lignes, et une colonne NOT NULL sans valeur par defaut les rendrait
    # invalides au moment meme de l'ajout. C'est exactement le genre de detail
    # qui transforme une migration en incident.
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("product_key", sa.String(length=512), nullable=False, server_default="")
        )

    # Les fiches existantes recoivent leur cle : le libelle normalise, qui est
    # deja la cle de regroupement utilisee par l'etape de deduplication.
    op.execute(sa.text("UPDATE products SET product_key = label_normalized WHERE product_key = ''"))

    # Deux fiches d'un meme magasin peuvent porter le meme libelle normalise :
    # ce sont les doublons que l'ancienne accumulation avait laisses, et la
    # contrainte d'unicite les refuserait. On garde la plus grosse, celle qui
    # represente le plus de lignes du magasin.
    op.execute(
        sa.text(
            """
            DELETE FROM products WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY store_id, product_key
                        ORDER BY source_rows DESC, id
                    ) AS rang
                    FROM products
                ) classement WHERE rang > 1
            )
            """
        )
    )

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_products_product_key"), ["product_key"], unique=False)
        batch_op.create_unique_constraint("uq_products_store_key", ["store_id", "product_key"])


def downgrade() -> None:
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_constraint("uq_products_store_key", type_="unique")
        batch_op.drop_index(batch_op.f("ix_products_product_key"))
        batch_op.drop_column("product_key")

    with op.batch_alter_table("product_source_rows", schema=None) as batch_op:
        batch_op.drop_index("ix_source_rows_store_row")
        batch_op.drop_index("ix_source_rows_product")
        batch_op.drop_index(batch_op.f("ix_product_source_rows_store_id"))
        batch_op.drop_index(batch_op.f("ix_product_source_rows_run_id"))

    op.drop_table("product_source_rows")
