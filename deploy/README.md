# deploy/

Scripts for putting Playlens on a Linux server that is already running other things.
The reasoning behind them is in [`../docs/FINAL_DEPLOYMENT_PLAN.md`](../docs/FINAL_DEPLOYMENT_PLAN.md);
this file is just what each one does and in what order.

| Script | Reads | Writes | Run it |
|---|---|---|---|
| `preflight.sh` | everything | **nothing** | first, always, before trusting any assumption |
| `install.sh` | `.env.example` | `/opt/playlens`, its `.env`, four containers | once, then again for any rebuild |
| `caddy-site.sh` | the existing Caddyfile | one appended site block, after a backup | once per public hostname |
| `backup.sh` | the database | a gzipped dump under `backups/` | nightly, from cron |
| `restore.sh` | a dump | the database | only deliberately; it asks |
| `update.sh` | git | the containers | to deploy a new commit |

## Order

```bash
bash deploy/preflight.sh                                    # 1. look
bash deploy/install.sh                                      # 2. build and start
#    3. put LLM_API_KEY and YOUTUBE_API_KEY into .env, flip the two ENABLED flags
SITE=playlens.<ip>.sslip.io bash deploy/caddy-site.sh        # 4. public HTTPS name
python -m scripts.prod_smoke https://playlens.<ip>.sslip.io  # 5. prove it
```

## The mistake to not repeat

`git pull && docker compose up -d` does **not** deploy the new code. The application is
baked into the image by `COPY app ./app`, so a pull changes the checkout and the
containers keep running what they were built from. It recreates containers, reports
success, and deploys nothing.

It bites hardest right after a fix: the log still shows the exact traceback you just
fixed, and the obvious conclusion — that the fix was wrong — is the wrong one.

Use `update.sh`. It builds. If you are running compose by hand, `--build` is not
optional.

## Rules these scripts follow

- **Nothing destructive is written down.** No `rm -rf /`, no `docker system prune`, no
  `docker volume rm`, no mass container stop, no firewall flush, no edit to `sshd_config`.
  A test asserts each of those absences, because on someone's live server the absence is
  the whole defence.
- **`preflight.sh` only reads.** It is the first thing to run and it runs before any
  assumption about the machine has been checked.
- **Edits to other people's files are backed up and validated first.** `caddy-site.sh`
  copies the Caddyfile to a timestamped backup, appends, runs `caddy validate`, and
  restores the backup if validation fails — so a site that was up stays up.
- **No new ports.** The reverse proxy already has 80 and 443; the app binds loopback and
  reaches Caddy over a Docker network.
- **Secrets are generated on the server and never printed.** Not to stdout, not into an
  argument (arguments show up in `ps` and in shell history), not into git.
- **Everything is undone by a written-down command.** They are in the rollback table of
  the plan.

## Uninstalling, completely

```bash
cd /opt/playlens
docker compose -f docker-compose.yml -f docker-compose.prod.yml down     # add -v to drop the data too
docker network disconnect playlens_default caddy
sudo cp -p /opt/caddy/Caddyfile.bak.<timestamp> /opt/caddy/Caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

The server is then exactly as it was.
