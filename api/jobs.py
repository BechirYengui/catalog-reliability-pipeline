"""Execution d'un run en tache de fond, etape par etape.

Contrairement a la CLI, qui enchaine tout d'une traite, ce module persiste
l'etat APRES CHAQUE ETAPE. Deux raisons :

1. l'interface doit pouvoir repondre « ou en est mon fichier » a tout instant,
   et montrer les resultats intermediaires de chaque etage ;
2. apres une panne, on sait exactement a quelle etape le traitement s'est
   arrete. La reprise automatique a l'etape suivante n'est PAS implementee :
   un run interrompu est relance depuis le debut. L'etat persiste rend cette
   reprise simple a ajouter, mais tant qu'elle ne l'est pas, ce commentaire ne
   doit pas laisser croire le contraire.

Les etapes du pipeline sont synchrones et gourmandes en CPU (Polars). Elles
tournent donc dans un thread separe : les executer dans la boucle asyncio
figerait l'API pendant les 4 minutes d'un run avec verification des URLs.
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select

from api import budget as budget_mod
from api import events
from api import llm_cache as llm_cache_mod
from api import url_cache as url_cache_mod
from db.models import (
    IngestionRun,
    LearnedRule,
    Product,
    ProductCorrection,
    ProductOverride,
    ProductSourceRow,
    ReviewTask,
    RunAnomaly,
    RunStep,
)
from db.session import SessionFactory
from pipeline.config import PipelineConfig
from pipeline.context import RunContext
from pipeline.llm.client import LlmClient
from pipeline.logging import bind_run, clear_run, get_logger
from pipeline.models import Anomaly, Correction
from pipeline.steps import (
    cross_checks,
    dedup,
    entity_resolution,
    field_checks,
    ingest,
    llm_enrich,
    report,
    scoring_routing,
)

log = get_logger(__name__)

# Etapes reellement executables aujourd'hui, dans l'ordre. Les autres etapes du
# pipeline (quasi-doublons, LLM) apparaissent dans l'interface avec le statut
# PENDING : l'utilisateur doit voir la chaine complete, pas seulement ce qui
# tourne. Les ajouter ici suffira a les activer.
EXECUTABLE_STEPS = [
    ("ingest", ingest),
    ("field_checks", field_checks),
    ("cross_checks", cross_checks),
    ("llm_enrich", llm_enrich),
    ("dedup_exact", dedup),
    ("entity_resolution", entity_resolution),
    ("scoring_routing", scoring_routing),
]

ALL_STEPS = [
    "ingest",
    "field_checks",
    "cross_checks",
    "llm_enrich",
    "dedup_exact",
    "entity_resolution",
    "scoring_routing",
    "report",
]


def _now() -> datetime:
    return datetime.now(UTC)


async def prepare_steps(session: Any, run_id: str) -> None:
    """Cree la liste complete des etapes en PENDING avant tout traitement.

    L'interface affiche ainsi le pipeline entier des la creation du run, et non
    une liste qui se remplit au fur et a mesure.
    """
    await session.execute(delete(RunStep).where(RunStep.run_id == run_id))
    executable = {name for name, _ in EXECUTABLE_STEPS} | {"report"}
    for position, step in enumerate(ALL_STEPS):
        session.add(
            RunStep(
                run_id=run_id,
                step=step,
                position=position,
                status="PENDING" if step in executable else "SKIPPED",
            )
        )
    await session.commit()


async def fail_orphan_runs() -> int:
    """Marque FAILED les runs que plus aucun processus n'execute.

    La tache de fond vit DANS le processus de l'API : un redemarrage — un
    deploiement, un crash, un kill — l'emporte avec lui. Le filet d'exception
    d'`execute_run` ne peut rien attraper dans ce cas, il est mort aussi. Sans
    ce balayage, le run resterait « RUNNING » a jamais dans l'interface, sans
    erreur, sans fin, et sans moyen d'en redeposer un identique (le depot
    refuse un fichier deja en cours).

    Appele au demarrage : a cet instant, par construction, aucun run ne peut
    legitimement etre en cours dans CE processus. Tout RUNNING trouve la est
    orphelin.
    """
    async with SessionFactory() as session:
        result = await session.execute(select(IngestionRun).where(IngestionRun.status == "RUNNING"))
        orphans = list(result.scalars())
        for run in orphans:
            run.status = "FAILED"
            run.error = (
                "interrompu : le serveur s'est arrêté pendant le traitement "
                "(redéploiement ou panne). Redéposer le fichier pour relancer."
            )
            run.finished_at = _now()
            if run.current_step:
                step = await _step(session, run.id, run.current_step)
                step.status = "FAILED"
                step.error = "le processus qui exécutait cette étape s'est arrêté"
                step.finished_at = _now()
            log.warning("job.orphan_failed", run_id=run.id, step=run.current_step)
        if orphans:
            await session.commit()
        return len(orphans)


async def execute_run(run_id: str) -> None:
    """Point d'entree de la tache de fond."""
    async with SessionFactory() as session:
        run = await session.get(IngestionRun, run_id)
        if run is None:
            log.error("job.run_not_found", run_id=run_id)
            return

        bind_run(run.id, run.store_id)
        source = Path(run.file_name)
        run.status = "RUNNING"
        run.started_at = _now()
        await session.commit()
        events.publish(run_id, {"type": "run.started", "run_id": run_id, "status": "RUNNING"})

        ctx = RunContext(
            store_id=run.store_id,
            source_file=source,
            config=PipelineConfig.load(),
            run_id=run.id,
            file_sha256=run.file_sha256,
            network_enabled=run.network_enabled,
        )
        if run.network_enabled:
            # Les statuts d'URL des runs precedents : un depot quotidien ne
            # verifie que les URLs nouvelles, pas les 9 000 memes images.
            ctx.url_status_seed = await url_cache_mod.load(session, ctx.config.http.cache_ttl_hours)

        # --- enveloppe de depense ----------------------------------------
        # Le plafond du run n'est jamais superieur a ce qui reste : c'est le
        # cumul qui protege, pas le plafond par run. `reserve` lit ce qui
        # reste ET reclame ce plafond dans la meme section critique, pour
        # qu'un second depot presque simultane ne lise pas le meme reliquat.
        llm_settings = llm_enrich.build_settings(ctx, batch_api=run.llm_mode == "batch")
        ceiling = await budget_mod.reserve(session, run.id, llm_settings.max_cost_usd)
        llm_settings.max_cost_usd = ceiling
        if ceiling <= 0:
            # Enveloppe epuisee (par ce run ou par un autre deja en vol) : on
            # coupe l'API en retirant la cle. L'etage se saute et le pipeline
            # continue, comme si aucune cle n'etait configuree — degradation
            # visible et reversible plutot qu'une facture inattendue.
            llm_settings.api_key = ""
            state = await budget_mod.read_budget(session)
            log.warning(
                "llm.budget_exhausted",
                limit_usd=state.limit_usd,
                spent_usd=round(state.spent_usd, 4),
            )
        cache = await llm_cache_mod.load(session, llm_settings.model)
        llm_client = LlmClient(
            llm_settings,
            cache,
            progress=lambda kind, data: events.publish(run_id, {"type": kind, **data}),
        )

        corrections: list[Correction] = []
        anomalies: list[Anomaly] = []
        metrics_list: list[Any] = []
        golden_records: list[Any] = []
        unresolved_pairs: list[Any] = []
        llm_usage: dict[str, Any] = {}
        # L'enveloppe se solde une fois, quel que soit le chemin de sortie.
        settled: set[str] = set()

        try:
            df = await asyncio.to_thread(ingest.read_csv, source)
            run.rows_in = df.height
            await session.commit()

            total = len(EXECUTABLE_STEPS) + 1  # +1 pour le rapport
            for index, (name, module) in enumerate(EXECUTABLE_STEPS):
                step_row = await _step(session, run_id, name)
                step_row.status = "RUNNING"
                step_row.started_at = _now()
                run.current_step = name
                run.progress = index / total
                await session.commit()
                events.publish(
                    run_id,
                    {"type": "step.started", "step": name, "progress": run.progress},
                )

                # Polars est synchrone et sature un cœur : hors de la boucle asyncio.
                if name == "llm_enrich":
                    result = await asyncio.to_thread(module.run, df, ctx, llm_client)
                elif name == "entity_resolution":
                    # Cette etape travaille sur les golden records, pas sur les
                    # lignes : sa cardinalite est celle des fiches. On lui passe
                    # les fusions deja tranchees par un humain, pour qu'une
                    # decision d'hier n'ait pas a etre reprise ce matin.
                    forced = await _learned_merges(session, run.store_id)
                    result = await asyncio.to_thread(module.run, df, ctx, golden_records, forced)
                elif name == "scoring_routing":
                    result = await asyncio.to_thread(module.run, df, ctx, golden_records)
                else:
                    result = await asyncio.to_thread(module.run, df, ctx)
                df = result.df
                corrections.extend(result.corrections)
                anomalies.extend(result.anomalies)
                golden_records = result.payload.get("golden_records", golden_records)
                unresolved_pairs = result.payload.get("unresolved_pairs", unresolved_pairs)
                llm_usage = result.payload.get("llm_usage", llm_usage) or llm_usage
                if result.metrics:
                    metrics_list.append(result.metrics)

                if name == "field_checks" and ctx.url_status_fresh:
                    # Persiste DES la fin de l'etape, pas a la fin du run : un
                    # run qui tombe plus loin a quand meme paye ces requetes,
                    # et le depot suivant doit en heriter.
                    await url_cache_mod.persist(session, ctx.url_status_fresh)

                step_row.status = "DONE"
                step_row.finished_at = _now()
                step_row.rows_in = result.metrics.rows_in if result.metrics else 0
                step_row.rows_out = result.metrics.rows_out if result.metrics else 0
                step_row.duration_ms = result.metrics.duration_ms if result.metrics else 0.0
                step_row.corrections = len(result.corrections)
                step_row.anomalies = len(result.anomalies)
                step_row.counters = dict(result.metrics.counters) if result.metrics else {}
                run.progress = (index + 1) / total
                await session.commit()
                events.publish(
                    run_id,
                    {
                        "type": "step.done",
                        "step": name,
                        "progress": run.progress,
                        "rows_in": step_row.rows_in,
                        "rows_out": step_row.rows_out,
                        "duration_ms": step_row.duration_ms,
                        "corrections": step_row.corrections,
                        "anomalies": step_row.anomalies,
                        "counters": step_row.counters,
                    },
                )

            # --- rapport -----------------------------------------------------
            step_row = await _step(session, run_id, "report")
            step_row.status = "RUNNING"
            step_row.started_at = _now()
            run.current_step = "report"
            await session.commit()

            run_report = report.build(
                df, ctx, corrections, anomalies, metrics_list, run.rows_in, golden_records
            )

            await _persist_results(
                session,
                run,
                corrections,
                anomalies,
                golden_records,
                unresolved_pairs,
                quarantined=run_report.quarantined,
            )
            await _settle_llm(session, run, llm_client, cache, settled)

            step_row.status = "DONE"
            step_row.finished_at = _now()
            step_row.rows_in = df.height
            step_row.rows_out = len(golden_records)
            run.rows_out = len(golden_records)
            run.publishable_rate = run_report.publishable_rate
            report_payload = run_report.model_dump(mode="json")
            report_payload["llm_usage"] = llm_usage
            report_payload["llm_budget"] = (await budget_mod.read_budget(session)).to_dict()
            run.report = report_payload
            run.status = "QUARANTINE" if run_report.quarantined else "COMPLETED"
            run.current_step = None
            run.progress = 1.0
            run.finished_at = _now()
            await session.commit()

            events.publish(
                run_id,
                {
                    "type": "run.finished",
                    "status": run.status,
                    "progress": 1.0,
                    "publishable_rate": run.publishable_rate,
                    "golden_records": len(golden_records),
                },
            )
            log.info("job.done", status=run.status, golden_records=len(golden_records))

        except Exception as exc:
            # Sans ce filet, un run plante resterait eternellement « RUNNING »
            # dans l'interface, sans indication de ce qui s'est passe.
            log.error("job.failed", error=str(exc), traceback=traceback.format_exc())
            # Rollback D'ABORD. Si c'est la BASE qui a fait tomber le run, la
            # transaction est avortee : ecrire FAILED dedans echoue aussi, le
            # filet meurt en silence et le run reste « RUNNING » a jamais —
            # constate en production sur le run 35b44a78 (cle NUL refusee par
            # Postgres, puis commit du filet refuse par la meme transaction).
            # Le rollback DEFAIT les attributs poses avant lui : les statuts
            # se posent apres, pas avant.
            await session.rollback()
            # Le rollback EXPIRE l'objet : le premier acces d'attribut qui
            # suivrait declencherait un rechargement SQL synchrone — interdit
            # dans une session async (MissingGreenlet), et le filet mourrait
            # une seconde fois. On recharge explicitement, en async.
            await session.refresh(run)
            run.status = "FAILED"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = _now()
            if run.current_step:
                failed = await _step(session, run_id, run.current_step)
                failed.status = "FAILED"
                failed.error = str(exc)
                failed.finished_at = _now()
            await session.commit()
            # Un run qui plante APRES avoir appele l'API a quand meme ete
            # facture. Ne rien enregistrer laisserait l'enveloppe croire que
            # cet argent est encore disponible, et le cache ferait repayer au
            # run suivant des libelles deja obtenus.
            await _settle_llm(session, run, llm_client, cache, settled)
            events.publish(run_id, {"type": "run.failed", "status": "FAILED", "error": run.error})
        finally:
            # La reservation n'a de sens que pendant que CE run est en vol :
            # l'oublier amputerait le reliquat pour rien jusqu'au redemarrage
            # du process, meme apres un succes.
            budget_mod.release(run.id)
            clear_run()


