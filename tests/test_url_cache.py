"""Le cache d'URL en base : ce qui rend le deuxieme depot rapide.

La table `url_check_cache` existait depuis la phase 1 — declaree, migree,
jamais branchee. Chaque depot quotidien repayait donc les ~4 minutes de
verification des 9 000 memes images. Ces tests verrouillent le branchement :
ce qui est ecrit, ce qui est relu, et ce que le TTL perime.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# L'environnement est pose AVANT d'importer l'API : le moteur est fixe au
# chargement du module (meme motif que test_quarantine.py).
_TMP = Path(tempfile.mkdtemp(prefix="catalog-urlcache-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'urlcache.db'}"
os.environ["DATA_DIR"] = str(_TMP)

from api import url_cache  # noqa: E402
from db.models import UrlCheckCache  # noqa: E402
from db.session import SessionFactory, create_all  # noqa: E402
from pipeline.steps.url_checker import UrlStatus  # noqa: E402


@pytest.mark.asyncio
async def test_ce_qui_est_persiste_est_relu_tel_quel() -> None:
    await create_all()
    fresh = {
        "https://cdn.example/a.jpg": UrlStatus("https://cdn.example/a.jpg", True, "ok", 200),
        "https://cdn.example/b.jpg": UrlStatus("https://cdn.example/b.jpg", False, "http_404", 404),
    }

    async with SessionFactory() as session:
        assert await url_cache.persist(session, fresh) == 2
        await session.commit()

    async with SessionFactory() as session:
        seed = await url_cache.load(session, ttl_hours=24)

    assert set(seed) == set(fresh)
    assert seed["https://cdn.example/a.jpg"].ok is True
    assert seed["https://cdn.example/b.jpg"].reason == "http_404"
    assert seed["https://cdn.example/b.jpg"].status_code == 404


@pytest.mark.asyncio
async def test_une_entree_perimee_nest_pas_servie() -> None:
    """Une image morte peut ressusciter, une vivante mourir : au-dela du TTL,
    le statut n'est plus une preuve et l'URL sera reverifiee."""
    await create_all()
    async with SessionFactory() as session:
        session.add(
            UrlCheckCache(
                url="https://cdn.example/vieille.jpg",
                domain="cdn.example",
                ok=True,
                status_code=200,
                reason="ok",
                checked_at=datetime.now(UTC) - timedelta(hours=48),
            )
        )
        await session.commit()

    async with SessionFactory() as session:
        seed = await url_cache.load(session, ttl_hours=24)

    assert "https://cdn.example/vieille.jpg" not in seed


@pytest.mark.asyncio
async def test_un_fichier_reel_ne_depasse_pas_la_limite_de_parametres() -> None:
    """9 045 URLs, le volume du fichier reel — pas 3.

    Postgres plafonne a 32 767 parametres par requete : a 6 colonnes, tout
    ecrire d'un coup en demande 54 270 et la requete part en erreur. Le run
    2a50f1c9 est mort dessus en production alors que les tests, qui
    n'ecrivaient que trois lignes, restaient verts. Le volume fait partie du
    contrat : il est teste comme tel.
    """
    await create_all()
    fresh = {
        f"https://picsum.photos/seed/p-{i}/640/480": UrlStatus(
            f"https://picsum.photos/seed/p-{i}/640/480", True, "ok", 200
        )
        for i in range(9045)
    }

    async with SessionFactory() as session:
        assert await url_cache.persist(session, fresh) == 9045
        await session.commit()

    async with SessionFactory() as session:
        seed = await url_cache.load(session, ttl_hours=24)

    assert len(seed) >= 9045
    assert url_cache._MAX_ROWS_PER_INSERT * 6 < 32_767, (
        "un lot doit tenir sous la limite de parametres de Postgres"
    )


@pytest.mark.asyncio
async def test_reverifier_remplace_lentree_et_repart_le_ttl() -> None:
    """Upsert, pas insert : la re-verification d'une URL connue met a jour le
    verdict ET l'horodatage, sans violer la cle primaire."""
    await create_all()
    async with SessionFactory() as session:
        await url_cache.persist(
            session,
            {"https://cdn.example/x.jpg": UrlStatus("https://cdn.example/x.jpg", True, "ok", 200)},
        )
        await session.commit()

    async with SessionFactory() as session:
        await url_cache.persist(
            session,
            {
                "https://cdn.example/x.jpg": UrlStatus(
                    "https://cdn.example/x.jpg", False, "http_410", 410
                )
            },
        )
        await session.commit()

    async with SessionFactory() as session:
        seed = await url_cache.load(session, ttl_hours=24)

    assert seed["https://cdn.example/x.jpg"].ok is False
    assert seed["https://cdn.example/x.jpg"].reason == "http_410"
