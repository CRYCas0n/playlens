"""Deduplication and run-uniqueness — the guarantees the assignment asks for by name."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from app.db.models import CrawlItem
from app.domain.enums import CrawlItemStatus, CrawlPhase, RunStatus, RunTrigger
from app.domain.errors import ConflictError
from app.repositories.crawl import CrawlRepository

pytestmark = pytest.mark.integration

TODAY = dt.date(2026, 9, 6)
YESTERDAY = dt.date(2026, 9, 5)


def _repo(session) -> CrawlRepository:
    return CrawlRepository(session)


def _open_run(session, date=TODAY):
    repo = _repo(session)
    repo.get_or_create_day(date)
    return repo.open_run(
        crawl_date=date,
        trigger=RunTrigger.SCHEDULE,
        triggered_by="test",
        phase=CrawlPhase.NEW_RELEASES,
        worker_id="w1",
    )


class TestDailyClaim:
    def test_claim_returns_only_new_games(self, db):
        repo = _repo(db)
        run = _open_run(db)
        first = repo.claim_games(
            ["elden-ring", "cyberpunk-2077"],
            crawl_date=TODAY, source="new_releases", run_id=run.id,
            lease_ttl_s=1800, budget=20,
        )
        assert len(first.claimed) == 2
        assert first.skipped_duplicates == 0

        second = repo.claim_games(
            ["elden-ring", "baldurs-gate-3"],
            crawl_date=TODAY, source="new_releases", run_id=run.id,
            lease_ttl_s=1800, budget=20,
        )
        assert [slug for _id, slug in second.claimed] == ["baldurs-gate-3"]
        assert second.skipped_duplicates == 1

    def test_budget_is_respected(self, db):
        """The assignment's hard limit: at most 20 games per hourly run (C-26)."""
        repo = _repo(db)
        run = _open_run(db)
        slugs = [f"game-{i}" for i in range(50)]
        result = repo.claim_games(
            slugs, crawl_date=TODAY, source="browse:page=1", run_id=run.id,
            lease_ttl_s=1800, budget=20,
        )
        assert len(result.claimed) == 20
        assert db.execute(sa.select(sa.func.count()).select_from(CrawlItem)).scalar_one() == 20

    def test_a_new_day_starts_over(self, db):
        repo = _repo(db)
        run_y = _open_run(db, YESTERDAY)
        repo.claim_games(["elden-ring"], crawl_date=YESTERDAY, source="new_releases",
                         run_id=run_y.id, lease_ttl_s=1800, budget=20)
        repo.finish_run(run_y, status=RunStatus.SUCCEEDED)
        db.commit()

        run_t = _open_run(db, TODAY)
        result = repo.claim_games(["elden-ring"], crawl_date=TODAY, source="new_releases",
                                  run_id=run_t.id, lease_ttl_s=1800, budget=20)
        assert len(result.claimed) == 1, "a new calendar day must restart the cycle"

    def test_concurrent_claims_yield_exactly_one_row(self, session_factory, db):
        """Eight threads, one slug. Real parallelism, not a mock.

        This is the test that proves the guarantee is the database's and not the
        application's: no lock is taken anywhere in this path.
        """
        run = _open_run(db)
        db.commit()
        run_id = run.id

        def claim() -> int:
            session = session_factory()
            try:
                result = CrawlRepository(session).claim_games(
                    ["contested-game"], crawl_date=TODAY, source="new_releases",
                    run_id=run_id, lease_ttl_s=1800, budget=1,
                )
                session.commit()
                return len(result.claimed)
            except Exception:
                session.rollback()
                return 0
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            winners = sum(pool.map(lambda _: claim(), range(8)))

        assert winners == 1
        count = db.execute(
            sa.select(sa.func.count()).select_from(CrawlItem).where(
                CrawlItem.game_slug == "contested-game"
            )
        ).scalar_one()
        assert count == 1


class TestRunUniqueness:
    def test_second_active_run_is_refused(self, db):
        _open_run(db)
        db.commit()
        with pytest.raises(ConflictError, match="already active"):
            _open_run(db)

    def test_a_finished_run_frees_the_slot(self, db):
        repo = _repo(db)
        run = _open_run(db)
        repo.finish_run(run, status=RunStatus.SUCCEEDED)
        db.commit()
        second = _open_run(db)
        assert second.id != run.id

    def test_active_run_is_discoverable(self, db):
        run = _open_run(db)
        db.commit()
        assert _repo(db).active_run().id == run.id


class TestReaper:
    def test_expired_lease_returns_to_pending(self, db):
        repo = _repo(db)
        run = _open_run(db)
        result = repo.claim_games(["stuck-game"], crawl_date=TODAY, source="new_releases",
                                  run_id=run.id, lease_ttl_s=1800, budget=5)
        item_id = result.claimed[0][0]
        db.execute(
            sa.update(CrawlItem).where(CrawlItem.id == item_id).values(
                lease_expires_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            )
        )
        db.flush()

        revived, failed = repo.reap_expired_items(max_attempts=3)
        assert [slug for _i, slug, _d in revived] == ["stuck-game"]
        assert failed == 0
        assert db.get(CrawlItem, item_id).status == CrawlItemStatus.PENDING.value

    def test_attempts_exhausted_becomes_failed(self, db):
        repo = _repo(db)
        run = _open_run(db)
        result = repo.claim_games(["doomed"], crawl_date=TODAY, source="new_releases",
                                  run_id=run.id, lease_ttl_s=1800, budget=5)
        item_id = result.claimed[0][0]
        db.execute(
            sa.update(CrawlItem).where(CrawlItem.id == item_id).values(
                lease_expires_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC), attempts=3
            )
        )
        db.flush()

        revived, failed = repo.reap_expired_items(max_attempts=3)
        assert revived == []
        assert failed == 1
        assert db.get(CrawlItem, item_id).status == CrawlItemStatus.FAILED.value

    def test_a_live_lease_is_left_alone(self, db):
        repo = _repo(db)
        run = _open_run(db)
        repo.claim_games(["healthy"], crawl_date=TODAY, source="new_releases",
                         run_id=run.id, lease_ttl_s=1800, budget=5)
        db.flush()
        revived, failed = repo.reap_expired_items(max_attempts=3)
        assert (revived, failed) == ([], 0)


class TestDayCursor:
    def test_cursor_is_persisted_and_survives_a_new_session(self, db, session_factory):
        repo = _repo(db)
        day = repo.get_or_create_day(TODAY)
        repo.update_day(day, browse_page=37, browse_offset=864, phase=CrawlPhase.BROWSE.value)
        db.commit()

        other = session_factory()
        try:
            reloaded = CrawlRepository(other).get_or_create_day(TODAY)
            assert reloaded.browse_page == 37
            assert reloaded.phase == CrawlPhase.BROWSE.value
        finally:
            other.close()

    def test_get_or_create_is_idempotent(self, db):
        repo = _repo(db)
        a = repo.get_or_create_day(TODAY)
        db.commit()
        b = repo.get_or_create_day(TODAY)
        assert a.crawl_date == b.crawl_date


def test_counts_for_date(db):
    repo = _repo(db)
    run = _open_run(db)
    result = repo.claim_games(["a", "b", "c"], crawl_date=TODAY, source="new_releases",
                              run_id=run.id, lease_ttl_s=1800, budget=10)
    repo.mark_item(result.claimed[0][0], status=CrawlItemStatus.DONE)
    db.flush()
    counts = repo.counts_for_date(TODAY)
    assert counts[CrawlItemStatus.DONE.value] == 1
    assert counts[CrawlItemStatus.PROCESSING.value] == 2
