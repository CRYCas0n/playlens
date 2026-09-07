#!/usr/bin/env bash
# Restore a dump made by backup.sh. Destructive by nature, so it asks.
#
#   bash restore.sh /opt/playlens/backups/playlens-20260907-120000.sql.gz
#
set -euo pipefail
DUMP="${1:?usage: restore.sh <dump.sql.gz>}"
APP_DIR="${APP_DIR:-/opt/playlens}"
cd "$APP_DIR"
[ -r "$DUMP" ] || { echo "cannot read $DUMP" >&2; exit 1; }
set -a; . ./.env; set +a
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

echo "This REPLACES the contents of database '$POSTGRES_DB' with $DUMP."
printf 'Type the word restore to continue: '
read -r answer
[ "$answer" = "restore" ] || { echo "aborted"; exit 1; }

# Writers down first: restoring under a running worker gives you a database that is half
# the dump and half whatever the worker did while it loaded.
"${COMPOSE[@]}" stop worker scheduler api
gunzip -c "$DUMP" | "${COMPOSE[@]}" exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
"${COMPOSE[@]}" up -d api
"${COMPOSE[@]}" up -d worker scheduler
echo "restored. Check /api/v1/health and the catalogue."
