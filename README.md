# Playlens

**Live: <https://playlens.45.67.202.162.sslip.io>** · [repository](https://github.com/CRYCas0n/playlens)

A game intelligence service built on Metacritic data. It answers one question a score
cannot: **do critics and players agree, and if not, why not?**

The site is in Russian; this document is in English, as is the codebase.

Every number the interface shows is traceable to a stored review, every AI sentence is
checked against cited evidence before anyone reads it, and every absence — no score, no
summary, no video, nothing comparable — is stated rather than filled in.

---

## What is implemented

| | |
|---|---|
| **Ingestion** | Hourly crawl at seven past, twenty games per run from New Releases and then the full listing sorted by date. One selection per UTC day, deduplicated by a unique constraint rather than by a check |
| **Game data** | Title, cover, developer, description, release date, video link, and per-platform Metascore and Userscore. A missing score is never a zero and a real zero is never a blank ([ADR-002](docs/adr/ADR-002-score-value-model.md)) |
| **Reviews** | Critic and player reviews in immutable versioned snapshots, so a summary can always be traced to the exact text it was written from |
| **AI summaries** | Separate critic and player summaries. Every claim cites reviews by a stable reference and is discarded before publication if the citation does not support it — 91.6% of claims survive that check on live data |
| **Verdict** | The sentence at the top of a game page is *arithmetic*, not model output, so it cannot contradict the numbers beside it ([ADR-012](docs/adr/ADR-012-derived-verdict.md)) |
| **Catalogue** | Search, platform filters with live counts, sorting by either score or by the critic–player gap, and similar games with a stated reason where a real one exists |
| **Let's Play** *(additional part 1)* | YouTube search, explainable ranking, selection, a transcript cascade with fallbacks, and a spoiler-free AI conclusion drawn from the transcript rather than from reviews |
| **Monitoring** *(additional part 2)* | Worker and scheduler state, the current job, 24-hour counters, AI spend against its ceiling, the queue, the problem log, and Run Now — updating live over SSE |

Acceptance against the original assignment, row by row and judged against production
rather than against the code: **[`docs/FINAL_TZ_ACCEPTANCE.md`](docs/FINAL_TZ_ACCEPTANCE.md)**.

## Architecture in one paragraph

FastAPI and server-rendered Jinja, PostgreSQL, and a job queue that is a table in that
same database — `FOR UPDATE SKIP LOCKED`, no Redis and no Celery
([ADR-003](docs/adr/ADR-003-job-queue.md)). Four containers: api, worker, scheduler,
database. Correctness lives in constraints rather than in code that remembers to check:
five partial unique indexes make a duplicate crawl, a duplicate summary and a second
lead platform impossible to write. Live updates come from an append-only event table
whose row id *is* the SSE `Last-Event-ID`, so a reconnecting browser resumes exactly
where it stopped.

---

> **Never run a project like this before?** Start with
> **[`docs/QUICKSTART.md`](docs/QUICKSTART.md)** — every command written out, with what
> you should see after each one. Then **[`docs/HANDOFF.md`](docs/HANDOFF.md)** for what
> to do next.

## Quick start

```bash
cp .env.example .env
python -c "import secrets; print('ADMIN_TOKEN=' + secrets.token_urlsafe(32))" >> .env

make install
make dev            # migrations + API on http://localhost:8000
```

In another terminal:

```bash
make worker         # processes the queue
make scheduler      # enqueues the hourly crawl
```

Then fill the catalogue. There is a command line for this, so no curl and no token:

```bash
python -m app.cli status             # health, counts, cost, queue depth
python -m app.cli crawl              # one crawl tick, inline
python -m app.cli seed --limit 500   # the top of the ranking, same pipeline (ADR-014)
python -m app.cli summarise --slug ashen-veil
```

Watch it at <http://localhost:8000/admin/monitoring>, or read
`GET /api/v1/monitoring/stream` for the same events as SSE.

With Docker instead: `ADMIN_TOKEN=... make up` brings up PostgreSQL, the API, two
workers and one scheduler.

### Without any keys

The default configuration has **no API keys at all** and is fully functional:

| Feature | Without a key |
|---|---|
| Catalogue, search, filters, game pages | works |
| Scores, verdicts, platform comparison, gap detection | works |
| Similarity and "why" reasons | works — the engine is arithmetic, not a model |
| AI review summaries | pending state, section kept, UI says why |
| Let's Play | section absent |

