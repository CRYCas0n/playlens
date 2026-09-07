"""The worker loop.

Claim, execute, record, repeat. Everything that would normally come from a broker —
at-least-once delivery, lease renewal, retry with backoff, dead-lettering — comes from the
``jobs`` table instead, in the same transaction as the work's own result (ADR-003).
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.container import Container, get_container
from app.domain.errors import BudgetExhausted, PermanentError, RetryableError
from app.logging import configure_logging, correlate, get_logger
from app.tasks.registry import get_task

log = get_logger(__name__)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class WorkerStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    idle_polls: int = 0


class Worker:
    def __init__(
        self,
        container: Container,
        *,
        queues: list[str] | None = None,
        identifier: str | None = None,
    ) -> None:
        self.container = container
        self.settings: Settings = container.settings
        self.queues = queues or self.settings.queues
        self.id = identifier or worker_id()
        self.stats = WorkerStats()
        self._stop = threading.Event()

    def request_stop(self, *_args: object) -> None:
        log.info("worker.stopping", extra={"worker_id": self.id})
        self._stop.set()

    # ------------------------------------------------------------------ loop

    def run(self, *, max_jobs: int | None = None, max_idle_polls: int | None = None) -> WorkerStats:
        log.info("worker.started", extra={"worker_id": self.id, "queues": self.queues})
        processed = 0
        while not self._stop.is_set():
            if max_jobs is not None and processed >= max_jobs:
                break
            did_work = self.run_once()
            if did_work:
                processed += 1
                self.stats.idle_polls = 0
            else:
                self.stats.idle_polls += 1
                if max_idle_polls is not None and self.stats.idle_polls >= max_idle_polls:
                    break
                self._stop.wait(self.settings.worker_poll_interval_s)
        return self.stats

    def run_once(self) -> bool:
        """Claim and run at most one job. Returns True if something was executed."""
        with self.container.uow() as uow:
            job = uow.jobs.claim(
                queues=self.queues,
                worker_id=self.id,
                lease_ttl_s=self.settings.job_lease_ttl_s,
            )
            if job is None:
                uow.workers.heartbeat(
                    self.id,
                    queues=",".join(self.queues),
                    active_jobs=0,
                    version=self.settings.app_version,
                )
                return False
            job_id, job_type, payload = job.id, job.job_type, dict(job.payload or {})
            game_id = job.game_id
            run_id = job.crawl_run_id
            uow.workers.heartbeat(
                self.id,
                queues=",".join(self.queues),
                active_jobs=1,
                version=self.settings.app_version,
            )

        self.stats.claimed += 1
        self._execute(job_id, job_type, payload, game_id=game_id, crawl_run_id=run_id)
        return True

    # ------------------------------------------------------------------ execution

    def _execute(
        self,
        job_id: int,
        job_type: str,
        payload: dict,
        *,
        game_id: int | None,
        crawl_run_id: int | None,
    ) -> None:
        started = time.monotonic()
        with correlate(
            job_id=job_id, job_type=job_type, game_id=game_id,
            crawl_run_id=crawl_run_id, worker_id=self.id,
        ):
            try:
                spec = get_task(job_type)
            except PermanentError as exc:
                self._record_failure(job_id, exc, retryable=False)
                return

            payload.setdefault("worker_id", self.id)

            # The handler's own transaction. Its result and the job's status are committed
            # together, so "the job says done but nothing was written" cannot happen.
            try:
                with self.container.uow() as uow:
                    uow.events.emit(
                        "job.started", job_id=job_id, game_id=game_id,
                        crawl_run_id=crawl_run_id, worker_id=self.id,
                        data={"job_type": job_type},
                    )
                    result = spec.handler(self.container, uow, payload)
                    job = uow.jobs.get(job_id)
                    assert job is not None
                    uow.jobs.succeed(job, result)
                    uow.events.emit(
                        "job.finished", job_id=job_id, game_id=game_id,
                        crawl_run_id=crawl_run_id, worker_id=self.id,
                        data={
                            "job_type": job_type,
                            "status": "succeeded",
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            **{k: v for k, v in (result or {}).items() if _is_scalar(v)},
                        },
                    )
                self.stats.succeeded += 1
            except BudgetExhausted as exc:
                # A quota ceiling defers work; it never fails a game (assignment scenario 3).
                self._record_failure(job_id, exc, retryable=True, retry_after_s=exc.retry_after_s)
            except RetryableError as exc:
                self._record_failure(job_id, exc, retryable=True, retry_after_s=exc.retry_after_s)
            except PermanentError as exc:
                self._record_failure(job_id, exc, retryable=False)
            except Exception as exc:
                # Unknown failures are retried: a transient fault is far more likely than
                # a permanent one, and the attempt counter bounds the cost of being wrong.
                log.exception("job.unexpected_error", extra={"job_type": job_type})
                self._record_failure(job_id, exc, retryable=True)

    def _record_failure(
        self,
        job_id: int,
        exc: BaseException,
        *,
        retryable: bool,
        retry_after_s: float | None = None,
    ) -> None:
        with self.container.uow() as uow:
            job = uow.jobs.get(job_id)
            if job is None:  # pragma: no cover - job deleted mid-flight
                return
            status = uow.jobs.fail(
                job,
                exc,
                retryable=retryable,
                backoff_base=self.settings.job_retry_backoff_base_s,
                backoff_max=self.settings.job_retry_backoff_max_s,
                retry_after_s=retry_after_s,
            )
            uow.events.emit(
                "job.finished",
                level="warning" if status.value == "retrying" else "error",
                job_id=job_id,
                game_id=job.game_id,
                crawl_run_id=job.crawl_run_id,
                worker_id=self.id,
                message=str(exc)[:500],
                data={
                    "job_type": job.job_type,
                    "status": status.value,
                    "error_class": type(exc).__name__,
                    "attempt": job.attempts,
                },
            )
        self.stats.failed += 1


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def main() -> None:  # pragma: no cover - process entry point
    settings = get_settings()
    configure_logging(settings)
    container = get_container()
    worker = Worker(container)
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)
    try:
        worker.run()
    finally:
        container.close()


if __name__ == "__main__":  # pragma: no cover
    main()
