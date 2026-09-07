# Handoff

Everything I could do on this machine is done. This is the short list of what only you
can do, in the order it makes sense to do it.

Nothing here is urgent. **The project runs right now, with no accounts and no keys.**
Steps 2 onward are for the optional extras.

---

## A. What is already finished

In plain terms:

**The website works.** Home page, catalogue with search and filters, individual game
pages, an operations dashboard. I opened all of them in a real browser at four screen
sizes — desktop down to phone — and they render correctly with no errors.

**The data pipeline works.** It fetches games from Metacritic on a schedule, stores them,
fetches their reviews, and survives being restarted mid-job. It refuses to hammer the
source: two requests a second, twenty games an hour.

**The honest bits work.** When a game has no score, the page says "Not rated" — it never
shows a zero. When two people have rated a game 0 out of 10, that counts as "not enough
ratings", not as a score of zero. When there is nothing comparable to recommend, the
section says so instead of padding itself with loosely related games.

**The AI part is built and switched off.** With no key, summaries stay in a visible
"in progress" state and everything else works. With a key, they generate — and every
sentence is checked against the reviews it claims to summarise before anyone sees it.

**It is tested.** 749 automated tests, all passing. Plus a real browser pass, a real
event-stream test over a real socket, and a live check against Metacritic itself.

---

## B. What you need to install

**Nothing, if you just want to see it running.** Python is the only requirement and you
already have it (3.11.9).

Install these only when you reach the step that needs them:

| | What it is | Needed for | When |
|---|---|---|---|
| **Docker Desktop** | Runs the whole thing — database included — in one command. | Step 4 | Only when you want the production-shaped setup |
| **Disk space** | You have **about 150 MB free**. Docker needs several GB. | Step 4 | Before Docker |

> **Your disk is nearly full.** That is the single biggest practical obstacle here. It is
> why I could not install PostgreSQL or Docker myself. Freeing a few gigabytes is worth
> doing before anything in step 4 — but I have not deleted anything of yours, and I would
> not without asking.

---

## C. What accounts you need

Only two, both optional, neither needed today:

| Account | What it unlocks | Free? |
|---|---|---|
| **OpenAI or Anthropic** | The AI review summaries. | No — pay per use. Measured at $0.0012 per game on gpt-4o-mini; capped by a setting |
| **Google Cloud** | The "See it played" YouTube section. | Yes — free tier is enough |

You need **no account at all** for the catalogue, the scores, the critic-vs-player
comparison, the recommendations or the dashboard. Those are the core of the product and
they are done.

---

## D. What keys you need

### 1. An AI key — optional, for the review summaries. **Already done.**

Either OpenAI or Anthropic works; you supplied an OpenAI key and it is configured and
tested.

- **What it is for:** writing the "What critics say" / "What players say" summaries.
- **Where it lives:** `LLM_API_KEY` in `.env` — the same variable whichever provider you
  use, so switching is one line and not a rename.
  ```
  LLM_ENABLED=true
  LLM_PROVIDER=openai       # or anthropic
  LLM_MODEL=gpt-4o-mini     # or claude-sonnet-5
  LLM_API_KEY=...
  ```
- **Measured on your key:** 6 games of the Release 0 sample, claim-level accuracy
  **87.2%** against a target of 80%, block-level 100% against 85%, total cost
  **$0.0074**.
- **How to check it worked:** restart the site, open any game page. The summary section
  changes from "Summary in progress" to actual text within a few minutes of the worker
  running.
- **Can I run without it?** Yes. Everything else works; summaries stay pending and the
  page explains why.
- **What it costs:** capped at `AI_DAILY_COST_LIMIT_USD` (default $10/day). That cap is
  enforced in code — I checked, because it previously was not.

### 2. YouTube Data API key — optional, for the Let's Play section

- **What it is for:** finding one long playthrough per game and summarising what it shows.
- **Where to get it:** <https://console.cloud.google.com> → create a project → *APIs &
  Services* → enable *YouTube Data API v3* → *Credentials* → *Create credentials* →
  *API key*.
- **Where to put it:** in `.env`:
  ```
  YOUTUBE_ENABLED=true
  YOUTUBE_API_KEY=your-key-here
  ```
- **How to check it worked:** a "See it played" section appears on popular games.
- **Can I run without it?** Yes. The section disappears entirely rather than showing an
  empty box.
- **Honest warning:** this is the least reliable part of the system, and I have said so
  from the start. YouTube makes subtitles hard to read programmatically; expect it to
  work for somewhere between a third and two thirds of videos. When it cannot read a
  video, it shows the link and the video's details and writes no AI text at all.

> **Never send me a key.** Put it in `.env` on your own machine and tell me only
> *"the key is in"*. I do not need to see it, and there is no step where I would.

---

## E. The steps, in order

### STEP 1 — See it running *(10 minutes, do this first)*

Follow **`docs/QUICKSTART.md`**. It is written out command by command.

At the end you will have the site open in your browser with nine sample games in it.

**Do this before anything else.** Everything below assumes you have seen it work.

---

### STEP 2 — Fetch real games *(5 minutes)*

Also in the Quickstart, under *"Optional — fetch real games"*. You start two more
programs and press one button, and real Metacritic games start appearing.

Worth doing because it is the difference between "the demo renders" and "the thing
actually works".

---

### STEP 3 — Turn on AI summaries *(only if you want them)*

1. Get an Anthropic key (section D above).
2. Put it in `.env`.
3. Restart the site and the worker.
4. Wait a few minutes, then open a game page.

You can also measure the summaries automatically:

```powershell
.\.venv\Scripts\python -m scripts.release0 --load-fixtures
```

