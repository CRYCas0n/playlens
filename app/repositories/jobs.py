"""The job queue and the event log.

The queue is a table (ADR-003). That means the state of a job and the record of what it
did are written in one transaction and cannot disagree — which is precisely what the
blueprint wanted from its own ``jobs`` table while also running a separate broker.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import Job, JobEvent, WorkerHeartbeat
from app.domain.enums import JobStatus
from app.logging import redact
from app.repositories.base import claim_one, insert_ignore_returning, utc_now
from app.tasks.contracts import max_attempts_for


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: int | None
    created: bool
    reason: str = ""


def backoff_delay(attempt: int, *, base: float, maximum: float) -> float:
    """Exponential backoff with full jitter.

    Full jitter rather than a fixed multiplier: when a shared dependency comes back after
    an outage, synchronised retries from every worker are how you knock it over again.
    """
    ceiling = min(maximum, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0, ceiling)


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ enqueue

    def enqueue(
        self,
        *,
        job_type: str,
        idempotency_key: str,
        queue: str,
        payload: dict[str, Any] | None = None,
        game_id: int | None = None,
        crawl_run_id: int | None = None,
        parent_job_id: int | None = None,
        priority: int = 5,
        max_attempts: int | None = None,
        run_after: dt.datetime | None = None,
    ) -> EnqueueResult:
        """Idempotent enqueue.

        ``UNIQUE(idempotency_key)`` makes a duplicate physically impossible, so callers
        never have to check first — and a redelivered message is harmless.
        """
        row = insert_ignore_returning(
            self.session,
            Job.__table__,
            {
                "job_type": job_type,
                "idempotency_key": idempotency_key,
                "queue": queue,
                "status": JobStatus.QUEUED.value,
                "priority": priority,
                "payload": payload or {},
                "game_id": game_id,
                "crawl_run_id": crawl_run_id,
                "parent_job_id": parent_job_id,
                # None means "whatever this task declared". Every call site used to pass
                # its own literal, and they had drifted: the orchestrator enqueued
                # game.sync with 5 while the registry declared 3.
                "max_attempts": (
                    max_attempts if max_attempts is not None else max_attempts_for(job_type)
                ),
                "next_retry_at": run_after,
                "queued_at": utc_now(),
            },
            index_elements=["idempotency_key"],
            returning=[Job.__table__.c.id],
        )
        if row is None:
            return EnqueueResult(job_id=None, created=False, reason="duplicate_idempotency_key")
        return EnqueueResult(job_id=int(row[0]), created=True)

    # ------------------------------------------------------------------ claim

    def claim(self, *, queues: list[str], worker_id: str, lease_ttl_s: int) -> Job | None:
        now = utc_now()
        row = claim_one(
            self.session,
            Job,
            where=sa.and_(
                Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRYING.value]),
                Job.queue.in_(queues),
                sa.or_(Job.next_retry_at.is_(None), Job.next_retry_at <= now),
            ),
            order_by=[Job.priority.desc(), Job.queued_at.asc(), Job.id.asc()],
            values={
                "status": JobStatus.RUNNING.value,
                "worker_id": worker_id,
                "started_at": now,
                "lease_expires_at": now + dt.timedelta(seconds=lease_ttl_s),
                "attempts": Job.attempts + 1,
            },
            returning=[Job.id],
        )
        if row is None:
            return None
        return self.session.get(Job, int(row[0]))

    def heartbeat_job(self, job_id: int, *, lease_ttl_s: int) -> None:
        self.session.execute(
            sa.update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=utc_now() + dt.timedelta(seconds=lease_ttl_s))
        )

    # ------------------------------------------------------------------ completion

    def succeed(self, job: Job, result: dict[str, Any] | None = None) -> None:
        self._finish(job, JobStatus.SUCCEEDED, result=result)

    def skip(self, job: Job, reason: str) -> None:
        """Not an error.

        500 skips because the input has not changed is a healthy system, and a dashboard
        that calls them failures lies to its operator.
        """
        self._finish(job, JobStatus.SKIPPED, result={"reason": reason})

    def fail(
        self,
        job: Job,
        error: BaseException,
        *,
        retryable: bool,
        backoff_base: float,
        backoff_max: float,
        retry_after_s: float | None = None,
    ) -> JobStatus:
        now = utc_now()
        job.error_class = type(error).__name__
        job.error_message = str(error)[:2000]
        job.lease_expires_at = None

        if retryable and job.attempts < job.max_attempts:
            delay = retry_after_s if retry_after_s is not None else backoff_delay(
                job.attempts, base=backoff_base, maximum=backoff_max
            )
            job.status = JobStatus.RETRYING.value
            job.next_retry_at = now + dt.timedelta(seconds=delay)
            job.started_at = None
        else:
            # `dead` (attempts exhausted) is kept apart from `failed` (permanent, a human
            # can retry it) so the dashboard distinguishes "needs a fix" from "gave up".
            job.status = (JobStatus.DEAD if retryable else JobStatus.FAILED).value
            job.finished_at = now
            if job.started_at:
                job.duration_ms = int((now - job.started_at).total_seconds() * 1000)
        self.session.flush()
        return JobStatus(job.status)

    def _finish(self, job: Job, status: JobStatus, *, result: dict | None = None) -> None:
        now = utc_now()
        job.status = status.value
        job.finished_at = now
        job.result = result
        job.lease_expires_at = None
        if job.started_at:
            job.duration_ms = int((now - job.started_at).total_seconds() * 1000)
        self.session.flush()

    def reap_expired_jobs(self) -> int:
        """Return jobs whose worker died to the queue.

        Equivalent to a broker's ``acks_late`` + visibility timeout, minus the broker.
        """
        now = utc_now()
        result = self.session.execute(
            sa.update(Job)
            .where(
                Job.status == JobStatus.RUNNING.value,
                Job.lease_expires_at.is_not(None),
                Job.lease_expires_at < now,
            )
            .values(
                status=JobStatus.RETRYING.value,
                lease_expires_at=None,
                next_retry_at=now,
                started_at=None,
                error_class="LeaseExpired",
                error_message="worker lease expired; job returned to the queue",
            )
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------ reads

    def get(self, job_id: int) -> Job | None:
        return self.session.get(Job, job_id)

    def by_idempotency_key(self, key: str) -> Job | None:
        return self.session.execute(
            sa.select(Job).where(Job.idempotency_key == key)
        ).scalar_one_or_none()

    def queue_depth(self) -> list[dict[str, Any]]:
        rows = self.session.execute(
            sa.select(
                Job.queue,
                Job.status,
                sa.func.count().label("n"),
                sa.func.min(Job.queued_at).label("oldest"),
            )
            .where(Job.status.in_([JobStatus.QUEUED.value, JobStatus.RETRYING.value,
                                   JobStatus.RUNNING.value]))
            .group_by(Job.queue, Job.status)
        ).all()
        return [
            {"queue": r.queue, "status": r.status, "count": int(r.n), "oldest": r.oldest}
            for r in rows
        ]

    def recent(self, *, limit: int = 50, status: str | None = None,
               job_type: str | None = None, game_id: int | None = None) -> list[Job]:
        stmt = sa.select(Job).order_by(Job.queued_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Job.status == status)
        if job_type:
            stmt = stmt.where(Job.job_type == job_type)
        if game_id:
            stmt = stmt.where(Job.game_id == game_id)
        return list(self.session.execute(stmt).scalars())

    def duration_percentiles(self, since: dt.datetime) -> dict[str, int | None]:
        durations = [
            int(d)
            for (d,) in self.session.execute(
                sa.select(Job.duration_ms).where(
                    Job.duration_ms.is_not(None), Job.finished_at >= since
                )
            ).all()
        ]
        if not durations:
            return {"p50": None, "p95": None}
        durations.sort()
        return {
            "p50": durations[len(durations) // 2],
            "p95": durations[min(len(durations) - 1, int(len(durations) * 0.95))],
        }

    def purge_old(self, older_than: dt.datetime) -> int:
        result = self.session.execute(
            sa.delete(Job).where(
                Job.finished_at.is_not(None),
                Job.finished_at < older_than,
            )
        )
        return int(result.rowcount or 0)


class EventRepository:
    """Append-only log. Also the SSE stream — ``id`` is the ``Last-Event-ID``."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def emit(
        self,
        event: str,
        *,
        level: str = "info",
        message: str | None = None,
        stage: str | None = None,
        crawl_run_id: int | None = None,
        job_id: int | None = None,
        game_id: int | None = None,
        worker_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        row = JobEvent(
            event=event,
            level=level,
            message=(message or "")[:2000] or None,
            stage=stage,
            crawl_run_id=crawl_run_id,
            job_id=job_id,
            game_id=game_id,
            worker_id=worker_id,
            # The same redactor as the logger: an operator must never read a token off a
            # dashboard (ADR-019 T5).
            data=redact(data or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def after(self, cursor: int, *, limit: int = 200,
              crawl_run_id: int | None = None) -> list[JobEvent]:
        stmt = sa.select(JobEvent).where(JobEvent.id > cursor).order_by(JobEvent.id).limit(limit)
        if crawl_run_id is not None:
            stmt = stmt.where(JobEvent.crawl_run_id == crawl_run_id)
        return list(self.session.execute(stmt).scalars())

    def consecutive(self, event: str, *, reset_event: str) -> int:
        """How many ``event`` rows since the last ``reset_event``.

        The event log is append-only and monotonically identified, so "consecutive" is a
        single comparison against the id of the last reset -- no counter to keep in a
        process, and the answer survives a restart. That matters here: the thing being
        counted is "the source changed shape", and a counter that resets when a worker
        restarts would never reach any threshold.
        """
        last_ok = self.session.execute(
            sa.select(sa.func.max(JobEvent.id)).where(JobEvent.event == reset_event)
        ).scalar() or 0
        return int(
            self.session.execute(
                sa.select(sa.func.count())
                .select_from(JobEvent)
                .where(JobEvent.event == event, JobEvent.id > last_ok)
            ).scalar_one()
        )

    def latest_id(self) -> int:
        return int(
            self.session.execute(
                sa.select(sa.func.coalesce(sa.func.max(JobEvent.id), 0))
            ).scalar_one()
        )

    def recent(self, *, limit: int = 50, level: str | None = None,
               crawl_run_id: int | None = None, game_id: int | None = None,
               since: dt.datetime | None = None) -> list[JobEvent]:
        stmt = sa.select(JobEvent).order_by(JobEvent.id.desc()).limit(limit)
        if level == "problems":
            stmt = stmt.where(JobEvent.level.in_(["warning", "error"]))
        elif level:
            stmt = stmt.where(JobEvent.level == level)
        if crawl_run_id is not None:
            stmt = stmt.where(JobEvent.crawl_run_id == crawl_run_id)
        if game_id is not None:
            stmt = stmt.where(JobEvent.game_id == game_id)
        if since is not None:
            stmt = stmt.where(JobEvent.ts >= since)
        return list(self.session.execute(stmt).scalars())

    def purge_old(self, older_than: dt.datetime) -> int:
        result = self.session.execute(sa.delete(JobEvent).where(JobEvent.ts < older_than))
        return int(result.rowcount or 0)


class WorkerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def heartbeat(self, worker_id: str, *, queues: str, active_jobs: int, version: str) -> None:
        existing = self.session.get(WorkerHeartbeat, worker_id)
        if existing is None:
            self.session.add(
                WorkerHeartbeat(
                    worker_id=worker_id, queues=queues, active_jobs=active_jobs, version=version
                )
            )
        else:
            existing.heartbeat_at = utc_now()
            existing.active_jobs = active_jobs
            existing.queues = queues
            existing.version = version
        self.session.flush()

    def all(self) -> list[WorkerHeartbeat]:
        return list(
            self.session.execute(
                sa.select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_id)
            ).scalars()
        )
