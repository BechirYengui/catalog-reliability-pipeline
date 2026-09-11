"""Le referentiel est cloisonne par magasin.

Ce test existe a cause d'un defaut precis : la liste des magasins etait deduite
cote interface des fiches deja chargees, c'est-a-dire d'une page limitee a 100
lignes triee par volume. Un magasin volumineux occupait la page entiere et les
autres devenaient invisibles, alors qu'ils etaient bien en base. Un decompte
qui porte sur l'echantillon affiche plutot que sur la table ne peut pas etre
juste : il repond a « qu'ai-je sous les yeux », pas a « qu'y a-t-il ».

D'ou la forme du test : on insere PLUS de fiches que la page n'en montre, pour
qu'un comptage cote client echoue et qu'un comptage cote base passe.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

PAGE_LIMIT = 100  # la limite par defaut de /api/products


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[object]:
    """API montee sur une base sqlite jetable, sans reseau ni postgres."""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    # Le depot ecrit le fichier recu sur disque : sans ce reglage, le test
    # viserait /data et echouerait sur les droits plutot que sur la regle.
    os.environ["DATA_DIR"] = str(tmp_path / "inbox")

    # L'import doit suivre la variable d'environnement : le moteur est cree au
    # chargement du module.
    #
    # Les PAQUETS `api` et `db` sont purges eux aussi, pas seulement leurs
    # sous-modules. Sans cela `api/main.py` fait `from api import jobs`, Python
    # trouve l'attribut `jobs` encore accroche a l'ancien objet paquet et rend
    # le module PRECEDENT — celui qui pointe sur la base du test d'avant. La
    # requete HTTP repondait juste et la tache de fond, elle, cherchait ses
    # tables dans une autre base.
    for name in [
        m for m in list(os.sys.modules) if m in ("api", "db") or m.startswith(("api.", "db."))
    ]:
        del os.sys.modules[name]

    import httpx

    from api.main import app
    from api.security import hash_password
    from db.models import Product, Store, User
    from db.session import SessionFactory, migrate

    await migrate()

    async with SessionFactory() as session:
        session.add(
            User(
                email="admin@ulty.fr",
                password_hash=hash_password("mot-de-passe-de-test"),
                role="admin",
            )
        )
        # Franprix depasse a lui seul la page : sans comptage en base, les deux
        # autres magasins sont invisibles.
        for store, count, publishable in [
            ("Franprix_Paris", PAGE_LIMIT + 20, PAGE_LIMIT + 20),
            ("Carrefour_Lyon", 2, 1),
            ("Casino_Nice", 1, 0),
        ]:
            session.add(Store(id=store, name=store))
            for i in range(count):
                session.add(
                    Product(
                        store_id=store,
                        product_key=f"{store} PRODUIT {i}".upper(),
                        label=f"{store} produit {i}",
                        label_normalized=f"{store} PRODUIT {i}".upper(),
                        publishable=i < publishable,
                        source_rows=count - i,  # tri par volume decroissant
                    )
                )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    # base_url en https : le cookie de session porte le drapeau `secure`, donc
    # un client en http ne le conserverait pas et tout le test tomberait en 401.
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        login = await http.post(
            "/api/auth/login",
            json={"email": "admin@ulty.fr", "password": "mot-de-passe-de-test"},
        )
        assert login.status_code == 200, login.text
        yield http


@pytest.mark.asyncio
async def test_chaque_magasin_a_son_referentiel(client) -> None:  # type: ignore[no-untyped-def]
    """Les trois magasins sont listes, avec le compte reel de chacun."""
    response = await client.get("/api/stores")
    assert response.status_code == 200

    catalogs = {row["store_id"]: row for row in response.json()}
    assert set(catalogs) == {"Franprix_Paris", "Carrefour_Lyon", "Casino_Nice"}

    # Le magasin volumineux depasse la page : c'est exactement le cas ou un
    # comptage cote interface se trompait.
    assert catalogs["Franprix_Paris"]["products"] == PAGE_LIMIT + 20
    assert catalogs["Carrefour_Lyon"]["products"] == 2
    assert catalogs["Casino_Nice"]["products"] == 1

    assert catalogs["Carrefour_Lyon"]["publishable"] == 1
    assert catalogs["Casino_Nice"]["publishable"] == 0


@pytest.mark.asyncio
async def test_les_petits_magasins_survivent_a_la_pagination(client) -> None:  # type: ignore[no-untyped-def]
    """Le defaut d'origine, reproduit : la page ne montre qu'un seul magasin.

    C'est ce que voyait l'utilisateur. Le test le fige pour que la liste des
    magasins ne redevienne jamais une deduction faite sur cette page.
    """
    page = (await client.get("/api/products")).json()
    assert {product["store_id"] for product in page} == {"Franprix_Paris"}

    listed = {row["store_id"] for row in (await client.get("/api/stores")).json()}
    assert listed > {"Franprix_Paris"}


@pytest.mark.asyncio
async def test_le_filtre_ne_renvoie_que_le_magasin_demande(client) -> None:  # type: ignore[no-untyped-def]
    """Selectionner un magasin isole son catalogue, sans fuite d'une enseigne."""
    products = (await client.get("/api/products?store_id=Carrefour_Lyon")).json()

    assert len(products) == 2
    assert {product["store_id"] for product in products} == {"Carrefour_Lyon"}


