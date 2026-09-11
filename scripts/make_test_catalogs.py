#!/usr/bin/env python3
"""Génère 5 catalogues de test, chacun conçu pour exercer un comportement précis.

Un jeu de test utile n'est pas un jeu « varié » : c'est un jeu dont on connaît
la réponse attendue avant de lancer. Chaque fichier ci-dessous a un résultat
prévisible, écrit dans docs/jeux-de-test.md — si la plateforme s'en écarte,
c'est elle qui a un problème, pas le fichier.

Usage : python3 scripts/make_test_catalogs.py
"""

from __future__ import annotations

import csv
import random
import uuid
from pathlib import Path

OUT = Path("data/samples/tests")

# Les 10 chemins du référentiel, avec le taux de TVA attendu pour chacun.
FAMILIES = [
    ("POM B 1K C1", ["ALIMENTAIRE", "FRUITS ET LEGUMES", "FRUITS", "POMMES"], "5.5"),
    ("TOM GRA FR KG", ["ALIMENTAIRE", "FRUITS ET LEGUMES", "LEGUMES", "TOMATES"], "5.5"),
    ("WHISKY ECOSSAIS 70 CL", ["ALIMENTAIRE", "BOISSONS", "ALCOOLS", "SPIRITUEUX"], "20"),
    ("COCA COLA ZERO 1L", ["ALIMENTAIRE", "BOISSONS", "SODAS", "COLA"], "5.5"),
    ("NUTELLA 750 G", ["ALIMENTAIRE", "EPICERIE SUCREE", "PATES A TARTINER", "NOISETTE CACAO"], "5.5"),
    ("CAFE MLU 250G", ["ALIMENTAIRE", "EPICERIE SUCREE", "CAFE THE", "CAFE MOULU"], "5.5"),
    ("CREME FRAICHE 30 20CL", ["ALIMENTAIRE", "PRODUITS FRAIS", "CREMERIE", "CREMES"], "5.5"),
    ("LESS LIQUIDE 200CL", ["NON ALIMENTAIRE", "ENTRETIEN", "LESSIVE", "LIQUIDE"], "20"),
    ("COUCHES PAMP TAILLE 4", ["NON ALIMENTAIRE", "BEBE", "COUCHES", "TAILLE 4"], "20"),
    ("DOLIPRANE 1 G", ["SANTE", "MEDICAMENTS", "ANTALGIQUES", "PARACETAMOL"], "2.1"),
]

COLUMNS = [
    "id_produit", "ean", "nom", "url_image",
    "taxonomie_niveau_1", "taxonomie_niveau_2",
    "taxonomie_niveau_3", "taxonomie_niveau_4", "tva",
]


def gs1_check_digit(body: str) -> str:
    """Chiffre de contrôle GS1 : en partant de la DROITE, le premier pèse 3."""
    digits = [int(c) for c in body]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def valid_ean13(seed: int) -> str:
    """EAN-13 dont la clé de contrôle GS1 tombe juste, hors préfixe 2
    (réservé à l'usage interne, que le pipeline route différemment)."""
    body = f"376{seed:09d}"[:12]
    return body + gs1_check_digit(body)


def ean13_leading_zeros(seed: int) -> str:
    """EAN-13 canonique commençant par des zéros : sa forme « zéros perdus »
    (telle qu'un tableur l'abîme) redevient valide par zfill(13)."""
    body = f"000{seed:09d}"
    return body + gs1_check_digit(body)


def plu_code(rng: random.Random) -> str:
    """Code court interne (PLU) qui ne devient JAMAIS un GTIN valide par ajout
    de zéros de tête : sans ce filtre, ~1 code sur 10 serait « réparé » par
    accident et les comptages attendus deviendraient flous."""
    while True:
        v = str(rng.randint(100, 99999))
        if all(
            gs1_check_digit(v.zfill(length)[:-1]) != v.zfill(length)[-1]
            for length in (8, 12, 13, 14)
        ):
            return v