async def _settle_llm(
    session: Any,
    run: IngestionRun,
    client: LlmClient,
    cache: llm_cache_mod.PreloadedCache,
    settled: set[str],
) -> None:
    """Solde ce que le run a consomme : depense inscrite, cache ecrit.

    Appele sur les DEUX sorties, succes comme echec. Un run qui tombe a
    l'etape de dedup a deja paye ses appels LLM : les oublier ferait afficher
    une enveloppe plus large que la realite, ce qui est exactement l'erreur
    contre laquelle ce garde-fou existe.

    Son propre echec ne doit jamais masquer celui du run : la comptabilite est
    importante, pas au point d'effacer la cause d'une panne.
    """
    if run.id in settled:
        return
    settled.add(run.id)
    try:
        await budget_mod.record_spend(session, run.id, run.store_id, client.usage)
        await llm_cache_mod.persist(session, cache)
        await session.commit()
    except Exception as exc:  # pragma: no cover - filet de securite
        log.error("llm.settle_failed", run_id=run.id, error=str(exc))
        await session.rollback()


def merge_rule_key(store_id: str, label_normalized: str) -> str:
    """La cle d'une fusion apprise : elle appartient a UN magasin.

    L'API refuse de fusionner deux fiches de magasins differents — appliquer
    ensuite la regle a toutes les enseignes dirait l'inverse du meme geste. Et
    deux magasins n'ecrivent pas leur caisse pareil : « COC COL ZER 1L » chez
    l'un peut designer autre chose que chez l'autre.

    Separateur : \\x1f (unit separator ASCII), et surtout PAS \\x00. PostgreSQL
    interdit l'octet NUL dans un TEXT : la premiere requete de production a
    craque dessus (`CharacterNotInRepertoireError`, run 35b44a78 du
    2026-08-11), alors que sqlite — donc toute la suite de tests — l'avale
    sans un mot. \\x1f est valide en Postgres, et ne peut apparaitre ni dans
    un identifiant magasin (prefixe de nom de fichier) ni dans un libelle
    normalise (`normalize_label` reduit tout non-alphanumerique a l'espace).
    Aucune regle heritee a migrer : une ECRITURE avec \\x00 aurait echoue de
    la meme facon, la table ne peut pas en contenir.
    """
    return f"{store_id}\x1f{label_normalized}"


