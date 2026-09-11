"""Fusionner ne doit jamais couper le lien avec le systeme du magasin.

Le referentiel ramene 10 000 lignes a une dizaine de fiches. Les 10 000 lignes,
elles, ne disparaissent pas : chacune garde le fil vers la fiche qui la
represente. Sans ce fil, la caisse du magasin annoncerait « stock de X = 12 »
sans que personne puisse dire ce qu'est X, et fusionner reviendrait a perdre le
magasin de vue.

Deux proprietes sont figees ici, et elles vont ensemble :

1. tout ce qui entre est rattache, y compris apres une fusion de quasi-doublons
   (le survivant heritait autrefois d'un total sans heriter des identifiants) ;
2. l'identite d'une fiche survit au depot du lendemain, faute de quoi le fil
   pointerait chaque matin vers une fiche disparue.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio


class _Record:
    """Golden record minimal : seuls les champs que la persistance lit."""

    def __init__(self, key: str, label: str, refs: list[tuple[str, str]]) -> None:
        self.key = key
        self.label = label
        self.label_normalized = key
        self.source_refs = refs
        self.source_rows = len(refs)
        self.ean = None
        self.internal_code = None
        self.url_image = None
        self.url_ok = True
        self.url_checked = True
        self.taxonomy = ("ALIMENTAIRE", "", "", "DIVERS")
        self.vat_rate = 5.5
        self.vat_conflict = False
        self.vat_distribution: dict[str, int] = {}
        self.quantity_value = None
        self.quantity_unit = None
        self.origin = None
        self.status = "VALIDATED"
        self.confidence = 1.0
        self.publishable = True
        self.routing_reasons: list[str] = []
        self.label_enriched = ""
        self.llm_confidence = 0.0

    @property
    def row_ids(self) -> list[str]:
        return [row_id for row_id, _ in self.source_refs]


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Any]:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'rattachement.db'}"
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]

    import api.jobs as jobs
    from db.models import IngestionRun, Product, ProductSourceRow, Store
    from db.session import SessionFactory, migrate

    await migrate()
    async with SessionFactory() as session:
        session.add(Store(id="magasin", name="Magasin"))
        await session.commit()

    class Harness:
        models = (IngestionRun, Product, ProductSourceRow)

        async def deposit(self, run_id: str, records: list[_Record]) -> None:
            async with SessionFactory() as session:
                run = IngestionRun(
                    id=run_id,
                    store_id="magasin",
                    file_name=f"{run_id}.csv",
                    file_sha256=run_id * 8,
                )
                session.add(run)
                await session.commit()
                await jobs._persist_catalogue(session, run, records)
                await session.commit()

        def session(self) -> Any:
            return SessionFactory()

    yield Harness()


@pytest.mark.asyncio
async def test_chaque_ligne_du_fichier_est_rattachee(db: Any) -> None:
    """Aucune ligne ne se perd entre le fichier et le referentiel."""
    from sqlalchemy import func, select

    from db.models import Product, ProductSourceRow

    records = [
        _Record("WHISKY", "WHISKY 70CL", [(f"w{i}", "WHISKY BTL 0.7L") for i in range(945)]),
        _Record("COLA", "COLA 1L", [(f"c{i}", "COCA COLA 1L") for i in range(994)]),
    ]
    await db.deposit("run1", records)

    async with db.session() as session:
        total = await session.scalar(select(func.count(ProductSourceRow.id)))
        products = (await session.execute(select(Product))).scalars().all()

    assert total == 945 + 994, "les 1 939 lignes doivent toutes garder leur fil"
    # Le compte annonce par la fiche et le nombre de fils doivent coincider :
    # une fiche qui annonce plus de lignes qu'elle n'en sait nommer ment.
    assert sum(p.source_rows for p in products) == total


@pytest.mark.asyncio
async def test_une_fusion_conserve_les_lignes_des_fiches_absorbees(db: Any) -> None:
    """Le defaut d'origine : le survivant heritait du total, pas des lignes.

    A la fusion, `source_rows` etait additionne mais les identifiants des
    fiches absorbees etaient jetes. La fiche annoncait « 945 lignes » et n'en
    savait nommer que 12.
    """
    from pipeline.steps.entity_resolution import merge_records

    left = _Record("COCA COLA ZERO 1L", "COCA COLA ZERO 1L", [("a1", "COCA COLA ZERO 1L")] * 1)
    right = _Record("COKA COLA ZERO 1 L", "COKA COLA ZERO 1 L", [("b1", "COKA COLA ZERO 1 L")])
    merged, _, _ = merge_records([left, right], ctx=None)  # type: ignore[arg-type]

    assert len(merged) == 1
    survivor = merged[0]
    assert survivor.source_rows == len(survivor.source_refs) == 2
    assert set(survivor.row_ids) == {"a1", "b1"}


@pytest.mark.asyncio
async def test_le_depot_du_lendemain_conserve_l_identite_des_fiches(db: Any) -> None:
    """Le catalogue est remplace, la fiche garde son identifiant.

    Effacer puis reinserer donnait a chaque fiche un nouvel identifiant chaque
    matin : le rattachement et l'historique des corrections auraient pointe
    vers une fiche disparue, alors que le produit du magasin n'a pas bouge.
    """
    from sqlalchemy import select

    from db.models import Product

    await db.deposit("run1", [_Record("WHISKY", "WHISKY 70CL", [("w1", "WHISKY BTL")])])
    async with db.session() as session:
        first = (await session.execute(select(Product))).scalar_one()
        first_id, first_label = first.id, first.label

    # Le lendemain : meme produit, libelle un peu different, plus de lignes.
    await db.deposit(
        "run2",
        [_Record("WHISKY", "WHISKY ECOSSAIS 70 CL", [("w1", "WHISKY BTL"), ("w2", "WHISKY 40")])],
    )
    async with db.session() as session:
        second = (await session.execute(select(Product))).scalar_one()

    assert second.id == first_id, "l'identifiant de la fiche doit survivre au depot"
    assert second.label != first_label, "le contenu, lui, est bien celui du jour"
    assert second.source_rows == 2


@pytest.mark.asyncio
async def test_un_produit_absent_du_nouveau_fichier_quitte_le_catalogue(db: Any) -> None:
    """Un instantane complet fait autorite : rien ne s'accumule."""
    from sqlalchemy import select

    from db.models import Product, ProductSourceRow

    await db.deposit(
        "run1",
        [
            _Record("WHISKY", "WHISKY 70CL", [("w1", "WHISKY")]),
            _Record("COLA", "COLA 1L", [("c1", "COLA")]),
        ],
    )
    await db.deposit("run2", [_Record("WHISKY", "WHISKY 70CL", [("w1", "WHISKY")])])

    async with db.session() as session:
        labels = {p.product_key for p in (await session.execute(select(Product))).scalars()}
        rows = {r.row_id for r in (await session.execute(select(ProductSourceRow))).scalars()}

    assert labels == {"WHISKY"}, "le produit disparu du fichier quitte le catalogue"
    assert rows == {"w1"}, "et ses rattachements partent avec lui"


@pytest.mark.asyncio
async def test_on_retrouve_la_fiche_a_partir_d_un_identifiant_du_magasin(db: Any) -> None:
    """La question que pose la caisse du magasin : « X, c'est quoi ? »."""
    from sqlalchemy import select

    from db.models import Product, ProductSourceRow

    await db.deposit(
        "run1",
        [_Record("WHISKY", "WHISKY 70CL", [("4f2a-91", "WHISKY BTL 0.7L")])],
    )

    async with db.session() as session:
        found = (
            await session.execute(
                select(ProductSourceRow, Product)
                .join(Product, Product.id == ProductSourceRow.product_id)
                .where(
                    ProductSourceRow.store_id == "magasin",
                    ProductSourceRow.row_id == "4f2a-91",
                )
            )
        ).first()

    assert found is not None
    source_row, product = found
    assert product.label == "WHISKY 70CL"
    # Le libelle d'origine est conserve : sans lui, une fusion erronee serait
    # invisible, faute de pouvoir la contester.
    assert source_row.source_label == "WHISKY BTL 0.7L"
