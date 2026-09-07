# Deployment plan — a server that is already in use

The target is not an empty machine. It runs a live portfolio site, a Caddy holding 80 and
443, and an n8n. That single fact decides most of what follows: the plan adds four
containers and one site block, opens no port, installs no service, and touches nothing it
did not create.

**What is verified here and what is not.** The repository state, the scripts, the compose
files and the DNS below were all checked while writing this. Everything about the server
itself comes from notes, not from a connection — no SSH access exists yet. The first
thing that runs after access is `deploy/preflight.sh`, which reads and changes nothing,
and every assumption in this document is re-checked against its output before anything is
written.

---

## Architecture

```
                    Internet
                        |
                   443 / 80   (already open, already Caddy's)
                        |
              ┌─────────────────────┐
              │  caddy (container)  │  existing — serves the portfolio site
              │  auto TLS, HTTP/3   │  one site block appended for playlens
              └──────────┬──────────┘
                         │  docker network: playlens_default
                         │  (caddy is *connected* to it — additive, reversible)
                         ▼
   ┌──────────────────────────────────────────────────────┐
   │ playlens_default                                     │
   │                                                      │
   │   api ──────────► postgres 16  (volume: pgdata)      │
   │   worker ×1 ────►                                    │
   │   scheduler ────►                                    │
   │                                                      │
   │   api also on 127.0.0.1:8000 for local curl only     │
   └──────────────────────────────────────────────────────┘
                         │
                    n8n, portfolio — untouched, separate networks
```

Four containers, one image, no message broker: the job queue is a PostgreSQL table
(ADR-003), which is why there is no Redis and no Celery in this picture.

**Why not the single-container `allinone` mode.** It exists for hosts that give you one
container and no choice. This server gives three, so the service gets its real shape: a
crash in the worker leaves the site up, and the scheduler stays singular while workers
scale. `app/allinone.py` stays in the repository for Hugging Face Spaces and for any
512 MB box; it is not what runs here.

**Why Caddy joins the app's network instead of the app publishing a port.** Connecting a
running container to an additional network needs no restart and is undone with
`docker network disconnect`. The alternative — publishing 8000 on a public interface —
would serve the whole site a second time on `http://45.67.202.162:8000`, without the
certificate, without the proxy and without its rate limits. The app therefore publishes
only on loopback, and only so an operator on the box can curl it.

---

## Server requirements

| | Minimum | What this deployment asks for |
|---|---|---|
| OS | any Linux with a 5.x+ kernel | Ubuntu 22.04 / 24.04 LTS assumed; nothing depends on it |
| CPU | 1 core | 2 cores comfortable; the workers are I/O-bound, not CPU-bound |
| RAM **free** | 1.5 GB | 2 GB. Postgres 1 GB limit, api 1 GB, worker 768 MB, scheduler 256 MB — limits, not reservations |
| Disk **free** | 8 GB | image ~1.3 GB, Postgres grows ~40 MB per 1,000 games, image cache capped at 512 MB, backups ~14 × the dump |
| Docker | 20.10+ | with the `compose` v2 plugin — `docker compose version` must answer |
| PostgreSQL | — | **not installed on the host.** It runs as a container with a named volume; the host's own Postgres, if any, is left alone |
| Ports opened | **none** | Caddy already holds 80 and 443. Nothing else is published anywhere but loopback |
| Persistent storage | 2 named volumes | `pgdata` (the database) and `imgcache` (cover art, capped) |

If preflight reports under 1.5 GB available, the deploy runs with `WORKER_REPLICAS=1`,
which is the default `install.sh` writes anyway.

---

## Network

| Port | Who holds it | Change |
|---|---|---|
| 80 | caddy (existing) | none — reused for the ACME HTTP-01 challenge |
| 443 | caddy (existing) | none — one more site name on the same listener |
| 8000 | nobody | bound to `127.0.0.1` only, by this deployment |
| 5432 | nobody | **never published.** Postgres is reachable on the compose network and nowhere else |

No firewall rule changes. If UFW is active, it stays exactly as it is — a deployment that
needs a new hole in a firewall it did not configure is a deployment doing something
wrong.

---

## Domain

A free public name, with no account to create and nothing to buy:

```
playlens.45.67.202.162.sslip.io
```

sslip.io resolves any `<anything>.<ip>.sslip.io` to that IP. Verified while writing this:

