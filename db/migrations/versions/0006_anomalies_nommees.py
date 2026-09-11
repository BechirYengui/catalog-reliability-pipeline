"""Une anomalie dit desormais QUOI et SUR QUEL PRODUIT.

Le detail d'une anomalie etait le meme pour tout un code : cent lignes
« prefixe GS1 reserve a l'usage interne », sans le code fautif ni le nom du
produit. On savait qu'il y en avait 1 438, jamais lesquelles, et l'utilisateur
ne pouvait pas les retrouver dans son fichier.

Deux colonnes, avec `server_default` : la table de production porte deja des
lignes, et une colonne NOT NULL sans defaut les rendrait invalides au moment
meme de l'ajout.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("run_anomalies", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("value", sa.String(length=512), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("label", sa.String(length=512), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("run_anomalies", schema=None) as batch_op:
        batch_op.drop_column("label")
        batch_op.drop_column("value")
