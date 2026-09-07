# Project Status

**Date:** 2026-09-06 · **Run:** `749 passed, 13 skipped` · `ruff: clean` · `make smoke: 15 routes` · browser pass: **all checks passed**

Statuses mean exactly one thing each:

| | |
|---|---|
| **VERIFIED** | The code exists **and** a test I ran proves the behaviour |
| **IMPLEMENTED — NOT VERIFIED** | The code exists; nothing available here could prove it works |
| **BLOCKED** | Cannot be done in this environment at all, and why |
| **DEFERRED** | Deliberately not done, with a reason |
| **NOT APPLICABLE** | Does not apply to this project |

---

## The table

| Area | Status | Evidence |
|---|---|---|
| **Backend** | VERIFIED | 749 tests. `make smoke` boots the assembled app and requests 15 routes. FastAPI, 9 runtime dependencies |
| **Database (SQLite)** | VERIFIED | Real Alembic migrations in every integration test. `test_migrations.py` runs upgrade → downgrade → upgrade and checks every constraint survives |
| **Database (PostgreSQL)** | BLOCKED | 149 MB free disk; PostgreSQL could not be installed. The schema is dialect-portable and `make test-pg` runs the same suite against it |
| **Migrations** | VERIFIED | 3 revisions. Round-trip tested, single head enforced, no silent no-op downgrades |
| **Crawler** | VERIFIED | `test_crawl_orchestrator.py` (15), `test_crawl_repository.py` (13). Concurrency proved with real threads, not mocks |
| **Schema-drift circuit** | VERIFIED | `test_schema_drift_circuit.py` (15). The crawl stops after N consecutive parse failures and alerts once, not hourly |
| **Reviews** | VERIFIED | Adaptive pagination, dedup, immutable snapshots. `test_snapshots.py`, `test_metacritic_parsers.py` |
| **Review resync policy** | VERIFIED | `test_full_resync.py` (10). A deep pass every 30 days; the page cap is genuinely lifted |
| **Scores** | VERIFIED | `test_scores.py`, `test_score_sql.py`. The Python rule and the SQL rule are proved identical over a matrix of cases |
| **AI pipeline** | VERIFIED (without a live model) | `test_summary_pipeline.py` (14), `test_validator.py` (27), end to end on a fixture provider. Prompt, snapshot, validation, storage, versioning |
| **AI cost ceiling** | VERIFIED | `test_ai_budget.py` (9). Was declared and unread; now blocks generation before the call |
| **AI prompt budget** | VERIFIED | `test_prompt_budget.py` (12). The corpus is sized to the context window, not to the review cap |
| **AI provider — OpenAI** | VERIFIED | `test_openai_client.py` (26) offline, plus a real call: 6 Release 0 games, PV1 87.2%, PV2 100%, $0.0074 |
| **AI provider — Anthropic** | IMPLEMENTED — NOT VERIFIED | No Anthropic key. The adapter and its tests exist; the protocol is the same one OpenAI now exercises for real |
| **AI summary quality, machine-checkable** | VERIFIED on a partial sample | 6 of 20 Release 0 games: claim-level 87.2% (target 80), block-level 100% (target 85) |
| **AI summary quality, is-it-useful** | BLOCKED | Needs a person, not a key (OQ-V4). `docs/HUMAN_EVALUATION.md`, twenty minutes |
| **Similarity** | VERIFIED | `test_similarity.py` (12). Explainable, deterministic, no reason invented |
| **YouTube** | IMPLEMENTED — NOT VERIFIED | `test_youtube_ranking.py` (29), `test_youtube_transcripts.py` (14), `test_letsplay_service.py` (14), all against fixtures. No API key exists (OQ-B2) |
| **Frontend (data rules)** | VERIFIED | `test_web_pages.py` (40). Walks the 28 cases of `design/EDGE_CASES.md` |
| **Frontend (rendering)** | VERIFIED (see the note below) | Real Chromium, 12 pages × 4 viewports (1440/1280/768/390): no horizontal overflow, no console errors, correct status codes |
| **Frontend (interaction)** | VERIFIED | Theme toggle, filter chips, SSE connection — all driven in a real browser |
| **Monitoring API** | VERIFIED | `TestMonitoringContract` asserts the shape of every indicator the assignment lists |
| **Monitoring page** | VERIFIED | Rendered in a browser. A bug that printed the whole status object into every KPI tile was found this way and fixed |
| **SSE** | VERIFIED | `test_sse_wire.py` (7) against a real uvicorn server: framing, ordering, `Last-Event-ID` replay, disconnect |
| **Image proxy** | VERIFIED | SSRF and allow-list offline; a real cover fetched, resized and cached over the network. A 403-on-default-User-Agent defect was found this way |
| **Image cache eviction** | VERIFIED | `test_image_cache.py` (11). Was declared and unread |
| **Query efficiency** | VERIFIED | `test_query_counts.py` (9). Counts real statements; proves the count does not grow with row count |
| **Security** | VERIFIED | `test_security.py` (30): admin auth, SSRF, XSS, prompt injection, secret redaction, rate limiting |
| **Secrets hygiene** | VERIFIED | No secrets in source (grep test), `.env` ignored, `.env.example` ships every secret empty, logs redacted |
| **Configuration** | VERIFIED | `test_no_dead_settings.py`. Every setting is read somewhere; 7 that could never be were removed with reasons |
| **Docker (files)** | VERIFIED as text | `test_compose_files.py` (21): parse, roles, non-root, healthcheck, layer order, no exposed database |
| **Docker (running)** | BLOCKED | Docker is not installed and there is no disk space for it |
| **Live source contract** | VERIFIED | `tests/contract/` run against the real API: listing, detail, per-platform stats, the 10-per-page critic rule, catalogue size |
| **Tests** | VERIFIED | 749 passing, 13 opt-in. Unit / integration / browser / wire / contract |
| **Documentation** | VERIFIED | Quickstart walked command by command; every command in it was run |
| **Legal position** | DEFERRED — owner decision | OQ-B3. Not a technical question and not mine to answer |
| **Authentication (end users)** | NOT APPLICABLE | The assignment has no user accounts. Admin actions use a token |

