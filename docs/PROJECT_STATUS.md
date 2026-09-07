# Project Status

**Date:** 2026-09-07 · **Repository:** <https://github.com/CRYCas0n/playlens>
**Production:** <https://playlens.45.67.202.162.sslip.io> — deployed, serving, ingesting

**CI:** green on GitHub Actions · **Run:** `965 tests` · `ruff: clean` · `362 integration tests green on PostgreSQL 16.4` ·
`smoke: 15 routes` · Release 0 on all 20 games against a live model

Statuses mean one thing each:

| | |
|---|---|
| **VERIFIED** | A check I ran proves the behaviour |
| **IMPLEMENTED — NOT VERIFIED** | Code exists; nothing available here could prove it |
| **BLOCKED** | Impossible in this environment, with the reason |
| **NEEDS HUMAN ACTION** | Only the owner can do it |
| **DEFERRED** | Deliberately not done |

---

## Status

| Area | Status | Evidence |
|---|---|---|
| **Repository** | VERIFIED | `github.com/CRYCas0n/playlens`, private, 271 files, no secrets in the index |
| **Backend** | VERIFIED | 781 tests. `scripts/smoke.py` boots the assembled app across 15 routes |
| **Database — PostgreSQL 16** | VERIFIED | 362 integration tests against a real server. `pg_trgm`, 4 NULLS LAST indexes, all 5 partial unique indexes present |
| **Database — SQLite** | VERIFIED | Same suite, same assertions, 768 passing |
| **Migrations** | VERIFIED | 3 revisions; upgrade → downgrade base → upgrade, constraints re-checked after the rebuild |
| **Concurrency on PostgreSQL** | VERIFIED | `FOR UPDATE SKIP LOCKED` compiles and runs; concurrent claim yields exactly one row, with real threads |
| **Metacritic ingestion** | VERIFIED | 17 contract tests against the **live** API, and in production: 179 games, 7,684 reviews, every `game.sync` job succeeded |
| **Deduplication** | VERIFIED | Re-running a crawl adds nothing; two workers cannot claim the same game |
| **Review snapshots** | VERIFIED | Immutable, stable evidence refs, 9 tests |
| **AI — OpenAI** | VERIFIED | 26 offline tests plus live calls. Full lifecycle: 401, 404, 429, 5xx, timeout, malformed JSON, prose instead of a call, unknown enum |
| **AI — Anthropic** | IMPLEMENTED — NOT VERIFIED | No Anthropic key. Same protocol the OpenAI adapter now exercises for real |
| **AI — Release 0, 20 games** | VERIFIED | gpt-4o: **PV1 85.7%**, PV2 100%, $0.015/game. gpt-4o-mini: PV1 78.2%, below target. `docs/RELEASE0_FINAL.md` |
| **AI — Russian claims** | VERIFIED | Live: claims, headings and overall paragraphs in Russian, evidence counts declined correctly (`3 рецензии`). The catalogue rewrites itself a day at a time against the $5 ceiling; a summary not yet reached shows the verified English |
| **AI — in production** | VERIFIED | 213 live calls, $2.72 of a $5/day ceiling, **91.6% claim acceptance** on real reviews — the rest rejected by evidence validation |
| **AI — evidence validation** | VERIFIED | 42 rejections on real data across 6 classes; 7 invented references caught, 0 published |
| **AI — cost ceiling** | VERIFIED | Enforced before the call, and it **defers** rather than dropping the work: 237 jobs observed in `retrying` with `next_retry_at` at the top of the hour, waiting for the window to reset. It used to return success having done nothing, which spent the job's key and lost the work silently |
| **AI — is it useful** | NEEDS HUMAN ACTION | Needs a person who did not write the summaries. `docs/HUMAN_EVALUATION.md`, 20 minutes |
| **YouTube — discovery and ranking** | VERIFIED | Live Data API, and in production: 42 `youtube.discover` jobs succeeded, 0 failed, real videos linked on game pages |
| **YouTube — transcripts** | BLOCKED | yt-dlp offered only an m3u8 caption track. No PO-token path, no paid provider. Degradation verified: link and metadata shown, **no AI text** |
| **Similarity** | VERIFIED | 12 tests. No reason invented below the contribution threshold |
| **Search, filters, sorting** | VERIFIED | Both dialects. Unrated never floats to the top in either direction |
| **UI — data rules** | VERIFIED | All 28 cases of `design/EDGE_CASES.md`, 44 tests |
| **UI — rendering** | VERIFIED | Real Chromium × 4 widths, against **the live public site**, no horizontal overflow anywhere |
| **UI — language** | VERIFIED | Russian throughout: templates, verdict copy, tier labels, monitoring stages, dates and relative times, empty states. A test rejects English prose in any template |
| **Source text — language** | VERIFIED | Genres from a 57-entry table (FPS, RPG, roguelike deliberately kept); **all 178 descriptions translated**, and the page says whether it shows a translation or the source's own words |
| **Stylesheet covers the markup** | VERIFIED | Sixteen classes had no rule, three of them visibly: raw checkboxes in every filter, the catalogue grid on the wrong element, `hide-tablet` hiding nothing. A test enumerates them |
| **UI — cover art** | VERIFIED | Boxes are 16/9 because all 45 covers measured in production are landscape, median 1.78. Card crop went from 65% of the image to 6% |
| **Accessibility** | IMPLEMENTED — NOT VERIFIED | Landmarks, labels, skip link, `aria-current` present. No axe audit (OQ-N3) |
| **Security** | VERIFIED | 30 assertions, one per threat in ADR-019: SSRF, XSS, prompt injection, secret redaction, admin auth, rate limiting |
| **Secrets** | VERIFIED | Index scanned before the first commit, 0 hits. `.env` ignored, `.env.example` ships every secret empty |
| **Monitoring** | VERIFIED | Every indicator the assignment lists, asserted as a shape rather than a 200 |
| **Schema-drift circuit** | VERIFIED | Stops the crawl, alerts once per incident, shows `down` with the specific reason |
| **SSE** | VERIFIED | Real uvicorn on a real socket: framing, ordering, `Last-Event-ID` replay, disconnect |
| **Worker and scheduler** | VERIFIED | Leases, retries, heartbeats, slot idempotency |
| **Image proxy and cache** | VERIFIED | Allow-list offline; a real 2.3 MB cover fetched, resized under 400 KB, cached; eviction tested |
| **Operator CLI** | VERIFIED | `python -m app.cli` — status, crawl, seed, summarise, translate, recompute, purge. The last three exist because work is queued by whatever changes the data, and a change to the *code* changes none |
| **Health and readiness** | VERIFIED | Reachable ≠ ready: `ok` / `not_migrated` / `down`, each saying what to do |
| **Docker — files** | VERIFIED as text | 30 static assertions. Two real deployment bugs found this way |
| **CI** | VERIFIED | 5 jobs green on GitHub Actions in 1m16s: lint, unit, integration **on a real PostgreSQL 16 service**, migration round trip plus boot smoke, security |
| **Single-container mode** | VERIFIED | `app/allinone.py` against PostgreSQL 16.4: migrated, both threads up, crawled Metacritic live, synced 12 real games in 25s while serving |
| **Deployment — public URL** | VERIFIED | <https://playlens.45.67.202.162.sslip.io> — Let's Encrypt over the existing Caddy, HTTP redirects, 22/22 production smoke green |
| **Deployment — the stack** | VERIFIED | Four containers on the owner's Ubuntu 24.04 box, beside an n8n and a live site that were not touched. No new port opened |
| **Deployment — data survives** | VERIFIED | Full `down` then `up`: 86 games, 2,229 reviews and 31 summaries still there. `restart: unless-stopped` on all four |
| **Deployment — update and rollback** | VERIFIED | `deploy/update.sh` run four times: backup, build, migrate, health, public-name check. Automatic code rollback path exercised by design, not by luck |
| **Backups** | VERIFIED | `pg_dump` + gzip, retention, `.partial` naming. Two dumps on disk, cron at 03:17 nightly. **Same disk as the database** — not off-site |
| **Docker — build and run** | VERIFIED | Built and running on the server. Not on this machine, which still has no daemon |
| **Deployment — free path** | IMPLEMENTED — NOT VERIFIED | Hugging Face Spaces + Neon stays in the repository as the no-server option; no HF account exists |
| **Deployment — paid blueprint** | IMPLEMENTED — NOT VERIFIED | `render.yaml` parses, never applied. **Render is not free for this shape** — no free worker plan, ~$22/mo |
| **Backups** | DEFERRED | `pg_dump` procedure documented; automating it before there is data worth losing is premature |
| **Documentation** | VERIFIED | Every referenced path and module cross-checked against the filesystem |
| **Legal position** | NEEDS HUMAN ACTION | OQ-B3. Not a technical question |

