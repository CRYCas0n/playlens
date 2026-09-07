"""Differential game synchronisation.

The point of this service is what it does NOT do. A game is re-claimed every day
(the assignment requires the daily cycle to restart), but a day on which nothing changed
must cost one HTTP request and zero LLM calls — otherwise the hourly schedule spends real
money re-summarising the same 18 games for ever (C-02).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from app.config import Settings
from app.domain.enums import CrawlItemStatus
from app.domain.errors import PermanentError, RetryableError
from app.domain.models import GameDetail, ScoreStats
from app.logging import correlate, get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

log = get_logger(__name__)


class ProviderLike(Protocol):
    def game_detail(self, slug: str) -> GameDetail: ...
    def user_stats(self, slug: str, platform_slug: str) -> ScoreStats: ...


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    game_id: int
    slug: str
    created: bool
    changed: bool
    scores_moved: bool
    platforms: int
    stats_fetched: int
    stats_failed: int
    follow_ups: tuple[str, ...]


class GameSyncService:
    def __init__(
        self,
        provider: ProviderLike,
        settings: Settings,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._now = now

    def _utcnow(self) -> dt.datetime:
        return self._now or dt.datetime.now(dt.UTC)

    def sync(
        self,
        uow: UnitOfWork,
        slug: str,
        *,
        crawl_item_id: int | None = None,
        crawl_run_id: int | None = None,
    ) -> SyncOutcome:
        with correlate(slug=slug, crawl_run_id=crawl_run_id, stage="fetching"):
            detail = self._provider.game_detail(slug)

            existing = uow.games.find(mc_title_id=detail.mc_title_id, slug=slug)
            previous_scores = self._score_map(existing)
            previous_fingerprint = existing.source_fingerprint if existing else None
            previous_reviews_synced = existing.reviews_synced_at if existing else None

            detail, fetched, failed = self._attach_user_stats(detail, slug)

            # ONE transaction for game + platforms + genres + companies + rollups.
            # Anything less allows a game with no platforms, or rollups that disagree
            # with the rows they summarise (FINAL_SPEC section 23).
            outcome = uow.games.upsert(detail)
            if outcome.slug_changed:
                log.warning(
                    "game.slug_changed",
                    extra={"old_slug": outcome.slug_changed, "slug": slug},
                )
                uow.events.emit(
                    "game.slug_changed",
                    level="warning",
                    game_id=outcome.game_id,
                    data={"old": outcome.slug_changed, "new": slug},
                )

            changed = outcome.created or previous_fingerprint != detail.source_fingerprint
            scores_moved = self._scores_moved(previous_scores, detail)

            if crawl_item_id is not None:
                uow.crawl.mark_item(
                    crawl_item_id,
                    status=CrawlItemStatus.DONE,
                    game_id=outcome.game_id,
                    changed=changed,
                )

            follow_ups = self._dispatch(
                uow,
                game_id=outcome.game_id,
                slug=slug,
                changed=changed,
                scores_moved=scores_moved,
                created=outcome.created,
                previous_reviews_synced=previous_reviews_synced,
                crawl_run_id=crawl_run_id,
            )

            uow.events.emit(
                "game.synced",
                game_id=outcome.game_id,
                crawl_run_id=crawl_run_id,
                stage="persisted",
                data={
                    "slug": slug,
                    "created": outcome.created,
                    "changed": changed,
                    "scores_moved": scores_moved,
                    "platforms": len(detail.platforms),
                    "follow_ups": list(follow_ups),
                },
            )

            return SyncOutcome(
                game_id=outcome.game_id,
                slug=slug,
                created=outcome.created,
                changed=changed,
                scores_moved=scores_moved,
                platforms=len(detail.platforms),
                stats_fetched=fetched,
                stats_failed=failed,
                follow_ups=follow_ups,
            )

    # ------------------------------------------------------------------ per-platform

    def _attach_user_stats(self, detail: GameDetail, slug: str) -> tuple[GameDetail, int, int]:
        """Fetch the per-platform Userscore.

        The composer response carries per-platform Metascore but no Userscore, and
        ``?platform=`` is ignored, so this is the only way to get the number the product's
        killer feature is built on (C-04). One request per platform; a failure on one
        platform degrades that row, not the game.
        """
        updated: list = []
        fetched = failed = 0
        for platform in detail.platforms:
            try:
                stats = self._provider.user_stats(slug, platform.slug)
            except (RetryableError, PermanentError) as exc:
                failed += 1
                log.warning(
                    "game.user_stats_failed",
                    extra={"slug": slug, "platform": platform.slug, "error": str(exc)},
                )
                updated.append(platform)
                continue
            fetched += 1
            updated.append(
                replace(
                    platform,
                    userscore=stats.score,
                    userscore_count=stats.review_count,
                    userscore_positive=stats.positive,
                    userscore_neutral=stats.neutral,
                    userscore_negative=stats.negative,
                    user_sentiment=stats.sentiment,
                )
            )
        return replace(detail, platforms=tuple(updated)), fetched, failed

    @staticmethod
    def _score_map(game) -> dict[str, tuple[int | None, float | None]]:
        if game is None:
            return {}
        return {
            gp.platform.slug: (gp.metascore_raw, gp.userscore_raw)
            for gp in game.platforms
            if gp.platform is not None
        }

    @staticmethod
    def _scores_moved(previous: dict[str, tuple], detail: GameDetail) -> bool:
        for platform in detail.platforms:
            before = previous.get(platform.slug)
            if before is None:
                # A brand-new platform row only counts as movement if it carries a score.
                if platform.metascore is not None or platform.userscore is not None:
                    return True
                continue
            if before[0] != platform.metascore or before[1] != platform.userscore:
                return True
        return False

    # ------------------------------------------------------------------ dispatch

    def _dispatch(
        self,
        uow: UnitOfWork,
        *,
        game_id: int,
        slug: str,
        changed: bool,
        scores_moved: bool,
        created: bool,
        previous_reviews_synced: dt.datetime | None,
        crawl_run_id: int | None,
    ) -> tuple[str, ...]:
        """Queue only the downstream work that can produce a different answer."""
        now = self._utcnow()
        today = now.date().isoformat()
        queued: list[str] = []

        reviews_stale = (
            created
            or previous_reviews_synced is None
            or scores_moved
            or (now - previous_reviews_synced)
            > dt.timedelta(hours=self._settings.reviews_min_interval_h)
        )
        def queue(job_type: str, key: str, **extra: object) -> None:
            if uow.jobs.enqueue(
                job_type=job_type,
                idempotency_key=key,
                queue="enrich",
                game_id=game_id,
                # game_id belongs in the payload as well as the column. Every handler
                # downstream reads payload["game_id"]; the column is how the job is
                # indexed, not how it is read. Sending only the slug meant every
                # follow-up this method queued died with KeyError and took its retries
                # with it -- reviews, similarity and YouTube all silently dead behind a
                # crawl that reported success.
                payload={"slug": slug, "game_id": game_id},
                **extra,  # type: ignore[arg-type]
            ).created:
                queued.append(job_type)

        if self._settings.reviews_enabled and reviews_stale:
            queue(
                "reviews.sync",
                f"reviews.sync:{today}:{game_id}",
                crawl_run_id=crawl_run_id,
            )

        if changed:
            queue("similarity.recompute", f"similarity:{game_id}:{today}")

        if self._settings.youtube_enabled:
            game = uow.games.get(game_id)
            if game is not None and game.youtube_searched_at is None:
                # One search per game for the lifetime of that game: search.list costs
                # 100 quota units out of a daily 10 000 (ADR-016).
                queue("youtube.discover", f"youtube.discover:{game_id}")

        return tuple(queued)
