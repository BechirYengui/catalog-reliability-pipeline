"""Etape 1 — ingestion.

Responsabilites : lire le fichier quel que soit son encodage, garantir le schema
de colonnes, reparer le mojibake, nettoyer les espaces, detacher les suffixes
marketing colles au libelle.

Constats du fichier reel qui dictent cette etape (docs/profiling.md §1 et §4) :
- BOM UTF-8 present -> `utf-8-sig` obligatoire, sinon la 1re colonne s'appelle
  '\\ufeffid_produit' et le schema est refuse ;
- 10 001 fins de ligne CRLF ;
- le mojibake n'est PAS que dans `nom` : 74 lignes dans taxonomie_niveau_2
  (« Ã‰PICERIE SUCREE ») et 30 dans taxonomie_niveau_3 (« CRÃˆMERIE »). Ne
  reparer que le libelle laisserait deux categories fantomes dans le referentiel.
"""

from __future__ import annotations

import time
from pathlib import Path

import ftfy
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

log = get_logger(__name__)

STEP = "ingest"

EXPECTED_COLUMNS = (
    "id_produit",
    "ean",
    "nom",
    "url_image",
    "taxonomie_niveau_1",
    "taxonomie_niveau_2",
    "taxonomie_niveau_3",
    "taxonomie_niveau_4",
    "tva",
)

TEXT_COLUMNS = (
    "nom",
    "taxonomie_niveau_1",
    "taxonomie_niveau_2",
    "taxonomie_niveau_3",
    "taxonomie_niveau_4",
)

# Colonnes ajoutees par cette etape.
ADDED_COLUMNS = ("origin", "marketing_mention")


class SchemaError(ValueError):
    """Le fichier n'a pas les colonnes attendues : on refuse le run entier
    plutot que d'integrer des donnees dont on ne sait rien."""


def read_csv(path: Path) -> pl.DataFrame:
    """Lecture robuste. Tout est lu en `str` : convertir la TVA en float des la
    lecture ferait disparaitre silencieusement les valeurs non numeriques, or
    c'est precisement ce qu'on veut detecter.

    L'octet NUL est retire ICI, sur le texte entier, avant meme le parsing.
    Trois raisons de le faire a cet endroit plutot qu'a l'etape suivante :

    1. PostgreSQL refuse `\\x00` dans un TEXT. Un NUL laisse passer ne se voit
       pas au traitement — il fait tomber le run a la PERSISTANCE, donc apres
       avoir paye l'etage LLM et la verification des images. C'est exactement
       ainsi qu'est mort le run 35b44a78 du 2026-08-11.
    2. Le nettoyage de l'etape suivante ne couvre que 5 colonnes sur 9, et il
       ne retire le NUL que par EFFET DE BORD de ftfy — une mise a jour de la
       dependance retirerait la protection sans que rien ne le signale. Ici, la
       couverture est totale par construction : il n'y a pas de liste de
       colonnes a tenir a jour.
    3. `read_csv` est appele separement de `run()` par les deux orchestrateurs
       (`runner.py`, `api/jobs.py`). Le seul point qu'ils partagent tous les
       deux, c'est celui-ci.

    Meme nature que le BOM et que les octets indecodables, traites deux lignes
    plus haut : un defaut de transport, retire avant que la donnee metier
    n'existe. La difference est qu'on le COMPTE — un nettoyage muet sur une
    valeur metier (un identifiant de ligne, une URL) ne doit pas passer
    inapercu.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")

    nuls = text.count("\x00")
    if nuls:
        text = text.replace("\x00", "")
        log.warning(
            "ingest.nul_bytes_removed",
            count=nuls,
            source=path.name,
            detail="octets NUL retires du fichier : PostgreSQL les refuse et le "
            "run tomberait a l'ecriture. Les valeurs concernees sont amputees "
            "de ces octets.",
        )

    df = pl.read_csv(
        text.encode("utf-8"),
        infer_schema_length=0,  # tout en str
        truncate_ragged_lines=False,
    ).fill_null("")
    return df


def validate_schema(df: pl.DataFrame) -> None:
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    if missing:
        raise SchemaError(f"colonnes manquantes : {missing} — colonnes recues : {df.columns}")


def _fix_text(value: str | None) -> str:
    """ftfy repare le mojibake (« CAFÃ‰ » -> « CAFÉ »), puis on normalise les
    espaces. ftfy est preferable a un table de remplacement maison : il gere les
    doubles encodages en cascade, qu'une table ne couvre jamais entierement."""
    if value is None:
        return ""
    return " ".join(ftfy.fix_text(value).split())