async def _learned_merges(session: Any, store_id: str) -> dict[str, str]:
    """Les fusions qu'un humain a deja tranchees : absorbe -> survivant.

    C'est la boucle d'apprentissage sur les quasi-doublons. Sans elle, une
    fusion decidee dans l'interface serait defaite par le depot du lendemain,
    et l'utilisateur referait chaque matin le meme geste — la meilleure facon
    de lui faire abandonner l'outil.

    Les regles sont cloisonnees par magasin, comme le referentiel : une fusion
    decidee chez Franprix ne doit pas regrouper en silence les fiches
    d'Intermarche, alors meme que l'API refuse de fusionner deux fiches de
    magasins differents.
    """
    prefix = merge_rule_key(store_id, "")
    result = await session.execute(
        select(LearnedRule).where(
            LearnedRule.scope == "same_product", LearnedRule.key.startswith(prefix)
        )
    )
    return {rule.key.removeprefix(prefix): rule.value for rule in result.scalars().all()}


async def _step(session: Any, run_id: str, name: str) -> RunStep:
    result = await session.execute(
        select(RunStep).where(RunStep.run_id == run_id, RunStep.step == name)
    )
    row: RunStep | None = result.scalar_one_or_none()
    if row is None:
        row = RunStep(run_id=run_id, step=name, position=ALL_STEPS.index(name))
        session.add(row)
        await session.flush()
    return row


