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
- LLM : API Anthropic, **`claude-opus-5` par défaut** (5 $/25 $ par million de
  jetons ; relevé du 2026-06-24 dans `pipeline/llm/pricing.py`). Sorties JSON
  contraintes par schéma, cache keyé par sha256(prompt+modèle), lots de 25
  libellés, plafonds d'appels ET de dépense par run.
  Descendre en gamme (`claude-sonnet-5`, `claude-haiku-4-5`) est un arbitrage
  de l'utilisateur : l'interface affiche le coût réel pour qu'il se fasse sur
  des chiffres. Ne jamais changer le modèle par défaut pour « économiser ».
- Frontend : React 18 + Vite + TypeScript + Tailwind (interface de revue)
- Orchestration : CLI `pipeline run <fichier>` + systemd timer sur le VPS.
  Pas de Prefect/Airflow : un seul pipeline, un seul serveur, ça doit rester léger.
- Déploiement : Docker Compose (postgres, api, front buildé servi par un reverse proxy),
  TLS automatique
- Tests : pytest (+ pytest-asyncio), données de test = extraits du CSV réel
- Qualité : ruff (lint+format), mypy strict sur le package pipeline

## Environnement réel (constaté le 2026-08-09, prime sur le kit)
- Poste local : Python 3.10, Docker, node 24, psql.
- VPS : Ubuntu 22.04, 2 vCPU / 3.8 Go RAM / 26 Go libres, SSH sur un port non
  standard (hôte, port et utilisateur uniquement dans les secrets GitHub).
- Le VPS héberge déjà deux autres sites derrière un **nginx hôte qui occupe
  80/443**. Caddy NE PEUT PAS prendre ces ports : le stack catalog s'expose sur
  127.0.0.1:33xx et l'nginx hôte fait reverse proxy. (Décision à confirmer phase 5.)
- Pas de domaine dédié au catalogue à ce jour.
- Le postgres existant du VPS n'est PAS partagé : le catalogue a son propre
  conteneur postgres16+pgvector et son propre volume.

## Règles de travail
- TOUJOURS travailler par petites étapes : implémenter, tester, me montrer, committer.
- Chaque étape du pipeline = un module pur avec entrée/sortie typées, testable seul.
- JAMAIS de secret dans le code ou le repo : tout passe par .env (fournir .env.example).
- Le pipeline est idempotent : rejouer le même fichier = même résultat, zéro doublon.
- Chaque correction porte : valeur d'origine, valeur corrigée, auteur (RULE|LLM|HUMAN),
  timestamp. C'est non négociable (audit trail).
- Le LLM ne reçoit que des libellés distincts (dédupliqués), jamais les 10k lignes.
- La TVA n'est JAMAIS modifiée automatiquement, même à confiance 100%.
- Ne jamais toucher aux services existants du VPS (autres sites, nginx hôte).
- Commits en anglais, format conventional commits (feat:, fix:, test:...).

## Le document de réponse fait foi
- `exercice/ULTY_Cas_pratique_Automatisation.tex` est la SOURCE DE VÉRITÉ de la
  réponse à l'exercice. Il n'est **jamais régénéré** depuis une autre version :
  toute modification passe par une édition de CE fichier, et seulement sur
  demande explicite.
  (Bechir l'a désigné sous le chemin `docs/reponse_ULTY_fiabilisation.tex`, qui
  n'existe pas dans le dépôt. Le fichier ci-dessus est celui qui joue ce rôle ;
  à renommer sur sa demande, pas de mon initiative.)
- Les annexes vivent dans un document SÉPARÉ,
  `exercice/ULTY_Annexes_Techniques.tex`. Le paragraphe final du document
  principal y renvoie : ne pas fusionner les deux, ne pas supprimer ce renvoi.
- **Invariants à ne JAMAIS modifier sans demande explicite :**
  (a) **36 écritures du cola**, comptage BRUT du fichier avant tout traitement.
      C'est la règle que veut Bechir, parce que l'en-tête du document promet des
      faits bruts. Le motif est `CO[CK]` : il couvre les abréviations
      (`COC COL ZER 1L`, `COCA Z S/SUCRE 1L`) qu'un filtre sur la sous-chaîne
      `COLA` raterait — ce détail vaut 13 écritures. Aucune définition ne donne
      35 : les valeurs possibles sont 23 (`COLA`), 25 (`COL`), 32
      (`COCA|COKA`), 36 (`COC|COK`), 85 (`CO`). 36 est le comptage complet.
  (b) le paragraphe final renvoyant au document d'annexes séparé ;
  (c) la phrase de l'en-tête sur les compteurs vérifiés en intégration continue
      et affichés dans l'interface. Elle n'est vraie que tant que
      `tests/test_doc_claims.py` passe.
