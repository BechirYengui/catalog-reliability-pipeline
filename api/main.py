"""API de la plateforme Catalog.

Sert : l'authentification, le depot de fichiers, le suivi d'execution en direct,
la file de revue humaine et le referentiel produit.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    BackgroundTasks,
    FastAPI,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import case, delete, distinct, func, select, update

from api import events, jobs
from api.deps import SESSION_COOKIE, CurrentUser, DbSession
from api.schemas import (
    PIPELINE_STEPS,
    AnomalyOut,
    AuditEntryOut,
    CorrectionOut,
    DecisionRequest,
    LoginRequest,
    MergeOut,
    MergeRequest,
    MetricsOut,
    OverrideOut,
    OverrideRequest,
    ProductOut,
    ReviewTaskOut,
    RowLookupOut,
    RunDetail,
    RunOut,
    SourceRowOut,
    StoreCatalogOut,
    UserOut,
)
from api.security import check_session_secret, hash_password, issue_token, verify_password
from db.models import (
    IngestionRun,
    LearnedRule,
    Product,
    ProductCorrection,
    ProductMerge,
    ProductOverride,
    ProductSourceRow,
    ReviewTask,
    RunAnomaly,
    Store,
    User,
)
from db.session import SessionFactory, migrate
from pipeline.config import PipelineConfig, Settings
from pipeline.context import compute_file_sha256
from pipeline.logging import configure_logging, get_logger

log = get_logger(__name__)
settings = Settings()

# Le nom de magasin voyage jusqu'a un nom de fichier : on le borne a des
# caracteres sans effet de bord. `\w` est unicode en Python, donc « Intermarché »
# passe, tandis que « ../ » et « / » sont refuses.
STORE_ID_PATTERN = re.compile(r"^[\w-]{1,64}$")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# `inbox` est la ZONE DE DEPOT du flux automatique : le balayage nocturne la
# vide, traite chaque CSV et le deplace dans `processed/` en le renommant. Un
# fichier depose par l'interface n'a donc rien a y faire — il y etait retraite
# une seconde fois chaque matin, puis renomme, ce qui cassait le telechargement
# de l'original et interdisait de rejouer un traitement.
INBOX = DATA_DIR / "inbox"
# Les fichiers deposes par l'interface, que rien ne balaie.
SOURCES = DATA_DIR / "sources"
# Ou chercher un original dont le chemin enregistre ne repond plus.
_SOURCE_DIRS = ("sources", "inbox", "processed", "quarantine")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level, json_output=settings.log_json)
    # AVANT tout le reste : une API qui demarre avec la cle de signature de
    # developpement est une API dont n'importe qui peut se declarer admin. Mieux
    # vaut ne pas demarrer du tout — le deploiement echoue, ce qui se voit, au
    # lieu d'exposer le referentiel de tous les magasins, ce qui ne se voit pas.
    check_session_secret()
    # Migrations plutot que create_all : une colonne ajoutee a une table qui
    # existe deja ne serait jamais creee autrement, et l'API repondrait 500
    # sur une colonne absente. C'est deja arrive.
    await migrate()
    await _bootstrap_admin()
    # Les taches de fond meurent avec le processus : tout run encore "RUNNING"
    # a cet instant est orphelin d'un arret precedent, et resterait fige dans
    # l'interface sans ce balayage.
    orphans = await jobs.fail_orphan_runs()
    if orphans:
        log.warning("api.orphan_runs_failed", count=orphans)
    INBOX.mkdir(parents=True, exist_ok=True)
    SOURCES.mkdir(parents=True, exist_ok=True)
    log.info("api.ready", data_dir=str(DATA_DIR))
    yield


async def _bootstrap_admin() -> None:
    """Cree le compte administrateur initial a partir de l'environnement.

    Sans cela, une instance fraiche n'a aucun compte et l'interface est
    inaccessible. Le mot de passe vient du .env genere au deploiement — il
    n'est jamais code en dur.
    """
    email = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or not password:
        log.warning("api.no_admin_configured")
        return

    async with SessionFactory() as session:
        existing = await session.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none():
            return
        session.add(User(email=email, password_hash=hash_password(password), role="admin"))
        await session.commit()
        log.info("api.admin_created", email=email)


app = FastAPI(
    title="Catalog Reliability Platform",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


# ---------------------------------------------------------------- sante
@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/llm/budget")
async def llm_budget(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Enveloppe de dépense LLM : plafond, dépensé, restant.

    Le plafond est CUMULÉ sur toute la démonstration. Un plafond par run seul
    ne protège de rien : dix runs sous leur plafond dépassent quand même
    l'enveloppe.
    """
    from api.budget import read_budget

    state = await read_budget(session)
    payload = state.to_dict()
    payload["note"] = (
        "Une fois l'enveloppe épuisée, l'étage LLM est sauté et le reste du "
        "pipeline continue — dégradation visible plutôt que facture inattendue."
    )
    return payload


@app.get("/api/llm/models")
async def llm_models() -> dict[str, Any]:
    """Catalogue des modèles, tarifs, et modèle réellement configuré.

    Affiché dans l'interface pour que le choix du modèle se fasse sur des
    chiffres. Les tarifs sont datés : un tarif de mémoire vieillit mal.
    """
    from pipeline.llm.pricing import CATALOG, PRICING_AS_OF

    configured = os.environ.get("LLM_MODEL", "claude-opus-5")
    return {
        "configured_model": configured,
        "api_key_present": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "pricing_as_of": PRICING_AS_OF.isoformat(),
        "models": [
            {
                "id": p.model_id,
                "display_name": p.display_name,
                "input_per_mtok": p.input_per_mtok,
                "output_per_mtok": p.output_per_mtok,
                "context_tokens": p.context_tokens,
                "note": p.note,
                "configured": p.model_id == configured,
            }
            for p in CATALOG.values()
        ],
    }


@app.get("/api/pipeline")
async def pipeline_definition() -> list[dict[str, str]]:
    """Description du pipeline, pour que l'interface affiche la chaine complete
    meme avant qu'un fichier ait ete depose."""
    return PIPELINE_STEPS


