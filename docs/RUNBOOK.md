# Runbook

For whoever is on call. Every section starts with what you see, not with what the system
does internally.

The single most useful page is `/admin/monitoring`. It names the affected capability
rather than colouring the whole system red, because partial failure is a crawler's normal
state.

---

## 0. The two commands

```bash
curl -s localhost:8000/api/v1/health | jq            # is the process alive
curl -s localhost:8000/api/v1/monitoring/status | jq # is the pipeline alive
```

`health` answers "should the load balancer keep sending traffic here". `monitoring/status`
answers "is the data getting stale". They are deliberately different questions: the API
serves a perfectly good catalogue while the crawler is broken, and taking it out of
rotation for that would be the wrong response.

---

## 1. Alerts

### `system.status = "down"` — no successful crawl for 3+ hours

**What it means.** The catalogue is going stale. Nothing a user sees is broken yet.

1. Is the scheduler running? `docker compose ps scheduler`, or check for a recent
   `crawl.tick` job: `GET /api/v1/monitoring/jobs?job_type=crawl.tick`.
2. Is there a stuck active run? `GET /api/v1/monitoring/status` → `current_job`. A run
   older than an hour is stuck.
   ```bash
   curl -X POST localhost:8000/api/v1/admin/crawl/runs/<id>/cancel -H "X-Admin-Token: $ADMIN_TOKEN"
   ```
   Cancelling releases the run's claim; the next tick starts cleanly.
3. Is the source reachable? Look for `source.circuit_open` in
   `GET /api/v1/monitoring/events?level=problems`. If the breaker is open, it closes
   itself after `CIRCUIT_RESET_TIMEOUT_S` (default 300s). Do not restart to force it —
   the breaker exists to stop us hammering someone else's site.
4. Trigger a run manually and watch it:
   ```bash
   curl -X POST localhost:8000/api/v1/admin/crawl/run -H "X-Admin-Token: $ADMIN_TOKEN"
   curl -N localhost:8000/api/v1/monitoring/stream
   ```

### `system.status = "degraded"` — no worker reporting

Queued work is not being processed. **The catalogue and the API are unaffected**; nothing
new is being ingested.

```bash
docker compose ps worker
docker compose logs --tail=200 worker
docker compose up -d --scale worker=2
```

Jobs left `running` by a worker that died are reclaimed automatically once their lease
expires (`JOB_LEASE_TTL_S`, default 900s). You do not need to reset them by hand.

### `schema_drift` events — the source changed shape

This is the alert that matters most, and the one designed to be loud.

**What it means.** `SCHEMA_DRIFT_THRESHOLD` (10) games in a row failed to parse with no
successful parse in between. The crawl has **stopped on purpose** and will not start again
until one game parses. It did not write empty games, and the alert fires once for the
incident rather than every hour.

The dashboard shows `down` with the specific message, and `system.schema_drift_streak`
carries the current count — it is visible from 1, long before the circuit opens.

```sql
SELECT ts, event, message, data
FROM job_events
WHERE event LIKE 'schema.%'
ORDER BY id DESC LIMIT 20;
```

`schema.drift` is one game that would not parse. `schema.ok` is one that did, and it
resets the counter. `schema.drift_circuit_open` is the alert.

`data` contains the fragment that failed. Steps:

1. Fetch the same URL by hand and compare with a fixture in `docs/research-fixtures/`.
2. If the shape changed, add the new fixture, fix `app/parsers/metacritic.py`, and add a
   test for the new shape **and** the old one — sources revert.
3. As a stopgap only, `METACRITIC_SOURCE=html` switches to the HTML+JSON-LD path. It is
   slower and returns less, and it is a bridge, not a destination.

**To resume after fixing the parser**, make one game parse: the counter resets on the
first `schema.ok`. A manual run is the quickest way.

```bash
curl -X POST localhost:8000/api/v1/admin/crawl/run -H "X-Admin-Token: $ADMIN_TOKEN"
```

Do **not** raise `SCHEMA_DRIFT_THRESHOLD` to make the alert stop, and do not set it to 0
(which disables the circuit entirely). The threshold is what stands between a source
change and a catalogue full of blank games.

### `summary.generate` jobs dying instantly, six attempts each

Check the error class before assuming a model problem:

```sql
select error_class, count(*), left(max(error_message), 120)
from jobs where status = 'dead' group by 1 order by 2 desc;
```

**`IntegrityError` on `uq_summary_fingerprint`** — two jobs for one game, both recording
"nothing to summarise". Fixed in the service; if it reappears, the guard in
`_record_skip` is not seeing the row it is about to write.

