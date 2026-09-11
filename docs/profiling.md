# Profilage du CSV réel — `data/samples/store_listing_produit.csv`

Mesuré le 2026-08-09 par `scripts/profile_csv.py` et `scripts/profile_deep.py`
(stdlib only, rejouables : `python3 scripts/profile_csv.py <fichier>`).

**Toutes les règles du pipeline sont fondées sur ces chiffres, pas sur des suppositions.**

---

## 0. Le fait structurant

> **10 000 lignes → 343 libellés distincts → 10 familles produit réelles.**

Le fichier est une réplication massive : chaque libellé apparaît ~230 à 244 fois,
avec du bruit injecté ligne par ligne (TVA fausse, taxonomie fausse, EAN synthétique,
URL cassée, mojibake). Trois conséquences qui décident de l'architecture :

| Conséquence | Impact |
|---|---|
| Le LLM ne doit voir que **343 libellés**, pas 10 000 lignes | ÷29 sur le coût LLM |
| Les embeddings = **343 vecteurs**, pas 10 000 | tient largement en RAM sur 2 vCPU |
| L'EAN **n'est pas une clé produit** (voir §3) | la fusion se fait sur libellé + attributs |

---

## 1. Encodage et structure

| Mesure | Valeur |
|---|---|
| Taille | 1 583 541 octets |
| **BOM UTF-8** | **présent** → `utf-8-sig` obligatoire |
| Fins de ligne | 10 001 CRLF, 0 LF seul |
| Décodable UTF-8 | oui (le mojibake est *dans* le texte, pas un problème de décodage) |
| Lignes de données | 10 000 |
| Colonnes | `id_produit, ean, nom, url_image, taxonomie_niveau_1..4, tva` — conformes |

### Mojibake : il n'est pas que dans `nom`

| Colonne | Lignes touchées | Exemple |
|---|---|---|
| `nom` | 14 | `CAFÃ‰ PUR ARAB 250 GR`, `CRÃˆME FR. EPAISSE 20 CL` |
| `taxonomie_niveau_2` | **74** | `Ã‰PICERIE SUCREE` |
| `taxonomie_niveau_3` | **30** | `CRÃˆMERIE` |

→ **Règle** : `ftfy.fix_text` sur **toutes** les colonnes texte. Ne pas se limiter au libellé.
Sinon `Ã‰PICERIE SUCREE` reste une 11ᵉ catégorie fantôme dans le référentiel.

---

## 2. `id_produit`

10 000 valeurs, **10 000 distinctes**, 100 % au format UUID, 0 vide.
→ Clé technique fiable de la ligne brute. Utilisable comme clé d'idempotence
`(store_id, run_hash, id_produit)`. **Mais ce n'est pas une clé produit** (voir §3).

---

## 3. `ean` — le piège principal

Distribution des longueurs :

| Longueur | Lignes | % |
|---|---|---|
| 0 (vide) | 1 300 | 13,00 % |
| 3–5 | 450 | 4,50 % |
| 8 | 1 438 | 14,38 % |
| 9–11 | 69 | 0,69 % |
| **13** | **5 204** | **52,04 %** |
| 14 | 1 290 | 12,90 % |
| 15–17 | 249 | 2,49 % |

**Checksum GS1 valide : 7 932 / 10 000 (79,32 %).** Les 768 restants (hors vides) se répartissent :

| Catégorie | Nombre | Traitement |
|---|---|---|
| Zéros de tête **manquants** (`'789'`, `'00020001018'`) | 386 | ✅ réparable auto → `zfill(13)` puis checksum revalidé |
| Zéros de tête **en trop** (`'0003760000001151'`) | 331 | ✅ réparable auto → `lstrip('0')` si le checksum tient |
| Code court non-EAN (`'31061'`, PLU/code interne) | 133 | ⚠️ à **router**, pas à corriger : champ `internal_code` |
| Réellement invalide | **0** | — |

### Les 1 290 codes à 14 chiffres ne sont pas une erreur de saisie

Tous ont un **checksum GS1 valide**, et ils recouvrent deux réalités opposées :