# ------------------------------------------------------- authentification
@app.post("/api/auth/login", response_model=UserOut)
async def login(payload: LoginRequest, response: Response, session: DbSession) -> User:
    result = await session.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    # Message identique dans les deux cas : distinguer « compte inconnu » de
    # « mot de passe faux » permettrait d'enumerer les comptes existants.
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        await asyncio.sleep(0.3)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "identifiants invalides")

    user.last_login_at = datetime.now(UTC)
    await session.commit()

    response.set_cookie(
        SESSION_COOKIE,
        issue_token(user.id, user.role),
        max_age=86_400 * 7,
        httponly=True,  # inaccessible au JavaScript : limite le vol par XSS
        samesite="lax",  # bloque l'envoi du cookie depuis un site tiers (CSRF)
        secure=True,
        path="/",
    )
    return user


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


# ------------------------------------------------------------------ runs
@app.post("/api/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    background: BackgroundTasks,
    session: DbSession,
    user: CurrentUser,
    file: UploadFile,
    store_id: Annotated[str, Form()] = "",
    check_urls: Annotated[bool, Form()] = False,
    # 'live' ou 'batch'. Le second coute moitie prix et repond plus tard :
    # l'arbitrage appartient a celui qui depose, pas au code.
    llm_mode: Annotated[str, Form()] = "live",
) -> IngestionRun:
    """Depose un CSV et lance le traitement en tache de fond."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "un fichier .csv est attendu")

    # Le magasin n'a pas de valeur par defaut : un depot anonyme irait grossir
    # un catalogue fourre-tout que personne ne reclamerait, et le referentiel
    # est cloisonne par magasin. C'est a celui qui depose de dire lequel.
    store_id = store_id.strip()
    if not store_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "le magasin est obligatoire")

    # Ce nom entre dans un chemin de fichier. Sans ce controle, un magasin
    # nomme « ../../etc » ecrirait hors du dossier de depot. Masquer le champ
    # dans l'interface ne protege rien : la validation appartient au serveur.
    if not STORE_ID_PATTERN.match(store_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "nom de magasin invalide : lettres, chiffres, tiret et souligne, 64 au plus",
        )

    SOURCES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = SOURCES / f"{store_id}_{stamp}.csv"
    with target.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    digest = compute_file_sha256(target)

    store = await session.get(Store, store_id)
    if store is None:
        session.add(Store(id=store_id, name=store_id))
        await session.flush()

    # Idempotence : le meme fichier pour le meme magasin ne relance pas un
    # traitement complet, on renvoie le run existant.
    existing = await session.execute(
        select(IngestionRun).where(
            IngestionRun.store_id == store_id, IngestionRun.file_sha256 == digest
        )
    )
    previous = existing.scalar_one_or_none()
    if previous and previous.status in ("COMPLETED", "RUNNING", "QUARANTINE"):
        target.unlink(missing_ok=True)
        log.info("run.duplicate_ignored", run_id=previous.id, sha=digest[:12])
        return previous

    run = IngestionRun(
        store_id=store_id,
        file_name=str(target),
        file_sha256=digest,
        file_size=target.stat().st_size,
        status="PENDING",
        network_enabled=check_urls,
        llm_mode="batch" if llm_mode == "batch" else "live",
        triggered_by=user.email,
    )
    session.add(run)
    await session.commit()

    await jobs.prepare_steps(session, run.id)
    background.add_task(jobs.execute_run, run.id)
    log.info(
        "run.created",
        run_id=run.id,
        store_id=store_id,
        check_urls=check_urls,
        llm_mode=run.llm_mode,
    )
    return run


@app.get("/api/runs", response_model=list[RunOut])
async def list_runs(session: DbSession, user: CurrentUser, limit: int = 50) -> list[IngestionRun]:
    result = await session.execute(
        select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(min(limit, 200))
    )
    return list(result.scalars().all())


@app.get("/api/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: str, session: DbSession, user: CurrentUser) -> IngestionRun:
    run = await session.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run inconnu")
    await session.refresh(run, ["steps"])
    return run


@app.delete("/api/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: str, session: DbSession, user: CurrentUser) -> None:
    """Supprime un traitement et tout ce qu'il a produit.

    Le registre des depenses LLM (`llm_spend`) n'est PAS efface : l'enveloppe
    a bien ete consommee, et la remettre a zero en supprimant un run
    transformerait le garde-fou budgetaire en illusion.
    """
    run = await session.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run inconnu")

    for table in (ProductCorrection, RunAnomaly, ReviewTask):
        await session.execute(delete(table).where(table.run_id == run_id))
    await session.execute(delete(Product).where(Product.run_id == run_id))
    await session.delete(run)
    await session.commit()
    log.info("run.deleted", run_id=run_id, by=user.email)


@app.get("/api/runs/{run_id}/anomalies", response_model=list[AnomalyOut])
async def run_anomalies(
    run_id: str,
    session: DbSession,
    user: CurrentUser,
    code: str | None = None,
    limit: int = 200,
) -> list[RunAnomaly]:
    query = select(RunAnomaly).where(RunAnomaly.run_id == run_id)
    if code:
        query = query.where(RunAnomaly.code == code)
    result = await session.execute(query.limit(min(limit, 1000)))
    return list(result.scalars().all())


@app.get("/api/audit", response_model=list[AuditEntryOut])
async def audit_trail(
    session: DbSession,
    user: CurrentUser,
    author: str | None = None,
    field: str | None = None,
    store_id: str | None = None,
    limit: int = 50,
) -> list[AuditEntryOut]:
    """Le journal d'audit, tous traitements confondus, du plus recent au plus ancien.

    La regle du projet est que toute valeur du referentiel doit pouvoir etre
    expliquee. Elle l'etait deja dans la base, mais nulle part a l'ecran : on
    voyait COMBIEN de corrections avaient ete faites, jamais lesquelles. Un
    audit trail qu'il faut une session psql pour lire n'en est pas tout a fait
    un.

    La jointure sur le produit et l'utilisateur est volontaire : une ligne qui
    dit « champ nom, ancienne valeur X » sans nommer le magasin ni le produit
    oblige a une seconde requete pour etre comprise.
    """
    # Presque toutes les corrections sont ecrites AVANT que les fiches
    # existent : une regle ou le LLM corrigent une ligne du fichier, et le
    # produit ne se forme qu'au regroupement. Leur `product_id` est donc vide,
    # et la jointure directe laissait 99 % du journal sans magasin ni produit.
    # On retombe sur le rattachement des lignes sources, qui relie justement
    # l'identifiant du magasin a la fiche qui le represente.
    query = (
        select(ProductCorrection, Product, User.email)
        .outerjoin(
            ProductSourceRow,
            ProductSourceRow.row_id == ProductCorrection.row_id,
        )
        .outerjoin(
            Product,
            Product.id == func.coalesce(ProductCorrection.product_id, ProductSourceRow.product_id),
        )
        .outerjoin(User, User.id == ProductCorrection.user_id)
        .order_by(ProductCorrection.created_at.desc())
    )
    if author:
        query = query.where(ProductCorrection.author == author.upper())
    if field:
        query = query.where(ProductCorrection.field_name == field)
    if store_id:
        query = query.where(Product.store_id == store_id)

    result = await session.execute(query.limit(min(limit, 500)))
    entrees: list[AuditEntryOut] = []
    for correction, produit, email in result.all():
        entrees.append(
            AuditEntryOut(
                id=correction.id,
                run_id=correction.run_id,
                store_id=produit.store_id if produit else None,
                product_label=(produit.label_enriched or produit.label) if produit else None,
                row_id=correction.row_id,
                field_name=correction.field_name,
                old_value=correction.old_value,
                new_value=correction.new_value,
                author=correction.author,
                rule=correction.rule,
                confidence=correction.confidence,
                # Distinguer « aucun humain derriere » de « le compte a ete
                # supprime » : une case vide laisserait croire a une correction
                # automatique alors qu'un humain l'a bien decidee.
                user_email=(
                    email if email else ("compte supprimé" if correction.user_id else None)
                ),
                created_at=correction.created_at,
            )
        )
    return entrees


@app.get("/api/runs/{run_id}/corrections", response_model=list[CorrectionOut])
async def run_corrections(
    run_id: str,
    session: DbSession,
    user: CurrentUser,
    field: str | None = None,
    limit: int = 200,
) -> list[ProductCorrection]:
    query = select(ProductCorrection).where(ProductCorrection.run_id == run_id)
    if field:
        query = query.where(ProductCorrection.field_name == field)
    result = await session.execute(query.limit(min(limit, 1000)))
    return list(result.scalars().all())


@app.get("/api/runs/{run_id}/summary")
async def run_summary(run_id: str, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """L'etat COURANT d'un run : ses fiches publiables, ses corrections par auteur.

    Le rapport du run porte deja ces chiffres, mais figes a la fin du
    traitement. Deux consequences, toutes deux constatees en production :

    - l'etage humain y vaut toujours zero, puisqu'une decision de revue, une
      fusion ou une correction a la main arrivent forcement APRES ;
    - les fiches publiables n'y bougent plus, alors que c'est precisement le
      travail humain qui les debloque. Sur un magasin, dix validations de TVA
      ont rendu publiables dix fiches que le rapport decrit encore comme
      rejetees.

    Compter en base repond donc a la seule question qui vaille — « qu'est-ce
    qui est publiable maintenant ? » — et donne le meme resultat que le CSV
    exporte, qui lit la meme table. Un rapport fige et un export vivant qui se
    contredisent, c'est l'utilisateur qui tranche, contre l'outil.
    """
    authors = await session.execute(
        select(ProductCorrection.author, func.count())
        .where(ProductCorrection.run_id == run_id)
        .group_by(ProductCorrection.author)
    )
    counts = {"RULE": 0, "LLM": 0, "HUMAN": 0}
    for author, count in authors.all():
        counts[author] = int(count)

    # Les DECISIONS, distinctes des lignes corrigees ci-dessus : une reponse du
    # LLM sur un libelle distinct vaut une decision, quel que soit le nombre de
    # lignes qui portent ce libelle. Compter les lignes pour dire « qui corrige
    # quoi » donnait 84 % au LLM et 16 % aux regles, l'inverse de qui a
    # reellement tranche. Meme definition pour les deux etages — une
    # transformation distincte (champ, valeur d'origine, valeur corrigee) —
    # sans quoi on comparerait de nouveau deux unites differentes.
    decision_rows = await session.execute(
        select(
            ProductCorrection.author,
            func.count(
                distinct(
                    func.concat(
                        ProductCorrection.field_name,
                        "\x1f",
                        func.coalesce(ProductCorrection.old_value, ""),
                        "\x1f",
                        func.coalesce(ProductCorrection.new_value, ""),
                    )
                )
            ),
        )
        .where(ProductCorrection.run_id == run_id)
        .group_by(ProductCorrection.author)
    )
    decisions = {"RULE": 0, "LLM": 0, "HUMAN": 0}
    for author, count in decision_rows.all():
        decisions[author] = int(count)

    records = await session.execute(
        select(
            func.count(Product.id),
            func.sum(case((Product.publishable.is_(True), 1), else_=0)),
        ).where(Product.run_id == run_id)
    )
    total, publishable = records.one()
    return {
        "corrections_by_author": counts,
        "decisions_by_author": decisions,
        "records_total": int(total or 0),
        "records_publishable": int(publishable or 0),
    }


@app.get("/api/events/runs/{run_id}")
async def run_events(run_id: str, user: CurrentUser) -> StreamingResponse:
    """Progression en direct (Server-Sent Events).

    SSE plutot que WebSocket : le flux est unidirectionnel (serveur vers
    navigateur), et SSE traverse les proxys HTTP sans configuration
    particuliere, ce qui compte ici puisque nginx est devant.
    """
    queue = events.subscribe(run_id)

    async def stream() -> AsyncIterator[str]:
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {message}\n\n"
                    if '"run.finished"' in message or '"run.failed"' in message:
                        break
                except TimeoutError:
                    # Commentaire SSE : maintient la connexion ouverte a travers
                    # les proxys, qui coupent les flux inactifs.
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _find_by_fingerprint(run: IngestionRun) -> Path | None:
    """Retrouve l'original d'un run dont le chemin enregistre ne repond plus.

    Le flux automatique deplace les CSV de `inbox/` vers `processed/` en les
    renommant : tout fichier depose par l'interface avant cette correction a
    donc bouge sous les pieds de son run. On le retrouve par son EMPREINTE, pas
    par son nom — le sha256 est deja en base, et c'est la seule identification
    qui ne ment pas apres un renommage.

    La taille sert de premier filtre : hacher tout un repertoire pour un fichier
    qui ne fait manifestement pas la bonne taille serait du gaspillage.
    """
    if not run.file_sha256:
        return None
    for nom in _SOURCE_DIRS:
        dossier = DATA_DIR / nom
        if not dossier.is_dir():
            continue
        for candidat in sorted(dossier.glob("*.csv")):
            if run.file_size and candidat.stat().st_size != run.file_size:
                continue
            if compute_file_sha256(candidat) == run.file_sha256:
                log.info("run.source_recovered", run_id=run.id, path=str(candidat))
                return candidat
    return None


@app.get("/api/runs/{run_id}/source.csv")
async def source_csv(run_id: str, session: DbSession, user: CurrentUser) -> FileResponse:
    """Le fichier tel qu'il a ete depose, avant tout traitement.

    Sans lui l'export fiabilise n'est pas verifiable : comparer les deux
    fichiers est la seule facon de constater ce que le pipeline a change.
    Les octets sont renvoyes intacts — pas de BOM ajoute, pas de reencodage :
    un original "corrige" a la volee ne serait plus une piece de comparaison.
    """
    run = await session.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run inconnu")

    path = Path(run.file_name).resolve()
    # Le chemin vient de la base et c'est l'API qui l'a ecrit, mais on le
    # contraint quand meme au repertoire de donnees : une ligne corrompue ne
    # doit jamais pouvoir servir un fichier arbitraire du serveur.
    inside = path.is_relative_to(DATA_DIR.resolve())
    if not inside or not path.is_file():
        found = _find_by_fingerprint(run)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "fichier d'origine introuvable")
        # Le chemin enregistre est repare : la recherche par empreinte coute une
        # lecture de fichier, il serait absurde de la refaire a chaque clic.
        run.file_name = str(found)
        await session.commit()
        path = found

    name = f"origine-{run.store_id}-{run.started_at:%Y%m%d}.csv"
    return FileResponse(path, media_type="text/csv", filename=name)


@app.get("/api/runs/{run_id}/export.csv")
async def export_csv(run_id: str, session: DbSession, user: CurrentUser) -> Response:
    """Le catalogue fiabilise, pret a etre pousse vers la marketplace.

    C'est la sortie qui compte pour le metier : le fichier d'entree ramene a ses
    fiches uniques, corrigees et annotees de leur statut.
    """
    run = await session.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run inconnu")

    result = await session.execute(
        select(Product).where(Product.run_id == run_id).order_by(Product.source_rows.desc())
    )
    products = list(result.scalars().all())

    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "ean",
            "internal_code",
            "nom",
            "nom_origine",
            "url_image",
            "taxonomie_niveau_1",
            "taxonomie_niveau_2",
            "taxonomie_niveau_3",
            "taxonomie_niveau_4",
            "tva",
            "contenance",
            "unite",
            "statut",
            "publiable",
            "lignes_fusionnees",
        ]
    )
    for p in products:
        writer.writerow(
            [
                p.ean or "",
                p.internal_code or "",
                # Le libelle vendable d'abord : c'est ce qu'attend la
                # marketplace. Le libelle de caisse reste dans la colonne
                # suivante, sans quoi le magasin ne reconnaitrait plus sa
                # propre ligne.
                p.label_enriched or p.label,
                p.label,
                p.url_image or "",
                p.taxonomy_1 or "",
                p.taxonomy_2 or "",
                p.taxonomy_3 or "",
                p.taxonomy_4 or "",
                "" if p.vat_rate is None else p.vat_rate,
                "" if p.quantity_value is None else p.quantity_value,
                p.quantity_unit or "",
                p.status,
                "oui" if p.publishable else "non",
                p.source_rows,
            ]
        )

    name = f"catalogue-{run.store_id}-{run.started_at:%Y%m%d}.csv"
    return Response(
        # BOM en tete : sans lui Excel ouvre le fichier en Latin-1 et reintroduit
        # exactement le mojibake que le pipeline vient de reparer.
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ------------------------------------------------------------ revue humaine
@app.get("/api/review/tasks", response_model=list[ReviewTaskOut])
async def list_review_tasks(
    session: DbSession, user: CurrentUser, status_filter: str = "pending", limit: int = 100
) -> list[ReviewTask]:
    query = select(ReviewTask)
    if status_filter != "all":
        query = query.where(ReviewTask.status == status_filter)
    result = await session.execute(
        query.order_by(ReviewTask.affected_rows.desc()).limit(min(limit, 500))
    )
    return list(result.scalars().all())


@app.post("/api/review/tasks/{task_id}/decision", response_model=ReviewTaskOut)
async def decide(
    task_id: str, payload: DecisionRequest, session: DbSession, user: CurrentUser
) -> ReviewTask:
    task = await session.get(ReviewTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tache inconnue")
    if task.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "tache deja tranchee")

    # Verification cote serveur : masquer le bouton ne protege rien.
    if task.requires_admin and user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cette decision touche a la TVA et demande un role admin",
        )

    value = payload.value if payload.action == "edit" else task.proposed_value

    # Une correction sans valeur n'est pas une correction. Sans ce refus, une
    # saisie vide tombait dans le `if ... and value` plus bas — donc rien
    # n'etait applique — pendant que la tache passait « editee » et disparaissait
    # de la file. La question etait close sans avoir ete tranchee.
    if payload.action == "edit" and not (value or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "aucune valeur fournie : pour refuser la proposition, utilisez « rejeter »",
        )
    task.status = {"approve": "approved", "reject": "rejected", "edit": "edited"}[payload.action]
    task.decided_by = user.id
    task.decided_at = datetime.now(UTC)
    task.decision_value = value

    if task.kind == "possible_duplicate":
        # La zone grise : deux fiches que le pipeline n'a pas ose separer ni
        # fusionner. Approuver revient a decider que c'est le meme produit, et
        # cette decision passe par la MEME route que la fusion manuelle : meme
        # audit trail, meme regle apprise, meme annulation possible. Dupliquer
        # ce code aurait fait diverger les deux chemins au premier correctif.
        if payload.action == "approve":
            contexte = task.context or {}
            fiches = await session.execute(
                select(Product).where(
                    Product.store_id == task.store_id,
                    Product.product_key.in_(
                        [contexte.get("left_key", ""), contexte.get("right_key", "")]
                    ),
                )
            )
            ids = [produit.id for produit in fiches.scalars().all()]
            if len(ids) < 2:
                # Une des deux fiches a disparu depuis le depot : la question
                # n'a plus d'objet, on la classe sans rien casser.
                log.info("review.duplicate_moot", task_id=task.id, found=len(ids))
            else:
                await merge_products(MergeRequest(product_ids=ids), session, user)
        await session.commit()
        log.info("review.decided", task_id=task_id, action=payload.action, user=user.email)
        return task

    if payload.action in ("approve", "edit") and value:
        # La TVA saisie est VERIFIEE avant d'etre ecrite. Le champ est libre
        # cote client, et `float(value)` acceptait aussi bien « abc » (500 brut
        # sur un geste banal) qu'un taux de 99 % — inscrit au referentiel par
        # le chemin meme qui existe pour proteger la TVA. Le pipeline refuse de
        # corriger un taux tout seul ; il ne s'ensuit pas qu'un humain puisse
        # en inventer un.
        if task.field_name == "tva":
            legaux = PipelineConfig.load().vat.legal_rates_fr
            try:
                taux = float(str(value).replace(",", ".").strip())
            except ValueError:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"« {value} » n'est pas un taux de TVA. Valeurs acceptées : "
                    + ", ".join(f"{t:g}" for t in legaux),
                ) from None
            if taux not in legaux:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"{taux:g} % n'est pas un taux de TVA français. Valeurs acceptées : "
                    + ", ".join(f"{t:g}" for t in legaux),
                )
            value = f"{taux:g}"

        # Application au referentiel + audit trail. La fiche est retrouvee par
        # sa cle NATURELLE, pas par son libelle : le libelle peut avoir change
        # depuis la creation de la tache, et la decision serait alors appliquee
        # a aucune fiche — ou, pire, a une homonyme.
        # Le repli sur le libelle sert les taches creees avant que la cle soit
        # portee dans le contexte : elles n'ont pas de `product_key`, et les
        # laisser sans critere les rendrait intranchables.
        cle = (task.context or {}).get("product_key")
        critere = Product.product_key == cle if cle else Product.label == task.title
        result = await session.execute(
            select(Product).where(Product.store_id == task.store_id, critere)
        )
        # Ce que la decision remplace REELLEMENT. La fiche porte deja le taux
        # majoritaire, herite de la fusion : ecrire « 20 -> 20 » etait vrai pour
        # la fiche et muet sur la decision, qui unifie les lignes divergentes.
        # Le journal doit dire ce qui a change, pas ce qui n'a pas bouge.
        distribution: dict[str, int] = (task.context or {}).get("distribution") or {}
        divergentes = {taux: n for taux, n in distribution.items() if str(taux) != str(value)}
        lignes_corrigees = sum(divergentes.values())
        remplaces = ", ".join(f"{taux} % ({n})" for taux, n in sorted(divergentes.items()))

        for product in result.scalars().all():
            old = remplaces or (str(product.vat_rate) if product.vat_rate is not None else None)
            if task.field_name == "tva":
                product.vat_rate = float(value)
                product.status = "VALIDATED"
                product.publishable = bool(product.ean and product.url_ok and product.taxonomy_4)
            session.add(
                ProductCorrection(
                    run_id=task.run_id,
                    product_id=product.id,
                    row_id=product.id,
                    field_name=task.field_name,
                    old_value=old,
                    new_value=value,
                    author="HUMAN",
                    rule=(
                        f"review.{task.kind}[{lignes_corrigees} lignes]"
                        if lignes_corrigees
                        else f"review.{task.kind}"
                    ),
                    confidence=1.0,
                    user_id=user.id,
                )
            )

        # LA BOUCLE D'APPRENTISSAGE. Sans elle, la meme question revient a
        # chaque depot du magasin. C'est la flèche en pointillés du schema.
        leaf = (task.context or {}).get("taxonomy", [None, None, None, None])[3]
        if task.field_name == "tva" and leaf:
            existing = await session.execute(
                select(LearnedRule).where(
                    LearnedRule.scope == "category_vat", LearnedRule.key == leaf
                )
            )
            rule = existing.scalar_one_or_none()
            if rule:
                rule.value = value
                rule.hits += 1
            else:
                session.add(
                    LearnedRule(scope="category_vat", key=leaf, value=value, created_by=user.id)
                )

    await session.commit()
    log.info("review.decided", task_id=task_id, action=payload.action, user=user.email)
    return task


# -------------------------------------------------------------- referentiel
@app.get("/api/stores", response_model=list[StoreCatalogOut])
async def list_store_catalogs(session: DbSession, user: CurrentUser) -> list[StoreCatalogOut]:
    """Le referentiel de chaque magasin, avec sa taille.

    Cote interface, la liste des magasins etait deduite des fiches deja
    chargees, c'est-a-dire d'une page de 100 lignes triee par nombre de
    lignes sources. Un magasin volumineux occupait la page entiere et les
    autres devenaient invisibles. Le comptage appartient a la base.
    """
    result = await session.execute(
        select(
            Product.store_id,
            func.count(Product.id),
            func.sum(case((Product.publishable.is_(True), 1), else_=0)),
            func.sum(Product.source_rows),
            func.max(Product.updated_at),
        ).group_by(Product.store_id)
    )
    return [
        StoreCatalogOut(
            store_id=store_id,
            products=count,
            publishable=int(publishable or 0),
            source_rows=int(rows or 0),
            last_run_at=updated,
        )
        for store_id, count, publishable, rows, updated in result.all()
    ]


@app.get("/api/products", response_model=list[ProductOut])
async def list_products(
    session: DbSession,
    user: CurrentUser,
    q: str | None = None,
    store_id: str | None = None,
    only_publishable: bool = False,
    limit: int = 100,
) -> list[Product]:
    query = select(Product)
    if q:
        query = query.where(Product.label_normalized.ilike(f"%{q.upper()}%"))
    if store_id:
        query = query.where(Product.store_id == store_id)
    if only_publishable:
        query = query.where(Product.publishable.is_(True))
    result = await session.execute(
        query.order_by(Product.source_rows.desc()).limit(min(limit, 500))
    )
    return list(result.scalars().all())


#: Les champs d'une fiche, pour pouvoir la reconstruire a l'identique. La liste
#: est explicite plutot que deduite du modele : ajouter une colonne un jour ne
#: doit pas embarquer silencieusement un identifiant ou un horodatage.
_SNAPSHOT_FIELDS = (
    "id",
    "store_id",
    "run_id",
    "product_key",
    "label",
    "label_normalized",
    "label_enriched",
    "ean",
    "internal_code",
    "url_image",
    "url_ok",
    "url_checked",
    "taxonomy_1",
    "taxonomy_2",
    "taxonomy_3",
    "taxonomy_4",
    "vat_rate",
    "brand",
    "quantity_value",
    "quantity_unit",
    "origin",
    "status",
    "publishable",
    "confidence",
    "source_rows",
)


# Les champs numeriques doivent redevenir des nombres : les stocker en texte
# est un detail de la table de corrections, pas une propriete de la fiche.
_NUMERIC_FIELDS = {"quantity_value"}


def _apply_override(product: Product, field_name: str, valeur: str | None) -> None:
    if field_name in _NUMERIC_FIELDS:
        try:
            setattr(product, field_name, None if valeur is None else float(valeur))
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{field_name} doit etre un nombre"
            ) from exc
    else:
        setattr(product, field_name, valeur)


def _snapshot(product: Product) -> dict[str, Any]:
    return {field: getattr(product, field) for field in _SNAPSHOT_FIELDS}


@app.patch("/api/products/{product_id}", response_model=ProductOut)
async def override_product_field(
    product_id: str,
    payload: OverrideRequest,
    session: DbSession,
    user: CurrentUser,
) -> Product:
    """Corrige un champ a la main, et fait tenir la correction.

    Le referentiel est recalcule a chaque depot depuis le CSV du magasin. Une
    correction ecrite seulement dans la fiche serait donc effacee le lendemain
    matin, sans prevenir : l'utilisateur ferait le geste, verrait le resultat,
    et le perdrait. C'est pire qu'un bouton absent.

    La correction est donc enregistree comme une DECISION portant sur la cle
    naturelle du produit, rejouee a chaque integration apres l'etage LLM.
    L'humain gagne toujours contre le modele.

    La TVA n'est pas modifiable ici : le schema la refuse. Elle passe par la
    file de revue, ou seul un administrateur peut trancher, parce que l'erreur
    a une consequence fiscale.
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fiche inconnue")

    valeur = (payload.value or "").strip() or None
    ancienne = getattr(product, payload.field_name, None)
    ancienne_texte = None if ancienne is None else str(ancienne)
    if valeur == ancienne_texte:
        return product

    # La valeur d'origine conservee est celle CALCULEE par le pipeline, pas
    # celle d'une correction humaine precedente : sinon, corriger deux fois
    # ferait perdre le point de retour.
    existante = (
        await session.execute(
            select(ProductOverride).where(
                ProductOverride.store_id == product.store_id,
                ProductOverride.product_key == product.product_key,
                ProductOverride.field_name == payload.field_name,
            )
        )
    ).scalar_one_or_none()

    if existante is None:
        session.add(
            ProductOverride(
                store_id=product.store_id,
                product_key=product.product_key,
                field_name=payload.field_name,
                previous_value=ancienne_texte,
                value=valeur,
                user_id=user.id,
            )
        )
    else:
        existante.value = valeur
        existante.user_id = user.id

    _apply_override(product, payload.field_name, valeur)

    session.add(
        ProductCorrection(
            run_id=product.run_id or "",
            product_id=product.id,
            row_id=product.product_key[:64],
            field_name=payload.field_name,
            old_value=ancienne_texte,
            new_value=valeur,
            author="HUMAN",
            rule="review.manual_override",
            confidence=1.0,
            user_id=user.id,
        )
    )

    await session.commit()
    await session.refresh(product)
    log.info(
        "product.override",
        store_id=product.store_id,
        field=payload.field_name,
        user=user.email,
    )
    return product