**`RetryableError` with "Request too large ... tokens per min"** — `AI_MAX_INPUT_TOKENS`
is above the account's TPM allowance, so the largest corpora can never be summarised.
Lower it below the limit minus `LLM_MAX_OUTPUT_TOKENS`. A new OpenAI account is 30,000
TPM on gpt-4o; 20,000 is safe there. This is now reported as a permanent error naming the
setting, rather than retried six times.

### AI cost approaching the daily limit

`GET /api/v1/monitoring/status` → `ai.cost_usd` against `ai.daily_limit_usd`.

At the limit, summary jobs skip with `budget_exhausted` and reschedule for the next day.
Nothing fails; summaries are simply not refreshed. To raise it, change
`AI_DAILY_COST_LIMIT_USD` and restart the workers.

### Claim acceptance rate falling

`ai.claim_acceptance_pct` on the monitoring page. A sustained drop means the model is
producing claims the validator rejects — a prompt or model regression, visible here
before a user ever reads a bad summary.

```sql
SELECT validation, validation_detail, count(*)
FROM summary_claims
WHERE created_at > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 3 DESC;
```

`rejected_low_support` in bulk usually means the corpus shrank, not that the model got
worse. `rejected_aspect_unsupported` in bulk means the prompt drifted.

### YouTube quota exhausted

Expected, not an incident. Discovery defers to the next day; **no game is marked failed**.
Confirm with `GET /api/v1/monitoring/events?level=problems` → `youtube.quota_exhausted`.

---

## 2. Common tasks

### Force a re-sync of one game

```bash
curl -X POST localhost:8000/api/v1/admin/games/<slug>/resync -H "X-Admin-Token: $ADMIN_TOKEN"
```

Queues `game.sync` and `reviews.sync`. Reviews respect `REVIEWS_MIN_INTERVAL_H`; a resync
inside that window updates metadata only.

### Backfill the catalogue

New Releases only ever yields ~2 genuinely new games per hour. To seed depth, the
reconciliation job walks the browse endpoint:

```sql
INSERT INTO jobs (job_type, queue, idempotency_key, status, payload, priority)
VALUES ('crawl.reconcile', 'crawl', 'manual-backfill-' || now()::date,
        'queued', '{"max_new": 2000}', 5);
```

It respects the same rate limit as everything else. 2,000 games is roughly 3 hours.

### Retry a dead job

```bash
curl -X POST localhost:8000/api/v1/admin/jobs/<id>/retry -H "X-Admin-Token: $ADMIN_TOKEN"
```

Look at `error_message` first. A job that died five times usually has a real cause, and
retrying it a sixth time is how you spend an afternoon.

### Backfill Russian for a catalogue that predates it

```bash
python -m app.cli translate     # source descriptions
python -m app.cli summarise --all   # claims and headings
```

Both exist for the same reason. The work is queued by whatever changes the data —
`game.sync` for a description, `reviews.sync` for a summary — so a game that nothing
touches again keeps whatever it had. Neither command generates inline; both queue, so
neither can race the worker, and both are free to run twice.

Genres need neither: they are a table in `app/domain/genre_names.py`. A genre the table
has not met shows the source's own name, which is a small blemish rather than an invented
translation of a term of art.

### Recompute the "why these are similar" chips

The reason on a similar-game chip is **stored** when similarity is computed, not derived
when the page renders. Changing `reason_for` therefore reaches new rows only; the existing
ones keep whatever words they were written with. It is arithmetic, so a recompute costs
nothing but time:

```bash
sed -i 's/^SIMILARITY_REFRESH_STALE_DAYS=.*/SIMILARITY_REFRESH_STALE_DAYS=0/' .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d worker
#   enqueue one similarity.refresh_stale job; it fans out to every game
sed -i 's/^SIMILARITY_REFRESH_STALE_DAYS=.*/SIMILARITY_REFRESH_STALE_DAYS=7/' .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d worker
```

Zero days means "everything is stale", which is exactly what a changed rule needs. Put the
value back afterwards or the scheduler recomputes the whole catalogue nightly for nothing.

### Rewrite every summary after a prompt change

```bash
python -m app.cli summarise --all
```

Changing a prompt is not enough on its own, for two separate reasons that both had to be
fixed before this worked:

1. Nothing *enqueues* the rewrite. `summary.generate` is queued by `reviews.sync`, which
   runs only when the reviews change, so a game nobody reviews again is never asked.
