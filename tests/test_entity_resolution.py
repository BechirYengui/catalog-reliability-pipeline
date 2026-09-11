"""L'etape des quasi-doublons, et le defaut qui l'a fait deraper.

Sur le catalogue propre (200 lignes, 10 produits), la plateforme a rendu 12
fiches au lieu de 10 : deux familles coupees en deux, 17+3 et 11+9. Le meme
fichier traite sans LLM en rendait 10. L'enrichissement n'ajoutait donc pas du
bruit, il changeait l'espace de comparaison : le LLM n'avait applique que 154
libelles sur 200, et une famille a moitie enrichie se faisait comparer
« Tomates grappe France 1 kg » contre « TOM GRA FR KG ».

Ces tests figent les deux exigences, qui tirent en sens inverse : une famille
partiellement enrichie reste UNE famille, et deux produits differents ne
fusionnent jamais — surtout pas a cause du correctif.
"""

from __future__ import annotations

from pipeline.steps.dedup import GoldenRecord
from pipeline.steps.entity_resolution import MERGE_THRESHOLD, pair_similarity, resolve

TOMATES = ("ALIMENTAIRE", "FRUITS ET LEGUMES", "LEGUMES", "TOMATES")
PARAPHARMACIE = ("PARAPHARMACIE", "MEDICATION FAMILIALE", "DOULEUR", "PARACETAMOL")


def record(
    label: str,
    *,
    enriched: str = "",
    taxonomy: tuple[str, ...] = TOMATES,
    brand: str = "",
) -> GoldenRecord:
    """Une fiche minimale : seuls comptent ici le libelle et la classe."""
    return GoldenRecord(
        key=label,
        label=label,
        label_normalized=label,
        ean=None,
        internal_code=None,
        url_image=None,
        url_ok=True,
        url_checked=True,
        taxonomy=(taxonomy[0], taxonomy[1], taxonomy[2], taxonomy[3]),
        vat_rate=5.5,
        vat_conflict=False,
        vat_distribution={},
        taxonomy_conflict=False,
        quantity_value=1.0,
        quantity_unit="KG",
        origin=None,
        publishable=True,
        source_rows=20,
        label_enriched=enriched,
        brand=brand,
    )


class TestFamillePartiellementEnrichie:
    """Le defaut mesure : 10 produits rendus en 12 fiches."""

    def test_une_famille_a_moitie_enrichie_reste_une_famille(self) -> None:
        """Deux ecritures d'un meme produit, une seule enrichie : un groupe.

        C'est exactement ce qui a scinde TOM GRA FR KG en 17+3 : les fiches
        enrichies formaient un groupe, les autres un second.
        """
        records = [
            record("TOM GRA FR KG REF001", enriched="Tomates grappe France 1 kg"),
            record("TOM GRA FR KG REF011", enriched="Tomates grappe France 1 kg"),
            record("TOM GRA FR KG REF051"),
            record("TOM GRA FR KG REF091"),
        ]

        groups, funnel, _ = resolve(records)

        assert len(groups) == 1, "la famille a ete scindee"
        assert funnel["records_out"] == 1

    def test_le_libelle_brut_sert_de_plancher(self) -> None:
        """Une fiche enrichie et une fiche brute se comparent sur le brut.

        Le brut existe toujours ; c'est ce qui rend la comparaison possible
        quel que soit l'etat de l'enrichissement.
        """
        enrichie = record("TOM GRA FR KG REF001", enriched="Tomates grappe France 1 kg")
        brute = record("TOM GRA FR KG REF051")

        assert pair_similarity(enrichie, brute) >= MERGE_THRESHOLD

    def test_l_enrichi_reste_le_second_signal_quand_les_deux_l_ont(self) -> None:
        """Deux abreviations de caisse du meme produit, reconciliees par le LLM.

        C'est la raison d'etre de l'etape : « COC COL ZER 1L » et « COCA COLA
        ZERO 1L » se ressemblent peu caractere par caractere, leurs formes
        enrichies sont identiques. Le correctif ne doit pas perdre ce signal.
        """
        gauche = record("COC COL ZER 1L", enriched="Coca-Cola Zero 1 L")
        droite = record("COCA COLA ZERO 1L", enriched="Coca-Cola Zero 1 L")

        assert pair_similarity(gauche, droite) >= MERGE_THRESHOLD
        assert len(resolve([gauche, droite])[0]) == 1


