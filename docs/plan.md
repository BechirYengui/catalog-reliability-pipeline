# Plan d'implémentation — 5 phases

Établi le 2026-08-09, après profilage réel (`docs/profiling.md`).
Chaque phase : arborescence, ordre d'implémentation, tests, critère de sortie.

Principe transverse : **chaque étape est un module pur** `run(df, ctx) -> StepResult`
(DataFrame annoté + liste de `Correction` + métriques). Aucune étape n'écrit en base
elle-même : l'orchestrateur persiste. C'est ce qui les rend testables isolément.

---

## Phase 1 — Socle + ingestion + contrôles déterministes

**Objectif de sortie** : `pipeline run data/samples/store_listing_produit.csv --store-id demo`
produit un rapport JSON avec les compteurs de `docs/profiling.md` §9, sans LLM,
sans base (sortie fichier), en < 30 s.

```
pyproject.toml            uv, ruff, mypy strict sur pipeline/
Makefile                  dev / test / lint / pipeline / deploy
.env.example
pipeline/
  __init__.py
  models.py               Pydantic v2 : RawProduct, Correction(author RULE|LLM|HUMAN),
                          StepResult, RunReport, ProductStatus (enum)
  config.py               chargement YAML + env ; seuils par champ
  logging.py              structlog JSON, run_id lié au contexte
  context.py              RunContext (run_id, store_id, config, horloge injectable)
  steps/
    ingest.py             utf-8-sig, ftfy sur TOUTES les colonnes texte, CRLF,
                          validation du schéma de colonnes, trim,
                          détachement des suffixes (liste fermée)
    field_checks.py       EAN (checksum GS1 + réparation zéros de tête conditionnelle,
                          routage PLU), URL (schéma + HEAD async), TVA (taux légaux),
                          taxonomie (complétude)
    url_checker.py        httpx async, sémaphore PAR DOMAINE, cache négatif DOMAINE,
                          timeout + retry borné, cache mémoire (base en phase 3)
    report.py             agrégation des compteurs + seuil de QUARANTINE
  cli.py                  typer : `pipeline run <file> --store-id X [--no-network]`
config/
  taxonomy.yaml           les 10 chemins de référence (extraits du CSV)
  vat_rules.yaml          taux légaux FR + table catégorie→TVA attendue
  label_suffixes.yaml     ORIGINE ITALIA, FAMILY SIZE
tests/
  conftest.py             fixtures = extraits RÉELS du CSV
  data/                   ~40 lignes choisies couvrant chaque anomalie
  test_ingest.py          mojibake CAFÃ‰→CAFÉ ; Ã‰PICERIE→ÉPICERIE ; BOM ;
                          'LESSIVE 2LORIGINE ITALIA' → ('LESSIVE 2L','ITALIA')
  test_field_checks.py    checksum : '3760000000017' ok ; '789'→'0000000000789' ;
                          '31061' → PLU non réparé ; 'htp://'→'https://' ;
                          'ftp://' non réparé
  test_url_checker.py     cache négatif domaine : 443 URLs sur un domaine DNS-FAIL
                          → 1 seule résolution (assert sur le compteur d'appels)
  test_report.py          quarantine si taux d'anomalies > seuil
```

**Ordre** : `models` → `config` → `ingest` (+tests) → `field_checks` sans réseau (+tests)
→ `url_checker` (+tests avec transport httpx mocké) → `report` → `cli`.

**Critère de sortie** : `make test` vert, `make lint` vert (mypy strict), et les
compteurs du rapport correspondent au §9 du profilage à ±0.

---

## Phase 2 — Résolution d'entités + golden record

**Objectif de sortie** : les 10 000 lignes se regroupent en ~10 entités,
avec le funnel visible dans les logs.

```
pipeline/steps/
  normalize.py            clé de blocking = tokens normalisés + format extrait
  attributes.py           extraction déterministe : contenance+unité (1L, 70CL,
                          250G, 0.75KG, 1K), marque, conditionnement
  entity_resolution.py    exact → blocking → RapidFuzz → embeddings → arbitrage
  embeddings.py           sentence-transformers MiniLM multilingue, CPU, batch,
                          cache disque (le modèle ne se recharge pas à chaque run)
  survivorship.py         fusion en golden record
config/
  survivorship.yaml       règles ORDONNÉES et commentées :
                          libellé = le plus long non abrégé ; EAN = celui qui passe
                          le checksum (sinon le plus fréquent) ; URL = celle qui
                          répond ; taxonomie = la plus profonde ; TVA = JAMAIS fusionnée
                          automatiquement (conflit → review)
tests/
  data/coca_zero.csv      30 lignes réelles de la famille COCA (test qui échoue d'abord)
  test_attributes.py      'COCA COLA ZERO 1L' → (contenance=1, unité=L)
  test_entity_resolution.py  les 30 lignes → 1 entité ; le funnel décroît
  test_survivorship.py    golden record attendu champ par champ
```

**Ordre** : test rouge sur `coca_zero.csv` d'abord, puis `normalize` → `attributes`
→ blocking → RapidFuzz → embeddings → survivorship, en loguant à chaque cran le
nombre de paires restantes.

**Point de vigilance mesuré** : l'EAN ne peut PAS servir de clé (8 700 EAN tous
distincts pour 343 libellés). Un test assert explicitement qu'aucune règle de fusion
ne s'appuie sur l'égalité d'EAN.