| Forme | Nombre | Ce que c'est | Traitement |
|---|---|---|---|
| `0` + 13 chiffres | 82 | Le **même** identifiant que le GTIN-13, avec un zéro de rembourrage. Les zéros de tête ne changent pas le chiffre de contrôle. | ✅ canonisé en 13 chiffres (compté dans les 331 ci-dessus) |
| `1` + 13 chiffres | 1 208 | Un **vrai GTIN-14** : le premier chiffre est l'indicateur de conditionnement. C'est le **carton**, pas l'unité vendue. | ⚠️ `ean_gtin14`, signalé, **jamais converti** |

Preuve que les seconds ne sont pas des EAN-13 abîmés : en retirant le chiffre de
tête, les 13 restants ont un checksum **faux** — le chiffre de contrôle a bien
été recalculé pour le GTIN-14. Quelqu'un a scanné le carton.

→ **Règle** : un GTIN-14 ne vaut pas comme EAN consommateur — une marketplace
attend l'unité vendue. Il est signalé et n'alimente pas la fiche produit. On ne
le convertit pas : déduire le GTIN-13 demanderait de **recalculer un chiffre de
contrôle**, donc d'inventer la donnée.

→ **Règle** : la réparation par zéros de tête n'est appliquée **que si le checksum
devient valide après réparation**. Sinon on ne touche à rien. 635 EAN récupérables
sur 768 anomalies, sans aucun risque.

### Pourquoi l'EAN ne peut pas servir de clé de fusion

**8 700 EAN non vides, 8 700 distincts.** Aucun EAN n'est porté par deux lignes.
Or le libellé `POM B 1K C1` apparaît 244 fois — avec 215 EAN **tous différents**.

Ce sont des EAN synthétiques (préfixe dominant `3760000` sur 5 204 lignes, séquentiels) :
ils identifient la **ligne**, pas le **produit**.

→ **Règle** : l'EAN est une donnée à fiabiliser et à conserver, **jamais une clé de
rapprochement**. La résolution d'entités se fait sur libellé normalisé + attributs
extraits (marque, contenance, unité).

---

## 4. `nom` — libellés caisse

| Mesure | Valeur |
|---|---|
| Distincts (bruts et normalisés) | **343** |
| Longueur min / médiane / max | 6 / 17 / 38 |
| Vides | 0 |
| Tout en majuscules | 10 000 (100 %) |
| Mojibake | 14 |
| **Suffixe marketing collé** | **153** |

Suffixes collés (mesure par token, pas par regex de contenance) :
`ORIGINE ITALIA` (79 lignes), `FAMILY SIZE` (74 lignes).

```
'LESSIVE 2LORIGINE ITALIA'        →  'LESSIVE 2L'      + origine='ITALIA'
'COCA COLA ZERO 1LFAMILY SIZE'    →  'COCA COLA ZERO 1L' + mention='FAMILY SIZE'
'TOMATES GRAPPEFAMILY SIZE'       →  'TOMATES GRAPPE'  + mention='FAMILY SIZE'
'ARABICA MOULU 0.25KGORIGINE ITALIA' → 'ARABICA MOULU 0.25KG' + origine='ITALIA'
```

→ **Règle** : détachement par **liste fermée de suffixes connus** (déterministe,
config), pas par heuristique de casse. Le suffixe part dans un champ dédié, il
n'est pas jeté — c'est de l'information commerciale.

Libellés les plus fréquents : `POM B 1K C1` (244), `COUCHES PAMP TAILLE 4` (242),
`TOM GRA FR KG` (241), `CAFE MLU 250G` (236), `CREME FRAICHE 30 20CL` (236),
`WHISKY ECOSSAIS 70 CL` (229).

---

## 5. `url_image`

| Schéma | Lignes |
|---|---|
| `https` | 9 036 |
| *(vide)* | 800 |
| *(aucun schéma)* — `www.store.example/…` | 82 |
| `htp://` (tronqué) | 43 |
| `ftp://` | 39 |

**9 085 URLs distinctes, réparties sur seulement 6 hôtes** :

