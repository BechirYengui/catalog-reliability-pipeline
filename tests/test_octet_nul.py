"""L'octet NUL ne doit atteindre aucune colonne.

PostgreSQL refuse `\\x00` dans un TEXT. Ce n'est pas theorique : le run
35b44a78 du 2026-08-11 est mort dessus en production
(`CharacterNotInRepertoireError`), sur une cle de regle apprise. Le separateur
a ete change, mais la porte d'entree est restee ouverte.

Mesure faite avant ce correctif, apres l'etape d'ingestion complete :

    nom          NUL retire    <- effet de bord de ftfy, pas du code du projet
    id_produit   NUL SURVIT    -> ProductSourceRow.row_id
    url_image    NUL SURVIT    -> Product.url_image, url_check_cache.url

Le nettoyage de texte ne s'appliquait qu'a 5 colonnes sur 9. Les quatre autres
(`id_produit`, `ean`, `url_image`, `tva`) ne recevaient qu'un `strip_chars`,
qui ne retire que les blancs — et `'\\x00'.isspace()` vaut False.

Deux consequences distinctes. La premiere : un magasin dont l'export de caisse
porte un NUL fait tomber le run a la PERSISTANCE, c'est-a-dire apres avoir paye
l'etage LLM et les quatre minutes de verification d'images. La seconde, plus
sournoise : la protection sur `nom` ne vient pas du projet mais d'une
dependance tierce, donc une mise a jour de ftfy la retirerait sans que rien ne
le signale.

Ces tests portent sur les NEUF colonnes, pas sur celles qui se trouvaient
protegees par accident.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pipeline.config import PipelineConfig
from pipeline.context import RunContext
from pipeline.steps import ingest

COLONNES = (
    "id_produit",
    "ean",
    "nom",
    "url_image",
    "taxonomie_niveau_1",
    "taxonomie_niveau_2",
    "taxonomie_niveau_3",
    "taxonomie_niveau_4",
    "tva",
)


def _csv_avec_nul(valeurs: dict[str, str]) -> Path:
    chemin = Path(tempfile.mkdtemp()) / "nul.csv"
    ligne = ",".join(valeurs[c] for c in COLONNES)
    chemin.write_bytes(f"{','.join(COLONNES)}\n{ligne}\n".encode())
    return chemin


@pytest.fixture
def base() -> dict[str, str]:
    return {
        "id_produit": "ID123",
        "ean": "3017620422003",
        "nom": "CAFE MOULU",
        "url_image": "https://exemple.test/a.jpg",
        "taxonomie_niveau_1": "ALIMENTAIRE",
        "taxonomie_niveau_2": "EPICERIE",
        "taxonomie_niveau_3": "CAFE",
        "taxonomie_niveau_4": "MOULU",
        "tva": "5.5",
    }


@pytest.mark.parametrize("colonne", COLONNES)
def test_aucune_colonne_ne_laisse_passer_un_nul(base: dict[str, str], colonne: str) -> None:
    """Chaque colonne est testee separement : un test global passerait encore
    si UNE seule colonne restait trouee, et c'est exactement ce qui s'etait
    produit."""
    valeurs = dict(base)
    valeurs[colonne] = valeurs[colonne][:2] + "\x00" + valeurs[colonne][2:]

    df = ingest.read_csv(_csv_avec_nul(valeurs))

    for nom_colonne in COLONNES:
        cellule = df[nom_colonne][0] or ""
        assert "\x00" not in cellule, (
            f"un NUL injecte dans {colonne!r} survit dans {nom_colonne!r} : "
            f"PostgreSQL refusera l'ecriture et le run tombera a la persistance"
        )


def test_le_nul_est_retire_sans_abimer_le_reste(base: dict[str, str]) -> None:
    """Retirer l'octet ne doit pas emporter les caracteres voisins."""
    valeurs = dict(base)
    valeurs["id_produit"] = "ID\x00123"
    valeurs["url_image"] = "https://exemple.test/a\x00.jpg"

    df = ingest.read_csv(_csv_avec_nul(valeurs))

    assert df["id_produit"][0] == "ID123"
    assert df["url_image"][0] == "https://exemple.test/a.jpg"


def test_le_nettoyage_survit_a_l_etape_complete(base: dict[str, str]) -> None:
    """`read_csv` est appele SEPAREMENT de `run()` par les deux orchestrateurs.
    Le nettoyage doit tenir sur le chemin reel, pas seulement a la lecture."""
    valeurs = dict(base)
    valeurs["id_produit"] = "ID\x00123"
    valeurs["url_image"] = "https://exemple.test/a\x00.jpg"
    chemin = _csv_avec_nul(valeurs)

    ctx = RunContext(
        store_id="t", source_file=chemin, config=PipelineConfig.load(), network_enabled=False
    )
    df = ingest.run(ingest.read_csv(chemin), ctx).df

    for nom_colonne in COLONNES:
        if nom_colonne in df.columns:
            assert "\x00" not in (df[nom_colonne][0] or "")


def test_un_fichier_sain_n_est_pas_touche(base: dict[str, str]) -> None:
    """Le nettoyage ne doit rien changer quand il n'y a rien a nettoyer."""
    df = ingest.read_csv(_csv_avec_nul(base))

    for colonne, attendu in base.items():
        assert df[colonne][0] == attendu, f"{colonne} a ete modifiee sans raison"
