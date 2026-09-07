# Release 0 — final measurement

**Date:** 2026-09-07 · **Database:** PostgreSQL 16.4 · **Corpus:** the 21-game Release 0
harvest, byte-for-byte as captured

Two runs, on the same corpus, through the same pipeline, differing only in the model.
Both are real API calls, not fixtures.

---

## The headline

| | gpt-4o-mini | gpt-4o |
|---|---|---|
| Games | **20** | 8 |
| Summaries attempted | 32 | 13 |
| Summaries published | 32 | 13 |
| Claims produced | 193 | 63 |
| Claims accepted | 151 | 54 |
| **PV1 — claim-level** | **78.2%** ❌ target 80% | **85.7%** ✅ |
| **PV2 — block-level** | **100%** ✅ target 85% | **100%** ✅ |
| Total cost | $0.0387 | $0.1200 |
| Cost per game | $0.0019 | $0.0150 |

**gpt-4o-mini missed the claim-level target.** The earlier six-game sample scored 87.2%
and looked comfortable; the full twenty told a different story. That is the whole reason
the sample size mattered, and it is why this document exists rather than a repetition of
the earlier number.

**The default was changed to gpt-4o.** It clears the target on the same corpus for about
1.3 cents a game — against a $10 daily ceiling and twenty games an hour, quality wins
without the cost being close to interesting.

---

## Where the rejected claims went

All 42 rejections from the twenty-game run, by reason:

| Reason | Count | What it means |
|---|---|---|
| `rejected_aspect_unsupported` | 27 | The claim's vocabulary does not appear in the reviews it cites. The most common failure and the least alarming: the model summarised correctly and cited the wrong subset |
| `rejected_missing_ref` | 7 | **The model invented a reference.** All seven on the player side. This is the one that matters, and the validator caught every one |
| `rejected_low_support` | 4 | Fewer than three cited reviews back the claim |
| `rejected_temporal_unsupported` | 2 | "Improved since launch" without a date span in the evidence |
| `rejected_vague` | 1 | "Reviews are mixed" — a restatement of the score, not a finding |
| `rejected_quote_not_found` | 1 | A quoted phrase that appears in no cited review |

Split by audience:

| | Accepted | Rejected | Rate |
|---|---|---|---|
| Critic | 95 | 17 | 84.8% |
| Player | 56 | 25 | 69.1% |

**Player reviews are markedly harder.** They are shorter, more repetitive, and more
likely to argue with each other; the model reaches further for a generalisation and the
validator catches it. Both invented-reference cases and most aspect failures are on that
side. This is a prompt problem, not a model problem, and it is the obvious next
improvement.

---

## What was published

Thirty-two summaries, all of them clean — PV2 100% means every published summary
survived validation entirely. Nothing partial reached a page.

Eight summaries were **not** attempted at all, every one for `below_threshold`: fewer
than five critic reviews or fewer than twenty player reviews with text. That is the
design working. A game with three reviews gets a stated absence, not a confident
paragraph.

---

## The rules that were exercised, and held

| Rule | Evidence from this run |
|---|---|
| A claim without enough cited reviews is discarded | 4 `rejected_low_support` |
| An invented reference is caught | 7 `rejected_missing_ref`, none published |
| Temporal claims need a date span in the evidence | 2 rejected |
| Vagueness is not a finding | 1 rejected |
| An empty negative list is a valid summary | Published, no fabricated criticism |
| A thin corpus produces no summary at all | 8 skipped, reason recorded |
| Cost is recorded per call | $0.0387 total, reconciled from `summaries.cost_usd` |
| `null` is never `0` | Score columns untouched by the AI path |

---

## How to repeat it

```powershell
$env:DATABASE_URL = "postgresql+psycopg://user:pass@host:5432/playlens_r0"
python -m alembic upgrade head
python -m scripts.release0 --load-fixtures
```

`--load-fixtures` loads the harvest itself, so the measurement needs no crawl and touches
Metacritic not at all. `--dry-run` swaps in a deterministic stand-in model: it proves the
plumbing and measures nothing.

Reports land in `docs/research-fixtures/release0/rerun/` as JSON, one file per run, with
`dry_run` recorded in each so a plumbing check can never be mistaken for a measurement.

---

## What this does not say

PV1 and PV2 say the summaries are **checkable**: every published sentence cites reviews
that exist, in numbers that support it, using words those reviews used.

They say nothing about whether the summaries are **useful**. A sentence can cite five
real reviews, pass every rule, and still tell a reader nothing they did not get from the
score. That question needs a person who did not write them, and
`docs/HUMAN_EVALUATION.md` is the twenty-minute procedure for answering it. It remains
the one measurement nobody has taken.
