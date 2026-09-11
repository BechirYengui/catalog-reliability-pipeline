"""Une correction faite a la main doit tenir au depot du lendemain.

C'est la seule chose qui compte ici. Un bouton « modifier » qui s'affiche, qui
marche, et dont l'effet disparait le matin suivant est pire qu'un bouton
absent : l'utilisateur fait le geste, constate le resultat, et perd son travail
sans qu'aucun message ne le signale.

Le referentiel etant recalcule depuis le CSV a chaque integration, la
correction n'est pas stockee comme un etat de la fiche mais comme une decision
portant sur la cle naturelle du produit, rejouee apres l'etage LLM.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio


class _Record:
    """Golden record minimal : les champs que la persistance lit."""

    def __init__(self, key: str, label: str, refs: list[tuple[str, str]]) -> None:
        self.key = key
        self.label = label
        self.label_normalized = key
        self.source_refs = refs
        self.source_rows = len(refs)
        self.ean = "3760000000017"
        self.internal_code = None
        self.url_image = None
        self.url_ok = True
        self.url_checked = True
        self.taxonomy = ("ALIMENTAIRE", "FRUITS", "POMMES", "POMMES")
        self.vat_rate = 5.5
        self.vat_conflict = False
        self.vat_distribution: dict[str, int] = {}
        self.quantity_value = 1.0
        self.quantity_unit = "KG"
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
async def plateforme(tmp_path: Path) -> AsyncIterator[Any]:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'overrides.db'}"
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]

    import httpx

    import api.jobs as jobs
    from api.main import app
    from api.security import hash_password
    from db.models import IngestionRun, Store, User
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
        await session.commit()

    class Plateforme:
        def __init__(self, http: Any) -> None:
            self.http = http

        async def deposer(self, run_id: str, records: list[_Record]) -> None:
            """Rejoue un depot complet, comme le ferait un fichier du magasin."""
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

        async def fiche(self) -> dict[str, Any]:
            produits = (await self.http.get("/api/products")).json()
            assert produits, "aucune fiche au catalogue"
            return produits[0]

    transport = httpx.ASGITransport(app=app)
    # https : le cookie de session porte le drapeau `secure`.
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        connexion = await http.post(
            "/api/auth/login",
            json={"email": "admin@ulty.fr", "password": "mot-de-passe-de-test"},
        )
        assert connexion.status_code == 200, connexion.text
        yield Plateforme(http)


POMME = [_Record("POM B 1K C1", "POM B 1K C1", [("r1", "POM B 1K C1")])]


@pytest.mark.asyncio
async def test_une_correction_survit_au_depot_du_lendemain(plateforme: Any) -> None:
    """Le test qui justifie toute la table.

    Sans rejeu, le depot suivant recalcule la fiche depuis le CSV et rend son
    libelle de caisse, effacant la correction sans le dire.
    """
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    modifiee = await plateforme.http.patch(
        f"/api/products/{fiche['id']}",
        json={"field_name": "label", "value": "Pommes bio 1 kg catégorie 1"},
    )
    assert modifiee.status_code == 200
    assert modifiee.json()["label"] == "Pommes bio 1 kg catégorie 1"

    # Le lendemain : le magasin redepose son fichier, inchange.
    await plateforme.deposer("run2", POMME)
    assert (await plateforme.fiche())["label"] == "Pommes bio 1 kg catégorie 1"


@pytest.mark.asyncio
async def test_annuler_rend_la_valeur_calculee_par_le_pipeline(plateforme: Any) -> None:
    """Annuler doit se voir tout de suite, pas au depot suivant.

    Supprimer la seule regle laisserait la fiche dans son etat corrige jusqu'au
    lendemain : l'utilisateur cliquerait et ne verrait rien changer.
    """
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    await plateforme.http.patch(
        f"/api/products/{fiche['id']}", json={"field_name": "label", "value": "Pommes bio"}
    )
    annulee = await plateforme.http.delete(f"/api/products/{fiche['id']}/overrides/label")

    assert annulee.status_code == 200
    assert annulee.json()["label"] == "POM B 1K C1"
    await plateforme.deposer("run2", POMME)
    assert (await plateforme.fiche())["label"] == "POM B 1K C1"


@pytest.mark.asyncio
async def test_corriger_deux_fois_garde_le_point_de_retour(plateforme: Any) -> None:
    """La valeur d'origine conservee est celle du pipeline, pas la precedente
    correction. Sinon, se raviser deux fois ferait perdre le retour arriere."""
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    for valeur in ("Pommes bio", "Pommes bio 1 kg"):
        await plateforme.http.patch(
            f"/api/products/{fiche['id']}", json={"field_name": "label", "value": valeur}
        )

    annulee = await plateforme.http.delete(f"/api/products/{fiche['id']}/overrides/label")
    assert annulee.json()["label"] == "POM B 1K C1"


@pytest.mark.asyncio
async def test_la_tva_n_est_pas_modifiable_par_cette_route(plateforme: Any) -> None:
    """Regle absolue du projet : une TVA ne se corrige pas dans un champ libre.

    Elle passe par la file de revue, ou le role administrateur est verifie cote
    serveur, parce que l'erreur a une consequence fiscale.
    """
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    refus = await plateforme.http.patch(
        f"/api/products/{fiche['id']}", json={"field_name": "vat_rate", "value": "20"}
    )
    assert refus.status_code == 422
    assert (await plateforme.fiche())["vat_rate"] == 5.5


@pytest.mark.asyncio
async def test_la_correction_est_tracee_dans_l_audit_trail(plateforme: Any) -> None:
    """Chaque correction porte sa valeur d'origine, son auteur et son horodatage."""
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    await plateforme.http.patch(
        f"/api/products/{fiche['id']}",
        json={"field_name": "taxonomy_4", "value": "POMMES BIO"},
    )

    overrides = (await plateforme.http.get(f"/api/products/{fiche['id']}/overrides")).json()
    assert len(overrides) == 1
    assert overrides[0]["field_name"] == "taxonomy_4"
    assert overrides[0]["previous_value"] == "POMMES"
    assert overrides[0]["value"] == "POMMES BIO"


