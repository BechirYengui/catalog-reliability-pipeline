.DEFAULT_GOAL := help
UV := uv
SAMPLE := data/samples/store_listing_produit.csv

.PHONY: help install test lint format pipeline profile clean

help: ## Affiche cette aide
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Installe les dependances (sans l'extra entity, lourd)
	$(UV) sync --group dev

install-all: ## Installe tout, y compris sentence-transformers (phase 2)
	$(UV) sync --all-extras --group dev

test: ## Lance les tests
	$(UV) run pytest

lint: ## ruff + mypy strict
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

format: ## Formate le code
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

pipeline: ## Run complet sur l'echantillon (FILE=... pour un autre fichier)
	$(UV) run pipeline run $(or $(FILE),$(SAMPLE)) --store-id demo --pretty

pipeline-offline: ## Idem sans acces reseau
	$(UV) run pipeline run $(or $(FILE),$(SAMPLE)) --store-id demo --no-network --pretty

profile: ## Regenere les mesures de docs/profiling.md
	python3 scripts/profile_csv.py $(SAMPLE)
	python3 scripts/profile_deep.py $(SAMPLE)

clean: ## Supprime les caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
