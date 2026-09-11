"""Tests du verificateur d'URL.

L'enjeu n'est pas seulement « est-ce que ca marche » mais « est-ce que ca coute
ce que ca doit couter » : sur le fichier reel, 525 URLs pointent vers 3 domaines
dont le DNS ne resout pas. Sans cache negatif au niveau du domaine, ce sont 525
timeouts de 5 s. Les tests ci-dessous verrouillent ce comportement.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from pipeline.config import HttpConfig
from pipeline.steps.url_checker import UrlChecker, UrlStatus, domain_of, is_supported, repair_url


class TestRepairUrl:
    @pytest.mark.parametrize(
        ("raw", "expected", "rule"),
        [
            (
                "htp://cdn.store.example/image.jpg",
                "https://cdn.store.example/image.jpg",
                "scheme_typo",
            ),
            ("www.store.example/a.jpg", "https://www.store.example/a.jpg", "scheme_missing"),
            (
                "https://picsum.photos/seed/apple-0/640/480",
                "https://picsum.photos/seed/apple-0/640/480",
                None,
            ),
            ("", "", None),
        ],
    )
    def test_repairs_only_known_typos(self, raw: str, expected: str, rule: str | None) -> None:
        assert repair_url(raw) == (expected, rule)

    def test_ftp_is_not_repaired(self) -> None:
        """'ftp://' n'est pas une faute de frappe mais un protocole qu'aucune
        marketplace ne consomme : le reparer serait masquer le probleme."""
        url, rule = repair_url("ftp://files.store.example/x.png")
        assert rule is None
        assert not is_supported(url)

    def test_not_a_url_stays_unsupported(self) -> None:
        url, _ = repair_url("not-a-url")
        assert not is_supported(url)


class TestDomainNegativeCache:
    """Le comportement qui evite 525 timeouts sur le fichier reel."""

    def test_one_dns_lookup_covers_every_url_of_a_dead_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def fake_resolve(domain: str) -> bool:
            calls.append(domain)
            # SUSPENSION OBLIGATOIRE. Sans elle, ce faux resolveur rend la main
            # sans jamais ceder le controle : les coroutines s'executent en
            # file indienne et le test passe meme si le cache est casse.
            # C'est exactement ce qui est arrive — le test etait vert pendant
            # que la production faisait 9 044 resolutions pour 6 domaines,
            # parce que le vrai `_resolve` suspend (getaddrinfo en executor).
            await asyncio.sleep(0)
            return False  # DNS FAIL, comme cdn.store.example dans le fichier reel

        monkeypatch.setattr("pipeline.steps.url_checker._resolve", fake_resolve)

        urls = [f"https://cdn.store.example/{i}.jpg" for i in range(443)]
        checker = UrlChecker(HttpConfig())
        results = asyncio.run(checker.check_many(urls))

        assert len(results) == 443
        assert all(not status.ok for status in results.values())
        assert all(status.reason == "domain_unresolvable" for status in results.values())
        # Le coeur du test : 443 URLs, 1 seule resolution, 0 requete HTTP.
        assert calls == ["cdn.store.example"]
        assert checker.dns_lookups == 1
        assert checker.http_requests == 0

    def test_dead_domain_does_not_block_a_live_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_resolve(domain: str) -> bool:
            return domain == "live.example"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        monkeypatch.setattr("pipeline.steps.url_checker._resolve", fake_resolve)
        transport = httpx.MockTransport(handler)

        async def scenario() -> dict[str, bool]:
            checker = UrlChecker(HttpConfig())
            async with httpx.AsyncClient(transport=transport) as client:
                statuses = await asyncio.gather(
                    checker.check("https://dead.example/a.jpg", client),
                    checker.check("https://live.example/b.jpg", client),
                )
            return {s.url: s.ok for s in statuses}

        results = asyncio.run(scenario())
        assert results["https://dead.example/a.jpg"] is False
        assert results["https://live.example/b.jpg"] is True


class TestHeadFallbackCache:
    """picsum.photos refuse HEAD. Sans memorisation, chacune de ses 8 600 images
    coute un aller-retour perdu : mesure avant correction, 17 553 requetes HTTP
    pour 9 045 URLs."""

    def test_head_rejection_is_remembered_for_the_whole_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(domain: str) -> bool:
            await asyncio.sleep(0)
            return True

        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(405) if request.method == "HEAD" else httpx.Response(206)

        monkeypatch.setattr("pipeline.steps.url_checker._resolve", fake_resolve)

        async def scenario() -> int:
            checker = UrlChecker(HttpConfig())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                for i in range(10):
                    await checker.check(f"https://cdn.example/{i}.jpg", client)
            return checker.http_requests

        requests = asyncio.run(scenario())
        # La 1re URL paie HEAD+GET, les 9 suivantes vont directement en GET.
        assert methods.count("HEAD") == 1
        assert methods.count("GET") == 10
        assert requests == 11  # et non 20


class TestUrlCache:
    def test_identical_urls_are_requested_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_resolve(domain: str) -> bool:
            return True

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200)

        monkeypatch.setattr("pipeline.steps.url_checker._resolve", fake_resolve)

        async def scenario() -> None:
            checker = UrlChecker(HttpConfig())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                for _ in range(5):
                    await checker.check("https://live.example/same.jpg", client)

        asyncio.run(scenario())
        assert len(seen) == 1


class TestSeedFromPreviousRuns:
    """La graine des runs precedents : ce qui rend le depot quotidien rapide.

    Le magasin redepose en gros les memes 9 000 images chaque matin. Une URL
    presente dans la graine ne coute NI resolution DNS NI requete — c'est
    mesurable, donc verrouille ici : le transport explose si on le touche.
    """

    def test_a_seeded_url_costs_no_network_at_all(self) -> None:
        seed = {
            "https://cdn.example/a.jpg": UrlStatus("https://cdn.example/a.jpg", True, "ok", 200),
            "https://cdn.example/morte.jpg": UrlStatus(
                "https://cdn.example/morte.jpg", False, "http_404", 404
            ),
        }

        async def scenario() -> dict[str, UrlStatus]:
            checker = UrlChecker(HttpConfig(), seed=seed)
            statuses = await checker.check_many(list(seed))
            # Les compteurs sont la preuve : aucune resolution, aucune requete.
            assert checker.seed_hits == 2
            assert checker.http_requests == 0
            assert checker.dns_lookups == 0
            # Rien de servi par la graine ne repart en persistance : il y est
            # deja, le reecrire rafraichirait un horodatage sans nouvelle
            # preuve.
            assert checker.fresh == {}
            return statuses

        statuses = asyncio.run(scenario())
        assert statuses["https://cdn.example/a.jpg"].ok is True
        assert statuses["https://cdn.example/morte.jpg"].ok is False

    def test_a_network_verdict_lands_in_fresh_for_persistence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(domain: str) -> bool:
            return True

        monkeypatch.setattr("pipeline.steps.url_checker._resolve", fake_resolve)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        async def scenario() -> UrlChecker:
            checker = UrlChecker(HttpConfig())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await checker.check("https://live.example/nouvelle.jpg", client)
                # Deterministe, aucun cout reseau : ne doit PAS etre persiste.
                await checker.check("ftp://vieux.example/z.jpg", client)
            return checker

        checker = asyncio.run(scenario())
        assert set(checker.fresh) == {"https://live.example/nouvelle.jpg"}


class TestRetryAndErrors:
    def test_timeout_is_retried_then_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_resolve(domain: str) -> bool:
            return True

        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ConnectTimeout("boom")

        monkeypatch.setattr("pipeline.steps.url_checker._resolve", fake_resolve)

        async def scenario() -> str:
            checker = UrlChecker(HttpConfig(max_retries=2))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                status = await checker.check("https://live.example/slow.jpg", client)
            return status.reason

        reason = asyncio.run(scenario())
        assert reason == "timeout"
        assert len(attempts) == 3  # 1 essai + 2 retries, borne

    def test_head_405_falls_back_to_ranged_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Certains CDN refusent HEAD ; sans repli on declarerait mortes des
        images parfaitement valides."""

        async def fake_resolve(domain: str) -> bool:
            return True

        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(405) if request.method == "HEAD" else httpx.Response(206)

        monkeypatch.setattr("pipeline.steps.url_checker._resolve", fake_resolve)

        async def scenario() -> bool:
            checker = UrlChecker(HttpConfig())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                status = await checker.check("https://live.example/pic.jpg", client)
            return status.ok

        assert asyncio.run(scenario()) is True
        assert methods == ["HEAD", "GET"]


class TestDomainOf:
    def test_extracts_host(self) -> None:
        assert domain_of("https://picsum.photos/seed/x/640/480") == "picsum.photos"