| Hôte | URLs | Réalité réseau (testée) |
|---|---|---|
| `picsum.photos` | 8 600 | **DNS ok, HTTP 200 en 0,5 s** — service réel et vivant |
| `cdn.store.example` | 443 | **DNS FAIL** |
| `files.store.example` | 39 | **DNS FAIL** |
| `www.store.example` | 43 | **DNS FAIL** |

Par ailleurs 419 URLs ont un chemin `/missing/` (mortes par construction).

### Mesures d'un run réseau réel (2026-08-09)

Le premier run avec vérification HTTP effective a corrigé deux illusions
qu'aucun test ne détectait — voir §10.

| | 1ᵉʳ run | après correction |
|---|---|---|
| Résolutions DNS | 9 044 | **3** (une par domaine) |
| Requêtes HTTP | 17 553 | **8 609** |
| Durée totale du run | 25 min 30 | **4 min 03** |
| URLs joignables | 8 430 | 8 600 |

→ **Règles**, dictées par ces chiffres :
1. **Cache négatif au niveau du DOMAINE**, pas seulement de l'URL. Si `cdn.store.example`
   ne résout pas, on ne lance pas 443 requêtes vouées à l'échec : 1 résolution DNS suffit
   à invalider les 443. Économie mesurée : **525 requêtes → 3 checks DNS**.
2. **Limite de concurrence PAR DOMAINE** : 8 600 des 9 085 URLs sont sur `picsum.photos`.
   Sans limite par domaine, on DoS un service tiers et on se fait blacklister.
3. Cache URL→statut **avec TTL en base** : d'un jour sur l'autre le même magasin
   redépose les mêmes URLs. Sans cache, 9 085 HEAD/jour pour rien.
4. Réparation de schéma déterministe : `htp://`→`https://`, `www.X`→`https://www.X`.
   `ftp://` et `not-a-url` → **pas de réparation**, signalement.

---

## 6. Taxonomie

| Niveau | Rempli | Valeurs distinctes |
|---|---|---|
| `niveau_1` | 10 000 (100 %) | 3 |
| `niveau_2` | 9 350 (93,5 %) | 8 |
| `niveau_3` | 9 600 (96,0 %) | 11 |
| `niveau_4` | 9 600 (96,0 %) | 10 |

Formes observées :

| Forme n1..n4 | Lignes | Nature |
|---|---|---|
| `X X X X` | 9 350 | complète |
| `X _ _ _` | 400 | tronquée après n1 → **à compléter (LLM)** |
| `X _ X X` | **250** | **trouée** : n2 vide alors que n3 et n4 sont remplis |

