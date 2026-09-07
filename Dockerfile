# Single-container image for a free host.
#
# The repository's real image is docker/backend.Dockerfile: three processes, three
# containers, one role each. This one exists because every genuinely free platform —
# Hugging Face Spaces, Koyeb's nano tier, a 512 MB VPS — gives you exactly one container,
# and the service needs the web server, a worker and the scheduler all running.
#
# It lives at the repository root because Hugging Face Spaces looks for `Dockerfile`
# there and nowhere else.
#
# What it gives up, said plainly: one failure domain instead of three. A crash in the
# worker no longer leaves the site standing. `app/allinone.py` restarts a crashed thread
# and logs it loudly, which is a mitigation and not a fix. Use the compose files wherever
# you can run three containers.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    LOG_FORMAT=json

# Hugging Face Spaces runs the container as uid 1000 and will not run it as root.
# Creating the user here rather than relying on the platform keeps the image portable.
RUN useradd -m -u 1000 playlens

WORKDIR /home/playlens/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies before code: a one-line change should not re-download the tree.
COPY --chown=playlens:playlens pyproject.toml README.md ./
RUN pip install --upgrade pip \
    && pip install "psycopg[binary]>=3.2" "Pillow>=10.0" "yt-dlp>=2024.8" \
    && python - <<'PY'
import subprocess, tomllib
with open("pyproject.toml", "rb") as fh:
    subprocess.check_call(["pip", "install", *tomllib.load(fh)["project"]["dependencies"]])
PY

COPY --chown=playlens:playlens alembic.ini ./
COPY --chown=playlens:playlens migrations ./migrations
COPY --chown=playlens:playlens app ./app
COPY --chown=playlens:playlens scripts ./scripts

USER playlens

# 7860 is what Hugging Face Spaces expects and routes to. Anywhere that sets $PORT wins
# over it; app/allinone.py reads both.
ENV PORT=7860
EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/api/v1/health" || exit 1

# Migrations run inside this process, before it serves. There is no second process to
# race with, which is the one simplification this mode genuinely buys.
CMD ["python", "-m", "app.allinone"]