**Critère de sortie** : 10 000 → ~10 entités, rappel vérifié sur la famille COCA,
funnel lisible dans les logs, < 60 s sur 2 vCPU.

---

## Phase 3 — Persistance + LLM + scoring

Cette phase introduit PostgreSQL (le pipeline devient stateful) puis le LLM.

```
db/
  alembic/                migrations
  models.py               stores, ingestion_runs, raw_products, products,
                          product_corrections, review_tasks, llm_cache,
                          label_embeddings (vector), url_check_cache
pipeline/
  repository.py           upserts transactionnels, idempotence (store_id, file_hash)
  steps/
    cross_checks.py       catégorie→TVA (matching PAR TOKEN — cf. le piège "GIN"
                          dans FRORIGINE), taxonomie↔nom, déduction n3→n2 (250 lignes),
                          consensus intra-cluster comme signal de confiance
    llm_enrich.py         appels sur les 343 libellés distincts uniquement
    scoring_routing.py    statut par produit, seuils par champ, TVA → NEEDS_REVIEW
  llm/
    client.py             Anthropic, tool use (JSON schema), retry+backoff,
                          budget max par run, compteur de tokens
    cache.py              sha256(prompt normalisé + modèle) → réponse, en base
    prompts/              français, taxonomie de référence injectée (liste fermée)
tests/
  test_cross_checks.py    NON-RÉGRESSION : 'TOM GRAPPE VRAC FRORIGINE ITALIA'
                          ne doit PAS être classé alcool
  test_llm_cache.py       2ᵉ run = 0 appel réel
  test_scoring.py         toute correction TVA → NEEDS_REVIEW, même à confiance 1.0
  test_idempotence.py     2 runs du même fichier = 0 nouveau golden record
```

**Ordre** : schéma + migrations → repository + test d'idempotence → `cross_checks`
(déterministe, sans LLM) → cache LLM → client LLM → prompts → `--dry-run` → scoring.

**Coût LLM mesuré à l'avance** : 343 libellés distincts, ~1 appel court chacun.
Un chiffrage réel (tokens in/out × tarif) sera produit par le `--dry-run` avant
le premier appel facturé.

**Critère de sortie** : `--dry-run` montre exactement ce qui partirait ; 2ᵉ run =
100 % cache ; rejouer le fichier ne crée aucun doublon.

---

## Phase 4 — API + interface de revue

```
api/
  main.py, deps.py, auth.py    sessions + rôles (admin = TVA, reviewer = le reste)
  routers/ runs.py review.py products.py metrics.py
  tests/                       httpx AsyncClient, base de test éphémère
web/
  src/pages/    Dashboard.tsx  Review.tsx  Catalog.tsx
  src/components/ DiffView.tsx  TaskCard.tsx  KeyboardHints.tsx
```

L'écran de revue est la pièce maîtresse : décisions **groupées** (« ces 7 lignes
de WHISKY ECOSSAIS 70 CL sont à 10 %, les 221 autres à 20 % → appliquer 20 % ? »),
raccourcis `a`/`r`/`e`, diff avant/après. Objectif : 20 décisions en < 2 min.

La **boucle d'apprentissage** est le point non négociable : une décision humaine
écrit dans `llm_cache` et/ou la table catégorie→TVA, pour que la question ne
revienne jamais. Un test le vérifie explicitement.

**Ordre** : API + tests d'abord, front ensuite. Démo bout en bout à la fin.

---

## Phase 5 — Durcissement + déploiement VPS

**Contrainte réelle constatée** : le VPS héberge déjà deux autres sites
derrière un **nginx hôte qui occupe 80/443**. Caddy ne peut pas prendre
ces ports.

→ **Adaptation du kit** : le stack catalog s'expose sur `127.0.0.1:33xx` et l'nginx
hôte existant sert de reverse proxy + TLS (certbot déjà en place). On ne touche
à aucun service existant.

```
docker-compose.prod.yml   postgres16+pgvector (volume + healthcheck),
                          api (uvicorn, non-root, réseau interne uniquement),
                          web (build statique), pas de Caddy → nginx hôte
scripts/deploy.sh         rsync (jamais .env ni .git) → build → alembic upgrade head
                          → healthcheck HTTP. Idempotent et verbeux.
scripts/backup.sh         pg_dump quotidien, rotation 14 jours
deploy/catalog.nginx      vhost à ajouter à l'nginx hôte
deploy/catalog.service    + .timer : traitement quotidien de /srv/catalog/inbox
```

Sur le VPS : `/srv/catalog/{inbox,processed,quarantine}`, `.env` en `chmod 600`
(valeurs listées, jamais inventées), vérification `docker history` (aucun secret
dans l'image). ufw : à **ne pas** reconfigurer à l'aveugle, le port SSH n'est pas 22
— un `ufw allow 22` suivi d'un `enable` couperait l'accès.

**Test final** : dépôt du CSV dans `inbox`, déclenchement manuel du timer, rapport
de run consulté via l'API en HTTPS.

---

## Points ouverts (décisions à prendre)

| # | Sujet | Options |
|---|---|---|
| 1 | Lieu d'exécution du dev | tout sur le VPS / local + déploiement |
| 2 | Domaine d'exposition | sous-domaine dédié / accès par tunnel SSH |
| 3 | Clé API Anthropic (phase 3) | fournie / mode `--dry-run` seulement |
