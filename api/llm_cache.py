"""Cache LLM adossé à PostgreSQL.

Le cache en mémoire du client ne survit pas au processus : chaque run repayait
les mêmes libellés. Sur une enveloppe de 4 $ à 0,44 $ le run, cela plafonne la
démonstration à neuf exécutions. Avec ce cache, seuls les libellés JAMAIS VUS
déclenchent un appel — le deuxième dépôt d'un même magasin ne coûte rien.

L'étape du pipeline est synchrone (Polars) et tourne dans un thread ; ce cache
est donc chargé en amont et réécrit en aval, plutôt que d'ouvrir une session
asynchrone depuis le thread de travail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import LlmCache
from pipeline.logging import get_logger

log = get_logger(__name__)


class PreloadedCache:
    """Cache lu en base avant le run, réécrit après.

    `get` sert les entrées préchargées ; `set` accumule les nouvelles, que
    l'orchestrateur persiste une fois l'étape terminée.
    """

    def __init__(self, preloaded: dict[str, dict[str, Any]]) -> None:
        self._preloaded = preloaded
        self.pending: dict[str, dict[str, Any]] = {}
        self.hits = 0

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._preloaded.get(key)
        if entry is not None:
            self.hits += 1
        return entry

    def set(
        self,
        key: str,
        model: str,
        prompt: str,
        response: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self.pending[key] = {
            "model": model,
            "prompt": prompt,
            "response": response,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }


async def load(session: AsyncSession, model: str, limit: int = 5000) -> PreloadedCache:
    """Charge le cache du modèle courant.

    Filtré par modèle : resservir la réponse d'un modèle pour un autre
    fausserait la qualité comme la comptabilité.
    """
    result = await session.execute(select(LlmCache).where(LlmCache.model == model).limit(limit))
    entries = {row.key: row.response for row in result.scalars().all()}
    log.info("llm_cache.loaded", model=model, entries=len(entries))
    return PreloadedCache(entries)


async def persist(session: AsyncSession, cache: PreloadedCache) -> int:
    """Écrit les nouvelles entrées. Une entrée déjà présente voit son compteur
    de réutilisations incrémenté plutôt que d'être réécrite."""
    written = 0
    for key, payload in cache.pending.items():
        existing = await session.get(LlmCache, key)
        if existing is not None:
            existing.hits += 1
            continue
        session.add(
            LlmCache(
                key=key,
                model=payload["model"],
                prompt=payload["prompt"][:20000],
                response=payload["response"],
                input_tokens=payload["input_tokens"],
                output_tokens=payload["output_tokens"],
                created_at=datetime.now(UTC),
            )
        )
        written += 1

    if written:
        log.info("llm_cache.persisted", entries=written)
    return written
