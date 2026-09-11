"""CLI du pipeline.

    pipeline run data/samples/store_listing_produit.csv --store-id demo

`--no-network` desactive les verifications HTTP : indispensable en CI, ou l'on
veut tester la logique sans dependre d'un service tiers.

C'est aussi le chemin du timer systemd de 5 h 30. A ce titre il consomme la
MEME enveloppe LLM que les depots faits depuis l'interface : un plafond
« cumule sur toute la demonstration » qui ne compterait que la moitie des runs
ne serait pas un plafond. Voir `_run_with_budget`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from pipeline.config import PipelineConfig, Settings
from pipeline.context import RunContext
from pipeline.llm.client import LlmClient
from pipeline.logging import configure_logging, get_logger
from pipeline.runner import RunOutcome, run_pipeline
from pipeline.steps import llm_enrich

log = get_logger(__name__)

app = typer.Typer(help="Pipeline de fiabilisation de catalogues produits", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Sans ce callback, Typer replie une application a commande unique sur sa
    seule commande, et `pipeline run <fichier>` devient `pipeline <fichier>`.
    Le garder fige l'interface : d'autres sous-commandes arriveront (`report`,
    `replay`) sans casser les scripts existants."""


@app.command()
def run(
    file: Annotated[Path, typer.Argument(help="CSV a traiter")],
    store_id: Annotated[str, typer.Option("--store-id", help="Identifiant du magasin")] = "demo",
    no_network: Annotated[
        bool, typer.Option("--no-network", help="Ne pas verifier les URLs par HTTP")
    ] = False,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Ecrit le rapport JSON dans ce fichier")
    ] = None,
    pretty: Annotated[bool, typer.Option("--pretty", help="Logs lisibles plutot que JSON")] = False,
) -> None:
    """Traite un fichier et produit un rapport de qualite."""
    if not file.exists():
        typer.secho(f"fichier introuvable : {file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    settings = Settings()
    configure_logging(settings.log_level, json_output=not pretty)

    ctx = RunContext(
        store_id=store_id,
        source_file=file,
        config=PipelineConfig.load(),
        network_enabled=not no_network,
    )
    # Sans cle, l'etage LLM est inerte : inutile d'ouvrir une base pour
    # constater qu'on ne depensera rien. C'est le cas du CI et des runs
    # hors-ligne, qui gardent exactement le comportement d'avant.
    outcome = run_pipeline(ctx) if not ctx.config.llm.api_key else _run_with_budget(ctx)

    payload = outcome.report.model_dump(mode="json")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        typer.secho(f"rapport ecrit : {output}", fg=typer.colors.GREEN, err=True)
    else:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")

    # Un run en quarantaine sort en erreur : le systemd timer et le CI doivent
    # le voir comme un echec, pas comme un succes silencieux.
    if outcome.report.quarantined:
        raise typer.Exit(code=1)


def _run_with_budget(ctx: RunContext) -> RunOutcome:
    """Execute le run en le rattachant a l'enveloppe cumulee et au cache en base.

    Trois raisons de passer par la base plutot que de laisser l'etage LLM se
    debrouiller seul :

    1. **Le plafond doit etre commun.** Le timer de nuit et les depots faits
       dans l'interface tapent dans la meme cle d'API et la meme facture. Un
       run CLI qui ignorait le cumul pouvait depenser son propre plafond
       chaque nuit pendant que le tableau de bord affichait une enveloppe
       intacte.
    2. **La depense doit etre inscrite**, y compris quand le run finit en
       erreur : les appels sont factures avant que le pipeline ne tombe.
    3. **Le cache doit survivre au conteneur.** Le run de nuit tourne dans un
       conteneur ephemere : avec un cache de session, chaque nuit repayait les
       memes 273 libelles.

    Si la base est injoignable, on ne devine pas ce qui reste : l'etage LLM est
    coupe et le pipeline continue sans lui. Degradation visible plutot que
    depense a l'aveugle — la meme regle que lorsque l'enveloppe est epuisee.
    """

    async def _main() -> RunOutcome:
        from api import budget as budget_mod
        from api import llm_cache as llm_cache_mod
        from api import url_cache as url_cache_mod
        from db.session import SessionFactory, engine

        settings = llm_enrich.build_settings(ctx)
        try:
            async with SessionFactory() as session:
                try:
                    # `reserve` lit ce qui reste ET reclame ce plafond dans la
                    # meme section critique : un depot fait depuis l'interface
                    # pendant que le timer tourne ne lit pas le meme reliquat.
                    # Ne protege que dans CE process (voir le commentaire sur
                    # `_reserved_by_run` dans `api/budget.py`) : la CLI et
                    # l'API sont deux processus separes, donc deux memoires
                    # separees — le vrai filet reste le plafond cumule verifie
                    # a l'ecriture, ceci reduit juste la fenetre de course.
                    ceiling = await budget_mod.reserve(session, ctx.run_id, settings.max_cost_usd)
                    cache = await llm_cache_mod.load(session, settings.model)
                    if ctx.network_enabled:
                        # Meme graine que les depots de l'interface : le run
                        # de nuit ne reverifie pas les URLs d'hier matin.
                        ctx.url_status_seed = await url_cache_mod.load(
                            session, ctx.config.http.cache_ttl_hours
                        )
                except Exception as exc:
                    # Base injoignable, ou schema pas encore migre : on ne sait
                    # pas ce qui reste de l'enveloppe, donc on ne depense pas.
                    log.error("llm.budget_unavailable", error=str(exc))
                    settings.api_key = ""
                    return await asyncio.to_thread(run_pipeline, ctx, LlmClient(settings))

                settings.max_cost_usd = ceiling
                if ceiling <= 0:
                    settings.api_key = ""
                    state = await budget_mod.read_budget(session)
                    log.warning(
                        "llm.budget_exhausted",
                        limit_usd=state.limit_usd,
                        spent_usd=round(state.spent_usd, 4),
                    )
                client = LlmClient(settings, cache)
                try:
                    return await asyncio.to_thread(run_pipeline, ctx, client)
                finally:
                    # Solde meme si le run tombe : les appels deja passes ont
                    # ete factures, et les reponses obtenues valent d'etre
                    # gardees plutot que rachetees demain.
                    await budget_mod.record_spend(session, ctx.run_id, ctx.store_id, client.usage)
                    await llm_cache_mod.persist(session, cache)
                    if ctx.url_status_fresh:
                        await url_cache_mod.persist(session, ctx.url_status_fresh)
                    await session.commit()
                    budget_mod.release(ctx.run_id)
        finally:
            # Le processus s'arrete apres ce run : laisser la pool ouverte
            # ferait attendre asyncio.run a la sortie.
            await engine.dispose()

    return asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    app()
