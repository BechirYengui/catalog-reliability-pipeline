"""Verification d'accessibilite des URLs d'image.

Trois optimisations dictees par le profilage (docs/profiling.md §5), pas par
principe :

1. **Cache negatif au niveau du DOMAINE.** 443 URLs pointent sur
   `cdn.store.example`, dont le DNS ne resout pas. Une seule resolution suffit a
   invalider les 443. Sans ca : 443 timeouts, soit ~37 min a 5 s de timeout.
   Mesure : 525 requetes evitees pour 3 resolutions DNS.

2. **Limite de concurrence PAR DOMAINE.** 8 600 des 9 085 URLs sont sur
   `picsum.photos`. Une limite globale ne protege pas le tiers : sans limite par
   domaine on lui envoie 32 requetes simultanees en rafale et on finit blackliste.

3. **Deduplication + cache par URL.** 10 000 lignes ne portent que 9 085 URLs
   distinctes, et d'un jour sur l'autre le meme magasin redepose les memes.
   Le cache est ici en memoire ; il passera en base avec TTL en phase 3.
"""

from __future__ import annotations

import asyncio
import socket
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import httpx

from pipeline.config import HttpConfig
from pipeline.logging import get_logger

log = get_logger(__name__)

# Schemas reparables : le profilage montre 43 'htp://' et 82 URLs sans schema.
# 'ftp://' n'est PAS repare : ce n'est pas une faute de frappe, c'est un
# protocole que les marketplaces ne savent pas consommer.
_SCHEME_TYPOS = {"htp": "https", "htps": "https", "hhtp": "http", "htttp": "http"}


@dataclass(slots=True)
class UrlStatus:
    url: str
    ok: bool
    reason: str
    status_code: int | None = None


def repair_url(raw: str | None) -> tuple[str, str | None]:
    """Repare ce qui est reparable de facon deterministe.

    Retourne (url, regle_appliquee). `regle_appliquee` vaut None si rien n'a change.
    """
    value = (raw or "").strip()
    if not value:
        return "", None

    parsed = urlparse(value)

    if parsed.scheme in _SCHEME_TYPOS:
        fixed = urlunparse(parsed._replace(scheme=_SCHEME_TYPOS[parsed.scheme]))
        return fixed, "scheme_typo"

    # 'www.store.example/x.jpg' : urlparse le lit comme un chemin, pas un hote.
    if not parsed.scheme and not parsed.netloc and value.startswith("www."):
        return f"https://{value}", "scheme_missing"

    return value, None


def is_supported(url: str) -> bool:
    """Seuls http(s) sont exploitables par une marketplace."""
    return urlparse(url).scheme in ("http", "https")


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


