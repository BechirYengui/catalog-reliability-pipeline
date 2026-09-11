"""La TVA saisie par un humain est verifiee avant d'etre ecrite.

Le pipeline ne corrige jamais un taux tout seul : c'est la regle non
negociable, et elle est tenue. Mais il ne s'ensuit pas qu'un humain puisse en
inventer un. Le champ « autre taux » de la revue etait une chaine libre,
appliquee par `float(value)` sans le moindre controle :

  « abc »  -> ValueError non capturee, HTTP 500 sur un geste banal
  « 99 »   -> 99 % inscrit au referentiel, par le chemin meme qui existe pour
              proteger la TVA

Second defaut du meme chemin : la decision retrouvait les fiches par
correspondance de LIBELLE. Le libelle peut changer entre la creation de la
tache et la decision — une correction humaine, ou simplement un depot ou un
autre libelle du groupe est le plus complet. La decision ne s'appliquait alors
a aucune fiche, mais la tache passait « approuvee », la regle etait ecrite et
l'audit trail restait muet : l'admin croyait avoir tranche.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def plateforme(tmp_path: Path) -> AsyncIterator[Any]:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'decision.db'}"
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]

    import httpx

    from api.main import app
    from api.security import hash_password
    from db.models import IngestionRun, Product, ReviewTask, Store, User
    from db.session import SessionFactory, migrate

    await migrate()
    async with SessionFactory() as session:
        session.add(Store(id="magasin", name="Magasin"))
        session.add(
            User(
                email="admin@ulty.fr",
                password_hash=hash_password("mot-de-passe-de-test"),
                role="admin",
            )
        )
        session.add(
            IngestionRun(id="run-1", store_id="magasin", file_name="x.csv", file_sha256="a" * 64)
        )
        session.add(
            Product(
                id="p-1",
                store_id="magasin",
                run_id="run-1",
                product_key="WHISKY 70CL",
                label="WHISKY 70CL",
                label_normalized="WHISKY 70CL",
                ean="3760000000017",
                url_ok=True,
                url_checked=True,
                taxonomy_1="ALIMENTAIRE",
                taxonomy_2="BOISSONS",
                taxonomy_3="ALCOOLS",
                taxonomy_4="SPIRITUEUX",
                vat_rate=5.5,
                status="NEEDS_REVIEW",
            )
        )
        session.add(
            ReviewTask(
                id="t-1",
                run_id="run-1",
                store_id="magasin",
                kind="vat_mismatch",
                field_name="tva",
                title="WHISKY 70CL",
                question="Appliquer 20 % à tout le groupe ?",
                current_value="5.5% sur 3 ligne(s)",
                proposed_value="20.0",
                source="RULE",
                confidence=0.8,
                affected_rows=3,
                context={"product_key": "WHISKY 70CL", "distribution": {"5.5": 3}},
                requires_admin=True,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        connexion = await http.post(
            "/api/auth/login",
            json={"email": "admin@ulty.fr", "password": "mot-de-passe-de-test"},
        )
        assert connexion.status_code == 200, connexion.text
        yield http, SessionFactory, Product, ReviewTask


class TestUnTauxSaisiEstVerifie:
    @pytest.mark.parametrize(
        ("saisie", "cas"),
        [
            ("99", "taux inexistant"),
            ("33.0", "taux inexistant, ecrit en decimal"),
            ("7", "ancien taux, supprime depuis"),
            ("abc", "pas un nombre"),
            ("", "vide"),
        ],
    )
    async def test_un_taux_hors_bareme_est_refuse(
        self, plateforme: Any, saisie: str, cas: str
    ) -> None:
        http, factory, Product, ReviewTask = plateforme

        reponse = await http.post(
            "/api/review/tasks/t-1/decision", json={"action": "edit", "value": saisie}
        )

        assert reponse.status_code != 500, (
            f"{cas} ({saisie!r}) provoque une erreur serveur au lieu d'un refus lisible"
        )
        assert reponse.status_code in (400, 422), (
            f"{cas} ({saisie!r}) accepte : reponse {reponse.status_code}"
        )

        async with factory() as session:
            fiche = await session.get(Product, "p-1")
            assert fiche is not None
            assert fiche.vat_rate == 5.5, (
                f"{cas} : le taux du referentiel a bouge alors que la saisie est refusee"
            )
            tache = await session.get(ReviewTask, "t-1")
            assert tache is not None
            assert tache.status == "pending", (
                f"{cas} : la tache est marquee tranchee alors que rien n'a ete applique"
            )

    async def test_le_message_de_refus_dit_les_taux_acceptes(self, plateforme: Any) -> None:
        """Une erreur explique ce qui ne va pas ET comment le corriger."""
        http, *_ = plateforme

        reponse = await http.post(
            "/api/review/tasks/t-1/decision", json={"action": "edit", "value": "99"}
        )

        detail = reponse.json()["detail"]
        assert "20" in detail and "5.5" in detail, (
            f"les taux acceptes ne sont pas listes : {detail}"
        )

    @pytest.mark.parametrize("saisie", ["20", "20.0", "5,5", " 10 "])
    async def test_un_taux_legal_est_applique(self, plateforme: Any, saisie: str) -> None:
        """Le garde-fou ne doit pas mordre sur les saisies valides, y compris
        avec une virgule decimale ou des espaces — ce que tape un humain."""
        http, factory, Product, _ = plateforme

        reponse = await http.post(
            "/api/review/tasks/t-1/decision", json={"action": "edit", "value": saisie}
        )

        assert reponse.status_code == 200, f"{saisie!r} refuse a tort : {reponse.text}"
        async with factory() as session:
            fiche = await session.get(Product, "p-1")
            assert fiche is not None
            assert fiche.vat_rate == float(saisie.strip().replace(",", "."))


class TestLaDecisionTrouveLaFicheParSaCle:
    async def test_un_libelle_qui_a_change_n_empeche_pas_la_decision(self, plateforme: Any) -> None:
        """Le defaut : la fiche etait retrouvee par `Product.label == task.title`.

        Ici le libelle de la fiche a change depuis la creation de la tache. Par
        le libelle, la decision ne trouvait rien — et se declarait quand meme
        appliquee.
        """
        http, factory, Product, _ = plateforme

        async with factory() as session:
            fiche = await session.get(Product, "p-1")
            assert fiche is not None
            fiche.label = "WHISKY ECOSSAIS 70 CL"  # un depot a choisi un autre libelle
            await session.commit()

        reponse = await http.post("/api/review/tasks/t-1/decision", json={"action": "approve"})
        assert reponse.status_code == 200, reponse.text

        async with factory() as session:
            fiche = await session.get(Product, "p-1")
            assert fiche is not None
            assert fiche.vat_rate == 20.0, (
                "la decision est marquee approuvee mais le referentiel n'a pas bouge"
            )
