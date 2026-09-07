"""Differential update: the behaviour that makes an hourly schedule affordable."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.models import Game, GameCompany, GamePlatform, Job, Platform
from app.db.uow import UnitOfWork
from app.domain.models import ScoreStats
from app.parsers.metacritic import parse_game_detail
from app.services.game_sync import GameSyncService
from tests.fakes import FakeMetacriticSource

pytestmark = pytest.mark.integration

BASE = "https://www.metacritic.com"


def settings(**overrides) -> Settings:
    base = {"admin_token": "t" * 32, "app_env": "test", "youtube_enabled": False}
    base.update(overrides)
    return Settings(**base)


class Provider:
    def __init__(self, source: FakeMetacriticSource) -> None:
        self.source = source
        self.stats_calls: list[tuple[str, str]] = []

    def game_detail(self, slug: str):
        return self.source.game_detail(slug)

    def user_stats(self, slug: str, platform_slug: str) -> ScoreStats:
        self.stats_calls.append((slug, platform_slug))
        from app.domain.enums import ReviewKind

        return self.source.score_stats(slug, platform_slug, kind=ReviewKind.USER)


@pytest.fixture
def elden_ring_provider(composer_elden_ring) -> Provider:
    source = FakeMetacriticSource()
    source.details["elden-ring"] = parse_game_detail(composer_elden_ring, base_url=BASE)
    # Real Cyberpunk-style spread, so the per-platform behaviour is exercised properly.
    from app.domain.enums import ReviewKind

    source.stats[("elden-ring", "playstation-5", ReviewKind.USER.value)] = ScoreStats(
        score=8.4, scale_max=10, review_count=24_375, positive=19_969, neutral=1_572,
        negative=2_834, sentiment="Generally favorable",
    )
    source.stats[("elden-ring", "pc", ReviewKind.USER.value)] = ScoreStats(
        score=7.9, scale_max=10, review_count=12_004, sentiment="Generally favorable",
    )
    source.stats[("elden-ring", "xbox-one", ReviewKind.USER.value)] = ScoreStats(
        score=0, scale_max=10, review_count=2,  # the false-zero shape from Release 0
    )
    return Provider(source)


class TestFirstSync:
    def test_persists_the_whole_game_in_one_go(self, session_factory, db, elden_ring_provider):
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            outcome = service.sync(uow, "elden-ring")

        assert outcome.created is True and outcome.changed is True
        game = db.execute(sa.select(Game).where(Game.mc_slug == "elden-ring")).scalar_one()
        assert game.title == "Elden Ring"
        assert game.mc_title_id == 1_300_501_979
        assert game.esrb_rating == "M"
        assert game.description

    def test_platforms_and_scores_are_normalised(self, session_factory, db, elden_ring_provider):
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")

        rows = db.execute(
            sa.select(Platform.slug, GamePlatform.metascore_raw, GamePlatform.userscore_raw)
            .join(GamePlatform, GamePlatform.platform_id == Platform.id)
        ).all()
        by_slug = {slug: (meta, user) for slug, meta, user in rows}
        assert by_slug["pc"][0] == 94
        assert by_slug["playstation-5"][1] == 8.4
        # Xbox One has no critic score at the source: stored as NULL, never 0.
        assert by_slug["xbox-one"][0] is None

    def test_exactly_one_lead_platform(self, session_factory, db, elden_ring_provider):
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")
        leads = db.execute(
            sa.select(sa.func.count()).select_from(GamePlatform).where(GamePlatform.is_lead)
        ).scalar_one()
        assert leads == 1

    def test_rollups_ignore_missing_scores(self, session_factory, db, elden_ring_provider):
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")
        game = db.execute(sa.select(Game)).scalar_one()
        assert game.best_metascore == 96          # PS5, not a 0 from the unrated platform
        assert game.platform_count == 5
        assert game.lead_platform_id is not None

    def test_companies_get_their_roles(self, session_factory, db, elden_ring_provider):
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")
        roles = db.execute(sa.select(GameCompany.role).distinct()).scalars().all()
        assert set(roles) == {"developer", "publisher"}

    def test_one_stats_call_per_platform(self, session_factory, db, elden_ring_provider):
        """The `1 + N` cost the research measured; ?platform= being ignored forces it."""
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")
        assert len(elden_ring_provider.stats_calls) == 5


class TestRepeatSync:
    def test_an_unchanged_day_creates_no_new_rows(self, session_factory, db, elden_ring_provider):
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")
        before = db.execute(sa.select(sa.func.count()).select_from(GamePlatform)).scalar_one()

        with UnitOfWork(session_factory) as uow:
            second = service.sync(uow, "elden-ring")

        after = db.execute(sa.select(sa.func.count()).select_from(GamePlatform)).scalar_one()
        assert second.created is False
        assert second.changed is False          # <-- the point of the fingerprint
        assert before == after
        assert db.execute(sa.select(sa.func.count()).select_from(Game)).scalar_one() == 1

    def test_unchanged_input_does_not_queue_similarity(
        self, session_factory, db, elden_ring_provider
    ):
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")
        db.execute(sa.delete(Job))
        db.commit()

        with UnitOfWork(session_factory) as uow:
            outcome = service.sync(uow, "elden-ring")

        assert "similarity.recompute" not in outcome.follow_ups
        queued = db.execute(sa.select(Job.job_type).distinct()).scalars().all()
        assert "similarity.recompute" not in queued

    def test_a_moved_score_forces_a_review_resync(
        self, session_factory, db, elden_ring_provider, composer_elden_ring
    ):
        import copy
        from dataclasses import replace

        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")
        # Pretend reviews were just synced, so only a score move can re-trigger them.
        db.execute(sa.update(Game).values(reviews_synced_at=dt.datetime.now(dt.UTC)))
        db.execute(sa.delete(Job))
        db.commit()

        moved = copy.deepcopy(composer_elden_ring)
        product = next(
            c for c in moved["components"] if c["meta"]["componentName"] == "product"
        )
        product["data"]["item"]["platforms"][1]["criticScoreSummary"]["score"] = 61
        elden_ring_provider.source.details["elden-ring"] = parse_game_detail(moved, base_url=BASE)

        with UnitOfWork(session_factory) as uow:
            outcome = service.sync(uow, "elden-ring")

        assert outcome.scores_moved is True
        assert "reviews.sync" in outcome.follow_ups
        assert replace  # keep the import meaningful for readers

    def test_a_renamed_slug_updates_rather_than_duplicates(
        self, session_factory, db, elden_ring_provider, composer_elden_ring
    ):
        import copy

        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring")

        renamed = copy.deepcopy(composer_elden_ring)
        product = next(
            c for c in renamed["components"] if c["meta"]["componentName"] == "product"
        )
        product["data"]["item"]["slug"] = "elden-ring-goty"
        detail = parse_game_detail(renamed, base_url=BASE)
        elden_ring_provider.source.details["elden-ring-goty"] = detail

        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring-goty")

        games = db.execute(sa.select(Game)).scalars().all()
        assert len(games) == 1, "matched on mc_title_id, so no duplicate was created"
        assert games[0].mc_slug == "elden-ring-goty"


class TestResilience:
    def test_a_failing_stats_call_degrades_only_that_platform(
        self, session_factory, db, elden_ring_provider
    ):
        from app.domain.errors import RetryableError

        calls = {"n": 0}
        original = elden_ring_provider.user_stats

        def flaky(slug: str, platform_slug: str):
            calls["n"] += 1
            if platform_slug == "pc":
                raise RetryableError("stats 503")
            return original(slug, platform_slug)

        elden_ring_provider.user_stats = flaky  # type: ignore[method-assign]
        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            outcome = service.sync(uow, "elden-ring")

        assert outcome.stats_failed == 1
        assert outcome.stats_fetched == 4
        # The game itself is still fully persisted.
        assert db.execute(sa.select(sa.func.count()).select_from(Game)).scalar_one() == 1

    def test_crawl_item_is_closed_out(self, session_factory, db, elden_ring_provider):
        from app.db.models import CrawlItem
        from app.domain.enums import CrawlItemStatus, CrawlPhase, RunTrigger

        with UnitOfWork(session_factory) as uow:
            today = dt.date(2026, 9, 6)
            uow.crawl.get_or_create_day(today)
            run = uow.crawl.open_run(
                crawl_date=today, trigger=RunTrigger.SCHEDULE, triggered_by="t",
                phase=CrawlPhase.NEW_RELEASES, worker_id="w",
            )
            claim = uow.crawl.claim_games(
                ["elden-ring"], crawl_date=today, source="new_releases",
                run_id=run.id, lease_ttl_s=1800, budget=1,
            )
            item_id = claim.claimed[0][0]

        service = GameSyncService(elden_ring_provider, settings())
        with UnitOfWork(session_factory) as uow:
            service.sync(uow, "elden-ring", crawl_item_id=item_id, crawl_run_id=run.id)

        item = db.get(CrawlItem, item_id)
        assert item.status == CrawlItemStatus.DONE.value
        assert item.game_id is not None
        assert item.changed is True
        assert item.lease_expires_at is None
