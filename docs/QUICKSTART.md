# Quickstart

**Goal:** get the site running on your own computer and open it in a browser.

**Time:** about 10 minutes. **You need:** Windows, and nothing else installed yet.

This guide assumes you have never run a project like this before. Every command is
written out. If something goes wrong, the last section lists the errors you might see and
what to do about each one.

---

## What you are about to run

Three things, and it helps to know what they are before you start them:

| | What it is | Do I need it? |
|---|---|---|
| **The website** | The pages you look at, and the data behind them. | Yes |
| **The worker** | A background program that does the slow jobs: fetching game data, writing summaries. | Only when you want new data |
| **The database** | Where everything is stored. For now, a single file on your disk. | It creates itself |

No accounts, no API keys, no Docker. Those come later, and only for the extras.

---

## Step 1 — Install Python

**What we are doing:** Python is the language this project is written in. Your computer
needs it to run anything here.

1. Open <https://www.python.org/downloads/> in your browser.
2. Download **Python 3.11** or newer.
3. Run the installer. **Tick the box that says "Add python.exe to PATH"** before clicking
   Install. This one box saves a lot of trouble later.

**Check it worked.** Open PowerShell (press the Windows key, type `powershell`, press
Enter) and type:

```powershell
python --version
```

**You should see:** something like `Python 3.11.9`.

If you see *"python is not recognized"*, the PATH box was not ticked. Re-run the
installer, choose *Modify*, and tick it.

---

## Step 2 — Go to the project folder

**What we are doing:** telling PowerShell which folder to work in. Every command after
this one runs inside that folder.

```powershell
cd "C:\Users\CRYCA\Claude VS Code Project\metacritic-service"
```

**You should see:** the prompt now ends with `...\metacritic-service>`.

Keep this window open. Everything below happens here.

---

## Step 3 — Install what the project needs

**What we are doing:** the project uses a few libraries other people wrote. This
downloads them into a private folder inside the project, so nothing else on your computer
is touched.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
```

**You should see:** a lot of scrolling, ending with `Successfully installed ...`.

**How long:** one or two minutes.

> If the folder already contains a `.venv` folder, the first command will say so. That is
> fine — skip it and run the second one.

---

## Step 4 — Create the settings file

**What we are doing:** the project reads its settings from a file called `.env`. It is
an ordinary text file. There is a template next to it.

```powershell
Copy-Item .env.example .env
```

Now add one required setting — a password for the admin buttons. This command generates
a random one and adds it to the file:

```powershell
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"
Add-Content .env "ADMIN_TOKEN=$token"
```

**You should see:** nothing. Silence means it worked.

> **Keep `.env` to yourself.** It is the one file in this project that holds secrets.
> Never paste its contents into a chat, an issue, or an email.

---

## Step 5 — Create the database tables

> **Do not skip this one.** It is the step people skip, because the site starts fine
> without it and only fails when you open a page. If you see *"The database has no tables
> yet"* in the browser, you are here.

**What we are doing:** the database file exists but is empty. This creates the tables
the application expects — one for games, one for reviews, one for jobs, and so on.

```powershell
.\.venv\Scripts\python -m alembic upgrade head
```

**You should see:** several lines beginning `INFO [alembic.runtime.migration] Running
upgrade`, ending at `0003`.

---

## Step 6 — Put some games in it, so there is something to look at

**What we are doing:** the catalogue starts empty. This adds nine made-up games so you
can see what the site looks like. They are obviously fictional; real games arrive later,
from the crawler.

```powershell
.\.venv\Scripts\python -m scripts.seed_demo
```

**You should see:** `demo catalogue ready: 9 games`.

---

## Step 7 — Start the website

**What we are doing:** starting the program that serves the pages.

```powershell
.\.venv\Scripts\python -m uvicorn app.main:create_app --factory --port 8000
```

**You should see:** `Uvicorn running on http://127.0.0.1:8000`.

**Leave this window open.** Closing it stops the site.

---

## Step 8 — Look at it

Open your browser and go to:

```
http://localhost:8000
```

**You should see:** a dark page called Playlens, with game cards on it.

Things worth clicking:

| Address | What it is |
|---|---|
| <http://localhost:8000> | Home. The "critics and players disagree" rail is the point of the whole product. |
| <http://localhost:8000/games> | The catalogue, with search and filters. |
| <http://localhost:8000/games/ashen-veil> | A full game page. |
| <http://localhost:8000/games/quiet-harbor> | A game with no scores — note that it says so rather than showing zeros. |
| <http://localhost:8000/admin/monitoring> | The pipeline dashboard. |
| <http://localhost:8000/api/v1/docs> | The API documentation, generated from the code. |

