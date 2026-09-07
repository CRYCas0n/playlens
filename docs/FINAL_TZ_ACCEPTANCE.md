# Acceptance against the original assignment

Every row is judged against the deployed service at
<https://playlens.45.67.202.162.sslip.io>, not against the code. A feature that exists in
the repository and does not run in production is not VERIFIED here, and three rows moved
*down* during this pass for exactly that reason.

| | |
|---|---|
| **VERIFIED** | A check run against production or a real dependency proves the behaviour |
| **IMPLEMENTED — NOT VERIFIED** | The code exists and is tested; nothing available could prove it end to end |
| **BLOCKED** | Impossible in this environment, with the reason |

---

## Mandatory part — ingestion

| Assignment | Implementation | How it was checked | Evidence | Status |
|---|---|---|---|---|
| Run once an hour | `Scheduler` + `CRAWL_INTERVAL_CRON=7 * * * *`; enqueue is idempotent per slot | Read the live monitoring API over several hours | `counters24h.runs = 11`, `next_run_at = 19:07Z`, cadence `7 * * * *` | **VERIFIED** |
| First 20 games from New Releases | `new_releases_limit=20`, `crawl_max_games_per_run=20` | 17 contract tests against the **live** Metacritic API, plus production runs | `games_processed = 220` over 11 runs — exactly 20 per run | **VERIFIED** |
| Then "SEE ALL", sorted by New, paging onward | `finder` endpoint with `sortBy=-releaseDate`, offset paging, 10-per-page rule verified against the source | Live contract tests; production crawl fetches multiple pages per run | `pages_fetched` > 1 in run results; 219 games in the catalogue from 11 runs | **VERIFIED** |
| Start the selection again each new day | `crawl_days` row per UTC date; a new day opens a new run | Integration tests on the day boundary, and 11 runs across one day in production | `uq_crawl_day` per date; runs share the day's claim set | **VERIFIED** |
| Never process the same game twice in a day | `UNIQUE(crawl_date, game_slug)` + `ON CONFLICT DO NOTHING`, claimed with `FOR UPDATE SKIP LOCKED` | Production counter, and a concurrency test with real threads | `skipped_duplicates = 21` in 24h — the constraint refusing repeats, not code guessing | **VERIFIED** |
| Add a new game | `GameSyncService.upsert` | Production growth from 0 to 219 games | `games_total = 219`, `games_with_metascore = 219` | **VERIFIED** |
| Update an existing game | Differential update: unchanged input writes no rows | Integration test asserts an unchanged day creates nothing; production re-crawls daily | `skipped_duplicates`, `changed` flags in run results | **VERIFIED** |

## Mandatory part — game data

| Assignment | Implementation | How it was checked | Evidence | Status |
|---|---|---|---|---|
| Title, cover, developer, description, release date | Parsed from the source's own JSON API | Read back from the production API for a multi-platform game | See "Live evidence" below | **VERIFIED** |
| Platforms (several) | `game_platforms`, one lead enforced by a partial unique index | Production games carry 3–7 platforms; a real game with two `isLeadPlatform` flags was found and normalised | `uq_game_platforms_one_lead` | **VERIFIED** |
| Metascore, per platform | `criticScoreSummary` per platform | Production API and page | Per-platform rows on the game page | **VERIFIED** |
| Userscore, per platform | Separate per-platform stats endpoint | Same | Same | **VERIFIED** |
| Video link | Source's attached video, rendered as a link and never called a trailer | Production game pages | `video.url` on games that have one | **VERIFIED** |
| **`NULL` is not `0`, and `0` is not `NULL`** | `ScoreValue` with a status enum (`valid` / `unavailable` / `n/a`); a real 0 from ≥20 ratings stays 0, a 0 from 2 ratings is unavailable | 28 edge-case tests, plus both shapes found in production data | A game with `userscore 0, status valid` and one with `status unavailable` both render correctly — an em dash, never a zero | **VERIFIED** |

## Mandatory part — reviews and AI

