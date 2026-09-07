# Final production audit

**Date:** 2026-09-07 · **Repository:** <https://github.com/CRYCas0n/playlens>

This audit was written by running things. Where something could not be run, the reason is
a command that failed and its output, not a judgement.

The full evidence table is `FINAL_PRODUCTION_GAP.md`. This document is the argument.

---

## 1. What changed since the previous audit

The previous audit claimed 749 green tests, a working OpenAI provider, YouTube enabled,
and a project that "runs locally". Every one of those was true. Three of them were
incomplete in ways that only running the real thing could reveal.

| Previously | Now | How the gap showed |
|---|---|---|
| PostgreSQL: **BLOCKED**, no disk | **VERIFIED** — 362 integration tests on a real 16.4 server | 8.4 GB had become free. The previous "blocked" was accurate when written and stale by the time it was read |
| Release 0 on **6 games**, PV1 87.2% | **20 games**, PV1 **78.2%** — below target | The small sample flattered the model. The full run is the honest number |
| YouTube "enabled" | Discovery **VERIFIED** live; transcripts **BLOCKED** | Running it found that yt-dlp offers only an m3u8 track for the selected video |
| No repository | `github.com/CRYCas0n/playlens`, 271 files, 3 commits | `gh` was authenticated the whole time |

The pattern is the one ADR-021 named: a status written honestly becomes false as the
environment changes, and the only cure is to re-check rather than re-read.

---

## 2. Six defects found, all by running rather than reading

### 2.1 The cron role would have failed silently, every hour

`render.yaml` declares a cron service. The container entrypoint switched on `ROLE` and
knew `api`, `worker`, `scheduler` — anything else hit `exit 64`. The scheduled crawl
would have failed hourly with no page to show it.

Found by a test that reads the blueprint and checks each declared role against the
entrypoint. **The fix is the more general behaviour**: an explicit command now wins over
`ROLE`, which is also what `docker compose run` expects.

### 2.2 A hardcoded port would have failed the health check

The entrypoint pinned `--port 8000`. Render, Railway, Heroku and Cloud Run all set `$PORT`
and route to it. The health check would never have connected and the first deploy would
have rolled back with "no open ports detected" — a message that says nothing about ports
you control.

Both of these were found **before** any deployment, by tests over files. That is the
argument for testing configuration as code rather than treating it as documentation.

### 2.3 An m3u8 manifest passed as a transcript

The worst of the six. On a real Elden Ring playthrough, yt-dlp offered an HLS caption
track. The download was 3,791 characters — comfortably past the 2,000-character minimum —
of this:

```
#EXTM3U
#EXT-X-VERSION:3
https://www.youtube.com/api/timedtext?caps=asr&v=...&expire=...
```

A list of URLs. It would have gone to the model as speech, and the model would have
written an "impression of how the game plays" from it.

This is precisely the failure ADR-016 was written to prevent, arriving through a door the
ADR did not anticipate: not an empty response, but a full one of the wrong kind. The
guard was length; length was the wrong question.

Two fixes: only formats we can actually parse are offered (the `formats[0]` fallback is
gone), and a structural check rejects anything that is a manifest or is mostly URLs by
weight.

### 2.4 The command line specified by an ADR did not exist

`ADR-014` specifies the seed catalogue as `python -m app.cli seed --limit 500`.
`FINAL_SPEC §20` uses the same entry point. `IMPLEMENTATION_PLAN` phase 5 makes
`crawl --once` its definition of done.

None of it existed. The only way to trigger anything was curl with an admin token.

`app/cli.py` is now that command line — `status`, `crawl`, `seed`, `summarise`, `purge` —
and every subcommand calls the same service the worker calls, so nothing in it can behave
differently from production.

### 2.5 The default model missed the quality target

Six games said gpt-4o-mini scored 87.2% claim-level. Twenty games said 78.2%, below the
80% target set by Release 0. gpt-4o scored 85.7% on the same corpus for roughly a cent
more per game, against a $10 daily ceiling.

