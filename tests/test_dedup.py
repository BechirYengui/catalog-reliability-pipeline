"""Tests de la deduplication et des regles de survivorship.

Fusionner sans regles ecrites, c'est perdre de l'information sans s'en
apercevoir. Chaque regle du fichier de configuration a son test ici.
"""

from __future__ import annotations

import polars as pl

from pipeline.context import RunContext
from pipeline.normalize import gtin_checksum_valid
from pipeline.steps import cross_checks, dedup, field_checks


def _records(df: pl.DataFrame, ctx: RunContext) -> list[dedup.GoldenRecord]:
    prepared = cross_checks.run(field_checks.run(df, ctx).df, ctx).df
    return dedup.build_golden_records(prepared)


class TestGrouping:
    def test_rows_collapse_into_fewer_records(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        records = _records(ingested, ctx)
        assert 0 < len(records) < ingested.height
        assert sum(r.source_rows for r in records) == ingested.height

    def test_no_record_is_keyed_on_ean(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        """Le profilage a montre 8 700 EAN tous distincts pour 343 libelles :
        l'EAN identifie la LIGNE, pas le produit. Grouper dessus ne fusionnerait
        jamais rien."""
        records = _records(ingested, ctx)
        merged = [r for r in records if r.source_rows > 1]
        assert merged, "aucun groupe : la cle de regroupement est probablement l'EAN"


class TestSurvivorship:
    def test_surviving_ean_passes_the_checksum(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        for record in _records(ingested, ctx):
            if record.ean:
                assert gtin_checksum_valid(record.ean)

    def test_surviving_label_is_the_most_complete(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        for record in _records(ingested, ctx):
            assert record.label
            assert len(record.label) >= len(record.label_normalized) - 2

    def test_a_group_with_several_vat_rates_is_marked_in_conflict(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """C'est la decision groupee que verra l'humain : « ces N lignes portent
        un taux different des autres »."""
        for record in _records(ingested, ctx):
            if len(record.vat_distribution) > 1:
                assert record.vat_conflict

    def test_a_record_in_vat_conflict_is_never_publishable(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        for record in _records(ingested, ctx):
            if record.vat_conflict:
                assert not record.publishable


class TestMarque:
    """La marque lue par le LLM doit ARRIVER jusqu'a la fiche.

    Elle etait extraite puis perdue a la construction du golden record. Une
    barriere qui ne recoit jamais de marque ne protege rien, et rien ne l'aurait
    signale : ce test est la pour ca.
    """

    def test_la_marque_du_llm_arrive_dans_la_fiche(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        prepared = cross_checks.run(field_checks.run(ingested, ctx).df, ctx).df
        # Ce que produit l'etage LLM : une colonne `brand` par ligne.
        marque = prepared.with_columns(
            pl.lit("Doliprane").alias("brand"),
        )

        records = dedup.build_golden_records(marque)

        assert records, "aucune fiche produite"
        assert all(r.brand == "Doliprane" for r in records)

    def test_sans_etage_llm_la_marque_reste_vide(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Pas de colonne `brand` du tout : le pipeline ne doit pas trebucher."""
        for record in _records(ingested, ctx):
            assert record.brand == ""


class TestDeterminism:
    def test_same_input_gives_same_records(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        first = _records(ingested, ctx)
        second = _records(ingested, ctx)
        assert [r.key for r in first] == [r.key for r in second]
        assert [r.ean for r in first] == [r.ean for r in second]


class TestDeuxMesuresDePubliable:
    """Le taux sur les LIGNES et le compte de FICHES ne disent pas la meme chose.

    Sur le catalogue de TVA incoherente : 400 lignes, 10 produits, et dans
    chaque produit 8 lignes sur 40 portent un taux faux. Cote lignes, 81,5 %
    restent coherentes ; cote fiches, le conflit remonte au regroupement et
    AUCUNE n'est publiable. L'interface affichait le premier chiffre sous le
    mot « produits » — donc l'inverse de ce que confirme l'export.
    """

    def test_un_conflit_de_tva_nait_au_regroupement(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        records = _records(ingested, ctx)
        en_conflit = [r for r in records if r.vat_conflict]
        assert en_conflit, "l'echantillon ne porte aucun conflit de TVA"
        # Une fiche en conflit n'est jamais publiable, quel que soit le taux
        # de lignes coherentes qui la composent.
        assert all(not r.publishable for r in en_conflit)