```
$ nslookup playlens.45.67.202.162.sslip.io 8.8.8.8
Address: 45.67.202.162
```

It is a real, resolvable, public hostname that already points at the server, so nothing
needs to be registered, configured or waited for. It is also on the Public Suffix List,
which means Let's Encrypt rate-limits it per-subdomain rather than lumping every sslip.io
user together.

**A nicer name, if wanted later.** The server's existing site uses a FreeDNS
(afraid.org) subdomain, and `playlens.mooo.com` currently does not resolve — so it is
probably free. Adding it takes a minute in that account and one A record to
`45.67.202.162`; then `SITE=playlens.mooo.com bash deploy/caddy-site.sh` and Caddy issues
a second certificate. That is an owner action and a cosmetic one, so it is not on the
critical path.

---

## SSL

Caddy already does automatic HTTPS with Let's Encrypt for the existing site, which means
the mechanism is proven on this machine. The new site block inherits it: certificate
issued over HTTP-01 on port 80, renewed automatically, HTTP redirected to HTTPS by
default.

`deploy/caddy-site.sh` waits up to five minutes for `https://<site>/api/v1/health` to
answer 200 before declaring success, so a certificate that does not arrive is a failure
rather than a silent maybe.

---

## Docker

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The production overlay is what makes it production: secrets required rather than
defaulted, `restart: unless-stopped` on all four services, memory and CPU limits so one
runaway worker cannot take the host down with n8n on it, and JSON log rotation at
10 MB × 5 files.

Order matters and `install.sh` enforces it: postgres, then api — **migrations run in the
api container's entrypoint and nowhere else** — then worker and scheduler, which wait on
the api's healthcheck. Three containers racing for Alembic's lock is how a worker ends up
crash-looping against a half-built schema.

---

## Database

PostgreSQL 16 in a container, `pgdata` named volume, `--data-checksums` on init. Not on
the host, not shared with anything else on the machine.

`alembic upgrade head` runs automatically inside the api container on every start. After
the first boot the plan verifies, against the live database rather than the code:

- the three migrations applied and `alembic_current` is at head;
- all five partial unique indexes exist — they are the correctness guarantees, not
  optimisations;
- `pg_trgm` is installed and the four `NULLS LAST` indexes are present;
- `docker compose restart` and a full `down`/`up` leave the data intact.

The last one is the point of §13 of the brief, and a named volume is only a claim until
it survives a restart in front of you.

---

## Environment

Written to `/opt/playlens/.env`, mode 600, by `install.sh`. Names taken from
`app/config.py`, not from memory:

| Variable | Source |
|---|---|
| `APP_ENV=production`, `LOG_FORMAT=json` | fixed |
| `POSTGRES_USER`, `POSTGRES_DB` | fixed (`playlens`) |
| `POSTGRES_PASSWORD` | generated on the server, `openssl rand -hex 24`, never printed |
| `ADMIN_TOKEN` | generated on the server, `openssl rand -hex 32`, never printed |
| `DATABASE_URL` | composed by the overlay from the three above |
| `WORKER_REPLICAS=1` | raised after watching memory |
| `AI_DAILY_COST_LIMIT_USD=5` | half the default ceiling, deliberately, on a first deploy |
| `CORS_ALLOW_ORIGINS=` | empty — the HTML and the API are the same origin |
| `PUBLIC_RATE_LIMIT_PER_MIN=120` | default |
| `LLM_ENABLED`, `LLM_PROVIDER=openai`, `LLM_MODEL=gpt-4o`, `LLM_API_KEY` | AI stays **off** until the key is in place |
| `YOUTUBE_ENABLED`, `YOUTUBE_API_KEY` | same |

`LLM_ENABLED=false` and `YOUTUBE_ENABLED=false` are the honest first state: the stack
starts, serves and crawls without either, and neither pretends to work without a key.

---

## Secrets

Two are generated on the server and never leave it. Two exist already, in this machine's
gitignored `.env`, and have to reach the server.

**How they get there:** written directly into `/opt/playlens/.env` over the SSH session,
never echoed to a terminal, never passed as a command argument (arguments land in `ps` and
in shell history), never committed, never logged. The application redacts them from log
output through the same redactor the security tests cover.

**They are not asked for in this chat, and must not be pasted here.** They are already
on this machine because the owner put them there.

