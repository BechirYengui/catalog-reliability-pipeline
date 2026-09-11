"""L'aiguillage decide ce qui part vers la marketplace. Il doit dire la verite.

La regle non negociable du projet est que la TVA n'est jamais corrigee
automatiquement. Elle etait tenue : aucune `Correction` du moteur ne porte sur
`tva`. Mais « on ne corrige pas » n'a de sens que si l'humain, lui, est appele.
Or une fiche portant un taux illegal ou absent ressortait `VALIDATED` avec le
motif « aucune anomalie », et donc publiable, tant qu'aucun conflit n'avait ete
signale par ailleurs.

Le declencheur exact : `vat_coherent` vaut True par defaut quand la feuille de
taxonomie n'a pas de regle dans `vat_rules.yaml`. Sans regle de categorie,
aucun conflit ; sans conflit, aucun aiguillage ; et un taux a 33 % — qui
n'existe pas en France — partait avec la mention « aucune anomalie ».
L'anomalie `vat_illegal_rate`, de severite ERROR, etait pourtant bien produite
par les controles par champ : elle etait produite, puis ignoree.

Ces tests verrouillent les deux bouts : le taux illegal ou absent envoie en
revue, et il ne peut pas etre declare publiable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pipeline.context import RunContext
from pipeline.models import ProductStatus
from pipeline.steps.scoring_routing import route


def fiche(**overrides: Any) -> Any:
    """Une fiche IRREPROCHABLE par ailleurs.

    Tout est en regle : EAN valide, image verifiee et repondante, taxonomie
    complete, aucun conflit. Seul le champ passe en parametre varie, donc le
    statut obtenu ne peut venir que de lui.
    """
    base: dict[str, Any] = {
        "label": "BROSSE A DENTS SOUPLE",
        "vat_rate": 20.0,
        "vat_conflict": False,
        "taxonomy_conflict": False,
        "taxonomy": ("MAISON", "HYGIENE", "DENTAIRE", "BROSSES"),
        "ean": "3017620422003",
        "url_ok": True,
        "url_checked": True,
        "publishable": True,
        "label_enriched": "",
        "llm_confidence": 1.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestTvaIllegaleOuAbsente:
    """Ce que le pipeline ne corrige pas, il doit le faire trancher."""

    @pytest.mark.parametrize(
        ("taux", "cas"),
        [
            (33.0, "taux inexistant en France"),
            (7.0, "ancien taux, supprime depuis"),
            (None, "taux absent"),
        ],
    )
    def test_un_taux_hors_bareme_part_en_revue(
        self, ctx: RunContext, taux: float | None, cas: str
    ) -> None:
        statut, _, motifs = route(fiche(vat_rate=taux), ctx)

        assert statut is ProductStatus.NEEDS_REVIEW, (
            f"{cas} ({taux}) ressort {statut.value} : une fiche que le pipeline "
            "refuse de corriger doit atteindre un humain, sinon elle part telle quelle"
        )
        assert any("TVA" in m or "tva" in m for m in motifs), (
            f"le motif affiche ne parle pas de la TVA : {motifs}"
        )

    def test_le_motif_nomme_le_taux_en_cause(self, ctx: RunContext) -> None:
        """Un statut sans motif exploitable est une decision qu'on ne peut pas
        contester. L'humain doit voir la valeur qui pose probleme."""
        _, _, motifs = route(fiche(vat_rate=33.0), ctx)

        assert any("33" in m for m in motifs), f"le taux fautif n'est pas cite : {motifs}"

    def test_les_taux_legaux_ne_declenchent_rien(self, ctx: RunContext) -> None:
        """Le garde-fou ne doit pas se declencher sur ce qui est normal.

        Un pipeline qui envoie tout en revue ne vaut pas mieux qu'un pipeline
        qui ne verifie rien : il deplace le travail au lieu de le supprimer.
        """
        for taux in ctx.config.vat.legal_rates_fr:
            statut, _, motifs = route(fiche(vat_rate=taux), ctx)
            assert statut is ProductStatus.VALIDATED, (
                f"le taux legal {taux} ressort {statut.value} ({motifs}) : "
                "le garde-fou mord sur du sain"
            )


class TestLaTvaPrimeSurLeReste:
    """L'ordre des controles porte une intention : ce qui a une consequence
    fiscale se signale AVANT ce qui a une consequence commerciale."""

    def test_un_taux_illegal_l_emporte_sur_une_fiche_incomplete(self, ctx: RunContext) -> None:
        _, _, motifs = route(fiche(vat_rate=33.0, publishable=False, ean=None), ctx)

        assert any("33" in m for m in motifs), (
            f"la fiche est signalee incomplete et le taux illegal est tu : {motifs}"
        )


class TestNonRegression:
    """Ce qui marchait doit continuer de marcher."""

    def test_un_libelle_vide_reste_rejete(self, ctx: RunContext) -> None:
        statut, _, _ = route(fiche(label="   "), ctx)
        assert statut is ProductStatus.REJECTED

    def test_un_conflit_de_tva_reste_en_revue(self, ctx: RunContext) -> None:
        statut, confiance, _ = route(fiche(vat_conflict=True), ctx)
        assert statut is ProductStatus.NEEDS_REVIEW
        assert confiance == 1.0

    def test_une_fiche_incomplete_reste_en_revue(self, ctx: RunContext) -> None:
        statut, _, motifs = route(fiche(publishable=False, ean=None), ctx)
        assert statut is ProductStatus.NEEDS_REVIEW
        assert any("EAN" in m for m in motifs)

    def test_une_reformulation_sous_le_seuil_reste_en_revue(self, ctx: RunContext) -> None:
        statut, _, _ = route(
            fiche(label_enriched="Brosse a dents souple", llm_confidence=0.10), ctx
        )
        assert statut is ProductStatus.NEEDS_REVIEW

    def test_une_reformulation_sure_reste_auto_corrigee(self, ctx: RunContext) -> None:
        statut, _, _ = route(
            fiche(label_enriched="Brosse a dents souple", llm_confidence=0.99), ctx
        )
        assert statut is ProductStatus.AUTO_CORRECTED
