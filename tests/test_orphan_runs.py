"""Un run que plus personne n'execute ne doit pas rester « en cours » a vie.

La tache de fond vit dans le processus de l'API : un redeploiement ou un crash
l'emporte avec elle. Le filet d'exception d'`execute_run` est mort aussi — il
ne peut rien attraper. Constate en vrai sur le depot `test01` du 2026-08-11 :
« Etape 6/8, 62,5 %, environ 27 min restantes » affiches pendant des heures,
alors qu'aucun processus ne travaillait plus depuis longtemps.

Le balayage du demarrage repose sur un invariant simple : a cet instant, par
construction, aucun run ne peut legitimement etre RUNNING dans ce processus.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# L'environnement est pose AVANT d'importer l'API : moteur et DATA_DIR sont
# fixes au chargement des modules (meme motif que test_quarantine.py).
_TMP = Path(tempfile.mkdtemp(prefix="catalog-orphans-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'orphans.db'}"
os.environ["DATA_DIR"] = str(_TMP)

from api import jobs  # noqa: E402
from db.models import IngestionRun, RunStep, Store  # noqa: E402
from db.session import SessionFactory, create_all  # noqa: E402


@pytest.mark.asyncio
async def test_un_run_orphelin_est_marque_failed_avec_son_etape() -> None:
    await create_all()
    async with SessionFactory() as session:
        session.add(Store(id="demo", name="demo"))
        session.add(
            IngestionRun(
                id="run-orphelin",
                store_id="demo",
                file_name="/nulle/part.csv",
                file_sha256="a" * 64,
                status="RUNNING",
                current_step="entity_resolution",
            )
        )
        session.add(
            RunStep(
                run_id="run-orphelin",
                step="entity_resolution",
                position=5,
                status="RUNNING",
            )
        )
        # Un run termine ne doit PAS etre touche : le balayage ne porte que
        # sur ce qui pretend encore tourner.
        session.add(
            IngestionRun(
                id="run-fini",
                store_id="demo",
                file_name="/nulle/part2.csv",
                file_sha256="b" * 64,
                status="COMPLETED",
            )
        )
        await session.commit()

    assert await jobs.fail_orphan_runs() == 1

    async with SessionFactory() as session:
        orphan = await session.get(IngestionRun, "run-orphelin")
        assert orphan is not None
        assert orphan.status == "FAILED"
        assert orphan.error is not None and "serveur" in orphan.error
        assert orphan.finished_at is not None

        # L'etape en cours porte aussi l'echec : l'interface montre OU c'est
        # mort, pas juste QUE c'est mort.
        from sqlalchemy import select

        step = (
            await session.execute(
                select(RunStep).where(
                    RunStep.run_id == "run-orphelin",
                    RunStep.step == "entity_resolution",
                )
            )
        ).scalar_one()
        assert step.status == "FAILED"

        untouched = await session.get(IngestionRun, "run-fini")
        assert untouched is not None
        assert untouched.status == "COMPLETED"


@pytest.mark.asyncio
async def test_sans_orphelin_le_balayage_ne_touche_rien() -> None:
    await create_all()
    assert await jobs.fail_orphan_runs() == 0