---

> **Про повторный прогон.** Зелёный результат зафиксирован 19:24; с тех пор ни один файл
> в `app/` не менялся (менялись только тесты, документация и скрипты), поэтому он
> относится к текущему коду. Повторить его сейчас нельзя: на диске осталось **97 МБ**, и
> Chrome падает с `Page crashed` ещё до первой страницы. Это ограничение машины, а не
> приложения, и оно же — причина, по которой не проверены PostgreSQL и Docker.

---

## What is still blocked

| | Why | What unblocks it |
|---|---|---|
| PostgreSQL | 149 MB free disk | A few GB free, then `make test-pg` |
| Docker | Not installed, no room to install it | Docker Desktop, then `docker compose up --build` |
| A blind human read of the summaries | Needs a person who did not write them | `docs/HUMAN_EVALUATION.md` |

None of the three blocks the service from running. It runs now, on SQLite, without a
single key.

---

## Twenty defects found and fixed during this pass

Full detail in `FINAL_AUDIT.md` §3. The ones that would have hurt most:

1. **The monitoring dashboard printed its entire internal state object into every KPI
   tile.** Forty HTML tests passed while it did. A browser found it in one second by
   measuring the page width. *(A Jinja `{% set %}` inside a `{% for %}` does not escape
   the loop.)*
2. **The image proxy got 403 from the CDN on every request** because it sent httpx's
   default User-Agent. Every cover silently fell back to a redirect, so the browser
   downloaded a 2.3 MB original for each of 24 cards on a page.
3. **`SCHEMA_DRIFT_THRESHOLD` did nothing.** The setting, the runbook and the audit all
   described a circuit that stops crawling after repeated parse failures. It did not
   exist.
4. **Twelve settings were declared and never read** — after `AI_DAILY_COST_LIMIT_USD`
   and `IMAGE_CACHE_MAX_MB` had already been found the same way. Five are now implemented,
   seven deleted with reasons, and a test prevents a thirteenth.
5. **Every page scrolled sideways on a phone.** Found by measuring in Chromium at 390px.

---

## What I would still do, in order

1. **PostgreSQL run** (`make test-pg`) — the single highest-value remaining check.
2. **Docker build** — proves the deployment story rather than describing it.
3. **A key and twenty minutes of reading** (`docs/HUMAN_EVALUATION.md`) — the only way to
   learn whether the summaries are worth having.
4. **The legal decision** — before any public address.