Set `LLM_ENABLED=true` with `LLM_API_KEY` for summaries — `LLM_PROVIDER` chooses between
`openai` and `anthropic`, and the key variable is the same either way, so switching is one
line. `YOUTUBE_ENABLED=true` with `YOUTUBE_API_KEY` for Let's Play.

---

## What it does

**Crawls** Metacritic hourly, taking at most 20 games per run. Each run claims its games
atomically, so two runs never process the same game (`crawl_items` has a partial unique
index that makes double-processing impossible rather than unlikely).

**Fetches reviews** for up to three platforms per game, paginating adaptively — the API
ignores the `limit` parameter on critic reviews and returns 10 per page regardless, so
the client measures the page it actually received instead of trusting the request.

**Snapshots** the reviews it used, immutably, before summarising. A summary cites
`C03`, `U17`; those references resolve to exact review texts for as long as the summary
exists, so a citation cannot rot when the source page changes.

**Summarises** with a language model, then validates every claim against the snapshot:
enough supporting reviews, aspect vocabulary actually present in them, no invented
temporal or comparative statement. A claim that fails is discarded before rendering,
with the reason stored.

**Compares** critics and players on one 0–100 axis and derives the verdict sentence
arithmetically. It cannot contradict the two numbers above it, because it is computed
from them.

**Recommends** similar games from a weighted, explainable blend of metadata, aspect
profiles and lexical overlap. When no component contributes enough to name a reason,
no reason chip appears — the rail never invents an explanation.

---

## The decisions that shaped it

Full reasoning in [`docs/adr/`](docs/adr/). The ones that most affect what you see:

| | Decision | Why |
|---|---|---|
| [ADR-002](docs/adr/ADR-002-score-value-model.md) | `null` is never `0` | A player score of 0 from two ratings is the absence of a score. All four zeros in the Release 0 sample had ≤3 ratings; the lowest genuine score was 0.5 from 6,382. Rendering it as 0 would put those games at the extreme of every ranking. |
| [ADR-008](docs/adr/ADR-008-evidence-driven-summaries.md) | Claims are validated, not trusted | The model proposes; a deterministic validator decides. Elden Ring's critic corpus is 86 positive, 0 negative — a fixed "3 pros / 3 cons" layout would have manufactured three complaints. |
| [ADR-012](docs/adr/ADR-012-derived-verdict.md) | The verdict is computed | A generated sentence can disagree with the scores next to it. An arithmetic one cannot, and needs no AI disclaimer. |
| [ADR-003](docs/adr/ADR-003-job-queue.md) | No Redis, no Celery | The queue is a table with `FOR UPDATE SKIP LOCKED`. Job state is then queryable with the same SQL as everything else, and there is one fewer thing to run. |
| [ADR-017](docs/adr/ADR-017-server-rendered-frontend.md) | Server-rendered Jinja2, not Next.js | The design package's CSS is ported verbatim. One process, one language, and the HTML and the JSON API are built from the same presenter — so a rule fixed in one is fixed in both. |
| [ADR-016](docs/adr/ADR-016-youtube-enrichment.md) | YouTube is isolated and off | An empty HTTP 200 from the caption endpoint is a provider failure, not "no subtitles". The whole cascade is built around refusing to record that false negative. |
| [ADR-020](docs/adr/ADR-020-settings-must-do-something.md) | A setting must do something | Twelve were declared and never read — including the schema-drift circuit the runbook described. Five implemented, seven deleted, and a test that stops the thirteenth. |
| [ADR-021](docs/adr/ADR-021-verification-environment.md) | Check the environment before calling something unverifiable | Three "cannot be tested here" claims turned out to be untrue. Chromium was installed all along, and it found five defects in an hour. |

The audit that produced them: [`docs/CONTRADICTIONS.md`](docs/CONTRADICTIONS.md) — 28
conflicts between the source documents, each resolved against primary fixture data.
[`docs/FINAL_SPEC.md`](docs/FINAL_SPEC.md) is the canonical specification.

---

## Architecture

```
                 ┌────────────┐
   scheduler ───▶│            │◀─── worker × N   (claims jobs, SKIP LOCKED)
                 │ PostgreSQL │
   API ─────────▶│            │     jobs · job_events · games · reviews
   (FastAPI)     └────────────┘     review_snapshots · summaries · similar_games
        │
        ├─ /api/v1/*      JSON, RFC 7807 problems, OpenAPI at /api/v1/docs
        ├─ /              server-rendered pages, same presenters as the API
        └─ /api/v1/monitoring/stream   SSE replayed from job_events
```

