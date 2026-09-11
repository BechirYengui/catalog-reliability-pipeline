"""Schema du referentiel.

Trois exigences dictent ce schema :

1. **Tracabilite** — `product_corrections` conserve pour chaque champ la valeur
   d'origine, la valeur corrigee, l'auteur (RULE|LLM|HUMAN) et l'horodatage.
   On doit pouvoir expliquer n'importe quelle valeur du referentiel et revenir
   en arriere. C'est non negociable.
2. **Reprise sur panne** — `run_steps` porte le statut de CHAQUE etape. Un run
   interrompu reprend a l'etape suivante au lieu de tout refaire, et
   l'interface sait montrer ou en est un fichier en cours de traitement.
3. **Idempotence** — la contrainte d'unicite (store_id, file_sha256) fait que
   rejouer exactement le meme fichier ne cree pas un second referentiel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


# JSONB en postgres, JSON ailleurs : les tests tournent sur SQLite pour rester
# rapides et sans dependance a un serveur.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class User(Base):
    """Authentification applicative.

    Un basic auth HTTP ne suffit pas : valider un taux de TVA et relire un
    libelle n'engagent pas la meme responsabilite. Le role porte cette
    distinction, et l'API la fait respecter.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # 'admin' peut valider une TVA ; 'reviewer' traite le reste.
    role: Mapped[str] = mapped_column(String(20), default="reviewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    runs: Mapped[list[IngestionRun]] = relationship(back_populates="store")


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        # Idempotence : le meme fichier, pour le meme magasin, ne peut pas
        # produire deux runs integres.
        UniqueConstraint("store_id", "file_sha256", name="uq_run_store_file"),
        Index("ix_runs_started", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(512))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)

    # PENDING | RUNNING | COMPLETED | QUARANTINE | FAILED
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    current_step: Mapped[str | None] = mapped_column(String(40))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)

    rows_in: Mapped[int] = mapped_column(Integer, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, default=0)
    publishable_rate: Mapped[float] = mapped_column(Float, default=0.0)
    report: Mapped[dict] = mapped_column(JSONType, default=dict)

    network_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 'live' : reponse immediate, plein tarif. 'batch' : traitement asynchrone
    # a moitie prix, pour un run que personne ne regarde. Ce n'est pas un
    # reglage technique mais un arbitrage cout/delai, donc il appartient a
    # l'utilisateur et se choisit au depot.
    llm_mode: Mapped[str] = mapped_column(String(10), default="live", server_default="live")
    triggered_by: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    store: Mapped[Store] = relationship(back_populates="runs")
    steps: Mapped[list[RunStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunStep.position"
    )


class RunStep(Base):
    """Etat d'une etape pour un run donne.

    C'est cette table qui permet de repondre a « ou en est mon fichier ? » et
    d'afficher les resultats intermediaires, etage par etage. `counters` porte
    le detail propre a chaque etape (EAN repares, URLs mortes, paires de
    deduplication restantes...).
    """

    __tablename__ = "run_steps"
    __table_args__ = (UniqueConstraint("run_id", "step", name="uq_step_per_run"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(Integer, default=0)
    # PENDING | RUNNING | DONE | SKIPPED | FAILED
    status: Mapped[str] = mapped_column(String(20), default="PENDING")

    rows_in: Mapped[int] = mapped_column(Integer, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    corrections: Mapped[int] = mapped_column(Integer, default=0)
    anomalies: Mapped[int] = mapped_column(Integer, default=0)
    counters: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[IngestionRun] = relationship(back_populates="steps")


class Product(Base):
    """Golden record : la fiche unique issue de la fusion des quasi-doublons."""

    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_store_label", "store_id", "label_normalized"),
        Index("ix_products_status", "status"),
        # L'identite d'un produit dans un magasin. Le depot du lendemain met a
        # jour la fiche existante au lieu d'en creer une nouvelle, sans quoi
        # l'identifiant changerait chaque jour et emporterait avec lui le
        # rattachement des lignes du magasin et l'historique des corrections.
        UniqueConstraint("store_id", "product_key", name="uq_products_store_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), index=True)
    # Le dernier run qui a touche la fiche, pas celui qui l'a creee.
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # Cle naturelle du produit chez ce magasin, stable d'un depot a l'autre.
    product_key: Mapped[str] = mapped_column(String(512), default="", server_default="", index=True)

    label: Mapped[str] = mapped_column(String(512), default="")
    label_normalized: Mapped[str] = mapped_column(String(512), default="", index=True)
    label_enriched: Mapped[str | None] = mapped_column(String(512))

    ean: Mapped[str | None] = mapped_column(String(20), index=True)
    internal_code: Mapped[str | None] = mapped_column(String(40))
    url_image: Mapped[str | None] = mapped_column(Text)
    url_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    # Distingue « image morte » de « image jamais verifiee » : ne pas
    # savoir et savoir que c'est casse appellent des actions differentes.
    url_checked: Mapped[bool] = mapped_column(Boolean, default=False)

    taxonomy_1: Mapped[str | None] = mapped_column(String(120))
    taxonomy_2: Mapped[str | None] = mapped_column(String(120))
    taxonomy_3: Mapped[str | None] = mapped_column(String(120))
    taxonomy_4: Mapped[str | None] = mapped_column(String(120))

    vat_rate: Mapped[float | None] = mapped_column(Float)
    brand: Mapped[str | None] = mapped_column(String(120))
    quantity_value: Mapped[float | None] = mapped_column(Float)
    quantity_unit: Mapped[str | None] = mapped_column(String(10))
    origin: Mapped[str | None] = mapped_column(String(120))

    # VALIDATED | AUTO_CORRECTED | NEEDS_REVIEW | REJECTED
    status: Mapped[str] = mapped_column(String(20), default="NEEDS_REVIEW")
    publishable: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Nombre de lignes brutes fusionnees dans ce golden record.
    source_rows: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ProductSourceRow(Base):
    """Le fil entre une ligne du fichier magasin et la fiche qui la represente.

    Fusionner sans conserver ce fil reviendrait a couper le lien avec le
    systeme du magasin. Sa caisse continue de parler avec SES identifiants :
    quand elle annonce « stock de 4f2a-91 = 12 », il faut pouvoir repondre que
    4f2a-91 est le whisky ecossais 70 cl. Le referentiel compte une dizaine de
    fiches, mais les 10 000 lignes gardent chacune son rattachement.

    Une ligne par ligne source, remplacee a chaque depot du magasin : elle
    decrit l'etat courant, l'historique des corrections vit ailleurs.
    """

    __tablename__ = "product_source_rows"
    __table_args__ = (
        # La question posee par le systeme du magasin : « a quoi correspond
        # cet identifiant chez moi ? »
        Index("ix_source_rows_store_row", "store_id", "row_id"),
        Index("ix_source_rows_product", "product_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    product_id: Mapped[str] = mapped_column(String(36))
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    # L'identifiant tel que le magasin l'ecrit, jamais reecrit par le pipeline.
    row_id: Mapped[str] = mapped_column(String(64))
    # Le libelle d'origine de CETTE ligne, avant fusion et avant enrichissement.
    # Sans lui, on saurait qu'une ligne est rattachee sans pouvoir montrer
    # pourquoi, et une fusion erronee resterait invisible.
    source_label: Mapped[str] = mapped_column(String(512), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProductMerge(Base):
    """Une fusion decidee par un humain, avec de quoi la defaire.

    Fusionner detruit une fiche. Sans garder ce qu'elle contenait, « annuler »
    ne pourrait que supprimer la regle apprise en laissant le referentiel dans
    l'etat fusionne jusqu'au lendemain — l'utilisateur cliquerait et ne verrait
    rien changer. On garde donc l'instantane de la fiche absorbee et la liste
    des lignes du magasin deplacees, ce qui suffit a tout remettre en place.

    C'est aussi ce qui rend la decision consultable : qui a fusionne quoi,
    quand, et est-ce encore en vigueur.
    """

    __tablename__ = "product_merges"
    __table_args__ = (Index("ix_merges_store", "store_id", "undone_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    survivor_id: Mapped[str] = mapped_column(String(36), index=True)
    survivor_label: Mapped[str] = mapped_column(String(512), default="")
    absorbed_label: Mapped[str] = mapped_column(String(512), default="")
    # La fiche absorbee, champ par champ, telle qu'elle etait juste avant.
    absorbed: Mapped[dict] = mapped_column(JSONType, default=dict)
    # Les identifiants des lignes du magasin qui ont suivi la fusion : sans
    # eux, annuler rendrait la fiche sans ses lignes.
    moved_row_ids: Mapped[dict] = mapped_column(JSONType, default=dict)

    user_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[str | None] = mapped_column(String(36))


class ProductOverride(Base):
    """Une valeur corrigee a la main, qui doit tenir au depot du lendemain.

    Sans cette table, un bouton « modifier » serait un piege : la correction
    s'afficherait, puis le depot du matin recalculerait la fiche depuis le CSV
    et l'effacerait sans prevenir. C'est exactement le probleme que la fusion
    manuelle a deja du resoudre.

    Une correction est donc stockee comme une DECISION portant sur la cle
    naturelle du produit chez ce magasin, pas comme un etat de la fiche. Elle
    est rejouee a chaque integration, APRES le LLM : l'humain gagne toujours
    contre le modele, jamais l'inverse.

    La TVA n'entre volontairement pas ici. Elle passe par la file de revue,
    ou seul un administrateur peut trancher, parce que l'erreur a une
    consequence fiscale. Un champ libre contournerait ce controle.
    """

    __tablename__ = "product_overrides"
    __table_args__ = (
        # Une seule valeur en vigueur par champ et par produit.
        UniqueConstraint(
            "store_id", "product_key", "field_name", name="uq_override_store_key_field"
        ),
        Index("ix_overrides_store", "store_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    # La cle naturelle, pas l'identifiant de la fiche : c'est elle qui survit
    # d'un depot a l'autre.
    product_key: Mapped[str] = mapped_column(String(512))
    field_name: Mapped[str] = mapped_column(String(40))
    # La valeur telle que le pipeline l'avait calculee, pour pouvoir revenir.
    previous_value: Mapped[str | None] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text)

    user_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ProductCorrection(Base):
    """Journal d'audit. Une ligne par valeur modifiee, sans exception."""

    __tablename__ = "product_corrections"
    __table_args__ = (Index("ix_corrections_run", "run_id", "field_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[str | None] = mapped_column(String(36), index=True)
    row_id: Mapped[str] = mapped_column(String(64), index=True)

    field_name: Mapped[str] = mapped_column(String(60))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(10))  # RULE | LLM | HUMAN
    rule: Mapped[str] = mapped_column(String(120), default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    user_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RunAnomaly(Base):
    __tablename__ = "run_anomalies"
    __table_args__ = (Index("ix_anomalies_run_code", "run_id", "code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    row_id: Mapped[str] = mapped_column(String(64))
    field_name: Mapped[str] = mapped_column(String(60))
    code: Mapped[str] = mapped_column(String(60), index=True)
    severity: Mapped[str] = mapped_column(String(10))
    detail: Mapped[str] = mapped_column(Text, default="")
    # La valeur fautive et le libelle de la ligne. Sans eux, cent anomalies d'un
    # meme code s'affichent en cent lignes identiques : on sait combien, jamais
    # lesquelles, et l'utilisateur ne peut pas les retrouver dans son fichier.
    value: Mapped[str] = mapped_column(String(512), default="", server_default="")
    label: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReviewTask(Base):
    """Une decision humaine porte sur un GROUPE de lignes, jamais sur une ligne.

    C'est ce qui rend la revue tenable : sur l'echantillon, les 945 lignes de
    whisky produisent une seule question (« 32 lignes a un taux different de
    20 % : appliquer 20 % ? ») au lieu de 32 arbitrages isoles.
    """

    __tablename__ = "review_tasks"
    __table_args__ = (Index("ix_review_status", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)

    kind: Mapped[str] = mapped_column(String(40))  # vat_mismatch | taxonomy | duplicate | label
    field_name: Mapped[str] = mapped_column(String(60), default="")
    title: Mapped[str] = mapped_column(String(512))
    question: Mapped[str] = mapped_column(Text, default="")

    current_value: Mapped[str | None] = mapped_column(Text)
    proposed_value: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(10), default="RULE")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    affected_rows: Mapped[int] = mapped_column(Integer, default=0)
    context: Mapped[dict] = mapped_column(JSONType, default=dict)

    # pending | approved | rejected | edited
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # Seul un admin peut trancher une tache qui touche a la TVA.
    requires_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    decided_by: Mapped[str | None] = mapped_column(String(36))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LearnedRule(Base):
    """La boucle d'apprentissage.

    Chaque decision humaine validee s'inscrit ici, pour que la meme question ne
    soit jamais posee deux fois. C'est la fleche en pointilles du schema.
    """

    __tablename__ = "learned_rules"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_learned_scope_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(40))  # category_vat | label_rewrite | taxonomy
    key: Mapped[str] = mapped_column(String(512))
    value: Mapped[str] = mapped_column(Text)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LlmCache(Base):
    """Cache des appels LLM, keye par sha256(prompt normalise + modele)."""

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(80))
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[dict] = mapped_column(JSONType, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LlmSpend(Base):
    """Registre des dépenses LLM — une ligne par run qui a réellement appelé.

    Le plafond par run ne suffit pas : dix runs sous le plafond dépassent
    quand même l'enveloppe. Ce registre porte le cumul, et c'est lui qui coupe
    l'API une fois l'enveloppe épuisée.

    On enregistre les jetons en plus du montant : si un tarif change, le coût
    historique reste recalculable au lieu d'être figé sur une valeur devenue
    fausse.
    """

    __tablename__ = "llm_spend"
    __table_args__ = (Index("ix_spend_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    store_id: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(80))

    calls_real: Mapped[int] = mapped_column(Integer, default=0)
    calls_cached: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UrlCheckCache(Base):
    """Statut d'une URL, avec duree de validite.

    Sans ce cache, on relance 9 000 requetes HEAD chaque jour vers les memes
    images. Avec, un run quotidien ne verifie que les URLs nouvelles.
    """

    __tablename__ = "url_check_cache"

    url: Mapped[str] = mapped_column(String(1024), primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(60), default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
