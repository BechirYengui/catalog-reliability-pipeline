"""Etape 6 — score et aiguillage.

Chaque fiche ressort avec un statut, et le seuil depend du CHAMP. Un libelle
mal reecrit se voit en rayon et se corrige ; un taux de TVA faux a une
consequence comptable. Les deux ne meritent pas la meme prudence.

    VALIDATED        rien a signaler, publiable en l'etat
    AUTO_CORRECTED   corrige par une regle sure ou par le LLM au-dessus du seuil
    NEEDS_REVIEW     un humain doit trancher
    REJECTED         inexploitable, ne sera pas publie

Deux regles absolues, verrouillees par des tests :

- **Toute fiche touchant a la TVA passe en NEEDS_REVIEW**, quelle que soit la
  confiance. Meme a 1,0.
- **Une proposition du LLM sous son seuil n'est pas appliquee** : elle est
  conservee comme proposition et la fiche part en revue.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import polars as pl

from pipeline.context import RunContext
from pipeline.logging import get_logger
from pipeline.models import Anomaly, AnomalyCode, ProductStatus, Severity, StepMetrics, StepResult

log = get_logger(__name__)

STEP = "scoring_routing"

# Cet etage a longtemps porte un ensemble `BLOCKING` de codes d'anomalie qui
# n'etait lu nulle part. Les codes qu'il listait sont desormais couverts, et
# chacun par la donnee portee par la fiche plutot que par un code a rapprocher :
#
#   LABEL_EMPTY       -> `record.label` vide, teste en premier ci-dessous
#   EAN_INVALID       -> `record.ean` est vide (les controles par champ ne
#                        laissent passer que les EAN valides), donc `publishable`
#                        est faux et la fiche part en revue
#   URL_MALFORMED     -> `record.url_ok` est faux, meme consequence
#   VAT_MISSING       -> teste explicitement ci-dessous
#   VAT_ILLEGAL_RATE  -> teste explicitement ci-dessous
#
# Un ensemble declare et jamais consulte est pire qu'une absence de garde-fou :
# il donne l'apparence d'une protection. Les codes eux-memes restent produits
# par les controles par champ, ou ils servent au rapport et a l'interface.


def route(record: Any, ctx: RunContext) -> tuple[ProductStatus, float, list[str]]:
    """Statut, confiance et raisons. Les raisons sont affichees a l'humain :
    un statut sans motif est une decision qu'on ne peut pas contester."""
    thresholds = ctx.config.thresholds
    reasons: list[str] = []

    # --- rejet : la fiche n'est pas exploitable --------------------------
    if not record.label.strip():
        return ProductStatus.REJECTED, 0.0, ["libellé vide : rien à publier"]

    # --- revue obligatoire : la TVA ---------------------------------------
    # Non negociable. Une TVA fausse a une consequence fiscale ; une validation
    # humaine coute trois secondes.
    if record.vat_conflict:
        reasons.append("taux de TVA en conflit dans le groupe — décision humaine requise")
        return ProductStatus.NEEDS_REVIEW, 1.0, reasons

    # Un taux hors bareme, ou absent. Le conflit ci-dessus ne l'attrape pas :
    # il ne se declenche que si les lignes du groupe DIVERGENT, ou si une regle
    # de categorie existe pour cette feuille. Une feuille sans regle laisse
    # `vat_coherent` a True par defaut, donc un taux a 33 % — qui n'existe pas
    # en France — ressortait « aucune anomalie », publiable, sans qu'aucun
    # humain ne soit appele.
    #
    # Le pipeline ne corrige toujours pas : il ne propose meme pas de valeur.
    # Il refuse simplement de declarer bonne une fiche dont il sait le taux
    # faux. « Jamais corrigee automatiquement » n'a de sens que si quelqu'un
    # tranche derriere.
    if record.vat_rate is None:
        reasons.append("taux de TVA absent — décision humaine requise")
        return ProductStatus.NEEDS_REVIEW, 1.0, reasons
    if not ctx.config.vat.is_legal(record.vat_rate):
        legaux = ", ".join(f"{t:g}" for t in ctx.config.vat.legal_rates_fr)
        reasons.append(
            f"taux de TVA à {record.vat_rate:g} % hors des taux légaux français "
            f"({legaux}) — décision humaine requise"
        )
        return ProductStatus.NEEDS_REVIEW, 1.0, reasons

    # --- revue : proposition LLM sous son seuil ---------------------------
    confidence = float(getattr(record, "llm_confidence", 0.0) or 0.0)
    proposal = getattr(record, "label_enriched", "") or ""
    if proposal and proposal != record.label and confidence < thresholds.min_label_confidence:
        reasons.append(
            f"reformulation proposée à {confidence:.0%} de confiance, "
            f"sous le seuil de {thresholds.min_label_confidence:.0%}"
        )
        return ProductStatus.NEEDS_REVIEW, confidence, reasons

    if record.taxonomy_conflict:
        reasons.append("les lignes fusionnées ne s'accordent pas sur la catégorie")
        return ProductStatus.NEEDS_REVIEW, 0.5, reasons

    # --- publiable ou incomplet -------------------------------------------
    if not record.publishable:
        missing = []
        if not record.ean:
            missing.append("EAN exploitable")
        if not record.url_ok:
            missing.append("image vérifiée" if not record.url_checked else "image valide")
        if not all(record.taxonomy):
            missing.append("taxonomie complète")
        reasons.append("incomplet : il manque " + ", ".join(missing))
        return ProductStatus.NEEDS_REVIEW, 0.6, reasons

    # Une fiche corrigee reste corrigee : le distinguer d'une fiche intacte
    # permet de mesurer ce que le pipeline apporte reellement.
    if proposal and proposal != record.label:
        reasons.append(f"libellé reformulé à {confidence:.0%} de confiance")
        return ProductStatus.AUTO_CORRECTED, confidence, reasons

    return ProductStatus.VALIDATED, 1.0, ["aucune anomalie"]