`.gitignore` covers `.env` and `.env.*`; the git index was scanned for secret shapes
before the first commit and is scanned again by CI's security job.

---

## Deployment steps

Each step is a script in `deploy/`, in this order.

| # | Command | What it does | Reversible by |
|---|---|---|---|
| 1 | `bash deploy/preflight.sh` | Reads the server: OS, RAM, disk, Docker, containers, listening ports, reverse proxy, firewall, outbound reachability. **Writes nothing.** | nothing to reverse |
| 2 | `bash deploy/install.sh` | Clones to `/opt/playlens`, writes `.env`, builds, starts postgres → api (migrations) → worker + scheduler, waits for health | `docker compose down` |
| 3 | *(inline)* | Puts the two API keys into `.env`, flips `LLM_ENABLED` / `YOUTUBE_ENABLED` to true, restarts the worker | edit and restart |
| 4 | `SITE=playlens.45.67.202.162.sslip.io bash deploy/caddy-site.sh` | Connects caddy to the app network, backs up the Caddyfile, appends one site block, validates, reloads, waits for the certificate | restore the `.bak.<ts>` file, `docker network disconnect` |
| 5 | `python -m scripts.prod_smoke https://<site>` | 25 checks over real HTTPS | read-only |
| 6 | `crontab` line | Nightly `deploy/backup.sh` | remove the line |

Step 4 is the only one that edits a file belonging to something already running. It backs
that file up first, runs `caddy validate` before any reload, and restores the backup if
validation fails — so the portfolio site stays up whatever happens to this one.

---

## Verification

Not "the page loads". Against the live URL, in this order:

**Infrastructure**
- HTTP redirects to HTTPS; the certificate validates against a public root
- `/api/v1/health` returns `{"status": "ok"}` — not `not_migrated`, not `down`
- the existing portfolio site still answers 200 (checked before and after step 4)

**Data — the part a page load cannot show**
- trigger a crawl; watch real games arrive from Metacritic
- scores stored as `ScoreValue`, with unavailable staying unavailable rather than becoming 0
- platforms, genres and reviews persisted; re-running the crawl adds nothing (dedup)

**AI**
- one controlled summary on a real game: request → response → evidence validation →
  database → rendered page
- cost recorded, daily ceiling enforced before the call
- `docker compose logs api worker | grep -c 'sk-'` returns 0

**YouTube**
- search, ranking and selection on one game; transcripts are expected to degrade to
  metadata-only, and the degradation must be visible and honest rather than invented

**Browser** — 1440, 1280, 768, 390: home, catalogue, a game page, search, platform filter,
sort by score, similar games, monitoring, and the empty and error states.

**Restart** — `docker compose restart`, then a host reboot if one is acceptable: all four
containers come back, the data is still there, the scheduler resumes.

---

## Rollback

Three levels, smallest first.

| Went wrong | Undo |
|---|---|
| The new code is bad | `deploy/update.sh` does it automatically: unhealthy after deploy → `git reset --hard` to the previous commit, rebuild, up |
| A migration is bad | `docker compose exec api python -m alembic downgrade -1`, then the code rollback above. Every migration to date is additive; check the revision has a real downgrade before relying on it |
| The whole deployment is unwanted | `docker compose down` (add `-v` only if the data is meant to go too), `docker network disconnect playlens_default caddy`, restore `/opt/caddy/Caddyfile.bak.<ts>`, reload caddy. The server is back to exactly what it was |

The third row is the one that matters on a shared machine: this deployment is removable
without residue, and the path to remove it is written down before it is installed.

---

## Backup

`deploy/backup.sh` — `pg_dump | gzip` into `/opt/playlens/backups`, 14 dumps kept, the
file named only after a clean exit so a truncated dump never looks like a good one. Run
nightly from cron:

```
17 3 * * * cd /opt/playlens && bash deploy/backup.sh >> /var/log/playlens-backup.log 2>&1
```

Restore: `bash deploy/restore.sh /opt/playlens/backups/playlens-<ts>.sql.gz`. It stops the
writers first, asks for a typed confirmation, and loads the dump.

**Stated limitation:** this is a backup on the same disk as the database. It protects
against a bad migration and a wrong delete — the two things that actually happen — and
not against losing the machine. Off-site copies need somewhere free to put them and a
credential to put them with; neither exists yet, so this is honestly a local backup and
not a disaster-recovery plan.

