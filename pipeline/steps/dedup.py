"""Etape 4a — doublons exacts apres normalisation, et construction du golden record.

Le profilage l'a montre : ce fichier ne contient AUCUN doublon strict. Aucun
`id_produit` en double, aucun EAN non vide en double. C'est apres normalisation
du libelle que les groupes apparaissent : 10 000 lignes se ramenent a ~273
libelles distincts.

La resolution d'entites floue (blocking, RapidFuzz, embeddings) est l'etape 4b
et arrive en phase 2. Cette etape-ci fait deja le gros du travail et donne des
golden records exploitables.

Regles de survivorship, explicites et testees — fusionner sans regles ecrites,
c'est perdre de l'information sans s'en apercevoir :
  libelle     : le plus long (le plus informatif parmi les abreviations)
  EAN         : celui qui passe le checksum, a defaut le plus frequent
  URL         : une URL qui repond, a defaut la premiere non vide
  taxonomie   : la plus profonde, puis la majoritaire
  TVA         : la MAJORITAIRE, et un conflit envoie le groupe en revue
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from pipeline.config import VatConfig
from pipeline.context import RunContext
from pipeline.logging import get_logger
from pipeline.models import StepMetrics, StepResult
from pipeline.normalize import extract_quantity, normalize_label

log = get_logger(__name__)

STEP = "dedup_exact"


@dataclass(slots=True)
class GoldenRecord:
    """Fiche unique issue de la fusion d'un groupe de lignes."""

    key: str
    label: str
    label_normalized: str
    ean: str | None
    internal_code: str | None
    url_image: str | None
    url_ok: bool
    url_checked: bool
    taxonomy: tuple[str, str, str, str]
    vat_rate: float | None
    vat_conflict: bool
    vat_distribution: dict[str, int]
    taxonomy_conflict: bool
    quantity_value: float | None
    quantity_unit: str | None
    origin: str | None
    publishable: bool
    source_rows: int
    # (identifiant magasin, libelle d'origine de CETTE ligne). Le couple plutot
    # que deux listes paralleles : une fusion qui n'en mettrait qu'une a jour
    # decalerait silencieusement les libelles d'un cran.
    source_refs: list[tuple[str, str]] = field(default_factory=list)
    # Renseignes par les etapes 4b et 6.
    label_enriched: str = ""
    # La marque lue dans le libelle par le LLM, vide s'il n'en a pas trouve ou
    # s'il n'a pas tourne. Elle sert de barriere a l'etape des quasi-doublons :
    # deux marques differentes ne sont jamais le meme produit.
    brand: str = ""
    llm_confidence: float = 0.0
    status: str = "NEEDS_REVIEW"
    confidence: float = 0.0
    routing_reasons: list[str] = field(default_factory=list)

    @property
    def row_ids(self) -> list[str]:
        """Les identifiants du magasin rattaches a cette fiche."""
        return [row_id for row_id, _ in self.source_refs]


def _most_common(values: list[str]) -> str | None:
    present = [v for v in values if v]
    if not present:
        return None
    return Counter(present).most_common(1)[0][0]


