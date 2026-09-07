#!/usr/bin/env bash
# Give Playlens a public HTTPS name on a Caddy that is already serving other sites.
#
# This is the only script here that edits a file belonging to something else, so it is
# the careful one:
#
#   * the Caddyfile is copied to a timestamped backup before anything is written;
#   * the new block is appended, never inserted, and only if the name is not there yet;
#   * `caddy validate` runs against the edited file BEFORE any reload;
#   * a failed validation or a failed reload restores the backup and reloads again,
#     so the sites that were up stay up.
#
# It publishes no new port. Caddy already holds 80 and 443; the certificate comes from
# Let's Encrypt over HTTP-01 on the port it is already listening on.
#
#   SITE=playlens.45.67.202.162.sslip.io bash caddy-site.sh
#
set -euo pipefail

SITE="${SITE:?set SITE to the public hostname}"
CADDY_CONTAINER="${CADDY_CONTAINER:-caddy}"
CADDYFILE="${CADDYFILE:-/opt/caddy/Caddyfile}"
CADDYFILE_IN_CONTAINER="${CADDYFILE_IN_CONTAINER:-/etc/caddy/Caddyfile}"
UPSTREAM="${UPSTREAM:-api:8000}"
NETWORK="${NETWORK:-playlens_default}"

log() { printf '\033[1m[caddy]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[caddy] %s\033[0m\n' "$*" >&2; exit 1; }

docker ps --format '{{.Names}}' | grep -qx "$CADDY_CONTAINER" \
  || die "no running container named $CADDY_CONTAINER (set CADDY_CONTAINER)"
[ -w "$CADDYFILE" ] || die "$CADDYFILE is not writable by $(id -un) (set CADDYFILE, or use sudo)"
docker network inspect "$NETWORK" >/dev/null 2>&1 \
  || die "network $NETWORK does not exist -- run deploy/install.sh first"

# Caddy runs in its own container and must be able to reach the app. Connecting it to
# the app's network is additive and reversible (`docker network disconnect`), needs no
# restart, and means the app never has to publish a port to the outside at all.
if docker network inspect "$NETWORK" -f '{{range .Containers}}{{.Name}} {{end}}' | grep -qw "$CADDY_CONTAINER"; then
  log "$CADDY_CONTAINER is already on $NETWORK"
else
  log "connecting $CADDY_CONTAINER to $NETWORK"
  docker network connect "$NETWORK" "$CADDY_CONTAINER"
fi

if grep -qF "$SITE" "$CADDYFILE"; then
  log "$SITE is already in $CADDYFILE; not touching it"
else
  BACKUP="${CADDYFILE}.bak.$(date -u +%Y%m%d-%H%M%S)"
  cp -p "$CADDYFILE" "$BACKUP"
  log "backed up to $BACKUP"

  cat >> "$CADDYFILE" <<BLOCK

# Playlens -- added by deploy/caddy-site.sh on $(date -u +%FT%TZ)
$SITE {
	encode zstd gzip
	# The app is reachable by service name on the compose network; nothing is published
	# to the host for this to work.
	reverse_proxy $UPSTREAM

	# The app sets its own CSP, headers and cache rules -- see app/main.py. Adding a
	# second, different set here is how they end up contradicting each other.
	log {
		output file /var/log/caddy/playlens.log {
			roll_size 10mb
			roll_keep 5
		}
	}
}
BLOCK
  log "appended a block for $SITE"

  if ! docker exec "$CADDY_CONTAINER" caddy validate --config "$CADDYFILE_IN_CONTAINER" >/dev/null 2>&1; then
    log "validation FAILED -- restoring $BACKUP"
    cp -p "$BACKUP" "$CADDYFILE"
    docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDYFILE_IN_CONTAINER" >/dev/null 2>&1 || true
    die "the Caddyfile did not validate; nothing was changed"
  fi
  log "config validates"
fi

log "reloading caddy"
if ! docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDYFILE_IN_CONTAINER"; then
  die "reload failed -- the previous config is still live; check 'docker logs $CADDY_CONTAINER'"
fi

log "waiting for the certificate (Let's Encrypt over HTTP-01, usually under a minute)"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$SITE/api/v1/health" 2>/dev/null || true)
  if [ "$code" = "200" ]; then log "https://$SITE is live"; exit 0; fi
  sleep 10
done
log "no certificate yet after five minutes. Check: docker logs $CADDY_CONTAINER | tail -40"
exit 1
