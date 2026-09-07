#!/usr/bin/env bash
# Read-only. Looks at the server and changes nothing.
#
# The server this is written for is already in use — a reverse proxy, other containers,
# a live site — so the first question is not "how do I install this" but "what is here
# already, and what would I break". Every command below reads. There is no rm, no
# systemctl, no docker rm, no write to any path outside /tmp.
#
#   bash preflight.sh            # human-readable
#
set -u

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "identity"
uname -a
[ -r /etc/os-release ] && . /etc/os-release && echo "distro: ${PRETTY_NAME:-unknown}"
echo "hostname: $(hostname -f 2>/dev/null || hostname)"
echo "user: $(id -un) (uid $(id -u)), groups: $(id -Gn)"

say "capacity"
nproc --all 2>/dev/null | sed 's/^/cpus: /'
free -h 2>/dev/null || true
df -h / /var /opt 2>/dev/null | sort -u

say "docker"
if have docker; then
  docker --version
  docker compose version 2>/dev/null || echo "compose plugin: MISSING"
  echo "-- containers --"
  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null
  echo "-- networks --"
  docker network ls 2>/dev/null
  echo "-- disk used by docker --"
  docker system df 2>/dev/null
else
  echo "docker: NOT INSTALLED"
fi

say "what is holding the ports"
if have ss; then
  ss -tlnp 2>/dev/null | awk 'NR==1 || /LISTEN/'
else
  netstat -tlnp 2>/dev/null || echo "no ss or netstat"
fi

say "reverse proxy"
for c in caddy nginx traefik; do
  if have docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
    echo "$c: running as a container"
  elif have systemctl && systemctl is-active --quiet "$c" 2>/dev/null; then
    echo "$c: running as a service"
  fi
done
for f in /opt/caddy/Caddyfile /etc/caddy/Caddyfile; do
  [ -r "$f" ] && echo "-- $f (site names only) --" && grep -oE '^[a-z0-9.*-]+\.[a-z]{2,}[^{]*\{' "$f" | tr -d '{'
done

say "firewall"
if have ufw; then ufw status 2>/dev/null | head -20
elif have firewall-cmd; then firewall-cmd --list-all 2>/dev/null | head -20
else echo "no ufw/firewalld; iptables rule count: $(iptables -S 2>/dev/null | wc -l)"; fi

say "outbound reachability the service needs"
for url in https://backend.metacritic.com https://api.openai.com https://www.googleapis.com; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$url" 2>/dev/null)
  echo "$url -> ${code:-unreachable}"
done

say "verdict"
mem=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
disk=$(df -Pm /opt 2>/dev/null | awk 'NR==2{print $4}' || echo 0)
echo "available RAM: ${mem} MB (need >= 1500 with one worker)"
echo "free disk on /opt: ${disk} MB (need >= 8000)"
[ "${mem:-0}" -lt 1500 ] && echo "WARNING: tight on RAM — deploy with WORKER_REPLICAS=1"
[ "${disk:-0}" -lt 8000 ] && echo "WARNING: tight on disk"
exit 0
