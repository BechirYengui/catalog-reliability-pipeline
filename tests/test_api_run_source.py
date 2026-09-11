"""Le fichier d'origine reste telechargeable, et intact.

Une plateforme qui ne rend que sa sortie demande qu'on la croie sur parole :
sans le fichier d'entree, personne ne peut verifier ce que le pipeline a
change. D'ou cet endpoint, et d'ou la forme de ces tests : ce qui compte n'est
pas qu'un CSV revienne, c'est que ce soient les MEMES octets — un original
« corrige » a la volee (BOM ajoute, reencodage) ne prouverait plus rien.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

# Encodage casse volontairement : le fichier d'origine doit revenir avec son
# mojibake, c'est justement ce que l'export fiabilise, lui, aura repare.
BROKEN_CSV = "ean;nom\n3017620422003;CAFÃ‰ MOULU 250G\n".encode()
# L'empreinte REELLE : c'est elle qui permet de retrouver un original renomme,
# donc un run de test qui en porterait une fausse ne prouverait rien.
BROKEN_SHA = hashlib.sha256(BROKEN_CSV).hexdigest()


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[object, Path]]:
    """API montee sur une base sqlite jetable, avec son propre DATA_DIR."""
    data_dir = tmp_path / "data"
    for nom in ("inbox", "sources", "processed", "quarantine"):
        (data_dir / nom).mkdir(parents=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    os.environ["DATA_DIR"] = str(data_dir)

    # L'import doit suivre les variables d'environnement : le moteur et le
    # DATA_DIR sont fixes au chargement du module. Les paquets `api` et `db`
    # sont purges avec leurs sous-modules : sinon `from api import jobs` rend
    # l'ancien module, encore accroche a l'objet paquet et branche sur la base
    # du test precedent.
    for name in [
        m for m in list(os.sys.modules) if m in ("api", "db") or m.startswith(("api.", "db."))
    ]:
        del os.sys.modules[name]

    import httpx

    from api.main import app
    from api.security import hash_password
    from db.models import IngestionRun, Store, User
    from db.session import SessionFactory, create_all

    await create_all()

    source = data_dir / "sources" / "Franprix_Paris_20260810-000000.csv"
    source.write_bytes(BROKEN_CSV)

    async with SessionFactory() as session:
        session.add(
            User(
                email="admin@ulty.fr",
                password_hash=hash_password("mot-de-passe-de-test"),
                role="admin",
            )
        )
        session.add(Store(id="Franprix_Paris", name="Franprix_Paris"))
        session.add(
            IngestionRun(
                id="run-1",
                store_id="Franprix_Paris",
                file_name=str(source),
                file_sha256=BROKEN_SHA,
                file_size=len(BROKEN_CSV),
                status="COMPLETED",
            )
        )
        # Un run dont le chemin pointe hors du repertoire de donnees : la base
        # ne doit pas pouvoir servir de tremplin vers le reste du serveur.
        session.add(
            IngestionRun(
                id="run-hors-zone",
                store_id="Franprix_Paris",
                file_name="/etc/passwd",
                file_sha256="b" * 64,
                status="COMPLETED",
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    # base_url en https : le cookie de session porte le drapeau `secure`.
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        login = await http.post(
            "/api/auth/login",
            json={"email": "admin@ulty.fr", "password": "mot-de-passe-de-test"},
        )
        assert login.status_code == 200, login.text
        yield http, source


@pytest.mark.asyncio
async def test_l_original_revient_octet_pour_octet(client) -> None:  # type: ignore[no-untyped-def]
    """Aucun BOM ajoute, aucun reencodage : la piece de comparaison est brute."""
    http, _ = client
    response = await http.get("/api/runs/run-1/source.csv")

    assert response.status_code == 200
    assert response.content == BROKEN_CSV
    assert not response.content.startswith(b"\xef\xbb\xbf")
    assert "attachment" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_un_chemin_hors_du_repertoire_de_donnees_est_refuse(client) -> None:  # type: ignore[no-untyped-def]
    """Le chemin vient de la base, il est quand meme contraint a DATA_DIR."""
    http, _ = client
    response = await http.get("/api/runs/run-hors-zone/source.csv")

    assert response.status_code == 404
    assert b"root:" not in response.content


@pytest.mark.asyncio
async def test_un_fichier_efface_repond_404(client) -> None:  # type: ignore[no-untyped-def]
    """Purger l'inbox ne doit pas casser la page, juste retirer le fichier."""
    http, source = client
    source.unlink()

    assert (await http.get("/api/runs/run-1/source.csv")).status_code == 404


@pytest.mark.asyncio
async def test_le_run_inconnu_repond_404(client) -> None:  # type: ignore[no-untyped-def]
    http, _ = client
    assert (await http.get("/api/runs/inexistant/source.csv")).status_code == 404


@pytest.mark.asyncio
async def test_le_telechargement_exige_une_session(tmp_path: Path) -> None:
    """Le fichier depose est une donnee commerciale : pas d'acces anonyme."""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'anon.db'}"
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    for name in [m for m in list(os.sys.modules) if m.startswith(("api.", "db."))]:
        del os.sys.modules[name]

    import httpx

    from api.main import app
    from db.session import create_all

    await create_all()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as http:
        assert (await http.get("/api/runs/run-1/source.csv")).status_code == 401


@pytest.mark.asyncio
async def test_un_original_deplace_est_retrouve_par_son_empreinte(client) -> None:  # type: ignore[no-untyped-def]
    """Le balayage nocturne renomme les fichiers ; le run doit s'y retrouver.

    Le flux automatique vide `inbox/` et deplace chaque CSV dans `processed/`
    avec un horodatage en plus. Les originaux deposes par l'interface y ont ete
    emportes : cinq runs de production pointaient vers un chemin mort. Le nom a
    change, l'empreinte non — et elle est deja en base.
    """
    http, source = client
    nom = "Franprix_Paris_20260810-000000-20260811-053218.csv"
    deplace = source.parent.parent / "processed" / nom
    deplace.write_bytes(source.read_bytes())
    source.unlink()

    response = await http.get("/api/runs/run-1/source.csv")

    assert response.status_code == 200
    assert response.content == BROKEN_CSV

    # Le chemin est repare en base : la recherche par empreinte ne se refait
    # pas a chaque clic.
    seconde = await http.get("/api/runs/run-1/source.csv")
    assert seconde.status_code == 200


@pytest.mark.asyncio
async def test_un_fichier_de_meme_taille_mais_different_ne_passe_pas(client) -> None:  # type: ignore[no-untyped-def]
    """La taille filtre, c'est l'empreinte qui decide."""
    http, source = client
    leurre = source.parent.parent / "processed" / "un-autre-magasin.csv"
    leurre.write_bytes(b"X" * len(BROKEN_CSV))
    source.unlink()

    assert (await http.get("/api/runs/run-1/source.csv")).status_code == 404
