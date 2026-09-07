"""Health and metrics."""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Response

from app.api.deps import container_dep, settings_dep
from app.config import Settings
from app.container import Container
from app.db.readiness import MIGRATE_COMMAND, schema_exists
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["ops"])


@router.get("/health")
def health(
    response: Response,
    container: Container = Depends(container_dep),
    settings: Settings = Depends(settings_dep),
) -> dict:
    checks: dict[str, str] = {}

    try:
        with container.uow() as uow:
            uow.session.execute(sa.text("SELECT 1"))
        # Reachable is not the same as ready. SELECT 1 succeeds against a database with
        # no tables in it, which is exactly what a first run produces.
        checks["database"] = "ok" if schema_exists(container.engine) else "not_migrated"
    except Exception as exc:
        # The class, not the message: a connection string with a password in it can end
        # up in a driver's error text, and this endpoint is deliberately unauthenticated.
        checks["database"] = "down"
        checks["database_error"] = type(exc).__name__
        log.warning("health.database_down", extra={"error_class": type(exc).__name__})

    if checks["database"] == "not_migrated":
        checks["fix"] = MIGRATE_COMMAND

    checks["metacritic_circuit"] = (
        container.transport.breaker.state if "transport" in container.__dict__ else "unknown"
    )
    checks["llm"] = "enabled" if settings.llm_enabled else "disabled"
    checks["youtube"] = "enabled" if settings.youtube_enabled else "disabled"

    healthy = checks["database"] == "ok"
    response.status_code = 200 if healthy else 503
    return {
        "status": "ok" if healthy else "down",
        "version": settings.app_version,
        "checks": checks,
        "checked_at": dt.datetime.now(dt.UTC),
    }


@router.get("/metrics", response_class=Response)
def metrics(
    container: Container = Depends(container_dep), settings: Settings = Depends(settings_dep)
) -> Response:
    """Prometheus text format.

    The scraping stack is not part of the deployment (FINAL_SPEC section 21); the endpoint
    is, so wiring one up later is a compose change rather than a code change.
    """
    if not settings.metrics_enabled:
        return Response(status_code=404, content="metrics disabled")

    lines: list[str] = []

    def gauge(name: str, value: float, help_text: str, **labels: str) -> None:
        if not any(line.startswith(f"# HELP {name}") for line in lines):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}")

    with container.uow() as uow:
        stats = uow.games.stats()
        gauge("playlens_games_total", stats["games_total"], "Games in the catalogue")
        gauge(
            "playlens_games_with_summaries",
            stats["games_with_summaries"],
            "Games with at least one AI summary",
        )
        gauge("playlens_reviews_total", uow.reviews.total_count(), "Stored reviews")
        for row in uow.jobs.queue_depth():
            gauge(
                "playlens_queue_depth",
                row["count"],
                "Jobs waiting or running per queue",
                queue=row["queue"],
                status=row["status"],
            )
        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
        cost = uow.summaries.cost_since(since)
        gauge("playlens_llm_cost_usd_24h", cost["cost_usd"], "LLM spend in the last 24h")
        gauge("playlens_llm_calls_24h", cost["calls"], "LLM calls in the last 24h")
        for validation, count in uow.summaries.claim_validation_counts(since).items():
            gauge(
                "playlens_summary_claims_total",
                count,
                "Claims by validation outcome in the last 24h",
                validation=validation,
            )

    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