---

## What I could not do, and why

| | Why | What unblocks it |
|---|---|---|
| **YouTube transcripts** | yt-dlp is offered only an m3u8 caption track; no PO-token path and no paid provider | A hosted transcript API, or a proxy. Degradation is honest today: link and metadata, no invented text |
| **Off-site backups** | The dumps sit on the same disk as the database. Somewhere to put them needs a credential that does not exist | An S3-compatible bucket, or any host with space. Ten lines in `deploy/backup.sh` |
| **A pretty hostname** | `playlens.mooo.com` is free but lives in the owner's FreeDNS account | One A record, then `SITE=playlens.mooo.com bash deploy/caddy-site.sh`. Cosmetic |

None of these stops anyone using the service.

---

## Found and fixed by deploying

Nine defects on the way to a live URL, every one found by running something on the
server. Not one of them could appear on the machine the code was written on.

1. **Compose concatenates `ports`.** The loopback bind was *added* to the base file's
   `0.0.0.0` instead of replacing it — two bindings, the second public. Caught by
   `docker compose config` before anything started.
2. **`cpus: "1.5"` on a one-core host.** Docker refuses rather than clamping, so the api
   would not start, and the worker and scheduler wait on its health.
3. **Every `.sh` was `100644` in git.** Written on Windows, where `chmod +x` changes
   nothing git records. The container died on its own entrypoint.