@pytest.mark.asyncio
async def test_le_referentiel_exige_une_session(tmp_path: Path) -> None:
    """Sans cookie, la liste des magasins n'est pas lisible.

    Un referentiel produit est une donnee commerciale : le controle d'acces
    vaut pour l'endpoint d'agregation autant que pour les fiches.
    """
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'anon.db'}"
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]

    import httpx

    from api.main import app
    from db.session import migrate

    await migrate()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        assert (await http.get("/api/stores")).status_code == 401


@pytest.mark.asyncio
async def test_un_depot_sans_magasin_est_refuse(client) -> None:  # type: ignore[no-untyped-def]
    """Le magasin n'a pas de valeur par defaut.

    Un depot anonyme irait grossir un catalogue fourre-tout que personne ne
    reclamerait, alors que le referentiel est cloisonne par magasin.
    """
    fichier = {"file": ("catalogue.csv", b"id_produit,nom\n1,POM\n", "text/csv")}
    refus = await client.post("/api/runs", files=fichier, data={"store_id": "   "})

    assert refus.status_code == 400
    assert "magasin" in refus.json()["detail"]


@pytest.mark.asyncio
async def test_un_nom_de_magasin_ne_peut_pas_sortir_du_dossier(client) -> None:  # type: ignore[no-untyped-def]
    """Ce nom entre dans un chemin de fichier.

    Sans controle, un magasin nomme « ../../etc » ecrirait hors du dossier de
    depot. Masquer le champ dans l'interface ne protege rien : la validation
    appartient au serveur.
    """
    fichier = {"file": ("catalogue.csv", b"id_produit,nom\n1,POM\n", "text/csv")}
    for hostile in ("../../etc/passwd", "a/b", "avec espace", "x" * 65):
        refus = await client.post("/api/runs", files=fichier, data={"store_id": hostile})
        assert refus.status_code == 400, f"{hostile!r} aurait du etre refuse"
        assert "invalide" in refus.json()["detail"]


def test_la_regle_des_noms_de_magasin_couvre_les_cas_reels() -> None:
    """La regle elle-meme, sans base ni fichier.

    Elle doit refuser ce qui sort du dossier de depot et accepter ce qui existe
    deja en production, « Intermarché_01 » compris : un controle qui casserait
    les magasins en service serait pire que pas de controle.
    """
    from api.main import STORE_ID_PATTERN

    for accepte in ("Franprix_Paris", "Intermarché_01", "carrefour-lyon", "M1"):
        assert STORE_ID_PATTERN.match(accepte), f"{accepte!r} devrait etre accepte"

    for refuse in ("../../etc/passwd", "a/b", "avec espace", "", "x" * 65, "."):
        assert not STORE_ID_PATTERN.match(refuse), f"{refuse!r} devrait etre refuse"
