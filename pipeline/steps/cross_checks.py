"""Etape 3 — coherence ENTRE les champs.

C'est l'etage qui porte la valeur reelle du controle de TVA. Le controle
syntaxique de l'etape precedente ne trouve rien sur ce fichier : les 4 taux
presents sont tous legaux. Pourtant plus d'une centaine de lignes sont fausses,
parce qu'un taux legal peut etre applique au mauvais produit — du whisky a
5,5 %, de la lessive a 2,1 %.

Trois controles :
1. taxonomie trouee (n2 vide, n3/n4 remplis) -> comblee par deduction, sans LLM ;
2. taxonomie contredisant le libelle -> signalee, jamais corrigee seule ;
3. categorie -> taux de TVA attendu -> signale, JAMAIS corrige automatiquement.

La detection de famille se fait par TOKEN, jamais par sous-chaine : « FRORIGINE »
contient « GIN », ce qui classait 89 lignes de tomates en spiritueux.
"""

from __future__ import annotations

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
from pipeline.normalize import tokenize
from pipeline.steps.field_checks import TAXONOMY_COLUMNS

log = get_logger(__name__)

STEP = "cross_checks"


def run(df: pl.DataFrame, ctx: RunContext) -> StepResult:
    started = time.perf_counter()
    corrections: list[Correction] = []
    anomalies: list[Anomaly] = []
    counters: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        counters[key] = counters.get(key, 0) + n

    taxonomy = ctx.config.taxonomy
    vat = ctx.config.vat

    row_ids = df["id_produit"].to_list()
    labels = df["nom"].to_list()
    rates = df["vat_rate"].to_list()
    levels = [df[c].to_list() for c in TAXONOMY_COLUMNS]

    filled: list[list[str]] = [[], [], [], []]
    vat_coherent: list[bool] = []
    expected_rates: list[float | None] = []

    for idx, row_id in enumerate(row_ids):
        values = [(levels[lvl][idx] or "").strip() for lvl in range(4)]
        label = labels[idx] or ""
        tokens = tokenize(label)

        # --- 1. comblement des trous de hierarchie -------------------------
        # 250 lignes ont n2 vide alors que n3 et n4 sont renseignes. La feuille
        # determine le chemin de facon univoque (0 conflit mesure), donc la
        # deduction est sure et ne merite pas un appel LLM.
        if values[3] and not all(values[:3]):
            reference = taxonomy.by_leaf.get(values[3])
            if reference:
                for lvl in range(3):
                    if not values[lvl]:
                        values[lvl] = reference[lvl]
                        bump("taxonomy_gap_filled")
                        corrections.append(
                            Correction(
                                run_id=ctx.run_id,
                                row_id=row_id,
                                field_name=TAXONOMY_COLUMNS[lvl],
                                old_value="",
                                new_value=reference[lvl],
                                author=Author.RULE,
                                rule="cross_checks.taxonomy.deduce_from_leaf",
                                created_at=ctx.now(),
                            )
                        )
                anomalies.append(
                    Anomaly(
                        run_id=ctx.run_id,
                        row_id=row_id,
                        field_name="taxonomie",
                        code=AnomalyCode.TAXONOMY_GAP_FILLED,
                        severity=Severity.INFO,
                        detail=" > ".join(reference),
                        value=" > ".join(reference),
                    )
                )

        for lvl in range(4):
            filled[lvl].append(values[lvl])

        # --- 2. taxonomie contredisant le libelle --------------------------
        # « WHISKY ECOSSAIS 70 CL » range en ENTRETIEN > LESSIVE est une
        # contradiction franche, detectable sans LLM.
        leaf = values[3]
        expected_leaf: str | None = None
        for candidate, keywords in taxonomy.keywords.items():
            if tokens & {k.upper() for k in keywords}:
                expected_leaf = candidate
                break

        taxonomy_contradicts_label = bool(expected_leaf and leaf and expected_leaf != leaf)
        if taxonomy_contradicts_label:
            bump("taxonomy_name_mismatch")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="taxonomie",
                    code=AnomalyCode.TAXONOMY_NAME_MISMATCH,
                    severity=Severity.WARNING,
                    detail=f"{label!r} classe en {leaf!r}, attendu {expected_leaf!r}",
                    value=str(leaf or ""),
                    label=str(label or ""),
                )
            )

        # --- 3. categorie -> TVA attendue ----------------------------------
        # Deux sources d'attente : la famille deduite du LIBELLE (prioritaire,
        # car elle reste valable meme si la taxonomie est fausse) puis la
        # categorie declaree.
        rate = rates[idx]
        expected: float | None = None
        origin = ""

        for family, rule in vat.keyword_families.items():
            if tokens & {t.upper() for t in rule.tokens}:
                expected = rule.expected
                origin = f"famille {family}"
                break

        # La categorie ne sert de reference QUE si elle n'a pas ete signalee
        # comme contredisant le libelle. Sinon on deduirait un taux attendu
        # d'une categorie qu'on vient soi-meme de declarer fausse, et une seule
        # erreur de taxonomie serait comptee deux fois : une fois comme erreur
        # de taxonomie, une fois comme fausse erreur de TVA.
        # Mesure : 615 incoherences de TVA remontees a tort, contre 72 reelles.
        if expected is None and leaf and not taxonomy_contradicts_label:
            category_rule = vat.expected_by_category.get(leaf)
            if category_rule:
                if rate is not None and category_rule.accepts(rate):
                    expected = rate
                else:
                    expected = category_rule.expected
                origin = f"categorie {leaf}"

        expected_rates.append(expected)
        coherent = expected is None or rate == expected
        if expected is not None and rate is not None and rate != expected:
            category_rule = vat.expected_by_category.get(leaf)
            if category_rule and category_rule.accepts(rate):
                coherent = True
        vat_coherent.append(coherent)

        if not coherent:
            bump("vat_category_mismatch")
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="tva",
                    code=AnomalyCode.VAT_CATEGORY_MISMATCH,
                    severity=Severity.WARNING,
                    detail=f"{rate}% pour {label!r} ({origin}) — attendu {expected}%",
                    value=str(rate),
                    label=str(label or ""),
                )
            )
            # AUCUNE correction n'est produite ici, deliberement. Une TVA fausse
            # a une consequence fiscale ; elle part en revue humaine, meme quand
            # la regle est certaine. Verrouille par un test.

    out = df.with_columns(
        *[
            pl.Series(column, filled[lvl], dtype=pl.String)
            for lvl, column in enumerate(TAXONOMY_COLUMNS)
        ],
        pl.Series("vat_coherent", vat_coherent, dtype=pl.Boolean),
        pl.Series("vat_expected", expected_rates, dtype=pl.Float64),
        pl.Series(
            "taxonomy_complete",
            [all(filled[lvl][i] for lvl in range(4)) for i in range(df.height)],
            dtype=pl.Boolean,
        ),
    )

    metrics = StepMetrics(
        step=STEP,
        rows_in=df.height,
        rows_out=out.height,
        duration_ms=(time.perf_counter() - started) * 1000,
        counters=counters,
    )
    log.info("step.done", step=STEP, rows_in=df.height, rows_out=out.height, **counters)
    return StepResult(df=out, corrections=corrections, anomalies=anomalies, metrics=metrics)