def internal_ean8(seed: int) -> str:
    """EAN-8 à checksum valide MAIS préfixe 20-29, réservé par GS1 à l'usage
    interne (codes balance) : à router en code interne, pas en EAN produit.
    Le fichier réel en porte 1 438."""
    body = f"20{seed % 100000:05d}"
    return body + gs1_check_digit(body)


def valid_gtin14(seed: int) -> str:
    """GTIN-14 (indicateur de conditionnement + 12 chiffres + clé recalculée
    sur 13) : le checksum est juste sur 14, faux sur les 13 de queue — c'est le
    carton scanné du fichier réel, à signaler, jamais à convertir."""
    body = "1376" + f"{seed:09d}"
    return body + gs1_check_digit(body)


def mojibake(s: str) -> str:
    """UTF-8 relu en cp1252 : « É » devient « Ã‰ », comme dans le fichier réel.
    Le mojibake est DANS le texte, le fichier reste décodable UTF-8."""
    return s.encode("utf-8").decode("cp1252")


def write(name: str, rows: list[dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    # BOM + CRLF : on reproduit exactement le format du fichier réel, sinon on
    # testerait un cas plus facile que la production.
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {path}  ({len(rows)} lignes)")


def row(nom: str, ean: str, url: str, tax: list[str], tva: str) -> dict[str, str]:
    return {
        "id_produit": str(uuid.uuid4()),
        "ean": ean,
        "nom": nom,
        "url_image": url,
        "taxonomie_niveau_1": tax[0],
        "taxonomie_niveau_2": tax[1],
        "taxonomie_niveau_3": tax[2],
        "taxonomie_niveau_4": tax[3],
        "tva": tva,
    }


def catalogue_propre() -> None:
    """1. Tout est correct. Sert à vérifier que le pipeline n'invente pas de
    problèmes : un contrôle qui ne trouve rien sur un fichier propre vaut
    autant qu'un contrôle qui trouve tout sur un fichier cassé.

    Le libellé est IDENTIQUE pour toutes les lignes d'un même produit. La
    première version suffixait chaque ligne (« POM B 1K C1 REF042 »), ce qui
    donnait 200 libellés distincts pour 10 produits : la déduplication ne
    jouait plus, le LLM payait 200 appels au lieu de 10, et le suffixe se
    retrouvait dans le libellé final du catalogue. Un vrai fichier de caisse
    répète le même libellé, c'est précisément pourquoi il y a des doublons.
    """
    rows = []
    for i in range(200):
        nom, tax, tva = FAMILIES[i % len(FAMILIES)]
        rows.append(
            row(
                nom,
                valid_ean13(100000 + i),
                f"https://picsum.photos/seed/clean-{i}/640/480",
                tax,
                tva,
            )
        )
    write("01-catalogue-propre.csv", rows)


def catalogue_catastrophe() -> None:
    """2. Fichier corrompu. Doit déclencher la QUARANTAINE : au-delà du seuil
    d'anomalies bloquantes, rien n'est intégré. C'est le cas d'un magasin dont
    l'export a changé de format pendant la nuit."""
    rows = []
    for i in range(300):
        nom, tax, tva = FAMILIES[i % len(FAMILIES)]
        if i % 10 < 7:
            # 70 % de lignes bloquantes : libellé vide ou URL inexploitable.
            rows.append(
                row(
                    "" if i % 2 else f"CAFÃ‰ {nom}",
                    "abc-pas-un-ean" if i % 3 else "",
                    "ftp://serveur.interne/img.png" if i % 2 else "not-a-url",
                    ["ALIMENTAIRE", "", "", ""],
                    "" if i % 4 else "33",
                )
            )
        else:
            rows.append(
                row(nom, valid_ean13(200000 + i),
                    f"https://picsum.photos/seed/x-{i}/640/480", tax, tva)
            )
    write("02-catalogue-catastrophe.csv", rows)


def catalogue_tva() -> None:
    """3. TVA légale mais appliquée au mauvais produit. Tous les taux sont
    valides, donc le contrôle syntaxique ne trouve RIEN : seul le contrôle
    croisé les détecte. Doit produire une décision groupée par produit."""
    rows = []
    rng = random.Random(42)
    for nom, tax, tva in FAMILIES:
        for i in range(40):
            # 8 lignes sur 40 portent un taux légal mais faux pour ce produit.
            faux = rng.choice([t for t in ("5.5", "20", "2.1", "10") if t != tva])
            rows.append(
                row(
                    nom,
                    valid_ean13(300000 + len(rows)),
                    f"https://picsum.photos/seed/vat-{len(rows)}/640/480",
                    tax,
                    faux if i < 8 else tva,
                )
            )
    write("03-catalogue-tva-incoherente.csv", rows)


def catalogue_doublons() -> None:
    """4. Le même produit écrit de plusieurs façons — mais des façons qui se
    ramènent à la MÊME chaîne après normalisation (casse, accents, espaces,
    ponctuation). Doit tout regrouper.

    Cas volontairement absent : les variantes réellement différentes
    (« COC COL ZER 1L »), que seule la résolution d'entités floue attraperait.
    Ce fichier teste ce qui existe, pas ce qui est promis.
    """
    rows = []
    for nom, tax, tva in FAMILIES:
        variantes = [
            nom,
            nom.lower(),
            f"  {nom}  ",
            nom.replace(" ", "  "),
            f"{nom}.",
            nom.replace("E", "É") if "E" in nom else nom,
        ]
        for i, variante in enumerate(variantes * 10):
            rows.append(
                row(
                    variante,
                    valid_ean13(400000 + len(rows)),
                    f"https://picsum.photos/seed/dup-{len(rows)}/640/480",
                    tax,
                    tva,
                )
            )
    write("04-catalogue-doublons.csv", rows)


# Trois libellés de caisse par famille, comme dans le fichier réel où le même
# produit s'écrit de plusieurs façons selon la caisse qui l'a saisi.
VARIANTS: dict[str, list[str]] = {
    "POM B 1K C1": ["POM B 1K C1", "POMME BIO 1KG CAT1", "POMMES GOLDEN 1 KG"],
    "TOM GRA FR KG": ["TOM GRA FR KG", "TOMATES GRAPPE", "TOMATE GRAPPE FR 1KG"],
    "WHISKY ECOSSAIS 70 CL": [
        "WHISKY ECOSSAIS 70 CL", "WHISKY ECOS 70CL", "WHISKY SCOTCH 0.7L",
    ],
    "COCA COLA ZERO 1L": ["COCA COLA ZERO 1L", "COKA COLA ZERO 1 L", "COC COL ZER 1L"],
    "NUTELLA 750 G": [
        "NUTELLA 750 G", "PATE TART NUTELLA 0.75KG", "NUTELLA PATE TARTINER 750G",
    ],
    "CAFE MLU 250G": ["CAFE MLU 250G", "CAFE PUR ARAB 250 GR", "ARABICA MOULU 0.25KG"],
    "CREME FRAICHE 30 20CL": [
        "CREME FRAICHE 30 20CL", "CREME FR. EPAISSE 20 CL", "CREME FRAICHE ENT 20CL",
    ],
    "LESS LIQUIDE 200CL": ["LESS LIQUIDE 200CL", "LESSIVE 2L", "LESSIVE LIQ 2 L"],
    "COUCHES PAMP TAILLE 4": [
        "COUCHES PAMP TAILLE 4", "COUCHES T4 PAMPERS", "PAMPERS T.4",
    ],
    "DOLIPRANE 1 G": ["DOLIPRANE 1 G", "DOLIPRANE 1G CPR", "PARACETAMOL DOLIP 1G"],
}

# Formes accentuées qui, une fois mojibakées, donnent les motifs du fichier
# réel (« CAFÃ‰ PUR ARAB 250 GR », « CRÃˆME FR. EPAISSE 20 CL »).
ACCENTED = {
    "CAFE PUR ARAB 250 GR": "CAFÉ PUR ARAB 250 GR",
    "CREME FR. EPAISSE 20 CL": "CRÈME FR. EPAISSE 20 CL",
}


def catalogue_realiste_5k() -> None:
    """5. Réplique réaliste à mi-échelle du fichier de production : 5 000
    lignes, 10 familles × 3 libellés de caisse, et le MÊME cocktail de défauts
    que celui mesuré dans docs/profiling.md — mais en proportions EXACTES et
    connues d'avance (les ensembles d'indices sont disjoints par champ).

    Comptages injectés, à retrouver dans le rapport du run :
      EAN   : 650 vides, 190 zéros perdus (réparables), 165 zéros en trop
              (réparables), 65 codes internes courts (routés), 700 EAN-8 à
              préfixe balance 20-29 (routés), 600 GTIN-14 (signalés, jamais
              convertis), 2 630 valides.
      URL   : 400 vides, 40 sans schéma (réparées mais domaine mort), 20 en
              htp:// (réparées ET vivantes), 20 en ftp:// (signalées),
              210 sur domaine irrésoluble, 4 310 vivantes (picsum).
      Nom   : 8 mojibake, 40 « ORIGINE ITALIA » collés, 36 « FAMILY SIZE »
              collés (à détacher).
      Taxo  : 200 tronquées après n1 (LLM), 125 trouées en n2 (réparables par
              règle), 36 mojibake n2, 16 mojibake n3.
      TVA   : 80 taux légaux mais faux pour la famille (8 par famille),
              JAMAIS corrigés automatiquement.
    Attendu (mesuré, run offline) : PAS de quarantaine, 71 incohérences de TVA
    détectées — les 9 autres sont 3 lignes à taxonomie tronquée (invérifiables)
    et ~6 taux tirés à 10 % sur PARACETAMOL/COLA, que vat_rules.yaml tolère —
    32 libellés distincts après ingestion (30 + les 2 formes accentuées
    restaurées du mojibake), 26 fiches sans LLM — 18 avec, car les
    quasi-doublons fusionnent sur le libellé enrichi. 0 fiche publiable tant
    que la revue n'a pas tranché les conflits de TVA : voulu, chaque famille
    en porte. Détail dans docs/jeux-de-test.md.
    """
    rng = random.Random(5000)
    n = 5000
    per_family = n // len(FAMILIES)

    # Un tirage global déterministe, découpé en tranches DISJOINTES par champ :
    # chaque ligne porte au plus un défaut d'EAN et un défaut d'URL, donc
    # chaque comptage du rapport a une seule cause possible.
    shuffled = list(range(n))
    rng.shuffle(shuffled)
    cursor = 0

    def take(k: int) -> set[int]:
        nonlocal cursor
        out = set(shuffled[cursor : cursor + k])
        cursor += k
        return out

    ean_empty, ean_lost_zeros, ean_extra_zeros = take(650), take(190), take(165)
    ean_internal, ean_balance, ean_carton = take(65), take(700), take(600)

    cursor = 0
    rng.shuffle(shuffled)
    url_empty, url_noscheme, url_htp = take(400), take(40), take(20)
    url_ftp, url_dead_domain = take(20), take(210)

    cursor = 0
    rng.shuffle(shuffled)
    tax_truncated, tax_holed = take(200), take(125)

    cursor = 0
    rng.shuffle(shuffled)
    vat_wrong_pool = take(2000)  # filtré à 8 par famille ci-dessous
    # Les 12 premières lignes de chaque famille sont réservées aux mojibake du
    # nom : les suffixes se collent ailleurs, pour qu'aucune ligne ne cumule
    # deux défauts de libellé et que chaque comptage garde une cause unique.
    eligible = [i for i in shuffled[cursor:] if i % per_family >= 12]
    suffix_italia, suffix_family = set(eligible[:40]), set(eligible[40:76])

    vat_wrong_by_family: dict[int, int] = {}
    rows = []
    moji_nom = moji_n2 = moji_n3 = 0
    for i in range(n):
        fam_idx = i // per_family
        nom_canon, tax, tva = FAMILIES[fam_idx]
        nom = VARIANTS[nom_canon][i % 3]

        # 8 mojibake dans le nom : sur les formes accentuées connues.
        if nom in ACCENTED and moji_nom < 8 and i % per_family < 12:
            nom = mojibake(ACCENTED[nom])
            moji_nom += 1
        elif i in suffix_italia:
            nom = f"{nom}ORIGINE ITALIA"
        elif i in suffix_family:
            nom = f"{nom}FAMILY SIZE"

        if i in ean_empty:
            ean = ""
        elif i in ean_lost_zeros:
            ean = ean13_leading_zeros(500000 + i).lstrip("0")
        elif i in ean_extra_zeros:
            ean = "000" + valid_ean13(500000 + i)
        elif i in ean_internal:
            ean = plu_code(rng)
        elif i in ean_balance:
            ean = internal_ean8(500000 + i)
        elif i in ean_carton:
            ean = valid_gtin14(500000 + i)
        else:
            ean = valid_ean13(500000 + i)

        row_id = str(uuid.uuid4())
        if i in url_empty:
            url = ""
        elif i in url_noscheme:
            url = f"www.store.example/img/{row_id}.jpg"
        elif i in url_htp:
            url = f"htp://picsum.photos/seed/r5k-{i}/640/480"
        elif i in url_ftp:
            url = f"ftp://files.store.example/img/{row_id}.png"
        elif i in url_dead_domain:
            # La moitié porte en plus le chemin /missing/ du fichier réel.
            path = f"missing/{row_id}" if i % 2 else f"img/{row_id}"
            url = f"https://cdn.store.example/{path}.jpg"
        else:
            url = f"https://picsum.photos/seed/r5k-{i}/640/480"

        t1, t2, t3, t4 = tax
        if i in tax_truncated:
            t2 = t3 = t4 = ""
        elif i in tax_holed:
            t2 = ""
        elif t2 == "EPICERIE SUCREE" and moji_n2 < 36:
            t2 = mojibake("ÉPICERIE SUCREE")
            moji_n2 += 1
        elif t3 == "CREMERIE" and moji_n3 < 16:
            t3 = mojibake("CRÈMERIE")
            moji_n3 += 1

        if i in vat_wrong_pool and vat_wrong_by_family.get(fam_idx, 0) < 8:
            vat_wrong_by_family[fam_idx] = vat_wrong_by_family.get(fam_idx, 0) + 1
            tva_out = rng.choice([t for t in ("5.5", "20", "2.1", "10") if t != tva])
        else:
            tva_out = tva

        rows.append(
            {
                "id_produit": row_id,
                "ean": ean,
                "nom": nom,
                "url_image": url,
                "taxonomie_niveau_1": t1,
                "taxonomie_niveau_2": t2,
                "taxonomie_niveau_3": t3,
                "taxonomie_niveau_4": t4,
                "tva": tva_out,
            }
        )

    assert moji_nom == 8 and moji_n2 == 36 and moji_n3 == 16
    assert sum(vat_wrong_by_family.values()) == 80
    write("05-catalogue-realiste-5k.csv", rows)


if __name__ == "__main__":
    print("Génération des jeux de test :")
    catalogue_propre()
    catalogue_catastrophe()
    catalogue_tva()
    catalogue_doublons()
    catalogue_realiste_5k()
