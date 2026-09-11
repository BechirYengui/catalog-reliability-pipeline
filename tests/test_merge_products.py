"""Fusionner deux fiches a la main, et que ca tienne demain.

Le pipeline n'a pas su trancher — s'il avait su, il l'aurait fait. La fusion
manuelle est donc une DECISION, pas une correction de donnee, et elle doit
laisser trois traces : le referentiel change, l'audit trail dit qui a decide,
une regle apprise rejoue la fusion au depot suivant. Sans la troisieme,
l'utilisateur refait le meme geste chaque matin.

Deux refus sont verrouilles ici : ce sont les seuls cas ou la machine SAIT que
l'humain se trompe.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[object, object]]:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'merge.db'}"
    os.environ["DATA_DIR"] = str(tmp_path)
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]

    import httpx

    from api.main import app
    from api.security import hash_password
    from db.models import Product, ProductSourceRow, Store, User
    from db.session import SessionFactory, create_all

    await create_all()

    def fiche(pid: str, label: str, rows: int, **kwargs: object) -> Product:
        return Product(
            id=pid,
            store_id="Franprix_Paris",
            label=label,
            label_normalized=label,
            product_key=label,
            source_rows=rows,
            publishable=True,
            status="VALIDATED",
            **kwargs,  # type: ignore[arg-type]
        )

    async with SessionFactory() as session:
        session.add(
            User(
                id="admin-1",
                email="admin@ulty.fr",
                password_hash=hash_password("mot-de-passe-de-test"),
                role="admin",
            )
        )
        session.add(Store(id="Franprix_Paris", name="Franprix_Paris"))
        # Le cas reel : une famille coupee en deux par l'etape floue.
        session.add(fiche("p-17", "TOM GRA FR KG REF001", 17, vat_rate=5.5))
        session.add(fiche("p-3", "TOM GRA FR KG REF051", 3, vat_rate=5.5))
        # Deux references distinctes, que la machine sait distinguer.
        session.add(fiche("p-doli", "DOLIPRANE 1000 MG", 20, brand="Doliprane"))
        session.add(fiche("p-gene", "PARACETAMOL 1000 MG", 20, brand="Biogaran"))
        session.add(fiche("p-1l", "COCA ZERO 1L", 10, quantity_value=1.0, quantity_unit="L"))
        session.add(fiche("p-33", "COCA ZERO 33CL", 10, quantity_value=0.33, quantity_unit="L"))
        for i in range(3):
            session.add(
                ProductSourceRow(
                    store_id="Franprix_Paris",
                    product_id="p-3",
                    run_id="run-1",
                    row_id=f"REF{i:03d}",
                    source_label="TOM GRA FR KG",
                )
            )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        login = await http.post(
            "/api/auth/login",
            json={"email": "admin@ulty.fr", "password": "mot-de-passe-de-test"},
        )
        assert login.status_code == 200, login.text
        yield http, SessionFactory


@pytest.mark.asyncio
async def test_la_fusion_regroupe_les_lignes_du_magasin(client) -> None:  # type: ignore[no-untyped-def]
    """Les 3 lignes de la fiche absorbee suivent : une fiche de 20 lignes."""
    http, factory = client

    response = await http.post("/api/products/merge", json={"product_ids": ["p-17", "p-3"]})

    assert response.status_code == 200, response.text
    survivor = response.json()
    # Le survivant porte le plus de lignes du magasin : son identifiant est
    # celui qui circule deja le plus chez le magasin.
    assert survivor["id"] == "p-17"
    assert survivor["source_rows"] == 20

    from sqlalchemy import select

    from db.models import Product, ProductSourceRow

    async with factory() as session:  # type: ignore[operator]
        assert await session.get(Product, "p-3") is None
        rows = (
            (
                await session.execute(
                    select(ProductSourceRow).where(ProductSourceRow.product_id == "p-17")
                )
            )
            .scalars()
            .all()
        )
        # Sans ce transfert, la fiche annoncerait 20 lignes sans savoir en
        # nommer trois.
        assert len(rows) == 3


@pytest.mark.asyncio
async def test_la_fusion_laisse_une_trace_et_une_regle(client) -> None:  # type: ignore[no-untyped-def]
    """Qui a decide quoi, et la meme fusion rejouee au depot suivant."""
    http, factory = client

    await http.post("/api/products/merge", json={"product_ids": ["p-17", "p-3"]})

    from sqlalchemy import select

    from api import jobs
    from db.models import LearnedRule, ProductCorrection

    async with factory() as session:  # type: ignore[operator]
        corrections = (
            (
                await session.execute(
                    select(ProductCorrection).where(ProductCorrection.field_name == "product_merge")
                )
            )
            .scalars()
            .all()
        )
        assert len(corrections) == 1
        assert corrections[0].author == "HUMAN"
        assert corrections[0].old_value == "TOM GRA FR KG REF051"
        assert corrections[0].new_value == "TOM GRA FR KG REF001"
        assert corrections[0].user_id == "admin-1"

        rules = (
            (await session.execute(select(LearnedRule).where(LearnedRule.scope == "same_product")))
            .scalars()
            .all()
        )
        assert len(rules) == 1
        # La cle porte le magasin : l'API refuse de fusionner deux fiches de
        # magasins differents, la regle ne doit donc pas franchir l'enseigne.
        # Separateur \x1f, jamais \x00 : Postgres refuse l'octet NUL dans un
        # TEXT (panne de production du 2026-08-11). La cle est construite par
        # `merge_rule_key`, on l'appelle plutot que de la recopier — recopier
        # le separateur ici a justement laisse passer le bug.
        assert rules[0].key == jobs.merge_rule_key("Franprix_Paris", "TOM GRA FR KG REF051")
        assert "\x00" not in rules[0].key
        assert rules[0].value == "TOM GRA FR KG REF001"


@pytest.mark.asyncio
async def test_annuler_rend_la_fiche_avec_ses_lignes(client) -> None:  # type: ignore[no-untyped-def]
    """Une decision qu'on ne peut pas reprendre est un piege, pas une decision.

    L'utilisateur tranche ici un cas que la machine n'a pas su trancher : il se
    trompera parfois. Annuler doit tout remettre en place TOUT DE SUITE — pas
    au depot du lendemain.
    """
    http, factory = client

    await http.post("/api/products/merge", json={"product_ids": ["p-17", "p-3"]})
    merges = (await http.get("/api/merges")).json()
    assert len(merges) == 1
    assert merges[0]["absorbed_label"] == "TOM GRA FR KG REF051"

    restored = await http.post(f"/api/merges/{merges[0]['id']}/undo")
    assert restored.status_code == 200, restored.text

    from sqlalchemy import select

    from db.models import LearnedRule, Product, ProductSourceRow

    async with factory() as session:  # type: ignore[operator]
        revenue = await session.get(Product, "p-3")
        assert revenue is not None, "la fiche absorbee n'est pas revenue"
        assert revenue.label == "TOM GRA FR KG REF051"
        assert revenue.source_rows == 3

        survivor = await session.get(Product, "p-17")
        assert survivor is not None
        assert survivor.source_rows == 17

        # Ses lignes lui reviennent : celles-la precisement, pas celles du
        # survivant.
        rows = (
            (
                await session.execute(
                    select(ProductSourceRow).where(ProductSourceRow.product_id == "p-3")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3

        # La regle apprise disparait, sinon le depot de demain referait la
        # fusion que l'utilisateur vient d'annuler.
        rules = (
            (await session.execute(select(LearnedRule).where(LearnedRule.scope == "same_product")))
            .scalars()
            .all()
        )
        assert rules == []


@pytest.mark.asyncio
async def test_annuler_deux_fois_est_refuse(client) -> None:  # type: ignore[no-untyped-def]
    http, _ = client

    await http.post("/api/products/merge", json={"product_ids": ["p-17", "p-3"]})
    merge_id = (await http.get("/api/merges")).json()[0]["id"]

    assert (await http.post(f"/api/merges/{merge_id}/undo")).status_code == 200
    assert (await http.post(f"/api/merges/{merge_id}/undo")).status_code == 409
    # Une fusion annulee ne figure plus dans la liste des fusions en vigueur.
    assert (await http.get("/api/merges")).json() == []


@pytest.mark.asyncio
async def test_l_audit_trail_garde_les_deux_gestes(client) -> None:  # type: ignore[no-untyped-def]
    """Annuler n'efface pas l'historique : il gagne une ligne de plus."""
    http, factory = client

    await http.post("/api/products/merge", json={"product_ids": ["p-17", "p-3"]})
    merge_id = (await http.get("/api/merges")).json()[0]["id"]
    await http.post(f"/api/merges/{merge_id}/undo")

    from sqlalchemy import select

    from db.models import ProductCorrection

    async with factory() as session:  # type: ignore[operator]
        fields = [
            c.field_name for c in (await session.execute(select(ProductCorrection))).scalars().all()
        ]
        assert sorted(fields) == ["product_merge", "product_merge_undone"]


@pytest.mark.asyncio
async def test_deux_marques_differentes_sont_refusees(client) -> None:  # type: ignore[no-untyped-def]
    """La machine sait que c'est faux : elle refuse, en disant pourquoi."""
    http, _ = client

    response = await http.post("/api/products/merge", json={"product_ids": ["p-doli", "p-gene"]})

    assert response.status_code == 409
    assert "marques" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_deux_contenances_differentes_sont_refusees(client) -> None:  # type: ignore[no-untyped-def]
    http, _ = client

    response = await http.post("/api/products/merge", json={"product_ids": ["p-1l", "p-33"]})

    assert response.status_code == 409
    assert "contenance" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_une_seule_fiche_ne_fusionne_rien(client) -> None:  # type: ignore[no-untyped-def]
    http, _ = client

    response = await http.post("/api/products/merge", json={"product_ids": ["p-17"]})

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_la_fusion_exige_une_session(tmp_path: Path) -> None:
    """Modifier le referentiel n'est pas ouvert a un anonyme."""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'anon.db'}"
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]

    import httpx

    from api.main import app
    from db.session import create_all

    await create_all()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        response = await http.post("/api/products/merge", json={"product_ids": ["a", "b"]})
        assert response.status_code == 401
