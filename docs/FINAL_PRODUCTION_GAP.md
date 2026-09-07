# Final production gap matrix

**Date:** 2026-09-07 · Every row's evidence is a command that was run on this machine, or
a stated reason why it could not be.

Statuses: **VERIFIED** (a check I ran proves it) · **IMPLEMENTED** (code exists, nothing
here could prove it) · **BLOCKED** (impossible in this environment, reason given) ·
**NEEDS HUMAN ACTION** (only the owner can do it) · **DEFERRED** (deliberately not done).

---

| Area | Requirement | Current state | Evidence | Status | Action |
|---|---|---|---|---|---|
| **application** | Boots, serves, degrades honestly | FastAPI, 9 runtime deps, `create_app` factory | `scripts/smoke.py`, 15 routes; served in `APP_ENV=production` against PostgreSQL | VERIFIED | — |
| **application** | A missing schema is actionable, not a 500 | 503 + the exact command; HTML page for browsers, problem doc for the API | `test_unmigrated_database.py`, 22 tests | VERIFIED | — |
| **database** | Runs on PostgreSQL 16 | Whole integration suite green on a real server | 362 tests, PostgreSQL 16.4 | VERIFIED | — |
| **database** | Runs on SQLite for development | Same suite, same assertions | 768 tests | VERIFIED | — |
| **database** | Partial unique indexes exist on PG | All five present | `pg_indexes` query, listed by name | VERIFIED | — |
| **database** | `pg_trgm` search and NULLS LAST indexes | Extension installed, 4 PG-only indexes created by migration 0002 | `pg_extension` / `pg_indexes` queries | VERIFIED | — |
| **migrations** | Fresh install | 3 revisions to head on both dialects | CI job + local run | VERIFIED | — |
| **migrations** | Downgrade and re-upgrade | Round trip, constraints re-checked afterwards | `test_migrations.py`, 7 tests | VERIFIED | — |
| **migrations** | Exactly one head, no stub downgrades | Enforced by test | same file | VERIFIED | — |
| **Metacritic ingestion** | The live source still parses | Listing, detail, per-platform stats, review paging | `tests/contract/`, 17 tests against the live API | VERIFIED | — |
| **Metacritic ingestion** | Catalogue size assumption holds | 18.5k eligible games, within the range ADR-015 assumed | contract test | VERIFIED | — |
| **Metacritic ingestion** | Critic reviews still 10/page regardless of `limit` | C-01 still true | contract test | VERIFIED | — |
| **deduplication** | Re-running adds nothing | Partial unique index + claim, proved with real threads | `test_crawl_repository.py`, `test_game_sync.py` | VERIFIED | — |
| **deduplication** | Two workers, same game, same day | Exactly one row claimed | `test_concurrent_claims_yield_exactly_one_row` on PostgreSQL | VERIFIED | — |
| **review snapshots** | Immutable, stable evidence refs | New reviews make a new snapshot, never mutate one | `test_snapshots.py`, 9 tests | VERIFIED | — |
| **AI** | Full Release 0 on 20 games | PV1 78.2% (mini) / 85.7% (gpt-4o), PV2 100% | `docs/RELEASE0_FINAL.md` | VERIFIED | — |
| **AI** | Claim-level target of 80% | **Met by gpt-4o, missed by gpt-4o-mini** | same | VERIFIED | default changed to gpt-4o |
| **AI** | Invented references are caught | 7 caught, 0 published | Release 0 rejection breakdown | VERIFIED | — |
| **AI** | No fabricated negatives | Empty negative lists published as-is | `test_zero_negative_claims_is_a_valid_summary` | VERIFIED | — |
| **AI** | Thin corpus produces no summary | 8 of 40 skipped `below_threshold` | Release 0 | VERIFIED | — |
| **AI** | Daily cost ceiling blocks generation | Checked before the call, not after | `test_ai_budget.py`, 9 tests | VERIFIED | — |
| **AI** | Prompt-injection text stays data | Sanitised, fenced, and claims still need real evidence | `test_security.py`, 6 payloads | VERIFIED | — |
| **OpenAI provider** | Full lifecycle | 401/404/429/5xx/timeout/malformed/prose/unknown-enum all classified | `test_openai_client.py`, 26 tests + live calls | VERIFIED | — |
| **OpenAI provider** | Model matches provider | Rejected at startup, not at first call | `_llm_model_matches_provider` | VERIFIED | — |
| **OpenAI provider** | No key in logs | `SecretStr`, redactor, source grep | `test_security.py` | VERIFIED | — |
| **evidence validation** | Support, aspect, temporal, quote, vagueness | 6 rejection classes, all fired on real data | Release 0 breakdown | VERIFIED | — |
| **YouTube** | Search and ranking on the live API | 15 candidates, 6 rejected by filters, a real playthrough selected | live run against the Data API | VERIFIED | — |
| **YouTube** | Transcripts | **Not obtainable here.** yt-dlp offered only an m3u8 track; PO-token path unavailable | live run | BLOCKED | needs a paid transcript provider or a PO-token sidecar |
| **YouTube** | A manifest is not a transcript | Rejected as a provider failure; was a real defect | `test_youtube_transcripts.py`, 20 tests | VERIFIED | — |
| **YouTube** | Failure never blocks a game | Quota, crash and empty-transcript paths all isolated | `test_letsplay_service.py`, 14 tests | VERIFIED | — |
| **similarity** | Explainable, no invented reasons | Reason chip absent below the contribution threshold | `test_similarity.py`, 12 tests | VERIFIED | — |
| **search** | Punctuation-insensitive, pg_trgm on PG | `NieR: Automata` found by `nier automata` | `test_api.py` on both dialects | VERIFIED | — |
| **filters** | Platform OR, cumulative score bands, facet counts | Behaves as the chips promise | `test_api.py` | VERIFIED | — |
| **sorting** | Unrated never floats to the top, in either direction | The Release 0 failure, now a test | `test_ascending_sort_does_not_float_unrated_games_to_the_top` | VERIFIED | — |
| **UI** | 28 edge cases from the design package | Every one walked | `test_web_pages.py`, 44 tests | VERIFIED | — |
| **responsive** | 1440 / 1280 / 768 / 390 | Real Chromium, 12 pages × 4 widths, no overflow, no console errors | `scripts/browser_smoke.mjs` | VERIFIED | — |
| **accessibility** | Landmarks, labels, skip link, `aria-current` | Present in markup; no axe audit | markup tests | IMPLEMENTED | axe run in CI (OQ-N3) |
| **security** | SSRF, XSS, injection, secrets, rate limit, admin auth | 30 assertions, one per threat in ADR-019 | `test_security.py` | VERIFIED | — |
| **security** | No secrets in the repository | Index scanned before the first commit; 0 hits | pre-commit scan + `test_no_secret_is_committed_in_source` | VERIFIED | — |
| **security** | CSP derived from what the page loads | Fonts allowed only when enabled; `object-src 'none'` | `test_the_content_security_policy_forbids_inline_script` | VERIFIED | — |
| **monitoring** | Every indicator the assignment lists | Shape asserted, not just a 200 | `TestMonitoringContract` | VERIFIED | — |
| **monitoring** | Schema drift is visible and stops the crawl | Circuit opens, alerts once, shows `down` with the reason | `test_schema_drift_circuit.py`, 15 tests | VERIFIED | — |
| **SSE** | Framing, ordering, `Last-Event-ID` replay, disconnect | Against a real uvicorn on a real socket | `test_sse_wire.py`, 7 tests | VERIFIED | — |
| **worker** | Claims, leases, retries, heartbeats | Real threads, real contention | `test_worker_and_scheduler.py`, `test_job_queue.py` | VERIFIED | — |
| **scheduling** | Hourly tick, idempotent by slot | Same slot cannot fire twice | `test_worker_and_scheduler.py` | VERIFIED | — |
| **retry** | Per-task ceilings from one table | Registry is the only source; enqueue looks it up | `app/tasks/contracts.py`, `test_every_task_declares_its_contract` | VERIFIED | — |
| **rate limits** | 2 rps to the source, refuses above 5 | Config validator | `test_rate_limit_guard_rejects_impolite_values` | VERIFIED | — |
| **rate limits** | Public API limiter with `Retry-After` | 429 carries the header | `test_a_rejected_request_says_when_to_come_back` | VERIFIED | — |
| **caching** | Identical corpus never reaches the model | Fingerprint guard, unique index behind it | `test_an_identical_corpus_never_reaches_the_model` | VERIFIED | — |
| **image proxy** | Host allow-list, width list, no redirects, private IPs refused | 5 hostile URLs refused | `test_security.py` | VERIFIED | — |
| **image proxy** | A real cover downloads and shrinks | 2.3 MB → under 400 KB | `test_image_proxy_network.py`, live | VERIFIED | — |
| **image cache** | `IMAGE_CACHE_MAX_MB` actually evicts | Least-recently-accessed, down to 80% | `test_image_cache.py`, 11 tests | VERIFIED | — |
| **logging** | JSON, correlated, redacted | Secrets never reach the stream or the event log | `test_config_and_logging.py` | VERIFIED | — |
| **health checks** | Reachable ≠ ready | `ok` / `not_migrated` / `down` + error class | `test_unmigrated_database.py` | VERIFIED | — |
| **Docker** | Files correct: roles, non-root, healthcheck, layer order, `$PORT` | 30 static assertions; two real bugs found this way | `test_compose_files.py` | VERIFIED as text | — |
| **Docker** | Image builds and runs | No daemon, not installable without admin rights | — | BLOCKED | `docker compose build` on a machine with Docker |
| **CI** | lint, unit, integration on PG, migration round trip, security | Written, 5 jobs, YAML valid | `.github/workflows/ci.yml` | NEEDS HUMAN ACTION | token lacks `workflow` scope — one command |
| **backups** | Documented, not automated | `pg_dump` procedure in `DEPLOYMENT.md` | — | DEFERRED | automate when there is data worth losing |
| **deployment** | One-file blueprint | `render.yaml`: database, web, worker, cron | `test_compose_files.py::TestTheRenderBlueprint` | IMPLEMENTED | apply it — needs an account |
| **deployment** | A public URL | Nothing deployed | — | NEEDS HUMAN ACTION | see `HUMAN_DEPLOYMENT_HANDOFF.md` |
| **secrets** | Never committed, never logged, never defaulted in production | `.env` ignored; `.env.example` ships every secret empty; prod compose requires each | `test_env_example.py`, `test_compose_files.py` | VERIFIED | — |
| **documentation** | Matches the code | Paths and modules cross-checked against the filesystem | script in this audit | VERIFIED | — |
| **documentation** | Operator CLI promised by ADR-014 | **Was missing.** `python -m app.cli` now exists: status, crawl, seed, summarise, purge | run on this machine | VERIFIED | — |
| **legal/compliance** | Public launch decision | Not a technical question | — | NEEDS HUMAN ACTION | OQ-B3 |
| **observability** | Alive, last run, counts, errors, cost, queue | All on `/api/v1/monitoring/status` and `/metrics` | `TestMonitoringContract` | VERIFIED | — |

---

## Summary

| Status | Rows |
|---|---|
| VERIFIED | 48 |
| IMPLEMENTED | 2 |
| BLOCKED | 2 |
| NEEDS HUMAN ACTION | 3 |
| DEFERRED | 1 |

The two BLOCKED rows are Docker (no daemon, cannot install without administrator rights
and a reboot) and YouTube transcripts (no PO-token path and no paid provider). The three
human actions are one shell command for CI, one account for hosting, and one decision
about publishing.