2. Until `recipe_changed` existed, nothing *decided* to rewrite either. Every staleness
   check asked whether the reviews had moved; none asked whether the prompt or the model
   had. A queued job would run, find the reviews unchanged, and report success having
   left the old summary in place.

So: bump the version **and** run the command. The command keys by date, so a run stopped
by the daily cost ceiling is resumed by running it again tomorrow.

It queues rather than generates, so it cannot race the worker, and the enqueue key carries
the prompt version: running it twice is free. The worker drains the queue one job at a
time and stops for the day at `AI_DAILY_COST_LIMIT_USD`, resuming the next day. A
catalogue of 100 games is about 200 jobs and roughly $3 at gpt-4o.

### Roll back a bad summary batch

Summaries are versioned and only a `fresh` one becomes current. To revert a game to its
previous version:

```sql
UPDATE summaries SET is_current = false
WHERE game_platform_id = :gp AND audience = :aud AND is_current;

UPDATE summaries SET is_current = true
WHERE id = (SELECT id FROM summaries
            WHERE game_platform_id = :gp AND audience = :aud AND status = 'fresh'
            ORDER BY version DESC OFFSET 1 LIMIT 1);
```

The partial unique index `uq_summaries_current` will refuse to let you leave two current
summaries behind, which is exactly what you want from a manual edit at 3am.

---

## 3. Deploying

```bash
git pull
docker compose build
docker compose up -d api          # migrations run here, once, before it serves
docker compose up -d worker scheduler
```

Migrations run **only** in the api container's entrypoint. Three containers racing for
Alembic's lock on a cold start is how you get a worker crash-looping against a half-built
schema.

Every migration to date is additive. If one ever is not, take the workers down first:
they run the old code against the new schema for the length of the deploy.

### Rollback

```bash
docker compose down worker scheduler
python -m alembic downgrade -1
docker compose up -d
```

Check the downgrade actually exists in the revision before relying on it.

---

## 4. Capacity

| Thing | Where it is | Default |
|---|---|---|
| Requests to the source | `METACRITIC_RPS` | 2/s, burst 4. **The config rejects >5.** |
| Consecutive parse failures | `SCHEMA_DRIFT_THRESHOLD` | 10, then the crawl stops |
| AI corpus size | `AI_MAX_INPUT_TOKENS` | 60k tokens; the corpus is trimmed to fit |
| Image cache on disk | `IMAGE_CACHE_MAX_MB` | 512 MB, evicted least-recently-used |
| Games per crawl run | `CRAWL_MAX_GAMES_PER_RUN` | 20 |
| Worker concurrency | replicas (one job per process) | 2 |
| Open SSE streams | `SSE_MAX_CLIENTS` | 50, then 503 |
| Public API | `PUBLIC_RATE_LIMIT_PER_MIN` | 120/min per IP |
| AI spend | `AI_DAILY_COST_LIMIT_USD` | $10/day |
| YouTube quota | `YOUTUBE_DAILY_UNIT_BUDGET` | 9,500 units ≈ 94 games |

One worker process runs one job at a time, deliberately: the unit of concurrency is the
process, so more throughput means `docker compose up -d --scale worker=4`.

Adding workers does not speed up crawling: the token bucket is shared through the
database, so ten workers make the same two requests per second as two. It speeds up
summarisation and similarity, which are CPU- and API-bound rather than source-bound.

---

## 5. Retention

Run by the scheduler; nothing to do unless the database is growing unexpectedly.

| Table | Kept | Setting |
|---|---|---|
| `job_events` | 30 days | `EVENTS_RETENTION_DAYS` |
| `jobs` (finished) | 90 days | `JOBS_RETENTION_DAYS` |
| `crawl_items` | 180 days | `CRAWL_ITEMS_RETENTION_DAYS` |
| `review_snapshots` | 90 days **and unreferenced** | `SNAPSHOT_RETENTION_DAYS` |

The snapshot rule has both conditions for a reason: a snapshot cited by a current summary
is never deleted regardless of age. Deleting it would break every citation in a summary
still on screen.

---

## 6. When something is genuinely wrong

Get the correlation id first. Every log line and every event carries one:

```bash
curl -sD- localhost:8000/api/v1/games | grep -i x-request-id
```

```sql
SELECT * FROM job_events WHERE data->>'request_id' = '<id>' ORDER BY id;
```

Logs are JSON and redacted — keys containing `key`, `token`, `password`, `secret`,
`authorization`, `cookie` are masked before they are written. If you need a real value to
debug, get it from the environment, not from the logs.
