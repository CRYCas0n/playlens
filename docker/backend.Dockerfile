# One image for all three processes. The API, the worker and the scheduler share every
# dependency and every line of domain code; three images would only guarantee that they
# eventually drift apart. The command decides the role.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is here for the container healthcheck and nothing else; build tools are installed,
# used and removed in one layer so they are not in the shipped image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first: application code changes far more often than pyproject.toml, and
# this ordering is what keeps a code-only rebuild off the network.
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip \
    && pip install "psycopg[binary]>=3.2" \
    && python - <<'PY'
import tomllib, subprocess
with open("pyproject.toml", "rb") as fh:
    deps = tomllib.load(fh)["project"]["dependencies"]
subprocess.check_call(["pip", "install", *deps])
PY

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Non-root. The application writes only to its cache directories, which are volumes.
RUN useradd --create-home --uid 10001 playlens \
    && mkdir -p /app/.cache/img /app/.cache/http \
    && chown -R playlens:playlens /app
USER playlens

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
