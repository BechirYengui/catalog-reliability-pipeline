"""L'enveloppe ne vaut que si elle voit TOUT ce qui est depense.

Deux trous mesures dans le code, tous les deux du meme genre : de l'argent
reellement facture par l'API n'arrivait jamais jusqu'au compteur affiche.

1. Un run qui plante apres l'etage LLM ne soldait rien. Les appels etaient
   payes, la ligne de depense n'etait ecrite que sur le chemin du succes, et le
   tableau de bord annoncait une enveloppe plus large que la realite. Le cache
   partait avec, donc le run suivant rachetait les memes libelles.
2. Le run du timer de nuit passe par la CLI, qui ne consultait pas le cumul et
   n'inscrivait pas sa depense. « Plafond cumule sur toute la demonstration »
   ne comptait en fait que les depots faits depuis l'interface.

Les deux tests ci-dessous partent d'une base sqlite jetable : ils verifient ce
qui est ECRIT, pas ce que le code a l'air de faire.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from pipeline.context import RunContext
from pipeline.models import StepResult

SAMPLE = Path(__file__).parent / "data" / "sample_anomalies.csv"

# 100 000 jetons d'entree sur Opus 5 (5 $ / Mjetons) = 0,50 $. Un chiffre rond
# choisi pour que l'assertion porte sur le montant, pas sur un arrondi.
SPENT_TOKENS = 100_000
SPENT_USD = 0.50


def _purge_api_modules() -> None:
    """Purge `api` et `db`, PAQUETS COMPRIS.

    Oublier les paquets ne se voit pas tout de suite : `api/main.py` fait
    `from api import jobs`, Python trouve l'attribut `jobs` encore accroche a
    l'ancien objet paquet et rend le module precedent — donc son moteur, donc
    la base d'un autre test. La requete HTTP repond juste, et la tache de fond
    cherche ses tables ailleurs.
    """
    for name in [
        m for m in list(sys.modules) if m in ("api", "db") or m.startswith(("api.", "db."))
    ]:
        del sys.modules[name]


def _fresh_db(tmp_path: Path) -> None:
    """Base jetable. Les modules `api.*`/`db.*` sont recharges : le moteur est
    cree au chargement du module, donc il doit suivre la variable."""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}"
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ.pop("LLM_BUDGET_USD", None)
    _purge_api_modules()


def _in_own_loop(work: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """Execute une coroutine dans une boucle a elle, pool refermee derriere.

    `_run_with_budget` appelle lui-meme `asyncio.run` — c'est un point d'entree
    CLI, pas une brique asynchrone. Les tests qui l'exercent sont donc
    synchrones, et la preparation de la base passe par ici.
    """

    async def _wrap() -> Any:
        from db.session import engine

        try:
            return await work()
        finally:
            await engine.dispose()

    return asyncio.run(_wrap())


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Any]:
    _fresh_db(tmp_path)

    async def _setup() -> Any:
        from db.session import migrate

        await migrate()

    _in_own_loop(_setup)
    from db.session import SessionFactory

    yield SessionFactory

    # Les modules sont recharges par le test suivant qui en a besoin ; on ne
    # laisse pas derriere nous un moteur pointant sur une base effacee.
    _purge_api_modules()
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("DATA_DIR", None)


def _spending_step(cache_key: str = "libelle-deja-paye") -> Any:
    """Etage LLM simule : il consomme du vrai budget et remplit le cache."""

    def run(df: pl.DataFrame, ctx: RunContext, client: Any = None) -> StepResult:
        client.usage.calls_real = 1
        client.usage.input_tokens = SPENT_TOKENS
        client.cache.set(
            cache_key, client.settings.model, "COC COL ZER 1L", {"label": "Cola"}, 0, 0
        )
        return StepResult(df=df)

    return SimpleNamespace(run=run)


def _exploding_step() -> Any:
    def run(df: pl.DataFrame, ctx: RunContext, *args: Any) -> StepResult:
        raise RuntimeError("dedup a explose apres que le LLM a ete facture")

    return SimpleNamespace(run=run)


class TestConcurrentReservationsDoNotOverspend:
    """Deux depots geres par la MEME API, presque au meme instant.

    Avant `reserve`, chaque run lisait le reliquat via `read_budget` AVANT que
    l'autre n'ait rien inscrit : les deux se croyaient chacun autorises a
    depenser jusqu'a ce reliquat. Sur une enveloppe de 5 $, deux runs
    demandant chacun 4 $ pouvaient a eux deux reclamer 8 $ — le depassement
    n'etait borne que par le nombre de runs simultanes, pas par l'enveloppe.
    """

    @pytest.mark.asyncio
    async def test_deux_reservations_concurrentes_ne_depassent_pas_lenveloppe(
        self, db: Any
    ) -> None:
        from api import budget as budget_mod

        async def _reserve(run_id: str) -> float:
            async with db() as session:
                return await budget_mod.reserve(session, run_id, configured_max_usd=4.0)

        try:
            ceiling_a, ceiling_b = await asyncio.gather(_reserve("run-a"), _reserve("run-b"))
        finally:
            budget_mod.release("run-a")
            budget_mod.release("run-b")

        # La somme des deux plafonds ne depasse jamais l'enveloppe de 5 $,
        # quel que soit l'ordre dans lequel les deux runs ont ete servis.
        assert ceiling_a + ceiling_b <= 5.0 + 1e-9
        # L'un des deux obtient son plafond demande en entier, l'autre ce
        # qu'il en restait : 4 $ et 1 $, dans un ordre ou l'autre.
        assert sorted([round(ceiling_a, 4), round(ceiling_b, 4)]) == [1.0, 4.0]

    @pytest.mark.asyncio
    async def test_une_reservation_relachee_rend_la_place_au_run_suivant(self, db: Any) -> None:
        """`release` doit etre appele quoi qu'il arrive : un run termine ne
        doit plus amputer le reliquat pour le suivant."""
        from api import budget as budget_mod

        async with db() as session:
            first = await budget_mod.reserve(session, "run-a", configured_max_usd=5.0)
        assert round(first, 4) == 5.0

        async with db() as session:
            blocked = await budget_mod.reserve(session, "run-b", configured_max_usd=5.0)
        assert blocked == 0.0, "run-a tient encore toute l'enveloppe"

        budget_mod.release("run-a")

        async with db() as session:
            freed = await budget_mod.reserve(session, "run-c", configured_max_usd=5.0)
        assert round(freed, 4) == 5.0

        budget_mod.release("run-c")


class TestSpendSurvivesAFailedRun:
    """Un run qui tombe APRES l'etage LLM a quand meme ete facture."""

    @pytest.mark.asyncio
    async def test_a_failed_run_still_charges_the_envelope(
        self, tmp_path: Path, db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from api import jobs
        from api.budget import read_budget
        from db.models import IngestionRun, LlmCache, Store

        source = tmp_path / "demo.csv"
        source.write_bytes(SAMPLE.read_bytes())

        async with db() as session:
            session.add(Store(id="demo", name="demo"))
            run = IngestionRun(
                store_id="demo",
                file_name=str(source),
                file_sha256="a" * 64,
                file_size=source.stat().st_size,
                status="PENDING",
                network_enabled=False,
            )
            session.add(run)
            await session.commit()
            run_id = run.id
            await jobs.prepare_steps(session, run_id)

        # L'etage LLM depense, l'etape suivante plante : exactement la sequence
        # qui perdait la depense.
        monkeypatch.setattr(
            jobs,
            "EXECUTABLE_STEPS",
            [("llm_enrich", _spending_step()), ("dedup_exact", _exploding_step())],
        )

        await jobs.execute_run(run_id)

        async with db() as session:
            failed = await session.get(IngestionRun, run_id)
            assert failed is not None
            assert failed.status == "FAILED"

            state = await read_budget(session)
            assert round(state.spent_usd, 4) == SPENT_USD
            assert state.runs_charged == 1
            # 5 $ d'enveloppe, 0,50 $ brules par un run rate.
            assert round(state.remaining_usd, 4) == 4.50

            # Le cache aussi survit : les reponses obtenues sont payees, les
            # racheter au prochain depot serait payer deux fois la meme chose.
            cached = await session.get(LlmCache, "libelle-deja-paye")
            assert cached is not None


class TestTheNightlyRunSharesTheEnvelope:
    """Le timer de 5 h 30 passe par la CLI, pas par l'API.

    Il tape dans la meme cle Anthropic et la meme facture : s'il ne consultait
    pas le cumul, le tableau de bord afficherait une enveloppe intacte pendant
    que la carte est debitee toutes les nuits.
    """

    @pytest.fixture
    def ctx_with_key(self, config: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[RunContext]:
        ctx = RunContext(
            store_id="demo",
            source_file=SAMPLE,
            config=config.model_copy(deep=True),
            network_enabled=False,
        )
        ctx.config.llm.api_key = "sk-test"
        yield ctx

    def test_the_cli_run_is_capped_by_what_remains_and_records_it(
        self, tmp_path: Path, db: Any, ctx_with_key: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from api.budget import read_budget
        from db.models import LlmSpend
        from pipeline import cli

        async def _seed() -> None:
            # 4,30 $ deja consommes par des depots faits dans l'interface.
            async with db() as session:
                session.add(
                    LlmSpend(
                        run_id="run-precedent",
                        store_id="demo",
                        model="claude-opus-5",
                        calls_real=9,
                        cost_usd=4.30,
                    )
                )
                await session.commit()

        _in_own_loop(_seed)

        seen: dict[str, Any] = {}

        def fake_pipeline(ctx: RunContext, client: Any = None) -> Any:
            seen["ceiling"] = client.settings.max_cost_usd
            seen["key"] = client.settings.api_key
            client.usage.calls_real = 1
            client.usage.input_tokens = SPENT_TOKENS // 10  # 0,05 $
            return SimpleNamespace(report=None)

        monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
        cli._run_with_budget(ctx_with_key)

        # Le plafond du run est le reliquat, pas les 5 $ de la configuration.
        assert round(seen["ceiling"], 4) == 0.70
        assert seen["key"] == "sk-test"

        async def _check() -> Any:
            async with db() as session:
                return await read_budget(session)

        state = _in_own_loop(_check)
        assert round(state.spent_usd, 4) == 4.35
        assert state.runs_charged == 2

    def test_an_exhausted_envelope_cuts_the_key_of_the_nightly_run(
        self, tmp_path: Path, db: Any, ctx_with_key: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from db.models import LlmSpend
        from pipeline import cli

        async def _seed() -> None:
            async with db() as session:
                session.add(
                    LlmSpend(
                        run_id="run-precedent",
                        store_id="demo",
                        model="claude-opus-5",
                        calls_real=40,
                        cost_usd=5.0,
                    )
                )
                await session.commit()

        _in_own_loop(_seed)

        seen: dict[str, Any] = {}

        def fake_pipeline(ctx: RunContext, client: Any = None) -> Any:
            seen["key"] = client.settings.api_key
            return SimpleNamespace(report=None)

        monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
        cli._run_with_budget(ctx_with_key)

        # Cle retiree : l'etage se saute et le reste du pipeline continue.
        assert seen["key"] == ""

    def test_an_unreachable_database_does_not_spend_blind(
        self, tmp_path: Path, ctx_with_key: RunContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sans base, on ne sait pas ce qui reste. On ne devine pas : on coupe."""
        _fresh_db(tmp_path)
        os.environ["DATABASE_URL"] = "postgresql+asyncpg://nobody@127.0.0.1:1/absent"
        for name in [m for m in list(sys.modules) if m.startswith(("api.", "db."))]:
            del sys.modules[name]

        from pipeline import cli

        seen: dict[str, Any] = {}

        def fake_pipeline(ctx: RunContext, client: Any = None) -> Any:
            seen["key"] = client.settings.api_key
            return SimpleNamespace(report=None)

        monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
        cli._run_with_budget(ctx_with_key)

        assert seen["key"] == ""

    def test_the_cli_entry_point_goes_through_the_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Le branchement lui-meme est teste : sans lui, tout le reste de ce
        fichier passerait encore alors que le timer de nuit depenserait hors
        de l'enveloppe."""
        from pipeline import cli

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        outcome = SimpleNamespace(
            report=SimpleNamespace(model_dump=lambda mode: {}, quarantined=False)
        )
        chemin: list[str] = []
        monkeypatch.setattr(
            cli, "_run_with_budget", lambda ctx: (chemin.append("enveloppe"), outcome)[1]
        )
        monkeypatch.setattr(
            cli, "run_pipeline", lambda ctx, client=None: (chemin.append("direct"), outcome)[1]
        )

        cli.run(file=SAMPLE, store_id="demo", no_network=True, output=tmp_path / "rapport.json")
        assert chemin == ["enveloppe"]

        # Sans cle, rien ne sera depense : inutile d'ouvrir une base pour le
        # constater. C'est le cas du CI et des runs hors-ligne.
        chemin.clear()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cli.run(file=SAMPLE, store_id="demo", no_network=True, output=tmp_path / "rapport.json")
        assert chemin == ["direct"]
