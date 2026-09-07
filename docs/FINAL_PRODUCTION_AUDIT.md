# Final production audit

**Repository:** <https://github.com/CRYCas0n/playlens> — public
**Service:** <https://playlens.45.67.202.162.sslip.io>

Row-by-row acceptance against the original assignment is in
[`FINAL_TZ_ACCEPTANCE.md`](FINAL_TZ_ACCEPTANCE.md); this file is the production side of
it.

## Verified against the running service

| | |
|---|---|
| Public HTTPS | Let's Encrypt through the Caddy that already served the host. No new port opened |
| Ingestion | 12 crawl runs in 24h at seven past the hour, 240 games processed, 0 failed, 21 duplicates refused by a unique constraint |
| Catalogue | 239 games, 10,788 reviews, 155 with AI summaries |
| AI | 346 live calls, 91.6% claim acceptance, spend held under a $5/day ceiling that *defers* work rather than dropping it |
| YouTube | 200 discoveries, 2,285 videos ranked, real transcripts of 24,541 / 18,422 / 142,542 characters, three spoiler-free conclusions written from the video |
| Monitoring | Worker and scheduler state, queue, counters, AI spend, problem log, Run Now, live over SSE |
| Restart | Full `restart` and a full `down`/`up`: four containers back, data intact |
| Smoke | 22/22 over the public name, reading bodies rather than status lines |
| Secrets | 624 blobs across the whole git history scanned for 8 secret classes. Nothing found. Secret scanning and push protection enabled on the repository |
| Tests | 986 passed, 13 skipped; ruff clean; CI green on 5 jobs including integration against PostgreSQL 16 |

## The one thing a reviewer should know

The service was deployed onto a server that was already running a live site, a Caddy and
an n8n. It added four containers and one site block, opened no port, installed no
service, and touched nothing it did not create. The removal procedure is written down in
`deploy/README.md` and leaves the machine exactly as it was.

## Not verified

| | Why |
|---|---|
| Accessibility audit | Landmarks, labels, a skip link and `aria-current` are present; no axe run (OQ-N3) |
| Off-site backups | Nightly `pg_dump` with retention, on the same disk as the database. Somewhere else to put them needs a credential that does not exist |
| The legal position | The service reads Metacritic through an interface Metacritic publishes for its own site, at two requests a second, with an identifying User-Agent, review text never republished in full and a not-affiliated notice on every page. Whether that is acceptable on a public address is a question about risk, not code (OQ-B3) |