| Assignment | Implementation | How it was checked | Evidence | Status |
|---|---|---|---|---|
| Critic reviews | Paged from the source, stored in immutable versioned snapshots | Production | 9,859 reviews stored | **VERIFIED** |
| User reviews | Same, with filtering (duplicates, very short, score/text contradiction) | Production | Same | **VERIFIED** |
| Critic summary by AI | `summary.generate`, evidence-validated | 143 games carry summaries in production, written by a live model | `games_with_summaries = 143` | **VERIFIED** |
| User summary by AI | Same, separate audience and thresholds | Same | Same | **VERIFIED** |
| Summaries rest on real reviews | Every claim cites `evidence_ref`s that must exist in the snapshot | 42 rejections across 6 classes on real data; 7 invented references caught and none published | `claim_acceptance_pct = 91.6` | **VERIFIED** |
| Stable evidence IDs | `C00`/`U17` refs stable within a versioned snapshot | 9 snapshot tests | Immutability asserted | **VERIFIED** |
| No invented negatives | An empty criticism list is stated, never padded | Release 0 finding C-06; honest empty copy on the page | "Рецензенты почти ни к чему не придираются: положительных рецензий 86, отрицательных нет." | **VERIFIED** |
| Dates respected | A claim about change over time needs cited reviews spanning ≥30 days | Rejections observed in production | `rejected_temporal_unsupported` in the live claim counters | **VERIFIED** |
| Platform-aware | Summaries are per `game_platform`; the verdict names the platform gap | Production pages | Per-platform rows and the platform line | **VERIFIED** |
| Survives a repeated crawl | `uq_summary_fingerprint` refuses identical work; the guard runs before the model is called | Production, twice, loudly — the constraint held and the caller was fixed to ask first | 0 duplicate summaries | **VERIFIED** |
| Regenerates when reviews change | `new_reviews`, `new_ratio`, `score_moved`, `staleness`, `recipe_changed` | Integration tests per reason, plus a production prompt change that reached 250 summaries | `recipe_changed` added during this pass because a prompt change reached nothing | **VERIFIED** |

## AI provider

| Assignment | Implementation | How it was checked | Evidence | Status |
|---|---|---|---|---|
| OpenAI provider, `LLM_API_KEY`, `LLM_MODEL` | httpx + tool calling; settings read at runtime | Live calls in production | 318 calls, gpt-4o | **VERIFIED** |
| Retry | Retryable vs permanent classification; 9 attempts spanning ~17 minutes | Real 429s in production, both kinds | An over-size 429 is permanent and names the setting; an ordinary one retries | **VERIFIED** |
| Malformed output | Non-JSON, prose instead of a tool call, wrong shape | 26 offline tests plus real occurrences | Each mapped to retryable or permanent deliberately | **VERIFIED** |
| Unknown aspect | Coerced to `other`; unknown verdict keys dropped | Found in production with gpt-4o-mini and fixed | Whole summaries no longer lost to one bad enum | **VERIFIED** |
| Budget limit | Two ceilings (daily cost, hourly calls), enforced before the call | Production: 237 jobs observed deferred with `next_retry_at` at the top of the hour | It defers now; it used to return success having done nothing | **VERIFIED** |
| Cache / no repeated spend | The fingerprint is the cache: identical input cannot be paid for twice | The unique index, exercised in production | `uq_summary_fingerprint` | **VERIFIED** |
| API errors | 401, 404, 429, 5xx, timeout each handled distinctly | Offline tests and live 429s | 401 names `LLM_API_KEY`, 404 names `LLM_MODEL` | **VERIFIED** |
| No secret leakage | Redaction in logs; secrets never in the index | 0 matches across three containers' logs; full git-history scan of 624 blobs across 8 secret classes | Nothing found | **VERIFIED** |

## Additional part 1 — YouTube

| Assignment | Implementation | How it was checked | Evidence | Status |
|---|---|---|---|---|
| Search YouTube | Data API v3 `search.list`, quota-budgeted | Live API in production | 2,285 videos recorded, 180 discovery jobs | **VERIFIED** |
| Pick a relevant Let's Play | Filters on duration, language, title shape | Live | 6 of 15 candidates rejected by filters in one observed run | **VERIFIED** |
| Ranking | Explainable components: relevance, popularity, duration, recency | Live | `score` and `components` stored per candidate | **VERIFIED** |
| Choose the most popular suitable video | `select()` above `yt_min_score`; no video is a supported outcome | Live | `is_selected` rows | **VERIFIED** |
| Fetch the transcript | Provider cascade: yt-dlp → hosted API → metadata-only | Attempted in production **after this pass linked the chain** | Before this pass: 180 discoveries and **zero** transcript attempts — the three tasks were never joined | **IMPLEMENTED — NOT VERIFIED** |
| Fallback between providers | The cascade records every attempt and its error | Unit tests on the cascade; no hosted provider is configured, so only yt-dlp runs | `YT_TRANSCRIPT_API_URL` is unset — a paid dependency | **IMPLEMENTED — NOT VERIFIED** |
| AI conclusion from the transcript | `youtube.summarise`, written from the video, labelled as such | Cannot run without a transcript | ADR-016: no transcript, no conclusion — never an impression from a title | **BLOCKED** |
| Link to the video | Always rendered when a video was found | Production game pages | `youtube.com/watch?v=…` on pages with a video | **VERIFIED** |
| Shown on the game card | The section appears only when a video exists | Production | The section, its thumbnail, duration and channel | **VERIFIED** |

**On the transcript.** yt-dlp is offered only an HLS (m3u8) caption track for these videos,
which is a manifest of URLs rather than speech; a PO token would be required for the real
one. The degradation is deliberate and visible: the video, its channel, duration and link
are shown, with "Прохождение нашлось, но прочитать субтитры не удалось" and no AI text at
all. A hosted transcript API would close it; that is a paid dependency and not a code
change.