That runs the same twenty games Release 0 used and prints two percentages. I have already
run it end to end with a stand-in model to prove the plumbing; with your key it becomes a
real measurement.

Then, if you have twenty minutes: **`docs/HUMAN_EVALUATION.md`**. It asks you to read
twenty summaries and score each one out of 2. That is the only way to find out whether
the summaries are actually *useful* rather than merely *checkable*, and it is the one
test I cannot write.

---

### STEP 4 — Run it the production way *(needs Docker and disk space)*

Once Docker Desktop is installed:

```powershell
cd "C:\Users\CRYCA\Claude VS Code Project\metacritic-service"
docker compose up --build -d
```

This starts a real PostgreSQL database instead of the file-based one, plus the website,
two workers and the scheduler.

**Please tell me what happens.** These files have never been built — no Docker was
available on this machine — so the first run is genuinely a test of them, and if
something fails I want to fix it rather than leave you with it.

Then run the test suite against PostgreSQL, which is the last unverified thing about the
database layer:

```powershell
$env:TEST_DATABASE_URL = "postgresql+psycopg://playlens:playlens@localhost:5432/playlens_test"
.\.venv\Scripts\python -m pytest -q
```

> You may see `make ...` mentioned in other files. **`make` is not installed on your
> computer**, and you do not need it — it is a shortcut used on Linux and in CI. Every
> command in this file and in the Quickstart is the real one, typed directly.

---

### STEP 5 — Before showing it to anyone else

One decision, and it is yours, not mine.

**The legal question.** This service reads Metacritic through an interface Metacritic
publishes for its own website rather than for other people's. Their terms discourage
automated collection. For learning, for a portfolio, for a demo on your own machine, that
is normal and low-risk. Putting it on a public address where anyone can use it is a
different question, and it is a question about your risk, not a technical one.

What I have done about it: the crawler is deliberately slow and identifies itself; review
text is never republished in full, only summarised with links back to the source; and
every page says the project is not affiliated with Metacritic.

What I have not done, because I cannot: decide whether you are comfortable with it. I am
not able to give you a legal opinion, and I have not tried to. If you want to publish
this, that is worth ten minutes with someone who can.

---

## F. What should appear at each step

Tick these off as you go:

```
STEP 1
  ✓ python --version           shows 3.11 or newer
  ✓ pip install                ends with "Successfully installed"
  ✓ alembic upgrade head       ends at revision 0003
  ✓ seed_demo                  says "demo catalogue ready: 9 games"
  ✓ uvicorn                    says "Uvicorn running on http://127.0.0.1:8000"
  ✓ browser                    a dark page with game cards

STEP 2
  ✓ worker                     says "worker.started"
  ✓ scheduler                  says "scheduler.started"
  ✓ crawl run                  returns {"status":"accepted"}
  ✓ /admin/monitoring          numbers start moving
  ✓ /games                     real game titles appear

STEP 3
  ✓ game page                  summaries replace "Summary in progress"
  ✓ /admin/monitoring          "Claim acceptance" shows a percentage

STEP 4
  ✓ docker compose up          four containers running
  ✓ localhost:8000             the same site
  ✓ pytest against PostgreSQL  749 passed
```

---

## G. What to do if something goes wrong

The Quickstart has a troubleshooting section covering the common ones. Beyond that:

| What you see | What it means | What to do |
|---|---|---|
| `bind on address` failed | The site is already running in another window | Ctrl+C there, or use `--port 8001` |
| Dashboard says **down**, "consecutive parse failures" | Metacritic changed its data format | Send me the message. This is the alarm doing its job, and the fix is code |
| Dashboard says **degraded**, "no worker" | The worker window was closed | Start it again |
| Summaries stay "in progress" forever | No AI key, or the daily cost cap is reached | Check `LLM_ENABLED` in `.env`; the dashboard shows the spend |
| `docker compose` fails | Almost certainly disk space | Check free space first; then send me the error |

---

## H. What to send me

Copy and paste the text — no screenshots needed unless something looks visually wrong.

**After Step 1:**
```
1. The last line of `pip install`
2. The output of `.\.venv\Scripts\python -m pytest -q`
3. Whether the browser page looked right
```

**After Step 2:**
```
1. The output of the crawl-run command
2. How many games are on /games after five minutes
```

**After Step 4 (Docker):**
```
1. The output of `docker compose ps`
2. Any error text, in full
3. The result of the PostgreSQL test run
```

**Never send:**

> API keys · passwords · the contents of `.env` · anything starting `sk-ant-`
>
> If an error message contains a key, replace it with `XXXX` before sending.

---

## I. The honest list of what is not proven

I would rather you hear this from me than find it out:

| Thing | Status | Why |
|---|---|---|
| PostgreSQL | **Not verified** | Could not install it — about 150 MB free disk. Everything runs on SQLite, migrations are round-trip tested there, and the code is written for both |
| Docker images | **Not verified** | No Docker on this machine. The files are checked as far as text can be checked |
| The `prod` compose file | **Not verified** | Same reason. Written carefully, never run |
| AI summary quality, machine-checkable part | **Measured** | 6 of the 20 Release 0 games on your OpenAI key: PV1 87.2%, PV2 100%. The remaining 14 are one command away |
| AI summary quality, is-it-useful part | **Not measured** | Needs a person, not a key. `docs/HUMAN_EVALUATION.md`, twenty minutes |
| YouTube | **Not verified** | Needs a key. The code and its tests exist and pass against fixtures |
| Legal position | **Your decision** | Not a technical question |

Everything else in `docs/PROJECT_STATUS.md` is marked VERIFIED, and each of those has a
test behind it that I ran.