async def _persist_results(
    session: Any,
    run: IngestionRun,
    corrections: list[Correction],
    anomalies: list[Anomaly],
    golden_records: list[Any],
    unresolved_pairs: list[Any] | None = None,
    quarantined: bool = False,
) -> None:
    """Ecrit le referentiel, l'audit trail et les taches de revue.

    Idempotence : on efface d'abord ce que CE run avait produit. Rejouer le
    meme fichier remplace ses resultats au lieu de les empiler.

    QUARANTAINE : le referentiel n'est PAS touche, et aucune tache de revue
    n'est creee. L'interface annonce « rien n'a ete integre » et le garde-fou
    n'a de valeur que s'il dit vrai — il ecrivait pourtant ses fiches comme un
    run normal, ce qui a depose « CAFE CREME FRAICHE 30 20CL » et un libelle
    vide dans le catalogue d'un magasin. Les anomalies et les corrections, en
    revanche, restent : ce sont elles qui expliquent POURQUOI le fichier a ete
    ecarte, et les jeter rendrait la quarantaine indiagnosticable.
    """
    for table in (ProductCorrection, RunAnomaly, ReviewTask):
        await session.execute(delete(table).where(table.run_id == run.id))

    session.add_all(
        ProductCorrection(
            run_id=c.run_id,
            row_id=c.row_id,
            field_name=c.field_name,
            old_value=c.old_value,
            new_value=c.new_value,
            author=c.author.value,
            rule=c.rule,
            confidence=c.confidence,
            created_at=c.created_at,
        )
        for c in corrections
    )

    # Les anomalies sont bornees : 4 000 lignes par run suffisent a diagnostiquer,
    # et le rapport porte de toute facon le decompte complet par code.
    session.add_all(
        RunAnomaly(
            run_id=a.run_id,
            row_id=a.row_id,
            field_name=a.field_name,
            code=a.code.value,
            severity=a.severity.value,
            detail=a.detail[:2000],
            value=a.value[:512],
            label=a.label[:512],
        )
        for a in anomalies[:5000]
    )

    if quarantined:
        log.warning(
            "run.quarantined_nothing_persisted",
            store_id=run.store_id,
            records_discarded=len(golden_records),
        )
    else:
        await _persist_catalogue(session, run, golden_records)
        _add_review_tasks(session, run, golden_records)
        _add_duplicate_tasks(session, run, unresolved_pairs or [])
    await session.commit()