- **Tout chiffre publié dans le document doit être couvert par
  `tests/test_doc_claims.py`.** Ajouter un chiffre au document sans l'ajouter au
  test, c'est rendre l'en-tête mensongère. Le test extrait chaque nombre par un
  motif : si une phrase est réécrite et que le motif ne correspond plus, le test
  échoue exprès — mettre le motif à jour, jamais supprimer la vérification.
- Deux comptages coexistent légitimement et ne doivent pas être confondus :
  **343** libellés distincts dans le fichier brut, **273** après ingestion, une
  fois les suffixes marketing détachés. C'est ce second nombre que le LLM voit.

## Commandes utiles
- `make dev` : lance postgres (docker) + api (uvicorn reload) + front (vite)
- `make pipeline FILE=data/samples/store_listing_produit.csv` : run complet local
- `make test` : pytest ; `make lint` : ruff + mypy
- `make deploy` : déploiement VPS (voir scripts/deploy.sh)

## Accès au serveur
> **2026-09-11 : TOUT le catalog a été SUPPRIMÉ du VPS, à la demande de
> Bechir** (le serveur ne garde que ses autres sites). Plus d'images,
> de base (`catalog_pgdata`), de réseau, de `/srv/catalog`, de vhost,
> d'htpasswd, d'unités systemd, de certificat, ni de clé SSH
> `github-actions-catalog-pipeline`. Les données ne sont plus récupérables.
> Ce qui suit décrit le déploiement tel qu'il a existé ; le code qui le
> recrée est toujours dans le dépôt. Ne rien redéployer sans demande explicite :
> `deploy.yml` échouerait de toute façon (clé retirée de `authorized_keys`).

- **Rapports** : https://catalog.example.com (auth basic, utilisateur
  `catalog`, mot de passe dans le secret GitHub `CATALOG_WEB_PASSWORD`).
  Pas de domaine nécessaire : un nom `sslip.io` résout vers l'IP. Vhost séparé
  (`/etc/nginx/sites-available/catalog.conf`), aucun port ouvert en plus,
  vhosts des autres sites intacts.
- **TLS** : actif, certificat Let's Encrypt pour le nom sslip.io, renouvelé par
  `certbot.timer` avec un deploy-hook qui recharge nginx. Le déploiement obtient
  le certificat tout seul s'il manque. HTTP redirige vers HTTPS.
- **nginx du VPS est en 1.18** : `http2` est un paramètre de `listen`, la
  directive autonome `http2 on;` (nginx ≥ 1.25) le fait planter. Le CI valide
  chaque vhost dans un conteneur `nginx:1.18-alpine`.
- **Traitement** : déposer un CSV dans `/srv/catalog/inbox`, puis
  `systemctl start catalog-pipeline.service` (ou attendre le timer, 05:30).
  Le préfixe du nom de fichier avant `_` sert d'identifiant magasin.
  Les fichiers partent ensuite dans `processed/` ou `quarantine/`.
- **Postgres** : `127.0.0.1:5433` sur le VPS uniquement. Depuis le poste :
  `ssh -L 5433:127.0.0.1:5433 <alias-du-vps>`.

## Dépôt et CI/CD
- GitHub : dépôt public, publié sans l'historique de développement (qui
  désignait le serveur de production) ; cet historique reste privé.
- `ci.yml` : ruff + mypy strict + pytest + run offline sur les 10 000 lignes,
  avec vérification des compteurs contre `docs/profiling.md`. ~30 s.
- `deploy.yml` : build de l'image dans Actions → GHCR → le VPS fait un `pull`.
  **Déclenchement manuel (`workflow_dispatch`) uniquement** tant que la
  confiance n'est pas établie ; le bloc `push` est prêt, commenté.
- Secrets posés : `VPS_HOST`, `VPS_PORT`, `VPS_USER`, `VPS_SSH_KEY` (clé ed25519
  DÉDIÉE à ce projet, distincte de celles des autres sites), `POSTGRES_PASSWORD`.
- Pièges du VPS déjà rencontrés, ne pas les refaire :
  - `MaxSessions 2` → `scp-action` échoue, tout doit tenir en UNE session SSH ;
  - un heredoc indenté dans un bloc YAML `script: |` casse bash ; le CI valide
    désormais la syntaxe de tous les scripts SSH via `bash -n` ;
  - le conteneur tourne en uid 1001 : les dossiers hôtes qu'il écrit doivent
    lui appartenir ;
  - le port SSH n'est **pas 22**. Ne jamais faire `ufw allow 22 && enable`.