@pytest.mark.asyncio
async def test_un_champ_numerique_reste_un_nombre(plateforme: Any) -> None:
    """La table stocke du texte ; la fiche, elle, doit garder son type."""
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    modifiee = await plateforme.http.patch(
        f"/api/products/{fiche['id']}", json={"field_name": "quantity_value", "value": "1.5"}
    )
    assert modifiee.json()["quantity_value"] == 1.5

    refus = await plateforme.http.patch(
        f"/api/products/{fiche['id']}", json={"field_name": "quantity_value", "value": "beaucoup"}
    )
    assert refus.status_code == 400


@pytest.mark.asyncio
async def test_le_journal_d_audit_est_lisible_sans_connaitre_la_base(plateforme: Any) -> None:
    """Une ligne du journal doit se comprendre seule.

    La table ne stocke que des identifiants. Sans le magasin, le produit et
    l'auteur joints, il faudrait une seconde requete pour savoir de quoi parle
    la ligne, et un journal qu'on ne peut pas lire n'en est pas un.
    """
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    await plateforme.http.patch(
        f"/api/products/{fiche['id']}",
        json={"field_name": "label", "value": "Pommes bio 1 kg"},
    )

    journal = (await plateforme.http.get("/api/audit")).json()
    assert journal, "le journal doit contenir la correction qu'on vient de faire"

    entree = journal[0]
    assert entree["author"] == "HUMAN"
    assert entree["field_name"] == "label"
    assert entree["old_value"] == "POM B 1K C1"
    assert entree["new_value"] == "Pommes bio 1 kg"
    assert entree["store_id"] == "magasin"
    assert entree["user_email"] == "admin@ulty.fr"


@pytest.mark.asyncio
async def test_le_journal_se_filtre_par_auteur(plateforme: Any) -> None:
    """Separer ce qu'une regle a fait de ce qu'un humain a decide est le premier
    usage du journal : les deux n'engagent pas la meme responsabilite."""
    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()
    await plateforme.http.patch(
        f"/api/products/{fiche['id']}", json={"field_name": "label", "value": "Pommes bio"}
    )

    humaines = (await plateforme.http.get("/api/audit?author=HUMAN")).json()
    regles = (await plateforme.http.get("/api/audit?author=RULE")).json()

    assert len(humaines) == 1
    assert all(e["author"] == "HUMAN" for e in humaines)
    assert regles == []


@pytest.mark.asyncio
async def test_une_decision_de_tva_dit_ce_qu_elle_remplace(plateforme: Any) -> None:
    """Le journal doit nommer les taux corriges, pas repeter celui qui reste.

    La fiche porte deja le taux majoritaire, herite de la fusion. Enregistrer
    « 20 -> 20 » etait vrai pour la fiche et muet sur la decision : l'utilisateur
    lisait une ligne ou rien ne semblait avoir change, alors qu'il venait
    d'unifier des dizaines de lignes divergentes.
    """
    from db.models import ReviewTask
    from db.session import SessionFactory

    await plateforme.deposer("run1", POMME)
    fiche = await plateforme.fiche()

    async with SessionFactory() as session:
        session.add(
            ReviewTask(
                run_id="run1",
                store_id="magasin",
                kind="vat_mismatch",
                field_name="tva",
                title=fiche["label"],
                question="Appliquer 5.5 % partout ?",
                current_value="5.5",
                proposed_value="5.5",
                source="RULE",
                confidence=1.0,
                affected_rows=32,
                context={"distribution": {"5.5": 913, "10": 15, "2.1": 8}},
                status="pending",
                requires_admin=True,
            )
        )
        await session.commit()

    taches = (await plateforme.http.get("/api/review/tasks?status_filter=pending")).json()
    assert taches, "la tache doit etre en attente"
    decision = await plateforme.http.post(
        f"/api/review/tasks/{taches[0]['id']}/decision", json={"action": "approve"}
    )
    assert decision.status_code == 200, decision.text

    journal = (await plateforme.http.get("/api/audit?author=HUMAN")).json()
    tva = next(e for e in journal if e["field_name"] == "tva")
    # Les taux remplaces sont nommes, avec le nombre de lignes concernees.
    assert "10" in (tva["old_value"] or "")
    assert "2.1" in (tva["old_value"] or "")
    assert "913" not in (tva["old_value"] or ""), "le taux retenu n'est pas un taux remplace"
    assert tva["new_value"] == "5.5"
    assert "23 lignes" in tva["rule"]