def detach_suffix(label: str, suffixes: list[tuple[str, str, str]]) -> tuple[str, dict[str, str]]:
    """Detache un suffixe marketing colle au libelle.

    Liste FERMEE, volontairement. Une heuristique generique du type « minuscule
    suivie d'une majuscule » decouperait aussi des libelles legitimes ; ici
    l'ajout d'un suffixe est un acte explicite et versionne.

    'LESSIVE 2LORIGINE ITALIA' -> ('LESSIVE 2L', {'origin': 'ITALIA'})
    """
    extracted: dict[str, str] = {}
    result = label
    for value, target_field, extracted_value in suffixes:
        # Comparaison sans espaces : le suffixe est colle, donc « 2LORIGINE
        # ITALIA » ne contient pas « ORIGINE ITALIA » avec ses espaces d'origine.
        compact_suffix = value.replace(" ", "")
        compact = result.replace(" ", "")
        if compact_suffix and compact.endswith(compact_suffix):
            cut = len(result)
            seen = 0
            # On remonte depuis la fin en ignorant les espaces jusqu'a avoir
            # consomme autant de caracteres significatifs que le suffixe.
            for idx in range(len(result) - 1, -1, -1):
                if not result[idx].isspace():
                    seen += 1
                if seen == len(compact_suffix):
                    cut = idx
                    break
            result = result[:cut].strip()
            extracted[target_field] = extracted_value
    return result, extracted


def run(df: pl.DataFrame, ctx: RunContext) -> StepResult:
    started = time.perf_counter()
    rows_in = df.height

    validate_schema(df)

    corrections: list[Correction] = []
    anomalies: list[Anomaly] = []
    counters: dict[str, int] = {}

    suffixes = [(s.value, s.target_field, s.extracted_value) for s in ctx.config.labels.suffixes]

    # --- reparation du texte sur TOUTES les colonnes texte ------------------
    out = df
    for column in TEXT_COLUMNS:
        original = out[column]
        fixed = pl.Series(column, [_fix_text(v) for v in original.to_list()], dtype=pl.String)
        changed = [
            (row_id, old or "", new)
            for row_id, old, new in zip(
                out["id_produit"].to_list(), original.to_list(), fixed.to_list(), strict=True
            )
            if (old or "") != new
        ]
        for row_id, old, new in changed:
            # Un simple retrait d'espaces n'est pas du mojibake : on ne le
            # remonte comme anomalie que si le texte lui-meme a change.
            is_mojibake = " ".join((old or "").split()) != new
            corrections.append(
                Correction(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name=column,
                    old_value=old,
                    new_value=new,
                    author=Author.RULE,
                    rule="ingest.fix_text",
                    created_at=ctx.now(),
                )
            )
            if is_mojibake:
                anomalies.append(
                    Anomaly(
                        run_id=ctx.run_id,
                        row_id=row_id,
                        field_name=column,
                        code=AnomalyCode.MOJIBAKE_FIXED,
                        severity=Severity.INFO,
                        detail=f"{old!r} -> {new!r}",
                        value=str(old),
                        label=str(new),
                    )
                )
                counters[f"mojibake_{column}"] = counters.get(f"mojibake_{column}", 0) + 1
        out = out.with_columns(fixed)

    # --- trim des colonnes restantes ---------------------------------------
    out = out.with_columns(
        [pl.col(c).str.strip_chars().alias(c) for c in ("ean", "url_image", "tva", "id_produit")]
    )

    # --- detachement des suffixes marketing --------------------------------
    origins: list[str] = []
    mentions: list[str] = []
    labels: list[str] = []
    for row_id, label in zip(out["id_produit"].to_list(), out["nom"].to_list(), strict=True):
        cleaned, extracted = detach_suffix(label or "", suffixes)
        labels.append(cleaned)
        origins.append(extracted.get("origin", ""))
        mentions.append(extracted.get("marketing_mention", ""))
        if extracted:
            corrections.append(
                Correction(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="nom",
                    old_value=label,
                    new_value=cleaned,
                    author=Author.RULE,
                    rule="ingest.detach_suffix",
                    created_at=ctx.now(),
                )
            )
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="nom",
                    code=AnomalyCode.MARKETING_SUFFIX_DETACHED,
                    severity=Severity.INFO,
                    detail=", ".join(f"{k}={v}" for k, v in extracted.items()),
                    value=str(label),
                    label=str(cleaned),
                )
            )
            counters["marketing_suffix"] = counters.get("marketing_suffix", 0) + 1

    out = out.with_columns(
        pl.Series("nom", labels, dtype=pl.String),
        pl.Series("origin", origins, dtype=pl.String),
        pl.Series("marketing_mention", mentions, dtype=pl.String),
    )

    # --- libelles vides : sans libelle, aucune etape aval ne peut travailler --
    for row_id, label in zip(out["id_produit"].to_list(), out["nom"].to_list(), strict=True):
        if not label.strip():
            anomalies.append(
                Anomaly(
                    run_id=ctx.run_id,
                    row_id=row_id,
                    field_name="nom",
                    code=AnomalyCode.LABEL_EMPTY,
                    severity=Severity.ERROR,
                    detail="libelle vide",
                )
            )
            counters["label_empty"] = counters.get("label_empty", 0) + 1

    metrics = StepMetrics(
        step=STEP,
        rows_in=rows_in,
        rows_out=out.height,
        duration_ms=(time.perf_counter() - started) * 1000,
        counters=counters,
    )
    log.info(
        "step.done",
        step=STEP,
        rows_in=rows_in,
        rows_out=out.height,
        corrections=len(corrections),
        anomalies=len(anomalies),
        **counters,
    )
    return StepResult(df=out, corrections=corrections, anomalies=anomalies, metrics=metrics)