The default changed. The more interesting finding is about method: **a six-game sample was
not enough to make a model decision, and it looked like it was.**

### 2.6 One route had no test

`/games/fragment` — the partial that filters the catalogue without a page reload. One of
26 routes, and the only one nothing touched.

---

## 3. What the audit could not fault

Where I went looking and found nothing wrong, which is worth recording so the search is
not repeated:

- **No TODO, FIXME, XXX or HACK** anywhere in `app/`, `tests/`, `scripts/`, `migrations/`.
- **No swallowed exceptions.** An AST pass found three broad handlers; all three fall back
  to a defined value with a stated reason. One was improved anyway — the health check now
  records the error *class* so an operator knows what kind of down it is, while still
  never putting a connection string in a response.
- **No hardcoded production values, no localhost in `app/`, no debug endpoints.**
- **No unused dependencies.** All nine runtime packages are reachable from the code.
- **No secrets anywhere.** The git index was scanned before the first commit; the pattern
  set covers OpenAI, Anthropic, Google, GitHub tokens and private keys. Zero hits.
- **No documentation drift in the canonical documents** after the `app.cli` fix.
  `BLUEPRINT.md` still describes Celery and a separate frontend, and that is correct: it
  is the historical record that ADR-003 and ADR-017 superseded, and `DOCUMENT_MAP.md`
  says it is not rewritten.

---

## 4. The parts that are load-bearing, and how they were checked

| Claim the product makes | How it was checked |
|---|---|
| A score of 0 from two ratings is absence, not zero | The rule exists twice — Python and SQL — and a test proves them identical over a matrix of `(value, count)` pairs, on both dialects |
| The verdict cannot contradict the numbers | It is arithmetic over the two scores; no model output reaches it |
| No claim without evidence reaches a page | 42 rejections on real data across 6 classes; 7 invented references caught and none published |
| No fabricated criticism | Empty negative lists publish as empty. Elden Ring's 86-positive-0-negative corpus is a fixture precisely because it is the shape that tempts a model |
| Two workers cannot process the same game | Partial unique index plus `FOR UPDATE SKIP LOCKED`, proved with real threads on real PostgreSQL |
| A source change stops the crawl rather than filling it with blanks | Consecutive-failure counter in the append-only event log; survives worker restarts; alerts once per incident |
| Review text cannot instruct the model | Sanitised, fenced as data, and — the actual defence — a claim still needs references that resolve |

---

## 5. What remains, honestly

**Three things need you.** None is a code problem.

1. **CI is written and cannot be pushed.** GitHub refuses OAuth pushes to
   `.github/workflows/` without the `workflow` scope, and granting it is a browser flow.
   One command: `gh auth refresh -s workflow`.
2. **There is no public URL** because there is no hosting account, and creating one needs
   an email confirmation and an OAuth grant that only the account owner can give.
   `render.yaml` describes the whole stack; applying it is ten minutes.
3. **The legal question** about running this publicly is not technical and not mine.

**Two things are blocked by the machine.** Docker cannot be installed without
administrator rights and a reboot. YouTube transcripts need a proof-of-origin path or a
paid provider; the degradation without them is verified and honest — the video and its
details are shown, and no AI text is written at all.

**One thing has no substitute.** Whether the summaries are *useful* rather than merely
*checkable* needs a person who did not write them. `HUMAN_EVALUATION.md` is twenty
minutes. It is the only measurement in this project nobody has taken.

---

## 6. Assessment

What this pass demonstrates, more than any individual fix: **the checks that found
defects were the ones that ran real things.** 749 tests over files passed while the cron
role was broken, the port was wrong, a manifest could pass as speech, and a command line
specified by an ADR did not exist. Static tests over configuration found the first two;
one live API call found the third; a script that reads documents and checks the paths in
them found the fourth.

The corresponding weakness: I keep finding this class of defect one environment at a time.
PostgreSQL was "blocked" until the disk freed and it took twenty minutes. Chromium was
"unavailable" until I looked. The rule from ADR-021 — check the environment, do not assume
it — has now paid for itself twice, and I only wrote it after being wrong the first time.
