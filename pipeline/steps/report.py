"""Etape finale — rapport de qualite et garde-fou de quarantaine.

Le taux de produits publiables est la seule metrique qui interesse vraiment le
metier : un produit est publiable s'il a un EAN exploitable, une image valide,
une taxonomie complete et une TVA coherente. Tout le reste du rapport sert a
comprendre POURQUOI ce taux bouge.

Garde-fou : si la proportion de lignes portant au moins une anomalie ERROR
depasse le seuil configure, le run passe en QUARANTINE et rien n'est integre.
Mieux vaut ne rien publier qu'inonder le referentiel d'un fichier corrompu.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import polars as pl

from pipeline.config import VatConfig
from pipeline.context import RunContext
from pipeline.logging import get_logger
from pipeline.models import Anomaly, Correction, RunReport, Severity, StepMetrics

log = get_logger(__name__)


def compute_publishable(df: pl.DataFrame, vat: VatConfig | None = None) -> pl.Series:
    """EAN exploitable + image valide + taxonomie complete + TVA legale.

    La coherence de TVA n'est evaluee qu'a partir de l'etape cross_checks ; tant
    qu'elle n'a pas tourne, la colonne est absente et on ne la compte pas.

    `vat` ajoute le controle de LEGALITE du taux, distinct de sa coherence :
    `vat_coherent` compare le taux a la regle de sa categorie et vaut True par
    defaut quand la categorie n'a pas de regle. Un taux a 33 % sur une feuille
    sans regle passait donc les deux controles precedents. Meme regle qu'a
    l'etage des fiches (`dedup.build_golden_records`) : les deux comptages
    doivent repondre a la meme question, sur des populations differentes.
    """
    condition = pl.col("ean_valid") & pl.col("url_ok") & pl.col("taxonomy_complete")
    if "url_checked" in df.columns:
        # « Publiable » exige une preuve positive : une image non
        # verifiee ne compte pas.
        condition = condition & pl.col("url_checked")
    if "vat_coherent" in df.columns:
        condition = condition & pl.col("vat_coherent")
    if vat is not None and "vat_rate" in df.columns:
        condition = condition & pl.col("vat_rate").is_in(vat.legal_rates_fr)
    return df.select(condition.alias("publishable"))["publishable"]


def build(
    df: pl.DataFrame,
    ctx: RunContext,
    corrections: list[Correction],
    anomalies: list[Anomaly],
    steps: list[StepMetrics],
    rows_in: int,
    records: list[Any] | None = None,
) -> RunReport:
    anomalies_by_code = Counter(a.code.value for a in anomalies)
    corrections_by_author = Counter(c.author.value for c in corrections)

    # DEUX mesures de plus, du meme genre que celles commentees plus bas, et le
    # meme piege : une CORRECTION porte sur une ligne, une DECISION sur une
    # valeur. L'etage LLM decide sur des libelles DISTINCTS puis applique sa
    # reponse a toutes les lignes qui portent le meme libelle : 273 decisions
    # deviennent 7 711 corrections. Les regles corrigent ligne par ligne, mais
    # repetent aussi la meme reparation (le meme EAN casse revient) : 1 428
    # corrections pour 842 decisions.
    #
    # Compter les lignes pour repondre a « qui corrige quoi » donnait donc 84 %
    # au LLM et 16 % aux regles, soit l'inverse exact de qui a tranche quoi.
    # Une decision = une transformation distincte (champ, valeur d'origine,
    # valeur corrigee), definie identiquement pour les deux etages : sans quoi
    # on comparerait encore deux unites differentes.
    decisions_by_author: Counter[str] = Counter()
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for c in corrections:
        key = (c.author.value, c.field_name, c.old_value, c.new_value)
        if key not in seen:
            seen.add(key)
            decisions_by_author[c.author.value] += 1

    rows_with_error = {a.row_id for a in anomalies if a.severity is Severity.ERROR}
    error_rate = len(rows_with_error) / df.height if df.height else 0.0

    # A-t-on reellement interroge le reseau ? Le signal doit etre explicite :
    # une URL vide compte comme « verifiee » (c'est un fait etabli), donc
    # regarder la colonne repondrait oui meme sans le moindre appel HTTP.
    urls_verified = ctx.network_enabled
    publishable = compute_publishable(df, vat=ctx.config.vat)
    publishable_rate = float(publishable.sum()) / df.height if df.height else 0.0

    # DEUX mesures differentes, qu'il ne faut jamais confondre :
    #
    # - `publishable_rate` porte sur les LIGNES recues. C'est la sante du
    #   fichier depose, et c'est elle qui arme le garde-fou de quarantaine.
    # - `records_publishable` porte sur les FICHES produites. C'est ce qui part
    #   reellement vers la marketplace, et c'est le seul chiffre que l'export
    #   confirme ligne par ligne.
    #
    # Les deux divergent des qu'un conflit apparait au REGROUPEMENT : dix
    # produits dont chacun porte huit lignes a la mauvaise TVA donnent 81,5 %
    # de lignes coherentes et ZERO fiche publiable. Afficher le premier chiffre
    # sous le mot « produits » racontait donc l'inverse de l'export.
    records_total = len(records) if records is not None else 0
    records_publishable = (
        sum(1 for r in records if getattr(r, "publishable", False)) if records else 0
    )

    thresholds = ctx.config.thresholds
    quarantined = False
    reason: str | None = None
    if error_rate > thresholds.quarantine_anomaly_rate:
        quarantined = True
        reason = (
            f"{error_rate:.1%} des lignes portent une anomalie bloquante "
            f"(seuil : {thresholds.quarantine_anomaly_rate:.1%})"
        )
    elif publishable_rate < thresholds.min_publishable_rate and urls_verified:
        # Ce garde-fou ne vaut que si les images ont ete verifiees : sans
        # verification le taux publiable est structurellement nul, et mettre un
        # fichier en quarantaine parce qu'on a choisi de ne pas verifier ses
        # images serait punir l'utilisateur pour sa propre option.
        quarantined = True
        reason = (
            f"taux de produits publiables {publishable_rate:.1%} sous le plancher "
            f"{thresholds.min_publishable_rate:.1%}"
        )

    report = RunReport(
        run_id=ctx.run_id,
        store_id=ctx.store_id,
        source_file=str(ctx.source_file),
        file_sha256=ctx.file_sha256,
        started_at=ctx.now(),
        finished_at=ctx.now(),
        quarantined=quarantined,
        quarantine_reason=reason,
        rows_in=rows_in,
        rows_out=df.height,
        steps=steps,
        anomalies_by_code=dict(sorted(anomalies_by_code.items())),
        corrections_by_author=dict(sorted(corrections_by_author.items())),
        decisions_by_author=dict(sorted(decisions_by_author.items())),
        publishable_rate=round(publishable_rate, 4),
        records_total=records_total,
        records_publishable=records_publishable,
        extra={
            "urls_verified": urls_verified,
            "rows_with_blocking_anomaly": len(rows_with_error),
            "error_rate": round(error_rate, 4),
            "distinct_labels": int(df["nom"].n_unique()),
        },
    )

    if quarantined:
        log.error("run.quarantined", reason=reason, publishable_rate=publishable_rate)
    else:
        log.info(
            "run.done",
            publishable_rate=publishable_rate,
            corrections=len(corrections),
            anomalies=len(anomalies),
        )
    return report