@app.get("/api/products/{product_id}/overrides", response_model=list[OverrideOut])
async def list_overrides(
    product_id: str, session: DbSession, user: CurrentUser
) -> list[ProductOverride]:
    """Les corrections humaines en vigueur sur cette fiche."""
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fiche inconnue")
    result = await session.execute(
        select(ProductOverride).where(
            ProductOverride.store_id == product.store_id,
            ProductOverride.product_key == product.product_key,
        )
    )
    return list(result.scalars().all())


@app.delete("/api/products/{product_id}/overrides/{field_name}", response_model=ProductOut)
async def undo_override(
    product_id: str, field_name: str, session: DbSession, user: CurrentUser
) -> Product:
    """Annule une correction et remet la valeur calculee par le pipeline.

    Supprimer la seule regle laisserait la fiche dans son etat corrige jusqu'au
    lendemain : l'utilisateur cliquerait « annuler » et ne verrait rien
    changer. On restitue donc aussi la valeur d'origine, tout de suite.
    """
    product = await session.get(Product, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fiche inconnue")

    override = (
        await session.execute(
            select(ProductOverride).where(
                ProductOverride.store_id == product.store_id,
                ProductOverride.product_key == product.product_key,
                ProductOverride.field_name == field_name,
            )
        )
    ).scalar_one_or_none()
    if override is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "aucune correction sur ce champ")

    _apply_override(product, field_name, override.previous_value)
    session.add(
        ProductCorrection(
            run_id=product.run_id or "",
            product_id=product.id,
            row_id=product.product_key[:64],
            field_name=field_name,
            old_value=override.value,
            new_value=override.previous_value,
            author="HUMAN",
            rule="review.undo_override",
            confidence=1.0,
            user_id=user.id,
        )
    )
    await session.delete(override)
    await session.commit()
    await session.refresh(product)
    return product


