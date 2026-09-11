"""Etape 2 — controles champ par champ.

Chaque champ est traite independamment ; les incoherences ENTRE champs (TVA vs
categorie, taxonomie vs libelle) relevent de l'etape `cross_checks`.

Ce qui est corrige automatiquement ici est uniquement ce qui est **sur par
construction** : un EAN n'est repare que si le checksum GS1 devient valide, une
URL n'est reparee que sur une faute de frappe de schema referencee.
"""

from __future__ import annotations

import asyncio
import time

import polars as pl

from pipeline.context import RunContext
from pipeline.logging import get_logger
from pipeline.models import (
    Anomaly,
    AnomalyCode,
    Author,
    Correction,
    Severity,
    StepMetrics,
    StepResult,
)
from pipeline.normalize import analyse_ean, normalize_label
from pipeline.steps.url_checker import UrlChecker, is_supported, repair_url

log = get_logger(__name__)

STEP = "field_checks"

_EAN_STATUS_TO_ANOMALY = {
    "missing": (AnomalyCode.EAN_MISSING, Severity.WARNING),
    "padded": (AnomalyCode.EAN_PADDED, Severity.INFO),
    "unpadded": (AnomalyCode.EAN_UNPADDED, Severity.INFO),
    "not_a_gtin": (AnomalyCode.EAN_NOT_A_GTIN, Severity.WARNING),
    "internal_gs1": (AnomalyCode.EAN_INTERNAL_GS1, Severity.INFO),
    "gtin14": (AnomalyCode.EAN_GTIN14, Severity.WARNING),
    "invalid": (AnomalyCode.EAN_INVALID, Severity.ERROR),
}

# Codes qui sont des identifiants magasin legitimes, pas des EAN : ils sont
# routes vers `internal_code` au lieu d'etre corriges ou jetes.
_INTERNAL_CODE_STATUSES = ("not_a_gtin", "internal_gs1")

TAXONOMY_COLUMNS = (
    "taxonomie_niveau_1",
    "taxonomie_niveau_2",
    "taxonomie_niveau_3",
    "taxonomie_niveau_4",
)


