# Catalog Reliability Pipeline

**Fiabiliser un catalogue magasin de 10 000 lignes coûte environ 0,44 $ de LLM
au lieu de 16 $, puis 0,02 $ le lendemain : le LLM ne paie que les 273 libellés
distincts qu'aucune règle ne sait corriger, jamais les 10 000 lignes.**

| | Tout envoyer au LLM | Ce pipeline |
|---|---|---|
| Ce que reçoit le LLM | 10 000 lignes, 400 appels | 273 libellés, 11 appels |
| Premier dépôt d'un magasin | ≈ 16 $ | ≈ 0,44 $ — ≈ 0,22 $ en traitement de nuit |
| Dépôt du lendemain, 5 % de libellés nouveaux | ≈ 16 $ | ≈ 0,02 $ (le reste sort du cache) |

Chiffrage à blanc du pipeline lui-même (`estimate_dry_run`) sur le fichier réel
fourni, avec Claude Opus 5 au tarif relevé le 2026-06-24 : ±25 % sur les
montants. Les volumes, eux, sont des comptages exacts, et les 11 appels ont été
constatés sur les runs de production. Chaque chiffre de ce tableau est recalculé
en intégration continue par `tests/test_readme_claims.py`.

Ce que le coût ne dit pas :

- **10 000 lignes en 2 secondes** hors réseau, 4 minutes en vérifiant les
  9 085 URLs d'images.
- **Une facture plafonnée par construction** : une enveloppe cumulée de 5 $
  couvre tous les runs ; une fois épuisée, l'étage LLM s'arrête et le reste du
  pipeline va au bout.
- **L'humain ne tranche que ce dont l'erreur coûte cher.** La TVA n'est jamais
  modifiée automatiquement, et chaque correction garde sa valeur d'origine, son
  auteur (règle, LLM ou humain) et son horodatage.

---

## Ce que c'est

Fiabilisation automatisée de catalogues produits CSV avant intégration dans un
référentiel unique. Architecture en trois étages : **règles déterministes →
enrichissement LLM → validation humaine**.

Cas d'usage : un SaaS qui synchronise les catalogues de magasins vers des
marketplaces (Uber Eats, Deliveroo). Chaque magasin dépose un CSV quotidien
d'environ 10 000 lignes, qui doit être contrôlé, corrigé et enrichi avant
publication.

Le projet livre deux choses : un **moteur de pipeline** utilisable en ligne de
commande, et une **plateforme web** (API + interface de revue) qui l'expose à
des utilisateurs métier. Les deux partagent le même code de traitement.

---

## Le principe

Chaque anomalie est traitée à l'étage le moins cher qui sait la résoudre.

| Étage | Traite | Exemple |
|---|---|---|
| **Règles déterministes** | Ce qui est vérifiable mécaniquement | Checksum GS1, schéma d'URL, taux de TVA légal |
| **LLM** | Ce qui demande de comprendre du langage | `PL EMMENTAL RAP 200G` → `Emmental râpé 200 g` |
| **Humain** | Ce dont l'erreur coûte trop cher | Toute correction de TVA, sans exception |

Un LLM ne sert pas à valider un checksum : c'est plus lent, plus cher et moins
fiable qu'une fonction de dix lignes. Inversement, aucune règle ne devinera que
`PL` signifie « plaquette ». Chaque étage fait ce qu'il fait le mieux.

## Ce que dit le fichier réel

Le profilage de l'échantillon fourni (`docs/profiling.md`) a produit trois
constats qui déterminent toute l'architecture :

- **10 000 lignes → 343 libellés distincts → 10 familles produit.** Une fois
  les suffixes marketing détachés à l'ingestion, il reste 273 libellés : c'est
  tout ce que voit le LLM, soit 37 fois moins que de lignes.
- **Les 8 700 EAN non vides sont tous uniques**, alors qu'un même libellé apparaît
  jusqu'à 244 fois. L'EAN identifie la ligne, pas le produit — il ne peut pas
  servir de clé de rapprochement.