async def _persist_catalogue(session: Any, run: IngestionRun, golden_records: list[Any]) -> None:
    """Met le catalogue du magasin a l'etat decrit par ce depot.

    Le CONTENU est remplace : un catalogue quotidien est un instantane complet,
    donc le depot du jour fait autorite et rien ne s'accumule. Un produit absent
    du nouveau fichier disparait du referentiel, c'est le sens d'un instantane.

    L'IDENTITE, elle, est conservee. Effacer puis reinserer donnait a chaque
    fiche un nouvel identifiant chaque matin : le rattachement des lignes du
    magasin et l'historique des corrections pointaient alors vers une fiche
    disparue. Le produit du magasin, lui, n'avait pas bouge. On met donc a jour
    la fiche existante, reconnue par sa cle naturelle chez ce magasin.

    Portee limitee au magasin : les autres enseignes ne sont pas touchees.
    """
    existing = {
        product.product_key: product
        for product in (
            await session.execute(select(Product).where(Product.store_id == run.store_id))
        )
        .scalars()
        .all()
    }

    seen: set[str] = set()
    for record in golden_records:
        key = record.key or record.label_normalized
        # Deux fiches ne peuvent pas partager une cle dans un meme depot : la
        # contrainte d'unicite le refuserait, et surtout ce serait le doublon
        # que cette etape est censee avoir supprime.
        if key in seen:
            log.warning("catalogue.duplicate_key", store_id=run.store_id, key=key)
            continue
        seen.add(key)

        product = existing.get(key)
        if product is None:
            product = Product(store_id=run.store_id, product_key=key)
            session.add(product)
            existing[key] = product

        product.run_id = run.id
        product.label = record.label
        product.label_normalized = record.label_normalized
        # Le libelle reecrit par le LLM etait calcule, facture, journalise
        # comme correction, puis jete : le catalogue livre restait en libelle
        # de caisse (« POM B 1K C1 »). On paie l'enrichissement, il doit
        # arriver dans le livrable.
        product.label_enriched = record.label_enriched or None
        product.ean = record.ean
        product.internal_code = record.internal_code
        product.url_image = record.url_image
        product.url_ok = record.url_ok
        product.url_checked = record.url_checked
        product.taxonomy_1 = record.taxonomy[0] or None
        product.taxonomy_2 = record.taxonomy[1] or None
        product.taxonomy_3 = record.taxonomy[2] or None
        product.taxonomy_4 = record.taxonomy[3] or None
        product.vat_rate = record.vat_rate
        product.quantity_value = record.quantity_value
        product.quantity_unit = record.quantity_unit
        product.origin = record.origin
        # La marque sert de barriere anti-fusion, cote pipeline comme cote
        # interface : encore faut-il qu'elle arrive jusqu'a la fiche.
        product.brand = getattr(record, "brand", "") or None
        product.status = record.status
        product.confidence = record.confidence
        product.publishable = record.publishable
        product.source_rows = record.source_rows

    # Les corrections humaines s'appliquent EN DERNIER, apres tout ce que le
    # pipeline a calcule. C'est ce qui les fait tenir : sans ce rejeu, le
    # depot du matin ecraserait la correction de la veille et l'utilisateur
    # referait chaque jour le meme geste.
    await _replay_overrides(session, run.store_id, existing, seen)

    retired = [product for key, product in existing.items() if key not in seen]
    for product in retired:
        del existing[product.product_key]
        await session.delete(product)

    # Les fiches creees a l'instant n'ont pas encore d'identifiant, et le
    # rattachement en a besoin.
    await session.flush()
    await _persist_source_rows(session, run, golden_records, existing)

    log.info(
        "catalogue.persisted",
        store_id=run.store_id,
        products=len(seen),
        retired=len(retired),
    )


