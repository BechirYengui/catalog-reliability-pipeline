"""La quarantaine doit tenir sa promesse.

L'interface annonce « rien n'a ete integre au referentiel ». Le code, lui,
ecrivait ses fiches comme un run normal : le catalogue catastrophe a depose
« CAFE CREME FRAICHE 30 20CL » et un libelle vide dans le referentiel d'un
magasin, sous un bandeau affirmant le contraire.

Un garde-fou qui n'arrete rien est pire que pas de garde-fou : il donne
confiance. D'ou ces tests, qui verifient les deux cotes — ce qui ne doit PAS
etre ecrit, et ce qui doit l'etre quand meme.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

# L'environnement est pose AVANT d'importer l'API : moteur et DATA_DIR sont
# fixes au chargement des modules. Un seul import pour tout le fichier, et les
# tables sont videes entre les tests — reimporter l'API a chaque test laisse
# derriere lui des moteurs encore references, et le run devient introuvable.
_TMP = Path(tempfile.mkdtemp(prefix="catalog-quarantine-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'quarantine.db'}"
os.environ["DATA_DIR"] = str(_TMP)

import httpx  # noqa: E402

from api import jobs  # noqa: E402
from api.main import app  # noqa: E402
from api.security import hash_password  # noqa: E402
from db.models import (  # noqa: E402
    IngestionRun,
    LearnedRule,
    Product,
    ProductCorrection,
    ReviewTask,
    RunAnomaly,
    RunStep,
    Store,
    User,
)
from db.session import SessionFactory, create_all  # noqa: E402

CATASTROPHE = Path("data/samples/tests/02-catalogue-catastrophe.csv")
PROPRE = Path("data/samples/tests/01-catalogue-propre.csv")


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    from sqlalchemy import delete

    await create_all()
    async with SessionFactory() as session:
        for table in (
            ProductCorrection,
            LearnedRule,
            RunAnomaly,
            ReviewTask,
            Product,
            RunStep,
            IngestionRun,
            Store,
            User,
        ):
            await session.execute(delete(table))
        session.add(
            User(
                email="admin@ulty.fr",
                password_hash=hash_password("mot-de-passe-de-test"),
                role="admin",
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test", timeout=120) as http:
        login = await http.post(
            "/api/auth/login",
            json={"email": "admin@ulty.fr", "password": "mot-de-passe-de-test"},
        )
        assert login.status_code == 200, login.text
        yield http


async def _depose(http: httpx.AsyncClient, fichier: Path, store: str) -> dict:
    """Depose un CSV et attend la fin du traitement, sans reseau ni LLM."""
    with fichier.open("rb") as fh:
        created = await http.post(
            "/api/runs",
            files={"file": (fichier.name, fh, "text/csv")},
            data={"store_id": store, "check_urls": "false"},
        )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    await jobs.execute_run(run_id)
    detail = (await http.get(f"/api/runs/{run_id}")).json()
    assert detail["status"] in ("COMPLETED", "QUARANTINE"), detail
    return detail


@pytest.mark.asyncio
async def test_un_fichier_en_quarantaine_n_ecrit_aucune_fiche(
    http_client: httpx.AsyncClient,
) -> None:
    run = await _depose(http_client, CATASTROPHE, "Magasin_Casse")

    assert run["status"] == "QUARANTINE"

    from sqlalchemy import func, select

    async with SessionFactory() as session:
        fiches = await session.scalar(select(func.count()).select_from(Product))
        taches = await session.scalar(select(func.count()).select_from(ReviewTask))

    assert fiches == 0, "le referentiel a ete pollue par un fichier rejete"
    assert taches == 0, "des decisions ont ete mises en file pour un fichier rejete"


@pytest.mark.asyncio
async def test_la_quarantaine_garde_de_quoi_s_expliquer(http_client: httpx.AsyncClient) -> None:
    """Sans les anomalies, le rejet serait indiagnosticable."""
    run = await _depose(http_client, CATASTROPHE, "Magasin_Casse")

    assert run["report"]["quarantine_reason"]

    from sqlalchemy import func, select

    async with SessionFactory() as session:
        anomalies = await session.scalar(select(func.count()).select_from(RunAnomaly))

    assert anomalies > 0


@pytest.mark.asyncio
async def test_un_fichier_sain_alimente_bien_le_referentiel(
    http_client: httpx.AsyncClient,
) -> None:
    """Le garde-fou ne doit pas bloquer ce qu'il est cense laisser passer."""
    run = await _depose(http_client, PROPRE, "Magasin_Sain")

    assert run["status"] == "COMPLETED"

    from sqlalchemy import func, select

    async with SessionFactory() as session:
        fiches = await session.scalar(select(func.count()).select_from(Product))

    assert fiches == 10
