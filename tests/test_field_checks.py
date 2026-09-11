"""Tests des controles champ par champ, sur les lignes reelles du CSV."""

from __future__ import annotations

import polars as pl

from pipeline.context import RunContext
from pipeline.models import AnomalyCode, Severity
from pipeline.normalize import gtin_checksum_valid
from pipeline.steps import field_checks


class TestEanHandling:
    def test_every_kept_ean_passes_the_checksum(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Garantie centrale : le pipeline ne produit jamais un EAN invalide."""
        result = field_checks.run(ingested, ctx)
        for ean in result.df["ean"].to_list():
            if ean:
                assert gtin_checksum_valid(ean), f"EAN invalide conserve : {ean}"

    def test_short_codes_are_routed_to_internal_code(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Un code PLU n'est ni repare ni jete : il change de colonne."""
        result = field_checks.run(ingested, ctx)
        routed = result.df.filter(pl.col("internal_code") != "")
        assert routed.height > 0
        for row in routed.iter_rows(named=True):
            assert row["ean"] == ""
            assert row["ean_valid"] is False

    def test_repairs_are_traced_in_the_audit_trail(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = field_checks.run(ingested, ctx)
        repairs = [c for c in result.corrections if c.field_name == "ean"]
        assert repairs
        for correction in repairs:
            assert correction.rule.startswith("field_checks.ean.")
            assert correction.new_value is not None
            assert gtin_checksum_valid(correction.new_value)

    def test_missing_ean_is_reported_not_invented(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = field_checks.run(ingested, ctx)
        assert AnomalyCode.EAN_MISSING in {a.code for a in result.anomalies}
        # Aucune correction ne doit fabriquer un EAN a partir de rien.
        for correction in result.corrections:
            if correction.field_name == "ean":
                assert correction.old_value


class TestUrlHandling:
    def test_scheme_typos_are_repaired_and_traced(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = field_checks.run(ingested, ctx)
        repaired = [c for c in result.corrections if c.field_name == "url_image"]
        assert repaired
        assert all((c.new_value or "").startswith(("http://", "https://")) for c in repaired)

    def test_unsupported_schemes_are_flagged_as_errors(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = field_checks.run(ingested, ctx)
        malformed = [a for a in result.anomalies if a.code == AnomalyCode.URL_MALFORMED]
        assert malformed
        assert all(a.severity is Severity.ERROR for a in malformed)

    def test_no_network_call_when_disabled(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        """ctx.network_enabled=False : le CI ne doit dependre d'aucun tiers."""
        assert ctx.network_enabled is False
        result = field_checks.run(ingested, ctx)
        assert result.metrics is not None
        assert result.metrics.counters["url_http_requests"] == 0
        assert result.metrics.counters["url_dns_lookups"] == 0

    def test_unverified_url_is_not_counted_as_valid(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """« Publiable » est une AFFIRMATION : elle exige une preuve positive.

        Sans verification reseau on ne sait pas, et ne pas savoir n'est pas la
        meme chose que savoir que l'image fonctionne. Compter l'inconnu comme
        valide gonflait l'indicateur qui compte le plus pour le metier.
        """
        result = field_checks.run(ingested, ctx)
        rows = result.df.filter(pl.col("url_image") != "")
        unchecked = rows.filter(~pl.col("url_checked"))
        assert unchecked.height > 0, "aucune URL non verifiee : le test ne prouve rien"
        assert not unchecked["url_ok"].any()


class TestVatHandling:
    def test_vat_is_never_modified(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        """Regle absolue du projet, verrouillee par un test : aucune correction
        automatique ne porte sur la TVA, quelle que soit la confiance."""
        result = field_checks.run(ingested, ctx)
        assert not [c for c in result.corrections if c.field_name == "tva"]

    def test_rates_are_parsed_into_a_numeric_column(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = field_checks.run(ingested, ctx)
        rates = [r for r in result.df["vat_rate"].to_list() if r is not None]
        assert rates
        assert set(rates) <= {0.0, 2.1, 5.5, 10.0, 20.0}


class TestTaxonomyCanonicalization:
    """Reparer le mojibake ne suffit pas : ftfy restaure « ÉPICERIE SUCREE »
    avec son accent, alors que le referentiel et 9 000 lignes sur 10 000
    ecrivent « EPICERIE SUCREE ». Sans canonicalisation, la reparation
    d'encodage CREE une categorie fantome au lieu d'en supprimer une.
    Mesure sur le fichier reel : 104 lignes concernees."""

    def test_accented_variant_is_folded_onto_the_reference_spelling(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        assert "ÉPICERIE SUCREE" in ingested["taxonomie_niveau_2"].to_list()
        result = field_checks.run(ingested, ctx)
        level2 = set(result.df["taxonomie_niveau_2"].to_list())
        assert "EPICERIE SUCREE" in level2
        assert "ÉPICERIE SUCREE" not in level2

    def test_canonicalization_is_traced(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        result = field_checks.run(ingested, ctx)
        traced = [c for c in result.corrections if c.rule == "field_checks.taxonomy.canonicalize"]
        assert traced
        assert all(c.old_value != c.new_value for c in traced)

    def test_no_unknown_path_remains_on_complete_rows(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Consequence attendue : apres canonicalisation, plus aucune ligne
        complete ne pointe vers un chemin absent du referentiel."""
        result = field_checks.run(ingested, ctx)
        unknown = [a for a in result.anomalies if a.code == AnomalyCode.TAXONOMY_UNKNOWN_PATH]
        assert unknown == []


class TestTaxonomyHandling:
    def test_gaps_and_truncations_are_distinguished(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Un trou (n2 vide, n3/n4 remplis) se repare sans LLM ; une troncature
        demande un enrichissement. Les confondre couterait des appels inutiles."""
        result = field_checks.run(ingested, ctx)
        codes = {a.code for a in result.anomalies}
        assert AnomalyCode.TAXONOMY_GAP in codes
        assert AnomalyCode.TAXONOMY_INCOMPLETE in codes

    def test_complete_known_paths_raise_nothing(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = field_checks.run(ingested, ctx)
        flagged = {
            a.row_id
            for a in result.anomalies
            if a.code
            in (
                AnomalyCode.TAXONOMY_GAP,
                AnomalyCode.TAXONOMY_INCOMPLETE,
                AnomalyCode.TAXONOMY_UNKNOWN_PATH,
            )
        }
        complete = result.df.filter(pl.col("taxonomy_complete"))
        known = set(ctx.config.taxonomy.known_paths)
        for row in complete.iter_rows(named=True):
            path = (
                row["taxonomie_niveau_1"],
                row["taxonomie_niveau_2"],
                row["taxonomie_niveau_3"],
                row["taxonomie_niveau_4"],
            )
            if path in known:
                assert row["id_produit"] not in flagged


class TestDeterminism:
    def test_two_identical_runs_produce_identical_output(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        first = field_checks.run(ingested, ctx)
        second = field_checks.run(ingested, ctx)
        assert first.df.equals(second.df)
        assert len(first.corrections) == len(second.corrections)
        assert len(first.anomalies) == len(second.anomalies)


class TestUneAnomalieSeRetrouveDansLeFichier:
    """Cent anomalies d'un meme code ne doivent pas etre cent lignes identiques.

    L'interface affichait « prefixe GS1 reserve a l'usage interne » cent fois,
    sans le code fautif ni le nom du produit : on savait qu'il y en avait
    1 438, jamais lesquelles. Une anomalie doit porter de quoi retrouver sa
    ligne dans le CSV du magasin.
    """

    def test_chaque_anomalie_porte_sa_valeur_et_son_produit(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        # Les codes « absent » n'ont, par nature, aucune valeur a montrer :
        # c'est le vide qui est l'anomalie. Ils doivent en revanche dire de
        # quel produit il s'agit, comme les autres.
        absences = {"ean_missing", "url_missing", "vat_missing", "label_empty"}

        result = field_checks.run(ingested, ctx)
        anomalies = result.anomalies
        assert anomalies, "aucune anomalie produite par l'echantillon"

        sans_valeur = [a for a in anomalies if not a.value and a.code.value not in absences]
        sans_produit = [a for a in anomalies if not a.label]
        assert not sans_valeur, f"{len(sans_valeur)} anomalies sans valeur en cause"
        assert not sans_produit, f"{len(sans_produit)} anomalies sans libelle de produit"

    def test_la_valeur_est_bien_celle_de_la_ligne(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Pas n'importe quelle valeur : celle que porte cette ligne-la."""
        result = field_checks.run(ingested, ctx)
        eans = dict(zip(ingested["id_produit"].to_list(), ingested["ean"].to_list(), strict=True))

        for anomaly in result.anomalies:
            if anomaly.field_name == "ean":
                assert anomaly.value == str(eans[anomaly.row_id] or "")
