"""Connexion a la base. Une seule fabrique de sessions pour toute l'application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Base
from pipeline.config import Settings
from pipeline.logging import get_logger

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parent.parent

# La revision qui decrit la base telle qu'elle existait avant Alembic.
BASELINE_REVISION = "0001"

_settings = Settings()

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # 2 vCPU partages avec deux autres services : une pool large ne servirait
    # qu'a saturer postgres sans rien accelerer.
    pool_size=5,
    max_overflow=5,
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


async def create_all() -> None:
    """Cree le schema d'un coup. Reserve aux tests.

    En production, c'est `migrate()` qui fait foi : `create_all` sait creer une
    table absente mais pas ajouter une colonne a une table qui existe deja. Ce
    silence a deja coute un 500 en production (`products.url_checked`).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", _settings.database_url)
    return config


def _upgrade(connection: Any) -> None:
    """Amene le schema a jour, en adoptant au passage une base preexistante.

    Alembic refuse de migrer une base qu'il ne connait pas. Or celle de
    production existait avant lui : ses tables sont la, sa table de version
    non. On la marque alors comme etant a la revision de reference, qui decrit
    exactement ce qu'elle contient, puis on applique les revisions suivantes.

    Sans cette adoption, introduire Alembic obligerait a recreer la base, donc
    a perdre le referentiel.
    """
    config = _alembic_config()
    config.attributes["connection"] = connection

    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables and "products" in tables:
        log.info("db.adopting_existing_schema", baseline=BASELINE_REVISION)
        command.stamp(config, BASELINE_REVISION)

    command.upgrade(config, "head")


async def migrate() -> None:
    """Applique les migrations en attente. Appele au demarrage de l'API."""
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
    log.info("db.schema_ready")