@app.post("/api/products/merge", response_model=ProductOut)
async def merge_products(payload: MergeRequest, session: DbSession, user: CurrentUser) -> Product:
    """Fusionne des fiches qu'un humain declare identiques.

    Le pipeline n'a pas su trancher — s'il avait su, il l'aurait fait. Cette
    route n'est donc pas une correction de donnee, c'est une DECISION, et elle
    s'inscrit a trois endroits : le referentiel change, l'audit trail garde qui
    a decide quoi, et une regle apprise rejoue la fusion au depot suivant. Sans
    ce troisieme point, la meme fusion serait a refaire chaque matin.

    Deux refus, non negociables, car ce sont les seuls cas ou la machine SAIT
    que l'humain se trompe : contenance differente (1 L n'est pas 33 cl) et
    marque differente (Doliprane n'est pas un generique). Le reste est le
    jugement de l'utilisateur et lui appartient — y compris fusionner deux EAN
    distincts, puisque l'EAN identifie la ligne, pas le produit.
    """
    if len(set(payload.product_ids)) < 2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "il faut au moins deux fiches")

    result = await session.execute(select(Product).where(Product.id.in_(payload.product_ids)))
    products = list(result.scalars().all())
    if len(products) != len(set(payload.product_ids)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fiche inconnue")
    if len({p.store_id for p in products}) > 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "ces fiches appartiennent a des magasins differents",
        )

    quantities = {(p.quantity_value, p.quantity_unit) for p in products if p.quantity_value}
    if len(quantities) > 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "contenances differentes : deux formats ne sont pas le meme produit",
        )
    brands = {(p.brand or "").strip().upper() for p in products if (p.brand or "").strip()}
    if len(brands) > 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"marques differentes ({', '.join(sorted(brands))}) : deux references distinctes",
        )

    # Le survivant porte le plus de lignes du magasin : c'est celui dont
    # l'identifiant circule deja le plus chez le magasin.
    survivor = max(products, key=lambda p: (p.source_rows, p.id))
    absorbed = [p for p in products if p.id != survivor.id]

    for product in absorbed:
        # Le fil vers les lignes du magasin suit la fusion, sinon la fiche
        # annoncerait un total dont elle ne sait plus nommer les lignes. On note
        # QUELLES lignes ont bouge : sans cette liste, annuler rendrait la fiche
        # sans ses lignes, melangees a celles du survivant.
        moved = await session.execute(
            select(ProductSourceRow.id).where(ProductSourceRow.product_id == product.id)
        )
        moved_ids = list(moved.scalars().all())
        await session.execute(
            update(ProductSourceRow)
            .where(ProductSourceRow.product_id == product.id)
            .values(product_id=survivor.id)
        )
        session.add(
            ProductMerge(
                store_id=product.store_id,
                survivor_id=survivor.id,
                survivor_label=survivor.label,
                absorbed_label=product.label,
                absorbed=_snapshot(product),
                moved_row_ids={"ids": moved_ids},
                user_id=user.id,
            )
        )
        session.add(
            ProductCorrection(
                run_id=product.run_id or "",
                product_id=survivor.id,
                row_id="",
                field_name="product_merge",
                old_value=product.label,
                new_value=survivor.label,
                author="HUMAN",
                rule="human_merge",
                confidence=1.0,
                user_id=user.id,
            )
        )
        # La regle apprise : demain, le pipeline fusionnera tout seul. Elle
        # porte le magasin, comme le referentiel qu'elle modifie.
        key = jobs.merge_rule_key(product.store_id, product.label_normalized)
        existing = await session.execute(
            select(LearnedRule).where(LearnedRule.scope == "same_product", LearnedRule.key == key)
        )
        rule = existing.scalar_one_or_none()
        if rule is None:
            session.add(
                LearnedRule(
                    scope="same_product",
                    key=key,
                    value=survivor.label_normalized,
                    created_by=user.id,
                )
            )
        else:
            rule.value = survivor.label_normalized
        await session.delete(product)

    survivor.source_rows = sum(p.source_rows for p in products)
    # Un desaccord de TVA entre fiches fusionnees appartient a l'humain, et la
    # fiche n'est pas publiable tant qu'il n'a pas tranche.
    if len({p.vat_rate for p in products if p.vat_rate is not None}) > 1:
        survivor.publishable = False
        survivor.status = "NEEDS_REVIEW"
    await session.commit()

    log.info(
        "products.merged",
        store_id=survivor.store_id,
        survivor=survivor.id,
        absorbed=len(absorbed),
        by=user.email,
    )
    return survivor


