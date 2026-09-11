"""Tests de l'étage sémantique : comptabilité, cache, garde-fous.

Le coût est une donnée de production comme une autre : s'il n'est pas testé,
il dérive sans que personne ne s'en aperçoive.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from pipeline.context import RunContext
from pipeline.llm.client import LlmClient, LlmSettings, LlmUsage, MemoryCache, cache_key
from pipeline.llm.pricing import CATALOG, DEFAULT_MODEL, compute_cost, pricing_for
from pipeline.steps import llm_enrich


class TestPricing:
    def test_default_model_is_in_the_catalog(self) -> None:
        assert DEFAULT_MODEL in CATALOG

    def test_cost_matches_the_published_rate(self) -> None:
        """1 M jetons d'entrée sur Opus 5 = 5 $, 1 M de sortie = 25 $."""
        cost = compute_cost("claude-opus-5", input_tokens=1_000_000, output_tokens=0)
        assert round(cost.total_usd, 2) == 5.00
        cost = compute_cost("claude-opus-5", input_tokens=0, output_tokens=1_000_000)
        assert round(cost.total_usd, 2) == 25.00

    def test_cache_read_is_far_cheaper_than_fresh_input(self) -> None:
        """C'est ce qui rend le cache rentable dès le deuxième passage."""
        fresh = compute_cost("claude-opus-5", input_tokens=100_000, output_tokens=0)
        cached = compute_cost(
            "claude-opus-5", input_tokens=0, output_tokens=0, cache_read_tokens=100_000
        )
        assert cached.total_usd < fresh.total_usd / 5

    def test_unknown_model_does_not_crash_a_run(self) -> None:
        """Un modèle inconnu ne doit pas interrompre un traitement pour une
        question de comptabilité — on facture au tarif par défaut."""
        assert pricing_for("modele-qui-nexiste-pas").model_id == DEFAULT_MODEL


class TestCacheKey:
    def test_key_includes_the_model(self) -> None:
        """La même question posée à deux modèles n'a pas la même réponse :
        resservir l'une pour l'autre fausserait qualité ET comptabilité."""
        assert cache_key("bonjour", "claude-opus-5") != cache_key("bonjour", "claude-haiku-4-5")

    def test_whitespace_does_not_change_the_key(self) -> None:
        assert cache_key("a  b\n c", "m") == cache_key("a b c", "m")


class TestBudgetGuards:
    def test_no_api_key_means_no_call_and_no_cost(self) -> None:
        """Dégradation gracieuse : sans clé, l'étage est sauté et le reste du
        pipeline continue."""
        client = LlmClient(LlmSettings(api_key=""))
        result = client.complete_batch("sys", "user", {}, "scope")
        assert result is None
        assert client.usage.calls_real == 0
        assert client.usage.cost.total_usd == 0.0
        assert client.usage.queued == 1

    def test_cache_hit_costs_nothing_and_is_counted(self) -> None:
        cache = MemoryCache()
        settings = LlmSettings(api_key="sk-test")
        key = cache_key("scope\x00sys\x00user", settings.model)
        cache.set(key, settings.model, "user", {"items": []}, 0, 0)

        client = LlmClient(settings, cache)
        assert client.complete_batch("sys", "user", {}, "scope") == {"items": []}
        assert client.usage.calls_cached == 1
        assert client.usage.calls_real == 0
        assert client.usage.cost.total_usd == 0.0

    def test_un_run_entierement_recycle_se_distingue_d_un_etage_saute(self) -> None:
        """Zero appel ne veut pas dire zero travail.

        Quand tous les libelles sortent du cache par libelle, `calls_real` et
        `calls_cached` valent 0 : l'interface en concluait « etage non execute,
        aucune cle API », juste au-dessous d'une etape annoncant 7 711
        corrections. C'est `labels_cached` qui distingue les deux situations,
        et le rapport doit donc le porter.
        """
        from pipeline.llm.client import LlmUsage

        recycle = LlmUsage(model="claude-opus-5", labels_total=273, labels_cached=273)
        saute = LlmUsage(model="claude-opus-5", queued=8)

        assert recycle.to_dict()["labels_cached"] == 273
        assert recycle.to_dict()["calls_real"] == 0
        assert recycle.to_dict()["cost_usd"] == 0.0
        assert recycle.to_dict()["cache_hit_rate"] == 1.0

        assert saute.to_dict()["labels_cached"] == 0
        assert saute.to_dict()["queued"] == 8

    def test_spend_ceiling_stops_further_calls(self) -> None:
        """Un run ne peut pas dépenser sans plafond parce qu'un fichier
        d'entrée était inhabituel."""
        client = LlmClient(LlmSettings(api_key="sk-test", max_cost_usd=0.001))
        client.usage.input_tokens = 10_000_000  # bien au-delà du plafond
        assert client.complete_batch("sys", "user", {}, "scope") is None
        assert client.usage.queued == 1

    def test_call_ceiling_stops_further_calls(self) -> None:
        client = LlmClient(LlmSettings(api_key="sk-test", max_calls_per_run=2))
        client.usage.calls_real = 2
        assert client.complete_batch("sys", "user", {}, "scope") is None


