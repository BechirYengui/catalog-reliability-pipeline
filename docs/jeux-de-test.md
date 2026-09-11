# Jeux de test

Cinq catalogues, chacun avec un résultat **attendu et connu à l'avance**.
Si la plateforme s'en écarte, c'est elle qui a un problème, pas le fichier.

Régénérer : `python3 scripts/make_test_catalogs.py`
Fichiers : `data/samples/tests/`

Tous reproduisent le format réel : BOM UTF-8 et fins de ligne CRLF. Tester sur
un format plus facile que la production ne prouverait rien.

---

## 1 — `01-catalogue-propre.csv` (200 lignes)

Tout est correct : EAN valides, images joignables, taxonomie complète, TVA
cohérente avec la catégorie. 10 produits, 20 lignes chacun, avec le libellé
**identique** d'une ligne à l'autre — c'est ainsi qu'écrit une caisse, et c'est
précisément pourquoi un catalogue contient des doublons.

**Attendu : 200 lignes → 10 fiches, 100 % publiable, ZÉRO anomalie, aucune
décision à prendre.**

C'est le test le plus important et le plus souvent oublié. Un contrôle qui ne
trouve rien sur un fichier propre vaut autant qu'un contrôle qui trouve tout
sur un fichier cassé — un pipeline qui signale des problèmes inexistants noie
la revue humaine et perd la confiance de l'utilisateur.

---

## 2 — `02-catalogue-catastrophe.csv` (300 lignes)

70 % de lignes inexploitables : libellés vides, EAN non numériques, URLs en
`ftp://` ou `not-a-url`, taxonomie tronquée, TVA absente ou hors barème.

**Attendu : QUARANTAINE. Rien n'est intégré au référentiel.**

C'est le cas d'un magasin dont l'export a changé de format pendant la nuit.
Intégrer ce fichier ferait plus de dégâts que le mettre de côté un jour.

---

## 3 — `03-catalogue-tva-incoherente.csv` (400 lignes)

10 produits, 40 lignes chacun. Dans chaque groupe, 8 lignes portent un taux de
TVA **légal mais faux pour ce produit** — du whisky à 5,5 %, du Doliprane à 20 %.

**Attendu : ~74 incohérences détectées, 10 décisions groupées, aucune
correction automatique de TVA.**

Le contrôle syntaxique ne trouve **rien** ici : les quatre taux sont tous
légaux. Seul le croisement catégorie ↔ taux les détecte. Et rien n'est corrigé
automatiquement, quelle que soit la confiance : une erreur de TVA a une
conséquence fiscale.

C'est le fichier à utiliser pour tester l'écran de revue au clavier.

---

## 4 — `04-catalogue-doublons.csv` (600 lignes)

10 produits, chacun écrit de 6 façons (casse, accents, espaces doubles,
ponctuation), répétées 10 fois.

**Attendu : 600 lignes → 10 fiches, 100 % publiable, zéro anomalie.**

Teste la normalisation et la fusion en golden record.

**Limite assumée** : ce fichier ne contient pas de variantes réellement
différentes (« COC COL ZER 1L » pour « COCA COLA ZERO 1L »). Celles-là passent
par l'étape de quasi-doublons (blocking + RapidFuzz sur le libellé enrichi),
qui ne joue pas ici : après normalisation, les six écritures sont déjà
identiques et la dédup exacte a tout regroupé. Ce jeu teste la normalisation,
pas la résolution floue.

---

## 5 — `05-catalogue-realiste-5k.csv` (5 000 lignes)

Réplique à mi-échelle du fichier de production : 10 familles × 3 libellés de
caisse, et le même cocktail de défauts que `docs/profiling.md` — mais en
proportions **exactes**, injectées sur des ensembles de lignes disjoints par
champ. Chaque compteur du rapport a donc une seule cause possible.

| Champ | Défauts injectés |
|---|---|
| EAN | 650 vides · 190 zéros perdus (réparables) · 165 zéros en trop (réparables) · 65 PLU courts (routés) · 700 EAN-8 balance 20-29 (routés) · 600 GTIN-14 (signalés) · 2 630 valides |
| URL | 400 vides · 40 sans schéma (réparées, domaine mort) · 20 `htp://` (réparées, vivantes) · 20 `ftp://` (signalées) · 210 domaine irrésoluble · 4 310 vivantes |
| Nom | 8 mojibake · 40 `ORIGINE ITALIA` collés · 36 `FAMILY SIZE` collés |
| Taxonomie | 200 tronquées après n1 · 125 trouées en n2 (réparables par règle) · 36 mojibake n2 · 16 mojibake n3 |
| TVA | 80 taux légaux mais faux (8 par famille), jamais corrigés automatiquement |

**Attendu (vérifié au run offline du 2026-08-12) : PAS de quarantaine
(error_rate 0,4 %), chaque compteur d'anomalie égal au comptage injecté,
71 incohérences de TVA** — les 9 manquantes sont voulues : 3 lignes à
taxonomie tronquée (catégorie inconnue, donc invérifiables) et ~6 taux tirés à
10 % sur PARACETAMOL/COLA, taux que `vat_rules.yaml` tolère explicitement.
**32 libellés distincts après ingestion** (30 + les 2 formes accentuées
restaurées par la réparation mojibake), 728 corrections RULE / 476 décisions.

Le nombre de fiches dépend de l'étage LLM, car les quasi-doublons se
comparent sur le **libellé enrichi** :
- hors réseau / sans LLM : 4 paires fusionnées → **26 fiches** ;
- run complet avec enrichissement (mesuré via l'interface le 2026-08-12,
  TEST02) : 12 paires fusionnées → **18 fiches**, 1 appel LLM réel pour
  0,05 $ (18 libellés servis par le cache inter-runs), 3 résolutions DNS et
  4 338 requêtes HTTP en 1 min.

**« 0 / 18 publiable » est le résultat ATTENDU, pas un échec** : chaque
famille porte 8 TVA fausses, donc chaque fiche hérite d'un conflit de TVA —
et la TVA n'est jamais corrigée automatiquement. Ce fichier laisse
volontairement la totalité du catalogue suspendue à la file de revue : c'est
le jeu à utiliser pour tester le circuit de validation humaine de bout en
bout, le taux publiable ne monte qu'à mesure que la revue tranche.

C'est le fichier à déposer pour tester la plateforme en conditions réelles
sans payer les 10 000 lignes : mêmes taux de défauts, moitié du volume, et
un rapport dont chaque chiffre se vérifie contre le tableau ci-dessus.

---

## Résultats mesurés

Mesuré le 2026-08-10, vérification des images activée.

| Fichier | Lignes | Fiches | Publiable | Statut |
|---|---:|---:|---:|---|
| 01 propre | 200 | 10 | 100,0 % | ok, 0 anomalie |
| 02 catastrophe | 300 | 8 | 30,0 % | **QUARANTAINE**, 1 081 anomalies |
| 03 TVA | 400 | 10 | 81,5 % | ok, 74 incohérences |
| 04 doublons | 600 | 10 | 100,0 % | ok, 0 anomalie |
| 05 réaliste 5k | 5 000 | 18 | 0 % avant revue TVA (voulu) | ok, 3 757 anomalies, 71 incohérences TVA |

Reproduire : `uv run pipeline run data/samples/tests/01-catalogue-propre.csv --store-id demo -o rapport.json`

Sans la vérification des images (`--no-network`), le taux publiable de tous ces
fichiers tombe à **0 %** : c'est voulu, une URL non contrôlée ne prouve rien.
Le nombre de fiches, lui, ne change pas.
