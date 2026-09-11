"""Etape 4b — quasi-doublons.

L'etape precedente ne regroupe que ce qui devient IDENTIQUE apres
normalisation. Restent les variantes reellement differentes : « COCA COLA ZERO
1L », « COKA COLA ZERO 1 L », « COC COL ZER 1L ».

Trois crans, du moins cher au plus cher, et le funnel se lit dans les logs :

1. **Blocking** — on ne compare que les fiches d'une meme classe (categorie
   feuille + contenance). Comparer 343 libelles deux a deux fait 58 653 paires ;
   le blocking ramene ca a quelques centaines. Inutile sur ce fichier, mais
   c'est ce qui tient la promesse « N magasins x 10 000 lignes ».
2. **RapidFuzz** — similarite textuelle dans chaque bloc.
3. **Le libelle enrichi par le LLM** comme second signal. C'est le point
   mesure : sur ce fichier, 27 % des vraies paires tombent sous 0,45 de
   similarite textuelle (« DOLIPRANE 1 G » contre « PARACETAMOL 1000 DOLI »).
   Une fois le LLM passe, les deux s'ecrivent pareil et le fuzzy retrouve la
   paire — d'ou l'ordre : llm_enrich AVANT cette etape.

Pas d'embeddings : le LLM voit deja chaque libelle distinct une fois, et
comble le fosse semantique que les embeddings devaient combler. Voir
docs/profiling.md.

Deux barrieres DURES, qu'aucune ressemblance de libelle ne franchit :

- **la contenance** — « COCA COLA ZERO 1L » et « COCA COLA ZERO 33CL » se
  ressemblent enormement et sont deux produits differents ;
- **la marque** — un Doliprane 1 g et un paracetamol generique 1 g portent la
  meme molecule et le meme dosage, et le LLM les enrichit presque pareil.

Les fusionner casserait le referentiel de facon invisible : une reference
disparait du catalogue du magasin sans que rien ne le signale.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import polars as pl

from pipeline.context import RunContext
from pipeline.logging import get_logger
from pipeline.models import StepMetrics, StepResult
from pipeline.normalize import normalize_label

log = get_logger(__name__)

STEP = "entity_resolution"

# Au-dessus : meme produit. En dessous de 0,72 : produits distincts. Entre les
# deux, l'ambiguite — que le LLM tranchera quand cet arbitrage sera branche.
MERGE_THRESHOLD = 0.86
REJECT_THRESHOLD = 0.72

# Au-dela de cette taille, un bloc bascule sur une comparaison par fenetre.
# 400 fiches font 79 800 paires, soit environ 0,12 s : encore raisonnable.
MAX_EXACT_BLOCK = 400
WINDOW = 100


@dataclass(frozen=True, slots=True)
class UnresolvedPair:
    """Deux fiches qui se ressemblent trop pour etre separees d'office, pas assez
    pour etre fusionnees sans risque.

    Le pipeline refuse de trancher : une fusion a tort est invisible et
    irreversible, un doublon restant se voit. Mais laisser ces paires dans le
    silence revient a livrer un catalogue dont on SAIT qu'il contient des
    doublons sans le dire. Elles partent donc en revue humaine, ou la decision
    devient une regle apprise et n'est jamais reposee.
    """

    left_key: str
    right_key: str
    left_label: str
    right_label: str
    score: float


# Facteur de conversion vers l'unite de base de chaque famille : le millilitre
# pour les volumes, le gramme pour les masses. Les familles ne se melangent
# jamais : 1 kg et 1 L ne sont pas la meme chose, meme si le nombre coincide.
_BASE_UNITS: dict[str, tuple[str, float]] = {
    "ML": ("VOL", 1.0),
    "CL": ("VOL", 10.0),
    "L": ("VOL", 1000.0),
    "G": ("MAS", 1.0),
    "K": ("MAS", 1000.0),
    "KG": ("MAS", 1000.0),
}


def blocking_key(record: Any) -> str:
    """Classe de comparaison : feuille de taxonomie + contenance NORMALISEE.

    Deux produits de contenances differentes ne sont jamais le meme produit,
    quelle que soit la ressemblance des libelles. Mais la contenance doit etre
    comparee en volume, pas en caracteres : le magasin ecrit la meme bouteille
    « 1 L », « 100CL » ou « 0.7L » selon la caisse.

    C'est le defaut que cette normalisation corrige. La cle portait sur l'unite
    telle qu'ecrite, donc « COCACOLA Z 100CL » et « COKA COLA ZERO 1 L », qui
    font tous deux 1 000 ml, tombaient dans deux classes distinctes et
    n'etaient JAMAIS compares. Le blocking, cense eviter des comparaisons
    inutiles, empechait des comparaisons necessaires.

    La barriere reste dure dans le bon sens : 33 cl vaut 330 ml, 1 L vaut
    1 000 ml, ils ne se rencontrent toujours pas. Ce sont deux references, deux
    fiches, deux annonces sur la marketplace.

    Une unite inconnue est conservee telle quelle plutot que rapprochee de
    quoi que ce soit : ne pas savoir convertir ne doit jamais valoir fusion.
    """
    leaf = record.taxonomy[3] if record.taxonomy[3] else "?"
    if not (record.quantity_value and record.quantity_unit):
        return f"{leaf}|?"

    conversion = _BASE_UNITS.get(record.quantity_unit.upper())
    if conversion is None:
        return f"{leaf}|{record.quantity_value:g}{record.quantity_unit}"

    famille, facteur = conversion
    return f"{leaf}|{famille}{record.quantity_value * facteur:g}"


def _similarity(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz

        # token_set_ratio ignore l'ordre et les mots en trop : « WHISKY 40 70CL »
        # et « WHISKY BTL 0.7L 40 » decrivent le meme produit.
        return max(fuzz.token_set_ratio(a, b), fuzz.ratio(a, b)) / 100.0
    except ImportError:  # pragma: no cover - rapidfuzz est une dependance
        import difflib

        return difflib.SequenceMatcher(None, a, b).ratio()


def brands_conflict(left: Any, right: Any) -> bool:
    """Deux marques differentes et connues : jamais le meme produit.

    Meme nature que la barriere de contenance, et pour la meme raison : un
    Doliprane 1 g et un paracetamol generique 1 g portent la meme molecule, le
    meme dosage, et souvent un libelle enrichi presque identique — mais ce sont
    deux references, deux prix, deux codes. Les fusionner ferait disparaitre un
    produit du catalogue du magasin, silencieusement.

    Une marque VIDE ne separe rien. Le LLM ne la trouve pas toujours, et il ne
    tourne pas toujours : traiter « inconnue » comme « differente » scinderait
    les familles exactement comme le faisait la comparaison entre libelle
    enrichi et libelle brut.
    """
    a = (getattr(left, "brand", "") or "").strip().upper()
    b = (getattr(right, "brand", "") or "").strip().upper()
    return bool(a) and bool(b) and a != b


def comparison_texts(record: Any) -> tuple[str, str]:
    """Les deux ecritures d'une fiche : (libelle normalise, libelle enrichi).

    Le second est vide tant que le LLM n'a pas repondu, ou quand sa proposition
    est passee sous le seuil de confiance.
    """
    enriched = getattr(record, "label_enriched", "") or ""
    return record.label_normalized, normalize_label(enriched) if enriched else ""


def pair_similarity(left: Any, right: Any) -> float:
    """Compare ce qui est comparable : brut contre brut, enrichi contre enrichi.

    Prendre « l'enrichi s'il existe, sinon le brut » melangeait les deux
    ecritures. L'enrichissement etant partiel par nature — le LLM ne repond pas
    sur tout, et ce qui passe sous le seuil de confiance n'est pas applique —
    une meme famille se retrouvait moitie en francais lisible, moitie en langage
    de caisse, et le fuzzy comparait « Tomates grappe France 1 kg » a « TOM GRA
    FR KG ». Score sous le seuil : la famille se coupait en deux.

    Mesure sur le catalogue propre (200 lignes, 10 produits, 154 libelles
    enrichis sur 200) : 12 fiches en sortie au lieu de 10, deux familles
    scindees en 17+3 et 11+9. Le meme fichier sans LLM donnait 10.

    Le libelle brut existe toujours : il sert de plancher commun. L'enrichi
    n'entre en jeu que si les DEUX fiches en ont un — c'est la qu'il apporte le
    signal mesure (27 % des vraies paires invisibles sur le libelle brut).
    """
    raw_left, enriched_left = comparison_texts(left)
    raw_right, enriched_right = comparison_texts(right)

    score = _similarity(raw_left, raw_right)
    if enriched_left and enriched_right:
        score = max(score, _similarity(enriched_left, enriched_right))
    return score


def _candidate_pairs(indices: list[int], records: list[Any]) -> Iterator[tuple[int, int]]:
    """Les paires a comparer dans un bloc.

    En dessous de `MAX_EXACT_BLOCK`, toutes les paires : le resultat est exact,
    et c'est le cas de tous les fichiers vus jusqu'ici (plus gros bloc mesure :
    29 fiches sur le catalogue reel).

    Au-dela, le cout devient intenable — le nombre de paires croit avec le CARRE
    de la taille du bloc, pas avec le nombre de fiches. Mesure : 5 000 fiches
    reparties en 300 blocs coutent 0,11 s, les memes 5 000 en 20 blocs coutent
    1,68 s, et 10 000 en 20 blocs coutent 7,1 s. On bascule alors sur une
    fenetre glissante : les fiches sont triees par libelle et chacune n'est
    comparee qu'a ses `WINDOW` voisines. Deux ecritures d'un meme produit se
    ressemblent, donc se trient cote a cote.

    Ce repli est un compromis assume, et il est journalise : il peut manquer une
    paire dont les deux ecritures divergent des la premiere lettre. Mieux vaut
    un doublon rate qu'un traitement qui n'aboutit pas — et le journal dit quand
    le cas s'est produit, au lieu de le taire.
    """
    if len(indices) <= MAX_EXACT_BLOCK:
        for i, left in enumerate(indices):
            for right in indices[i + 1 :]:
                yield left, right
        return

    ordonnes = sorted(indices, key=lambda i: records[i].label_normalized)
    for position, left in enumerate(ordonnes):
        for right in ordonnes[position + 1 : position + 1 + WINDOW]:
            yield left, right


def resolve(
    records: list[Any], forced: Mapping[str, str] | None = None
) -> tuple[list[list[int]], dict[str, int], list[UnresolvedPair]]:
    """Regroupe les fiches. Retourne les groupes d'indices et le funnel.

    `forced` porte les fusions deja tranchees par un humain : libelle normalise
    absorbe -> libelle normalise survivant. Elles s'appliquent AVANT toute
    comparaison textuelle et sans seuil — un humain a regarde les deux fiches,
    aucun score n'a a le contredire. C'est ce qui fait qu'une decision prise
    aujourd'hui n'est pas a reprendre demain matin au depot suivant.
    """
    blocks: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        blocks[blocking_key(record)].append(index)

    total_pairs = len(records) * (len(records) - 1) // 2
    blocked_pairs = sum(len(b) * (len(b) - 1) // 2 for b in blocks.values())

    # Union-find : une fusion transitive (A~B, B~C) donne un seul groupe.
    parent = list(range(len(records)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Les decisions humaines d'abord : elles ne dependent ni du blocking ni du
    # seuil. Deux fiches que quelqu'un a declarees identiques le restent, meme
    # si leur contenance ou leur marque a ete lue differemment — c'est
    # justement ce genre d'erreur de lecture qu'une fusion manuelle repare.
    human_merged = 0
    if forced:
        by_key: dict[str, list[int]] = defaultdict(list)
        for index, record in enumerate(records):
            key = record.label_normalized
            by_key[forced.get(key, key)].append(index)
        for indices in by_key.values():
            for other in indices[1:]:
                union(indices[0], other)
                human_merged += 1

    merged = 0
    ambiguous = 0
    brand_blocked = 0
    windowed_blocks = 0
    grey_zone: list[tuple[int, int, float]] = []
    largest_block = max((len(b) for b in blocks.values()), default=0)

    compared = 0
    for indices in blocks.values():
        for left, right in _candidate_pairs(indices, records):
            compared += 1
            # La marque tranche avant le texte : aucune ressemblance de
            # libelle ne rend deux marques identiques.
            if brands_conflict(records[left], records[right]):
                brand_blocked += 1
                continue
            score = pair_similarity(records[left], records[right])
            if score >= MERGE_THRESHOLD:
                union(left, right)
                merged += 1
            elif score >= REJECT_THRESHOLD:
                # Zone grise : on ne fusionne pas d'office. Une fusion a tort
                # est invisible et irreversible ; un doublon restant se voit.
                # La paire part en revue humaine plutot que d'etre oubliee.
                ambiguous += 1
                grey_zone.append((left, right, score))
        if len(indices) > MAX_EXACT_BLOCK:
            windowed_blocks += 1

    if largest_block > MAX_EXACT_BLOCK:
        log.warning(
            "entity_resolution.block_too_large",
            largest_block=largest_block,
            limit=MAX_EXACT_BLOCK,
            windowed_blocks=windowed_blocks,
            detail="comparaison par fenetre glissante : des paires eloignees "
            "dans l'ordre alphabetique ont pu etre manquees",
        )

    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        groups[find(index)].append(index)

    # Une paire ambigue dont les deux fiches ont FINI dans le meme groupe n'a
    # plus lieu d'etre : la transitivite l'a tranchee (A~B doutait, mais A et B
    # ont tous deux fusionne avec C). Ne restent que les doutes reels.
    unresolved = [
        UnresolvedPair(
            left_key=records[left].key,
            right_key=records[right].key,
            left_label=records[left].label,
            right_label=records[right].label,
            score=score,
        )
        for left, right, score in grey_zone
        if find(left) != find(right)
    ]

    funnel = {
        "records_in": len(records),
        "pairs_without_blocking": total_pairs,
        "pairs_after_blocking": blocked_pairs,
        "blocks": len(blocks),
        "pairs_merged": merged,
        # Le cout est quadratique DANS un bloc : c'est cette taille qu'il faut
        # surveiller, pas le nombre de fiches. Sans ce compteur, la degradation
        # arriverait sans prevenir le jour ou un magasin depose un catalogue
        # concentre sur peu de categories.
        "largest_block": largest_block,
        "windowed_blocks": windowed_blocks,
        # Ce qui a REELLEMENT ete compare. `pairs_after_blocking` reste le
        # compte theorique du bloc ; les deux different des qu'une fenetre
        # entre en jeu, et confondre les deux masquerait justement le repli.
        "pairs_compared": compared,
        "pairs_ambiguous": ambiguous,
        # Compte visible dans l'interface : une barriere qu'on ne mesure pas
        # est une barriere dont on ignore si elle protege ou si elle decoupe.
        "pairs_blocked_by_brand": brand_blocked,
        "pairs_merged_by_human": human_merged,
        "records_out": len(groups),
    }
    funnel["pairs_unresolved"] = len(unresolved)
    return list(groups.values()), funnel, unresolved


def run(
    df: pl.DataFrame,
    ctx: RunContext,
    records: list[Any] | None = None,
    forced: Mapping[str, str] | None = None,
) -> StepResult:
    """Cette etape travaille sur les GOLDEN RECORDS produits par l'etape 4a,
    pas sur les lignes : sa cardinalite est celle des fiches."""
    started = time.perf_counter()

    if not records:
        metrics = StepMetrics(step=STEP, rows_in=df.height, rows_out=df.height)
        metrics.duration_ms = (time.perf_counter() - started) * 1000
        log.info("step.skipped", step=STEP, reason="aucun golden record en entree")
        return StepResult(df=df, metrics=metrics)

    merged, funnel, unresolved = merge_records(records, ctx, forced)
    metrics = StepMetrics(
        step=STEP,
        rows_in=len(records),
        rows_out=len(merged),
        duration_ms=(time.perf_counter() - started) * 1000,
        counters=funnel,
    )
    log.info("step.done", step=STEP, **funnel)
    return StepResult(
        df=df,
        metrics=metrics,
        payload={"golden_records": merged, "unresolved_pairs": unresolved},
    )


def merge_records(
    records: list[Any], ctx: RunContext, forced: Mapping[str, str] | None = None
) -> tuple[list[Any], dict[str, int], list[UnresolvedPair]]:
    """Applique la resolution a une liste de golden records et fusionne.

    Regles de survivorship identiques a l'etape 4a : le libelle le plus
    complet, l'EAN qui passe le checksum, l'URL qui repond, la taxonomie la
    plus profonde. La TVA n'est jamais fusionnee d'autorite : un desaccord
    marque le groupe en conflit et l'envoie en revue.
    """
    groups, funnel, unresolved = resolve(records, forced)
    if len(groups) == len(records):
        return records, funnel, unresolved

    merged: list[Any] = []
    for indices in groups:
        members = [records[i] for i in indices]
        if len(members) == 1:
            merged.append(members[0])
            continue

        survivor = max(members, key=lambda r: (r.source_rows, len(r.label)))

        # Les lignes sources des fiches absorbees suivent la fusion. Sans cela
        # `source_rows` annoncait un total dont les identifiants n'existaient
        # plus : la fiche disait « 945 lignes » et n'en savait nommer que 12.
        # C'est le fil qui relie la fiche aux identifiants du magasin, donc il
        # ne doit jamais etre coupe par une fusion.
        survivor.source_refs = [ref for m in members for ref in m.source_refs]
        survivor.source_rows = sum(m.source_rows for m in members)

        # Identite stable d'un depot a l'autre : la cle la plus petite du
        # groupe, pas celle du membre le plus volumineux. Les volumes varient
        # chaque jour, donc choisir par le volume ferait changer l'identite
        # d'un produit qui n'a pas bouge, et casserait le rattachement des
        # lignes du magasin comme l'historique des corrections.
        survivor.key = min(m.key for m in members)

        survivor.ean = next((m.ean for m in members if m.ean), survivor.ean)
        working = next((m for m in members if m.url_ok), None)
        if working is not None:
            survivor.url_image, survivor.url_ok = working.url_image, True
            survivor.url_checked = working.url_checked
        deepest = max(members, key=lambda r: sum(1 for level in r.taxonomy if level))
        survivor.taxonomy = deepest.taxonomy

        # Un desaccord de TVA entre fiches fusionnees est exactement le genre
        # de decision qui appartient a l'humain.
        rates = {m.vat_rate for m in members if m.vat_rate is not None}
        if len(rates) > 1:
            survivor.vat_conflict = True
            survivor.publishable = False
        merged.append(survivor)

    return merged, funnel, unresolved
