"""Tests de l'ingestion, sur les lignes reelles de tests/data/sample_anomalies.csv."""

from __future__ import annotations

from typing import ClassVar

import polars as pl
import pytest

from pipeline.config import PipelineConfig
from pipeline.context import RunContext
from pipeline.models import AnomalyCode
from pipeline.steps import ingest
from pipeline.steps.ingest import SchemaError, detach_suffix

from .conftest import SAMPLE


class TestReadCsv:
    def test_bom_does_not_pollute_the_first_column_name(self) -> None:
        """Sans utf-8-sig, la 1re colonne s'appellerait '\\ufeffid_produit' et la
        validation de schema echouerait — le fichier reel porte bien un BOM."""
        assert SAMPLE.read_bytes().startswith(b"\xef\xbb\xbf")
        df = ingest.read_csv(SAMPLE)
        assert df.columns[0] == "id_produit"

    def test_all_columns_are_read_as_strings(self) -> None:
        df = ingest.read_csv(SAMPLE)
        # Lire la TVA en float des la lecture ferait disparaitre les valeurs
        # illisibles, precisement ce qu'on veut detecter.
        assert df.schema["tva"] == pl.String
        assert df.schema["ean"] == pl.String


class TestValidateSchema:
    def test_rejects_a_file_missing_a_column(self) -> None:
        df = pl.DataFrame({"id_produit": ["a"], "ean": ["1"]})
        with pytest.raises(SchemaError, match="colonnes manquantes"):
            ingest.validate_schema(df)


class TestDetachSuffix:
    SUFFIXES: ClassVar[list[tuple[str, str, str]]] = [
        ("ORIGINE ITALIA", "origin", "ITALIA"),
        ("FAMILY SIZE", "marketing_mention", "FAMILY SIZE"),
    ]

    @pytest.mark.parametrize(
        ("label", "expected_label", "expected_extract"),
        [
            ("LESSIVE 2LORIGINE ITALIA", "LESSIVE 2L", {"origin": "ITALIA"}),
            (
                "COCA COLA ZERO 1LFAMILY SIZE",
                "COCA COLA ZERO 1L",
                {"marketing_mention": "FAMILY SIZE"},
            ),
            ("TOMATES GRAPPEFAMILY SIZE", "TOMATES GRAPPE", {"marketing_mention": "FAMILY SIZE"}),
            ("ARABICA MOULU 0.25KGORIGINE ITALIA", "ARABICA MOULU 0.25KG", {"origin": "ITALIA"}),
            ("WHISKY ECOSSAIS 70 CL", "WHISKY ECOSSAIS 70 CL", {}),
        ],
    )
    def test_detaches_known_suffixes_only(
        self, label: str, expected_label: str, expected_extract: dict[str, str]
    ) -> None:
        cleaned, extracted = detach_suffix(label, self.SUFFIXES)
        assert cleaned == expected_label
        assert extracted == expected_extract

    def test_suffix_is_kept_as_data_not_discarded(self) -> None:
        _, extracted = detach_suffix("LESSIVE 2LORIGINE ITALIA", self.SUFFIXES)
        assert extracted["origin"] == "ITALIA"


class TestRunIngest:
    def test_mojibake_is_fixed_in_the_label(self, raw_df: pl.DataFrame, ctx: RunContext) -> None:
        assert any("CAFÃ‰" in n for n in raw_df["nom"].to_list())
        result = ingest.run(raw_df, ctx)
        labels = result.df["nom"].to_list()
        assert any("CAFÉ" in n for n in labels)
        assert not any("Ã" in n for n in labels)

    def test_mojibake_is_fixed_in_the_taxonomy_too(
        self, raw_df: pl.DataFrame, ctx: RunContext
    ) -> None:
        """74 lignes du fichier reel portent « Ã‰PICERIE SUCREE ». Ne reparer que
        le libelle laisserait une categorie fantome dans le referentiel."""
        assert any("Ã‰" in v for v in raw_df["taxonomie_niveau_2"].to_list())
        result = ingest.run(raw_df, ctx)
        level2 = result.df["taxonomie_niveau_2"].to_list()
        assert "ÉPICERIE SUCREE" in level2
        assert not any("Ã" in v for v in level2)

    def test_every_change_produces_a_correction(
        self, raw_df: pl.DataFrame, ctx: RunContext
    ) -> None:
        """L'audit trail est non negociable : aucune valeur ne change sans trace."""
        result = ingest.run(raw_df, ctx)
        before = dict(zip(raw_df["id_produit"], raw_df["nom"], strict=True))
        after = dict(zip(result.df["id_produit"], result.df["nom"], strict=True))
        changed = {rid for rid, value in after.items() if before[rid] != value}
        traced = {c.row_id for c in result.corrections if c.field_name == "nom"}
        assert changed <= traced

    def test_corrections_carry_the_full_audit_payload(
        self, raw_df: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = ingest.run(raw_df, ctx)
        correction = next(c for c in result.corrections if c.field_name == "nom")
        assert correction.author == "RULE"
        assert correction.rule.startswith("ingest.")
        assert correction.old_value != correction.new_value
        assert correction.run_id == ctx.run_id
        assert correction.created_at == ctx.now()  # horloge injectee => deterministe

    def test_marketing_suffix_anomalies_are_reported(
        self, raw_df: pl.DataFrame, ctx: RunContext
    ) -> None:
        result = ingest.run(raw_df, ctx)
        codes = [a.code for a in result.anomalies]
        assert AnomalyCode.MARKETING_SUFFIX_DETACHED in codes
        assert AnomalyCode.MOJIBAKE_FIXED in codes

    def test_row_count_is_preserved(self, raw_df: pl.DataFrame, ctx: RunContext) -> None:
        """L'ingestion annote, elle ne filtre jamais : une ligne rejetee doit
        rester visible dans le rapport."""
        result = ingest.run(raw_df, ctx)
        assert result.df.height == raw_df.height
        assert result.metrics is not None
        assert result.metrics.rows_in == result.metrics.rows_out

    def test_is_deterministic(self, raw_df: pl.DataFrame, ctx: RunContext) -> None:
        first = ingest.run(raw_df, ctx)
        second = ingest.run(raw_df, ctx)
        assert first.df.equals(second.df)
        assert len(first.corrections) == len(second.corrections)


class TestConfigLoading:
    def test_taxonomy_reference_has_no_mojibake(self, config: PipelineConfig) -> None:
        """Le referentiel versionne ne doit pas contenir les chemins fantomes."""
        for path in config.taxonomy.paths:
            for level in path:
                assert "Ã" not in level, f"mojibake dans config/taxonomy.yaml : {level}"

    def test_every_leaf_maps_to_exactly_one_path(self, config: PipelineConfig) -> None:
        """Condition de validite du comblement de trous : mesure sur le fichier
        reel, aucune feuille n'est rattachee a deux chemins."""
        assert len(config.taxonomy.by_leaf) == len(config.taxonomy.paths)