def _parse_rate(raw: str | None) -> float | None:
    value = (raw or "").strip().replace(",", ".").rstrip("%")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def run(df: pl.DataFrame, ctx: RunContext) -> StepResult:
    started = time.perf_counter()
    corrections: list[Correction] = []
    anomalies: list[Anomaly] = []
    counters: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        counters[key] = counters.get(key, 0) + n

    row_ids = df["id_produit"].to_list()
    # Le libelle de la ligne, pour que chaque anomalie dise DE QUEL produit elle
    # parle. Cent lignes « prefixe GS1 reserve » sans nom ni valeur ne se
    # distinguent pas les unes des autres, et ne se retrouvent pas dans le CSV.
    label_of = dict(zip(row_ids, df["nom"].to_list(), strict=True))

    # ------------------------------------------------------------------ EAN
    ean_values: list[str] = []
    internal_codes: list[str] = []
    ean_valid: list[bool] = []
    for row_id, raw in zip(row_ids, df["ean"].to_list(), strict=True):
        verdict = analyse_ean(raw)
        bump(f"ean_{verdict.status}")

        if verdict.status in ("padded", "unpadded"):
            corrections.append(
                Correction(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="ean",
                    old_value=raw,
                    new_value=verdict.value,
                    author=Author.RULE,
                    rule=f"field_checks.ean.{verdict.status}",
                    created_at=ctx.now(),
                )
            )

        if verdict.status != "ok":
            code, severity = _EAN_STATUS_TO_ANOMALY[verdict.status]
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="ean",
                    code=code,
                    severity=severity,
                    detail=verdict.detail,
                    value=str(raw or ""),
                    label=label_of.get(row_id, "") or "",
                )
            )

        # Un code non-GTIN est route vers un champ dedie, pas ecrase : c'est une
        # donnee valide du magasin, simplement pas un EAN.
        if verdict.status in _INTERNAL_CODE_STATUSES:
            ean_values.append("")
            internal_codes.append(verdict.value or "")
            ean_valid.append(False)
        elif verdict.status == "gtin14":
            # Le code reste sur la ligne — il est reel — mais il ne vaut pas
            # comme EAN consommateur : il ne remontera pas dans la fiche, et un
            # produit qui n'a que celui-la n'est pas publiable. « Publiable »
            # est une affirmation, elle exige le bon identifiant.
            ean_values.append(verdict.value or "")
            internal_codes.append("")
            ean_valid.append(False)
        elif verdict.status in ("ok", "padded", "unpadded"):
            ean_values.append(verdict.value or "")
            internal_codes.append("")
            ean_valid.append(True)
        else:
            ean_values.append("")
            internal_codes.append("")
            ean_valid.append(False)

    # ------------------------------------------------------------------ URL
    repaired_urls: list[str] = []
    for row_id, raw in zip(row_ids, df["url_image"].to_list(), strict=True):
        fixed, rule = repair_url(raw)
        repaired_urls.append(fixed)
        if rule:
            bump("url_scheme_repaired")
            corrections.append(
                Correction(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="url_image",
                    old_value=raw,
                    new_value=fixed,
                    author=Author.RULE,
                    rule=f"field_checks.url.{rule}",
                    created_at=ctx.now(),
                )
            )
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="url_image",
                    code=AnomalyCode.URL_SCHEME_REPAIRED,
                    severity=Severity.INFO,
                    detail=f"{raw!r} -> {fixed!r}",
                    value=str(raw or ""),
                    label=label_of.get(row_id, "") or "",
                )
            )

    checkable = [u for u in repaired_urls if u and is_supported(u)]
    statuses = {}
    # La graine vient des runs precedents, le frais y retourne : c'est ce qui
    # fait qu'un depot quotidien ne verifie que les URLs nouvelles.
    checker = UrlChecker(ctx.config.http, seed=ctx.url_status_seed, fresh_out=ctx.url_status_fresh)
    if ctx.network_enabled and ctx.config.http.enabled and checkable:
        statuses = asyncio.run(checker.check_many(checkable))
    elif checkable:
        log.info("url.check_skipped", reason="network disabled", urls=len(checkable))

    url_ok: list[bool] = []
    # Verifiee ou non : « publiable » est une AFFIRMATION, elle exige une
    # preuve positive. Sans verification reseau on ne sait pas, et ne pas
    # savoir n'est pas la meme chose que savoir que c'est bon.
    url_checked: list[bool] = []
    for row_id, url in zip(row_ids, repaired_urls, strict=True):
        if not url:
            url_ok.append(False)
            url_checked.append(True)  # une URL vide est un fait etabli
            bump("url_missing")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="url_image",
                    code=AnomalyCode.URL_MISSING,
                    severity=Severity.WARNING,
                    detail="aucune image",
                    label=label_of.get(row_id, "") or "",
                )
            )
            continue
        if not is_supported(url):
            url_ok.append(False)
            url_checked.append(True)  # un schema inexploitable est un fait etabli
            bump("url_malformed")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="url_image",
                    code=AnomalyCode.URL_MALFORMED,
                    severity=Severity.ERROR,
                    detail=f"schema non exploitable : {url!r}",
                    value=str(url or ""),
                    label=label_of.get(row_id, "") or "",
                )
            )
            continue

        status = statuses.get(url)
        if status is None:
            # Reseau desactive : etat inconnu. On ne declare pas l'URL morte
            # (ce serait une anomalie inventee), mais on ne la compte pas non
            # plus comme publiable — l'inconnu n'est pas une preuve.
            url_ok.append(False)
            url_checked.append(False)
            continue
        url_ok.append(status.ok)
        url_checked.append(True)
        if not status.ok:
            is_dns = status.reason == "domain_unresolvable"
            bump("url_domain_unresolvable" if is_dns else "url_unreachable")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="url_image",
                    code=(
                        AnomalyCode.URL_DOMAIN_UNRESOLVABLE
                        if is_dns
                        else AnomalyCode.URL_UNREACHABLE
                    ),
                    severity=Severity.WARNING,
                    detail=status.reason,
                    value=str(url or ""),
                    label=label_of.get(row_id, "") or "",
                )
            )

    # ------------------------------------------------------------------ TVA
    vat_rates: list[float | None] = []
    for row_id, raw in zip(row_ids, df["tva"].to_list(), strict=True):
        rate = _parse_rate(raw)
        vat_rates.append(rate)
        if rate is None:
            bump("vat_missing")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="tva",
                    code=AnomalyCode.VAT_MISSING,
                    severity=Severity.ERROR,
                    detail=f"valeur illisible : {raw!r}",
                    value=str(raw or ""),
                    label=label_of.get(row_id, "") or "",
                )
            )
        elif not ctx.config.vat.is_legal(rate):
            # Jamais corrige, meme si un taux voisin semble evident : la TVA est
            # la seule donnee dont une erreur automatique a un cout fiscal.
            bump("vat_illegal")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="tva",
                    code=AnomalyCode.VAT_ILLEGAL_RATE,
                    severity=Severity.ERROR,
                    detail=f"{rate} hors taux legaux FR",
                    value=str(rate),
                    label=label_of.get(row_id, "") or "",
                )
            )

    # ------------------------------------------------------------ taxonomie
    # Canonicalisation d'abord : « ÉPICERIE SUCREE » et « EPICERIE SUCREE » sont
    # la meme categorie et ne doivent pas produire deux entrees du referentiel.
    canonical = ctx.config.taxonomy.canonical_by_level
    taxonomy_complete: list[bool] = []
    levels = [df[c].to_list() for c in TAXONOMY_COLUMNS]
    canonical_levels: list[list[str]] = [[], [], [], []]

    for idx, row_id in enumerate(row_ids):
        values = [(levels[lvl][idx] or "").strip() for lvl in range(4)]
        for lvl, value in enumerate(values):
            reference = canonical[lvl].get(normalize_label(value)) if value else None
            if reference and reference != value:
                values[lvl] = reference
                bump("taxonomy_canonicalized")
                corrections.append(
                    Correction(
                        run_id=ctx.run_id,
                        row_id=row_id,
                        field_name=TAXONOMY_COLUMNS[lvl],
                        old_value=value,
                        new_value=reference,
                        author=Author.RULE,
                        rule="field_checks.taxonomy.canonicalize",
                        created_at=ctx.now(),
                    )
                )
            canonical_levels[lvl].append(values[lvl])

        filled = [bool(v) for v in values]
        taxonomy_complete.append(all(filled))

        if not all(filled):
            # Un trou (niveau vide suivi d'un niveau rempli) est reparable sans
            # LLM ; une troncature simple demandera un enrichissement.
            has_gap = any(not filled[i] and any(filled[i + 1 :]) for i in range(3))
            bump("taxonomy_gap" if has_gap else "taxonomy_incomplete")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="taxonomie",
                    code=(AnomalyCode.TAXONOMY_GAP if has_gap else AnomalyCode.TAXONOMY_INCOMPLETE),
                    severity=Severity.WARNING,
                    detail=" > ".join(v or "?" for v in values),
                    value=" > ".join(v or "?" for v in values),
                    label=label_of.get(row_id, "") or "",
                )
            )
        elif tuple(values) not in ctx.config.taxonomy.known_paths:
            bump("taxonomy_unknown_path")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="taxonomie",
                    code=AnomalyCode.TAXONOMY_UNKNOWN_PATH,
                    severity=Severity.WARNING,
                    detail=" > ".join(values),
                    value=" > ".join(values),
                    label=label_of.get(row_id, "") or "",
                )
            )

    out = df.with_columns(
        *[
            pl.Series(column, canonical_levels[lvl], dtype=pl.String)
            for lvl, column in enumerate(TAXONOMY_COLUMNS)
        ],
        pl.Series("ean", ean_values, dtype=pl.String),
        pl.Series("internal_code", internal_codes, dtype=pl.String),
        pl.Series("ean_valid", ean_valid, dtype=pl.Boolean),
        pl.Series("url_image", repaired_urls, dtype=pl.String),
        pl.Series("url_ok", url_ok, dtype=pl.Boolean),
        pl.Series("url_checked", url_checked, dtype=pl.Boolean),
        pl.Series("vat_rate", vat_rates, dtype=pl.Float64),
        pl.Series("taxonomy_complete", taxonomy_complete, dtype=pl.Boolean),
    )

    counters["url_dns_lookups"] = checker.dns_lookups
    counters["url_http_requests"] = checker.http_requests

    metrics = StepMetrics(
        step=STEP,
        rows_in=df.height,
        rows_out=out.height,
        duration_ms=(time.perf_counter() - started) * 1000,
        counters=counters,
    )
    log.info("step.done", step=STEP, rows_in=df.height, rows_out=out.height, **counters)
    return StepResult(df=out, corrections=corrections, anomalies=anomalies, metrics=metrics)
