# Deployment handoff

Everything that could be done without you is done. The code is on GitHub, it runs on
PostgreSQL, the AI works on your key, and the deployment is described by one file in the
repository.

What is left is short, and every item is genuinely something only you can do.

---

## What I need from you

### 1. One shell command, to turn on CI *(30 seconds)*

The GitHub token on this machine can push code but not workflow files — GitHub restricts
that scope specifically. The CI configuration is written and waiting locally.

```powershell
cd "C:\Users\CRYCA\Claude VS Code Project\metacritic-service"
gh auth refresh -s workflow
```

It prints a code and opens a browser. Paste the code, approve, done. Then tell me, and I
will push the workflow and confirm it runs green.

Five jobs: lint, unit tests, integration tests against a real PostgreSQL 16, a migration
round trip, and a security job.

### 2. Two free accounts, for the public URL *(8 minutes, no card)*

This is the only thing standing between the project and a link you can open.

Render was the wrong recommendation and you were right to push back: its free tier has no
worker, so a real deployment there is about $22/month. The free path is two accounts,
neither of which asks for payment details:

- **<https://neon.tech>** — PostgreSQL. Sign in with GitHub, create a project, copy the
  connection string.
- **<https://huggingface.co>** — the app. New Space → **SDK: Docker** → Public → CPU basic.

The exact steps, including the one edit the Neon string needs, are in
[`DEPLOYMENT_OPTIONS.md`](DEPLOYMENT_OPTIONS.md). It takes two commands at the end:

```powershell
git remote add space https://huggingface.co/spaces/<your-username>/playlens
git push space main
```

**Do not send me the keys.** They go into the Space's own secrets form.

The whole stack runs in one container there — web, worker and scheduler together. That
mode is verified: on this machine it migrated, started both threads, crawled Metacritic
and synced 12 real games in 25 seconds.

### 3. One decision, before anyone else sees it

The service reads Metacritic through an interface Metacritic publishes for its own site.
Their terms discourage automated collection. On your own machine that is unremarkable; on
a public address it is a question about your risk, and it is not a technical one.

What the code already does: two requests a second, an identifying User-Agent, review text
never republished in full, a not-affiliated notice on every page.

What it cannot do is decide. I am not able to give you a legal opinion and have not tried
to. If this goes somewhere public, that is worth ten minutes with someone who can.

---

## What I will do once you have done those

**After (1):**
```
push .github/workflows/ci.yml
watch the run
report: green, or the failure and the fix
```

**After (2), against your real URL:**
```
GET /api/v1/health          -> {"status":"ok"}
GET /                       -> the catalogue
GET /games/<a real slug>    -> a game page with scores and a verdict
GET /admin/monitoring       -> the pipeline
trigger the first crawl and watch real games arrive
run the browser pass at 1440 / 1280 / 768 / 390 against the live site
```

Then I update `PROJECT_STATUS.md`, turning the deployment rows from NEEDS HUMAN ACTION to
VERIFIED — or reporting exactly what broke.

---

## If you would rather use a server you already have

Skip Render entirely. On any machine with Docker and ~2 GB of RAM:

```bash
git clone https://github.com/CRYCas0n/playlens.git && cd playlens
cp .env.example .env      # then fill ADMIN_TOKEN, LLM_API_KEY, YOUTUBE_API_KEY
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

If you want me to do it, I need SSH. **Do not paste a private key into this chat.**
Instead:

- put my public key in `~/.ssh/authorized_keys` on the server, or
- create a throwaway user with a password you rotate afterwards, and tell me the host and
  username only.

Either way, tell me the host, the user, and whether a domain points at it. Nothing else.

---

## What is already true, so you do not have to check

| | |
|---|---|
| Repository | <https://github.com/CRYCas0n/playlens> — private, `main`, 270 files, no secrets |
| Tests | 768 green on SQLite, 362 of them green again on PostgreSQL 16.4 |
| PostgreSQL | Real server: all five partial unique indexes, `pg_trgm`, `FOR UPDATE SKIP LOCKED` |
| AI | Full Release 0 on all 20 games with your key: PV1 85.7% on gpt-4o, cost $0.12 |
| YouTube | Live API: discovery and ranking work; transcripts do not, and the fallback is honest |
| Browser | Real Chromium at four widths against the PostgreSQL-backed app |
| Deployment files | `render.yaml`, Dockerfile, three compose files — all parse, all tested statically |

Two real deployment bugs were found and fixed by those static tests before any deploy: a
hardcoded port that would have failed Render's health check, and a cron role the entrypoint
did not understand, which would have failed silently every hour.
