"""Cache des statuts d'URL adosse a PostgreSQL, avec duree de validite.

Le cache en memoire du verificateur ne survit pas au processus : chaque depot
repayait les ~4 minutes de verification des 9 000 memes images. La table
`url_check_cache` existait pour ca — declaree en phase 1, jamais branchee.
C'est ce module qui la branche, sur le meme motif que le cache LLM : charge en
amont par l'orchestrateur, injecte dans l'etape (qui reste pure), persiste en
aval.

Le TTL est celui de la configuration HTTP (`cache_ttl_hours`, 24 h par
defaut) : une image morte peut ressusciter et une vivante mourir, un statut
n'est donc une preuve que pour un temps. Une entree perimee n'est simplement
pas chargee — elle sera reverifiee et reecrite par le run qui la rencontre.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import UrlCheckCache
from pipeline.logging import get_logger
from pipeline.steps.url_checker import UrlStatus, domain_of

log = get_logger(__name__)

# Postgres n'accepte pas plus de 32 767 parametres par requete. Six colonnes
# par ligne, donc 5 461 lignes au maximum theorique : on prend une marge large
# plutot que de raser la limite, une colonne ajoutee un jour ferait replonger
# le calcul sans prevenir.
_MAX_ROWS_PER_INSERT = 2000


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


async def load(session: AsyncSession, ttl_hours: int) -> dict[str, UrlStatus]:
    """Charge les statuts encore valides. Le TTL s'applique ICI, a la lecture :
    la table peut garder des entrees perimees, elles ne servent juste plus."""
    horizon = datetime.now(UTC) - timedelta(hours=ttl_hours)
    result = await session.execute(select(UrlCheckCache).where(UrlCheckCache.checked_at >= horizon))
    seed = {
        row.url: UrlStatus(url=row.url, ok=row.ok, reason=row.reason, status_code=row.status_code)
        for row in result.scalars()
    }
    log.info("url_cache.loaded", entries=len(seed), ttl_hours=ttl_hours)
    return seed


async def persist(session: AsyncSession, fresh: dict[str, UrlStatus]) -> int:
    """Ecrit ce que le run a etabli par le reseau. Upsert : une URL reverifiee
    apres peremption remplace son ancienne entree, l'horodatage repart."""
    if not fresh:
        return 0

    now = datetime.now(UTC)
    rows = [
        {
            "url": status.url[:1024],
            "domain": domain_of(status.url)[:255],
            "ok": status.ok,
            "status_code": status.status_code,
            "reason": status.reason[:60],
            "checked_at": now,
        }
        for status in fresh.values()
    ]

    # Par LOTS, jamais en une fois. Le protocole Postgres plafonne a 32 767
    # parametres par requete : a 6 colonnes, un fichier reel (9 045 URLs) en
    # demande 54 270 et la requete est rejetee en bloc. Constate en production
    # le 2026-08-11, run 2a50f1c9. La borne ne depend pas du fichier mais du
    # protocole : elle doit vivre dans le code, pas dans l'espoir que les
    # fichiers restent petits. sqlite a la meme limite de variables, et les
    # deux dialectes savent faire l'upsert : un seul chemin pour les deux,
    # donc le decoupage est teste par la suite au lieu d'exister seulement en
    # production.
    insert = pg_insert if _is_postgres(session) else sqlite_insert
    for start in range(0, len(rows), _MAX_ROWS_PER_INSERT):
        chunk = rows[start : start + _MAX_ROWS_PER_INSERT]
        statement = insert(UrlCheckCache).values(chunk)
        statement = statement.on_conflict_do_update(
            index_elements=[UrlCheckCache.url],
            set_={
                "ok": statement.excluded.ok,
                "status_code": statement.excluded.status_code,
                "reason": statement.excluded.reason,
                "checked_at": statement.excluded.checked_at,
            },
        )
        await session.execute(statement)

    log.info(
        "url_cache.persisted", entries=len(rows), batches=-(-len(rows) // _MAX_ROWS_PER_INSERT)
    )
    return len(rows)