async def _replay_overrides(
    session: Any, store_id: str, produits: dict[str, Any], seen: set[str]
) -> None:
    """Reapplique les valeurs corrigees a la main sur les fiches de ce magasin.

    Une correction porte sur la cle naturelle du produit, pas sur
    l'identifiant de la fiche : elle survit donc a la disparition puis au
    retour d'un produit dans le catalogue.

    Une correction dont le produit n'est plus au catalogue n'est pas
    supprimee : le magasin peut le redeposer demain, et la decision doit
    l'attendre plutot que d'etre a reprendre.
    """
    result = await session.execute(
        select(ProductOverride).where(ProductOverride.store_id == store_id)
    )
    overrides = list(result.scalars().all())
    if not overrides:
        return

    appliquees = 0
    for override in overrides:
        produit = produits.get(override.product_key)
        if produit is None or override.product_key not in seen:
            continue
        valeur: Any = override.value
        if override.field_name == "quantity_value" and valeur is not None:
            try:
                valeur = float(valeur)
            except ValueError:
                log.warning(
                    "override.invalid_number",
                    store_id=store_id,
                    field=override.field_name,
                    value=override.value,
                )
                continue
        setattr(produit, override.field_name, valeur)
        appliquees += 1

    log.info("overrides.replayed", store_id=store_id, applied=appliquees, stored=len(overrides))


async def _persist_source_rows(
    session: Any,
    run: IngestionRun,
    golden_records: list[Any],
    products: dict[str, Any],
) -> None:
    """Ecrit le fil entre chaque ligne du fichier et la fiche qui la represente.

    C'est ce qui empeche la fusion de couper le lien avec le magasin. Sa caisse
    continue de parler avec SES identifiants ; sans cette table, « stock de X =
    12 » ne designerait plus rien. Le referentiel compte une dizaine de fiches,
    les 10 000 lignes gardent chacune son rattachement.

    Remplace a chaque depot du magasin : la table decrit l'etat courant, pas
    l'historique des depots, qui vit dans l'audit trail.
    """
    await session.execute(delete(ProductSourceRow).where(ProductSourceRow.store_id == run.store_id))

    mappings = []
    for record in golden_records:
        product = products.get(record.key or record.label_normalized)
        if product is None:
            continue

        for row_id, source_label in record.source_refs:
            mappings.append(
                {
                    "id": str(uuid.uuid4()),
                    "store_id": run.store_id,
                    "product_id": product.id,
                    "run_id": run.id,
                    "row_id": row_id,
                    "source_label": source_label[:512],
                }
            )

    if mappings:
        # Insertion en masse : 10 000 objets ORM couteraient plusieurs secondes
        # de construction d'instances pour des lignes qu'on ne relit jamais
        # depuis la session.
        await session.execute(insert(ProductSourceRow), mappings)


