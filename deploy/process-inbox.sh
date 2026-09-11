#!/usr/bin/env bash
# Traite tous les CSV deposes dans /srv/catalog/inbox.
#
# Declenche par le systemd timer, ou a la main : `catalog-run`
#
# Pour chaque fichier :
#   - lance le pipeline dans un conteneur ephemere
#   - ecrit le rapport dans reports/<fichier>-<horodatage>.json
#   - deplace le CSV dans processed/ (succes) ou quarantine/ (echec)
#
# Le CSV n'est JAMAIS supprime : en cas de doute on veut pouvoir rejouer la
# source exacte. Le pipeline etant idempotent, rejouer est sans risque.

set -euo pipefail

APP_DIR=/srv/catalog
COMPOSE="docker compose -f ${APP_DIR}/docker-compose.prod.yml --env-file ${APP_DIR}/.env"
STAMP=$(date +%Y%m%d-%H%M%S)

cd "$APP_DIR"

shopt -s nullglob
files=("$APP_DIR"/inbox/*.csv)

if [ ${#files[@]} -eq 0 ]; then
  echo "inbox vide, rien a faire"
  exit 0
fi

echo "=== ${#files[@]} fichier(s) a traiter ==="
failures=0

for path in "${files[@]}"; do
  name=$(basename "$path" .csv)
  # L'identifiant de magasin est le prefixe du nom de fichier avant le premier
  # underscore, sinon le nom complet. Convention simple et lisible :
  #   carrefour-lyon_2026-08-09.csv -> store_id = carrefour-lyon
  store="${name%%_*}"
  report="reports/${name}-${STAMP}.json"

  echo
  echo "--- ${name}.csv (magasin: ${store}) ---"

  if $COMPOSE run --rm pipeline run "/data/inbox/${name}.csv" \
       --store-id "$store" --output "/data/${report}"; then
    mv "$path" "$APP_DIR/processed/${name}-${STAMP}.csv"
    echo "OK -> processed/${name}-${STAMP}.csv"
  else
    # Le pipeline sort en code 1 quand le run part en QUARANTINE : le fichier
    # est mis de cote et rien n'est integre.
    mv "$path" "$APP_DIR/quarantine/${name}-${STAMP}.csv"
    echo "QUARANTAINE -> quarantine/${name}-${STAMP}.csv"
    failures=$((failures + 1))
  fi
done

# Index HTML des rapports, regenere a chaque passage. Volontairement statique :
# tant que l'API de la phase 4 n'existe pas, un fichier suffit a consulter les
# resultats, et ca ne fait tourner aucun service supplementaire.
"$APP_DIR/render-index.sh" 2>/dev/null || true

echo
echo "=== termine : $(( ${#files[@]} - failures )) succes, ${failures} en quarantaine ==="
exit 0
