"""Le document de reponse ne peut pas contredire le pipeline.

Le document affirme des dizaines de chiffres sur le fichier fourni, et son
en-tete promet qu'ils sont verifies a chaque integration continue. Sans ce
test, cette phrase serait une intention : rien n'empecherait le pipeline de
deriver pendant que le document reste fige, ni l'inverse.

Le principe : chaque chiffre publie est extrait du .tex par son motif, puis
recalcule depuis `data/samples/store_listing_produit.csv`. Un ecart fait
echouer la CI. C'est volontairement rigide — un chiffre publie qui n'est plus
vrai est pire qu'un chiffre absent, parce qu'il sera lu comme mesure.

Deux regles de lecture, pour que le test dise ce qu'il mesure :

* Les comptages de la section de profilage portent sur le fichier BRUT, avant
  tout traitement. C'est ce que promet l'en-tete du document : des faits du
  fichier, pas des sorties du pipeline.
* Les comptages de l'entonnoir portent sur ce que le pipeline voit REELLEMENT
  a cette etape, donc apres ingestion. Les deux nombres different (343 contre
  273) et cette difference est un fait, pas une incoherence : le detachement
  des suffixes marketing fusionne des libelles.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from pathlib import Path

import polars as pl
import pytest

from pipeline.config import PipelineConfig
from pipeline.context import RunContext
from pipeline.normalize import analyse_ean
from pipeline.steps import cross_checks, dedup, entity_resolution, field_checks, ingest

ROOT = Path(__file__).resolve().parent.parent
SOURCE_CSV = ROOT / "data" / "samples" / "store_listing_produit.csv"

# Le document de reponse fait foi. Il n'est jamais regenere, seulement edite.
DOCUMENT = ROOT / "exercice" / "ULTY_Cas_pratique_Automatisation.tex"

# Famille cola : le motif retenu couvre les abreviations du fichier
# (« COC COL ZER 1L », « COCA Z S/SUCRE 1L »), qu'un filtre sur la sous-chaine
# « COLA » raterait. Ce detail vaut 13 ecritures sur 36.
COLA = re.compile(r"CO[CK]")


@pytest.fixture(scope="module")
def mesures() -> dict[str, int]:
    """Tous les chiffres du document, recalcules depuis le fichier fourni."""
    if not SOURCE_CSV.exists():  # pragma: no cover - garde-fou d'environnement
        pytest.skip(f"fichier de reference absent : {SOURCE_CSV}")

    brut = SOURCE_CSV.read_bytes().decode("utf-8-sig", "replace")
    noms_bruts = [(row.get("nom") or "") for row in csv.DictReader(io.StringIO(brut))]

    ctx = RunContext(
        store_id="doc",
        source_file=SOURCE_CSV,
        config=PipelineConfig.load(),
        run_id="doc",
        network_enabled=False,  # aucun test ne depend d'un service tiers
    )
    raw = ingest.read_csv(SOURCE_CSV)
    apres_ingestion = ingest.run(raw, ctx)
    champs = field_checks.run(apres_ingestion.df, ctx)
    croises = cross_checks.run(champs.df, ctx)

    codes = Counter(
        a.code.value for a in apres_ingestion.anomalies + champs.anomalies + croises.anomalies
    )
    regles = Counter(
        c.rule for c in apres_ingestion.corrections + champs.corrections + croises.corrections
    )
    ean = Counter(analyse_ean(code).status for code in raw["ean"].to_list())

    records = dedup.run(croises.df, ctx).payload["golden_records"]
    resolution = entity_resolution.run(croises.df, ctx, records)

    identifiants = raw["id_produit"].to_list()
    codes_barres = [e for e in raw["ean"].to_list() if e and str(e).strip()]
    urls = [u or "" for u in raw["url_image"].to_list()]
    distincts_apres = len({n for n in apres_ingestion.df["nom"].to_list() if n})

    whisky = raw.filter(pl.col("nom").str.to_uppercase().str.contains("WHISKY"))
    tva_whisky = Counter(whisky["tva"].to_list())
    niveaux_whisky = Counter(whisky["taxonomie_niveau_2"].to_list())
    urls_whisky = [u or "" for u in whisky["url_image"].to_list()]
    frequences = [n for _, n in Counter(whisky["nom"].to_list()).most_common(4)]

    return {
        # --- faits bruts du fichier -----------------------------------------
        "lignes": raw.height,
        "libelles_distincts_bruts": len(set(noms_bruts)),
        "doublons_id": len(identifiants) - len(set(identifiants)),
        "doublons_ean": len(codes_barres) - len(set(codes_barres)),
        "cola_ecritures_brutes": len({n for n in noms_bruts if COLA.search(n.upper())}),
        # --- tri des EAN, qui doit totaliser 10 000 -------------------------
        "ean_ok": ean["ok"],
        "ean_padded": ean["padded"],
        "ean_unpadded": ean["unpadded"],
        "ean_not_a_gtin": ean["not_a_gtin"],
        "ean_internal_gs1": ean["internal_gs1"],
        "ean_gtin14": ean["gtin14"],
        "ean_missing": ean["missing"],
        "ean_echecs_checksum": ean["padded"] + ean["unpadded"] + ean["not_a_gtin"],
        # --- anomalies mesurees ---------------------------------------------
        "mojibake": codes.get("mojibake_fixed", 0),
        "suffixes_colles": codes.get("marketing_suffix_detached", 0),
        "url_vides": codes.get("url_missing", 0),
        "url_chemin_missing": sum("/missing/" in u for u in urls),
        "url_malformees": codes.get("url_malformed", 0),
        "url_schema_repare": codes.get("url_scheme_repaired", 0),
        "sans_image_exploitable": (
            codes.get("url_missing", 0)
            + sum("/missing/" in u for u in urls)
            + codes.get("url_malformed", 0)
        ),
        "taxonomy_gap": codes.get("taxonomy_gap", 0),
        "taxonomy_incomplete": codes.get("taxonomy_incomplete", 0),
        "taxonomy_name_mismatch": codes.get("taxonomy_name_mismatch", 0),
        "vat_category_mismatch": codes.get("vat_category_mismatch", 0),
        "categories_fantomes": regles.get("field_checks.taxonomy.canonicalize", 0),
        # --- entonnoir : ce que le pipeline voit REELLEMENT ------------------
        "libelles_apres_ingestion": distincts_apres,
        "appels_llm": -(-distincts_apres // 25),  # lots de 25, arrondi au superieur
        "facteur_economie": round(raw.height / distincts_apres),
        "paires_sans_blocking": resolution.metrics.counters["pairs_without_blocking"],
        "paires_apres_blocking": resolution.metrics.counters["pairs_after_blocking"],
        "golden_records_sans_llm": resolution.metrics.rows_out,
        # --- le whisky, exemple deroule de bout en bout ---------------------
        "whisky_lignes": whisky.height,
        "whisky_libelles": len({n for n in whisky["nom"].to_list() if n}),
        "whisky_freq_1": frequences[0],
        "whisky_freq_2": frequences[1],
        "whisky_freq_3": frequences[2],
        "whisky_freq_4": frequences[3],
        "whisky_ean_vides": sum(1 for e in whisky["ean"].to_list() if not (e or "").strip()),
        "whisky_url_vides": sum(not u.strip() for u in urls_whisky),
        "whisky_chemin_missing": sum("/missing/" in u for u in urls_whisky),
        "whisky_tva_hors_20": sum(v for k, v in tva_whisky.items() if str(k) not in ("20", "20.0")),
        "whisky_tva_10": tva_whisky.get("10", 0),
        "whisky_tva_5_5": tva_whisky.get("5.5", 0),
        "whisky_tva_2_1": tva_whisky.get("2.1", 0),
        "whisky_boissons": niveaux_whisky.get("BOISSONS", 0),
        "whisky_entretien": niveaux_whisky.get("ENTRETIEN", 0),
        "whisky_medicaments": niveaux_whisky.get("MEDICAMENTS", 0),
    }


@pytest.fixture(scope="module")
def texte() -> str:
    if not DOCUMENT.exists():  # pragma: no cover - garde-fou d'environnement
        pytest.skip(f"document de reponse absent : {DOCUMENT}")
    # Les milliers s'ecrivent « 1\,438 » en LaTeX ; on les ramene a « 1438 »
    # pour pouvoir chercher un nombre sans se soucier de sa typographie.
    return DOCUMENT.read_text(encoding="utf-8").replace("\\,", "")


def _nombre(texte: str, motif: str) -> int:
    """Extrait l'entier capture par `motif`, en echouant si le motif a bouge.

    Un motif qui ne correspond plus signifie que la phrase a ete reecrite. Le
    test doit alors echouer bruyamment : une affirmation chiffree qui echappe
    au controle est exactement ce que ce fichier existe pour empecher.
    """
    trouve = re.search(motif, texte)
    assert trouve is not None, (
        f"motif introuvable dans le document : {motif!r}. "
        "Si la phrase a ete reecrite, mettre le motif a jour ici — ne pas "
        "supprimer la verification."
    )
    return int(trouve.group(1).replace(" ", "").replace("\u00a0", ""))


# Chaque entree : (libelle lisible, motif d'extraction, cle de mesure).
CLAIMS: list[tuple[str, str, str]] = [
    ("lignes du fichier", r"\\textbf\{(\d+) lignes brutes\}", "lignes"),
    (
        "libelles distincts (brut)",
        r"\\textbf\{(\d+)\} libellés distincts pour",
        "libelles_distincts_bruts",
    ),
    (
        "libelles bruts (entonnoir)",
        r"\\textbf\{(\d+) libellés distincts\} dans le fichier",
        "libelles_distincts_bruts",
    ),
    ("facteur d'economie", r"soit un facteur (\d+)\.", "facteur_economie"),
    (
        "paires apres blocking evoquees",
        r"blocking~: (\d+) paires potentielles évitées",
        "paires_sans_blocking",
    ),
    ("ecritures du cola", r"Le cola s'écrit de \\textbf\{(\d+)\}", "cola_ecritures_brutes"),
    ("TVA incoherentes", r"\\textbf\{(\d+)\} sont incohérents", "vat_category_mismatch"),
    ("echecs de checksum", r"\\textbf\{(\d+)\} codes ne passent pas", "ean_echecs_checksum"),
    ("EAN completes", r"\\textbf\{(\d+)\} redeviennent valides", "ean_padded"),
    ("EAN dezerotes", r"\\textbf\{(\d+)\} une fois débarrassés", "ean_unpadded"),
    ("codes courts PLU", r"\\textbf\{(\d+)\} n'ont que 3 à 5 chiffres", "ean_not_a_gtin"),
    ("prefixe GS1 interne", r"\\textbf\{(\d+)\} codes en préfixe GS1", "ean_internal_gs1"),
    ("EAN vides", r"\\textbf\{(\d+)\} vides\.", "ean_missing"),
    ("EAN valides tels quels", r"\\textbf\{(\d+)\} valides tels quels", "ean_ok"),
    ("mojibake", r"\\textbf\{(\d+)\} lignes en mojibake", "mojibake"),
    ("suffixes colles", r"\\textbf\{(\d+)\} lignes avec un\nsuffixe marketing", "suffixes_colles"),
    (
        "sans image exploitable",
        r"\\textbf\{(\d+)\} lignes sans image exploitable",
        "sans_image_exploitable",
    ),
    ("URLs vides", r"(\d+) URLs vides", "url_vides"),
    ("chemin /missing/", r"(\d+) pointant vers un chemin", "url_chemin_missing"),
    ("URLs malformees", r"(\d+) malformées", "url_malformees"),
    ("URLs reparables", r"(\d+) URLs réparables", "url_schema_repare"),
    ("niveau 3 sans niveau 2", r"\\textbf\{(\d+)\} lignes ont un niveau 3", "taxonomy_gap"),
    (
        "hierarchie incomplete",
        r"\\textbf\{(\d+)\} ont une hiérarchie incomplète",
        "taxonomy_incomplete",
    ),
    (
        "taxonomie contre nom",
        r"\\textbf\{(\d+)\} taxonomies contredisent",
        "taxonomy_name_mismatch",
    ),
    ("categories fantomes", r"sans cela, (\d+) lignes se retrouvent", "categories_fantomes"),
    ("libelles vus par le LLM", r"\\textbf\{(\d+) libellés enrichis\}", "libelles_apres_ingestion"),
    ("appels au LLM", r"libellés enrichis\} par le LLM \((\d+) appels\)", "appels_llm"),
    (
        "paires sans blocking",
        r"blocking~: (\d+) paires potentielles évitées",
        "paires_sans_blocking",
    ),
    ("golden records hors LLM", r"\\textbf\{(\d+) golden records\}", "golden_records_sans_llm"),
    ("comparaisons utiles", r"ramène\n\s*ça à (\d+) comparaisons utiles", "paires_apres_blocking"),
    ("whisky : lignes", r"\\textbf\{(\d+) lignes\} du fichier décrivent", "whisky_lignes"),
    ("whisky : libelles", r"sous \\textbf\{(\d+)\n?libellés\}", "whisky_libelles"),
    ("whisky : EAN vides", r"Sur les 945 lignes~: (\d+) EAN vides", "whisky_ean_vides"),
    ("whisky : URLs vides", r"EAN vides, (\d+) URLs vides", "whisky_url_vides"),
    ("whisky : /missing/", r"URLs vides et (\d+) images pointant", "whisky_chemin_missing"),
    ("whisky : TVA hors 20 %", r"repère (\d+) lignes hors des 20", "whisky_tva_hors_20"),
    ("whisky : TVA a 10 %", r"alcool~: (\d+) à 10", "whisky_tva_10"),
    ("whisky : TVA a 5,5 %", r"à 10~\\%, (\d+) à 5,5", "whisky_tva_5_5"),
    ("whisky : TVA a 2,1 %", r"à 5,5~\\%, (\d+) à 2,1", "whisky_tva_2_1"),
    ("whisky : en ENTRETIEN", r"repère\n(\d+) lignes classées en ENTRETIEN", "whisky_entretien"),
    (
        "whisky : en MEDICAMENTS",
        r"lignes classées en ENTRETIEN et (\d+) en MEDICAMENTS",
        "whisky_medicaments",
    ),
    ("whisky : en BOISSONS", r"BOISSONS \((\d+) lignes sur 945\)", "whisky_boissons"),
]


@pytest.mark.parametrize("libelle,motif,cle", CLAIMS, ids=[c[0] for c in CLAIMS])
def test_le_document_dit_ce_que_le_pipeline_mesure(
    libelle: str, motif: str, cle: str, texte: str, mesures: dict[str, int]
) -> None:
    publie = _nombre(texte, motif)
    mesure = mesures[cle]
    assert publie == mesure, (
        f"{libelle} : le document publie {publie}, le pipeline mesure {mesure}. "
        "Corriger le document ou expliquer l'ecart — ne pas relacher ce test."
    )


def test_le_tri_des_ean_totalise_le_fichier(mesures: dict[str, int]) -> None:
    """La somme des branches doit faire 10 000, sinon la repartition ment.

    C'est ce controle qui rend visible une erreur de repartition : un total
    juste peut masquer deux moities fausses en sens inverse, et c'est
    exactement ce qui s'etait produit dans une version anterieure du document.
    """
    branches = (
        mesures["ean_ok"]
        + mesures["ean_padded"]
        + mesures["ean_unpadded"]
        + mesures["ean_not_a_gtin"]
        + mesures["ean_internal_gs1"]
        + mesures["ean_gtin14"]
        + mesures["ean_missing"]
    )
    assert branches == mesures["lignes"]


def test_le_fichier_ne_contient_aucun_doublon_exact(mesures: dict[str, int]) -> None:
    """L'affirmation qui justifie tout le travail de resolution d'entites."""
    assert mesures["doublons_id"] == 0
    assert mesures["doublons_ean"] == 0
