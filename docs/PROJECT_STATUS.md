# Project Status

**Date:** 2026-09-07 · **Repository:** <https://github.com/CRYCas0n/playlens>

**CI:** green on GitHub Actions · **Run:** `791 tests` · `ruff: clean` · `362 integration tests green on PostgreSQL 16.4` ·
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
| **Metacritic ingestion** | VERIFIED | 17 contract tests against the **live** API: listing, detail, per-platform stats, the 10-per-page rule, catalogue size |
| **Deduplication** | VERIFIED | Re-running a crawl adds nothing; two workers cannot claim the same game |
| **Review snapshots** | VERIFIED | Immutable, stable evidence refs, 9 tests |
| **AI — OpenAI** | VERIFIED | 26 offline tests plus live calls. Full lifecycle: 401, 404, 429, 5xx, timeout, malformed JSON, prose instead of a call, unknown enum |
| **AI — Anthropic** | IMPLEMENTED — NOT VERIFIED | No Anthropic key. Same protocol the OpenAI adapter now exercises for real |
| **AI — Release 0, 20 games** | VERIFIED | gpt-4o: **PV1 85.7%**, PV2 100%, $0.015/game. gpt-4o-mini: PV1 78.2%, below target. `docs/RELEASE0_FINAL.md` |
| **AI — evidence validation** | VERIFIED | 42 rejections on real data across 6 classes; 7 invented references caught, 0 published |
| **AI — cost ceiling** | VERIFIED | Enforced before the call, not after. 9 tests |
| **AI — is it useful** | NEEDS HUMAN ACTION | Needs a person who did not write the summaries. `docs/HUMAN_EVALUATION.md`, 20 minutes |
| **YouTube — discovery and ranking** | VERIFIED | Live Data API: 15 candidates, 6 rejected by filters, a real playthrough selected with explainable components |
| **YouTube — transcripts** | BLOCKED | yt-dlp offered only an m3u8 caption track. No PO-token path, no paid provider. Degradation verified: link and metadata shown, **no AI text** |
| **Similarity** | VERIFIED | 12 tests. No reason invented below the contribution threshold |
| **Search, filters, sorting** | VERIFIED | Both dialects. Unrated never floats to the top in either direction |
| **UI — data rules** | VERIFIED | All 28 cases of `design/EDGE_CASES.md`, 44 tests |
| **UI — rendering** | VERIFIED | Real Chromium, 12 pages × 4 widths, against the PostgreSQL-backed app |
| **Accessibility** | IMPLEMENTED — NOT VERIFIED | Landmarks, labels, skip link, `aria-current` present. No axe audit (OQ-N3) |
| **Security** | VERIFIED | 30 assertions, one per threat in ADR-019: SSRF, XSS, prompt injection, secret redaction, admin auth, rate limiting |
| **Secrets** | VERIFIED | Index scanned before the first commit, 0 hits. `.env` ignored, `.env.example` ships every secret empty |
| **Monitoring** | VERIFIED | Every indicator the assignment lists, asserted as a shape rather than a 200 |
| **Schema-drift circuit** | VERIFIED | Stops the crawl, alerts once per incident, shows `down` with the specific reason |
| **SSE** | VERIFIED | Real uvicorn on a real socket: framing, ordering, `Last-Event-ID` replay, disconnect |
| **Worker and scheduler** | VERIFIED | Leases, retries, heartbeats, slot idempotency |
| **Image proxy and cache** | VERIFIED | Allow-list offline; a real 2.3 MB cover fetched, resized under 400 KB, cached; eviction tested |
| **Operator CLI** | VERIFIED | `python -m app.cli` — status, crawl, seed, summarise, purge. Promised by ADR-014 and previously missing |
| **Health and readiness** | VERIFIED | Reachable ≠ ready: `ok` / `not_migrated` / `down`, each saying what to do |
| **Docker — files** | VERIFIED as text | 30 static assertions. Two real deployment bugs found this way |
| **Docker — build and run** | BLOCKED | No daemon; installing Docker Desktop needs administrator rights and a reboot |
| **CI** | VERIFIED | 5 jobs green on GitHub Actions in 1m16s: lint, unit, integration **on a real PostgreSQL 16 service**, migration round trip plus boot smoke, security |
| **Single-container mode** | VERIFIED | `app/allinone.py` against PostgreSQL 16.4: migrated, both threads up, crawled Metacritic live, synced 12 real games in 25s while serving |
| **Deployment — free path** | IMPLEMENTED — NOT VERIFIED | Hugging Face Spaces + Neon, no card. Root `Dockerfile` and `allinone` mode both tested; no HF account exists |
| **Deployment — paid blueprint** | IMPLEMENTED — NOT VERIFIED | `render.yaml`: database, web, worker, cron. Parses, tested statically, never applied. **Render is not free for this shape** — no free worker plan, ~$22/mo |
| **Deployment — public URL** | NEEDS HUMAN ACTION | No hosting account exists. `docs/HUMAN_DEPLOYMENT_HANDOFF.md` |
| **Backups** | DEFERRED | `pg_dump` procedure documented; automating it before there is data worth losing is premature |
| **Documentation** | VERIFIED | Every referenced path and module cross-checked against the filesystem |
| **Legal position** | NEEDS HUMAN ACTION | OQ-B3. Not a technical question |

---

## What I could not do, and why

| | Why | What unblocks it |
|---|---|---|
| **Public URL** | No hosting account. Creating one needs an email confirmation and an OAuth grant only the account owner can give | 10 minutes on render.com — `HUMAN_DEPLOYMENT_HANDOFF.md` §2 |
| **Docker verified** | Docker Desktop needs administrator rights and a reboot | Install it, then `docker compose build` |

Neither is a code problem. The service runs, on PostgreSQL, with working AI and green CI,
right now.

---

## Found and fixed in this pass

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
