#!/usr/bin/env python3
"""Second passage de profilage : verifie les 3 points qui decident de l'architecture.

1. Les EAN sont-ils vraiment tous uniques ? (=> peut-on fusionner par libelle ?)
2. Quelle est la vraie incoherence categorie<->TVA (matching par TOKEN, pas substring) ?
3. Quelle est la structure exacte des suffixes marketing et des trous de taxonomie ?
"""
from __future__ import annotations

import csv
import re
import sys
import unicodedata
from collections import Counter, defaultdict


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", s.upper()).split())


def ean_ok(code: str) -> bool:
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    d = [int(c) for c in code]
    body = d[:-1][::-1]
    return (10 - sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(body)) % 10) % 10 == d[-1]


rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8-sig")))
TAX = [f"taxonomie_niveau_{i}" for i in range(1, 5)]

print("=" * 78)
print("A. LES EAN SONT-ILS DISCRIMINANTS ? (decide la strategie de fusion)")
print("=" * 78)
by_label: dict[str, list[dict]] = defaultdict(list)
for r in rows:
    by_label[norm(r["nom"])].append(r)

big = sorted(by_label.items(), key=lambda kv: -len(kv[1]))[:3]
for label, group in big:
    eans = [r["ean"].strip() for r in group]
    non_empty = [e for e in eans if e]
    print(f"\nlibelle {label!r} -> {len(group)} lignes")
    print(f"  EAN non vides {len(non_empty)}, distincts {len(set(non_empty))}")
    print(f"  echantillon EAN : {sorted(set(non_empty))[:6]}")
    print(f"  TVA presentes   : {dict(Counter(r['tva'] for r in group))}")
    print(f"  taxonomies n4   : {dict(Counter(r[TAX[3]] for r in group))}")
    print(f"  URLs distinctes : {len(set(r['url_image'] for r in group))}")

allean = [r["ean"].strip() for r in rows if r["ean"].strip()]
print(f"\nEAN non vides : {len(allean):,} | distincts : {len(set(allean)):,}")
prefixes = Counter(e[:7] for e in allean if len(e) >= 13)
print(f"prefixes (7 premiers chiffres) les plus frequents, len>=13 :")
for p, c in prefixes.most_common(5):
    print(f"  {p} : {c:,}")
print("=> si tous les EAN sont uniques et sequentiels, ce sont des EAN SYNTHETIQUES :")
print("   ils identifient la LIGNE, pas le PRODUIT. La fusion doit se faire sur le")
print("   libelle+attributs, et l'EAN devient une donnee a conserver, pas une cle.")

print()
print("=" * 78)
print("B. INCOHERENCE CATEGORIE <-> TVA (matching par token exact)")
print("=" * 78)
ALCOHOL = {"WHISKY", "VODKA", "RHUM", "GIN", "BIERE", "BIERES", "VIN", "CHAMPAGNE",
           "PASTIS", "TEQUILA", "COGNAC", "RICARD", "BTL"}
MEDS = {"DOLIPRANE", "DOLIP", "PARACETAMOL", "IBUPROFENE", "ASPIRINE", "DOLI"}
suspects: dict[str, Counter] = defaultdict(Counter)
for r in rows:
    toks = set(norm(r["nom"]).split())
    tva = r["tva"].strip()
    if toks & ALCOHOL:
        suspects["ALCOOL (attendu 20%)"][tva] += 1
    if toks & MEDS:
        suspects["MEDICAMENT (attendu 2.1%)"][tva] += 1
for fam, cnt in suspects.items():
    total = sum(cnt.values())
    print(f"{fam} : {total:,} lignes -> {dict(cnt.most_common())}")

print("\ndetail : un meme libelle porte-t-il plusieurs TVA ?")
multi_tva = {lab: Counter(r["tva"] for r in g) for lab, g in by_label.items()}
multi = {lab: c for lab, c in multi_tva.items() if len(c) > 1}
print(f"  libelles portant >1 taux de TVA : {len(multi)} / {len(by_label)}")
for lab, c in sorted(multi.items(), key=lambda kv: -sum(kv[1].values()))[:8]:
    print(f"    {lab!r:<38} {dict(c.most_common())}")

print()
print("=" * 78)
print("C. SUFFIXES MARKETING & TROUS DE TAXONOMIE")
print("=" * 78)
SUFFIXES = ["ORIGINE ITALIA", "FAMILY SIZE", "ORIGINEITALIA", "FAMILYSIZE"]
glued = Counter()
for r in rows:
    n = r["nom"]
    for s in SUFFIXES:
        if s.replace(" ", "") in n.replace(" ", "") and not n.endswith(" " + s):
            glued[s] += 1
            break
print(f"lignes avec suffixe marketing colle : {sum(glued.values()):,} -> {dict(glued)}")
print("exemples et decoupe attendue :")
seen = set()
for r in rows:
    n = r["nom"]
    for s in SUFFIXES:
        if s.replace(" ", "") in n.replace(" ", "") and s not in seen:
            idx = n.replace(" ", "").index(s.replace(" ", ""))
            print(f"  {n!r}")
            seen.add(s)

print("\ntrous de taxonomie :")
pattern = Counter()
for r in rows:
    pattern[tuple(bool(r[c].strip()) for c in TAX)] += 1
for p, c in pattern.most_common():
    shape = " ".join("X" if b else "_" for b in p)
    print(f"  n1..n4 = [{shape}] : {c:,}")
print("\nexemples de lignes trouees (n2 vide, n3/n4 remplis) :")
k = 0
for r in rows:
    if not r[TAX[1]].strip() and r[TAX[2]].strip():
        print(f"  {r['nom']!r:<34} {[r[c] for c in TAX]}")
        k += 1
        if k >= 5:
            break

print("\nchemins de taxonomie complets observes (referentiel de reference) :")
paths = Counter(tuple(r[c].strip() for c in TAX) for r in rows if all(r[c].strip() for c in TAX))
for p, c in paths.most_common():
    print(f"  {c:>5}x  {' > '.join(p)}")

print()
print("=" * 78)
print("D. VOLUMETRIE POUR LE DIMENSIONNEMENT")
print("=" * 78)
labels = set(norm(r["nom"]) for r in rows)
print(f"lignes                                    : {len(rows):,}")
print(f"libelles distincts (apres normalisation)  : {len(labels):,}")
print(f"=> appels LLM au pire (1/libelle distinct): {len(labels):,}")
print(f"=> embeddings a calculer                  : {len(labels):,} vecteurs")
print(f"URLs distinctes a verifier (HEAD)         : {len(set(r['url_image'] for r in rows if r['url_image'].strip())):,}")
hosts = Counter(re.sub(r'^\w+://', '', u).split('/')[0] for u in (r['url_image'] for r in rows) if u.strip())
print(f"  reparties sur {len(hosts)} hotes -> {dict(hosts.most_common())}")
n = len(labels)
print(f"paires a comparer sans blocking            : {n*(n-1)//2:,}")