- **9 085 URLs réparties sur 6 hôtes seulement**, dont 3 dont le DNS ne résout
  pas. Un cache négatif au niveau du domaine remplace 525 requêtes vouées à
  l'échec par 3 résolutions DNS.

Aucune règle de ce projet n'a été écrite sans une mesure derrière.

---

## Le pipeline, étape par étape

Huit étapes, dans cet ordre. Chacune est un module pur : elle annote un
DataFrame Polars, journalise ses corrections et ses anomalies, et n'écrit
jamais en base. C'est ce qui la rend testable seule.

| # | Étape | Nature | Ce qu'elle fait |
|---|---|---|---|
| 1 | `ingest` | règles | Encodage, BOM, mojibake, suffixes marketing collés au libellé |
| 2 | `field_checks` | règles | EAN (checksum GS1), URL (DNS + HEAD), TVA, taxonomie |
| 3 | `cross_checks` | règles | Catégorie ↔ TVA, taxonomie ↔ nom, comblement des trous |
| 4 | `llm_enrich` | sémantique | Libellés caisse, taxonomie manquante, marque, contenance |
| 5 | `dedup_exact` | règles | Regroupement après normalisation, règles de survivorship |
| 6 | `entity_resolution` | sémantique | Blocking par contenance, RapidFuzz sur le libellé enrichi |
| 7 | `scoring_routing` | règles | Statut par produit, seuils par champ |
| 8 | `report` | règles | Taux publiable, anomalies par champ, garde-fou quarantaine |

L'enrichissement LLM passe **avant** la déduplication : il travaille sur des
libellés distincts, et un libellé abrégé rapproché de sa forme complète donne
un regroupement plus juste à l'étape suivante.

Pas d'embeddings : le LLM voit déjà chaque libellé distinct une fois et comble
le fossé sémantique que les vecteurs devaient combler. La dépendance
`sentence-transformers` reste disponible en extra, elle n'est pas utilisée.

---

## Démarrage rapide

### Le moteur seul, sans base ni serveur

```bash
# 1. Installer l'outillage (uv gère aussi la version de Python)
curl -LsSf https://astral.sh/uv/install.sh | sh
make install

# 2. Lancer le pipeline sur l'échantillon fourni
make pipeline

# ou sans toucher au réseau (aucune URL n'est vérifiée) :
make pipeline-offline

# 3. Tests et qualité
make test
make lint
```

Le rapport de qualité sort en JSON sur la sortie standard, ou dans un fichier
avec `--output rapport.json`. La commande complète :

```bash
uv run pipeline run <fichier.csv> [--store-id demo] [--no-network] [--pretty] [-o rapport.json]
```

`--no-network` change le résultat, il ne l'accélère pas seulement : **sans
vérification des images, aucun produit ne peut être déclaré publiable.** Une URL
non contrôlée ne prouve rien.

### La plateforme (API + interface)

Il n'y a pas de compose de développement : les trois briques se lancent à la
main.

```bash
cp .env.example .env          # puis remplir POSTGRES_PASSWORD, ADMIN_*, SESSION_SECRET

# 1. Postgres + pgvector
docker run -d --name catalog-db -p 5433:5432 \
  -e POSTGRES_USER=catalog -e POSTGRES_PASSWORD=catalog -e POSTGRES_DB=catalog \
  pgvector/pgvector:pg16

# 2. API (migrations Alembic appliquées au démarrage)
uv run uvicorn api.main:app --reload --port 8000

# 3. Interface
cd web && npm install && npm run dev     # http://localhost:5173
```

Vite proxifie `/api` vers `http://127.0.0.1:8000` : même origine, donc pas de
CORS et pas de cookie tiers. Le compte administrateur est créé au démarrage à
partir de `ADMIN_EMAIL` / `ADMIN_PASSWORD` ; sans eux, l'API démarre mais aucun
compte n'existe et l'interface est inaccessible.

L'interface a quatre écrans : tableau de bord, traitement d'un fichier (dépôt +
suivi en direct par SSE), file de revue humaine, et référentiel produit.

