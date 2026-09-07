"""Worker and scheduler: the parts a broker would normally provide."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.container import Container
from app.db.models import Job, JobEvent
from app.domain.enums import JobStatus
from app.domain.errors import BudgetExhausted, PermanentError, RetryableError
from app.queue.scheduler import CronExpr, Scheduler, slot_key
from app.queue.worker import Worker
from app.tasks import contracts, registry

pytestmark = pytest.mark.integration


@pytest.fixture
def container(db_settings, session_factory) -> Container:
    c = Container(settings=db_settings)
    # Reuse the test engine so the worker and the assertions see one database.
    # Writing into __dict__ overrides the cached_property without touching production code.
    c.__dict__["session_factory"] = session_factory
    c.__dict__["engine"] = session_factory.kw["bind"]
    return c


@pytest.fixture
def register_probe():
    """Register a temporary task, then remove it again."""
    added: list[str] = []

    def _register(name: str, handler, *, queue: str = "enrich", max_attempts: int = 3):
        registry.TASKS[name] = registry.TaskSpec(
            name=name, queue=queue, handler=handler, retryable=True,
            idempotent=True, description="test probe",
        )
        # The ceiling lives in the contracts table, not on the spec, so a probe has to
        # declare itself there too -- exactly as a real task does.
        contracts.MAX_ATTEMPTS[name] = max_attempts
        added.append(name)
        return name

    yield _register
    for name in added:
        registry.TASKS.pop(name, None)
        contracts.MAX_ATTEMPTS.pop(name, None)


class TestWorkerExecution:
    def test_runs_a_job_and_records_the_result(self, container, db, register_probe):
        calls = []
        register_probe("probe.ok", lambda c, uow, payload: calls.append(payload) or {"ok": 1})

        with container.uow() as uow:
            uow.jobs.enqueue(
                job_type="probe.ok", idempotency_key="p1", queue="enrich",
                payload={"value": 42},
            )

        worker = Worker(container, identifier="w-test")
        assert worker.run_once() is True

        db.expire_all()
        job = db.execute(sa.select(Job)).scalars().one()
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.result == {"ok": 1}
        assert job.duration_ms is not None
        assert calls[0]["value"] == 42

    def test_events_bracket_every_job(self, container, db, register_probe):
        register_probe("probe.ev", lambda c, uow, payload: {})
        with container.uow() as uow:
            uow.jobs.enqueue(job_type="probe.ev", idempotency_key="p1", queue="enrich")
        Worker(container, identifier="w").run_once()

        events = [e.event for e in db.execute(sa.select(JobEvent).order_by(JobEvent.id)).scalars()]
        assert "job.started" in events
        assert "job.finished" in events

    def test_a_retryable_failure_goes_back_to_the_queue(self, container, db, register_probe):
        def boom(c, uow, payload):
            raise RetryableError("upstream 503")

        register_probe("probe.retry", boom)
        with container.uow() as uow:
            uow.jobs.enqueue(job_type="probe.retry", idempotency_key="p1", queue="enrich")

        Worker(container, identifier="w").run_once()
        db.expire_all()
        job = db.execute(sa.select(Job)).scalars().one()
        assert job.status == JobStatus.RETRYING.value
        assert job.next_retry_at is not None
        assert job.error_class == "RetryableError"

    def test_a_permanent_failure_stops_immediately(self, container, db, register_probe):
        def boom(c, uow, payload):
            raise PermanentError("404")

        register_probe("probe.perm", boom)
        with container.uow() as uow:
            uow.jobs.enqueue(job_type="probe.perm", idempotency_key="p1", queue="enrich")

        Worker(container, identifier="w").run_once()
        db.expire_all()
        assert db.execute(sa.select(Job)).scalars().one().status == JobStatus.FAILED.value

    def test_an_exhausted_quota_defers_rather_than_fails(self, container, db, register_probe):
        """Assignment scenario 3: a YouTube quota ceiling must not fail the game."""

        def boom(c, uow, payload):
            raise BudgetExhausted("daily quota reached", retry_after_s=3600)

        register_probe("probe.budget", boom)
        with container.uow() as uow:
            uow.jobs.enqueue(job_type="probe.budget", idempotency_key="p1", queue="enrich")

        before = dt.datetime.now(dt.UTC)
        Worker(container, identifier="w").run_once()
        db.expire_all()
        job = db.execute(sa.select(Job)).scalars().one()
        assert job.status == JobStatus.RETRYING.value
        assert (job.next_retry_at - before).total_seconds() > 3000

    def test_an_unknown_error_is_treated_as_retryable(self, container, db, register_probe):
        def boom(c, uow, payload):
            raise ZeroDivisionError("surprise")

        register_probe("probe.unknown", boom)
        with container.uow() as uow:
            uow.jobs.enqueue(job_type="probe.unknown", idempotency_key="p1", queue="enrich")
        Worker(container, identifier="w").run_once()
        db.expire_all()
        assert db.execute(sa.select(Job)).scalars().one().status == JobStatus.RETRYING.value

    def test_an_unknown_job_type_fails_permanently(self, container, db):
        with container.uow() as uow:
            uow.jobs.enqueue(job_type="nope.nothing", idempotency_key="p1", queue="enrich")
        Worker(container, identifier="w").run_once()
        db.expire_all()
        job = db.execute(sa.select(Job)).scalars().one()
        assert job.status == JobStatus.FAILED.value
        assert "unknown job type" in job.error_message

    def test_an_empty_queue_is_not_an_error(self, container, db):
        worker = Worker(container, identifier="w")
        assert worker.run_once() is False
        assert worker.stats.claimed == 0

    def test_the_worker_heartbeats_even_when_idle(self, container, db):
        Worker(container, identifier="w-idle").run_once()
        db.expire_all()
        with container.uow() as uow:
            workers = uow.workers.all()
        assert [w.worker_id for w in workers] == ["w-idle"]

    def test_the_result_and_the_status_commit_together(self, container, db, register_probe):
        """A handler that writes and then fails leaves nothing behind."""

        def write_then_fail(c, uow, payload):
            uow.events.emit("probe.side_effect")
            raise RetryableError("failed after writing")

        register_probe("probe.tx", write_then_fail)
        with container.uow() as uow:
            uow.jobs.enqueue(job_type="probe.tx", idempotency_key="p1", queue="enrich")
        Worker(container, identifier="w").run_once()

        events = [e.event for e in db.execute(sa.select(JobEvent)).scalars()]
        assert "probe.side_effect" not in events


class TestCron:
    @pytest.mark.parametrize(
        ("expression", "moment", "expected"),
        [
            ("7 * * * *", dt.datetime(2026, 9, 6, 10, 7), True),
            ("7 * * * *", dt.datetime(2026, 9, 6, 10, 8), False),
            ("*/5 * * * *", dt.datetime(2026, 9, 6, 10, 15), True),
            ("*/5 * * * *", dt.datetime(2026, 9, 6, 10, 16), False),
            ("0 3 * * 0", dt.datetime(2026, 9, 6, 3, 0), True),   # a Sunday
            ("0 3 * * 0", dt.datetime(2026, 9, 7, 3, 0), False),  # a Monday
        ],
    )
    def test_matching(self, expression, moment, expected):
        assert CronExpr.parse(expression).matches(moment) is expected

    def test_next_after(self):
        nxt = CronExpr.parse("7 * * * *").next_after(dt.datetime(2026, 9, 6, 10, 7))
        assert nxt == dt.datetime(2026, 9, 6, 11, 7)

    def test_a_bad_expression_is_rejected_at_construction(self):
        with pytest.raises(PermanentError):
            CronExpr.parse("not a cron")


class TestScheduler:
    def test_fires_the_hourly_tick_on_its_minute(self, container, db):
        scheduler = Scheduler(container)
        fired = scheduler.tick(dt.datetime(2026, 9, 6, 10, 7, tzinfo=dt.UTC))
        assert "crawl.tick" in fired
        types = db.execute(sa.select(Job.job_type)).scalars().all()
        assert "crawl.tick" in types

    def test_the_same_slot_cannot_fire_twice(self, container, db):
        """Two schedulers running by accident must not double-book an hour."""
        scheduler = Scheduler(container)
        moment = dt.datetime(2026, 9, 6, 10, 7, tzinfo=dt.UTC)
        first = scheduler.tick(moment)
        second = scheduler.tick(moment)
        assert "crawl.tick" in first
        assert "crawl.tick" not in second
        count = db.execute(
            sa.select(sa.func.count()).select_from(Job).where(Job.job_type == "crawl.tick")
        ).scalar_one()
        assert count == 1

    def test_a_disabled_crawl_is_not_scheduled(self, session_factory, db_settings, db):
        settings = Settings(**{**db_settings.model_dump(), "crawl_enabled": False})
        container = Container(settings=settings)
        container.__dict__["session_factory"] = session_factory
        scheduler = Scheduler(container)
        assert "crawl.tick" not in [t.job_type for t in scheduler.tasks]

    def test_next_run_is_reported_for_the_dashboard(self, container):
        scheduler = Scheduler(
            container, clock=lambda: dt.datetime(2026, 9, 6, 10, 30, tzinfo=dt.UTC)
        )
        assert scheduler.next_run_at().minute == 7

    def test_slot_keys_are_per_minute(self):
        a = slot_key("crawl.tick", dt.datetime(2026, 9, 6, 10, 7))
        b = slot_key("crawl.tick", dt.datetime(2026, 9, 6, 11, 7))
        assert a != b


class TestRegistry:
    def test_every_task_declares_its_contract(self):
        """Assignment section 11 asks for exactly this to be explicit."""
        for name, spec in registry.TASKS.items():
            assert spec.queue in {"crawl", "enrich", "ai"}, name
            assert spec.max_attempts >= 1, name
            assert spec.description, name

    def test_the_hourly_tick_is_not_retried(self):
        # There is no point retrying a schedule: the next hour does the same work.
        assert registry.TASKS["crawl.tick"].retryable is False
