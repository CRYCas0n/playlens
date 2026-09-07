"""`REVIEWS_FULL_RESYNC_DAYS` was declared and unread.

User reviews are paged newest-first and there can be thousands of them, so an ordinary
pass reads only the first few pages. Without a periodic deep pass, everything past page
five is fetched once and never looked at again: edits, deletions and score changes
upstream would never reconcile, and nothing would ever say so.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.config import Settings
from app.db.models import GamePlatform
from app.domain.enums import ReviewKind
from app.services.review_sync import ReviewSyncService

pytestmark = pytest.mark.integration


def service(**overrides) -> ReviewSyncService:
    base = {
        "admin_token": "t" * 32,
        "app_env": "test",
        "reviews_full_resync_days": 30,
        "reviews_incremental_max_pages": 5,
    }
    base.update(overrides)
    from tests.fakes import FakeMetacriticSource

    return ReviewSyncService(FakeMetacriticSource(), Settings(**base))


def platform_with(stamp: dt.datetime | None) -> GamePlatform:
    row = GamePlatform(game_id=1, platform_id=1)
    row.critic_reviews_synced_at = stamp
    row.user_reviews_synced_at = stamp
    return row


class TestWhenAFullPassIsDue:
    def test_a_stream_never_synced_gets_a_full_pass(self):
        """The first pass should read everything it is allowed to."""
        assert service()._needs_full_resync(platform_with(None), ReviewKind.USER) is True

    def test_a_recent_pass_stays_shallow(self):
        recent = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
        assert service()._needs_full_resync(platform_with(recent), ReviewKind.USER) is False

    def test_an_old_pass_goes_deep_again(self):
        old = dt.datetime.now(dt.UTC) - dt.timedelta(days=31)
        assert service()._needs_full_resync(platform_with(old), ReviewKind.USER) is True

    def test_the_boundary_is_inclusive(self):
        exact = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        assert service()._needs_full_resync(platform_with(exact), ReviewKind.USER) is True

    def test_the_interval_is_configurable(self):
        old = dt.datetime.now(dt.UTC) - dt.timedelta(days=10)
        assert service(reviews_full_resync_days=7)._needs_full_resync(
            platform_with(old), ReviewKind.USER
        ) is True
        assert service(reviews_full_resync_days=90)._needs_full_resync(
            platform_with(old), ReviewKind.USER
        ) is False

    def test_each_stream_is_judged_on_its_own_stamp(self):
        row = GamePlatform(game_id=1, platform_id=1)
        row.critic_reviews_synced_at = dt.datetime.now(dt.UTC)
        row.user_reviews_synced_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=40)
        svc = service()
        assert svc._needs_full_resync(row, ReviewKind.CRITIC) is False
        assert svc._needs_full_resync(row, ReviewKind.USER) is True


class TestTheCapIsActuallyLifted:
    """The decision above is worthless if the page cap is applied anyway."""

    def _pages_requested(self, *, full: bool, kind: ReviewKind) -> object:
        seen = {}

        class Recorder:
            def iter_reviews(self, slug, platform_slug, *, kind, cap, max_pages):
                seen["max_pages"] = max_pages
                return iter(())

        svc = service()
        svc._provider = Recorder()

        class NullUow:
            class reviews:
                @staticmethod
                def upsert_batch(*a, **k):
                    raise AssertionError("unreachable: the recorder yields nothing")

            class events:
                @staticmethod
                def emit(*a, **k):
                    return 0

        svc._sync_stream(
            NullUow(),
            game_id=1,
            slug="ashen-veil",
            game_platform_id=1,
            platform_slug="pc",
            kind=kind,
            full=full,
        )
        return seen["max_pages"]

    def test_an_incremental_user_pass_is_capped(self):
        assert self._pages_requested(full=False, kind=ReviewKind.USER) == 5

    def test_a_full_user_pass_has_no_cap(self):
        assert self._pages_requested(full=True, kind=ReviewKind.USER) is None

    def test_critic_passes_are_never_capped(self):
        """There are tens of them, not thousands; reading all of them is always cheap."""
        assert self._pages_requested(full=False, kind=ReviewKind.CRITIC) is None


def test_the_stamp_is_written_so_the_next_decision_can_be_made(db, uow, load_harvest):
    """A resync interval needs something to measure from."""
    _game_id, gp_id = load_harvest("nba-2k27", "playstation-5")
    row = db.execute(sa.select(GamePlatform).where(GamePlatform.id == gp_id)).scalar_one()
    assert hasattr(row, "critic_reviews_synced_at")
    assert hasattr(row, "user_reviews_synced_at")