## Commandes

| Commande | Effet |
|---|---|
| `make install` | Dépendances, sans l'extra `entity` (lourd) |
| `make install-all` | Tout, y compris `sentence-transformers` |
| `make pipeline` | Run complet sur l'échantillon (`FILE=...` pour un autre fichier) |
| `make pipeline-offline` | Idem, sans accès réseau |
| `make test` | pytest |
| `make lint` | ruff check + ruff format --check + mypy strict |
| `make format` | ruff format + ruff check --fix |
| `make profile` | Régénère les mesures de `docs/profiling.md` |
| `make clean` | Supprime les caches |

`make help` liste ces cibles.

---

## Structure

```
pipeline/              moteur : étapes pures, testables isolément
  normalize.py         fonctions pures (normalisation, checksum GS1)
  steps/               les 8 étapes, une par fichier
  llm/                 client Anthropic, tarifs datés, chiffrage
  runner.py            orchestrateur
  cli.py               `pipeline run <fichier>`
api/                   FastAPI : auth, dépôt, SSE, revue, référentiel, budget
  jobs.py              exécution d'un run et persistance du catalogue
  budget.py            enveloppe de dépense LLM, cumulée
  llm_cache.py         cache LLM persistant (table `llm_cache`)
db/                    SQLAlchemy 2 async + migrations Alembic
  migrations/          révisions ; 0001 = base de référence
web/                   React 18 + Vite + TypeScript + Tailwind
config/                règles versionnées (taxonomie, TVA, suffixes marketing)
deploy/                vhosts nginx, unité systemd + timer, scripts d'inbox
tests/                 données de test = extraits RÉELS du CSV
docs/profiling.md      les mesures qui fondent chaque règle
docs/plan.md           plan des 5 phases
```

## Règles non négociables

- **La TVA n'est jamais corrigée automatiquement**, même à confiance 100 %. Une
  erreur de TVA a un coût fiscal réel ; une validation humaine coûte trois
  secondes. Verrouillé par des tests (`test_vat_is_never_corrected_automatically`,
  `test_vat_is_never_modified`).
- **Chaque correction porte son audit trail** : valeur d'origine, valeur
  corrigée, auteur (`RULE` | `LLM` | `HUMAN`), règle appliquée, confiance,
  horodatage.
- **Un EAN n'est réparé que si le checksum devient valide** après réparation.
  Sinon on signale, on n'invente pas.
