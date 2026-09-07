#!/bin/sh
# Migrations run in ONE place: here, in the api container, before it serves.
#
# Running them from every process would have three containers racing to take Alembic's
# lock on a cold start, and a worker that loses the race would crash-loop against a
# half-built schema. The worker and the scheduler wait for the api's healthcheck instead.
set -eu

case "${ROLE:-api}" in
  api)
    echo "running migrations"
    python -m alembic upgrade head
    exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
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
