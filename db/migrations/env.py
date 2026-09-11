"""Contexte Alembic.

L'URL de la base vient de la configuration applicative, jamais de alembic.ini :
elle contient le mot de passe postgres, qui n'a rien a faire dans le depot.

Le moteur applicatif est asynchrone ; Alembic ne l'est pas. On ouvre donc une
connexion async et on lui confie la migration synchrone via `run_sync`.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from db.models import Base
from pipeline.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", Settings().database_url)

target_metadata = Base.metadata


def _configure(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # sqlite ne sait pas modifier une colonne en place : sans le mode batch,
        # toute migration autre qu'un ajout echouerait sur la base de test.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(lambda sync_conn: _configure(sync_conn))
        await connection.run_sync(lambda _: context.run_migrations())
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif (connection := config.attributes.get("connection")) is not None:
    # Appel depuis l'application : la connexion est deja ouverte, et nous
    # sommes deja dans une boucle asyncio. En creer une seconde ici leverait
    # « asyncio.run() cannot be called from a running event loop ».
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()
else:
    # Appel depuis la ligne de commande `alembic`.
    asyncio.run(run_migrations_online())