4. **The worker and the scheduler inherited an HTTP healthcheck** they cannot pass, and
   sat marked unhealthy while working. A permanently red light is the one you learn to
   ignore.
5. **`LLM_PROVIDER=openai` never reached the containers.** It was in `.env` and in no
   `environment:` block, so the worker used the default — anthropic — and failed every
   summary while holding a working OpenAI key.
6. **Every follow-up job a crawl queued was dead on arrival.** `payload={"slug": slug}`
   with `game_id` passed as a column; three handlers read `payload["game_id"]`. Reviews,
   similarity and YouTube were silently dead behind a crawl reporting 20/20 success and a
   healthy API. **The service looked perfect and did almost nothing.**
7. **A broken thumbnail on every game page with a video.** The image proxy's allow-list
   held only metacritic; the Let's Play section renders `i.ytimg.com`, so the page asked
   its own proxy for an image and got a 400.
8. **A game Metacritic would not let us ingest at all.** Two platforms came back with
   `isLeadPlatform` true; the database enforces one, correctly, so the insert died and
   the game was never stored.
9. **The suite was reading the developer's `.env`.** An untracked file silently overrode
   defaults under test, so a correct assertion failed locally and passed in CI.

Number 6 is the one worth remembering. Both sides of that seam had tests. The join
between them had none, and nothing short of running it would have said so.

---

## Found and fixed before that

Six defects, every one found by running something rather than reading it:

1. **`ROLE=cron` would have failed every hour, silently.** The Render blueprint uses a
   cron service; the container entrypoint understood only `api`, `worker` and
   `scheduler`, and would have exited 64. Found by a test that checks the blueprint
   against the entrypoint.
2. **A hardcoded port would have failed the health check.** Render, Railway and Cloud Run
   route to `$PORT`; the entrypoint pinned 8000. The deploy would have rolled back with
   "no open ports detected".
3. **An m3u8 manifest passed as a transcript.** On a real Elden Ring playthrough yt-dlp
   offered an HLS caption track; the download was 3,791 characters of URLs — long enough
   to clear the minimum-length guard — and would have gone to the model as speech. The
   exact failure ADR-016 exists to prevent.
4. **`python -m app.cli` did not exist.** ADR-014, FINAL_SPEC and IMPLEMENTATION_PLAN all
   specified it. The only way to trigger anything was curl with an admin token.
5. **gpt-4o-mini misses the claim-level target.** Six games said 87.2%; all twenty said
   78.2%. The default is now gpt-4o, which scores 85.7% on the same corpus for about a
   cent more per game.
6. **`/games/fragment` had no test** — the one route of 26 that nothing covered.

---

## What I would do next, in order

1. **Two free accounts** — Neon for the database, Hugging Face for the app. Eight
   minutes, no card, and the link exists. `docs/DEPLOYMENT_OPTIONS.md`.
2. **Twenty minutes reading summaries** (`docs/HUMAN_EVALUATION.md`) — the only
   measurement nobody has taken, and the only one that answers whether this is useful.
3. **The legal decision** — before the URL is shared with anyone.