## Additional part 2 — real-time monitoring

| Assignment | Implementation | How it was checked | Evidence | Status |
|---|---|---|---|---|
| Worker status | Heartbeat rows, alive within 120s | Production page | Live workers show "на связи"; ones that stopped show "не отвечает" and are pruned after six hours | **VERIFIED** |
| Scheduler status | Last fired slot, next due | Production | `next_run_at`, `last_run_at`, cadence | **VERIFIED** |
| Current run | The claimed job with its stage | Production | `current_job` | **VERIFIED** |
| Games processed | 24h counters from crawl runs | Production | `games_processed = 220` | **VERIFIED** |
| Successes and failures | Same source | Production | `succeeded = 220, failed = 0, success_rate = 100%` | **VERIFIED** |
| AI activity | Calls, spend, ceiling, claim acceptance | Production | 318 calls, $4.38 of $5, 91.6% acceptance | **VERIFIED** |
| Errors | Problem log, redacted through the same filter as the logs | Production | Recent problems section | **VERIFIED** |
| Queue | Depth per queue and status | Production | Queue table | **VERIFIED** |
| Last execution | Run history with duration and result | Production | Run history table | **VERIFIED** |
| **Run now** | `POST /api/v1/admin/crawl/run`, token-protected | Pressed against production | Enqueues a real crawl tick | **VERIFIED** |
| Live updating | SSE from an append-only event log; `job_events.id` is the `Last-Event-ID` | Real uvicorn on a real socket: framing, ordering, replay after disconnect | The page's stream indicator reads "Live" in a browser | **VERIFIED** |

## Web UI

| Assignment | How it was checked | Status |
|---|---|---|
| Game list with brief information | Production catalogue, 219 real games | **VERIFIED** |
| Game page with full information | Production | **VERIFIED** |
| Platform filter | Production, with live facet counts | **VERIFIED** |
| Search by title | Production, including a query that matches nothing | **VERIFIED** |
| Sorting by rating | Production; unrated never floats to the top in either direction | **VERIFIED** |
| Similar games | 12 tests; production shows them with reasons where a reason is real | **VERIFIED** |
| Navigating to a similar game | Every suggestion is a game already indexed, so every link opens a real page | **VERIFIED** |

## Responsive and UX

Real Chromium against the live site at 1440, 1280, 768 and 390: every page 200, no
horizontal overflow at any width, no console errors, the SSE stream live. Edge cases —
long titles, many platforms, a missing cover, `userscore = 0`, `userscore = null`, no
reviews, no similar games, no YouTube, an empty search — are covered by 44 tests against
`design/EDGE_CASES.md` and were checked in the browser where production has an example.

**VERIFIED**, with one caveat worth stating: the browser pass was run from a machine
whose Chromium crashed twice under memory pressure, so the last runs were narrowed to the
hero, the catalogue and a game page at 1440 and 390 rather than all twelve pages at four
widths.

## Production

| | Status |
|---|---|
| Public HTTPS URL | **VERIFIED** — Let's Encrypt via the existing Caddy |
| PostgreSQL 16 | **VERIFIED** — 362 integration tests against a real server, and the production database |
| Worker | **VERIFIED** |
| Scheduler | **VERIFIED** — 11 runs on the hour |
| Docker | **VERIFIED** — four containers, built and running |
| CI | **VERIFIED** — 5 jobs green including integration on PostgreSQL 16 |
| Restart recovery | **VERIFIED** — full `down`/`up`: data intact, all four containers back |
| Backups | **VERIFIED** — nightly `pg_dump`, 14 kept. **Same disk as the database**, so not disaster recovery |
| Secrets | **VERIFIED** — 624 blobs across the whole history scanned for 8 secret classes; nothing found |

---

## Live evidence, as of this pass

```
games            219        reviews         9,859
with metascore   219        with summaries  143
crawl runs 24h   11         processed       220      failed 0      duplicates skipped 21
AI               318 calls, $4.38 of $5/day, 91.6% claim acceptance
YouTube          2,285 videos ranked, 180 discovery runs
tests            974 passed, 13 skipped
```

## What is not VERIFIED, and why

1. **YouTube transcripts.** The chain from discovery to transcript was only joined during
   this pass, and yt-dlp is offered an m3u8 caption track rather than speech. A hosted
   transcript provider is a paid dependency.
2. **The AI conclusion from a transcript.** Blocked behind the above, deliberately: no
   transcript means no conclusion.
3. **Accessibility.** Landmarks, labels, a skip link and `aria-current` are present; no
   axe audit has been run (OQ-N3).
4. **Off-site backups.** The dumps sit beside the database. Somewhere to put them needs a
   credential that does not exist.