```
app/
  domain/        scores, verdicts, enums, errors — no I/O, no framework
  normalizers/   text handling, including prompt sanitisation
  adapters/      metacritic · llm · youtube · http  (all external access)
  parsers/       JSON and HTML shape handling, one module each
  repositories/  every query in the system
  services/      crawl · game_sync · review_sync · snapshots · summaries ·
                 similarity · letsplay · catalog · monitoring
  ai/            corpus building, prompts, output schemas, claim validator
  queue/         worker and scheduler
  api/           routers, schemas, presenters, problem responses
  web/           Jinja2 templates, ported CSS/JS, image proxy
```

Scraping lives entirely in `adapters/metacritic/` and `parsers/`. Nothing else in the
codebase knows the source exists — swapping it means writing one adapter.

---

## Testing

`make` is a convenience for Linux and CI; on Windows type the command inside each target
directly (`python -m pytest -q`, and so on) — [`docs/QUICKSTART.md`](docs/QUICKSTART.md)
spells them out.

```bash
make test              # 781 tests
make test-unit         # no database, no network
make test-integration  # real Alembic migrations, real queries
make test-pg           # the same suite against PostgreSQL 16 (362 of them, green)
make lint
make smoke             # boot the assembled app, request every route
make browser           # real Chromium over every page at 1440/1280/768/390
make contract          # real requests to the live source (opt-in)
```

Five kinds of test, because each catches things the others cannot:

| | What it proves |
|---|---|
| **unit** | A rule is right. No database, no network |
| **integration** | The rule survives contact with a real schema and real queries |
| **browser** | The page a person sees is correct — it found a dashboard printing its own internals into every tile, and a phone layout that scrolled sideways |
| **wire** | SSE really works over a socket: framing, ordering, `Last-Event-ID` replay |
| **contract** | The live source still has the shape the parser expects. Opt-in: it makes real requests |

Integration tests build the schema by running the **real migrations**, not
`create_all` — a migration that drifts from the models is a production incident, so the
tests exercise the path production uses.

Fixtures are real captured responses in `docs/research-fixtures/`, including the
21-game Release 0 harvest. The AI pipeline is tested end to end with a fixture provider,
so it runs without an API key.

`make demo` loads nine fictional games covering every awkward shape the design package
names — no score, no cover, eleven platforms, a 72-character title — so there is
something to look at before the first crawl.

---

## API

`GET /api/v1/docs` for the full OpenAPI document.

```
GET  /api/v1/games                    ?q= &platform= &score_band= &released=
                                      &sort=metascore|userscore|release_date|gap|title
GET  /api/v1/games/{slug}             ?platform=
GET  /api/v1/games/{slug}/similar
GET  /api/v1/games/{slug}/summaries   version history, with claim validation results
GET  /api/v1/platforms
GET  /api/v1/genres
GET  /api/v1/search/suggest           ?q=
GET  /api/v1/stats
GET  /api/v1/monitoring/status        health, queues, workers, data quality, AI cost
GET  /api/v1/monitoring/stream        SSE; Last-Event-ID replays from the event log
POST /api/v1/admin/crawl/run          X-Admin-Token
GET  /api/v1/health
```

Scores are always objects, never bare numbers:

```json
"userscore": {
  "value": null, "normalized": null, "status": "unavailable",
  "tier": "none", "tier_label": "Not rated", "review_count": 2, "scale_max": 10
}
```

`status: "unavailable"` with `review_count: 2` is that game's real state: two people
rated it 0, which is not a score of zero.

---

## Operating it

| | |
|---|---|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Get it running, command by command |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | What still needs a person, in order |
| [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) | What is verified, what is not, and why |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Every alert, its diagnosis and its fix |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Render, a VPS, and what each costs |
| [`docs/RELEASE0_FINAL.md`](docs/RELEASE0_FINAL.md) | The AI measurement on all 20 games, including the number that missed target |
| [`docs/FINAL_PRODUCTION_GAP.md`](docs/FINAL_PRODUCTION_GAP.md) | Every area, its evidence, its status |
| [`docs/FINAL_AUDIT.md`](docs/FINAL_AUDIT.md) | What was checked by running it, and the 21 defects that found |
| [`docs/HUMAN_EVALUATION.md`](docs/HUMAN_EVALUATION.md) | Twenty minutes to find out whether the AI summaries are any good |

---

## Legal

Not affiliated with, endorsed by, or connected to Metacritic or Fandom. Review text is
never republished in full; summaries are compressions with counts and links back to the
source. Requests are rate-limited to 2/second — the configuration refuses to go above 5.
