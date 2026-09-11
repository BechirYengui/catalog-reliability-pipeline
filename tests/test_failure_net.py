"""Le filet d'echec doit survivre a la mort de sa propre transaction.

Reproduction de la panne de production du 2026-08-11 (run 35b44a78) : la
requete des regles apprises portait une cle avec un octet NUL, que Postgres
refuse. L'exception a bien ete attrapee — mais le filet ecrivait FAILED dans
la MEME session, dont la transaction etait avortee : son commit a echoue
aussi, et le run est reste « RUNNING » a jamais, sans erreur visible.

Deux verrous ici : la cle ne peut plus contenir de NUL, et le filet fait
rollback AVANT d'ecrire l'echec — il fonctionne donc meme quand c'est la base
elle-meme qui a fait tomber le run.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

# L'environnement est pose AVANT d'importer l'API : moteur et DATA_DIR sont
# fixes au chargement des modules (meme motif que test_quarantine.py).
_TMP = Path(tempfile.mkdtemp(prefix="catalog-failnet-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP / 'failnet.db'}"
os.environ["DATA_DIR"] = str(_TMP)

from sqlalchemy import text  # noqa: E402

from api import jobs  # noqa: E402
from db.models import IngestionRun, Store  # noqa: E402
from db.session import SessionFactory, create_all  # noqa: E402

SAMPLE = Path(__file__).parent / "data" / "sample_anomalies.csv"


class TestMergeRuleKey:
    def test_la_cle_ne_contient_jamais_d_octet_nul(self) -> None:
        """Postgres refuse 0x00 dans un TEXT ; sqlite l'avale sans un mot.
        C'est ce silence des tests qui a laisse passer la panne : l'invariant
        est desormais verrouille ici, cote sqlite aussi."""
        key = jobs.merge_rule_key("TEST01", "COCA COLA ZERO 1L")
        assert "\x00" not in key

    def test_le_prefixe_d_un_magasin_ne_matche_pas_un_autre(self) -> None:
        """Le separateur doit rester un vrai separateur : « TEST0 » ne doit
        pas prefixer les regles de « TEST01 »."""
        prefix = jobs.merge_rule_key("TEST0", "")
        assert not jobs.merge_rule_key("TEST01", "COCA").startswith(prefix)


@pytest.mark.asyncio
async def test_un_run_tombe_sur_une_erreur_de_base_finit_quand_meme_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La panne exacte de production : l'etape qui interroge la base fait
    avorter la transaction, et le filet doit ecrire FAILED malgre tout."""
    await create_all()

    source = _TMP / "TEST01_depot.csv"
    source.write_bytes(SAMPLE.read_bytes())

    async with SessionFactory() as session:
        session.add(Store(id="TEST01", name="TEST01"))
        run = IngestionRun(
            id="run-prod-35b44a78",
            store_id="TEST01",
            file_name=str(source),
            file_sha256="c" * 64,
            file_size=source.stat().st_size,
            status="PENDING",
            network_enabled=False,
        )
        session.add(run)
        await session.commit()
        await jobs.prepare_steps(session, "run-prod-35b44a78")

    async def learned_merges_qui_avorte(session: Any, store_id: str) -> dict[str, str]:
        # Une requete invalide laisse la session en transaction avortee :
        # c'est l'etat exact dans lequel Postgres a laisse le filet.
        await session.execute(text("SELECT rien FROM table_inexistante"))
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr(jobs, "_learned_merges", learned_merges_qui_avorte)

    await jobs.execute_run("run-prod-35b44a78")

    async with SessionFactory() as session:
        failed = await session.get(IngestionRun, "run-prod-35b44a78")
        assert failed is not None
        assert failed.status == "FAILED", "le filet doit survivre a sa propre transaction"
        assert failed.error is not None
        assert failed.finished_at is not None
