"""The database-backed queue (ADR-003).

These tests carry the weight that a battle-tested broker would otherwise carry, so they
are deliberately about concurrency and crash recovery rather than happy paths.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from app.db.models import Job
from app.domain.enums import JobStatus
from app.domain.errors import PermanentError, RetryableError
from app.repositories.jobs import EventRepository, JobRepository, WorkerRepository, backoff_delay

pytestmark = pytest.mark.integration


def enqueue(session, key: str, *, queue: str = "enrich", **kw):
    return JobRepository(session).enqueue(
        job_type="game.sync", idempotency_key=key, queue=queue, **kw
    )


class TestIdempotentEnqueue:
    def test_first_enqueue_creates(self, db):
        result = enqueue(db, "game.sync:2026-09-06:elden-ring")
        db.commit()
        assert result.created and result.job_id

    def test_duplicate_is_refused_by_the_constraint(self, db):
        enqueue(db, "game.sync:2026-09-06:elden-ring")
        db.commit()
        again = enqueue(db, "game.sync:2026-09-06:elden-ring")
        db.commit()
        assert again.created is False
        assert again.reason == "duplicate_idempotency_key"
        assert db.execute(sa.select(sa.func.count()).select_from(Job)).scalar_one() == 1

    def test_a_different_day_is_a_different_job(self, db):
        enqueue(db, "game.sync:2026-09-06:elden-ring")
        enqueue(db, "game.sync:2026-09-07:elden-ring")
        db.commit()
        assert db.execute(sa.select(sa.func.count()).select_from(Job)).scalar_one() == 2


class TestClaim:
    def test_claim_marks_running_and_takes_a_lease(self, db):
        enqueue(db, "k1")
        db.commit()
        job = JobRepository(db).claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        assert job is not None
        assert job.status == JobStatus.RUNNING.value
        assert job.worker_id == "w1"
        assert job.attempts == 1
        assert job.lease_expires_at is not None

    def test_claim_respects_the_queue_filter(self, db):
        enqueue(db, "k1", queue="ai")
        db.commit()
        assert JobRepository(db).claim(queues=["crawl"], worker_id="w1", lease_ttl_s=900) is None
        assert JobRepository(db).claim(queues=["ai"], worker_id="w1", lease_ttl_s=900) is not None

    def test_priority_then_age(self, db):
        JobRepository(db).enqueue(job_type="a", idempotency_key="low", queue="enrich", priority=1)
        JobRepository(db).enqueue(job_type="b", idempotency_key="high", queue="enrich", priority=9)
        db.commit()
        job = JobRepository(db).claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        assert job.idempotency_key == "high"

    def test_a_deferred_retry_is_not_claimable_yet(self, db):
        future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        JobRepository(db).enqueue(
            job_type="a", idempotency_key="later", queue="enrich", run_after=future
        )
        db.commit()
        assert JobRepository(db).claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900) is None

    def test_two_workers_never_claim_the_same_job(self, db, session_factory):
        """The property a broker would give us; here it comes from the database."""
        for i in range(12):
            enqueue(db, f"k{i}")
        db.commit()

        def drain() -> list[int]:
            session = session_factory()
            got: list[int] = []
            try:
                while True:
                    job = JobRepository(session).claim(
                        queues=["enrich"], worker_id="w", lease_ttl_s=900
                    )
                    if job is None:
                        break
                    got.append(job.id)
                    session.commit()
            finally:
                session.rollback()
                session.close()
            return got

        with ThreadPoolExecutor(max_workers=4) as pool:
            batches = list(pool.map(lambda _: drain(), range(4)))

        claimed = [jid for batch in batches for jid in batch]
        assert len(claimed) == len(set(claimed)) == 12


class TestCompletion:
    def test_success_records_duration(self, db):
        enqueue(db, "k1")
        db.commit()
        repo = JobRepository(db)
        job = repo.claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        repo.succeed(job, {"games": 3})
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.duration_ms is not None
        assert job.result == {"games": 3}
        assert job.lease_expires_at is None

    def test_skip_is_not_an_error(self, db):
        enqueue(db, "k1")
        db.commit()
        repo = JobRepository(db)
        job = repo.claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        repo.skip(job, "identical_input")
        assert job.status == JobStatus.SKIPPED.value
        assert job.error_message is None

    def test_retryable_failure_reschedules(self, db):
        enqueue(db, "k1")
        db.commit()
        repo = JobRepository(db)
        job = repo.claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        status = repo.fail(
            job, RetryableError("429"), retryable=True, backoff_base=2, backoff_max=600
        )
        assert status is JobStatus.RETRYING
        assert job.next_retry_at is not None
        assert job.finished_at is None

    def test_permanent_failure_does_not_retry(self, db):
        enqueue(db, "k1")
        db.commit()
        repo = JobRepository(db)
        job = repo.claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        status = repo.fail(
            job, PermanentError("404"), retryable=False, backoff_base=2, backoff_max=600
        )
        assert status is JobStatus.FAILED
        assert job.next_retry_at is None

    def test_exhausted_attempts_become_dead(self, db):
        JobRepository(db).enqueue(
            job_type="a", idempotency_key="k1", queue="enrich", max_attempts=1
        )
        db.commit()
        repo = JobRepository(db)
        job = repo.claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        status = repo.fail(
            job, RetryableError("timeout"), retryable=True, backoff_base=2, backoff_max=600
        )
        # dead != failed: one needs a human, the other can simply be retried.
        assert status is JobStatus.DEAD

    def test_retry_honours_retry_after(self, db):
        enqueue(db, "k1")
        db.commit()
        repo = JobRepository(db)
        job = repo.claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        before = dt.datetime.now(dt.UTC)
        repo.fail(
            job, RetryableError("429"), retryable=True, backoff_base=2, backoff_max=600,
            retry_after_s=120,
        )
        assert (job.next_retry_at - before).total_seconds() >= 119


class TestReaper:
    def test_expired_lease_returns_the_job(self, db):
        enqueue(db, "k1")
        db.commit()
        repo = JobRepository(db)
        job = repo.claim(queues=["enrich"], worker_id="dead-worker", lease_ttl_s=900)
        db.execute(
            sa.update(Job).where(Job.id == job.id).values(
                lease_expires_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            )
        )
        db.flush()

        assert repo.reap_expired_jobs() == 1
        db.expire_all()
        revived = db.get(Job, job.id)
        assert revived.status == JobStatus.RETRYING.value
        assert revived.error_class == "LeaseExpired"

        # and it is claimable again
        assert repo.claim(queues=["enrich"], worker_id="w2", lease_ttl_s=900) is not None

    def test_a_live_lease_is_left_alone(self, db):
        enqueue(db, "k1")
        db.commit()
        repo = JobRepository(db)
        repo.claim(queues=["enrich"], worker_id="w1", lease_ttl_s=900)
        assert repo.reap_expired_jobs() == 0


class TestBackoff:
    def test_grows_and_is_capped(self):
        assert all(0 <= backoff_delay(a, base=2, maximum=600) <= 600 for a in range(1, 20))
        assert backoff_delay(1, base=2, maximum=600) <= 2
        assert backoff_delay(2, base=2, maximum=600) <= 4

    def test_jitter_spreads_retries(self):
        """Synchronised retries are how a recovering dependency gets knocked over again."""
        samples = {round(backoff_delay(6, base=2, maximum=600), 4) for _ in range(40)}
        assert len(samples) > 1


class TestEvents:
    def test_events_are_ordered_and_resumable(self, db):
        events = EventRepository(db)
        first = events.emit("run.started", crawl_run_id=1)
        events.emit("game.synced", game_id=10)
        last = events.emit("run.finished", crawl_run_id=1)
        db.commit()

        assert events.latest_id() == last
        # This is exactly what the SSE endpoint does with Last-Event-ID.
        after_first = events.after(first)
        assert [e.event for e in after_first] == ["game.synced", "run.finished"]

    def test_secrets_never_reach_the_event_log(self, db):
        events = EventRepository(db)
        eid = events.emit("llm.failed", data={"api_key": "sk-leak", "model": "claude-sonnet-5"})
        db.commit()
        row = db.execute(sa.select(sa.text("data")).select_from(sa.text("job_events")).where(
            sa.text(f"id = {eid}")
        )).scalar_one()
        assert "sk-leak" not in str(row)

    def test_problem_filter(self, db):
        events = EventRepository(db)
        events.emit("ok", level="info")
        events.emit("bad", level="error")
        events.emit("meh", level="warning")
        db.commit()
        problems = events.recent(level="problems")
        assert {e.event for e in problems} == {"bad", "meh"}


def test_queue_depth_and_workers(db):
    for i in range(3):
        enqueue(db, f"k{i}")
    JobRepository(db).enqueue(job_type="x", idempotency_key="ai1", queue="ai")
    db.commit()
    depth = {(d["queue"], d["status"]): d["count"] for d in JobRepository(db).queue_depth()}
    assert depth[("enrich", "queued")] == 3
    assert depth[("ai", "queued")] == 1

    WorkerRepository(db).heartbeat("w1", queues="enrich,ai", active_jobs=2, version="1.0.0")
    db.commit()
    workers = WorkerRepository(db).all()
    assert workers[0].worker_id == "w1" and workers[0].active_jobs == 2
