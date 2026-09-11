"""Étape 5 — enrichissement sémantique.

Ce que les règles ne savent pas faire, parce qu'il faut comprendre du texte :
réécrire un libellé caisse abrégé, compléter une taxonomie tronquée, extraire
la marque et la contenance.

Ce que le LLM ne fait PAS ici, et c'est délibéré :
- il ne valide aucun checksum — c'est de l'arithmétique, une règle le fait
  gratuitement et sans risque d'invention ;
- il ne fixe aucun taux de TVA — la conséquence d'une erreur est fiscale ;
- il n'invente aucune catégorie — la taxonomie de référence est injectée dans
  le prompt comme liste fermée et il choisit dedans.

Le LLM ne voit que des libellés DISTINCTS : ~273 pour 10 000 lignes.
"""

from __future__ import annotations

import json
import time
from typing import Any

import polars as pl

from pipeline.context import RunContext
from pipeline.llm.client import LlmClient, LlmSettings
from pipeline.logging import get_logger
from pipeline.models import Author, Correction, StepMetrics, StepResult

log = get_logger(__name__)

STEP = "llm_enrich"

SYSTEM_PROMPT = """Tu fiabilises un catalogue produit de grande distribution française.

On te donne des libellés de caisse : abrégés, tout en majuscules, souvent
tronqués (« CAFE MLU 250G », « PL EMMENTAL RAP 200G »). Pour chacun, tu produis :

1. `label` — le libellé lisible par un client, en français correct.
   « CAFE MLU 250G » devient « Café moulu 250 g ». Tu n'inventes ni marque ni
   contenance : tu ne fais que développer ce qui est déjà écrit.
2. `taxonomy` — le chemin choisi DANS LA LISTE FOURNIE, jamais ailleurs.
   Si aucun chemin ne convient, tu renvoies une chaîne vide.
3. `brand` — la marque si elle apparaît dans le libellé, sinon vide.
   « NUTELLA 750 G » a pour marque « Nutella ». « CREME FRAICHE 30 20CL » n'en a pas.
4. `quantity_value` et `quantity_unit` — la contenance si elle est lisible.
5. `confidence` — entre 0 et 1, ta confiance dans CETTE proposition.
   Sois honnête : une confiance basse envoie le cas en revue humaine, ce qui
   est le comportement souhaité quand tu n'es pas sûr. Une confiance haute sur
   une proposition fausse coûte bien plus cher qu'une confiance basse sur une
   proposition juste.

Tu ne proposes JAMAIS de taux de TVA : ce champ ne t'est pas soumis.

Chemins de taxonomie autorisés :
{taxonomy}"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "label": {"type": "string"},
                    "taxonomy": {"type": "string"},
                    "brand": {"type": "string"},
                    "quantity_value": {"type": "number"},
                    "quantity_unit": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "source",
                    "label",
                    "taxonomy",
                    "brand",
                    "quantity_value",
                    "quantity_unit",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def build_settings(ctx: RunContext, dry_run: bool = False, batch_api: bool = False) -> LlmSettings:
    settings = ctx.config.llm
    return LlmSettings(
        batch_api=batch_api,
        api_key=settings.api_key,
        model=settings.model,
        batch_size=settings.batch_size,
        max_calls_per_run=settings.max_calls_per_run,
        max_cost_usd=settings.max_cost_usd,
        dry_run=dry_run or not settings.api_key,
        effort=settings.effort,
    )


def run(df: pl.DataFrame, ctx: RunContext, client: LlmClient | None = None) -> StepResult:
    started = time.perf_counter()
    corrections: list[Correction] = []
    counters: dict[str, int] = {}

    taxonomy_lines = [" > ".join(path) for path in ctx.config.taxonomy.paths]
    system_prompt = SYSTEM_PROMPT.format(taxonomy="\n".join(f"- {p}" for p in taxonomy_lines))

    llm = client or LlmClient(build_settings(ctx))

    # LA règle d'économie : on déduplique avant d'appeler.
    labels = df["nom"].to_list()
    distinct = list(dict.fromkeys(label for label in labels if label and label.strip()))
    counters["rows_in"] = df.height
    counters["distinct_labels"] = len(distinct)
    counters["dedup_factor"] = round(df.height / max(len(distinct), 1))

    # Le cache est interroge libelle par libelle : seuls les inconnus partent
    # en lot. Avec une cle par lot de 25, un produit nouveau en debut de
    # fichier decalait tous les lots suivants et le depot du lendemain
    # repayait le catalogue entier.
    enriched = llm.complete_labels(
        system_prompt=system_prompt,
        labels=distinct,
        schema=RESPONSE_SCHEMA,
        cache_scope="llm_enrich.v1",
        key_of=lambda item: str(item.get("source", "")),
    )

    counters["labels_enriched"] = len(enriched)
    counters["labels_from_cache"] = llm.usage.labels_cached

    # Apercu du prompt reellement envoye, pour que l'interface montre ce que
    # le LLM a vu plutot que de le decrire.
    prompts_preview = [
        "Libellés à traiter :\n" + "\n".join(f"- {label}" for label in batch)
        for batch in LlmClient.batches(distinct, llm.settings.batch_size)[:2]
    ]

    # --- application aux lignes ------------------------------------------
    thresholds = ctx.config.thresholds
    labels_out: list[str] = []
    brands: list[str] = []
    confidences: list[float] = []

    for row_id, label in zip(df["id_produit"].to_list(), labels, strict=True):
        item = enriched.get(label or "")
        if not item:
            labels_out.append(label or "")
            brands.append("")
            confidences.append(0.0)
            continue

        confidence = float(item.get("confidence", 0.0))
        proposal = (item.get("label") or "").strip()
        confidences.append(confidence)
        brands.append((item.get("brand") or "").strip())

        # Sous le seuil, la proposition existe mais n'est PAS appliquée : elle
        # part en revue. Au-dessus, elle est appliquée et tracée comme toute
        # autre correction, avec son auteur et sa confiance.
        if proposal and proposal != label and confidence >= thresholds.min_label_confidence:
            labels_out.append(proposal)
            corrections.append(
                Correction(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="nom",
                    old_value=label,
                    new_value=proposal,
                    author=Author.LLM,
                    rule=f"llm_enrich.label[{llm.settings.model}]",
                    confidence=confidence,
                    created_at=ctx.now(),
                )
            )
            counters["labels_applied"] = counters.get("labels_applied", 0) + 1
        else:
            labels_out.append(label or "")
            if proposal and proposal != label:
                counters["labels_below_threshold"] = counters.get("labels_below_threshold", 0) + 1

    out = df.with_columns(
        pl.Series("nom_enrichi", labels_out, dtype=pl.String),
        pl.Series("brand", brands, dtype=pl.String),
        pl.Series("llm_confidence", confidences, dtype=pl.Float64),
    )

    usage = llm.usage.to_dict()
    counters["llm_calls_real"] = llm.usage.calls_real
    counters["llm_calls_cached"] = llm.usage.calls_cached
    counters["llm_queued"] = llm.usage.queued

    metrics = StepMetrics(
        step=STEP,
        rows_in=df.height,
        rows_out=out.height,
        duration_ms=(time.perf_counter() - started) * 1000,
        counters=counters,
    )
    log.info(
        "step.done",
        step=STEP,
        model=llm.settings.model,
        distinct_labels=len(distinct),
        calls_real=llm.usage.calls_real,
        calls_cached=llm.usage.calls_cached,
        cost_usd=round(llm.usage.cost.total_usd, 6),
        **{k: v for k, v in counters.items() if k.startswith("labels")},
    )

    return StepResult(
        df=out,
        corrections=corrections,
        metrics=metrics,
        payload={"llm_usage": usage, "llm_prompts_preview": prompts_preview},
    )


def estimate_dry_run(df: pl.DataFrame, ctx: RunContext) -> dict[str, Any]:
    """Ce qui SERAIT envoyé, et ce que ça coûterait — sans appeler l'API.

    Permet de connaître la facture avant le premier appel payant.
    """
    labels = [label for label in df["nom"].to_list() if label and label.strip()]
    distinct = list(dict.fromkeys(labels))
    settings = build_settings(ctx, dry_run=True)
    batches = LlmClient.batches(distinct, settings.batch_size)

    taxonomy_lines = [" > ".join(p) for p in ctx.config.taxonomy.paths]
    system_prompt = SYSTEM_PROMPT.format(taxonomy="\n".join(f"- {p}" for p in taxonomy_lines))

    # Approximation volontairement grossière et signalée comme telle : ~4
    # caractères par jeton en français. Le chiffre exact viendra de
    # `count_tokens`, mais une estimation à ±25 % suffit à décider si on lance.
    system_tokens = len(system_prompt) // 4
    body_tokens = sum(len(label) for label in distinct) // 4
    output_tokens = len(distinct) * 60  # ~60 jetons de réponse par libellé

    input_tokens = system_tokens * len(batches) + body_tokens
    from pipeline.llm.pricing import compute_cost, pricing_for

    cost = compute_cost(settings.model, input_tokens, output_tokens)
    price = pricing_for(settings.model)

    # Le contrefactuel : chaque ligne envoyée telle quelle, mêmes lots, même
    # prompt. C'est ce que la déduplication évite de payer, et le chiffre que le
    # README met en tête — il doit sortir du même calcul que l'estimation.
    naive_batches = -(-len(labels) // settings.batch_size)
    without_dedup = compute_cost(
        settings.model,
        system_tokens * naive_batches + sum(len(label) for label in labels) // 4,
        len(labels) * 60,
    )

    return {
        "model": settings.model,
        "model_display_name": price.display_name,
        "rows": df.height,
        "distinct_labels": len(distinct),
        "dedup_factor": round(df.height / max(len(distinct), 1), 1),
        "batches": len(batches),
        "batch_size": settings.batch_size,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": round(cost.total_usd, 4),
        "estimated_cost_without_batching_usd": round(
            compute_cost(
                settings.model,
                system_tokens * len(distinct) + body_tokens,
                output_tokens,
            ).total_usd,
            4,
        ),
        "estimated_cost_without_dedup_usd": round(without_dedup.total_usd, 4),
        "sample_prompt": json.dumps(
            {"system_head": system_prompt[:300], "first_batch": distinct[:5]},
            ensure_ascii=False,
        ),
        "note": "Estimation ±25 % (≈4 caractères par jeton). Le cache ramène "
        "le coût des runs suivants à près de zéro.",
    }