That is the whole setup. Everything below is optional.

---

## Optional — check that everything really works

**What we are doing:** running the project's own tests. This proves the installation is
sound, and it takes a minute and a half.

Open a **second** PowerShell window (leave the site running in the first), then:

```powershell
cd "C:\Users\CRYCA\Claude VS Code Project\metacritic-service"
.\.venv\Scripts\python -m pytest -q
```

**You should see:** `749 passed, 13 skipped` or similar. The skipped ones are the tests
that talk to Metacritic over the internet; they are opt-in.

---

## Optional — fetch real games

**What we are doing:** the demo games are invented. This starts the background programs
that fetch real ones from Metacritic.

You need **two more** PowerShell windows, each in the project folder.

In the first:

```powershell
.\.venv\Scripts\python -m app.queue.worker
```

**You should see:** `worker.started`. It will sit there waiting for jobs.

In the second:

```powershell
.\.venv\Scripts\python -m app.queue.scheduler
```

**You should see:** `scheduler.started`. It queues a crawl every hour.

To fetch some games immediately rather than waiting for the hour, open a fourth
window. The simplest way is the command line: no token, no curl.

```powershell
cd "C:\Users\CRYCA\Claude VS Code Project\metacritic-service"
.\.venv\Scripts\python -m app.cli crawl
```

**You should see:** a small table ending with how many games were queued.

Or through the API, if you prefer:

```powershell
cd "C:\Users\CRYCA\Claude VS Code Project\metacritic-service"
$env:ADMIN_TOKEN = (Get-Content .env | Select-String '^ADMIN_TOKEN=').ToString().Split('=')[1]
curl.exe -X POST http://localhost:8000/api/v1/admin/crawl/run -H "X-Admin-Token: $env:ADMIN_TOKEN"
```

**You should see:** `{"status":"accepted","job_id":1,...}`.

Now watch <http://localhost:8000/admin/monitoring>. It updates by itself. Within a minute
or two, real games appear at <http://localhost:8000/games>.

> The crawler takes at most 20 games per run and makes 2 requests per second. That is
> deliberate: it is somebody else's website.

---

## Stopping everything

Press **Ctrl+C** in each PowerShell window. Nothing is left running, and the database file
stays where it is.

To start again later, you only need Step 2 and Step 7.

---

## If something goes wrong

### `python is not recognized`

Python is not installed, or the PATH box was not ticked during installation. Go back to
Step 1 and re-run the installer with *Modify* → tick *Add python.exe to PATH*.

### `No module named uvicorn` or `No module named app`

Either Step 3 did not finish, or you are in the wrong folder. Check the prompt ends with
`metacritic-service>`, then re-run Step 3.

### `[Errno 10048] error while attempting to bind on address`

Something is already using port 8000 — most likely a copy of this site you started
earlier and forgot. Either find that window and press Ctrl+C, or use a different port:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:create_app --factory --port 8001
```

Then use `http://localhost:8001` in the browser.

### `ValidationError: admin_token`

Step 4 did not add `ADMIN_TOKEN` to `.env`. Open `.env` in Notepad and check there is a
line starting `ADMIN_TOKEN=` with something after the `=`.

### The page says "The database has no tables yet"

Step 5 was skipped. Run it, then reload the page — no need to restart the site:

```powershell
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m scripts.seed_demo
```

### `The database at ... has no tables yet` when running seed_demo

Same cause, same fix: Step 5 first, then Step 6.

### The page loads but there are no games

Step 6 was skipped, or it wrote to a different database. Run it again:

```powershell
.\.venv\Scripts\python -m scripts.seed_demo
```

### Every game says "Summary in progress"

That is correct. Summaries need an AI key, which you do not have yet. Everything else on
the page works without one. See `docs/HANDOFF.md` if you want to turn them on.

### The Let's Play section is missing from every game

Also correct, and for the same reason: it needs a YouTube key. The section disappears
entirely rather than showing an empty box.

---

## What to read next

| | |
|---|---|
| `docs/HANDOFF.md` | What to do next, including the optional keys — step by step. |
| `README.md` | What the project is and why it works the way it does. |
| `docs/RUNBOOK.md` | What to do when something breaks. For later. |
