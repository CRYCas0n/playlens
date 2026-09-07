"""Operations view.

Covers every indicator the assignment's Bonus 2 lists, plus the product metrics the
blueprint asked for on top: an operator cares less that "worker-3 processed 47 records"
than that "12% of the catalogue has no summary, so users are seeing empty cards".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.domain.enums import JobStatus, RunStatus
from app.queue.scheduler import next_run_at
from app.services.drift import consecutive_drift, drift_circuit_open

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

#: A crawl that has not succeeded for this long is not "quiet", it is broken.
STALE_AFTER_HOURS = 3
WORKER_ALIVE_SECONDS = 120


@dataclass
class MonitoringStatus:
    system: dict[str, Any]
    schedule: dict[str, Any]
    counters24h: dict[str, Any]
    current_job: dict[str, Any] | None
    stages: list[dict[str, Any]]
    workers: list[dict[str, Any]]
    queues: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    problems: list[dict[str, Any]]
    data_quality: dict[str, Any]
    ai: dict[str, Any]
    youtube: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MonitoringService:
    def __init__(self, settings: Settings, *, clock=None) -> None:
        self._settings = settings
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    def status(self, uow: UnitOfWork) -> MonitoringStatus:
        now = self._clock()
        since = now - dt.timedelta(hours=24)

        runs = uow.crawl.recent_runs(limit=20)
        active = next((r for r in runs if r.status == RunStatus.RUNNING.value), None)
        last_success = next(
            (r for r in runs if r.status in (RunStatus.SUCCEEDED.value, RunStatus.PARTIAL.value)),
            None,
        )

        workers = uow.workers.all()
        alive = [
            w
            for w in workers
            if (now - w.heartbeat_at).total_seconds() < WORKER_ALIVE_SECONDS
        ]

        queues = uow.jobs.queue_depth()
        problems = uow.events.recent(level="problems", limit=25, since=since)
        counters = self._counters(runs, since)
        quality = self._data_quality(uow)
        ai = self._ai(uow, since)

        # Asked before every other health question: a broken parser is not "degraded",
        # and the operator needs the specific message, not "12 problems in 24 hours".
        drift = drift_circuit_open(uow, self._settings.schema_drift_threshold)
        if drift is not None:
            status, message = "down", drift
        else:
            status, message = self._health(
                last_success=last_success,
                alive_workers=len(alive),
                problems=len(problems),
                now=now,
            )

        return MonitoringStatus(
            system={
                "status": status,
                "message": message,
                "version": self._settings.app_version,
                "source": self._settings.metacritic_source,
                "llm_enabled": self._settings.llm_enabled,
                "youtube_enabled": self._settings.youtube_enabled,
                "checked_at": now,
                "schema_drift_streak": consecutive_drift(uow),
            },
            schedule={
                "cadence": self._settings.crawl_interval_cron,
                "timezone": self._settings.crawl_timezone,
                "last_run_at": runs[0].started_at if runs else None,
                "last_success_at": last_success.started_at if last_success else None,
                "last_run_duration_ms": runs[0].duration_ms if runs else None,
                "next_run_at": next_run_at(self._settings, now=now),
                "max_games_per_run": self._settings.crawl_max_games_per_run,
            },
            counters24h=counters,
            current_job=self._current_job(uow, active),
            stages=self._stages(uow, active),
            workers=[
                {
                    "id": w.worker_id,
                    "queues": w.queues,
                    "state": "healthy"
                    if (now - w.heartbeat_at).total_seconds() < WORKER_ALIVE_SECONDS
                    else "idle",
                    "active_jobs": w.active_jobs,
                    "heartbeat_at": w.heartbeat_at,
                    "uptime_s": int((now - w.started_at).total_seconds()),
                }
                for w in workers
            ],
            queues=queues,
            runs=[
                {
                    "id": r.id,
                    "started_at": r.started_at,
                    "duration_ms": r.duration_ms,
                    "status": r.status,
                    "trigger": r.trigger,
                    "discovered": r.games_discovered,
                    "claimed": r.games_claimed,
                    "skipped_duplicates": r.games_skipped_dupe,
                    "failed": r.games_failed,
                }
                for r in runs
            ],
            problems=[
                {
                    "ts": e.ts,
                    "level": e.level,
                    "event": e.event,
                    "message": e.message,
                    "game_id": e.game_id,
                    "job_id": e.job_id,
                    "ref": f"ev_{e.id:08x}",
                }
                for e in problems
            ],
            data_quality=quality,
            ai=ai,
            youtube=uow.youtube.transcript_stats() if self._settings.youtube_enabled else {},
        )

    # ------------------------------------------------------------------ pieces

    def _health(self, *, last_success, alive_workers: int, problems: int, now) -> tuple[str, str]:
        if last_success is None:
            return (
                "degraded",
                "No successful crawl recorded yet. The first run populates the catalogue.",
            )
        age_h = (now - last_success.started_at).total_seconds() / 3600
        if age_h > STALE_AFTER_HOURS:
            return (
                "down",
                f"The last successful crawl was {age_h:.1f} hours ago. "
                "Catalogue data is going stale.",
            )
        if alive_workers == 0:
            return (
                "degraded",
                "No worker has reported in. Queued work is not being processed; "
                "the catalogue and API are unaffected.",
            )
        if problems:
            # Partial failure is a crawler's normal state, and the header says which
            # capability is affected rather than implying everything is broken.
            return (
                "degraded",
                f"{problems} problems in the last 24 hours. Ingest and the API are "
                "serving; check the problem log for the affected capability.",
            )
        return "healthy", "All pipelines are running on schedule."

    def _counters(self, runs, since) -> dict[str, Any]:
        recent = [r for r in runs if r.started_at >= since]
        processed = sum(r.games_claimed for r in recent)
        failed = sum(r.games_failed for r in recent)
        durations = [r.duration_ms for r in recent if r.duration_ms]
        return {
            "runs": len(recent),
            "games_processed": processed,
            "succeeded": max(0, processed - failed),
            "failed": failed,
            "skipped_duplicates": sum(r.games_skipped_dupe for r in recent),
            "success_rate": round(100 * (processed - failed) / processed, 1) if processed else None,
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
        }

    def _current_job(self, uow: UnitOfWork, active_run) -> dict[str, Any] | None:
        running = uow.jobs.recent(limit=1, status=JobStatus.RUNNING.value)
        if not running and active_run is None:
            return None
        job = running[0] if running else None
        return {
            "id": f"job_{job.id}" if job else f"run_{active_run.id}",
            "job_type": job.job_type if job else "crawl.tick",
            "kind": active_run.trigger if active_run else "scheduled",
            "started_at": (job.started_at if job else active_run.started_at),
            "game_id": job.game_id if job else None,
            "run_id": active_run.id if active_run else None,
            "processed": active_run.games_claimed if active_run else None,
            "total": self._settings.crawl_max_games_per_run if active_run else None,
        }

    def _stages(self, uow: UnitOfWork, active_run) -> list[dict[str, Any]]:
        """The pipeline as it actually is, not a generic job list.

        Named after this product's stages so a stall is located at a glance
        (design/PAGES.md section 4).
        """
        stage_queues = {
            "Crawl Metacritic": ["crawl.tick", "game.sync"],
            "Fetch reviews": ["reviews.sync"],
            "AI summarise": ["summary.generate", "summary.gap"],
            "Similarity index": ["similarity.recompute", "similarity.refresh_stale"],
            "YouTube discovery": ["youtube.discover", "youtube.transcript", "youtube.summarise"],
        }
        depth = {(d["queue"], d["status"]): d["count"] for d in uow.jobs.queue_depth()}
        stages = []
        for name, job_types in stage_queues.items():
            queued = sum(
                len(uow.jobs.recent(limit=200, job_type=jt, status=JobStatus.QUEUED.value))
                for jt in job_types
            )
            running = sum(
                len(uow.jobs.recent(limit=50, job_type=jt, status=JobStatus.RUNNING.value))
                for jt in job_types
            )
            failed = sum(
                len(uow.jobs.recent(limit=50, job_type=jt, status=JobStatus.DEAD.value))
                for jt in job_types
            )
            if name == "YouTube discovery" and not self._settings.youtube_enabled:
                state = "disabled"
            elif failed:
                state = "error"
            elif running:
                state = "running"
            elif queued:
                state = "queued"
            else:
                state = "done"
            stages.append(
                {
                    "name": name,
                    "state": state,
                    "queued": queued,
                    "running": running,
                    "failed": failed,
                }
            )
        assert depth is not None
        return stages

    def _data_quality(self, uow: UnitOfWork) -> dict[str, Any]:
        stats = uow.games.stats()
        total = stats["games_total"] or 0
        similarity = uow.similarity.coverage(min_count=3)
        return {
            "games_total": total,
            "with_metascore": stats["games_with_metascore"],
            "with_summaries": stats["games_with_summaries"],
            "summary_coverage_pct": round(
                100 * stats["games_with_summaries"] / total, 1
            )
            if total
            else None,
            "similarity_coverage_pct": round(
                100 * similarity["with_similar"] / total, 1
            )
            if total
            else None,
            "reviews_total": uow.reviews.total_count(),
            "last_crawl_at": stats["last_crawl_at"],
        }

    def _ai(self, uow: UnitOfWork, since) -> dict[str, Any]:
        cost = uow.summaries.cost_since(since)
        validations = uow.summaries.claim_validation_counts(since)
        accepted = validations.get("accepted", 0)
        total_claims = sum(validations.values())
        return {
            **cost,
            "daily_limit_usd": self._settings.ai_daily_cost_limit_usd,
            "claims": validations,
            # A falling acceptance rate is a prompt or model regression, visible here
            # before a user reads a bad summary (ADR-008).
            "claim_acceptance_pct": round(100 * accepted / total_claims, 1)
            if total_claims
            else None,
            "enabled": self._settings.llm_enabled,
        }
