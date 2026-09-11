"""Le README ne peut pas promettre une economie que le pipeline ne calcule pas.

Son premier paragraphe est ecrit pour quelqu'un qui a trente secondes : il met
un chiffre d'impact avant l'architecture. C'est exactement le chiffre qu'on
lira comme mesure, donc il est soumis a la meme regle que le document de
reponse (`test_doc_claims.py`) : chaque nombre est extrait du README par son
motif et recalcule depuis le fichier fourni, avec l'estimateur du pipeline.

Si une phrase est reecrite et que le motif ne correspond plus, le test echoue
expres : mettre le motif a jour, jamais supprimer la verification.

Le modele est fige sur le defaut du projet : un `LLM_MODEL` pose dans
l'environnement changerait les montants sans que le README ait menti.
"""

from __future__ import annotations

import re
from pathlib import Path

import polars as pl
import pytest

from api.budget import DEFAULT_BUDGET_USD
from pipeline.config import LlmConfig, PipelineConfig
from pipeline.context import RunContext
from pipeline.llm.pricing import BATCH_MULTIPLIER
from pipeline.steps import ingest, llm_enrich

ROOT = Path(__file__).resolve().parent.parent
SOURCE_CSV = ROOT / "data" / "samples" / "store_listing_produit.csv"
README = ROOT / "README.md"

# Part de libelles jamais vus au depot du lendemain, hypothese affichee dans le
# tableau. La meme que celle du document de reponse.
NEW_LABELS_SHARE = 0.05


def _amount(text: str) -> float:
    return float(text.replace(",", ".").replace(" ", "").replace(" ", ""))


def _grab(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    assert match, f"motif introuvable dans le README : {pattern!r}"
    return match.group(1)


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def estimates() -> dict[str, float]:
    if not SOURCE_CSV.exists():  # pragma: no cover - garde-fou d'environnement
        pytest.skip(f"fichier de reference absent : {SOURCE_CSV}")

    config = PipelineConfig.load().model_copy(update={"llm": LlmConfig()})
    ctx = RunContext(
        store_id="readme",
        source_file=SOURCE_CSV,
        config=config,
        run_id="readme",
        network_enabled=False,
    )
    df = ingest.run(ingest.read_csv(SOURCE_CSV), ctx).df
    first = llm_enrich.estimate_dry_run(df, ctx)

    distinct = list(dict.fromkeys(label for label in df["nom"].to_list() if label))
    new = distinct[: round(len(distinct) * NEW_LABELS_SHARE)]
    next_day = llm_enrich.estimate_dry_run(df.filter(pl.col("nom").is_in(new)), ctx)

    return {
        "rows": first["rows"],
        "labels": first["distinct_labels"],
        "batches": first["batches"],
        "naive_batches": -(-first["rows"] // first["batch_size"]),
        "naive_usd": first["estimated_cost_without_dedup_usd"],
        "first_usd": first["estimated_cost_usd"],
        "night_usd": first["estimated_cost_usd"] * BATCH_MULTIPLIER,
        "next_day_usd": next_day["estimated_cost_usd"],
    }


def test_headline_sentence(readme: str, estimates: dict[str, float]) -> None:
    head = readme[: readme.index("| |")]
    rows = _grab(r"catalogue magasin de ([\d\s ]+) lignes", head)
    first = _grab(r"coûte environ (\d+,\d+) \$", head)
    naive = _grab(r"au lieu de (\d+) \$", head)
    next_day = _grab(r"puis (\d+,\d+) \$ le lendemain", head)
    labels = _grab(r"que les (\d+) libellés", head)

    assert int(re.sub(r"\D", "", rows)) == estimates["rows"]
    assert _amount(first) == pytest.approx(estimates["first_usd"], abs=0.005)
    assert _amount(naive) == pytest.approx(estimates["naive_usd"], abs=0.5)
    assert _amount(next_day) == pytest.approx(estimates["next_day_usd"], abs=0.005)
    assert int(labels) == estimates["labels"]


def test_cost_table(readme: str, estimates: dict[str, float]) -> None:
    table = readme[readme.index("| |") : readme.index("Chiffrage à blanc")]
    naive_calls = _grab(r"lignes, (\d+) appels", table)
    labels, calls = re.search(r"(\d+) libellés, (\d+) appels", table).groups()  # type: ignore[union-attr]
    first_naive, first = re.search(  # type: ignore[union-attr]
        r"Premier dépôt[^|]*\| ≈ (\d+) \$ \| ≈ (\d+,\d+) \$", table
    ).groups()
    night = _grab(r"≈ (\d+,\d+) \$ en traitement de nuit", table)
    next_naive, next_day = re.search(  # type: ignore[union-attr]
        r"lendemain[^|]*\| ≈ (\d+) \$ \| ≈ (\d+,\d+) \$", table
    ).groups()
    share = _grab(r"lendemain, (\d+) % de libellés nouveaux", table)

    assert int(naive_calls) == estimates["naive_batches"]
    assert int(labels) == estimates["labels"]
    assert int(calls) == estimates["batches"]
    assert _amount(first_naive) == pytest.approx(estimates["naive_usd"], abs=0.5)
    # Sans cache, le lendemain repaie tout : meme montant que le premier depot.
    assert _amount(next_naive) == pytest.approx(estimates["naive_usd"], abs=0.5)
    assert _amount(first) == pytest.approx(estimates["first_usd"], abs=0.005)
    assert _amount(night) == pytest.approx(estimates["night_usd"], abs=0.005)
    assert _amount(next_day) == pytest.approx(estimates["next_day_usd"], abs=0.005)
    assert int(share) / 100 == NEW_LABELS_SHARE


def test_dedup_factor_and_budget(readme: str, estimates: dict[str, float]) -> None:
    factor = _grab(r"soit (\d+) fois moins que de lignes", readme)
    budget = _grab(r"enveloppe cumulée de (\d+) \$", readme)

    assert int(factor) == round(estimates["rows"] / estimates["labels"])
    assert float(budget) == DEFAULT_BUDGET_USD