## État d'avancement
(Claude : tiens cette section à jour à la fin de chaque session)
- [x] Phase 0 : scaffolding, profilage du CSV réel, plan validé
- [x] Phase 1 : socle + ingestion + contrôles déterministes
      (10 000 lignes en 2,2 s hors réseau, 4 min avec vérification des 9 085 URLs)
- [~] Phase 2 : dédup exacte + golden records faits (10 000 → 273) ;
      reste le flou : blocking, RapidFuzz, embeddings (étape 4b)
- [~] Phase 3 : étage LLM fait (client, cache, lots, budget, comptabilité du
      coût, prompts, chiffrage --dry-run). Cache LLM en base et enveloppe
      cumulée (5 $) partagés par les DEUX chemins d'exécution : l'API et la
      CLI du timer de nuit. Reste : le scoring/aiguillage par champ.
- [~] Phase 4 : schéma, authentification, API HTTP, front React et conteneurs
      api/web livrés et déployés. Alembic en place (`db/migrations`), appliqué
      au démarrage : `create_all()` ne servait plus qu'aux tests.
      Le référentiel porte l'état courant de CHAQUE magasin, avec identité de
      fiche stable d'un dépôt à l'autre et rattachement des lignes sources
      (`product_source_rows`).
      Le vhost de la plateforme attend dans `deploy/catalog-platform.nginx` —
      NE PAS l'installer avant que les conteneurs existent (502 garanti).
- [ ] Phase 5 : durcissement + déploiement VPS

## Journal de session
- 2026-08-09 — Scaffolding + profilage du CSV (voir `docs/profiling.md`).
  Le kit d'origine est archivé dans `docs/kit-claude-code.md`.
- 2026-08-09 — Phase 1 livrée, CI vert, déployée sur le VPS.
  Trois défauts trouvés par la mesure plutôt que par la lecture du code :
  la réparation du mojibake créait une catégorie fantôme (accent vs référentiel),
  le cache DNS par domaine ne résistait pas à la concurrence (9 044 résolutions
  pour 6 domaines), et le repli HEAD→GET était payé sur chaque URL.
  Leçon transférable aux phases suivantes : **exposer le coût dans les logs et
  le regarder**. Un cache dont on n'observe pas le taux de succès est un cache
  dont on ne sait pas s'il existe.
- 2026-08-10 — Référentiel : trois défauts trouvés en répondant à des questions
  de l'utilisateur, pas en relisant le code.
  1. La liste des magasins était déduite des 100 fiches affichées : un magasin
     volumineux masquait tous les autres. Le comptage appartient à la base.
  2. Le catalogue était effacé puis réinséré à chaque dépôt, donc chaque fiche
     changeait d'identifiant chaque matin. Désormais : contenu remplacé,
     identité conservée via `product_key` (clé naturelle par magasin).
  3. La fusion de quasi-doublons additionnait `source_rows` sans reprendre les
     `row_ids` des fiches absorbées : une fiche annonçait 945 lignes et n'en
     savait nommer que 12.
  Leçon transférable : **un compte affiché doit dire sur quoi il porte.**
  Les trois défauts sont le même : une agrégation calculée sur l'échantillon
  sous la main plutôt que sur l'ensemble qu'elle prétend décrire.
  Alembic ajouté à cette occasion, avec adoption d'une base préexistante
  (`stamp 0001`) : sans ça, l'introduire aurait imposé de recréer la base.
- 2026-08-11 — Enveloppe LLM portée de 4 à 5 $, et deux fuites de comptabilité
  bouchées — les deux laissaient partir de l'argent réellement facturé sans
  qu'il atteigne le compteur affiché.
  1. La dépense n'était inscrite que sur le chemin du succès. Un run qui
     tombait après l'étage LLM avait pourtant payé ses appels : l'enveloppe se
     croyait plus large qu'elle ne l'était, et le cache partait avec, donc le
     dépôt suivant rachetait les mêmes libellés.
  2. Le run du timer de 5 h 30 passe par la CLI, qui ne lisait pas le cumul,
     n'inscrivait pas sa dépense et n'avait qu'un cache de session dans un
     conteneur éphémère. « Plafond cumulé sur toute la démonstration » ne
     comptait en fait que les dépôts faits depuis l'interface, et chaque nuit
     repayait les 273 libellés.
  Leçon transférable : **un garde-fou ne couvre que les chemins qu'on lui
  branche.** Le plafond était juste, sa lecture aussi ; c'est l'ensemble des
  runs qu'il prétendait couvrir qui était plus petit que promis.
  À traiter ensuite : deux dépôts simultanés lisent le reliquat avant que l'un
  des deux ne l'inscrive (dépassement borné à un run), et un lot Batch API
  abandonné sur expiration est facturé sans être compté.