def build_golden_records(df: pl.DataFrame, vat: VatConfig | None = None) -> list[GoldenRecord]:
    """Construit les fiches uniques. `vat` sert au SEUL controle de legalite du
    taux, et `run()` le fournit toujours.

    Omettre `vat` ne desactive aucune correction — la TVA n'est de toute facon
    jamais corrigee — mais laisse `publishable` ignorer la legalite du taux.
    Les tests unitaires de survivorship l'omettent parce qu'ils portent sur le
    choix des valeurs, pas sur la publiabilite.
    """
    rows = df.to_dicts()
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = normalize_label(row.get("nom"))
        groups.setdefault(key, []).append(row)

    records: list[GoldenRecord] = []
    for key, members in groups.items():
        # --- libelle : le plus complet ------------------------------------
        label = max((m.get("nom") or "" for m in members), key=len)

        # --- EAN : la validite prime sur la frequence ----------------------
        valid_eans = [m["ean"] for m in members if m.get("ean") and m.get("ean_valid")]
        ean = _most_common(valid_eans)
        internal = _most_common([m.get("internal_code") or "" for m in members])

        # --- URL : celle qui repond ---------------------------------------
        working = [m["url_image"] for m in members if m.get("url_image") and m.get("url_ok")]
        url = working[0] if working else _most_common([m.get("url_image") or "" for m in members])
        url_ok = bool(working)
        # Le groupe n'est « verifie » que si au moins une de ses lignes l'a ete.
        url_checked = any(m.get("url_checked") for m in members)

        # --- taxonomie : la plus profonde, puis la majoritaire -------------
        paths = [
            (
                m.get("taxonomie_niveau_1") or "",
                m.get("taxonomie_niveau_2") or "",
                m.get("taxonomie_niveau_3") or "",
                m.get("taxonomie_niveau_4") or "",
            )
            for m in members
        ]
        complete = [p for p in paths if all(p)]
        path_counts = Counter(complete or paths)
        taxonomy = path_counts.most_common(1)[0][0]
        taxonomy_conflict = len(path_counts) > 1

        # --- TVA : majoritaire, et tout desaccord part en revue -------------
        rate_values = [m.get("vat_rate") for m in members if m.get("vat_rate") is not None]
        distribution = Counter(str(r) for r in rate_values)
        vat_rate = float(distribution.most_common(1)[0][0]) if distribution else None
        # Un groupe qui porte plusieurs taux signale une erreur de saisie sur
        # les lignes minoritaires : c'est exactement la decision groupee que
        # l'interface de revue presente a l'humain.
        vat_conflict = len(distribution) > 1 or any(m.get("vat_coherent") is False for m in members)

        enriched = _most_common([m.get("nom_enrichi") or "" for m in members]) or ""
        llm_confidence = max((float(m.get("llm_confidence") or 0.0) for m in members), default=0.0)
        quantity = extract_quantity(label)
        # Un taux hors bareme, ou absent, interdit « publiable » au meme titre
        # qu'une image morte. `vat_conflict` ne suffit pas : il ne se leve que
        # si les lignes du groupe divergent, ou si une regle de categorie
        # existe. Une feuille sans regle laissait donc passer un taux a 33 %.
        # Le controle est le meme que celui de l'aiguillage, pour que la fiche
        # ne puisse pas etre a la fois « en revue » et « publiable ».
        vat_legal = vat is None or (vat_rate is not None and vat.is_legal(vat_rate))
        # Une image non verifiee ne peut pas soutenir « publiable ».
        publishable = (
            bool(ean)
            and url_ok
            and url_checked
            and all(taxonomy)
            and not vat_conflict
            and vat_legal
        )

        records.append(
            GoldenRecord(
                key=key,
                label=label,
                label_normalized=key,
                ean=ean,
                internal_code=internal or None,
                url_image=url or None,
                url_ok=url_ok,
                url_checked=url_checked,
                taxonomy=taxonomy,
                vat_rate=vat_rate,
                vat_conflict=vat_conflict,
                vat_distribution=dict(distribution),
                taxonomy_conflict=taxonomy_conflict,
                quantity_value=quantity[0] if quantity else None,
                quantity_unit=quantity[1] if quantity else None,
                origin=_most_common([m.get("origin") or "" for m in members]),
                publishable=publishable,
                source_rows=len(members),
                source_refs=[(m["id_produit"], (m.get("nom") or "")[:512]) for m in members],
                label_enriched=enriched if enriched != label else "",
                llm_confidence=llm_confidence,
                brand=_most_common([m.get("brand") or "" for m in members]) or "",
            )
        )

    records.sort(key=lambda r: -r.source_rows)
    return records


def run(df: pl.DataFrame, ctx: RunContext) -> StepResult:
    started = time.perf_counter()
    records = build_golden_records(df, vat=ctx.config.vat)

    counters = {
        "golden_records": len(records),
        "rows_merged": df.height - len(records),
        "vat_conflicts": sum(1 for r in records if r.vat_conflict),
        "taxonomy_conflicts": sum(1 for r in records if r.taxonomy_conflict),
        "publishable": sum(1 for r in records if r.publishable),
    }

    metrics = StepMetrics(
        step=STEP,
        rows_in=df.height,
        rows_out=len(records),
        duration_ms=(time.perf_counter() - started) * 1000,
        counters=counters,
    )
    log.info("step.done", step=STEP, rows_in=df.height, rows_out=len(records), **counters)

    # Les golden records voyagent hors du DataFrame : ils ont une cardinalite
    # differente (273 fiches pour 10 000 lignes) et les etapes suivantes
    # continuent de travailler sur les lignes.
    return StepResult(df=df, metrics=metrics, payload={"golden_records": records})
