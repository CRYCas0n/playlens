"""Review ingestion.

Two decisions shape this service:

* which platforms are worth collecting for (a five-platform game is ten review streams,
  and most of them are empty);
* how to stop early without missing new reviews, given that ``sort=date`` is NOT strictly
  monotonic at the source — the naive "stop at the first review we already have" is wrong.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.config import Settings
from app.domain.enums import ReviewKind
from app.domain.errors import PermanentError, RetryableError
from app.domain.models import ReviewPage
from app.logging import correlate, get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

log = get_logger(__name__)


class ReviewProvider(Protocol):
    def iter_reviews(
        self,
        slug: str,
        platform_slug: str,
        *,
        kind: ReviewKind,
        cap: int,
        sentiment: str = "all",
        max_pages: int | None = None,
    ) -> Iterator[ReviewPage]: ...

    def critic_stats(self, slug: str, platform_slug: str): ...


@dataclass(frozen=True, slots=True)
class PlatformSyncResult:
    platform_slug: str
    kind: ReviewKind
    inserted: int
    updated: int
    pages: int
    failed: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewSyncResult:
    game_id: int
    results: tuple[PlatformSyncResult, ...]
    platforms_considered: int

    @property
    def inserted(self) -> int:
        return sum(r.inserted for r in self.results)

    @property
    def updated(self) -> int:
        return sum(r.updated for r in self.results)

    @property
    def any_new(self) -> bool:
        return self.inserted > 0 or self.updated > 0


class ReviewSyncService:
    def __init__(self, provider: ReviewProvider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings

    def sync(self, uow: UnitOfWork, *, game_id: int) -> ReviewSyncResult:
        game = uow.games.get(game_id)
        if game is None:
            raise PermanentError(f"game {game_id} disappeared before review sync")

        targets = self._platform_budget(game)
        results: list[PlatformSyncResult] = []

        with correlate(game_id=game_id, slug=game.mc_slug, stage="reviews_fetching"):
            for game_platform in targets:
                platform_slug = game_platform.platform.slug
                for kind in (ReviewKind.CRITIC, ReviewKind.USER):
                    results.append(
                        self._sync_stream(
                            uow,
                            game_id=game_id,
                            slug=game.mc_slug,
                            game_platform_id=game_platform.id,
                            platform_slug=platform_slug,
                            kind=kind,
                            full=self._needs_full_resync(game_platform, kind),
                        )
                    )
                    self._refresh_text_counts(uow, game_platform, kind)

            uow.games.touch(game_id, reviews_synced_at=dt.datetime.now(dt.UTC))
            result = ReviewSyncResult(
                game_id=game_id, results=tuple(results), platforms_considered=len(targets)
            )
            uow.events.emit(
                "reviews.synced",
                game_id=game_id,
                stage="reviews_fetching",
                data={
                    "inserted": result.inserted,
                    "updated": result.updated,
                    "platforms": len(targets),
                    "failures": sum(1 for r in results if r.failed),
                },
            )
            return result

    # ------------------------------------------------------------------ budget

    def _platform_budget(self, game) -> list:
        """Always the lead platform, plus the ones with enough material to be worth it.

        Elden Ring on five platforms would otherwise be ten streams and tens of thousands
        of rows, most of which no summary would ever look at.
        """
        rows = [gp for gp in game.platforms if gp.platform is not None]
        lead = [gp for gp in rows if gp.is_lead]
        others = [
            gp
            for gp in rows
            if not gp.is_lead
            and (
                gp.metascore_count >= self._settings.reviews_min_platform_critics
                or gp.userscore_count >= self._settings.reviews_min_platform_users
            )
        ]
        others.sort(key=lambda gp: gp.metascore_count + gp.userscore_count, reverse=True)
        budget = max(1, self._settings.reviews_max_platforms)
        return (lead + others)[:budget]

    def _needs_full_resync(self, game_platform, kind: ReviewKind) -> bool:
        """Has this stream gone long enough without a deep pass?

        Never synced counts as needing one: the first pass should read everything it is
        allowed to, and only later passes are allowed to be shallow.
        """
        stamp = (
            game_platform.critic_reviews_synced_at
            if kind is ReviewKind.CRITIC
            else game_platform.user_reviews_synced_at
        )
        if stamp is None:
            return True
        age = dt.datetime.now(dt.UTC) - stamp
        return age >= dt.timedelta(days=self._settings.reviews_full_resync_days)

    # ------------------------------------------------------------------ one stream

    def _sync_stream(
        self,
        uow: UnitOfWork,
        *,
        game_id: int,
        slug: str,
        game_platform_id: int,
        platform_slug: str,
        kind: ReviewKind,
        full: bool = False,
    ) -> PlatformSyncResult:
        cap = (
            self._settings.reviews_critic_cap
            if kind is ReviewKind.CRITIC
            else self._settings.reviews_user_cap
        )
        # User reviews are paged newest-first and there can be thousands, so an ordinary
        # pass reads only the first few pages. That alone would mean everything past page
        # five is fetched once and never looked at again -- edits and removals upstream
        # would never reconcile. REVIEWS_FULL_RESYNC_DAYS is how often the cap comes off.
        max_pages = None
        if kind is ReviewKind.USER and not full:
            max_pages = self._settings.reviews_incremental_max_pages

        inserted = updated = pages = 0
        consecutive_stale = 0

        try:
            for page in self._provider.iter_reviews(
                slug, platform_slug, kind=kind, cap=cap, max_pages=max_pages,
            ):
                pages += 1
                outcome = uow.reviews.upsert_batch(
                    list(page.items), game_id=game_id, game_platform_id=game_platform_id
                )
                inserted += outcome.inserted
                updated += outcome.updated

                if outcome.touched == 0:
                    consecutive_stale += 1
                    # Tolerance, because `sort=date` is not strictly monotonic upstream:
                    # observed order began 2026-09-04, 2026-08-30, 2026-08-29, 2026-07-15,
                    # 2026-08-25. Stopping at the first known review would skip new ones.
                    if consecutive_stale >= self._settings.reviews_stale_page_tolerance:
                        break
                else:
                    consecutive_stale = 0

        except (RetryableError, PermanentError) as exc:
            # One stream failing must not fail the game: this is assignment scenario 2,
            # "Game=SUCCESS, Reviews=FAILED".
            log.warning(
                "reviews.stream_failed",
                extra={"slug": slug, "platform": platform_slug, "kind": kind.value,
                       "error": str(exc)},
            )
            uow.events.emit(
                "reviews.stream_failed",
                level="warning",
                game_id=game_id,
                data={"platform": platform_slug, "kind": kind.value, "error": str(exc)},
            )
            return PlatformSyncResult(
                platform_slug=platform_slug, kind=kind, inserted=inserted,
                updated=updated, pages=pages, failed=True, error=str(exc),
            )

        return PlatformSyncResult(
            platform_slug=platform_slug, kind=kind,
            inserted=inserted, updated=updated, pages=pages,
        )

    def _refresh_text_counts(self, uow: UnitOfWork, game_platform, kind: ReviewKind) -> None:
        """Store reviews-with-text separately from ratings.

        The source aggregates its score over ratings INCLUDING those with no text (4 384
        vs 1 790 on one platform). Conflating the two would let the UI claim a summary was
        built from four thousand reviews when it saw eighteen hundred.
        """
        count = uow.reviews.count_with_text(
            game_platform_id=game_platform.id,
            kind=kind,
            min_chars=self._settings.corpus_min_chars,
        )
        now = dt.datetime.now(dt.UTC)
        if kind is ReviewKind.CRITIC:
            game_platform.critic_reviews_with_text = count
            game_platform.critic_reviews_synced_at = now
        else:
            game_platform.user_reviews_with_text = count
            game_platform.user_reviews_synced_at = now
        uow.flush()