@app.get("/api/merges", response_model=list[MergeOut])
async def list_merges(
    session: DbSession,
    user: CurrentUser,
    store_id: str | None = None,
    limit: int = 20,
) -> list[ProductMerge]:
    """Les fusions decidees a la main, la plus recente d'abord."""
    query = select(ProductMerge).where(ProductMerge.undone_at.is_(None))
    if store_id:
        query = query.where(ProductMerge.store_id == store_id)
    result = await session.execute(
        query.order_by(ProductMerge.created_at.desc()).limit(min(limit, 100))
    )
    return list(result.scalars().all())


@app.post("/api/merges/{merge_id}/undo", response_model=ProductOut)
async def undo_merge(merge_id: str, session: DbSession, user: CurrentUser) -> Product:
    """Defait une fusion : la fiche absorbee revient, avec ses lignes.

    Une decision qu'on ne peut pas reprendre n'est pas une decision, c'est un
    piege — surtout ici, ou l'utilisateur tranche un cas que la machine n'a pas
    su trancher. Il se trompera parfois.

    Trois choses reviennent en arriere : la fiche est recreee telle qu'elle
    etait, ses lignes du magasin lui sont rendues (celles-la precisement, pas
    celles du survivant), et la regle apprise disparait pour que le depot de
    demain ne refasse pas la fusion. L'audit trail, lui, ne s'efface pas : il
    gagne une ligne de plus.
    """
    merge = await session.get(ProductMerge, merge_id)
    if merge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fusion inconnue")
    if merge.undone_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "fusion deja annulee")

    survivor = await session.get(Product, merge.survivor_id)
    if survivor is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "la fiche survivante n'existe plus : rien a defaire",
        )

    snapshot = dict(merge.absorbed)
    restored = Product(**snapshot)
    session.add(restored)
    await session.flush()

    row_ids = list(merge.moved_row_ids.get("ids", []))
    if row_ids:
        await session.execute(
            update(ProductSourceRow)
            .where(ProductSourceRow.id.in_(row_ids))
            .values(product_id=restored.id)
        )
    survivor.source_rows = max(survivor.source_rows - int(snapshot.get("source_rows") or 0), 0)

    await session.execute(
        delete(LearnedRule).where(
            LearnedRule.scope == "same_product",
            LearnedRule.key
            == jobs.merge_rule_key(merge.store_id, str(snapshot.get("label_normalized") or "")),
        )
    )
    session.add(
        ProductCorrection(
            run_id=snapshot.get("run_id") or "",
            product_id=restored.id,
            row_id="",
            field_name="product_merge_undone",
            old_value=merge.survivor_label,
            new_value=merge.absorbed_label,
            author="HUMAN",
            rule="human_merge_undo",
            confidence=1.0,
            user_id=user.id,
        )
    )

    merge.undone_at = datetime.now(UTC)
    merge.undone_by = user.id
    await session.commit()

    log.info(
        "products.merge_undone",
        store_id=merge.store_id,
        survivor=survivor.id,
        restored=restored.id,
        by=user.email,
    )
    return restored


