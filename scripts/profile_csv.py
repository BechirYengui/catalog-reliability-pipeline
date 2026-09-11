#!/usr/bin/env python3
"""Profilage jetable du CSV d'échantillon. Stdlib uniquement, aucune dépendance.

Objectif : fonder les règles du pipeline sur ce que le fichier contient REELLEMENT.
Usage : python3 scripts/profile_csv.py data/samples/store_listing_produit.csv
"""
from __future__ import annotations

import csv
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import urlparse

LEGAL_VAT_FR = {"0", "0.0", "2.1", "5.5", "10", "10.0", "20", "20.0", "5.50", "2.10"}


def ean_checksum_ok(code: str) -> bool:
    """Checksum GS1 (EAN-8 / EAN-13 / UPC-A / GTIN-14)."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits[-1]
    body = digits[:-1][::-1]  # de droite a gauche depuis le rang juste avant la cle
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    return (10 - total % 10) % 10 == check


def normalize_label(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Ã©", "Ã¨", "Ã‰", "ï»¿", "Ãª", "Ã´", "Ã§")


def main(path: str) -> None:
    raw = open(path, "rb").read()

    print("=" * 78)
    print("1. ENCODAGE / STRUCTURE DU FICHIER")
    print("=" * 78)
    print(f"taille                 : {len(raw):,} octets")
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    n_crlf = raw.count(b"\r\n")
    n_lf_only = raw.count(b"\n") - n_crlf
    print(f"BOM UTF-8 en tete      : {has_bom}")
    print(f"fins de ligne CRLF     : {n_crlf:,}  |  LF seuls : {n_lf_only:,}")
    try:
        raw.decode("utf-8")
        print("decodable en UTF-8     : oui")
    except UnicodeDecodeError as e:
        print(f"decodable en UTF-8     : NON ({e})")

    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(text.splitlines()))
    cols = list(rows[0].keys()) if rows else []
    print(f"lignes de donnees      : {len(rows):,}")
    print(f"colonnes               : {cols}")

    # ------------------------------------------------------------------ id
    print()
    print("=" * 78)
    print("2. id_produit")
    print("=" * 78)
    ids = [r["id_produit"] or "" for r in rows]
    id_counts = Counter(ids)
    dup_ids = {k: v for k, v in id_counts.items() if v > 1}
    print(f"valeurs                : {len(ids):,}  |  distinctes : {len(id_counts):,}")
    print(f"ids en doublon         : {len(dup_ids):,} (soit {sum(dup_ids.values()):,} lignes)")
    print(f"vides                  : {sum(1 for i in ids if not i.strip()):,}")
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    print(f"au format UUID         : {sum(1 for i in ids if uuid_re.match(i)):,}")
    for k, v in list(dup_ids.items())[:3]:
        print(f"  exemple doublon      : {k} x{v}")

    # ----------------------------------------------------------------- ean
    print()
    print("=" * 78)
    print("3. ean")
    print("=" * 78)
    eans = [(r["ean"] or "").strip() for r in rows]
    by_len: Counter[int] = Counter(len(e) for e in eans)
    print("distribution des longueurs :")
    for length, n in sorted(by_len.items()):
        print(f"  len={length:>3} : {n:>6,}  ({100*n/len(eans):5.2f}%)")

    non_digit = [e for e in eans if e and not e.isdigit()]
    print(f"non entierement numeriques : {len(non_digit):,}")
    for e in list(dict.fromkeys(non_digit))[:8]:
        print(f"  ex: {e!r}")

    ok = sum(1 for e in eans if ean_checksum_ok(e))
    print(f"checksum GS1 VALIDE        : {ok:,}  ({100*ok/len(eans):.2f}%)")

    # Reparabilite : zeros de tete manquants / en trop
    repairable_pad = []
    repairable_strip = []
    truly_invalid = []
    non_ean_short = []
    for e in eans:
        if not e or ean_checksum_ok(e):
            continue
        if not e.isdigit():
            truly_invalid.append(e)
            continue
        if len(e) < 8:
            # code court type PLU / code interne : a router, pas a corriger
            if ean_checksum_ok(e.zfill(8)) or ean_checksum_ok(e.zfill(13)):
                repairable_pad.append(e)
            else:
                non_ean_short.append(e)
            continue
        if len(e) < 13 and ean_checksum_ok(e.zfill(13)):
            repairable_pad.append(e)
        elif len(e) > 13 and ean_checksum_ok(e.lstrip("0").zfill(13)):
            repairable_strip.append(e)
        elif len(e) == 14 and ean_checksum_ok(e[1:]):
            repairable_strip.append(e)
        else:
            truly_invalid.append(e)

    print(f"  -> reparable (zeros de tete a AJOUTER) : {len(repairable_pad):,}")
    for e in list(dict.fromkeys(repairable_pad))[:5]:
        print(f"       {e!r} -> {e.zfill(13)!r}")
    print(f"  -> reparable (zeros de tete a RETIRER) : {len(repairable_strip):,}")
    for e in list(dict.fromkeys(repairable_strip))[:5]:
        print(f"       {e!r}")
    print(f"  -> code court non-EAN (PLU/interne)    : {len(non_ean_short):,}")
    for e in list(dict.fromkeys(non_ean_short))[:5]:
        print(f"       {e!r}")
    print(f"  -> REELLEMENT invalide (a signaler)    : {len(truly_invalid):,}")
    for e in list(dict.fromkeys(truly_invalid))[:8]:
        print(f"       {e!r}")

    ean_counts = Counter(e for e in eans if e)
    dup_eans = {k: v for k, v in ean_counts.items() if v > 1}
    print(f"EAN portes par >1 ligne    : {len(dup_eans):,} EAN / {sum(dup_eans.values()):,} lignes")

    # ----------------------------------------------------------------- nom
    print()
    print("=" * 78)
    print("4. nom (libelle)")
    print("=" * 78)
    noms = [(r["nom"] or "") for r in rows]
    print(f"distincts bruts            : {len(set(noms)):,}")
    norm = [normalize_label(n) for n in noms]
    print(f"distincts apres normalisation : {len(set(norm)):,}")
    lens = sorted(len(n) for n in noms)
    print(f"longueur  min/median/max   : {lens[0]} / {lens[len(lens)//2]} / {lens[-1]}")
    print(f"vides                      : {sum(1 for n in noms if not n.strip()):,}")
    print(f"tout en MAJUSCULES         : {sum(1 for n in noms if n.isupper()):,}")
    moji = [n for n in noms if any(m in n for m in MOJIBAKE_MARKERS)]
    print(f"suspects de MOJIBAKE       : {len(moji):,}")
    for n in list(dict.fromkeys(moji))[:8]:
        print(f"  ex: {n!r}")

    # suffixe marketing colle : ...1LORIGINE ITALIA / minuscule collee a majuscule
    glued = [n for n in noms if re.search(r"[0-9](?:L|CL|ML|G|KG)[A-Z]{4,}", n)]
    print(f"suffixe marketing colle    : {len(glued):,}")
    for n in list(dict.fromkeys(glued))[:8]:
        print(f"  ex: {n!r}")

    print("libelles les plus frequents :")
    for n, c in Counter(noms).most_common(8):
        print(f"  {c:>4}x  {n!r}")

    # familles de quasi-doublons (par cle normalisee grossiere)
    fam: dict[str, set[str]] = defaultdict(set)
    for n, nn in zip(noms, norm):
        key = " ".join(sorted(set(nn.split()) - {"DE", "LA", "LE", "DU", "ET"}))
        fam[key].add(n)
    multi = sorted(((len(v), k, v) for k, v in fam.items() if len(v) > 1), reverse=True)
    print(f"cles normalisees portant >1 orthographe : {len(multi):,}")
    for cnt, key, variants in multi[:5]:
        print(f"  {cnt} variantes -> {sorted(variants)[:4]}")

    # ----------------------------------------------------------- url_image
    print()
    print("=" * 78)
    print("5. url_image")
    print("=" * 78)
    urls = [(r["url_image"] or "").strip() for r in rows]
    schemes = Counter()
    hosts = Counter()
    for u in urls:
        if not u:
            schemes["<vide>"] += 1
            continue
        p = urlparse(u)
        schemes[p.scheme or "<aucun>"] += 1
        if p.netloc:
            hosts[p.netloc] += 1
    print("schemes :")
    for s, c in schemes.most_common():
        print(f"  {s:<12} : {c:>6,}")
    print("domaines :")
    for h, c in hosts.most_common(10):
        print(f"  {h:<40} : {c:>6,}")
    bad_scheme = [u for u in urls if u and urlparse(u).scheme not in ("http", "https")]
    print(f"scheme non http(s) (tronque ?) : {len(bad_scheme):,}")
    for u in list(dict.fromkeys(bad_scheme))[:8]:
        print(f"  ex: {u!r}")
    print(f"URLs distinctes                : {len(set(u for u in urls if u)):,}")
    suspicious = [u for u in urls if "missing" in u or "404" in u or "placeholder" in u]
    print(f"URLs au chemin suspect         : {len(suspicious):,}")
    for u in list(dict.fromkeys(suspicious))[:5]:
        print(f"  ex: {u!r}")

    # ---------------------------------------------------------- taxonomie
    print()
    print("=" * 78)
    print("6. taxonomie (4 niveaux)")
    print("=" * 78)
    tax_cols = [c for c in cols if c.startswith("taxonomie")]
    for c in tax_cols:
        vals = [(r[c] or "").strip() for r in rows]
        filled = sum(1 for v in vals if v)
        print(f"{c:<22} rempli {filled:>6,} ({100*filled/len(vals):5.1f}%)  distinct {len(set(v for v in vals if v)):>4}")
    depth = Counter()
    holes = 0
    for r in rows:
        vals = [(r[c] or "").strip() for c in tax_cols]
        d = 0
        for v in vals:
            if v:
                d += 1
            else:
                break
        depth[d] += 1
        # trou = niveau vide suivi d'un niveau rempli
        if any(not vals[i] and vals[i + 1] for i in range(len(vals) - 1)):
            holes += 1
    print("profondeur complete par ligne :")
    for d in sorted(depth):
        print(f"  {d} niveau(x) : {depth[d]:>6,} ({100*depth[d]/len(rows):5.1f}%)")
    print(f"hierarchies TROUEES (niveau vide puis rempli) : {holes:,}")
    print("top niveau 1 :")
    for v, c in Counter((r[tax_cols[0]] or "").strip() for r in rows).most_common(10):
        print(f"  {v!r:<35} : {c:>6,}")

    # incoherence hierarchique : un meme n2 rattache a plusieurs n1
    parent: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        n1, n2 = (r[tax_cols[0]] or "").strip(), (r[tax_cols[1]] or "").strip()
        if n1 and n2:
            parent[n2].add(n1)
    conflicting = {k: v for k, v in parent.items() if len(v) > 1}
    print(f"niveau2 rattaches a plusieurs niveau1 : {len(conflicting):,}")
    for k, v in list(conflicting.items())[:5]:
        print(f"  {k!r} <- {sorted(v)}")

    # ----------------------------------------------------------------- tva
    print()
    print("=" * 78)
    print("7. tva")
    print("=" * 78)
    tvas = [(r["tva"] or "").strip() for r in rows]
    for v, c in Counter(tvas).most_common():
        legal = "legal FR" if v in LEGAL_VAT_FR else ">>> HORS TAUX LEGAL"
        print(f"  {v!r:<10} : {c:>6,} ({100*c/len(tvas):5.2f}%)  {legal}")

    # croisement categorie x tva : reperer les incoherences metier
    print()
    print("croisement taxonomie_niveau_1 x tva :")
    cross: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        cross[(r[tax_cols[0]] or "?").strip()][(r["tva"] or "").strip()] += 1
    for cat, cnt in sorted(cross.items()):
        parts = ", ".join(f"{k}%={v}" for k, v in cnt.most_common())
        print(f"  {cat:<32} {parts}")

    # signaux metier : alcool a taux reduit = incoherence classique
    print()
    print("signaux metier (a confirmer en phase cross_checks) :")
    alcohol_kw = ("WHISKY", "VODKA", "RHUM", "GIN", "BIERE", "VIN", "CHAMPAGNE", "PASTIS", "TEQUILA", "COGNAC")
    hits = [
        (r["nom"], r["tva"], (r[tax_cols[0]] or ""))
        for r in rows
        if any(k in normalize_label(r["nom"] or "") for k in alcohol_kw)
    ]
    bad_alcohol = [h for h in hits if h[1] not in ("20", "20.0")]
    print(f"  produits alcoolises detectes           : {len(hits):,}")
    print(f"  dont TVA != 20% (suspect)              : {len(bad_alcohol):,}")
    for n, t, c in list(dict.fromkeys(bad_alcohol))[:10]:
        print(f"    {t:>4}%  {n!r}  [{c}]")

    # --------------------------------------------------------- doublons
    print()
    print("=" * 78)
    print("8. DOUBLONS (le nerf de la guerre)")
    print("=" * 78)
    exact_key = Counter((r["nom"], r["ean"], r["tva"]) for r in rows)
    print(f"lignes strictement identiques (nom+ean+tva) : {sum(v - 1 for v in exact_key.values() if v > 1):,}")
    norm_key = Counter(norm)
    n_dupe_groups = sum(1 for v in norm_key.values() if v > 1)
    n_dupe_rows = sum(v for v in norm_key.values() if v > 1)
    print(f"groupes de libelles normalises identiques   : {n_dupe_groups:,} ({n_dupe_rows:,} lignes)")
    print("plus gros groupes apres normalisation :")
    for k, c in norm_key.most_common(10):
        print(f"  {c:>4}x  {k!r}")
    print()
    print(f"=> BORNE BASSE de reduction par dedup exacte : {len(rows):,} -> {len(set(norm)):,} lignes")
    print("   (le fuzzy/embeddings de la phase 2 ira plus loin)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/samples/store_listing_produit.csv")
