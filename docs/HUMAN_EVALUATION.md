# Human evaluation of the AI summaries

**Time needed:** about 20 minutes. **What you need:** the site running, and a person.

---

## Why this exists

The system already checks its own summaries mechanically. Every claim has to cite real
reviews, enough of them, using words those reviews actually used. Claims that fail are
thrown away before anyone sees them, and the rejection rate is on the dashboard.

That machinery answers one question: **is this summary checkable?**

It cannot answer the other one: **is this summary any good?** A sentence can cite five
real reviews, pass every rule, and still be a bland restatement of the score that helps
nobody. Only a person can tell the difference, and it has to be a person who did not
write the summaries — which is exactly what Release 0 admitted it did not have.

You are that person. You do not need to know anything about how the system works.

---

## Before you start

The summaries only exist once an AI key is configured and a crawl has run. If the game
pages say *"Summary in progress"* or *"Nothing to summarise yet"*, there is nothing to
evaluate yet — see `docs/HANDOFF.md`, step 3.

---

## What to do

### 1. Open twenty game pages

```
http://localhost:8000/games?sort=metascore
```

Click into twenty games. Prefer games **you have actually played or know about** — that
is the whole point. If you only know five, do five; five informed judgements are worth
more than twenty guesses.

### 2. On each page, read only two blocks

Scroll to **"Critics & players"**. There are two columns:

- **What critics say**
- **What players say**

Read the paragraph and the bullet points. Ignore everything else on the page.

### 3. Score each summary

For each of the two blocks, write down one number:

| Score | Meaning |
|---|---|
| **2** | Matches what I know about this game. I would show this to a friend. |
| **1** | Not wrong, but bland. It says nothing the score did not already say. |
| **0** | Wrong, or says something the game is not like. |

That is the whole scale. Do not agonise — first impression is the right one.

### 4. Note anything that looks invented

Separately, jot down anything that made you stop. In particular:

**A claim you believe is false.** "Praised for its multiplayer" on a single-player game.
Note the game and the sentence.

**A claim that cannot come from reviews.** Anything comparing this game to another one,
or talking about how it changed over time, unless the reviews would plausibly have said
so. These are the two things the model is most likely to invent.

**A sentence that says nothing.** "The game received mixed reviews." That is not a
finding, it is a restatement of the number above it.

**Both columns saying the same thing.** The entire point is that critics and players
often disagree. If the two columns are interchangeable, something is wrong.

---

## Working out the result

Add up your scores. Divide by the number of summaries you read, then divide by 2.

```
Example: 30 summaries read, total score 47.
47 / 30 / 2 = 0.78  ->  78%
```

| Result | What it means |
|---|---|
| **80% or above** | Good. The summaries earn their place on the page. |
| **60–79%** | Usable, but the prompt needs work. Look at your "says nothing" notes first. |
| **Below 60%** | Do not ship the summaries. The pages work without them. |

**Any score of 0 for a factual error matters more than the average.** One invented claim
about a game somebody loves costs more trust than ten bland ones. If you found even one,
say so — the fix is different from the fix for blandness.

---

## What to send back

Just this, in a message:

```
Games read:        12
Summaries scored:  24
Total points:      38
Result:            79%

Wrong claims (games and sentences):
  - Ashen Veil, critics: "praised for its multiplayer" -- it is single-player
  - (or: none)

Bland summaries:   6 of 24
Both columns identical: 2 games
```

No screenshots needed. No file contents. Nothing from `.env`.

---

## What happens next

- **Wrong claims** → the validator's rules get tightened, and a test is added for that
  exact shape so it cannot come back.
- **Bland summaries** → the prompt gets rewritten and `PROMPT_VERSION_CRITIC` is bumped,
  which makes every summary regenerate.
- **Identical columns** → the two prompts have converged and need pulling apart.

Each of those is a code change, not a configuration change, so the result of this
evaluation genuinely decides what gets worked on next.