def run(df: pl.DataFrame, ctx: RunContext, records: list[Any] | None = None) -> StepResult:
    started = time.perf_counter()

    if not records:
        metrics = StepMetrics(step=STEP, rows_in=df.height, rows_out=df.height)
        metrics.duration_ms = (time.perf_counter() - started) * 1000
        log.info("step.skipped", step=STEP, reason="aucun golden record en entree")
        return StepResult(df=df, metrics=metrics)

    anomalies: list[Anomaly] = []
    counts: Counter[str] = Counter()

    for record in records:
        status, confidence, reasons = route(record, ctx)
        record.status = status.value
        record.confidence = confidence
        record.routing_reasons = reasons
        counts[status.value] += 1

        if status is ProductStatus.REJECTED:
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=record.row_ids[0] if record.row_ids else record.key,
                    field_name="nom",
                    code=AnomalyCode.LABEL_EMPTY,
                    severity=Severity.ERROR,
                    detail="; ".join(reasons),
                )
            )

    counters = dict(counts)
    counters["records"] = len(records)
    # Le taux d'automatisation : la part traitee sans intervention humaine.
    # C'est ce chiffre qui dit si le pipeline tient sa promesse.
    automated = counts[ProductStatus.VALIDATED.value] + counts[ProductStatus.AUTO_CORRECTED.value]
    counters["automation_rate_pct"] = round(100 * automated / max(len(records), 1))

    metrics = StepMetrics(
        step=STEP,
        rows_in=len(records),
        rows_out=len(records),
        duration_ms=(time.perf_counter() - started) * 1000,
        counters=counters,
    )
    log.info("step.done", step=STEP, **counters)
    return StepResult(
        df=df, anomalies=anomalies, metrics=metrics, payload={"golden_records": records}
    )