@app.get("/api/products/{product_id}/source-rows", response_model=list[SourceRowOut])
async def product_source_rows(
    product_id: str,
    session: DbSession,
    user: CurrentUser,
    limit: int = 200,
) -> list[ProductSourceRow]:
    """Les lignes du fichier magasin que cette fiche represente.

    Une fiche annonce « 945 lignes fusionnees » ; ceci les nomme. Sans cette
    liste, la fusion serait une affirmation invérifiable, et une fusion erronee
    resterait invisible : c'est le libelle d'origine de chaque ligne qui permet
    de la contester.
    """
    result = await session.execute(
        select(ProductSourceRow)
        .where(ProductSourceRow.product_id == product_id)
        .order_by(ProductSourceRow.row_id)
        .limit(min(limit, 1000))
    )
    return list(result.scalars().all())


@app.get("/api/stores/{store_id}/rows/{row_id}", response_model=RowLookupOut)
async def lookup_row(
    store_id: str,
    row_id: str,
    session: DbSession,
    user: CurrentUser,
) -> RowLookupOut:
    """A quoi correspond un identifiant du magasin ?

    C'est la question que pose le systeme du magasin. Sa caisse annonce
    « stock de 4f2a-91 = 12 » avec SES identifiants, et le referentiel doit
    savoir repondre que 4f2a-91 est le whisky ecossais 70 cl. Fusionner sans
    pouvoir repondre a ca reviendrait a couper le lien avec le magasin.
    """
    result = await session.execute(
        select(ProductSourceRow, Product)
        .join(Product, Product.id == ProductSourceRow.product_id)
        .where(ProductSourceRow.store_id == store_id, ProductSourceRow.row_id == row_id)
    )
    found = result.first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ligne inconnue pour ce magasin")

    source_row, product = found
    return RowLookupOut(
        row_id=source_row.row_id,
        store_id=source_row.store_id,
        product_id=product.id,
        product_label=product.label,
        source_label=source_row.source_label,
        publishable=product.publishable,
    )