class TestFusionsHumaines:
    """Une decision prise dans l'interface ne doit pas etre a reprendre demain.

    Le depot du lendemain recalcule tout : sans rejeu, la fusion decidee hier
    disparait et l'utilisateur refait le meme geste chaque matin.
    """

    def test_une_fusion_decidee_par_un_humain_est_rejouee(self) -> None:
        """Deux libelles trop eloignes pour le fuzzy, reunis par la decision."""
        gauche = record("TOM GRA FR KG")
        droite = record("TOMATES GRAPPE FRANCE AU KILO")

        assert pair_similarity(gauche, droite) < MERGE_THRESHOLD, (
            "le texte seul les rapproche deja : le test ne prouve rien"
        )
        assert len(resolve([gauche, droite])[0]) == 2

        groups, funnel, _ = resolve(
            [gauche, droite],
            forced={"TOMATES GRAPPE FRANCE AU KILO": "TOM GRA FR KG"},
        )

        assert len(groups) == 1
        assert funnel["pairs_merged_by_human"] == 1

    def test_la_decision_humaine_ignore_le_blocking(self) -> None:
        """Le blocking separe par contenance ; l'humain a vu les deux fiches.

        Une contenance mal lue est justement le genre d'erreur qu'une fusion
        manuelle repare : la barriere protege du fuzzy, pas de l'utilisateur.
        """
        connue = record("HUILE OLIVE 1L")
        connue.quantity_value, connue.quantity_unit = 1.0, "L"
        muette = record("HUILE OLIVE VIERGE EXTRA")
        muette.quantity_value, muette.quantity_unit = None, None

        groups, _, _ = resolve(
            [connue, muette], forced={"HUILE OLIVE VIERGE EXTRA": "HUILE OLIVE 1L"}
        )

        assert len(groups) == 1

    def test_sans_regle_apprise_rien_ne_change(self) -> None:
        records = [record("TOM GRA FR KG"), record("TOMATES GRAPPE FRANCE AU KILO")]

        assert resolve(records, forced={})[1]["pairs_merged_by_human"] == 0
        assert resolve(records, forced=None)[1]["pairs_merged_by_human"] == 0


class TestFusionsInterdites:
    """Le correctif elargit les fusions : ces limites ne bougent pas."""

    def test_deux_produits_differents_ne_fusionnent_pas(self) -> None:
        records = [
            record("TOM GRA FR KG REF001", enriched="Tomates grappe France 1 kg"),
            record("POM B 1K C1 REF002", enriched="Pommes Golden 1 kg categorie 1"),
        ]

        groups, _, _ = resolve(records)

        assert len(groups) == 2

    def test_une_marque_n_est_pas_une_molecule(self) -> None:
        """Doliprane et un paracetamol generique sont deux references distinctes.

        Meme molecule, meme dosage, deux produits : ils n'ont ni le meme prix
        ni le meme code, et les fusionner ferait disparaitre une reference du
        catalogue du magasin. Le dosage commun ne suffit jamais.

        Nuance a ne pas confondre avec ce cas : dans le fichier reel, les 237
        libelles contenant PARACETAMOL portent TOUS la mention DOLI —
        « PARACETAMOL 1000 DOLI » est l'ecriture caisse du Doliprane, pas un
        generique. Aucun generique n'y figure.
        """
        doliprane = record("DOLIPRANE 1000 MG", taxonomy=PARAPHARMACIE)
        generique = record("PARACETAMOL 1000 MG BIOGARAN", taxonomy=PARAPHARMACIE)

        assert pair_similarity(doliprane, generique) < MERGE_THRESHOLD
        assert len(resolve([doliprane, generique])[0]) == 2

    def test_deux_marques_differentes_ne_fusionnent_jamais(self) -> None:
        """Le cas dangereux : le LLM efface la marque en reecrivant le libelle.

        « Paracetamol 1000 mg » contre « Paracetamol 1000 mg Biogaran » score
        1,000 — token_set_ratio ignore par construction le mot en trop, et
        c'est justement ce mot qui distingue les deux references. La marque
        tranche avant le texte.
        """
        doliprane = record(
            "DOLIPRANE 1000 MG",
            enriched="Paracetamol 1000 mg",
            taxonomy=PARAPHARMACIE,
            brand="Doliprane",
        )
        generique = record(
            "PARACETAMOL 1000 MG BIOGARAN",
            enriched="Paracetamol 1000 mg Biogaran",
            taxonomy=PARAPHARMACIE,
            brand="Biogaran",
        )

        assert pair_similarity(doliprane, generique) >= MERGE_THRESHOLD, (
            "sans la marque, le texte seul les aurait fusionnes"
        )

        groups, funnel, _ = resolve([doliprane, generique])

        assert len(groups) == 2
        assert funnel["pairs_blocked_by_brand"] == 1

    def test_une_marque_inconnue_ne_separe_rien(self) -> None:
        """Le LLM ne trouve pas toujours la marque, et il ne tourne pas toujours.

        Traiter « inconnue » comme « differente » scinderait les familles
        exactement comme le faisait la comparaison enrichi contre brut. Une
        marque vide s'efface donc devant la ressemblance des libelles.
        """
        connue = record("NUTELLA 750 G", brand="Nutella")
        muette = record("NUTELLA 750 GR")

        groups, funnel, _ = resolve([connue, muette])

        assert len(groups) == 1
        assert funnel["pairs_blocked_by_brand"] == 0

    def test_la_meme_marque_ecrite_autrement_ne_bloque_pas(self) -> None:
        """« NUTELLA » et « Nutella » sont la meme marque, pas deux references."""
        haut = record("NUTELLA 750 G", brand="NUTELLA")
        bas = record("NUTELLA POT 750 G", brand="Nutella ")

        assert len(resolve([haut, bas])[0]) == 1

    def test_la_contenance_reste_une_barriere_dure(self) -> None:
        """Meme libelle, contenances differentes : deux produits, sans discussion.

        Le blocking les separe avant meme la comparaison textuelle, qui les
        aurait trouves quasi identiques.
        """
        litre = record("COCA COLA ZERO 1L")
        litre.quantity_value, litre.quantity_unit = 1.0, "L"
        canette = record("COCA COLA ZERO 33CL")
        canette.quantity_value, canette.quantity_unit = 0.33, "L"

        groups, funnel, _ = resolve([litre, canette])

        assert len(groups) == 2
        assert funnel["pairs_after_blocking"] == 0


