#!/bin/sh
# Migrations run in ONE place: here, in the api container, before it serves.
#
# Running them from every process would have three containers racing to take Alembic's
# lock on a cold start, and a worker that loses the race would crash-loop against a
# half-built schema. The worker and the scheduler wait for the api's healthcheck instead.
set -eu

# An explicit command wins over ROLE. Render's cron jobs and `docker compose run` both
# pass one, and without this branch they hit the unknown-role case and exit 64 --
# which is how a scheduled job fails silently every hour.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

case "${ROLE:-api}" in
  api)
    echo "running migrations"
    python -m alembic upgrade head
    # The port comes from the platform. Render, Railway, Heroku and Cloud Run all set
    # $PORT and route to it; a hardcoded 8000 means the health check never connects and
    # the deploy rolls back with "no open ports detected".
    exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python -m app.queue.worker
    ;;
  scheduler)
    exec python -m app.queue.scheduler
    ;;
  *)
    echo "unknown ROLE: ${ROLE}" >&2
    exit 64
    ;;
esac
