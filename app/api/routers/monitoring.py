"""Monitoring: read is open, mutation is not (ADR-019 section 3).

Reading is open because the assignment requires the web UI to show pipeline state and
because there are no secrets in it — error messages pass through the same redactor as the
logs. Every mutating endpoint requires the admin token, because an unauthenticated
"Run now" points our crawler at Metacritic and our budget at the LLM.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import container_dep, require_admin, settings_dep, uow_dep
from app.config import Settings
from app.container import Container
from app.db.uow import UnitOfWork
from app.domain.enums import RunTrigger
from app.services.monitoring_service import MonitoringService

router = APIRouter(tags=["monitoring"])

_sse_clients = 0


class RunNowRequest(BaseModel):
    max_games: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Lower the per-run ceiling for this run. It can never RAISE it: the "
            "configured maximum is a promise to the source, not a suggestion."
        ),
    )


@router.get("/monitoring/status")
def status(
    uow: UnitOfWork = Depends(uow_dep), settings: Settings = Depends(settings_dep)
) -> dict[str, Any]:
    return MonitoringService(settings).status(uow).as_dict()


@router.get("/monitoring/runs")
def runs(limit: int = Query(20, ge=1, le=100), uow: UnitOfWork = Depends(uow_dep)) -> dict:
    return {
        "items": [
            {
                "id": r.id,
                "crawl_date": r.crawl_date,
                "trigger": r.trigger,
                "triggered_by": r.triggered_by,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "duration_ms": r.duration_ms,
                "discovered": r.games_discovered,
                "claimed": r.games_claimed,
                "skipped_duplicates": r.games_skipped_dupe,
                "failed": r.games_failed,
                "error_class": r.error_class,
                "source_params": r.source_params,
            }
            for r in uow.crawl.recent_runs(limit=limit)
        ]
    }


@router.get("/monitoring/runs/{run_id}")
def run_detail(run_id: int, uow: UnitOfWork = Depends(uow_dep)) -> dict:
    runs_ = {r.id: r for r in uow.crawl.recent_runs(limit=200)}
    run = runs_.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "items": uow.crawl.items_for_run(run_id),
        "events": [
            {"ts": e.ts, "event": e.event, "level": e.level, "message": e.message,
             "stage": e.stage, "data": e.data}
            for e in uow.events.recent(limit=100, crawl_run_id=run_id)
        ],
    }


@router.get("/monitoring/jobs")
def jobs(
    status_filter: str | None = Query(None, alias="status"),
    job_type: str | None = None,
    game_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    uow: UnitOfWork = Depends(uow_dep),
) -> dict:
    return {
        "items": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "queue": j.queue,
                "status": j.status,
                "attempts": j.attempts,
                "max_attempts": j.max_attempts,
                "game_id": j.game_id,
                "queued_at": j.queued_at,
                "finished_at": j.finished_at,
                "duration_ms": j.duration_ms,
                "error_class": j.error_class,
                "error_message": j.error_message,
            }
            for j in uow.jobs.recent(
                limit=limit, status=status_filter, job_type=job_type, game_id=game_id
            )
        ]
    }


@router.get("/monitoring/events")
def events(
    level: str | None = Query(None, description="'problems' for warnings and errors"),
    crawl_run_id: int | None = None,
    game_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    uow: UnitOfWork = Depends(uow_dep),
) -> dict:
    return {
        "items": [
            {
                "id": e.id,
                "ts": e.ts,
                "level": e.level,
                "event": e.event,
                "stage": e.stage,
                "message": e.message,
                "game_id": e.game_id,
                "job_id": e.job_id,
                "crawl_run_id": e.crawl_run_id,
                "data": e.data,
            }
            for e in uow.events.recent(
                limit=limit, level=level, crawl_run_id=crawl_run_id, game_id=game_id
            )
        ]
    }


def _sse(event: str, data: dict, *, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, default=str)}")
    return "\n".join(lines) + "\n\n"


@router.get("/monitoring/stream")
async def stream(
    request: Request,
    run_id: int | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    container: Container = Depends(container_dep),
    settings: Settings = Depends(settings_dep),
) -> StreamingResponse:
    """Server-sent events read straight from the durable event log (ADR-013).

    ``job_events.id`` IS the ``Last-Event-ID``, so a client that reconnects after a minute
    receives everything it missed — not merely whatever was broadcast while it was away,
    which is what a pub/sub transport would give.
    """
    global _sse_clients
    if _sse_clients >= settings.sse_max_clients:
        raise HTTPException(status_code=503, detail="Too many open event streams.")

    try:
        cursor = int(last_event_id) if last_event_id else -1
    except ValueError:
        cursor = -1

    async def generator() -> AsyncIterator[str]:
        global _sse_clients
        _sse_clients += 1
        position = cursor
        try:
            with container.uow() as uow:
                latest = uow.events.latest_id()
                if position < 0:
                    position = latest
                elif latest - position > settings.sse_max_backlog:
                    # Too far behind to replay: send a fresh snapshot and move on.
                    position = latest
                yield _sse(
                    "snapshot",
                    MonitoringService(settings).status(uow).as_dict(),
                    event_id=position,
                )

            heartbeat_due = dt.datetime.now(dt.UTC)
            while not await request.is_disconnected():
                with container.uow() as uow:
                    batch = uow.events.after(position, limit=200, crawl_run_id=run_id)
                    for row in batch:
                        position = row.id
                        yield _sse(
                            row.event,
                            {
                                "id": row.id,
                                "ts": row.ts.isoformat(),
                                "level": row.level,
                                "event": row.event,
                                "stage": row.stage,
                                "message": row.message,
                                "game_id": row.game_id,
                                "crawl_run_id": row.crawl_run_id,
                                "data": row.data,
                            },
                            event_id=row.id,
                        )
                now = dt.datetime.now(dt.UTC)
                if now >= heartbeat_due:
                    # Keeps proxies from closing an idle connection.
                    yield ": ping\n\n"
                    heartbeat_due = now + dt.timedelta(seconds=settings.sse_heartbeat_s)
                await asyncio.sleep(settings.sse_poll_interval_ms / 1000)
        finally:
            _sse_clients -= 1

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- admin

admin_router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


@admin_router.post("/admin/crawl/run", status_code=202)
def run_now(
    payload: RunNowRequest | None = None,
    uow: UnitOfWork = Depends(uow_dep),
    container: Container = Depends(container_dep),
) -> dict:
    """Queue a crawl. Never runs it inline — a request must not hold a crawl open."""
    active = uow.crawl.active_run()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A crawl run is already in progress (run {active.id}, started "
                f"{active.started_at.isoformat()}). Wait for it to finish."
            ),
        )
    now = dt.datetime.now(dt.UTC)
    result = uow.jobs.enqueue(
        job_type="crawl.tick",
        idempotency_key=f"crawl.tick:manual:{now.strftime('%Y-%m-%dT%H:%M:%S')}",
        queue="crawl",
        priority=9,
        payload={
            "trigger": RunTrigger.MANUAL.value,
            "triggered_by": "admin",
            # Bounded by the configured ceiling inside the orchestrator (ADR-019 T1).
            "max_games": payload.max_games if payload else None,
        },
    )
    uow.events.emit("run.requested", data={"job_id": result.job_id, "trigger": "manual"})
    return {
        "status": "accepted",
        "job_id": result.job_id,
        "stream_url": f"{container.settings.api_prefix}/monitoring/stream",
    }


@admin_router.post("/admin/games/{slug}/resync", status_code=202)
def resync(slug: str, uow: UnitOfWork = Depends(uow_dep)) -> dict:
    game = uow.games.get_by_slug(slug)
    if game is None:
        raise HTTPException(status_code=404, detail="We don't have this game yet.")
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    job_ids = []
    for job_type, queue in (("game.sync", "enrich"), ("reviews.sync", "enrich")):
        result = uow.jobs.enqueue(
            job_type=job_type,
            idempotency_key=f"{job_type}:resync:{slug}:{now}",
            queue=queue,
            game_id=game.id,
            priority=8,
            payload={"slug": slug, "game_id": game.id},
        )
        if result.job_id:
            job_ids.append(result.job_id)
    return {"status": "accepted", "job_ids": job_ids}


@admin_router.post("/admin/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: int, uow: UnitOfWork = Depends(uow_dep)) -> dict:
    job = uow.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if job.status in {"queued", "running", "retrying"}:
        raise HTTPException(status_code=409, detail="That job has not finished yet.")
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    result = uow.jobs.enqueue(
        job_type=job.job_type,
        idempotency_key=f"{job.idempotency_key}:retry:{now}",
        queue=job.queue,
        game_id=job.game_id,
        payload=dict(job.payload or {}),
        priority=8,
    )
    return {"status": "accepted", "job_id": result.job_id}


@admin_router.post("/admin/crawl/runs/{run_id}/cancel", status_code=202)
def cancel_run(run_id: int, uow: UnitOfWork = Depends(uow_dep)) -> dict:
    from app.domain.enums import RunStatus

    active = uow.crawl.active_run()
    if active is None or active.id != run_id:
        raise HTTPException(status_code=409, detail="That run is not active.")
    uow.crawl.finish_run(
        active, status=RunStatus.CANCELLED, error=RuntimeError("cancelled by an operator")
    )
    uow.events.emit("run.cancelled", level="warning", crawl_run_id=run_id)
    return {"status": "cancelling", "run_id": run_id}