@app.get("/api/products/{product_id}/history", response_model=list[CorrectionOut])
async def product_history(
    product_id: str, session: DbSession, user: CurrentUser
) -> list[ProductCorrection]:
    result = await session.execute(
        select(ProductCorrection)
        .where(ProductCorrection.product_id == product_id)
        .order_by(ProductCorrection.created_at.desc())
    )
    return list(result.scalars().all())


# ----------------------------------------------------------------- metriques
@app.get("/api/metrics", response_model=MetricsOut)
async def metrics(session: DbSession, user: CurrentUser) -> MetricsOut:
    week_ago = datetime.now(UTC) - timedelta(days=7)

    runs_total = await session.scalar(select(func.count()).select_from(IngestionRun)) or 0
    runs_week = (
        await session.scalar(
            select(func.count())
            .select_from(IngestionRun)
            .where(IngestionRun.started_at >= week_ago)
        )
        or 0
    )
    products_total = await session.scalar(select(func.count()).select_from(Product)) or 0
    publishable = (
        await session.scalar(
            select(func.count()).select_from(Product).where(Product.publishable.is_(True))
        )
        or 0
    )
    pending = (
        await session.scalar(
            select(func.count()).select_from(ReviewTask).where(ReviewTask.status == "pending")
        )
        or 0
    )
    learned = await session.scalar(select(func.count()).select_from(LearnedRule)) or 0

    trend_rows = await session.execute(
        select(IngestionRun)
        .where(IngestionRun.status.in_(("COMPLETED", "QUARANTINE")))
        .order_by(IngestionRun.started_at.desc())
        .limit(20)
    )
    runs = list(trend_rows.scalars().all())
    trend = [
        {
            "run_id": r.id,
            "store_id": r.store_id,
            "started_at": r.started_at.isoformat(),
            "publishable_rate": r.publishable_rate,
            "rows_in": r.rows_in,
        }
        for r in reversed(runs)
    ]

    aggregated: dict[str, int] = {}
    for run in runs[:5]:
        for code, count in (run.report or {}).get("anomalies_by_code", {}).items():
            aggregated[code] = aggregated.get(code, 0) + count

    return MetricsOut(
        runs_total=runs_total,
        runs_last_7d=runs_week,
        products_total=products_total,
        publishable_rate=(publishable / products_total) if products_total else 0.0,
        pending_reviews=pending,
        learned_rules=learned,
        trend=trend,
        anomalies_by_code=dict(sorted(aggregated.items(), key=lambda kv: -kv[1])),
    )
