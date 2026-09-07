# Where to host this, free

You were right about Render: its free tier covers a web service and a database for 30
days, but there is **no free worker plan**, and this service needs one. A full Render
deployment is about $22/month. That was my mistake to recommend without checking the
worker line.

Here is what is actually free, checked against what this service needs.

---

## What the service needs

| | Why |
|---|---|
| PostgreSQL | The job queue is a table with `FOR UPDATE SKIP LOCKED`; partial unique indexes are the correctness guarantees |
| A long-running process | The worker fetches games, reviews and summaries between requests |
| An hourly trigger | The crawl tick |
| Secrets | Two API keys |
| HTTPS on a public name | So you can open it |
| Persistent data | An empty catalogue after every restart is not a catalogue |

The trick that makes free tiers work: **one container running all three roles**.
`app/allinone.py` does that, and the root `Dockerfile` builds it. Verified on this
machine — it migrated, started both background threads, crawled Metacritic and synced 12
real games in 25 seconds.

---

## The options

| | Free forever | Card needed | Sleeps | Postgres | Verdict |
|---|---|---|---|---|---|
| **HF Spaces + Neon** | ✅ | ❌ no | after 48h idle, wakes on request | Neon free 0.5 GB | **recommended** |
| **Koyeb + Neon** | ✅ one service | ⚠️ usually | no | Neon | good if you have an account |
| **Fly.io** | trial credit | ✅ yes | no | their Postgres, paid | not free any more |
| **Railway** | $5 credit once | ✅ yes | no | included | runs out |
| **Replit** | ❌ | ✅ for Deployments | — | via Neon | Reserved VM is $7/mo minimum |
| **Render** | web + db only | ❌ | after 15 min | 30 days free | worker is paid — your point stands |
| **Oracle Cloud** | ✅ genuinely | ✅ verification | no | you install it | free forever, an hour of setup |

**Replit specifically:** free Repls can run while the tab is open, but a public URL that
stays up needs a Reserved VM or Autoscale deployment, both paid. It is a good editor and
a poor free host for something that must keep running.

---

## Recommended: Hugging Face Spaces + Neon

Both free, neither asks for a card, and together they give a real HTTPS URL.

Yes, Spaces is meant for ML demos. It is also a plain Docker host with a public URL and
no payment details, and nothing in its terms objects to a small web service.

### Step 1 — the database *(3 minutes)*

1. <https://neon.tech> → sign in with GitHub. No card.
2. **Create project** → name it `playlens`, region near you, **PostgreSQL 16**.
3. Copy the connection string. It looks like:
   ```
   postgresql://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require
   ```
4. **Change the scheme** so SQLAlchemy uses psycopg 3 — this one edit matters:
   ```
   postgresql+psycopg://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require
   ```

Free tier: 0.5 GB, which holds roughly 20–30 thousand games with their reviews. The
compute sleeps after five minutes idle and wakes in about a second.

### Step 2 — the app *(5 minutes)*

1. <https://huggingface.co> → sign up. No card.
2. **New Space**:
   - Owner: you · Name: `playlens`
   - License: whatever you like
   - **SDK: Docker** → **Blank**
   - **Public** (a private Space is not reachable without a token)
   - Hardware: **CPU basic — free**
3. In the Space: **Settings → Variables and secrets**. Add as **Secrets**:

   | Name | Value |
   |---|---|
   | `DATABASE_URL` | the Neon string from step 1, with `+psycopg` |
   | `ADMIN_TOKEN` | any long random string |
   | `LLM_API_KEY` | your OpenAI key |
   | `YOUTUBE_API_KEY` | your YouTube key |

   And as **Variables** (not secret):

   | Name | Value |
   |---|---|
   | `LLM_ENABLED` | `true` |
   | `LLM_PROVIDER` | `openai` |
   | `LLM_MODEL` | `gpt-4o` |
   | `YOUTUBE_ENABLED` | `true` |

4. Push the code to the Space:
   ```powershell
   cd "C:\Users\CRYCA\Claude VS Code Project\metacritic-service"
   git remote add space https://huggingface.co/spaces/<your-username>/playlens
   git push space main
   ```
   It asks for your Hugging Face username and an access token as the password — make one
   at <https://huggingface.co/settings/tokens> with **write** permission.

5. The Space builds for 5–10 minutes, then serves at:
   ```
   https://<your-username>-playlens.hf.space
   ```

### What to check when it is up

```
https://<you>-playlens.hf.space/api/v1/health     -> {"status":"ok"}
https://<you>-playlens.hf.space/                  -> the catalogue
https://<you>-playlens.hf.space/admin/monitoring  -> the pipeline
```

The catalogue fills itself: the scheduler inside the container queues a crawl at seven
minutes past each hour, and the worker in the same container processes it. Twenty games
an hour, two requests a second to the source.

### The limits, stated up front

| | |
|---|---|
| Sleeps after 48 hours with no visitor | Wakes on the next request, ~30 seconds |
| The container's disk is ephemeral | Fine: all state is in Neon. Only the image cache is lost on restart, and it refills |
| 16 GB RAM, 2 vCPU | More than enough |
| Public | Anyone with the link can read. `/admin/*` still needs the token |

---

## If you would rather have no sleeping at all

**Oracle Cloud Free Tier** is the only genuinely permanent free option: 4 ARM cores and
24 GB of RAM, free forever, no time limit. It asks for a card to verify identity and does
not charge it.

On that machine the normal three-container setup works:

```bash
git clone https://github.com/CRYCas0n/playlens.git && cd playlens
cp .env.example .env && nano .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

An hour of setup for a machine you keep. If you go this way, tell me the host and user
and I will do the deployment — see `HUMAN_DEPLOYMENT_HANDOFF.md` for how to hand over
access without putting a key in a chat message.

---

## What is verified about all this

| | |
|---|---|
| `app/allinone.py` runs web + worker + scheduler in one process | **VERIFIED** — against PostgreSQL 16.4, crawled 12 real games |
| The root `Dockerfile` is well-formed, non-root, healthchecked, `$PORT`-aware | **VERIFIED** as text — no Docker daemon here to build it |
| Neon's connection string works with `+psycopg` | **IMPLEMENTED** — the driver is tested against PostgreSQL 16.4 locally; Neon itself is untested |
| A Space actually builds and serves | **NOT VERIFIED** — no Hugging Face account exists |

The last row is the honest one: I can hand you a container that provably works and a
platform that provably takes containers, but I cannot create the account that joins them.
