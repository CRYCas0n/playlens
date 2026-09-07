"""The scheduler.

A cron parser and a loop. It only ENQUEUES; a slow job can never make the schedule drift,
and running two schedulers by accident is harmless because every enqueue carries a slot
key protected by ``UNIQUE(idempotency_key)``.
"""

from __future__ import annotations

import datetime as dt
import signal
import threading
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings
from app.container import Container, get_container
from app.domain.errors import PermanentError
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CronExpr:
    """Minimal five-field cron: minute hour day month weekday.

    Supports ``*``, plain numbers, comma lists, ranges and ``*/n``. That is the whole of
    what the schedule needs, and a dependency for it would be a dependency to keep current.
    """

    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]

    @staticmethod
    def parse(expression: str) -> CronExpr:
        parts = expression.split()
        if len(parts) != 5:
            raise PermanentError(f"invalid cron expression: {expression!r}")
        ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        fields = [
            _parse_field(part, low, high)
            for part, (low, high) in zip(parts, ranges, strict=True)
        ]
        return CronExpr(*fields)

    def matches(self, moment: dt.datetime) -> bool:
        return (
            moment.minute in self.minute
            and moment.hour in self.hour
            and moment.day in self.day
            and moment.month in self.month
            and (moment.weekday() + 1) % 7 in self.weekday
        )

    def next_after(
        self, moment: dt.datetime, *, horizon_minutes: int = 60 * 24 * 8
    ) -> dt.datetime | None:
        candidate = moment.replace(second=0, microsecond=0)
        for _ in range(horizon_minutes):
            candidate += dt.timedelta(minutes=1)
            if self.matches(candidate):
                return candidate
        return None


def _parse_field(part: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for chunk in part.split(","):
        if chunk == "*":
            values.update(range(low, high + 1))
        elif chunk.startswith("*/"):
            step = int(chunk[2:])
            values.update(range(low, high + 1, step))
        elif "-" in chunk:
            start, end = chunk.split("-", 1)
            values.update(range(int(start), int(end) + 1))
        else:
            values.add(int(chunk))
    invalid = [v for v in values if not low <= v <= high]
    if invalid:
        raise PermanentError(f"cron field out of range: {part!r}")
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    job_type: str
    queue: str
    cron: CronExpr
    payload: dict
    priority: int = 5
    enabled: bool = True


def build_schedule(settings: Settings) -> list[ScheduledTask]:
    tasks = [
        ScheduledTask(
            "crawl.tick", "crawl", CronExpr.parse(settings.crawl_interval_cron),
            {"trigger": "schedule", "triggered_by": "scheduler"},
            priority=7, enabled=settings.crawl_enabled,
        ),
        ScheduledTask(
            "crawl.reap", "crawl", CronExpr.parse("*/5 * * * *"), {}, priority=6
        ),
        ScheduledTask(
            "crawl.reconcile", "crawl", CronExpr.parse(settings.reconcile_cron), {},
            priority=2, enabled=settings.reconcile_enabled,
        ),
        ScheduledTask(
            "similarity.refresh_stale", "enrich",
            CronExpr.parse(settings.similarity_refresh_cron), {}, priority=2,
        ),
        ScheduledTask(
            "maintenance.cleanup", "crawl", CronExpr.parse("0 5 * * *"), {}, priority=1
        ),
    ]
    return [task for task in tasks if task.enabled]


def slot_key(job_type: str, moment: dt.datetime) -> str:
    """One job per (task, minute). Two schedulers cannot double-book a slot."""
    return f"{job_type}:{moment.strftime('%Y-%m-%dT%H:%M')}"


class Scheduler:
    def __init__(self, container: Container, *, clock=None) -> None:
        self.container = container
        self.settings = container.settings
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._tz = ZoneInfo(self.settings.crawl_timezone)
        self.tasks = build_schedule(self.settings)
        self._stop = threading.Event()

    def request_stop(self, *_args: object) -> None:
        self._stop.set()

    def tick(self, moment: dt.datetime | None = None) -> list[str]:
        now = (moment or self._clock()).astimezone(self._tz).replace(second=0, microsecond=0)
        fired: list[str] = []
        with self.container.uow() as uow:
            for task in self.tasks:
                if not task.cron.matches(now):
                    continue
                result = uow.jobs.enqueue(
                    job_type=task.job_type,
                    idempotency_key=slot_key(task.job_type, now),
                    queue=task.queue,
                    payload=task.payload,
                    priority=task.priority,
                )
                if result.created:
                    fired.append(task.job_type)
        if fired:
            log.info("scheduler.fired", extra={"tasks": fired, "slot": now.isoformat()})
        return fired

    def next_run_at(self, job_type: str = "crawl.tick") -> dt.datetime | None:
        for task in self.tasks:
            if task.job_type == job_type:
                return task.cron.next_after(self._clock().astimezone(self._tz))
        return None

    def run(self, *, max_ticks: int | None = None) -> int:  # pragma: no cover - loop
        log.info("scheduler.started", extra={"tasks": [t.job_type for t in self.tasks]})
        ticks = 0
        while not self._stop.is_set():
            if max_ticks is not None and ticks >= max_ticks:
                break
            self.tick()
            ticks += 1
            self._stop.wait(self.settings.scheduler_tick_interval_s)
        return ticks


def next_run_at(settings: Settings, *, now: dt.datetime | None = None) -> dt.datetime | None:
    """Used by the monitoring endpoint to answer "when does it run next"."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(ZoneInfo(settings.crawl_timezone))
    return CronExpr.parse(settings.crawl_interval_cron).next_after(moment)


def main() -> None:  # pragma: no cover - process entry point
    settings = get_settings()
    configure_logging(settings)
    container = get_container()
    scheduler = Scheduler(container)
    signal.signal(signal.SIGINT, scheduler.request_stop)
    signal.signal(signal.SIGTERM, scheduler.request_stop)
    try:
        scheduler.run()
    finally:
        container.close()


if __name__ == "__main__":  # pragma: no cover
    main()
