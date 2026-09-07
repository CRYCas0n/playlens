#!/usr/bin/env bash
# pg_dump into a local directory, with retention. Run it from cron.
#
# This is a backup on the same disk as the database, which protects against the two
# things that actually happen -- a bad migration and a wrong DELETE -- and not against
# losing the machine. Off-site copies need somewhere to put them; see docs/FINAL_
# DEPLOYMENT_PLAN.md, which says plainly that this one is not off-site.
#
#   bash backup.sh                       # /opt/playlens/backups/playlens-<ts>.sql.gz
#   RETENTION_DAYS=30 bash backup.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/playlens}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
cd "$APP_DIR"

# Read from .env without echoing it.
set -a; . ./.env; set +a
: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/playlens-$STAMP.sql.gz"

# --clean --if-exists so the dump can be restored over a live schema.
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
  | gzip -9 > "$OUT.partial"

# Rename only after a clean exit: a truncated file with the right name is worse than no
# file, because it looks like a backup.
mv "$OUT.partial" "$OUT"
chmod 600 "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Delete old dumps only inside the backup directory, only files this script's own naming
# scheme produced, and only after at least one recent backup exists.
if [ "$(find "$BACKUP_DIR" -maxdepth 1 -name 'playlens-*.sql.gz' -mtime -1 | wc -l)" -gt 0 ]; then
  find "$BACKUP_DIR" -maxdepth 1 -name 'playlens-*.sql.gz' -mtime "+$RETENTION_DAYS" -print -delete
else
  echo "no backup from the last day -- keeping every old dump" >&2
fi
