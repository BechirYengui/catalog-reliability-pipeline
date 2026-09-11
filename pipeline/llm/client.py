"""Couche d'appel au LLM : cache, budget, comptabilité, dégradation gracieuse.

Ordre de construction volontaire — le cache et la comptabilité d'abord, les
prompts ensuite. Un cache dont on n'observe pas le taux de succès est un cache
dont on ne sait pas s'il existe ; on a déjà payé ce prix sur le cache DNS de la
phase 1 (9 044 résolutions pour 6 domaines, invisibles jusqu'à ce qu'un
compteur les rende visibles).

Trois garde-fous :

1. **Le LLM ne voit que des libellés DISTINCTS.** 10 000 lignes se ramènent à
   ~273 libellés : le facteur 37 est la principale économie du projet.
2. **Les libellés partent par lots.** 273 libellés en lots de 25, c'est 11
   appels au lieu de 273 — l'en-tête du prompt (taxonomie de référence,
   consignes) est écrit 11 fois au lieu de 273.
3. **Budget maximal par run.** Au-delà, le pipeline s'arrête d'appeler et le
   reste part en file d'attente. Un run ne peut pas dépenser sans plafond
   parce qu'un fichier d'entrée était inhabituel.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from typing import Literal

from pipeline.llm.pricing import DEFAULT_MODEL, UsageCost, compute_cost, pricing_for
from pipeline.logging import get_logger

log = get_logger(__name__)


class CacheBackend(Protocol):
    """Le cache est branchable : un dictionnaire en CLI, PostgreSQL dans l'API."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(
        self,
        key: str,
        model: str,
        prompt: str,
        response: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
    ) -> None: ...


class MemoryCache:
    """Cache de session. Suffisant pour la CLI ; l'API branche PostgreSQL."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._store.get(key)

    def set(
        self,
        key: str,
        model: str,
        prompt: str,
        response: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        self._store[key] = response


@dataclass(slots=True)
class LlmUsage:
    """Ce que le run a réellement consommé. Publié dans le rapport et affiché."""

    model: str = DEFAULT_MODEL
    calls_real: int = 0
    calls_cached: int = 0
    # Le cache se mesure en LIBELLES : c'est l'unite qu'on paie et celle que
    # l'utilisateur reconnait. Un taux calcule sur les lots repondait a une
    # question que personne ne se pose.
    labels_total: int = 0
    labels_cached: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    errors: int = 0
    queued: int = 0
    # Reponses que l'on n'a pas su rattacher a un libelle envoye : payees et
    # perdues. A zero normalement ; non nul, c'est un signal.
    unmatched: int = 0
    dry_run: bool = False
    duration_ms: float = 0.0
    # Le tarif applique depend du mode : la comptabilite doit le savoir, sinon
    # elle affiche le double de ce qui sera facture.
    batch_api: bool = False
    batch_ids: list[str] = field(default_factory=list)

    @property
    def cost(self) -> UsageCost:
        return compute_cost(
            self.model,
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            batch_api=self.batch_api,
        )

    @property
    def cache_hit_rate(self) -> float:
        if self.labels_total:
            return self.labels_cached / self.labels_total
        total = self.calls_real + self.calls_cached
        return self.calls_cached / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        cost = self.cost
        price = pricing_for(self.model)
        return {
            "model": self.model,
            "model_display_name": price.display_name,
            "input_price_per_mtok": price.input_per_mtok,
            "output_price_per_mtok": price.output_per_mtok,
            "calls_real": self.calls_real,
            "calls_cached": self.calls_cached,
            "labels_total": self.labels_total,
            "labels_cached": self.labels_cached,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(cost.total_usd, 6),
            "cost_breakdown_usd": {
                "input": round(cost.input_usd, 6),
                "output": round(cost.output_usd, 6),
                "cache_read": round(cost.cache_read_usd, 6),
                "cache_write": round(cost.cache_write_usd, 6),
            },
            "errors": self.errors,
            "queued": self.queued,
            "unmatched": self.unmatched,
            "dry_run": self.dry_run,
            "batch_api": self.batch_api,
            "batch_ids": list(self.batch_ids),
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass(slots=True)
class LlmSettings:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    batch_size: int = 25
    max_calls_per_run: int = 500
    # Plafond de dépense par run, en dollars. Un run qui l'atteint arrête
    # d'appeler et met le reste en file d'attente plutôt que de continuer.
    max_cost_usd: float = 5.0
    max_retries: int = 3
    dry_run: bool = False
    # `low` suffit largement pour réécrire un libellé caisse et choisir une
    # catégorie dans une liste fermée. Monter l'effort coûterait sans rien
    # apporter sur une tâche de cette nature.
    effort: str = "low"
    # Traitement par lots asynchrone : moitie prix, mais la reponse arrive plus
    # tard (souvent quelques minutes, 24 h au maximum garanti). A reserver aux
    # runs que personne ne regarde — le timer de nuit, pas un depot fait a la
    # main devant une barre de progression.
    batch_api: bool = False
    # Au-dela, on renonce a attendre et le pipeline continue sans l'etage LLM.
    # L'identifiant du lot est conserve : rien n'est paye pour rien.
    batch_timeout_s: int = 7200
    batch_poll_s: int = 15


@dataclass(slots=True)
class LlmResult:
    """Sortie d'un lot : la réponse par libellé, plus ce qu'elle a coûté."""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: LlmUsage = field(default_factory=LlmUsage)
    prompts_preview: list[str] = field(default_factory=list)


def cache_key(prompt: str, model: str) -> str:
    """sha256(prompt normalisé + modèle).

    Le modèle fait partie de la clé : la même question posée à deux modèles
    n'a pas la même réponse, et resservir la réponse de l'un pour l'autre
    fausserait aussi bien la qualité que la comptabilité.
    """
    normalized = " ".join(prompt.split())
    return hashlib.sha256(f"{model}\x00{normalized}".encode()).hexdigest()


def _user_prompt(batch: Sequence[str]) -> str:
    return "Libellés à traiter :\n" + "\n".join(f"- {label}" for label in batch)


def _squash(text: str) -> str:
    """Forme comparable d'un libelle : espaces reduits, casse ignoree."""
    return " ".join(text.split()).upper()