Les 250 lignes trouées sont **réparables sans LLM** : n3+n4 déterminent n2 de façon
univoque (0 conflit mesuré — aucun niveau 2 n'est rattaché à deux niveaux 1 différents).

```
'PATE TART NUTELLA 0.75KG'  ['ALIMENTAIRE', '', 'PATES A TARTINER', 'NOISETTE CACAO']
                                            ↑ déductible : EPICERIE SUCREE
```

### Référentiel de taxonomie (10 chemins complets, extraits du fichier)

```
SANTE       > MEDICAMENTS       > ANTALGIQUES      > PARACETAMOL     (955)
ALIMENTAIRE > BOISSONS          > ALCOOLS          > SPIRITUEUX      (950)
ALIMENTAIRE > FRUITS ET LEGUMES > LEGUMES          > TOMATES         (939)
NON ALIM.   > ENTRETIEN         > LESSIVE          > LIQUIDE         (938)
ALIMENTAIRE > FRUITS ET LEGUMES > FRUITS           > POMMES          (930)
NON ALIM.   > BEBE              > COUCHES          > TAILLE 4        (927)
ALIMENTAIRE > BOISSONS          > SODAS            > COLA            (919)
ALIMENTAIRE > EPICERIE SUCREE   > PATES A TARTINER > NOISETTE CACAO  (908)
ALIMENTAIRE > PRODUITS FRAIS    > CREMERIE         > CREMES          (903)
ALIMENTAIRE > EPICERIE SUCREE   > CAFE THE         > CAFE MOULU      (877)
```
+ 3 chemins fantômes dus au mojibake (`Ã‰PICERIE SUCREE` ×74, `CRÃˆMERIE` ×30) qui
disparaissent une fois `ftfy` appliqué aux colonnes de taxonomie.

→ **Règle** : ce référentiel de 10 chemins est **extrait du fichier, versionné en
config**, et injecté dans le prompt LLM comme liste fermée. Le LLM choisit dans une
liste, il n'invente pas de catégorie.

---

## 7. `tva` — aucun taux illégal, que des incohérences métier

| Taux | Lignes | % | Légal FR |
|---|---|---|---|
| 5,5 % | 5 819 | 58,19 % | ✅ |
| 20 % | 2 961 | 29,61 % | ✅ |
| 2,1 % | 1 089 | 10,89 % | ✅ |
| 10 % | 131 | 1,31 % | ✅ |

**Le contrôle syntaxique ne détecte rien.** Toute la valeur est dans le contrôle croisé.

Croisement par famille métier (matching par **token exact** — voir l'avertissement ci-dessous) :

| Famille | Lignes | Répartition TVA | Suspects |
|---|---|---|---|
| Alcool (attendu 20 %) | 957 | `20`=925, `10`=15, `5.5`=9, `2.1`=8 | **32** |
| Médicament (attendu 2,1 %) | 906 | `2.1`=866, `10`=15, `5.5`=14, `20`=11 | **40** |

> ⚠️ **Piège rencontré et corrigé** : une première passe en *matching par sous-chaîne*
> remontait 89 faux positifs — `"FRORIGINE"` contient `"GIN"`, donc les tomates
> étaient classées spiritueux. Le matching de mots-clés doit se faire **par token
> après normalisation**, jamais par `in`. Ce test est déjà dans le plan de tests
> de la phase 3 (`cross_checks`).

### Le signal le plus exploitable : le consensus intra-groupe

40 libellés sur 343 portent **plusieurs taux de TVA**, avec une majorité écrasante :

```
'POM B 1K C1'            5.5 ×242 | 2.1 ×2      → les 2 lignes à 2.1 sont l'anomalie
'WHISKY ECOSSAIS 70 CL'  20  ×221 | 10 ×7 | 5.5 ×1
'COUCHES PAMP TAILLE 4'  20  ×229 | 5.5 ×7 | 2.1 ×4 | 10 ×2
```

Idem sur la taxonomie : `POM B 1K C1` → `POMMES` ×214, mais aussi `SPIRITUEUX` ×2,
`CAFE MOULU` ×3, `TOMATES` ×3 (bruit injecté).

→ **Règle** : le consensus intra-cluster est un **signal de détection** très fort ici,
mais il ne devient la **règle de correction** que via la table `catégorie→TVA` de
référence. Raison : le consensus n'est fiable que parce que ce fichier réplique
chaque produit ~230 fois ; sur un vrai catalogue à 1 ligne par SKU il ne dit rien.
Le pipeline utilise donc **les deux** : table de référence = règle, consensus = signal
qui alimente le scoring de confiance.
Et dans tous les cas : **la TVA n'est jamais corrigée automatiquement** → `NEEDS_REVIEW`.

---

## 8. Doublons

| Mesure | Valeur |
|---|---|
| Lignes strictement identiques (`nom`+`ean`+`tva`) | 1 144 |
| Groupes de libellés normalisés identiques | 284 groupes / 9 941 lignes |
| **Réduction par dédup exacte seule** | **10 000 → 343** |
| Paires à comparer sans blocking (343²/2) | 58 653 |

→ **Note de dimensionnement honnête** : à 343 libellés distincts, 58 653 paires se
comparent en RapidFuzz en moins d'une seconde. **Le blocking n'est pas nécessaire
pour CE fichier** — il est nécessaire pour tenir la promesse « N magasins × 10k lignes »
sans exploser en O(n²). Je l'implémente donc pour l'échelle, en le mesurant sur ce
fichier pour prouver qu'il ne dégrade pas le rappel.

---

## 9. Récapitulatif : ce que chaque étage traite

| Anomalie | Volume | Étage | Auto-corrigeable ? |
|---|---|---|---|
| BOM + CRLF | tout le fichier | `ingest` | ✅ oui |
| Mojibake (nom + 2 col. taxonomie) | 118 lignes | `ingest` (ftfy) | ✅ oui |
| Suffixe marketing collé | 153 lignes | `ingest` (liste fermée) | ✅ oui |
| EAN zéros de tête | 635 lignes | `field_checks` | ✅ oui (si checksum revalide) |
| Code court non-EAN (PLU) | 133 lignes | `field_checks` | ⚠️ routage, pas correction |
| EAN vide | 1 300 lignes | `field_checks` | ❌ signalement |
| URL schéma tronqué (`htp://`, `www.`) | 125 lignes | `field_checks` | ✅ oui |
| URL `ftp://` / `not-a-url` | 39 + qq | `field_checks` | ❌ signalement |
| URL morte (domaine DNS FAIL) | 525 lignes | `field_checks` (HEAD + cache) | ❌ signalement |
| URL vide | 800 lignes | `field_checks` | ❌ signalement |
| Taxonomie trouée `X _ X X` | 250 lignes | `cross_checks` (déduction n3→n2) | ✅ oui |
| Taxonomie tronquée `X _ _ _` | 400 lignes | `llm_enrich` (liste fermée) | ⚠️ selon confiance |
| Taxonomie contredisant le nom | ~qq dizaines/libellé | `cross_checks` | ⚠️ review |
| **TVA incohérente** | **~72 lignes** | `cross_checks` | ❌ **jamais** → `NEEDS_REVIEW` |
| Quasi-doublons | 9 941 lignes → 343 | `entity_resolution` | ✅ fusion + golden record |
| Libellé caisse abrégé | 343 distincts | `llm_enrich` | ⚠️ selon confiance |

---

## 10. Ce que le premier run réseau a révélé

Deux défauts que la suite de tests validait comme corrects. Ils sont documentés
ici parce que le mécanisme se reproduira dans les phases suivantes.

### Le cache par domaine ne fonctionnait pas sous concurrence

Mesure : **9 044 résolutions DNS pour 6 domaines**, et un compteur
`requests_saved` négatif.

Toutes les URLs d'un domaine partent dans le même `asyncio.gather`. Chacune
franchit le test « ce domaine est-il déjà dans le cache ? » **avant** que la
première n'ait eu le temps d'y écrire son résultat : elles lancent donc toutes
leur propre résolution.

Correction : mémoriser la **tâche** de résolution, enregistrée avant le moindre
`await`, et non son résultat. Les suivantes attendent la même tâche.

> Le test correspondant était vert du début à la fin. Son faux `_resolve`
> retournait sans jamais suspendre : les coroutines s'exécutaient en file
> indienne et le scénario concurrent n'était jamais joué. **Un test de
> comportement concurrent doit contenir un point de suspension** — ici un
> `await asyncio.sleep(0)`. Sans lui, il teste autre chose que ce qu'il annonce.

### Le repli HEAD → GET était payé à chaque URL

Mesure : **17 553 requêtes pour 9 045 URLs**. `picsum.photos` répond 405 à HEAD,
donc chacune de ses 8 600 images coûtait un aller-retour perdu avant le GET.

Correction : mémoriser par domaine le refus de HEAD. La première URL le
découvre, les 8 599 suivantes vont directement en GET.

### Ce qu'il faut en retenir pour les phases suivantes

Ces deux défauts ont la même origine : **un compteur mesurait le coût réel, et
personne ne le regardait**. Les logs structurés exposent désormais
`dns_lookups`, `http_requests` et `requests_saved` à chaque run — et le nombre
d'appels LLM réels contre les hits de cache le sera de la même façon en phase 3.
Un cache dont on n'observe pas le taux de succès est un cache dont on ne sait
pas s'il existe.
