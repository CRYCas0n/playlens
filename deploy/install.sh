#!/usr/bin/env bash
# Deploy Playlens from git onto a server that is already running other things.
#
# Idempotent: safe to run again. It creates only under $APP_DIR, publishes only on
# loopback, and touches no service it did not create. It does not install Docker, edit
# sshd_config, change firewall rules or restart anything belonging to anyone else --
# on a server with a live site and an n8n beside it, that restraint is the point.
#
#   APP_DIR=/opt/playlens bash install.sh
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/playlens}"
REPO="${REPO:-https://github.com/CRYCas0n/playlens.git}"
BRANCH="${BRANCH:-main}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

log() { printf '\033[1m[playlens]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[playlens] %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon (are you in the docker group?)"

# ---------------------------------------------------------------- code
if [ -d "$APP_DIR/.git" ]; then
  log "updating $APP_DIR"
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet "$BRANCH"
  git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH"
else
  log "cloning into $APP_DIR"
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"
log "at $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"

# ---------------------------------------------------------------- secrets
# Generated here and never printed. An existing .env is left exactly as it is: a rerun
# that rotated the database password would leave the volume unreadable.
if [ ! -f .env ]; then
  log "writing .env (generated secrets, never echoed)"
  umask 077
  {
    echo "# Written by deploy/install.sh on $(date -u +%FT%TZ). Not in git."
    echo "APP_ENV=production"
    echo "LOG_FORMAT=json"
    echo "POSTGRES_USER=playlens"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    echo "POSTGRES_DB=playlens"
    echo "ADMIN_TOKEN=$(openssl rand -hex 32)"
    echo "WORKER_REPLICAS=${WORKER_REPLICAS:-1}"
    echo "PUBLIC_RATE_LIMIT_PER_MIN=120"
    echo "CORS_ALLOW_ORIGINS="
    echo "CRAWL_ENABLED=true"
    echo "# Filled in separately -- see deploy/README.md. The stack starts without them;"
    echo "# AI and YouTube stay off until they are present, which is the honest default."
    echo "LLM_ENABLED=false"
    echo "LLM_PROVIDER=openai"
    echo "LLM_MODEL=gpt-4o"
    echo "LLM_API_KEY="
    echo "AI_DAILY_COST_LIMIT_USD=${AI_DAILY_COST_LIMIT_USD:-5}"
    echo "YOUTUBE_ENABLED=false"
    echo "YOUTUBE_API_KEY="
  } > .env
else
  log ".env exists, leaving it alone"
fi
chmod 600 .env

# ---------------------------------------------------------------- build and start
log "building (first build takes a few minutes)"
"${COMPOSE[@]}" build

log "starting the database"
"${COMPOSE[@]}" up -d postgres

log "starting the api -- migrations run inside it, once, before it serves"
"${COMPOSE[@]}" up -d api

log "waiting for health"
for i in $(seq 1 60); do
  if curl -fsS -m 5 http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1; then
    log "api healthy after ${i}0s" ; break
  fi
  [ "$i" = 60 ] && { "${COMPOSE[@]}" logs --tail 60 api; die "api did not become healthy"; }
  sleep 10
done

log "starting the worker and the scheduler"
"${COMPOSE[@]}" up -d worker scheduler

"${COMPOSE[@]}" ps
log "done. The app answers on 127.0.0.1:8000 and on the compose network as api:8000."
log "next: deploy/caddy-site.sh to give it a public HTTPS name."
