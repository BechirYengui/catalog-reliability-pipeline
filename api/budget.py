"""Enveloppe de dépense LLM, cumulée sur toute la durée de la démo.

Le plafond par run ne protège de rien tout seul : douze runs à 0,44 $ dépassent
une enveloppe de 5 $ sans qu'aucun run n'ait franchi son propre plafond. Le
garde-fou qui compte est donc CUMULÉ, et il est vérifié AVANT chaque run.

Fonctionnement :
- avant de lancer un run, on lit ce qui reste ;
- ce reliquat devient le plafond du run (jamais plus que ce qui reste) ;
- à 0, l'étage LLM est simplement sauté et le reste du pipeline continue —
  exactement comme lorsqu'aucune clé n'est configurée.

Volontairement conservateur : on coupe l'API plutôt que de risquer le
dépassement. Une facture inattendue est un incident, un enrichissement
manquant est une dégradation visible et réversible.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import LlmSpend
from pipeline.llm.client import LlmUsage
from pipeline.logging import get_logger

log = get_logger(__name__)

# Verrou du PROCESS, pas de la base : ce que `reserve` protege se joue entre
# l'instant ou un run lit le reliquat et l'instant, bien plus tard, ou il
# inscrit sa depense reelle. Deux depots geres par la MEME API pendant cette
# fenetre lisaient jusqu'ici le meme reliquat et se croyaient chacun
# autorises a le depenser en entier : le depassement n'etait borne que par le
# nombre de runs simultanes, pas par l'enveloppe.
#
# Deliberement en memoire, pas en base : reserver le plafond ENTIER d'un run
# des son demarrage (le seul moyen de fermer la fenetre via une ecriture
# durable, donc visible d'un autre PROCESS) ferait afficher une enveloppe
# epuisee pendant toute la duree de CHAQUE run, alors que la depense reelle
# reste presque toujours tres inferieure au plafond — un faux "budget a sec"
# permanent, pire que le trou qu'on corrige. La reservation en memoire evite
# ce cout visible et couvre le cas reel : deux depots presque simultanes geres
# par le meme serveur API.
#
# Ce qu'elle ne couvre PAS : le second processus qui partage l'enveloppe, le
# timer de nuit lance par la CLI (memoire separee). Risque juge negligeable
# en pratique — le timer tourne a 5h30, hors des fenetres de depot interactif
# — mais reel si ça change : il faudrait alors une reservation DURABLE
# (colonne dediee, pas une ligne LlmSpend qu'on ferait passer pour une
# depense) protegee par un verrou Postgres (pg_advisory_lock). Volontairement
# pas fait ici : ça change ce que la base enregistre, donc ça se discute
# avant de s'ecrire.
_lock = asyncio.Lock()
_reserved_by_run: dict[str, float] = {}

# Enveloppe totale de la démonstration, en dollars. L'API Anthropic facture en
# USD ; afficher des euros supposerait un taux de change qui ne serait qu'une
# approximation de plus.
DEFAULT_BUDGET_USD = 5.0


def budget_limit() -> float:
    raw = os.environ.get("LLM_BUDGET_USD", "").strip()
    try:
        return float(raw) if raw else DEFAULT_BUDGET_USD
    except ValueError:
        return DEFAULT_BUDGET_USD


@dataclass(frozen=True, slots=True)
class BudgetState:
    limit_usd: float
    spent_usd: float
    runs_charged: int

    @property
    def remaining_usd(self) -> float:
        return max(self.limit_usd - self.spent_usd, 0.0)

    @property
    def exhausted(self) -> bool:
        # Marge d'un dixième de centime : sous ce seuil il ne reste pas de quoi
        # payer un appel utile, et laisser filer un résidu inviterait à le
        # dépasser.
        return self.remaining_usd <= 0.001

    @property
    def used_ratio(self) -> float:
        return min(self.spent_usd / self.limit_usd, 1.0) if self.limit_usd else 1.0

    def to_dict(self) -> dict[str, object]:
        return {
            "limit_usd": round(self.limit_usd, 4),
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
            "used_ratio": round(self.used_ratio, 4),
            "exhausted": self.exhausted,
            "runs_charged": self.runs_charged,
            "currency": "USD",
        }


async def read_budget(session: AsyncSession) -> BudgetState:
    """Ce qui est REELLEMENT depense, tel qu'affiche au tableau de bord.

    Ne compte pas les reservations en vol (voir `reserve`) : celles-ci ne sont
    pas de la depense, seulement une place retenue le temps qu'un run se
    termine. Les compter ici referait apparaitre l'enveloppe comme epuisee
    pendant chaque run — precisement l'effet de bord que `reserve` evite.
    """
    total = await session.scalar(select(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0)))
    count = await session.scalar(select(func.count()).select_from(LlmSpend))
    return BudgetState(
        limit_usd=budget_limit(),
        spent_usd=float(total or 0.0),
        runs_charged=int(count or 0),
    )


async def reserve(session: AsyncSession, run_id: str, configured_max_usd: float) -> float:
    """Reclame un plafond pour CE run sans depasser ce qui reste compte tenu
    des runs deja en vol dans ce process.

    C'est cet appel, pas seulement `read_budget`, qui ferme la course : la
    lecture du reliquat et la reservation qui en decoule sont dans la MEME
    section critique, donc un second run qui arrive pendant qu'un premier est
    deja en vol voit le reliquat DEJA AMPUTE de ce que le premier a reclame.
    """
    async with _lock:
        state = await read_budget(session)
        already_reserved = sum(_reserved_by_run.values())
        remaining = max(state.remaining_usd - already_reserved, 0.0)
        ceiling = min(max(configured_max_usd, 0.0), remaining)
        if ceiling > 0:
            _reserved_by_run[run_id] = ceiling
        return ceiling


def release(run_id: str) -> None:
    """A appeler dans un `finally`, quoi qu'il arrive au run : succes, echec,
    exception. Une reservation oubliee ampute le reliquat pour rien jusqu'au
    redemarrage du process."""
    _reserved_by_run.pop(run_id, None)


async def record_spend(session: AsyncSession, run_id: str, store_id: str, usage: LlmUsage) -> None:
    """N'enregistre que les runs ayant réellement appelé l'API.

    Un run entièrement servi par le cache n'a rien coûté : lui créer une ligne
    à 0 $ gonflerait le compteur de runs facturés sans montant, et rendrait le
    registre trompeur.
    """
    if usage.calls_real == 0:
        return

    cost = usage.cost
    session.add(
        LlmSpend(
            run_id=run_id,
            store_id=store_id,
            model=usage.model,
            calls_real=usage.calls_real,
            calls_cached=usage.calls_cached,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=cost.total_usd,
        )
    )
    log.info(
        "llm.spend_recorded",
        run_id=run_id,
        model=usage.model,
        calls=usage.calls_real,
        cost_usd=round(cost.total_usd, 6),
    )
