"""Catalogue des modèles et calcul du coût réel.

Une plateforme qui appelle un LLM sans afficher ce qu'il coûte est une
plateforme dont personne ne peut arbitrer l'usage. Chaque appel est donc
comptabilisé : modèle, jetons d'entrée, jetons de sortie, jetons servis par le
cache, et le montant correspondant.

Les tarifs sont exprimés en dollars par MILLION de jetons, comme les publie
Anthropic. Ils sont datés : un tarif recopié de mémoire dans six mois sera faux,
donc la date de relevé est dans le code et affichée dans l'interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Relevé du 2026-06-24. Les prix d'entrée/sortie sont en dollars par million de
# jetons. À revérifier sur platform.claude.com/docs/en/pricing avant toute
# décision budgétaire — ce fichier est un cache, pas une source de vérité.
PRICING_AS_OF = date(2026, 6, 24)


@dataclass(frozen=True, slots=True)
class ModelPricing:
    model_id: str
    display_name: str
    input_per_mtok: float
    output_per_mtok: float
    context_tokens: int
    note: str = ""


CATALOG: dict[str, ModelPricing] = {
    "claude-opus-5": ModelPricing(
        "claude-opus-5",
        "Claude Opus 5",
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        context_tokens=1_000_000,
        note="Le plus capable de la gamme Opus. Défaut du projet.",
    ),
    "claude-sonnet-5": ModelPricing(
        "claude-sonnet-5",
        "Claude Sonnet 5",
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        context_tokens=1_000_000,
        note="Bon compromis vitesse/intelligence pour du volume.",
    ),
    "claude-haiku-4-5": ModelPricing(
        "claude-haiku-4-5",
        "Claude Haiku 4.5",
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        context_tokens=200_000,
        note="Le plus rapide et le moins cher, pour des tâches simples.",
    ),
}

DEFAULT_MODEL = "claude-opus-5"

# Un jeton servi par le cache coûte environ 10 % du tarif d'entrée ; l'écrire
# coûte environ 1,25 fois ce tarif. C'est ce qui rend le cache rentable dès le
# deuxième passage sur un même libellé.
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True, slots=True)
class UsageCost:
    """Décomposition d'un coût, en dollars."""

    input_usd: float
    output_usd: float
    cache_read_usd: float
    cache_write_usd: float

    @property
    def total_usd(self) -> float:
        return self.input_usd + self.output_usd + self.cache_read_usd + self.cache_write_usd


# Traitement par lots asynchrone : moitie prix sur tous les jetons. Ce n'est
# pas un tarif de nuit, c'est le prix de la patience — Anthropic case ces
# requetes dans ses creux de charge au lieu de garder une machine prete a
# repondre en deux secondes.
BATCH_MULTIPLIER = 0.5


def pricing_for(model_id: str) -> ModelPricing:
    """Tarif d'un modèle. Un modèle inconnu ne doit pas faire tomber un run :
    on facture au tarif du modèle par défaut et l'écart se verra dans le
    rapport, plutôt que d'interrompre un traitement pour une question de
    comptabilité."""
    return CATALOG.get(model_id) or CATALOG[DEFAULT_MODEL]


def compute_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch_api: bool = False,
) -> UsageCost:
    price = pricing_for(model_id)
    discount = BATCH_MULTIPLIER if batch_api else 1.0
    per_input = price.input_per_mtok / 1_000_000 * discount
    per_output = price.output_per_mtok / 1_000_000 * discount
    return UsageCost(
        input_usd=input_tokens * per_input,
        output_usd=output_tokens * per_output,
        cache_read_usd=cache_read_tokens * per_input * CACHE_READ_MULTIPLIER,
        cache_write_usd=cache_write_tokens * per_input * CACHE_WRITE_MULTIPLIER,
    )


def format_usd(amount: float) -> str:
    """Un coût de 0,004 $ affiché « 0,00 $ » donne l'impression que rien n'a été
    dépensé. On garde donc assez de décimales pour que le chiffre reste vrai."""
    if amount == 0:
        return "0,00 $"
    if amount < 0.01:
        return f"{amount:.4f} $".replace(".", ",")
    return f"{amount:.2f} $".replace(".", ",")
