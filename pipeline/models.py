"""Types partages par toutes les etapes du pipeline.

Deux familles :
- les enregistrements **serialisables** (Correction, Anomaly) -> Pydantic, ils
  finissent en base et dans le rapport JSON ;
- les **conteneurs de travail** (StepResult) -> dataclass, ils portent un
  DataFrame Polars qui n'a pas vocation a etre serialise.

L'audit trail repose sur `Correction` : toute valeur modifiee par le pipeline en
produit une, sans exception. C'est ce qui rend chaque golden record explicable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - import lourd, uniquement pour le typage
    import polars as pl


class Author(StrEnum):
    """Qui a produit la correction. Determine le niveau de confiance accorde."""

    RULE = "RULE"
    LLM = "LLM"
    HUMAN = "HUMAN"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ProductStatus(StrEnum):
    VALIDATED = "VALIDATED"
    AUTO_CORRECTED = "AUTO_CORRECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"


class AnomalyCode(StrEnum):
    """Codes stables : le rapport et les seuils de quarantaine s'appuient dessus.

    Les valeurs sont volontairement lisibles telles quelles dans un log JSON.
    """

    # ingest
    MOJIBAKE_FIXED = "mojibake_fixed"
    MARKETING_SUFFIX_DETACHED = "marketing_suffix_detached"
    LABEL_EMPTY = "label_empty"

    # ean
    EAN_MISSING = "ean_missing"
    EAN_PADDED = "ean_padded"
    EAN_UNPADDED = "ean_unpadded"
    EAN_NOT_A_GTIN = "ean_not_a_gtin"
    EAN_INTERNAL_GS1 = "ean_internal_gs1"
    EAN_GTIN14 = "ean_gtin14"
    EAN_INVALID = "ean_invalid"

    # url
    URL_MISSING = "url_missing"
    URL_SCHEME_REPAIRED = "url_scheme_repaired"
    URL_MALFORMED = "url_malformed"
    URL_UNREACHABLE = "url_unreachable"
    URL_DOMAIN_UNRESOLVABLE = "url_domain_unresolvable"

    # tva
    VAT_MISSING = "vat_missing"
    VAT_ILLEGAL_RATE = "vat_illegal_rate"
    VAT_CATEGORY_MISMATCH = "vat_category_mismatch"

    # taxonomie
    TAXONOMY_INCOMPLETE = "taxonomy_incomplete"
    TAXONOMY_GAP = "taxonomy_gap"
    TAXONOMY_GAP_FILLED = "taxonomy_gap_filled"
    TAXONOMY_UNKNOWN_PATH = "taxonomy_unknown_path"
    TAXONOMY_NAME_MISMATCH = "taxonomy_name_mismatch"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Correction(BaseModel):
    """Une valeur a change. Non negociable : on garde l'avant, l'apres et le pourquoi."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    row_id: str
    field_name: str
    old_value: str | None
    new_value: str | None
    author: Author
    rule: str = Field(description="Identifiant de la regle ou du prompt applique")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_utcnow)


class Anomaly(BaseModel):
    """Un probleme constate. Peut coexister avec une correction (anomalie reparee)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    row_id: str
    field_name: str
    code: AnomalyCode
    severity: Severity
    detail: str = ""
    # La valeur qui pose probleme, et le libelle de la ligne. Sans elles, cent
    # anomalies d'un meme code s'affichent en cent lignes identiques : on sait
    # qu'il y a 1 438 codes internes, pas LESQUELS, ni sur quels produits. Or
    # c'est exactement ce qu'un utilisateur doit retrouver dans son CSV.
    value: str = ""
    label: str = ""


class StepMetrics(BaseModel):
    """Le funnel doit se lire dans les logs : chaque etape declare ce qu'elle a vu."""

    step: str
    rows_in: int
    rows_out: int
    duration_ms: float = 0.0
    counters: dict[str, int] = Field(default_factory=dict)


@dataclass(slots=True)
class StepResult:
    """Sortie d'une etape. `df` circule d'etape en etape, le reste s'accumule."""

    df: pl.DataFrame
    corrections: list[Correction] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    metrics: StepMetrics | None = None
    # Sorties dont la cardinalite differe de celle du DataFrame : les golden
    # records de l'etape de deduplication (273 fiches pour 10 000 lignes) et les
    # taches de revue groupees. Elles ne peuvent pas etre des colonnes.
    payload: dict[str, Any] = field(default_factory=dict)


class RunReport(BaseModel):
    """Rapport de qualite d'un run. C'est l'objet que l'API expose et que
    l'interface de revue affiche en tendance."""

    run_id: str
    store_id: str
    source_file: str
    file_sha256: str
    started_at: datetime
    finished_at: datetime | None = None
    quarantined: bool = False
    quarantine_reason: str | None = None

    rows_in: int = 0
    rows_out: int = 0

    steps: list[StepMetrics] = Field(default_factory=list)
    anomalies_by_code: dict[str, int] = Field(default_factory=dict)
    corrections_by_author: dict[str, int] = Field(default_factory=dict)
    # Les LIGNES corrigees ci-dessus, les DECISIONS ici. Une reponse du LLM sur
    # un libelle distinct s'applique a toutes les lignes qui le portent : la
    # meme decision compte pour une ici et pour des milliers au-dessus.
    # Confondre les deux fait passer l'etage qui decide le moins pour celui qui
    # corrige le plus.
    decisions_by_author: dict[str, int] = Field(default_factory=dict)
    status_counts: dict[str, int] = Field(default_factory=dict)

    publishable_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Part des LIGNES recues exploitables : EAN + image + taxonomie + TVA "
            "coherente. Sante du fichier depose, et seuil du garde-fou de quarantaine."
        ),
    )
    # Les FICHES, elles, se comptent apres regroupement — et un conflit ne
    # nait souvent qu'au regroupement. Les deux mesures divergent alors, et
    # c'est celle-ci que l'export confirme fiche par fiche.
    records_total: int = 0
    records_publishable: int = 0
    llm_calls_real: int = 0
    llm_calls_cached: int = 0

    extra: dict[str, Any] = Field(default_factory=dict)