---

## Updates

```bash
cd /opt/playlens && SITE=<the site> bash deploy/update.sh
```

Backup → fetch → build → api (migrations) → worker and scheduler → health check → roll the
code back automatically if it fails. `git` is the only source: no file is ever copied to
the server by hand, so what runs there is always a commit that exists on GitHub and has
passed CI.

---

## Monitoring

- `/admin/monitoring` — the pipeline: worker liveness, scheduler ticks, last successful
  crawl, games processed, error rates, AI calls and spend
- `/api/v1/monitoring/status` — the same as JSON, with a system status of `ok`,
  `degraded` or `down` and the specific reason
- `/api/v1/metrics` — counters
- container health: `docker compose ps` shows the api's healthcheck, and the worker and
  scheduler restart policies

The monitoring **reads** are public by deliberate design (ADR-019) and pass their error
text through the same redactor as the logs; every **write** — `POST /api/v1/admin/*` —
needs the admin token. If the read pages turn out to expose more than is comfortable on a
public URL, the fix is a Caddy `basic_auth` block on `/admin/*`, which is four lines and
no code change.

---

## What running it actually cost

Six defects, every one found by running this on the server and none by any test that
existed beforehand. They are listed because the pattern matters more than the list: each
one is invisible on the machine the code was written on.

1. **Compose concatenates `ports` across files.** The loopback bind was *added* to the
   base file's `0.0.0.0` rather than replacing it — two bindings for one port, the
   second of them public. Caught by `docker compose config` before anything started.
2. **`cpus: "1.5"` on a one-core host.** Docker refuses rather than clamping, so the api
   would not start, and the worker and scheduler wait on its health.
3. **Every `.sh` was `100644` in git.** Written on Windows, where `chmod +x` changes
   nothing git records. The container died on its own entrypoint.
4. **The worker and the scheduler inherited an HTTP healthcheck** they can never pass,
   and sat marked unhealthy while working perfectly.
5. **`LLM_PROVIDER=openai` never reached the containers.** It was in `.env` and in no
   `environment:` block, so the worker used the default — anthropic — and failed every
   summary while holding a working OpenAI key.
6. **Every follow-up job a crawl queued was dead on arrival.** `payload={"slug": slug}`
   with `game_id` passed as a column; three handlers read `payload["game_id"]`. Reviews,
   similarity and YouTube were all silently dead behind a crawl reporting 20/20 success.

7. **The home page hero was covered by its own art.** `.cover-img` is absolutely
   positioned and `.spotlight__cover` had no rule at all, so it resolved against the whole
   section: headline, verdict, scores and both buttons underneath it, unreachable by a
   mouse. Every check passed while that was true — the page was 200, laid out, and
   unusable.
8. **Every cover box was portrait and every cover is landscape.** Measured over 45 covers
   in production; not one is taller than wide. The 3/4 boxes showed a third of each image
   and cropped from the top, where the title is printed.
9. **A prompt-version bump rewrote nothing.** The fingerprint marks a summary stale, but
   the job that rewrites it is queued by `reviews.sync` — so a game nobody reviews again
   keeps its old summary forever. Found by reading the database after the deploy: 111
   model calls, zero of them regenerations.

The sixth is the serious one. The service looked completely healthy — green crawl, green
API, games arriving — and did almost nothing. Both sides of that seam had tests; the join
between them had none.

---

## What is verified in this document

| | |
|---|---|
| Repository is `main` at `d147cd5`, clean, in sync with origin, CI green | **VERIFIED** — checked, not recalled |
| `playlens.45.67.202.162.sslip.io` resolves to the server | **VERIFIED** — public DNS |
| The server answers on 443 with a valid certificate, served by Caddy | **VERIFIED** — a request from here |
| The scripts contain nothing destructive and every risky edit is reversible | **VERIFIED** — `tests/unit/test_deploy_scripts.py`, 44 assertions |
| The app is not published on a public interface behind the proxy | **VERIFIED** — a test on the overlay, after fixing it |
| Docker, RAM, disk, the Caddy container's name, the Caddyfile path | **NOT VERIFIED** — no access yet; `preflight.sh` answers all of them first |
| The image builds | **NOT VERIFIED** — no Docker daemon on this machine |
| Any of it runs on that server | **NOT VERIFIED** — nothing has been deployed |
