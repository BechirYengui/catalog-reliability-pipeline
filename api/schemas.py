"""Contrats d'entree/sortie de l'API.

Pydantic v2, partages avec le moteur de pipeline : un seul endroit ou la forme
des donnees est definie, donc pas de derive entre le traitement et l'affichage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# Le pipeline complet, tel qu'il est presente dans l'interface. Les etapes non
# encore implementees apparaissent quand meme, en attente : l'utilisateur doit
# voir la chaine entiere, pas seulement ce qui tourne aujourd'hui.
PIPELINE_STEPS: list[dict[str, str]] = [
    {
        "key": "ingest",
        "label": "Ingestion",
        "family": "rules",
        "description": "Encodage, BOM, mojibake, suffixes marketing collés",
    },
    {
        "key": "field_checks",
        "label": "Contrôles par champ",
        "family": "rules",
        "description": "EAN (checksum GS1), URL (HEAD), TVA, taxonomie",
    },
    {
        "key": "cross_checks",
        "label": "Cohérence entre champs",
        "family": "rules",
        "description": "Catégorie ↔ TVA, taxonomie ↔ nom, comblement des trous",
    },
    {
        "key": "llm_enrich",
        "label": "Enrichissement LLM",
        "family": "semantic",
        "description": "Libellés caisse, taxonomie manquante, marque, contenance",
    },
    {
        "key": "dedup_exact",
        "label": "Doublons exacts & golden record",
        "family": "rules",
        "description": "Regroupement après normalisation, règles de survivorship",
    },
    {
        "key": "entity_resolution",
        "label": "Quasi-doublons",
        "family": "semantic",
        "description": "Blocking par contenance, RapidFuzz sur le libellé enrichi",
    },
    {
        "key": "scoring_routing",
        "label": "Score & aiguillage",
        "family": "rules",
        "description": "Statut par produit, seuils par champ",
    },
    {
        "key": "report",
        "label": "Rapport de qualité",
        "family": "rules",
        "description": "Taux publiable, anomalies par champ, garde-fou quarantaine",
    },
]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: str


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step: str
    position: int
    status: str
    rows_in: int
    rows_out: int
    duration_ms: float
    corrections: int
    anomalies: int
    counters: dict[str, Any]
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    store_id: str
    file_name: str
    file_sha256: str
    status: str
    current_step: str | None
    progress: float
    rows_in: int
    rows_out: int
    publishable_rate: float
    error: str | None
    network_enabled: bool = True
    llm_mode: str = "live"
    triggered_by: str | None
    started_at: datetime
    finished_at: datetime | None


class RunDetail(RunOut):
    steps: list[StepOut] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)


class AnomalyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    row_id: str
    field_name: str
    code: str
    severity: str
    detail: str
    # De quel produit parle-t-on, et quelle valeur pose probleme : sans ces
    # deux-la, cent anomalies d'un meme code sont cent lignes identiques.
    value: str = ""
    label: str = ""


class CorrectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    row_id: str
    field_name: str
    old_value: str | None
    new_value: str | None
    author: str
    rule: str
    confidence: float
    created_at: datetime


class AuditEntryOut(BaseModel):
    """Une ligne du journal d'audit, lisible sans connaitre la base.

    La table stocke des identifiants ; un humain a besoin du magasin et du
    produit concernes pour que la ligne veuille dire quelque chose.
    """

    id: str
    run_id: str
    store_id: str | None = None
    product_label: str | None = None
    row_id: str
    field_name: str
    old_value: str | None
    new_value: str | None
    author: str
    rule: str
    confidence: float
    user_email: str | None = None
    created_at: datetime


class StoreCatalogOut(BaseModel):
    """Une ligne par magasin : chacun a son propre referentiel.

    Compte sur la table entiere, pas sur la page affichee : une liste de
    magasins deduite des 100 premieres fiches n'en montre qu'un seul.
    """

    store_id: str
    products: int
    publishable: int
    source_rows: int
    last_run_at: datetime | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    store_id: str
    label: str
    label_normalized: str
    label_enriched: str | None = None
    ean: str | None
    internal_code: str | None
    url_image: str | None
    url_ok: bool
    url_checked: bool = True
    taxonomy_1: str | None
    taxonomy_2: str | None
    taxonomy_3: str | None
    taxonomy_4: str | None
    vat_rate: float | None
    brand: str | None = None
    quantity_value: float | None
    quantity_unit: str | None
    status: str
    publishable: bool
    source_rows: int


class ReviewTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    store_id: str
    kind: str
    field_name: str
    title: str
    question: str
    current_value: str | None
    proposed_value: str | None
    source: str
    confidence: float
    affected_rows: int
    context: dict[str, Any]
    status: str
    requires_admin: bool
    created_at: datetime


class SourceRowOut(BaseModel):
    """Une ligne du fichier magasin rattachee a une fiche."""

    row_id: str
    source_label: str
    run_id: str

    model_config = {"from_attributes": True}


class MergeRequest(BaseModel):
    """« Ces fiches sont le meme produit. »

    C'est l'humain qui l'affirme, apres avoir vu les deux libelles et les
    lignes du magasin derriere chacun. Le pipeline ne sait pas trancher ce cas
    — s'il savait, il l'aurait deja fusionne.
    """

    product_ids: list[str]


class MergeOut(BaseModel):
    """Une fusion decidee a la main, et de quoi la reprendre."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    store_id: str
    survivor_id: str
    survivor_label: str
    absorbed_label: str
    created_at: datetime


class OverrideRequest(BaseModel):
    """« Cette valeur est fausse, voici la bonne. »

    Un seul champ a la fois : une correction porte sur une valeur precise, et
    l'audit trail doit pouvoir dire laquelle. La TVA est refusee ici, elle
    passe par la file de revue avec le role administrateur.
    """

    field_name: str = Field(
        pattern="^(label|ean|internal_code|url_image|"
        "taxonomy_1|taxonomy_2|taxonomy_3|taxonomy_4|"
        "quantity_value|quantity_unit)$"
    )
    value: str | None = None


class OverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    store_id: str
    product_key: str
    field_name: str
    previous_value: str | None
    value: str | None
    created_at: datetime


class RowLookupOut(BaseModel):
    """Reponse a « a quoi correspond cet identifiant chez moi ? »."""

    row_id: str
    store_id: str
    product_id: str
    product_label: str
    source_label: str
    publishable: bool


class DecisionRequest(BaseModel):
    # approve : appliquer la proposition ; reject : la refuser ;
    # edit : appliquer une autre valeur, fournie dans `value`.
    action: str = Field(pattern="^(approve|reject|edit)$")
    value: str | None = None


class MetricsOut(BaseModel):
    runs_total: int
    runs_last_7d: int
    products_total: int
    publishable_rate: float
    pending_reviews: int
    learned_rules: int
    trend: list[dict[str, Any]] = Field(default_factory=list)
    anomalies_by_code: dict[str, int] = Field(default_factory=dict)
