"""Tests de la coherence entre champs.

C'est l'etage qui porte la valeur du controle de TVA : le controle syntaxique
ne trouve rien sur ce fichier, tous les taux presents sont legaux.
"""

from __future__ import annotations

import polars as pl

from pipeline.context import RunContext
from pipeline.models import AnomalyCode
from pipeline.steps import cross_checks, field_checks


def _prepared(df: pl.DataFrame, ctx: RunContext) -> pl.DataFrame:
    return field_checks.run(df, ctx).df


class TestVatCrossCheck:
    def test_vat_is_never_corrected_automatically(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """La regle absolue du projet. Une TVA fausse a un cout fiscal reel ;
        aucune confiance ne justifie de la corriger sans un humain."""
        result = cross_checks.run(_prepared(ingested, ctx), ctx)
        assert not [c for c in result.corrections if c.field_name == "tva"]

    def test_alcohol_at_a_reduced_rate_is_flagged(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = cross_checks.run(_prepared(ingested, ctx), ctx)
        mismatches = [a for a in result.anomalies if a.code == AnomalyCode.VAT_CATEGORY_MISMATCH]
        assert mismatches
        assert any("WHISKY" in a.detail.upper() for a in mismatches)

    def test_tomatoes_are_not_mistaken_for_spirits(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """NON-REGRESSION. « FRORIGINE » contient « GIN » : un matching par
        sous-chaine classait 89 lignes de tomates en spiritueux et exigeait
        20 % de TVA sur des fruits et legumes."""
        result = cross_checks.run(_prepared(ingested, ctx), ctx)
        for anomaly in result.anomalies:
            if anomaly.code == AnomalyCode.VAT_CATEGORY_MISMATCH:
                detail = anomaly.detail.upper()
                if "TOM" in detail or "POMME" in detail:
                    assert "20.0%" not in detail, f"tomate/pomme attendue a 20% : {detail}"


class TestTaxonomyGapFilling:
    def test_gap_is_deduced_from_the_leaf(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        """250 lignes du fichier ont n2 vide alors que n3 et n4 sont remplis.
        La feuille determine le chemin sans ambiguite : pas besoin du LLM."""
        prepared = _prepared(ingested, ctx)
        holes = prepared.filter(
            (pl.col("taxonomie_niveau_2") == "") & (pl.col("taxonomie_niveau_4") != "")
        )
        assert holes.height > 0

        result = cross_checks.run(prepared, ctx)
        after = result.df.filter(pl.col("id_produit").is_in(holes["id_produit"].to_list()))
        assert (after["taxonomie_niveau_2"] != "").all()

        filled = [a for a in result.anomalies if a.code == AnomalyCode.TAXONOMY_GAP_FILLED]
        assert filled

    def test_deduction_is_traced_like_any_correction(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = cross_checks.run(_prepared(ingested, ctx), ctx)
        traced = [
            c for c in result.corrections if c.rule == "cross_checks.taxonomy.deduce_from_leaf"
        ]
        assert traced
        assert all(c.old_value == "" and c.new_value for c in traced)


class TestTaxonomyNameMismatch:
    def test_whisky_filed_under_detergent_is_flagged(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Libelle reel du fichier, range dans une categorie incompatible.
        Une regle de mots-cles suffit : inutile de solliciter un LLM."""
        prepared = _prepared(ingested, ctx)
        forged = prepared.head(1).with_columns(
            pl.Series("nom", ["WHISKY ECOSSAIS 70 CL"]),
            pl.Series("taxonomie_niveau_1", ["NON ALIMENTAIRE"]),
            pl.Series("taxonomie_niveau_2", ["ENTRETIEN"]),
            pl.Series("taxonomie_niveau_3", ["LESSIVE"]),
            pl.Series("taxonomie_niveau_4", ["LIQUIDE"]),
        )
        result = cross_checks.run(forged, ctx)
        codes = {a.code for a in result.anomalies}
        assert AnomalyCode.TAXONOMY_NAME_MISMATCH in codes

    def test_a_false_taxonomy_does_not_also_produce_a_false_vat_alert(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Une taxonomie fausse ne doit pas servir de reference pour la TVA :
        on compterait une seule erreur deux fois, et l'humain recevrait une
        question qui n'a pas lieu d'etre. Mesure avant correction : 615
        incoherences de TVA remontees, contre 394 reelles."""
        prepared = _prepared(ingested, ctx)
        forged = prepared.head(1).with_columns(
            # Des pommes a 5,5 % (taux correct), mais rangees en spiritueux.
            pl.Series("nom", ["POM B 1K C1"]),
            pl.Series("vat_rate", [5.5]),
            pl.Series("taxonomie_niveau_1", ["ALIMENTAIRE"]),
            pl.Series("taxonomie_niveau_2", ["BOISSONS"]),
            pl.Series("taxonomie_niveau_3", ["ALCOOLS"]),
            pl.Series("taxonomie_niveau_4", ["SPIRITUEUX"]),
        )
        result = cross_checks.run(forged, ctx)
        codes = [a.code for a in result.anomalies]
        assert AnomalyCode.TAXONOMY_NAME_MISMATCH in codes
        assert AnomalyCode.VAT_CATEGORY_MISMATCH not in codes