- **Le pipeline est idempotent** : rejouer le même fichier ne crée aucun
  doublon. Garanti en base par une contrainte d'unicité `(magasin, sha256 du
  fichier)`, pas seulement par convention.
- **Le LLM ne reçoit que des libellés distincts**, jamais les 10 000 lignes.
- **Aucun secret dans le dépôt** : tout passe par `.env` (voir `.env.example`).

## Coût du LLM

`claude-opus-5` par défaut (5 $ / 25 $ par million de jetons, relevé daté dans
`pipeline/llm/pricing.py`). Descendre en gamme est un arbitrage de
l'utilisateur, pas un défaut imposé : l'interface affiche le coût réel de chaque
run pour que cet arbitrage se fasse sur des chiffres.

Quatre garde-fous, dont un seul ne suffirait pas :

- lots de 25 libellés, sorties JSON contraintes par schéma ;
- cache persistant en base, keyé par `sha256(prompt + modèle)` : redéposer le
  même fichier ne rappelle pas l'API ;
- plafond par run (`LLM_MAX_CALLS_PER_RUN`, `LLM_MAX_COST_USD`) ;
- **enveloppe cumulée** (`LLM_BUDGET_USD`, 5 $ par défaut) sur toute la
  démonstration — douze runs sous leur plafond individuel dépasseraient quand
  même le budget. Une fois épuisée, l'étage LLM est sauté et le reste du
  pipeline continue : dégradation visible plutôt que facture inattendue.

Cette enveloppe ne vaut que si elle voit **tout** ce qui part chez Anthropic :
les dépôts faits dans l'interface comme le run du timer de 5 h 30, qui passe
par la CLI et tape dans la même clé. Un run qui échoue en cours de route est
donc débité lui aussi — ses appels ont bien été facturés — et le cache qu'il a
rempli est conservé plutôt que racheté le lendemain.

Sans `ANTHROPIC_API_KEY`, l'étage sémantique est sauté et le pipeline va au bout.

## Base de données

PostgreSQL 16 + pgvector. Le schéma est géré par **Alembic**, appliqué au
démarrage de l'API : `create_all()` sait créer une table absente mais pas
ajouter une colonne à une table qui existe déjà, et ce silence a déjà coûté un
500 en production. Une base antérieure à Alembic est adoptée automatiquement
(estampillée à la révision `0001`, qui décrit exactement cet état) plutôt que
recréée.

Le référentiel porte l'**état courant** de chaque magasin : le dépôt du
lendemain met à jour les fiches existantes au lieu d'empiler des doublons, et
chaque fiche garde son identité d'un jour à l'autre (`product_key`). Les
10 000 lignes du fichier restent rattachées à la fiche qui les représente
(`product_source_rows`), pour qu'un « stock de 4f2a-91 = 12 » venu de la caisse
du magasin trouve encore sa réponse après fusion.

## Déploiement

Docker Compose sur un VPS : `postgres`, `api`, `web` (build statique servi par
nginx), plus un service `pipeline` déclenché par un timer systemd.

Le stack a tourné sur ce VPS, en TLS, avec des fichiers de plusieurs magasins
traités par le timer de nuit. **Il est arrêté pour l'instant** et l'instance publique
n'est plus accessible : pour voir l'interface, la lancer en local (voir
[Démarrage rapide](#démarrage-rapide)). La configuration de déploiement
ci-dessous est conservée telle quelle.

- **Interface** : TLS Let's Encrypt, vhost dédié derrière le nginx de l'hôte,
  qui garde les ports 80/443. Le stack n'écoute que sur `127.0.0.1:33xx`.
- **Traitement automatique** : déposer un CSV dans `/srv/catalog/inbox`, puis
  `systemctl start catalog-pipeline.service` (ou attendre le timer, 05:30). Le
  préfixe du nom de fichier avant `_` sert d'identifiant magasin ; les fichiers
  partent ensuite dans `processed/` ou `quarantine/`.
- **Images** : construites dans GitHub Actions, poussées sur GHCR, le VPS fait
  un `pull`. Aucune compilation sur le serveur.

### CI/CD

- `ci.yml` (à chaque push) : ruff, ruff format, mypy strict, pytest, puis un run
  hors réseau sur les 10 000 lignes réelles dont les compteurs sont **comparés
  aux chiffres de `docs/profiling.md`**. Si le pipeline dérive, le CI le voit
  tout de suite et pas trois semaines plus tard en production.
- `deploy.yml` : **déclenchement manuel uniquement** (`workflow_dispatch`) tant
  que la confiance n'est pas établie — le VPS héberge déjà deux sites en
  production. Le bloc `push` est écrit et commenté.

## État d'avancement

- [x] Phase 0 — scaffolding, profilage du CSV réel, plan
- [x] Phase 1 — socle, ingestion, contrôles déterministes
      (10 000 lignes en 2,2 s hors réseau, 4 min avec la vérification des URLs)
- [x] Phase 2 — dédup exacte, golden records, quasi-doublons (blocking +
      RapidFuzz). Embeddings écartés, mesure à l'appui.
- [x] Phase 3 — étage LLM : client, cache persistant, lots, budget, comptabilité
      du coût, chiffrage à blanc
- [x] Phase 4 — base, authentification, API HTTP, interface de revue, conteneurs
- [~] Phase 5 — durcissement et déploiement : le stack a tourné sur le VPS en
      TLS avec CI/CD, il est arrêté pour l'instant ; reste le durcissement
      (sauvegardes, supervision)

Détail dans `docs/plan.md`.
