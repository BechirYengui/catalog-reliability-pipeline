"""Normalisation de texte et validation d'identifiants. Fonctions pures, sans
etat ni I/O : c'est ici que se concentre la logique metier testable.

Point de vigilance issu du profilage reel : le matching de mots-cles se fait
TOUJOURS par token, jamais par sous-chaine. Une premiere version en `in`
classait « TOM GRAPPE VRAC FRORIGINE ITALIA » comme spiritueux, parce que
« FRORIGINE » contient « GIN ». 89 faux positifs. Voir docs/profiling.md §7.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
# Meme chose, mais en preservant le separateur decimal : sans ca, « 0.75KG »
# devient « 0 75KG » et l'extraction de contenance lit 75 kg au lieu de 0,75 kg.
_NON_ALNUM_KEEP_DECIMAL = re.compile(r"[^A-Z0-9.,]+")

# Contenance : '1L', '70 CL', '250G', '0.75KG', '1K', '200CL'
_QUANTITY = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>KG|CL|ML|L|G|K)\b")

_UNIT_CANONICAL = {"K": "KG", "L": "L", "CL": "CL", "ML": "ML", "G": "G", "KG": "KG"}


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_label(text: str | None) -> str:
    """Forme canonique d'un libelle : sans accents, majuscules, ponctuation
    reduite a des espaces simples. Sert de cle de regroupement."""
    if not text:
        return ""
    return " ".join(_NON_ALNUM.sub(" ", strip_accents(text).upper()).split())


def tokenize(text: str | None) -> set[str]:
    """Tokens normalises. L'unique facon autorisee de tester la presence d'un
    mot-cle dans un libelle."""
    normalized = normalize_label(text)
    return set(normalized.split()) if normalized else set()


def has_any_token(text: str | None, tokens: list[str] | set[str]) -> bool:
    present = tokenize(text)
    return any(normalize_label(t) in present for t in tokens)


def extract_quantity(text: str | None) -> tuple[float, str] | None:
    """Extrait (valeur, unite canonique) du libelle. '0.75KG' -> (0.75, 'KG').

    Utilise en phase 2 comme composante de la cle de blocking : deux produits de
    contenances differentes ne sont jamais le meme produit, meme si les libelles
    se ressemblent enormement ('COCA COLA ZERO 1L' vs 'COCA COLA ZERO 33CL').
    """
    if not text:
        return None
    candidate = " ".join(_NON_ALNUM_KEEP_DECIMAL.sub(" ", strip_accents(text).upper()).split())
    match = _QUANTITY.search(candidate)
    if not match:
        return None
    raw = match.group("value").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:  # pragma: no cover - le regex garantit deja le format
        return None
    return value, _UNIT_CANONICAL[match.group("unit")]


# --------------------------------------------------------------------------
# EAN / GTIN
# --------------------------------------------------------------------------

VALID_GTIN_LENGTHS = (8, 12, 13, 14)


def gtin_checksum_valid(code: str) -> bool:
    """Checksum GS1, valable pour EAN-8, UPC-A, EAN-13 et GTIN-14.

    La cle est le dernier chiffre. On pondere les autres alternativement par 3
    et 1 **en partant de la droite**, ce qui rend l'algorithme independant de la
    longueur — c'est ce qui permet de traiter les 4 formats avec le meme code.
    """
    if not code or not code.isdigit() or len(code) not in VALID_GTIN_LENGTHS:
        return False
    digits = [int(c) for c in code]
    body = digits[:-1][::-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    return (10 - total % 10) % 10 == digits[-1]


class EanVerdict:
    """Resultat d'analyse d'un EAN. Les codes correspondent a AnomalyCode."""

    __slots__ = ("detail", "status", "value")

    def __init__(self, status: str, value: str | None, detail: str = "") -> None:
        self.status = status
        self.value = value
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - confort de debug
        return f"EanVerdict({self.status!r}, {self.value!r}, {self.detail!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EanVerdict):
            return NotImplemented
        return (self.status, self.value) == (other.status, other.value)


def _is_gs1_internal(code: str) -> bool:
    """Prefixes GS1 reserves a l'usage interne (« restricted circulation »).

    - EAN-13 commencant par 20 a 29 ;
    - EAN-8 commencant par 2 (la specification EAN-8 n'a qu'un chiffre de
      prefixe pour cette plage).
    """
    if len(code) == 8:
        return code[0] == "2"
    if len(code) in (13, 14):
        return code[:2] in {f"2{d}" for d in "0123456789"}
    return False


def analyse_ean(raw: str | None) -> EanVerdict:
    """Classe un EAN et le repare quand c'est sur.

    Regle de prudence : une reparation par zeros de tete n'est retenue que si le
    checksum devient valide APRES reparation. Sinon on ne touche a rien et on
    signale. Mesure sur le fichier reel : 635 EAN recuperes sur 768 anomalies,
    sans aucun faux positif possible par construction.

    Statuts :
      ok            deja valide
      missing       vide
      padded        zeros de tete ajoutes -> repare
      unpadded      zeros de tete retires -> repare
      gtin14        code carton (GTIN-14) -> signale, jamais converti
      not_a_gtin    code court type PLU / code interne -> a router, pas a corriger
      invalid       numerique mais irrecuperable -> signalement
    """
    value = (raw or "").strip()
    if not value:
        return EanVerdict("missing", None)

    if not value.isdigit():
        return EanVerdict("invalid", value, "caracteres non numeriques")

    if gtin_checksum_valid(value):
        # GS1 reserve les prefixes 20-29 a l'usage interne du distributeur :
        # produits peses, codes balance, marques de distributeur. Le checksum
        # est valide, mais ce n'est PAS un identifiant produit universel — deux
        # enseignes peuvent porter le meme code pour des produits differents.
        # Le traiter comme un EAN ferait fusionner des produits sans rapport
        # des qu'on rapprochera plusieurs magasins.
        # Mesure sur le fichier reel : 1 438 codes concernes, tous en EAN-8.
        if _is_gs1_internal(value):
            return EanVerdict("internal_gs1", value, "prefixe GS1 reserve a l'usage interne")

        # GTIN-14 : le premier chiffre est l'indicateur de conditionnement.
        if len(value) == 14:
            # Indicateur 0 : meme identifiant que le GTIN-13, avec un zero de
            # rembourrage. Les zeros de tete ne changent pas le chiffre de
            # controle, donc la forme a 13 est la forme canonique du MEME code —
            # sans quoi le referentiel garde le meme produit ecrit de deux
            # facons (mesure : 82 lignes du fichier reel).
            if value[0] == "0" and gtin_checksum_valid(value[1:]):
                return EanVerdict("unpadded", value[1:], "zero de rembourrage GTIN-14 retire")
            if value[0] != "0":
                # Indicateur 1 a 8 : c'est le CARTON, pas l'unite vendue au
                # client. Le checksum est valide et le code designe bien le
                # produit, mais une marketplace attend l'EAN-13 consommateur.
                # On ne le convertit pas : deduire le GTIN-13 demanderait de
                # recalculer un chiffre de controle, donc d'inventer la donnee.
                # Mesure : 1 208 lignes, dont les 13 derniers chiffres ont un
                # checksum FAUX — ce sont de vrais GTIN-14, pas des EAN abimes.
                return EanVerdict(
                    "gtin14",
                    value,
                    f"GTIN-14 (indicateur {value[0]}) : unite logistique, pas l'unite consommateur",
                )
        return EanVerdict("ok", value)

    # Zeros de tete manquants : on tente les longueurs GTIN superieures.
    for length in VALID_GTIN_LENGTHS:
        if len(value) < length:
            candidate = value.zfill(length)
            if gtin_checksum_valid(candidate):
                return EanVerdict("padded", candidate, f"complete a {length} chiffres")

    # Zeros de tete en trop : on retire le rembourrage puis on re-cadre.
    stripped = value.lstrip("0")
    if stripped and stripped != value:
        if gtin_checksum_valid(stripped):
            return EanVerdict("unpadded", stripped, "zeros de tete retires")
        for length in VALID_GTIN_LENGTHS:
            if len(stripped) <= length:
                candidate = stripped.zfill(length)
                if gtin_checksum_valid(candidate):
                    return EanVerdict("unpadded", candidate, f"recadre sur {length} chiffres")

    # GTIN-14 : le premier chiffre est un indicateur de niveau d'emballage.
    if len(value) == 14 and gtin_checksum_valid(value[1:]):
        return EanVerdict("unpadded", value[1:], "indicateur GTIN-14 retire")

    if len(value) < 8:
        return EanVerdict("not_a_gtin", value, "code court : PLU ou code interne")

    return EanVerdict("invalid", value, "checksum GS1 invalide, non reparable")
