"""Orchestrateur : enchaine les etapes et agrege leurs sorties.

Les etapes n'ecrivent jamais en base elles-memes — c'est ce qui les rend
testables isolement. L'orchestrateur est le seul point qui connait la
persistance (a partir de la phase 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from pipeline.context import RunContext
from pipeline.llm.client import LlmClient
from pipeline.logging import bind_run, clear_run, get_logger
from pipeline.models import Anomaly, Correction, RunReport, StepMetrics
from pipeline.steps import (
    cross_checks,
    dedup,
    entity_resolution,
    field_checks,
    ingest,
    llm_enrich,
    report,
    scoring_routing,
)

log = get_logger(__name__)


@dataclass(slots=True)
class RunOutcome:
    report: RunReport
    df: pl.DataFrame
    corrections: list[Correction]
    anomalies: list[Anomaly]
    golden_records: list[Any] = field(default_factory=list)
    llm_usage: dict[str, Any] = field(default_factory=dict)


def run_pipeline(ctx: RunContext, llm_client: LlmClient | None = None) -> RunOutcome:
    """`llm_client` permet a l'appelant d'imposer SON client : celui de l'API
    porte le cache PostgreSQL et le reliquat de l'enveloppe. Sans lui, l'etage
    se construit un client de session, sans memoire d'un run a l'autre."""
    bind_run(ctx.run_id, ctx.store_id)
    try:
        log.info(
            "run.start",
            source_file=str(ctx.source_file),
            file_sha256=ctx.file_sha256[:12],
            network_enabled=ctx.network_enabled,
        )

        df = ingest.read_csv(ctx.source_file)
        rows_in = df.height

        golden_records: list[Any] = []
        llm_usage: dict[str, Any] = {}
        corrections: list[Correction] = []
        anomalies: list[Anomaly] = []
        steps: list[StepMetrics] = []

        for step in (
            ingest,
            field_checks,
            cross_checks,
            llm_enrich,
            dedup,
            entity_resolution,
            scoring_routing,
        ):
            if step in (entity_resolution, scoring_routing):
                result = step.run(df, ctx, golden_records)
            elif step is llm_enrich:
                result = step.run(df, ctx, llm_client)
            else:
                result = step.run(df, ctx)
            df = result.df
            corrections.extend(result.corrections)
            anomalies.extend(result.anomalies)
            if result.metrics:
                steps.append(result.metrics)
            golden_records = result.payload.get("golden_records", golden_records)
            llm_usage = result.payload.get("llm_usage", llm_usage)

        run_report = report.build(df, ctx, corrections, anomalies, steps, rows_in, golden_records)
        return RunOutcome(
            report=run_report,
            df=df,
            corrections=corrections,
            anomalies=anomalies,
            golden_records=golden_records,
            llm_usage=llm_usage,
        )
    finally:
        clear_run()
