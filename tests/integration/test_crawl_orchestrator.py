"""The crawler scenarios the assignment names, end to end against a real database."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.models import CrawlItem, CrawlRun, Job
from app.db.uow import UnitOfWork
from app.domain.enums import CrawlItemStatus, CrawlPhase, RunStatus, RunTrigger
from app.domain.errors import RetryableError, SchemaDriftError
from app.services.crawl_orchestrator import CrawlOrchestrator
from tests.fakes import FakeMetacriticSource, make_listing

pytestmark = pytest.mark.integration

DAY1 = dt.datetime(2026, 9, 6, 10, 7, tzinfo=dt.UTC)
DAY2 = dt.datetime(2026, 9, 7, 10, 7, tzinfo=dt.UTC)


class Provider:
    """Only the discovery surface the orchestrator needs."""

    def __init__(self, source: FakeMetacriticSource) -> None:
        self.source = source

    def new_releases(self, *, limit: int):
        return self.source.new_releases(limit=limit)

    def browse_new(self, *, page: int, page_size: int):
        return self.source.browse(offset=(page - 1) * page_size, limit=page_size)

    def browse_top(self, *, offset: int, limit: int):
        return self.source.browse(offset=offset, limit=limit, by_score=True)

    def sitemap_shards(self):
        return self.source.sitemap_shards()

    def sitemap_slugs(self, shard_url: str):
        return self.source.sitemap_slugs(shard_url)


def settings(**overrides) -> Settings:
    base = {
        "admin_token": "t" * 32,
        "app_env": "test",
        "crawl_max_games_per_run": 20,
        "new_releases_limit": 20,
        "browse_page_size": 24,
        "youtube_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def uow_of(session_factory) -> UnitOfWork:
    return UnitOfWork(session_factory)


def source_with(new_release_slugs: list[str], browse_pages: dict[int, list[str]] | None = None,
                **kw) -> FakeMetacriticSource:
    source = FakeMetacriticSource(**kw)
    source.listings["new_releases"] = make_listing(new_release_slugs, total=18_524)
    for offset, slugs in (browse_pages or {}).items():
        source.listings[f"browse:{offset}"] = make_listing(slugs, total=18_524, offset=offset)
    return source


class TestNewReleasesPhase:
    def test_claims_the_first_twenty(self, session_factory, db):
        source = source_with([f"game-{i}" for i in range(40)])
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            result = orch.tick(uow)

        assert result.status is RunStatus.SUCCEEDED
        # The assignment's ceiling, not the blueprint's 40 (C-26).
        assert result.claimed == 20
        assert db.execute(sa.select(sa.func.count()).select_from(Job)).scalar_one() == 20

    def test_a_second_tick_the_same_day_adds_nothing(self, session_factory, db):
        """New Releases is done for the day, so the hour spends itself on browse."""
        source = source_with([f"game-{i}" for i in range(20)])
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            first = orch.tick(uow)
        with uow_of(session_factory) as uow:
            second = orch.tick(uow)

        assert first.claimed == 20
        assert second.claimed == 0
        assert db.execute(sa.select(sa.func.count()).select_from(CrawlItem)).scalar_one() == 20
        # And no duplicate work was queued either.
        assert db.execute(sa.select(sa.func.count()).select_from(Job)).scalar_one() == 20

    def test_games_already_claimed_today_are_counted_as_duplicates(self, session_factory, db):
        """The browse page repeats games New Releases already took this run.

        This is the shape the research predicted: the two listings overlap heavily, so
        the duplicate counter is a normal operating signal rather than an error (C-02).
        """
        overlap = [f"game-{i}" for i in range(10)]
        source = source_with(overlap, {0: overlap + [f"fresh-{i}" for i in range(14)]})
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            result = orch.tick(uow)

        assert result.skipped_duplicates == 10
        assert result.claimed == 20  # 10 from New Releases + 10 fresh from browse
        assert db.execute(sa.select(sa.func.count()).select_from(CrawlItem)).scalar_one() == 20

    def test_leftover_budget_spills_into_browse(self, session_factory, db):
        """Fewer than 20 new releases: the rest of the budget is spent on browse."""
        source = source_with(
            ["a", "b", "c"],
            {0: [f"browse-{i}" for i in range(24)]},
        )
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)
        with uow_of(session_factory) as uow:
            result = orch.tick(uow)

        assert result.claimed == 20
        sources = {
            row[0]
            for row in db.execute(sa.select(CrawlItem.source).distinct()).all()
        }
        assert sources == {"new_releases", "browse:page=1"}


class TestBrowsePhase:
    def test_cursor_advances_and_persists(self, session_factory, db):
        source = source_with(
            ["a"],
            {0: [f"p1-{i}" for i in range(24)], 24: [f"p2-{i}" for i in range(24)]},
        )
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)
        with uow_of(session_factory) as uow:
            orch.tick(uow)
            day = uow.crawl.get_or_create_day(DAY1.date())
            assert day.browse_page == 2
            assert day.phase == CrawlPhase.BROWSE.value

    def test_a_page_of_pure_duplicates_still_advances_the_cursor(self, session_factory, db):
        """Otherwise the crawler is trapped on a page of already-known games for ever."""
        repeated = [f"dup-{i}" for i in range(24)]
        source = source_with([], {0: repeated, 24: repeated, 48: ["fresh-1"]})
        cfg = settings(crawl_max_games_per_run=5)
        orch = CrawlOrchestrator(Provider(source), cfg, clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            first = orch.tick(uow)
        assert first.claimed == 5

        with uow_of(session_factory) as uow:
            day = uow.crawl.get_or_create_day(DAY1.date())
            page_after_first = day.browse_page
        assert page_after_first > 1

        with uow_of(session_factory) as uow:
            second = orch.tick(uow)
        with uow_of(session_factory) as uow:
            day = uow.crawl.get_or_create_day(DAY1.date())
        assert day.browse_page > page_after_first
        assert second.skipped_duplicates > 0

    def test_exhaustion_is_recorded(self, session_factory, db):
        source = source_with(["a"], {0: []})
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)
        with uow_of(session_factory) as uow:
            orch.tick(uow)
            day = uow.crawl.get_or_create_day(DAY1.date())
        assert day.browse_exhausted is True
        assert day.phase == CrawlPhase.EXHAUSTED.value


class TestDailyRollover:
    def test_a_new_day_restarts_the_cycle(self, session_factory, db):
        slugs = [f"game-{i}" for i in range(20)]
        source = source_with(slugs)
        day1 = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)
        day2 = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY2)

        with uow_of(session_factory) as uow:
            first = day1.tick(uow)
        with uow_of(session_factory) as uow:
            second = day2.tick(uow)

        assert first.claimed == 20
        # The assignment: "at the start of each new calendar day the cycle starts again".
        assert second.claimed == 20
        assert db.execute(sa.select(sa.func.count()).select_from(CrawlItem)).scalar_one() == 40


class TestFailureHandling:
    def test_source_failure_leaves_the_cursor_untouched(self, session_factory, db):
        source = source_with(["a"], {0: [f"p-{i}" for i in range(24)]})
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            orch.tick(uow)
        with uow_of(session_factory) as uow:
            day = uow.crawl.get_or_create_day(DAY1.date())
            page_before = day.browse_page

        source.fail_with = RetryableError("upstream 503")
        with uow_of(session_factory) as uow:
            result = orch.tick(uow)

        assert result.status is RunStatus.PARTIAL
        with uow_of(session_factory) as uow:
            day = uow.crawl.get_or_create_day(DAY1.date())
        assert day.browse_page == page_before, "a failed fetch must not advance the cursor"

    def test_schema_drift_is_partial_not_fatal(self, session_factory, db):
        source = source_with(["a"])
        source.fail_with = SchemaDriftError("title disappeared")
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)
        with uow_of(session_factory) as uow:
            result = orch.tick(uow)
        assert result.status is RunStatus.PARTIAL
        run = db.execute(sa.select(CrawlRun)).scalars().first()
        assert run.status == RunStatus.PARTIAL.value
        assert run.error_class == "SchemaDriftError"

    def test_a_second_tick_during_an_active_run_is_skipped(self, session_factory, db):
        source = source_with(["a"])
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)
        with uow_of(session_factory) as uow:
            uow.crawl.get_or_create_day(DAY1.date())
            uow.crawl.open_run(
                crawl_date=DAY1.date(), trigger=RunTrigger.MANUAL, triggered_by="someone",
                phase=CrawlPhase.NEW_RELEASES, worker_id="w0",
            )
        with uow_of(session_factory) as uow:
            result = orch.tick(uow)
        assert result.reason == "active_run_exists"
        assert result.run_id is None


class TestBudgetCeiling:
    def test_max_games_can_be_lowered_but_never_raised(self, session_factory, db):
        source = source_with([f"g-{i}" for i in range(100)])
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            lowered = orch.tick(uow, max_games=5)
        assert lowered.claimed == 5

        with uow_of(session_factory) as uow:
            raised = orch.tick(uow, max_games=10_000)
        # Configured ceiling stands: an admin request cannot turn the manual button into
        # a denial-of-service tool against the source (ADR-019 T1).
        assert raised.claimed <= 20


class TestSeedAndReconcile:
    def test_seed_uses_the_same_pipeline_with_a_different_sort(self, session_factory, db):
        source = FakeMetacriticSource()
        source.listings["browse:0"] = make_listing([f"top-{i}" for i in range(24)], total=100)
        source.listings["browse:24"] = make_listing([f"top-{i}" for i in range(24, 48)], total=100)
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            added = orch.seed(uow, limit=30, page_size=24)

        assert added == 30
        by_source = db.execute(
            sa.select(CrawlItem.source, sa.func.count()).group_by(CrawlItem.source)
        ).all()
        assert dict(by_source) == {"seed": 30}
        assert any(call[1].get("by_score") for call in source.calls if call[0] == "browse")

    def test_reconcile_adds_only_unknown_slugs_and_respects_the_cap(self, session_factory, db):
        source = FakeMetacriticSource()
        source.shards = ["https://www.metacritic.com/games/1.xml"]
        source.shard_slugs = {
            "https://www.metacritic.com/games/1.xml": [f"sitemap-{i}" for i in range(50)]
        }
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)

        with uow_of(session_factory) as uow:
            added = orch.reconcile_sitemap(uow, max_new=10)

        assert added == 10
        sources = db.execute(sa.select(CrawlItem.source).distinct()).scalars().all()
        assert sources == ["sitemap"]


class TestReaper:
    def test_expired_items_are_requeued(self, session_factory, db):
        source = source_with(["stuck"])
        orch = CrawlOrchestrator(Provider(source), settings(), clock=lambda: DAY1)
        with uow_of(session_factory) as uow:
            orch.tick(uow)

        db.execute(
            sa.update(CrawlItem).values(
                lease_expires_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            )
        )
        db.commit()

        with uow_of(session_factory) as uow:
            report = orch.reap(uow)
        assert report["items_revived"] == 1

        statuses = db.execute(sa.select(CrawlItem.status)).scalars().all()
        assert statuses == [CrawlItemStatus.PROCESSING.value]
