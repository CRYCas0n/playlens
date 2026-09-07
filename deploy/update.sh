#!/usr/bin/env bash
# Pull, build, migrate, restart, smoke -- and roll the code back if the smoke fails.
#
#   bash update.sh
#   SITE=playlens.example.com bash update.sh    # smoke against the public name too
#
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/playlens}"
BRANCH="${BRANCH:-main}"
cd "$APP_DIR"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
log() { printf '\033[1m[update]\033[0m %s\n' "$*"; }

PREVIOUS=$(git rev-parse HEAD)
log "current: $(git rev-parse --short HEAD)"

# A dump before every deploy, so a bad migration has something to go back to.
bash deploy/backup.sh || log "WARNING: backup failed -- continuing, but rollback will be code-only"

git fetch --quiet origin "$BRANCH"
git reset --hard --quiet "origin/$BRANCH"
log "updating to $(git rev-parse --short HEAD)"

"${COMPOSE[@]}" build
# Workers run the old code against the new schema for the length of the deploy. Every
# migration so far is additive, which makes that safe; if one ever is not, this is the
# line to change.
"${COMPOSE[@]}" up -d api          # migrations happen here
sleep 15
"${COMPOSE[@]}" up -d worker scheduler

if curl -fsS -m 10 http://127.0.0.1:8000/api/v1/health >/dev/null; then
  log "healthy at $(git rev-parse --short HEAD)"
  [ -n "${SITE:-}" ] && curl -fsS -m 15 "https://$SITE/api/v1/health" >/dev/null && log "public name answers too"
  exit 0
fi

log "UNHEALTHY -- rolling the code back to ${PREVIOUS:0:7}"
git reset --hard --quiet "$PREVIOUS"
"${COMPOSE[@]}" build
"${COMPOSE[@]}" up -d
log "rolled back. If the schema also needs it: python -m alembic downgrade -1 inside the api container."
exit 1