def _add_duplicate_tasks(session: Any, run: IngestionRun, pairs: list[Any]) -> None:
    """La zone grise cesse d'etre silencieuse.

    Entre les deux seuils, le pipeline refuse de trancher : une fusion a tort
    est invisible et irreversible, un doublon restant se voit. Mais s'arreter la
    revenait a livrer un catalogue dont on SAIT qu'il contient des doublons sans
    le dire — sur le fichier reel, 173 paires restaient dans ce silence.

    Chaque paire devient donc une question, et la reponse devient une regle
    apprise rejouee aux depots suivants. La decision ne se reprend pas chaque
    matin, et le doute finit par disparaitre au lieu de se reproduire.

    Pas de role administrateur ici : fusionner deux fiches n'a pas de
    consequence fiscale, et c'est reversible.
    """
    for pair in pairs:
        session.add(
            ReviewTask(
                run_id=run.id,
                store_id=run.store_id,
                kind="possible_duplicate",
                field_name="produit",
                title=pair.left_label,
                question=(
                    f"« {pair.left_label} » et « {pair.right_label} » se ressemblent "
                    f"à {pair.score:.0%}, sans certitude. Est-ce le même produit ?"
                ),
                current_value=pair.right_label,
                proposed_value=pair.left_key,
                source="RULE",
                confidence=pair.score,
                affected_rows=0,
                context={
                    "left_key": pair.left_key,
                    "right_key": pair.right_key,
                    "left_label": pair.left_label,
                    "right_label": pair.right_label,
                    "score": pair.score,
                },
                requires_admin=False,
            )
        )


def _add_review_tasks(session: Any, run: IngestionRun, golden_records: list[Any]) -> None:
    """Une tache = un GROUPE de lignes, jamais une ligne.

    C'est ce qui rend la revue tenable. Sur l'echantillon, les centaines de
    lignes d'un meme whisky produisent UNE question — « ces N lignes portent un
    taux different des autres, appliquer le taux majoritaire ? » — au lieu de N
    arbitrages isoles.
    """
    for record in golden_records:
        if not record.vat_conflict or not record.vat_distribution:
            continue
        distribution = record.vat_distribution
        majority = max(distribution.items(), key=lambda kv: kv[1])
        minority = sum(v for k, v in distribution.items() if k != majority[0])
        if minority == 0:
            continue

        detail = ", ".join(
            f"{rate}% sur {count} ligne(s)"
            for rate, count in sorted(distribution.items(), key=lambda kv: -kv[1])
        )
        session.add(
            ReviewTask(
                run_id=run.id,
                store_id=run.store_id,
                kind="vat_mismatch",
                field_name="tva",
                title=record.label,
                question=(
                    f"{minority} ligne(s) de « {record.label} » portent un taux de TVA "
                    f"différent de la majorité. Appliquer {majority[0]} % à tout le groupe ?"
                ),
                current_value=detail,
                proposed_value=majority[0],
                source="RULE",
                confidence=majority[1] / max(sum(distribution.values()), 1),
                affected_rows=minority,
                context={
                    # La cle NATURELLE de la fiche, stable d'un depot a
                    # l'autre. C'est par elle que la decision retrouvera la
                    # fiche : le libelle, lui, peut changer entre la creation
                    # de la tache et la decision — une correction humaine, ou
                    # simplement un depot ou un autre libelle du groupe est le
                    # plus complet. Resoudre sur le libelle laissait alors la
                    # decision sans effet, et la tache marquee « approuvee ».
                    "product_key": record.key or record.label_normalized,
                    "distribution": distribution,
                    "total_rows": record.source_rows,
                    "taxonomy": list(record.taxonomy),
                    "ean": record.ean,
                    "sample_row_ids": record.row_ids[:10],
                },
                # La TVA a une consequence fiscale : seul un admin tranche.
                requires_admin=True,
            )
        )