class TestBatching:
    def test_labels_are_grouped(self) -> None:
        """273 libellés en lots de 25 = 11 appels au lieu de 273."""
        batches = LlmClient.batches([str(i) for i in range(273)], 25)
        assert len(batches) == 11
        assert sum(len(b) for b in batches) == 273


class TestDryRunEstimate:
    def test_estimate_reports_cost_before_any_paid_call(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        estimate = llm_enrich.estimate_dry_run(ingested, ctx)
        assert estimate["distinct_labels"] > 0
        assert estimate["estimated_cost_usd"] >= 0
        assert estimate["model"] == ctx.config.llm.model

    def test_batching_is_cheaper_than_one_call_per_label(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        estimate = llm_enrich.estimate_dry_run(ingested, ctx)
        assert estimate["estimated_cost_usd"] < estimate["estimated_cost_without_batching_usd"]


class TestEnrichStep:
    def test_only_distinct_labels_reach_the_llm(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """La règle d'économie centrale du projet, verrouillée par un test."""
        seen: list[list[str]] = []

        class SpyClient(LlmClient):
            def complete_batch(
                self,
                system_prompt: str,
                user_prompt: str,
                schema: Any,
                cache_scope: str,
                store_batch: bool = True,
            ) -> dict[str, Any] | None:
                seen.append(
                    [line[2:] for line in user_prompt.splitlines() if line.startswith("- ")]
                )
                return None

        spy = SpyClient(LlmSettings(api_key="sk-test"))
        result = llm_enrich.run(ingested, ctx, client=spy)

        sent = [label for batch in seen for label in batch]
        assert len(sent) == len(set(sent)), "un libellé a été envoyé deux fois"
        assert len(sent) < ingested.height
        assert result.metrics is not None
        assert result.metrics.counters["distinct_labels"] == len(sent)

    def test_low_confidence_proposals_are_not_applied(
        self, ingested: pl.DataFrame, ctx: RunContext
    ) -> None:
        """Sous le seuil, la proposition part en revue au lieu d'être appliquée :
        un libellé mal réécrit se voit en rayon."""
        labels = [label for label in ingested["nom"].to_list() if label][:5]

        class LowConfidenceClient(LlmClient):
            def complete_batch(
                self,
                system_prompt: str,
                user_prompt: str,
                schema: Any,
                cache_scope: str,
                store_batch: bool = True,
            ) -> dict[str, Any] | None:
                return {
                    "items": [
                        {
                            "source": label,
                            "label": f"Proposition douteuse {i}",
                            "taxonomy": "",
                            "brand": "",
                            "quantity_value": 0,
                            "quantity_unit": "",
                            "confidence": 0.2,
                        }
                        for i, label in enumerate(labels)
                    ]
                }

        result = llm_enrich.run(
            ingested, ctx, client=LowConfidenceClient(LlmSettings(api_key="sk-test"))
        )
        assert not [c for c in result.corrections if c.author == "LLM"]

    def test_llm_never_touches_the_vat(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        """Règle absolue : le champ TVA n'est même pas soumis au modèle."""
        assert "tva" not in llm_enrich.SYSTEM_PROMPT.lower().split("jamais")[0].split("`")
        assert "JAMAIS de taux de TVA" in llm_enrich.SYSTEM_PROMPT

        result = llm_enrich.run(ingested, ctx, client=LlmClient(LlmSettings(api_key="")))
        assert not [c for c in result.corrections if c.field_name == "tva"]

    def test_usage_is_published_for_display(self, ingested: pl.DataFrame, ctx: RunContext) -> None:
        """Sans ces chiffres, l'interface ne peut rien montrer du coût."""
        result = llm_enrich.run(ingested, ctx, client=LlmClient(LlmSettings(api_key="")))
        usage = result.payload["llm_usage"]
        for field_name in ("model", "cost_usd", "calls_real", "calls_cached", "cache_hit_rate"):
            assert field_name in usage


class TestDemoBudget:
    """L'enveloppe est CUMULÉE sur toute la démonstration.

    Un plafond par run ne protège de rien tout seul : douze runs à 0,44 $ sous
    un plafond de 5 $ chacun dépassent quand même une enveloppe de 5 $.
    """

    def test_remaining_is_limit_minus_spent(self) -> None:
        from api.budget import BudgetState

        state = BudgetState(limit_usd=5.0, spent_usd=1.25, runs_charged=3)
        assert state.remaining_usd == 3.75
        assert not state.exhausted

    def test_exhausted_when_nothing_meaningful_is_left(self) -> None:
        from api.budget import BudgetState

        # Marge d'un dixième de centime : sous ce seuil il ne reste pas de quoi
        # payer un appel utile, et laisser filer un résidu inviterait à le
        # dépasser.
        assert BudgetState(limit_usd=5.0, spent_usd=5.0, runs_charged=9).exhausted
        assert BudgetState(limit_usd=5.0, spent_usd=4.9999, runs_charged=9).exhausted
        assert not BudgetState(limit_usd=5.0, spent_usd=4.99, runs_charged=9).exhausted

    def test_overspend_never_reports_a_negative_remainder(self) -> None:
        from api.budget import BudgetState

        state = BudgetState(limit_usd=5.0, spent_usd=6.5, runs_charged=12)
        assert state.remaining_usd == 0.0
        assert state.used_ratio == 1.0

    def test_default_limit_is_five_dollars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Le chiffre affiché dans le tableau de bord vient d'ici : le figer
        dans un test évite qu'il dérive du plafond réellement appliqué."""
        from api.budget import DEFAULT_BUDGET_USD, budget_limit

        assert DEFAULT_BUDGET_USD == 5.0
        monkeypatch.delenv("LLM_BUDGET_USD", raising=False)
        assert budget_limit() == 5.0
        # Une valeur illisible ne doit pas ouvrir l'enveloppe en grand.
        monkeypatch.setenv("LLM_BUDGET_USD", "cinq dollars")
        assert budget_limit() == 5.0
        monkeypatch.setenv("LLM_BUDGET_USD", "2.5")
        assert budget_limit() == 2.5

    def test_run_ceiling_is_capped_by_what_remains(self) -> None:
        """Le plafond d'un run ne dépasse jamais le reliquat de l'enveloppe :
        c'est cette borne qui empêche le dernier run de la faire exploser."""
        from api.budget import BudgetState

        state = BudgetState(limit_usd=5.0, spent_usd=4.8, runs_charged=8)
        settings = LlmSettings(api_key="sk-test", max_cost_usd=5.0)
        settings.max_cost_usd = min(settings.max_cost_usd, state.remaining_usd)
        assert round(settings.max_cost_usd, 4) == 0.2

        client = LlmClient(settings)
        client.usage.input_tokens = 100_000  # 0,50 $ sur Opus 5 : au-delà du reliquat
        assert client.complete_batch("sys", "user", {}, "scope") is None


class TestLabelLevelCache:
    """Le cache porte sur le libellé, pas sur le lot qui le transporte.

    Mesure a l'origine de ce test : avec une cle par lot de 25, un depot du
    lendemain comportant 5 % de nouveautes repayait 100 % des libelles, parce
    qu'un produit insere en debut de fichier decale la composition de tous les
    lots suivants. Avec une cle par libelle, il n'en repaie que 5 %.
    """

    @staticmethod
    def _client(cache: MemoryCache, sent: list[list[str]]) -> LlmClient:
        class Recorder(LlmClient):
            def complete_batch(
                self,
                system_prompt: str,
                user_prompt: str,
                schema: Any,
                cache_scope: str,
                store_batch: bool = True,
            ) -> dict[str, Any] | None:
                batch = [line[2:] for line in user_prompt.splitlines() if line.startswith("- ")]
                sent.append(batch)
                self.usage.calls_real += 1
                return {
                    "items": [
                        {
                            "source": label,
                            "label": label.title(),
                            "taxonomy": "",
                            "brand": "",
                            "quantity_value": 0,
                            "quantity_unit": "",
                            "confidence": 0.95,
                        }
                        for label in batch
                    ]
                }

        return Recorder(LlmSettings(api_key="sk-test", batch_size=25), cache)

    def _enrich(self, cache: MemoryCache, labels: list[str], sent: list[list[str]]) -> None:
        self._client(cache, sent).complete_labels(
            system_prompt="consignes",
            labels=labels,
            schema={},
            cache_scope="llm_enrich.v1",
            key_of=lambda item: str(item.get("source", "")),
        )

    def test_a_second_deposit_only_pays_for_what_is_new(self) -> None:
        cache = MemoryCache()
        day1 = [f"PRODUIT {i} 500G" for i in range(100)]

        first: list[list[str]] = []
        self._enrich(cache, day1, first)
        assert sum(len(b) for b in first) == 100, "le premier depot paie tout"

        # Le lendemain : cinq nouveautes inserees EN TETE, donc toute la
        # composition des lots est decalee. C'est precisement le cas qui
        # mettait l'ancien cache en echec.
        day2 = [f"NOUVEAU {i}" for i in range(5)] + day1
        second: list[list[str]] = []
        self._enrich(cache, day2, second)

        repaid = [label for batch in second for label in batch]
        assert sorted(repaid) == sorted(f"NOUVEAU {i}" for i in range(5))

    def test_changing_the_instructions_invalidates_the_cache(self) -> None:
        """Une reponse cachee repond a une question precise.

        Si les consignes changent, resservir l'ancienne reponse reviendrait a
        repondre a une question qu'on ne pose plus.
        """
        cache = MemoryCache()
        labels = ["CAFE MLU 250G"]

        first: list[list[str]] = []
        self._enrich(cache, labels, first)

        second: list[list[str]] = []
        self._client(cache, second).complete_labels(
            system_prompt="consignes DIFFERENTES",
            labels=labels,
            schema={},
            cache_scope="llm_enrich.v1",
            key_of=lambda item: str(item.get("source", "")),
        )
        assert second == [labels], "le libelle doit etre repose au modele"

    def test_a_response_echoed_with_different_spacing_is_still_matched(self) -> None:
        """Le modele reecho le libelle ; il lui arrive de le reecrire.

        Un rapprochement strict jetterait une reponse deja payee et laisserait
        le libelle non enrichi sans que rien ne le signale.
        """
        cache = MemoryCache()

        class SloppyEcho(LlmClient):
            def complete_batch(
                self,
                system_prompt: str,
                user_prompt: str,
                schema: Any,
                cache_scope: str,
                store_batch: bool = True,
            ) -> dict[str, Any] | None:
                return {
                    "items": [
                        {
                            "source": "cafe   mlu 250g",  # espaces et casse differents
                            "label": "Café moulu 250 g",
                            "taxonomy": "",
                            "brand": "",
                            "quantity_value": 250,
                            "quantity_unit": "G",
                            "confidence": 0.95,
                        }
                    ]
                }

        client = SloppyEcho(LlmSettings(api_key="sk-test"), cache)
        found = client.complete_labels(
            system_prompt="consignes",
            labels=["CAFE MLU 250G"],
            schema={},
            cache_scope="llm_enrich.v1",
            key_of=lambda item: str(item.get("source", "")),
        )
        assert found["CAFE MLU 250G"]["label"] == "Café moulu 250 g"
        assert client.usage.unmatched == 0


class TestBatchApi:
    """Le mode lot : moitie prix contre de la latence.

    Ce n'est pas un reglage technique mais un arbitrage cout/delai, choisi au
    depot. Les tests portent donc sur ce qui compte pour celui qui choisit :
    le tarif applique, et ce qui arrive quand l'attente est trop longue.
    """

    def test_batch_pricing_is_half(self) -> None:
        plein = compute_cost("claude-opus-5", 10_000, 10_000)
        lot = compute_cost("claude-opus-5", 10_000, 10_000, batch_api=True)
        assert lot.total_usd == pytest.approx(plein.total_usd / 2)

    def test_the_run_is_billed_at_the_rate_it_actually_used(self) -> None:
        """La comptabilite doit connaitre le mode, sinon elle affiche le double."""
        usage = LlmUsage(model="claude-opus-5", output_tokens=23_254, batch_api=True)
        plein = LlmUsage(model="claude-opus-5", output_tokens=23_254)
        assert usage.cost.total_usd == pytest.approx(plein.cost.total_usd / 2)
        assert usage.to_dict()["batch_api"] is True

    def test_a_batch_that_never_answers_degrades_instead_of_failing(self) -> None:
        """Depassement du delai : le pipeline continue sans l'etage LLM.

        Et l'identifiant du lot est conserve. Le travail est paye cote
        Anthropic ; le perdre de vue serait payer deux fois.
        """
        submitted = SimpleNamespace(id="msgbatch_123")
        counts = SimpleNamespace(succeeded=0, processing=8)

        class NeverEnds:
            class messages:
                class batches:
                    @staticmethod
                    def create(requests: Any) -> Any:
                        return submitted

                    @staticmethod
                    def retrieve(batch_id: str) -> Any:
                        return SimpleNamespace(
                            processing_status="in_progress", request_counts=counts
                        )

        client = LlmClient(
            LlmSettings(api_key="sk-test", batch_api=True, batch_timeout_s=0, batch_poll_s=0)
        )
        client._client = NeverEnds()

        seen: list[str] = []
        found = client.complete_labels(
            system_prompt="consignes",
            labels=["CAFE MLU 250G"],
            schema={},
            cache_scope="llm_enrich.v1",
            key_of=lambda item: str(item.get("source", "")),
            progress=lambda kind, data: seen.append(kind),
        )

        assert found == {}, "aucune reponse, mais pas d'exception"
        assert client.usage.queued == 1
        assert client.usage.batch_ids == ["msgbatch_123"]
        assert "batch.timeout" in seen

    def test_an_expired_batch_is_cancelled_and_its_partial_work_is_counted(self) -> None:
        """Le trou que ce test ferme : un lot abandonne sur expiration etait
        facture cote Anthropic sans jamais atteindre le compteur.

        Renoncer a ATTENDRE n'est pas renoncer a COMPTER. Le lot est annule —
        ce qui borne la facture aux requetes deja servies — et ces reponses,
        payees de toute facon, sont collectees : jetons comptes, libelles mis
        en cache. Le reste repart en file pour le prochain run.
        """
        submitted = SimpleNamespace(id="msgbatch_456")
        journal: list[str] = []

        served = SimpleNamespace(
            custom_id="lot-0",
            result=SimpleNamespace(
                type="succeeded",
                message=SimpleNamespace(
                    stop_reason="end_turn",
                    usage=SimpleNamespace(input_tokens=1_000, output_tokens=200),
                    content=[
                        SimpleNamespace(
                            type="text",
                            text='{"items": [{"source": "CAFE MLU 250G", "label": "Café"}]}',
                        )
                    ],
                ),
            ),
        )

        class ExpiresThenCancels:
            class messages:
                class batches:
                    @staticmethod
                    def create(requests: Any) -> Any:
                        return submitted

                    @staticmethod
                    def retrieve(batch_id: str) -> Any:
                        # « ended » seulement une fois l'annulation demandee :
                        # avant elle, le lot traine, c'est le scenario du trou.
                        status = "ended" if "cancel" in journal else "in_progress"
                        return SimpleNamespace(processing_status=status, request_counts=None)

                    @staticmethod
                    def cancel(batch_id: str) -> Any:
                        journal.append("cancel")
                        return SimpleNamespace(id=batch_id)

                    @staticmethod
                    def results(batch_id: str) -> Any:
                        # Un lot sur deux a ete servi avant l'annulation.
                        return iter([served])

        cache = MemoryCache()
        client = LlmClient(
            LlmSettings(
                api_key="sk-test", batch_api=True, batch_size=1, batch_timeout_s=0, batch_poll_s=0
            ),
            cache,
        )
        client._client = ExpiresThenCancels()

        found = client.complete_labels(
            system_prompt="consignes",
            labels=["CAFE MLU 250G", "THE VRT 100G"],
            schema={},
            cache_scope="llm_enrich.v1",
            key_of=lambda item: str(item.get("source", "")),
        )

        assert journal == ["cancel"], "le lot expire est annule, une seule fois"
        # La moitie servie est comptee ET utilisable.
        assert found == {"CAFE MLU 250G": {"source": "CAFE MLU 250G", "label": "Café"}}
        assert client.usage.calls_real == 1
        assert client.usage.input_tokens == 1_000
        assert client.usage.output_tokens == 200
        # L'autre moitie repart en file, elle n'a pas ete facturee.
        assert client.usage.queued == 1
        assert client.usage.batch_ids == ["msgbatch_456"]

    def test_a_submission_failure_does_not_break_the_run(self) -> None:
        class Refuses:
            class messages:
                class batches:
                    @staticmethod
                    def create(requests: Any) -> Any:
                        raise RuntimeError("service indisponible")

        client = LlmClient(LlmSettings(api_key="sk-test", batch_api=True))
        client._client = Refuses()

        found = client.complete_labels(
            system_prompt="consignes",
            labels=["CAFE MLU 250G"],
            schema={},
            cache_scope="llm_enrich.v1",
            key_of=lambda item: str(item.get("source", "")),
        )
        assert found == {}
        assert client.usage.errors == 1
        assert client.usage.calls_real == 0