async def _resolve(domain: str) -> bool:
    """Resolution DNS dans un thread : `getaddrinfo` est bloquant."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, socket.getaddrinfo, domain, None)
    except (socket.gaierror, UnicodeError):
        return False
    return True


class UrlChecker:
    """Verificateur avec cache. Une instance par run.

    `seed` porte les statuts deja etablis par un run precedent (charges de la
    base par l'orchestrateur, avec leur TTL deja applique) : une URL presente
    dans la graine ne coute NI resolution NI requete. C'est ce qui fait qu'un
    depot quotidien ne verifie que les URLs nouvelles — le magasin redepose en
    gros les memes 9 000 images chaque matin.

    `fresh` expose ce que CE run a etabli par le reseau, et seulement cela :
    c'est ce que l'orchestrateur persiste. Les statuts deterministes (schema
    inexploitable, pas d'hote) n'y figurent pas — les stocker ne ferait
    qu'occuper la table pour un calcul qui ne coute rien.
    """

    def __init__(
        self,
        config: HttpConfig,
        seed: Mapping[str, UrlStatus] | None = None,
        fresh_out: dict[str, UrlStatus] | None = None,
    ) -> None:
        self.config = config
        self._url_cache: dict[str, UrlStatus] = {}
        self._seed = dict(seed) if seed else {}
        self.fresh: dict[str, UrlStatus] = fresh_out if fresh_out is not None else {}
        self._domain_resolvable: dict[str, bool] = {}
        self._domain_tasks: dict[str, asyncio.Task[bool]] = {}
        self._domain_locks: dict[str, asyncio.Semaphore] = {}
        # Domaines dont on a constate qu'ils refusent HEAD : on y va directement
        # en GET, au lieu de payer un aller-retour perdu par URL.
        self._head_unsupported: set[str] = set()
        self.dns_lookups = 0
        self.http_requests = 0
        self.seed_hits = 0

    def _semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Semaphore(self.config.concurrency_per_domain)
        return self._domain_locks[domain]

    async def _domain_ok(self, domain: str) -> bool:
        """Resolution mutualisee : la PREMIERE URL d'un domaine cree la tache,
        les suivantes attendent le meme resultat.

        Un simple `if domain not in cache: cache[domain] = await _resolve(...)`
        ne suffit pas. Les 443 URLs d'un domaine partent dans le meme
        `asyncio.gather` : toutes franchissent le test avant que la premiere
        n'ait ecrit le cache, et chacune lance sa propre resolution. Mesure sur
        le fichier reel avant correction : 9 044 resolutions DNS pour 6 domaines.

        On memorise la TACHE, pas le resultat : elle est enregistree avant le
        moindre `await`, donc aucune autre coroutine ne peut s'intercaler.
        """
        cached = self._domain_resolvable.get(domain)
        if cached is not None:
            return cached

        task = self._domain_tasks.get(domain)
        if task is None:
            task = asyncio.create_task(_resolve(domain))
            self._domain_tasks[domain] = task
            self.dns_lookups += 1

        resolvable = await task
        if domain not in self._domain_resolvable:
            self._domain_resolvable[domain] = resolvable
            if not resolvable:
                log.warning("url.domain_unresolvable", domain=domain)
        return resolvable

    async def check(self, url: str, client: httpx.AsyncClient) -> UrlStatus:
        if url in self._url_cache:
            return self._url_cache[url]

        # Statut etabli par un run precedent : rien a payer.
        seeded = self._seed.get(url)
        if seeded is not None:
            self.seed_hits += 1
            self._url_cache[url] = seeded
            return seeded

        if not is_supported(url):
            status = UrlStatus(url, False, "scheme_unsupported")
            self._url_cache[url] = status
            return status

        domain = domain_of(url)
        if not domain:
            status = UrlStatus(url, False, "no_host")
            self._url_cache[url] = status
            return status

        # Le court-circuit qui evite les 443 timeouts.
        if not await self._domain_ok(domain):
            status = UrlStatus(url, False, "domain_unresolvable")
            self._url_cache[url] = status
            self.fresh[url] = status
            return status

        async with self._semaphore(domain):
            status = await self._request(url, client)
        self._url_cache[url] = status
        self.fresh[url] = status
        return status

    async def _request(self, url: str, client: httpx.AsyncClient) -> UrlStatus:
        domain = domain_of(url)
        last_error = "unknown"
        for attempt in range(self.config.max_retries + 1):
            try:
                # Une fois qu'un domaine a refuse HEAD, inutile de le lui
                # redemander pour chacune de ses 8 600 images : mesure sur le
                # fichier reel avant correction, 17 553 requetes pour 9 045 URLs.
                if domain in self._head_unsupported:
                    self.http_requests += 1
                    response = await client.get(
                        url, follow_redirects=True, headers={"Range": "bytes=0-0"}
                    )
                else:
                    self.http_requests += 1
                    response = await client.head(url, follow_redirects=True)
                    if response.status_code in (403, 405):
                        self._head_unsupported.add(domain)
                        self.http_requests += 1
                        response = await client.get(
                            url, follow_redirects=True, headers={"Range": "bytes=0-0"}
                        )
                ok = 200 <= response.status_code < 400
                return UrlStatus(
                    url,
                    ok,
                    "ok" if ok else f"http_{response.status_code}",
                    response.status_code,
                )
            except httpx.TimeoutException:
                last_error = "timeout"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            if attempt < self.config.max_retries:
                await asyncio.sleep(0.2 * (2**attempt))  # backoff exponentiel borne
        return UrlStatus(url, False, last_error)

    async def check_many(self, urls: list[str]) -> dict[str, UrlStatus]:
        """Verifie une liste d'URLs. Deduplique et regroupe par domaine avant
        d'attaquer le reseau."""
        unique = [u for u in dict.fromkeys(urls) if u]
        if not unique:
            return {}

        by_domain: dict[str, list[str]] = defaultdict(list)
        for url in unique:
            by_domain[domain_of(url)].append(url)
        log.info(
            "url.check_start",
            urls_total=len(urls),
            urls_unique=len(unique),
            domains=len(by_domain),
            largest_domain=max(((len(v), k) for k, v in by_domain.items()), default=(0, ""))[1],
        )

        limits = httpx.Limits(max_connections=self.config.global_concurrency)
        timeout = httpx.Timeout(self.config.timeout_s)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            results = await asyncio.gather(*(self.check(u, client) for u in unique))

        mapping = {status.url: status for status in results}
        reachable = sum(1 for s in results if s.ok)
        log.info(
            "url.check_done",
            checked=len(mapping),
            reachable=reachable,
            unreachable=len(mapping) - reachable,
            dns_lookups=self.dns_lookups,
            http_requests=self.http_requests,
            # Les deux economies, separement : ce que la graine d'un run
            # precedent a servi, et ce que la dedup de CE run a evite. Un
            # cache dont on n'observe pas le taux de succes est un cache dont
            # on ne sait pas s'il existe.
            served_by_seed=self.seed_hits,
            requests_saved=len(unique) - self.http_requests,
        )
        return mapping
