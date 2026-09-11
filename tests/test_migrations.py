"""Les migrations doivent atteindre une base qui existe deja.

Ce test existe parce que `create_all()` a deja cause un 500 en production : il
cree une table absente mais reste muet sur une colonne ajoutee a une table
existante. La colonne manquait, l'API repondait 500, et rien dans le
demarrage ne l'avait signale.

Le scenario dangereux n'est donc pas « migrer une base vierge », que tout le
monde teste, mais « migrer la base qui porte les donnees ». C'est celui-la qui
est fige ici, avec sa contrainte : le referentiel doit survivre.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text


def _upgrade_to(connection, revision: str) -> None:  # type: ignore[no-untyped-def]
    """Amene la base a une revision precise, sur une connexion deja ouverte."""
    from alembic import command

    import db.session as session_mod

    config = session_mod._alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def _fresh_modules(database_url: str) -> tuple[object, object]:
    os.environ["DATABASE_URL"] = database_url
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]
    return importlib.import_module("db.session"), importlib.import_module("db.models")


@pytest.mark.asyncio
async def test_une_base_vierge_recoit_le_schema_complet(tmp_path: Path) -> None:
    path = tmp_path / "vierge.db"
    session_mod, _ = _fresh_modules(f"sqlite+aiosqlite:///{path}")

    await session_mod.migrate()  # type: ignore[attr-defined]

    tables = set(inspect(create_engine(f"sqlite:///{path}")).get_table_names())
    assert "product_source_rows" in tables
    assert "alembic_version" in tables


@pytest.mark.asyncio
async def test_une_base_preexistante_est_adoptee_sans_perdre_les_donnees(
    tmp_path: Path,
) -> None:
    """Le cas de la production : les tables sont la, la table de version non.

    Alembic refuserait de migrer une base qu'il ne connait pas. On verifie
    qu'elle est adoptee, migree, et que le referentiel est toujours debout.
    """
    path = tmp_path / "production.db"
    session_mod, _ = _fresh_modules(f"sqlite+aiosqlite:///{path}")

    # L'ancien schema, construit par la revision de reference elle-meme : plus
    # fidele que de le reconstituer a la main, et cela verifie au passage que
    # cette revision decrit bien la production.
    async with session_mod.engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.run_sync(_upgrade_to, "0001")

    sync = create_engine(f"sqlite:///{path}")
    with sync.begin() as conn:
        # Une base anterieure a Alembic n'a pas de table de version : c'est
        # exactement ce qui fait echouer une premiere migration.
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(
            text("INSERT INTO stores (id, name, created_at) VALUES ('m', 'M', '2026-01-01')")
        )
        for index, (label, rows) in enumerate([("WHISKY 70CL", 945), ("COLA 1L", 994)]):
            conn.execute(
                text(
                    "INSERT INTO products (id, store_id, label, label_normalized, url_ok,"
                    " url_checked, status, publishable, confidence, source_rows,"
                    " created_at, updated_at)"
                    f" VALUES ('p{index}', 'm', '{label}', '{label}', 0, 0, 'VALIDATED',"
                    f" 1, 1.0, {rows}, '2026-01-01', '2026-01-01')"
                )
            )

    before = set(inspect(sync).get_table_names())
    assert "alembic_version" not in before
    assert "product_source_rows" not in before

    await session_mod.migrate()  # type: ignore[attr-defined]

    inspector = inspect(create_engine(f"sqlite:///{path}"))
    assert "product_source_rows" in inspector.get_table_names()
    assert "product_key" in {column["name"] for column in inspector.get_columns("products")}

    with create_engine(f"sqlite:///{path}").begin() as conn:
        rows = conn.execute(text("SELECT label, product_key, source_rows FROM products")).all()

    # Le referentiel a survecu, et chaque fiche a recu son identite.
    assert {row[0] for row in rows} == {"WHISKY 70CL", "COLA 1L"}
    assert all(row[1] for row in rows), "chaque fiche doit avoir une cle"
    assert {row[2] for row in rows} == {945, 994}


@pytest.mark.asyncio
async def test_migrer_deux_fois_ne_change_rien(tmp_path: Path) -> None:
    """Le demarrage de l'API appelle `migrate()` a chaque fois.

    Un conteneur qui redemarre trois fois ne doit pas se comporter autrement
    qu'un conteneur qui demarre une fois.
    """
    path = tmp_path / "double.db"
    session_mod, _ = _fresh_modules(f"sqlite+aiosqlite:///{path}")

    await session_mod.migrate()  # type: ignore[attr-defined]
    await session_mod.migrate()  # type: ignore[attr-defined]

    with create_engine(f"sqlite:///{path}").begin() as conn:
        versions = conn.execute(text("SELECT version_num FROM alembic_version")).all()
    assert len(versions) == 1
