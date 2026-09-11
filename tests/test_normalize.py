"""Tests des fonctions pures : normalisation, tokens, EAN."""

from __future__ import annotations

import pytest

from pipeline.normalize import (
    analyse_ean,
    extract_quantity,
    gtin_checksum_valid,
    has_any_token,
    normalize_label,
    tokenize,
)


class TestNormalizeLabel:
    def test_strips_accents_and_punctuation(self) -> None:
        assert normalize_label("CRÈME FR. EPAISSE 20 CL") == "CREME FR EPAISSE 20 CL"

    def test_collapses_whitespace(self) -> None:
        assert normalize_label("  LESS.   LIQUIDE  200CL ") == "LESS LIQUIDE 200CL"

    def test_handles_none_and_empty(self) -> None:
        assert normalize_label(None) == ""
        assert normalize_label("") == ""


class TestTokenMatching:
    """Le matching par token est une regle de securite, pas un detail de style."""

    def test_gin_inside_frorigine_is_not_a_match(self) -> None:
        # NON-REGRESSION. Une premiere version en `x in label` classait cette
        # ligne comme spiritueux : « FRORIGINE » contient « GIN ». 89 faux
        # positifs sur le fichier reel. Voir docs/profiling.md §7.
        label = "TOM GRAPPE VRAC FRORIGINE ITALIA"
        assert "GIN" in label.replace(" ", "")  # le piege existe bel et bien
        assert not has_any_token(label, ["GIN"])  # mais le matching par token n'y tombe pas

    def test_real_alcohol_label_matches(self) -> None:
        assert has_any_token("WHISKY ECOSSAIS 70 CL", ["WHISKY", "VODKA"])

    def test_tokenize_is_accent_insensitive(self) -> None:
        assert "CAFE" in tokenize("CAFÉ PUR ARAB 250 GR")


class TestExtractQuantity:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("COCA COLA ZERO 1L", (1.0, "L")),
            ("WHISKY ECOSSAIS 70 CL", (70.0, "CL")),
            ("CAFE MLU 250G", (250.0, "G")),
            ("PATE TART NUTELLA 0.75KG", (0.75, "KG")),
            ("POM B 1K C1", (1.0, "KG")),
            ("COUCHES PAMP TAILLE 4", None),
        ],
    )
    def test_extracts_value_and_canonical_unit(
        self, label: str, expected: tuple[float, str] | None
    ) -> None:
        assert extract_quantity(label) == expected


class TestGtinChecksum:
    @pytest.mark.parametrize("code", ["3760000000017", "3760000000024", "3760000002790"])
    def test_accepts_real_valid_eans(self, code: str) -> None:
        assert gtin_checksum_valid(code)

    @pytest.mark.parametrize("code", ["3760000000018", "abc", "", "12345"])
    def test_rejects_invalid(self, code: str) -> None:
        assert not gtin_checksum_valid(code)


class TestAnalyseEan:
    def test_valid_ean_is_left_alone(self) -> None:
        verdict = analyse_ean("3760000000017")
        assert verdict.status == "ok"
        assert verdict.value == "3760000000017"

    def test_empty_is_missing(self) -> None:
        assert analyse_ean("").status == "missing"
        assert analyse_ean(None).status == "missing"

    def test_leading_zeros_are_restored_when_checksum_validates(self) -> None:
        # Cas reel du fichier : '00020001018' -> '0000020001018'
        verdict = analyse_ean("00020001018")
        assert verdict.status == "padded"
        assert verdict.value is not None
        assert gtin_checksum_valid(verdict.value)

    def test_extra_leading_zeros_are_removed(self) -> None:
        # Cas reel : '003760000000130'
        verdict = analyse_ean("003760000000130")
        assert verdict.status == "unpadded"
        assert verdict.value is not None
        assert gtin_checksum_valid(verdict.value)

    def test_short_internal_code_is_routed_not_repaired(self) -> None:
        # '31061' est un code PLU/interne : le reparer serait inventer une donnee.
        verdict = analyse_ean("31061")
        assert verdict.status == "not_a_gtin"
        assert verdict.value == "31061"

    def test_repair_never_produces_an_invalid_checksum(self) -> None:
        """Garantie centrale : on ne repare que si le resultat est valide."""
        for raw in ("789", "1076", "00020001018", "003760000000895", "31061", "42"):
            verdict = analyse_ean(raw)
            if verdict.status in ("padded", "unpadded"):
                assert verdict.value is not None
                assert gtin_checksum_valid(verdict.value), f"{raw} -> {verdict.value}"


class TestGtin14:
    """Un code a 14 chiffres recouvre deux realites opposees.

    Mesure sur le fichier reel : 1 290 codes de 14 chiffres, tous avec un
    checksum GS1 valide. 82 sont un GTIN-13 avec un zero de rembourrage — le
    meme identifiant. Les 1 208 autres sont de vrais GTIN-14 : le carton, pas
    l'unite vendue au client.
    """

    def test_le_zero_de_rembourrage_est_retire(self) -> None:
        """Meme code, deux ecritures : le referentiel n'en garde qu'une.

        Les zeros de tete ne changent pas le chiffre de controle, donc les deux
        formes sont valides — et la forme a 13 chiffres est la canonique.
        """
        verdict = analyse_ean("03760000000284")

        assert verdict.status == "unpadded"
        assert verdict.value == "3760000000284"
        assert gtin_checksum_valid(verdict.value)

    def test_un_vrai_gtin14_est_signale_jamais_converti(self) -> None:
        """Le convertir demanderait de recalculer un chiffre de controle."""
        verdict = analyse_ean("13760000000212")

        assert verdict.status == "gtin14"
        # La valeur ressort INTACTE : on ne fabrique pas un EAN-13.
        assert verdict.value == "13760000000212"

    def test_ce_ne_sont_pas_des_ean13_abimes(self) -> None:
        """La preuve : sans le chiffre de tete, le checksum est faux."""
        assert gtin_checksum_valid("13760000000212")
        assert not gtin_checksum_valid("3760000000212")

    def test_un_ean13_ordinaire_reste_intact(self) -> None:
        verdict = analyse_ean("3760000000284")

        assert verdict.status == "ok"
        assert verdict.value == "3760000000284"
