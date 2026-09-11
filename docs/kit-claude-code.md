# Kit Claude Code — Projet « Catalog Reliability Pipeline » (ULTY)

Ce fichier contient tout ce qu'il faut, dans l'ordre d'utilisation :
1. La préparation de Claude Code (installation, CLAUDE.md, commandes slash, permissions)
2. Le PROMPT MAÎTRE à coller dans Claude Code
3. Les prompts de suivi, phase par phase
4. Le prompt de déploiement VPS via SSH

---

## 1. Préparation de Claude Code

### 1.1 Installation et démarrage
```bash
# Sur ton PC (pas sur le VPS : Claude Code tourne en local, il déploiera via SSH)
npm install -g @anthropic-ai/claude-code
mkdir catalog-pipeline && cd catalog-pipeline
# Copie le CSV de l'exercice dans data/samples/
mkdir -p data/samples && cp /chemin/vers/store_listing_produit.csv data/samples/
claude
```

Au premier lancement, tape `/init` pour que Claude Code crée un CLAUDE.md, puis **remplace son contenu** par celui de la section 1.2 (c'est la mémoire permanente du projet : chaque session le relit).

### 1.2 CLAUDE.md (à placer à la racine du projet)

```markdown
# Catalog Reliability Pipeline

## Ce que fait ce projet
Pipeline de fiabilisation de catalogues produits CSV (10k lignes/jour/magasin) avant
intégration dans un référentiel unique. Architecture en 3 étages : règles déterministes
→ enrichissement LLM → validation humaine. Inclut un moteur de pipeline, une API, et
une interface web de revue humaine.

## Stack (décidée, ne pas remettre en question sans me demander)
- Python 3.12, gestion des deps avec uv
- Moteur pipeline : Polars (traitement vectorisé), RapidFuzz (similarité),
  sentence-transformers multilingue MiniLM (embeddings, tourne en CPU sur le VPS)
- Backend : FastAPI + Pydantic v2, SQLAlchemy 2 async
- Base : PostgreSQL 16 + extension pgvector (référentiel + vecteurs + cache LLM)
- LLM : API Anthropic (claude-sonnet-4-6), sorties JSON contraintes par schéma,
  cache en base keyé par hash(prompt+modèle)
- Frontend : React 18 + Vite + TypeScript + Tailwind (interface de revue)
- Orchestration : CLI `pipeline run <fichier>` + systemd timer sur le VPS.
  Pas de Prefect/Airflow : un seul pipeline, un seul serveur, ça doit rester léger.
- Déploiement : Docker Compose (postgres, api, front buildé servi par Caddy),
  Caddy pour TLS automatique
- Tests : pytest (+ pytest-asyncio), données de test = extraits du CSV réel
- Qualité : ruff (lint+format), mypy strict sur le package pipeline

## Règles de travail
- TOUJOURS travailler par petites étapes : implémenter, tester, me montrer, committer.
- Chaque étape du pipeline = un module pur avec entrée/sortie typées, testable seul.
- JAMAIS de secret dans le code ou le repo : tout passe par .env (fournir .env.example).
- Le pipeline est idempotent : rejouer le même fichier = même résultat, zéro doublon.
- Chaque correction porte : valeur d'origine, valeur corrigée, auteur (RULE|LLM|HUMAN),
  timestamp. C'est non négociable (audit trail).
- Le LLM ne reçoit que des libellés distincts (dédupliqués), jamais les 10k lignes.
- La TVA n'est JAMAIS modifiée automatiquement, même à confiance 100%.
- Commits en anglais, format conventional commits (feat:, fix:, test:...).

## Commandes utiles
- `make dev` : lance postgres (docker) + api (uvicorn reload) + front (vite)
- `make pipeline FILE=data/samples/store_listing_produit.csv` : run complet local
- `make test` : pytest ; `make lint` : ruff + mypy
- `make deploy` : déploiement VPS (voir scripts/deploy.sh)

## État d'avancement
(Claude : tiens cette section à jour à la fin de chaque session)
- [ ] Phase 1 : socle + ingestion + contrôles déterministes
- [ ] Phase 2 : résolution d'entités + golden record
- [ ] Phase 3 : enrichissement LLM + cache + scoring
- [ ] Phase 4 : API + interface de revue
- [ ] Phase 5 : durcissement + déploiement VPS
```

### 1.3 Commandes slash personnalisées (`.claude/commands/`)

Crée ces deux fichiers ; ils te donnent des raccourcis réutilisables.

`.claude/commands/run-sample.md` :
```markdown
Lance le pipeline complet sur data/samples/store_listing_produit.csv,
affiche le rapport de qualité (anomalies par champ, taux de produits publiables,
nb d'appels LLM réels vs cache), et signale toute régression par rapport
au dernier run (compare les compteurs stockés en base).
```

`.claude/commands/review-code.md` :
```markdown
Relis les changements non commités avec un œil de reviewer senior :
idempotence du pipeline, audit trail complet, aucun secret en dur,
gestion d'erreurs des appels externes (timeout, retry), typage mypy.
Liste les problèmes par sévérité puis propose les corrections.
```

### 1.4 Permissions (`.claude/settings.json`)

```json
{
  "permissions": {
    "allow": [
      "Bash(make:*)", "Bash(uv:*)", "Bash(pytest:*)", "Bash(ruff:*)",
      "Bash(docker compose:*)", "Bash(git add:*)", "Bash(git commit:*)",
      "Bash(npm:*)", "Bash(psql:*)"
    ],
    "deny": [
      "Read(.env)", "Read(**/*.pem)", "Read(~/.ssh/**)",
      "Bash(git push:*)", "Bash(rm -rf:*)"
    ]
  }
}
```
Le `deny` sur `.env` et `~/.ssh` protège tes secrets et ta clé SSH : Claude Code
n'a jamais besoin de LIRE la clé, il a juste besoin d'EXÉCUTER ssh/scp (tu
approuveras ces commandes au cas par cas quand il déploiera). `git push` reste
manuel : c'est toi qui pousses.

### 1.5 Conseils d'usage
- Démarre chaque grosse phase en mode plan (Shift+Tab) : Claude propose son plan,
  tu valides, il exécute. Ça évite les dérives.
- Une phase = une session. Entre deux, `/clear` pour repartir avec un contexte propre
  (le CLAUDE.md garde la mémoire).
- S'il part dans une mauvaise direction : Échap, corrige, repars.

---

## 2. LE PROMPT MAÎTRE (à coller tel quel dans Claude Code, en mode plan)

```
Je veux construire un projet complet de fiabilisation de catalogues produits,
inspiré d'un cas réel (SaaS type ULTY qui synchronise des catalogues magasins vers
des marketplaces comme Uber Eats/Deliveroo). Lis d'abord CLAUDE.md : la stack y est
décidée et justifiée, ne la remets pas en question.

## Le problème
Chaque magasin dépose un CSV quotidien (~10 000 lignes) : id_produit, ean, nom,
url_image, taxonomie_niveau_1..4, tva. Le fichier contient : encodage cassé
(mojibake type "CAFÃ‰"), BOM, EAN invalides ou avec zéros de tête perdus/ajoutés,
codes courts non-EAN (type PLU), URLs mortes ou au schéma tronqué (htp://), TVA
légale mais incohérente avec le produit (whisky à 5,5%), taxonomies incomplètes ou
contredisant le nom, libellés caisse abrégés ("PL EMMENTAL RAP 200G"), suffixes
marketing collés ("...1LORIGINE ITALIA"), et surtout des quasi-doublons massifs
(le même produit écrit de dizaines de façons). Un échantillon réel est dans
data/samples/store_listing_produit.csv : PROFILE-LE D'ABORD et fonde les règles
sur ce que tu y observes réellement.

## L'architecture cible

### Moteur pipeline (package Python `pipeline/`)
Étapes séquentielles, chacune = un module pur, testable isolément, qui annote un
DataFrame Polars et journalise ses corrections :
1. ingest : détection encodage + BOM, réparation mojibake (ftfy), parsing CSV
   robuste, validation du schéma de colonnes, trim, détachement des suffixes
   marketing collés au libellé
2. field_checks : EAN (checksum GS1 ; distinguer réparable/zéros de tête,
   non-EAN/code interne à router, réellement invalide à signaler), URL (validation
   syntaxique + HEAD asynchrones avec limite de concurrence PAR DOMAINE, timeout,
   retry, et cache des résultats en base avec TTL pour ne pas re-vérifier chaque
   jour les mêmes URLs ; réparation des schémas tronqués), TVA (appartenance aux
   taux légaux FR), taxonomie (hiérarchie complète, valeurs dans le référentiel)
3. cross_checks : table catégorie→TVA attendue (contrôle croisé, JAMAIS de
   correction auto sur la TVA), incohérence taxonomie↔nom par mots-clés,
   incohérence EAN↔nom (branché sur un client Open Food Facts avec cache,
   désactivable par config)
4. entity_resolution : doublons exacts post-normalisation, puis blocking
   (clé = tokens normalisés + format extrait), similarité RapidFuzz dans chaque
   bloc, embeddings (sentence-transformers multilingue, vecteurs en pgvector)
   pour les paires sous le seuil, LLM uniquement pour trancher les ambiguës ;
   fusion en golden record avec règles de survivorship EXPLICITES et configurables
   (libellé le plus complet, EAN qui passe le checksum, URL qui répond,
   taxonomie la plus profonde)
5. llm_enrich : réécriture des libellés caisse, complétion de taxonomie
   (taxonomie de référence fournie dans le prompt), extraction d'attributs
   (marque, contenance, unité). Appels UNIQUEMENT sur libellés distincts,
   sortie JSON contrainte (tool use / json schema), score de confiance exigé,
   cache postgres keyé par sha256(prompt normalisé + modèle), batch + retry
   avec backoff. Si l'API est indisponible : les lignes partent en file
   d'attente, le reste du pipeline continue (dégradation gracieuse).
6. scoring_routing : statut par produit (VALIDATED / AUTO_CORRECTED /
   NEEDS_REVIEW / REJECTED) avec seuils PAR CHAMP configurables ;
   tout ce qui touche la TVA → NEEDS_REVIEW obligatoirement
7. report : rapport de qualité par run (anomalies par champ, corrections par
   auteur, taux de produits publiables = EAN exploitable + image valide +
   taxonomie complète + TVA cohérente, appels LLM réels vs cache hits) ;
   garde-fou : si le taux d'anomalies dépasse un seuil configurable vs
   l'historique, le run passe en QUARANTINE et rien n'est intégré

Exécution : batch par fichier (la dédup exige de voir tout le fichier),
CLI `pipeline run <file> --store-id X`, conçu pour que N fichiers tournent
en parallèle sans conflit (upserts transactionnels). Idempotent : rejouer
le même fichier ne crée rien de nouveau. Reprise sur panne : chaque étape
persiste son résultat (statut par étape dans ingestion_runs), un run
interrompu reprend à l'étape suivante au lieu de tout refaire. Logs
structurés (structlog, JSON) avec run_id partout : le funnel doit se lire
dans les logs (lignes entrées/sorties par étape, paires de dédup restantes,
appels LLM réels vs cache).

### Base de données (postgres + pgvector)
Tables : stores, ingestion_runs (avec statut et rapport JSON), raw_products
(lignes brutes par run), products (golden records du référentiel),
product_corrections (audit trail : run_id, product_id, champ, valeur_origine,
valeur_corrigée, auteur RULE|LLM|HUMAN, règle/prompt appliqué, confiance,
timestamp), review_tasks (décisions GROUPÉES : une tâche = un groupe de lignes +
une proposition + un contexte), llm_cache, label_embeddings (vector).
Migrations avec Alembic.

### API (FastAPI, `api/`)
- POST /runs (upload ou chemin de fichier) → lance le pipeline en tâche de fond
- GET /runs, GET /runs/{id} (statut + rapport)
- GET /review/tasks?status=pending (groupées, avec tout le contexte pour décider)
- POST /review/tasks/{id}/decision (approve/reject/edit) → applique au référentiel,
  écrit l'audit trail, et ALIMENTE LA BOUCLE : la décision enrichit le cache LLM
  et/ou la table catégorie→TVA pour que la même question ne revienne jamais
- GET /products (recherche/filtres), GET /products/{id}/history
- GET /metrics (taux publiable, évolution par run)
- Auth simple mais réelle : sessions + rôles (admin peut valider la TVA,
  reviewer le reste). Pas d'OAuth externe pour l'instant.

### Frontend (React+Vite+TS+Tailwind, `web/`)
3 écrans, sobres et denses :
1. Dashboard : liste des runs, statut, taux publiable en tendance, alertes quarantaine
2. Revue : file de décisions groupées ("ces 40 lignes = le même whisky à 5,5% →
   appliquer 20% ?") avec diff avant/après, raccourcis clavier (a=approuver,
   r=rejeter, e=éditer), et le contexte complet (lignes concernées, source de la
   proposition, confiance)
3. Référentiel : recherche produits, fiche avec historique complet des corrections

## Ta mission MAINTENANT
1. Profile data/samples/store_listing_produit.csv (script jetable) et montre-moi
   les chiffres réels : distribution des longueurs d'EAN, checksums valides,
   libellés distincts, valeurs de TVA, URLs par type. Les règles découleront de ça.
2. Propose le plan détaillé des 5 phases du CLAUDE.md avec, pour chaque phase :
   arborescence des fichiers, ordre d'implémentation, tests prévus.
3. Attends ma validation avant d'écrire le code de la phase 1.

Contraintes de qualité : mypy strict sur pipeline/, tests pytest sur chaque module
d'étape avec des cas tirés du CSV réel, .env.example complet, Makefile, README
avec quickstart. Tout doit tourner sur un VPS 2 vCPU / 4 Go RAM : mesure et
optimise en conséquence (le modèle d'embeddings en CPU, les HEAD en async).
```

---

## 3. Prompts de suivi, phase par phase

Colle-les au début de chaque nouvelle session (après `/clear`).

**Phase 2 (résolution d'entités)** :
```
Lis CLAUDE.md et l'état d'avancement. On attaque la phase 2 : entity_resolution.
Commence par un test qui échoue : prends 30 lignes réelles du CSV représentant
le même produit écrit différemment (famille "coca zero") et exige que le module
les regroupe en une seule entité avec le bon golden record. Puis implémente
blocking → RapidFuzz → embeddings → arbitrage, dans cet ordre, en me montrant
à chaque cran combien de paires restent à traiter (le funnel doit se voir dans
les logs). Les règles de survivorship vont dans un fichier de config commenté.
```

**Phase 3 (LLM)** :
```
Phase 3 : llm_enrich. D'abord le cache et la couche d'appel (retry, backoff,
budget max par run configurable, compteur de tokens), ensuite seulement les
prompts. Sortie contrainte par json schema via tool use. Écris les prompts en
français, avec la taxonomie de référence injectée. Ajoute un mode --dry-run qui
loggue ce qui SERAIT envoyé sans appeler l'API. Teste avec de vrais libellés du
CSV et montre-moi le coût estimé d'un run complet avant/après cache.
```

**Phase 4 (API + front)** :
```
Phase 4. D'abord l'API avec tests (httpx), ensuite le front. L'écran de revue est
la pièce maîtresse : je veux traiter 20 décisions groupées en moins de 2 minutes
au clavier. Fais-moi une démo locale bout en bout : upload du CSV → run →
tâches de revue → décision → vérification que la boucle d'apprentissage a bien
écrit dans le cache/la table TVA.
```

---

## 4. Déploiement VPS via SSH (phase 5)

Prérequis côté toi (une seule fois) :
```bash
# Vérifie que ta clé fonctionne : Claude Code utilisera la config SSH, pas la clé elle-même
ssh mon-vps "echo ok"
# Ajoute un alias dans ~/.ssh/config si ce n'est pas fait :
# Host mon-vps
#   HostName <ip-du-vps>
#   User <user>
#   IdentityFile ~/.ssh/<ta-clé>
```

Prompt de déploiement :
```
Phase 5 : déploiement sur mon VPS (alias ssh "mon-vps", Ubuntu, 2 vCPU/4Go,
domaine catalog.mondomaine.tld pointant déjà dessus). Prépare :
1. docker-compose.prod.yml : postgres16+pgvector (volume persistant, healthcheck),
   api (uvicorn, non-root), web (build statique servi par Caddy), caddy (TLS auto).
   Le service pipeline s'exécute via `docker compose run` déclenché par un
   systemd timer quotidien qui traite les fichiers déposés dans /srv/catalog/inbox.
2. scripts/deploy.sh : rsync du code vers le VPS (jamais .env ni .git), puis
   ssh mon-vps "docker compose ... up -d --build", puis healthcheck HTTP et
   `alembic upgrade head`. Idempotent et verbeux.
3. Sur le VPS : crée /srv/catalog/{inbox,processed,quarantine}, le .env de prod
   (tu me listes les valeurs à remplir, tu ne les inventes pas), ufw (22/80/443
   seulement), et un script de backup pg_dump quotidien avec rotation 14 jours.
4. Sécurité : l'API n'écoute que sur le réseau docker interne, seul Caddy est
   exposé ; les secrets restent dans le .env du VPS (chmod 600) ; vérifie
   qu'aucun secret n'est parti dans l'image docker (docker history).
Fais-le étape par étape en me montrant chaque commande ssh avant de l'exécuter.
Termine par un test bout en bout : dépose le CSV d'exemple dans inbox, déclenche
le timer manuellement, et montre-moi le rapport de run via l'API en HTTPS.
```

---

## 5. Récap des choix techniques et de leurs raisons (si on te demande)

| Choix | Pourquoi | Pourquoi pas l'alternative |
|---|---|---|
| uv | Install déterministe et rapide, lockfile | pip/poetry plus lents, moins reproductibles |
| Polars | Vectorisé multi-thread, mode streaming pour gros fichiers | pandas plus lent, Spark = cluster injustifié |
| pgvector | Vecteurs + référentiel + cache au même endroit, transactions | Base vectorielle dédiée = un système de plus à opérer pour 10k vecteurs |
| sentence-transformers local | Pas de coût par appel, données restent sur le serveur, CPU suffit pour des libellés courts | Embeddings API = dépendance + coût récurrent inutiles |
| CLI + systemd timer | Un pipeline, un serveur : la simplicité EST la robustesse | Prefect/Airflow = superstructure pour rien à cette échelle |
| FastAPI | Async natif (HEAD checks, LLM), Pydantic partagé avec le pipeline | Django trop lourd pour une API + 3 écrans |
| React+Vite | Écran de revue riche (raccourcis clavier, diffs) | Server-side pur trop limité pour l'UX de revue |
| Docker Compose + Caddy | Reproductible, TLS automatique, adapté à UN VPS | k8s = complexité sans objet ici |

---

## 6. Écarts constatés sur l'environnement réel (ajouté à l'exécution, 2026-08-09)

- **VPS** : Ubuntu 22.04, 2 vCPU / 3.8 Go RAM / 26 Go libres, SSH sur un port
  non standard. Conforme à la cible.
- **Ports 80/443 déjà pris** par un nginx *hôte* qui sert deux autres sites.
  → Caddy ne peut pas prendre 80/443. Le stack catalog s'expose sur un port local
  (ex. 127.0.0.1:3300) et l'nginx hôte existant fait office de reverse proxy +
  TLS (certbot déjà en place pour les autres sites). Décision à acter en phase 5.
- **Aucun domaine** fourni pour le catalogue à ce stade.
- Postgres 16 tourne déjà en conteneur pour un autre site → le catalogue aura son
  **propre** conteneur postgres+pgvector et son propre volume (pas de partage).
