#!/usr/bin/env bash
# Genere reports/index.html a partir des rapports JSON.
#
# Vue minimale et statique, en attendant le dashboard React de la phase 4.
# Aucun service a faire tourner : nginx sert un fichier, c'est tout.

set -euo pipefail
REPORTS=/srv/catalog/reports

python3 - "$REPORTS" <<'PY'
import html
import json
import pathlib
import sys

reports_dir = pathlib.Path(sys.argv[1])
rows = []

for path in sorted(reports_dir.glob("*.json"), reverse=True):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        continue
    rows.append((path.name, data))

def cell(value: str, extra: str = "") -> str:
    return f"<td {extra}>{html.escape(str(value))}</td>"

body = []
for name, r in rows:
    quarantined = r.get("quarantined")
    status = "QUARANTAINE" if quarantined else "OK"
    color = "#b00" if quarantined else "#070"
    rate = r.get("publishable_rate", 0)
    anomalies = sum(r.get("anomalies_by_code", {}).values())
    corrections = sum(r.get("corrections_by_author", {}).values())
    detail = "".join(
        f"<li>{html.escape(k)} : <b>{v:,}</b></li>"
        for k, v in sorted(r.get("anomalies_by_code", {}).items())
    )
    body.append(
        f"""<tr>
  <td><code>{html.escape(name)}</code></td>
  {cell(r.get('store_id', '?'))}
  <td style="color:{color};font-weight:600">{status}</td>
  {cell(f"{r.get('rows_in', 0):,} -> {r.get('rows_out', 0):,}")}
  <td><b>{rate:.1%}</b></td>
  {cell(f"{anomalies:,}")}
  {cell(f"{corrections:,}")}
  {cell((r.get('started_at') or '')[:19].replace('T', ' '))}
</tr>
<tr class="detail"><td colspan="8"><details><summary>anomalies par code</summary>
  <ul>{detail}</ul></details></td></tr>"""
    )

page = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catalog — rapports de run</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 68rem; padding: 0 1rem; }}
  h1 {{ font-size: 1.4rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #8884; }}
  th {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; opacity: .7; }}
  tr.detail td {{ border-bottom: 2px solid #8884; padding-top: 0; }}
  ul {{ columns: 3; margin: .4rem 0; font-size: .9rem; }}
  code {{ font-size: .85rem; }}
  .empty {{ opacity: .6; font-style: italic; }}
</style></head><body>
<h1>Catalog — rapports de run</h1>
<p>{len(rows)} rapport(s). Un produit est <b>publiable</b> s'il a un EAN
exploitable, une image valide, une taxonomie complète et une TVA cohérente.</p>
<table>
<tr><th>Rapport</th><th>Magasin</th><th>Statut</th><th>Lignes</th>
    <th>Publiable</th><th>Anomalies</th><th>Corrections</th><th>Début</th></tr>
{''.join(body) if body else '<tr><td colspan="8" class="empty">Aucun rapport : déposez un CSV dans /srv/catalog/inbox</td></tr>'}
</table>
</body></html>"""

index = reports_dir / "index.html"
index.write_text(page, encoding="utf-8")
# Permissions explicites : ce fichier est servi par nginx (www-data). S'en
# remettre au umask ambiant l'a deja rendu illisible une fois, et l'interface
# repondait 403.
index.chmod(0o644)
print(f"index.html regenere ({len(rows)} rapport(s))")
PY