def label_cache_key(label: str, model: str, context: str) -> str:
    """Clé d'UN libellé, pas du lot qui le transporte.

    La clé portait sur le lot de 25. Un seul produit nouveau au début du
    fichier décalait la composition de tous les lots suivants, et le dépôt du
    lendemain repayait le catalogue entier. Mesuré sur les 343 libellés
    réels, avec 5 % de nouveautés : 100 % des libellés étaient refacturés,
    contre 5 % avec la clé par libellé.

    `context` porte l'en-tête (consignes et taxonomies) : changer les
    consignes doit invalider les réponses, sinon on resservirait la réponse à
    une question qu'on ne pose plus.
    """
    return hashlib.sha256(f"{model}\x00{context}\x00{_squash(label)}".encode()).hexdigest()


class LlmClient:
    """Client d'appel. Ne connaît rien au métier : il exécute, compte, et cache."""

    def __init__(
        self,
        settings: LlmSettings,
        cache: CacheBackend | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache or MemoryCache()
        # Attendre un lot peut durer des minutes. Sans signal, l'interface
        # afficherait une etape figee et l'utilisateur conclurait a une panne.
        self.progress = progress
        self.usage = LlmUsage(
            model=settings.model, dry_run=settings.dry_run, batch_api=settings.batch_api
        )
        self._client: Any = None

    @property
    def available(self) -> bool:
        """Sans clé, l'étage LLM est sauté et le reste du pipeline continue.
        C'est la dégradation gracieuse : un catalogue à moitié enrichi vaut
        mieux qu'un run en échec."""
        return bool(self.settings.api_key) and not self.settings.dry_run

    def _anthropic(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.settings.api_key)
        return self._client

    def _budget_exhausted(self) -> bool:
        return (
            self.usage.calls_real >= self.settings.max_calls_per_run
            or self.usage.cost.total_usd >= self.settings.max_cost_usd
        )

    def complete_batch(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        cache_scope: str,
        store_batch: bool = True,
    ) -> dict[str, Any] | None:
        """Un appel, avec cache et comptabilité. Retourne None si rien n'a pu
        être obtenu (budget épuisé, pas de clé, ou échec après reprises).

        `store_batch=False` quand l'appelant range lui-même la réponse libellé
        par libellé : cacher aussi le lot entier doublerait le stockage pour
        une entrée qui ne resservira presque jamais, la composition des lots
        changeant d'un dépôt à l'autre.
        """
        key = cache_key(f"{cache_scope}\x00{system_prompt}\x00{user_prompt}", self.settings.model)

        if store_batch:
            cached = self.cache.get(key)
            if cached is not None:
                self.usage.calls_cached += 1
                return cached

        if self.settings.dry_run or not self.settings.api_key:
            self.usage.queued += 1
            return None

        if self._budget_exhausted():
            self.usage.queued += 1
            log.warning(
                "llm.budget_exhausted",
                calls=self.usage.calls_real,
                cost_usd=round(self.usage.cost.total_usd, 4),
            )
            return None

        started = time.perf_counter()
        for attempt in range(self.settings.max_retries):
            try:
                response = self._anthropic().messages.create(
                    model=self.settings.model,
                    max_tokens=8000,
                    system=[
                        {
                            "type": "text",
                            "text": system_prompt,
                            # L'en-tête est identique d'un lot à l'autre : le
                            # mettre en cache le facture ~10 % à partir du
                            # deuxième appel du run.
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    output_config={
                        "effort": self.settings.effort,
                        "format": {"type": "json_schema", "schema": schema},
                    },
                    messages=[{"role": "user", "content": user_prompt}],
                )

                # Un refus est une réponse valide côté HTTP : lire content[0]
                # sans vérifier stop_reason ferait planter le run.
                if response.stop_reason == "refusal":
                    self.usage.errors += 1
                    log.warning("llm.refusal", detail=str(getattr(response, "stop_details", "")))
                    return None

                usage = response.usage
                self.usage.calls_real += 1
                self.usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
                self.usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
                self.usage.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
                self.usage.cache_write_tokens += (
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                )

                text = next((b.text for b in response.content if b.type == "text"), "")
                parsed: dict[str, Any] = json.loads(text)

                self.cache.set(
                    key,
                    self.settings.model,
                    user_prompt,
                    parsed,
                    getattr(usage, "input_tokens", 0) or 0,
                    getattr(usage, "output_tokens", 0) or 0,
                )
                return parsed

            except json.JSONDecodeError:
                # La sortie est contrainte par schéma : un JSON invalide est
                # anormal et ne se répare pas en réessayant à l'identique.
                self.usage.errors += 1
                log.error("llm.invalid_json")
                return None
            except Exception as exc:
                if attempt == self.settings.max_retries - 1:
                    self.usage.errors += 1
                    self.usage.queued += 1
                    log.error("llm.failed", error=str(exc), attempts=attempt + 1)
                    return None
                # Repli exponentiel : 0,5 s, 1 s, 2 s.
                time.sleep(0.5 * (2**attempt))
            finally:
                self.usage.duration_ms += (time.perf_counter() - started) * 1000

        return None

    def complete_labels(
        self,
        system_prompt: str,
        labels: Sequence[str],
        schema: dict[str, Any],
        cache_scope: str,
        key_of: Callable[[dict[str, Any]], str],
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Enrichit une liste de libellés, en ne payant que les inconnus.

        Le cache est interrogé libellé par libellé ; seuls les manquants sont
        regroupés en lots et envoyés. Chaque réponse est ensuite rangée sous
        SON libellé, donc elle reste réutilisable même si la composition des
        lots change au dépôt suivant, ce qui est le cas dès qu'un produit
        apparaît ou disparaît.

        `key_of` extrait d'un item la clé sous laquelle le ranger : le client
        ne connaît pas la forme métier de la réponse.
        """
        context = hashlib.sha256(
            f"{cache_scope}\x00{' '.join(system_prompt.split())}".encode()
        ).hexdigest()

        found: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for label in labels:
            cached = self.cache.get(label_cache_key(label, self.settings.model, context))
            if cached is None:
                missing.append(label)
            else:
                found[label] = cached
                self.usage.labels_cached += 1

        self.usage.labels_total += len(labels)
        if missing:
            log.info(
                "llm.cache_lookup",
                labels=len(labels),
                served_by_cache=len(found),
                to_request=len(missing),
            )

        progress = progress or self.progress
        groups = self.batches(missing, self.settings.batch_size)
        if self.settings.batch_api and groups and self.available:
            payloads = self._run_via_batch_api(system_prompt, groups, schema, progress)
        else:
            payloads = {}
            for index, batch in enumerate(groups):
                payloads[index] = self.complete_batch(
                    system_prompt=system_prompt,
                    user_prompt=_user_prompt(batch),
                    schema=schema,
                    cache_scope=cache_scope,
                    store_batch=False,
                )

        for index, batch in enumerate(groups):
            payload = payloads.get(index)
            if payload is None:
                continue

            # Le modele reecho le libelle ; il lui arrive de le reecrire a
            # une espace ou une casse pres. Un rapprochement strict jetterait
            # une reponse deja payee, donc on retombe sur la forme normalisee.
            by_normalized = {_squash(label): label for label in batch}
            for item in payload.get("items", []):
                echoed = key_of(item)
                matched = echoed if echoed in batch else by_normalized.get(_squash(echoed))
                if matched is None:
                    self.usage.unmatched += 1
                    continue
                found[matched] = item
                # Une réponse par libellé : c'est ce qui la rend réutilisable.
                self.cache.set(
                    label_cache_key(matched, self.settings.model, context),
                    self.settings.model,
                    matched,
                    item,
                    0,
                    0,
                )

        return found

    def _run_via_batch_api(
        self,
        system_prompt: str,
        groups: list[list[str]],
        schema: dict[str, Any],
        progress: Callable[[str, dict[str, Any]], None] | None,
    ) -> dict[int, dict[str, Any] | None]:
        """Soumet tous les lots en une fois, attend, et collecte.

        Moitie prix contre de la latence. La reponse arrive souvent en quelques
        minutes, 24 h au maximum garanti : c'est acceptable pour un run que
        personne ne regarde, jamais pour un depot fait a la main.

        Si l'attente depasse le plafond, on renonce et le pipeline continue
        sans cet etage — la degradation gracieuse habituelle. L'identifiant du
        lot est conserve dans le rapport : le travail est paye et recuperable,
        il n'est pas perdu.
        """
        from anthropic.types import TextBlockParam
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        # Le SDK type ces champs precisement ; on annote pour que mypy strict
        # verifie la forme au lieu de la subir a l'execution.
        system: list[TextBlockParam] = [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        effort = cast(
            'Literal["low", "medium", "high", "xhigh", "max"]',
            self.settings.effort,
        )
        requests = [
            Request(
                custom_id=f"lot-{index}",
                params=MessageCreateParamsNonStreaming(
                    model=self.settings.model,
                    max_tokens=8000,
                    system=system,
                    output_config={
                        "effort": effort,
                        "format": {"type": "json_schema", "schema": schema},
                    },
                    messages=[{"role": "user", "content": _user_prompt(batch)}],
                ),
            )
            for index, batch in enumerate(groups)
        ]

        started = time.perf_counter()
        client = self._anthropic()
        try:
            submitted = client.messages.batches.create(requests=requests)
        except Exception as exc:
            self.usage.errors += 1
            self.usage.queued += len(groups)
            log.error("llm.batch_submit_failed", error=str(exc))
            return {}

        self.usage.batch_ids.append(submitted.id)
        log.info("llm.batch_submitted", batch_id=submitted.id, requests=len(requests))
        if progress:
            progress("batch.submitted", {"batch_id": submitted.id, "requests": len(requests)})

        deadline = started + self.settings.batch_timeout_s
        cancelled = False
        while True:
            state = client.messages.batches.retrieve(submitted.id)
            if state.processing_status == "ended":
                break
            if time.perf_counter() > deadline:
                if cancelled:
                    # Meme l'annulation ne converge pas : on renonce, l'id
                    # reste dans le rapport. Ce qui aura ete servi d'ici la
                    # fin sera facture sans etre compte — c'est exactement ce
                    # que ce chemin ne sait plus eviter, donc il le dit.
                    self.usage.queued += len(groups)
                    log.error("llm.batch_cancel_stuck", batch_id=submitted.id)
                    return {}
                # Renoncer a ATTENDRE n'est pas renoncer a COMPTER. Sans
                # annulation, le lot continue cote Anthropic : il est facture
                # en entier, hors du compteur — l'enveloppe croit cet argent
                # encore disponible. Annuler borne la facture (les requetes
                # pas encore servies ne sont pas facturees), et les reponses
                # deja servies restent collectables : payees de toute facon,
                # autant les compter ET les mettre en cache plutot que de les
                # racheter demain.
                cancelled = True
                deadline = time.perf_counter() + 120  # l'annulation doit converger
                log.warning(
                    "llm.batch_timeout",
                    batch_id=submitted.id,
                    waited_s=round(time.perf_counter() - started),
                )
                if progress:
                    progress("batch.timeout", {"batch_id": submitted.id})
                try:
                    client.messages.batches.cancel(submitted.id)
                except Exception as exc:
                    self.usage.queued += len(groups)
                    log.error("llm.batch_cancel_failed", batch_id=submitted.id, error=str(exc))
                    return {}
            if progress:
                counts = getattr(state, "request_counts", None)
                progress(
                    "batch.waiting",
                    {
                        "batch_id": submitted.id,
                        "succeeded": getattr(counts, "succeeded", 0),
                        "processing": getattr(counts, "processing", 0),
                        "waited_s": round(time.perf_counter() - started),
                    },
                )
            time.sleep(self.settings.batch_poll_s)

        payloads: dict[int, dict[str, Any] | None] = {}
        for result in client.messages.batches.results(submitted.id):
            index = int(str(result.custom_id).rsplit("-", 1)[-1])
            if result.result.type != "succeeded":
                self.usage.errors += 1
                log.warning(
                    "llm.batch_request_failed",
                    custom_id=result.custom_id,
                    type=result.result.type,
                )
                continue

            message = result.result.message
            # Un refus est une reponse valide cote transport : lire le contenu
            # sans verifier ferait tomber le run.
            if message.stop_reason == "refusal":
                self.usage.errors += 1
                log.warning("llm.refusal", custom_id=result.custom_id)
                continue

            usage = message.usage
            self.usage.calls_real += 1
            self.usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
            self.usage.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.usage.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

            text = next((block.text for block in message.content if block.type == "text"), "")
            try:
                payloads[index] = json.loads(text)
            except json.JSONDecodeError:
                self.usage.errors += 1
                log.error("llm.invalid_json", custom_id=result.custom_id)

        # Les lots jamais revenus (annules avant d'etre servis, ou en echec)
        # repartent en file : leurs libelles seront retentes au prochain run.
        missing = len(groups) - len(payloads)
        if missing:
            self.usage.queued += missing

        self.usage.duration_ms += (time.perf_counter() - started) * 1000
        log.info(
            "llm.batch_collected",
            batch_id=submitted.id,
            collected=len(payloads),
            requests=len(requests),
            cancelled=cancelled,
            cost_usd=round(self.usage.cost.total_usd, 4),
        )
        return payloads

    @staticmethod
    def batches(items: Sequence[str], size: int) -> list[list[str]]:
        return [list(items[i : i + size]) for i in range(0, len(items), size)]
