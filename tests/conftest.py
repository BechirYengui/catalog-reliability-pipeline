"""Fixtures communes.

Regle du projet : les donnees de test sont des **extraits reels** du CSV fourni,
jamais des exemples inventes. `tests/data/sample_anomalies.csv` contient 43
lignes selectionnees pour couvrir chaque famille d'anomalie mesuree dans
docs/profiling.md — BOM et CRLF compris.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from pipeline.config import PipelineConfig
from pipeline.context import RunContext
from pipeline.steps import ingest

TESTS_DIR = Path(__file__).parent
SAMPLE = TESTS_DIR / "data" / "sample_anomalies.csv"
FULL_SAMPLE = TESTS_DIR.parent / "data" / "samples" / "store_listing_produit.csv"

FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def config() -> PipelineConfig:
    return PipelineConfig.load()


@pytest.fixture
def ctx(config: PipelineConfig) -> RunContext:
    """Contexte deterministe : horloge figee et reseau coupe.

    Les tests ne doivent jamais dependre d'un service tiers — ni de sa
    disponibilite, ni de sa latence.
    """
    return RunContext(
        store_id="test-store",
        source_file=SAMPLE,
        config=config,
        run_id="00000000-0000-0000-0000-000000000000",
        clock=lambda: FIXED_NOW,
        network_enabled=False,
    )


@pytest.fixture
def raw_df() -> pl.DataFrame:
    return ingest.read_csv(SAMPLE)


@pytest.fixture
def ingested(raw_df: pl.DataFrame, ctx: RunContext) -> Iterator[pl.DataFrame]:
    yield ingest.run(raw_df, ctx).df
