"""« Qui corrige quoi » doit pouvoir montrer l'humain.

La repartition portee par le rapport est figee a la fin du traitement. Or une
decision de revue, une fusion ou une correction a la main arrivent forcement
APRES : l'etage humain y valait donc zero de facon permanente, meme quand
l'utilisateur venait de trancher. Le graphique annoncait « Validation humaine
0 % » en ayant sous les yeux le contraire.

Ce test compare les deux sources sur un meme run, apres un geste humain.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

_TMP = Path(tempfile.mkdtemp(prefix="catalog-corrections-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'corrections.db'}"
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
    ProductSourceRow,
    ReviewTask,
    RunAnomaly,
    RunStep,
    Store,
    User,
)
from db.session import SessionFactory, create_all  # noqa: E402

PROPRE = Path("data/samples/tests/01-catalogue-propre.csv")
# Repete ses defauts : 200 lignes corrigees pour 20 decisions. C'est ce qui
# permet de prouver que les deux comptages ne disent pas la meme chose.
DOUBLONS = Path("data/samples/tests/04-catalogue-doublons.csv")


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
            ProductSourceRow,
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
        await http.post(
            "/api/auth/login",
            json={"email": "admin@ulty.fr", "password": "mot-de-passe-de-test"},
        )
        yield http


@pytest.mark.asyncio
async def test_une_fusion_apparait_dans_la_repartition(http_client: httpx.AsyncClient) -> None:
    with PROPRE.open("rb") as fh:
        created = await http_client.post(
            "/api/runs",
            files={"file": (PROPRE.name, fh, "text/csv")},
            data={"store_id": "Magasin", "check_urls": "false"},
        )
    run_id = created.json()["id"]
    await jobs.execute_run(run_id)

    avant = (await http_client.get(f"/api/runs/{run_id}/summary")).json()["corrections_by_author"]
    assert avant["HUMAN"] == 0

    fiches = (await http_client.get("/api/products")).json()
    assert len(fiches) >= 2
    fusion = await http_client.post(
        "/api/products/merge",
        json={"product_ids": [fiches[0]["id"], fiches[1]["id"]]},
    )
    assert fusion.status_code == 200, fusion.text

    apres = (await http_client.get(f"/api/runs/{run_id}/summary")).json()["corrections_by_author"]
    assert apres["HUMAN"] == 1, "le geste humain n'apparait pas dans la repartition"
    assert apres["RULE"] == avant["RULE"], "les corrections des regles ont bouge"

    # Et la preuve du defaut : le rapport, lui, reste a zero pour toujours.
    rapport = (await http_client.get(f"/api/runs/{run_id}")).json()["report"]
    assert rapport["corrections_by_author"].get("HUMAN", 0) == 0


@pytest.mark.asyncio
async def test_une_decision_repetee_sur_plusieurs_lignes_ne_compte_qu_une_fois(
    http_client: httpx.AsyncClient,
) -> None:
    """« Qui corrige quoi » demande qui a TRANCHE, pas combien de lignes ont bougé.

    Le LLM décide sur des libellés distincts puis applique sa réponse à toutes
    les lignes qui portent le même libellé : sur le fichier réel, 273 décisions
    devenaient 7 711 corrections, et la carte donnait 84 % au LLM contre 16 %
    aux règles — l'inverse exact de qui avait tranché. Les règles répètent
    elles aussi la même réparation (le même EAN cassé revient), donc la
    définition doit être la même des deux côtés, sinon on compare encore deux
    unités différentes.

    Le catalogue de doublons sert ici parce qu'il RÉPÈTE ses défauts : 200
    lignes corrigées pour 20 décisions. Sur le catalogue propre, les deux
    comptes valent zéro et le test passerait sans rien démontrer.
    """
    with DOUBLONS.open("rb") as fh:
        created = await http_client.post(
            "/api/runs",
            files={"file": (DOUBLONS.name, fh, "text/csv")},
            data={"store_id": "Magasin", "check_urls": "false"},
        )
    run_id = created.json()["id"]
    await jobs.execute_run(run_id)

    summary = (await http_client.get(f"/api/runs/{run_id}/summary")).json()
    lignes = summary["corrections_by_author"]
    decisions = summary["decisions_by_author"]

    # Une decision ne peut jamais depasser le nombre de lignes qu'elle corrige.
    for auteur in ("RULE", "LLM", "HUMAN"):
        assert decisions[auteur] <= lignes[auteur], (
            f"{auteur} : plus de decisions que de lignes corrigees, "
            "le comptage distinct ne distingue rien"
        )

    # Et sur un fichier qui repete ses defauts, il y en a STRICTEMENT moins :
    # sans cela le compteur existerait sans rien mesurer.
    assert sum(decisions.values()) < sum(lignes.values()), (
        "aucune correction repetee detectee : le comptage par decision "
        "n'apporte rien sur ce fichier, le test ne prouve rien"
    )

    # Le rapport fige porte la meme distinction que le comptage en base.
    rapport = (await http_client.get(f"/api/runs/{run_id}")).json()["report"]
    assert rapport["decisions_by_author"]["RULE"] <= rapport["corrections_by_author"]["RULE"]


@pytest.mark.asyncio
async def test_les_fiches_publiables_se_comptent_en_base(http_client: httpx.AsyncClient) -> None:
    """Le rapport est fige a la fin du run ; le referentiel, lui, vit.

    C'est le travail humain qui debloque une fiche : sur un magasin reel, dix
    validations de TVA ont rendu publiables dix fiches que le rapport decrit
    encore comme rejetees. Et les runs anterieurs a ce champ n'en ont aucun,
    ce qui affichait « 0 / 43 » sur un catalogue qui en publie 28.
    """
    with PROPRE.open("rb") as fh:
        created = await http_client.post(
            "/api/runs",
            files={"file": (PROPRE.name, fh, "text/csv")},
            data={"store_id": "Magasin", "check_urls": "false"},
        )
    run_id = created.json()["id"]
    await jobs.execute_run(run_id)

    summary = (await http_client.get(f"/api/runs/{run_id}/summary")).json()
    assert summary["records_total"] == 10
    # Sans verification des images, aucune fiche n'est publiable : une URL non
    # controlee ne prouve rien.
    assert summary["records_publishable"] == 0

    # Ce que ferait une validation humaine : la fiche devient publiable, et le
    # comptage doit suivre sans attendre un nouveau depot.
    from sqlalchemy import select

    async with SessionFactory() as session:
        fiche = (await session.execute(select(Product).limit(1))).scalars().one()
        fiche.publishable = True
        await session.commit()

    apres = (await http_client.get(f"/api/runs/{run_id}/summary")).json()
    assert apres["records_publishable"] == 1