- 2026-08-11 (soir) — Les deux « à traiter ensuite » ci-dessus sont traités,
  plus deux défauts trouvés en diagnostiquant un run réel figé (`test01`).
  1. Course sur l'enveloppe : `reserve()` lit le reliquat ET réclame le
     plafond du run dans la même section critique (verrou du process, choix
     documenté dans `api/budget.py` : une réservation durable afficherait
     « budget épuisé » pendant toute la durée de chaque run). La fenêtre
     inter-processus (CLI de nuit vs API) reste ouverte et documentée — la
     fermer change ce que la base enregistre, donc ça se décide, pas ça se
     patche.
  2. Lot Batch expiré : renoncer à ATTENDRE n'est plus renoncer à COMPTER.
     Le lot est annulé (borne la facture), le déjà-servi est collecté
     (jetons comptés, libellés mis en cache), le reste repart en file.
  3. Un run dont le processus meurt restait « RUNNING » à jamais dans
     l'interface (constaté sur `test01` : « étape 6/8, ~27 min restantes »
     pendant des heures). Au démarrage de l'API, tout RUNNING est par
     construction orphelin → FAILED, avec l'étape fautive marquée.
  4. La table `url_check_cache` existait depuis la phase 1 et RIEN ne la
     lisait : chaque dépôt repayait ~4 min de vérifications d'URLs. Branchée
     sur le motif du cache LLM (chargée en amont, étape pure, persistée en
     aval, TTL 24 h, upsert), sur les DEUX chemins d'exécution.
  Leçon transférable : **une ressource déclarée n'est pas une ressource
  branchée.** La table de cache, le plafond, le filet d'exception : chacun
  existait et semblait couvrir son cas ; c'est le branchement effectif sur
  tous les chemins (échec compris, processus mort compris) qui fait le
  garde-fou. Vérifié en fin de session : 245 tests verts, run complet des
  10 000 lignes en 2,0 s hors réseau. (Poussé et redéployé dans la nuit du
  12 : le VPS tourne sur ces correctifs, cache d'URLs actif et alimenté.)
- 2026-08-12 — Nouveau jeu de test `05-catalogue-realiste-5k.csv` (5 000
  lignes) : le cocktail de défauts du fichier réel à mi-échelle, injecté sur
  des ensembles d'indices DISJOINTS par champ → chaque compteur du rapport a
  une seule cause possible. Attendus mesurés et figés dans
  `docs/jeux-de-test.md` (71/80 TVA détectées : 3 invérifiables, ~6 tolérées
  par `vat_rules.yaml` ; 32 libellés, 26 fiches). Piège corrigé au passage :
  un PLU sur ~10 devenait un GTIN valide par zfill et se faisait « réparer » —
  les codes internes du générateur sont désormais irréparables par
  construction. 250 tests verts, ruff + mypy OK.
- 2026-09-11 — README : impact business en tête (≈ 0,44 $ de LLM au lieu de
  ≈ 16 $ par run de 10 000 lignes, ≈ 0,02 $ le lendemain), chiffres sortis de
  `estimate_dry_run` et verrouillés par `tests/test_readme_claims.py`.
  **Constat VPS : le stack catalog est ARRÊTÉ depuis le 2026-08-31 10:43 UTC.**
  Conteneurs supprimés, vhost retiré de `sites-enabled` (reste dans
  `sites-available`), timer et service désactivés — les trois au même instant,
  donc un arrêt volontaire, pas une panne. Intacts : volume `catalog_pgdata`,
  certificat, `/srv/catalog`. Le lien de la section « Accès au serveur » ne
  répond plus. Les 93 images catalog (dont pgvector) ont été supprimées à sa
  demande : disque 69 % → 30 %, autres sites vérifiés en 200.
  Le stack n'a pas été relancé : décision de l'utilisateur.
  Bechir confirme l'arrêt VOLONTAIRE : le README ne présente plus le lien
  comme en ligne. Puis, à sa demande, suppression COMPLÈTE du catalog sur le
  VPS, données comprises (voir l'encadré « Accès au serveur »). Vérifié après
  coup : autres sites en 200, `nginx -t` OK, nouvelle session SSH OK.
  Dépôt publié en public à partir d'une copie nettoyée, sans historique :
  l'ancien historique désignait le serveur de production.