class TestZoneGrise:
    """Ce dont le pipeline doute ne doit pas rester silencieux.

    Entre les deux seuils, il refuse de trancher, et c'est le bon reflexe : une
    fusion a tort est invisible et irreversible. Mais s'arreter la revenait a
    livrer un catalogue dont on SAIT qu'il contient des doublons sans le dire.
    """

    def test_une_paire_douteuse_ressort_pour_etre_tranchee(self) -> None:
        from pipeline.steps.entity_resolution import REJECT_THRESHOLD

        gauche = record("NUTELLA 750 G")
        droite = record("NUTELLA POT 750GR")
        score = pair_similarity(gauche, droite)
        assert REJECT_THRESHOLD <= score < MERGE_THRESHOLD, (
            f"score {score:.2f} hors zone grise : le test ne prouve rien"
        )

        groups, funnel, unresolved = resolve([gauche, droite])

        assert len(groups) == 2, "la paire n'est PAS fusionnee d'office"
        assert funnel["pairs_unresolved"] == 1
        assert len(unresolved) == 1
        assert {unresolved[0].left_label, unresolved[0].right_label} == {
            gauche.label,
            droite.label,
        }

    def test_un_doute_leve_par_transitivite_ne_ressort_pas(self) -> None:
        """A~B doutait, mais A et B ont fusionne avec C : la question n'a plus lieu.

        Sans ce filtrage, l'utilisateur recevrait des questions deja tranchees
        par le pipeline lui-meme, et perdrait confiance dans la file.
        """
        records = [
            record("TOM GRA FR KG REF001", enriched="Tomates grappe France 1 kg"),
            record("TOM GRA FR KG REF011", enriched="Tomates grappe France 1 kg"),
            record("TOM GRA FR KG REF051"),
        ]

        groups, _, unresolved = resolve(records)

        assert len(groups) == 1
        assert unresolved == [], "aucune question sur des fiches deja reunies"


class TestBlocsTropGros:
    """Le cout est quadratique DANS un bloc, pas dans le nombre de fiches."""

    def test_un_bloc_normal_reste_exact(self) -> None:
        """En dessous du seuil, toutes les paires sont comparees."""
        from pipeline.steps.entity_resolution import MAX_EXACT_BLOCK

        records = [record(f"PRODUIT {i}") for i in range(30)]
        _, funnel, _ = resolve(records)

        assert funnel["largest_block"] == 30
        assert funnel["windowed_blocks"] == 0
        assert funnel["pairs_compared"] == funnel["pairs_after_blocking"]
        assert MAX_EXACT_BLOCK >= 30

    def test_un_bloc_enorme_bascule_sur_une_fenetre(self) -> None:
        """Au-dela du seuil, on compare moins, et le funnel le dit.

        Le compte theorique et le compte reel divergent : les confondre
        masquerait exactement le repli qu'on veut pouvoir constater.
        """
        from pipeline.steps.entity_resolution import MAX_EXACT_BLOCK

        records = [record(f"PRODUIT {i:04d}") for i in range(MAX_EXACT_BLOCK + 100)]
        _, funnel, _ = resolve(records)

        assert funnel["windowed_blocks"] == 1
        assert funnel["pairs_compared"] < funnel["pairs_after_blocking"]
