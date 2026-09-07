"""The hourly tick.

Reads as the assignment describes it — New Releases first, then browse pages, restarting
each calendar day, at most 20 games per run — with the three corrections the research
forced (C-02, C-03, C-26).

The tick only ENQUEUES. It never does the heavy work itself, so it finishes in seconds and
a slow game cannot make the schedule drift.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from app.config import Settings
from app.domain.enums import CrawlPhase, CrawlSource, RunStatus, RunTrigger
from app.domain.errors import ConflictError, PermanentError, RetryableError, SchemaDriftError
from app.domain.models import Listing
from app.logging import correlate, get_logger
from app.services.drift import (
    SCHEMA_CIRCUIT_EVENT,
    circuit_already_announced,
    drift_circuit_open,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

log = get_logger(__name__)


class DiscoveryProvider(Protocol):
    def new_releases(self, *, limit: int) -> Listing: ...
    def browse_new(self, *, page: int, page_size: int) -> Listing: ...
    def browse_top(self, *, offset: int, limit: int) -> Listing: ...
    def sitemap_shards(self) -> list[str]: ...
    def sitemap_slugs(self, shard_url: str) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class TickResult:
    run_id: int | None
    status: RunStatus
    claimed: int
    discovered: int
    skipped_duplicates: int
    pages_fetched: int
    phase: CrawlPhase
    reason: str = ""


class CrawlOrchestrator:
    def __init__(
        self,
        provider: DiscoveryProvider,
        settings: Settings,
        *,
        clock: callable[[], dt.datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    def _today(self) -> dt.date:
        tz = ZoneInfo(self._settings.crawl_timezone)
        return self._clock().astimezone(tz).date()

    # ------------------------------------------------------------------ hourly tick

    def tick(
        self,
        uow: UnitOfWork,
        *,
        trigger: RunTrigger = RunTrigger.SCHEDULE,
        triggered_by: str = "scheduler",
        worker_id: str | None = None,
        max_games: int | None = None,
    ) -> TickResult:
        today = self._today()
        day = uow.crawl.get_or_create_day(today)

        # Before anything is fetched: has the parser stopped working entirely?
        stopped = drift_circuit_open(uow, self._settings.schema_drift_threshold)
        if stopped is not None:
            # Once per incident. The counter does not move once the crawl has stopped,
            # so "count == threshold" would re-alert every hour; the question to ask is
            # whether this incident has already been announced.
            if not circuit_already_announced(uow):
                uow.events.emit(SCHEMA_CIRCUIT_EVENT, level="error", message=stopped)
            return TickResult(
                run_id=None,
                status=RunStatus.FAILED,
                claimed=0,
                discovered=0,
                skipped_duplicates=0,
                pages_fetched=0,
                phase=CrawlPhase(day.phase),
                reason="schema_drift_circuit_open",
            )

        # Budget: the assignment's hard ceiling. A caller may lower it, never raise it.
        budget = self._settings.crawl_max_games_per_run
        if max_games is not None:
            budget = max(1, min(budget, max_games))

        try:
            run = uow.crawl.open_run(
                crawl_date=today,
                trigger=trigger,
                triggered_by=triggered_by,
                phase=CrawlPhase(day.phase),
                worker_id=worker_id,
                source_params={
                    "eligibility": "metaScoreMin=1",
                    "budget": budget,
                    "new_releases_limit": self._settings.new_releases_limit,
                    "browse_page_size": self._settings.browse_page_size,
                },
            )
        except ConflictError as exc:
            uow.events.emit("run.skipped", level="warning", message=str(exc))
            return TickResult(
                run_id=None, status=RunStatus.SUCCEEDED, claimed=0, discovered=0,
                skipped_duplicates=0, pages_fetched=0, phase=CrawlPhase(day.phase),
                reason="active_run_exists",
            )

        with correlate(crawl_run_id=run.id, worker_id=worker_id):
            uow.events.emit(
                "run.started", crawl_run_id=run.id, stage="discovery",
                data={"phase": day.phase, "budget": budget, "date": today.isoformat()},
            )
            counters = {"claimed": 0, "discovered": 0, "duplicates": 0, "pages": 0}
            claimed_items: list[tuple[int, str]] = []
            status = RunStatus.SUCCEEDED
            error: Exception | None = None

            try:
                if not day.new_releases_done:
                    claimed_items += self._phase_new_releases(uow, run, day, budget, counters)

                remaining = budget - len(claimed_items)
                if remaining > 0 and not day.browse_exhausted:
                    claimed_items += self._phase_browse(uow, run, day, remaining, counters, today)

            except (RetryableError, SchemaDriftError, PermanentError) as exc:
                # The cursor is NOT advanced. The next tick retries the same page, and the
                # data already in the database is untouched.
                status = RunStatus.PARTIAL
                error = exc
                uow.events.emit(
                    "run.error", level="error", crawl_run_id=run.id, message=str(exc),
                    data={"error_class": type(exc).__name__},
                )

            for item_id, slug in claimed_items:
                uow.jobs.enqueue(
                    job_type="game.sync",
                    idempotency_key=f"game.sync:{today.isoformat()}:{slug}",
                    queue="enrich",
                    crawl_run_id=run.id,
                    payload={"slug": slug, "crawl_item_id": item_id},
                )

            uow.crawl.finish_run(
                run,
                status=status,
                error=error,
                games_discovered=counters["discovered"],
                games_claimed=len(claimed_items),
                games_skipped_dupe=counters["duplicates"],
                pages_fetched=counters["pages"],
                jobs_enqueued=len(claimed_items),
            )
            uow.crawl.update_day(day, games_claimed=day.games_claimed + len(claimed_items))
            uow.events.emit(
                "run.finished", crawl_run_id=run.id,
                data={
                    "status": status.value,
                    "claimed": len(claimed_items),
                    "discovered": counters["discovered"],
                    "skipped_duplicates": counters["duplicates"],
                },
            )

            return TickResult(
                run_id=run.id,
                status=status,
                claimed=len(claimed_items),
                discovered=counters["discovered"],
                skipped_duplicates=counters["duplicates"],
                pages_fetched=counters["pages"],
                phase=CrawlPhase(day.phase),
            )

    # ------------------------------------------------------------------ phases

    def _phase_new_releases(
        self, uow: UnitOfWork, run, day, budget: int, counters: dict[str, int]
    ) -> list[tuple[int, str]]:
        listing = self._provider.new_releases(limit=self._settings.new_releases_limit)
        counters["pages"] += 1
        counters["discovered"] += len(listing.items)

        result = uow.crawl.claim_games(
            [item.slug for item in listing.items],
            crawl_date=day.crawl_date,
            source=CrawlSource.NEW_RELEASES.value,
            run_id=run.id,
            lease_ttl_s=self._settings.crawl_item_lease_ttl_s,
            budget=budget,
        )
        counters["duplicates"] += result.skipped_duplicates
        counters["claimed"] += len(result.claimed)

        uow.crawl.update_day(
            day,
            new_releases_done=True,
            new_releases_seen=len(listing.items),
            phase=CrawlPhase.BROWSE.value,
        )
        uow.events.emit(
            "run.progress", crawl_run_id=run.id, stage="new_releases",
            data={"discovered": len(listing.items), "claimed": len(result.claimed),
                  "duplicates": result.skipped_duplicates},
        )
        return result.claimed

    def _phase_browse(
        self,
        uow: UnitOfWork,
        run,
        day,
        budget: int,
        counters: dict[str, int],
        today: dt.date,
    ) -> list[tuple[int, str]]:
        claimed: list[tuple[int, str]] = []
        page_size = self._settings.browse_page_size

        while len(claimed) < budget and not day.browse_exhausted:
            if self._today() != today:
                # Midnight crossed mid-run. Finish cleanly; the next tick opens a new day.
                uow.events.emit("run.date_rolled", crawl_run_id=run.id, level="warning")
                break

            page = day.browse_page
            listing = self._provider.browse_new(page=page, page_size=page_size)
            counters["pages"] += 1
            counters["discovered"] += len(listing.items)

            if not listing.items:
                uow.crawl.update_day(
                    day, browse_exhausted=True, phase=CrawlPhase.EXHAUSTED.value
                )
                break

            result = uow.crawl.claim_games(
                [item.slug for item in listing.items],
                crawl_date=today,
                source=f"{CrawlSource.BROWSE.value}:page={page}",
                run_id=run.id,
                lease_ttl_s=self._settings.crawl_item_lease_ttl_s,
                budget=budget - len(claimed),
            )
            claimed += result.claimed
            counters["duplicates"] += result.skipped_duplicates
            counters["claimed"] += len(result.claimed)

            # The cursor advances even when every item was a duplicate. Otherwise a page
            # of already-known games traps the crawler on it for ever (C-03).
            dates = [i.release_date for i in listing.items if i.release_date]
            uow.crawl.update_day(
                day,
                browse_page=page + 1,
                browse_offset=page * page_size,
                browse_pages_done=day.browse_pages_done + 1,
                release_date_watermark=min(dates) if dates else day.release_date_watermark,
            )
            uow.events.emit(
                "run.progress", crawl_run_id=run.id, stage="browse",
                data={"page": page, "discovered": len(listing.items),
                      "claimed": len(result.claimed),
                      "duplicates": result.skipped_duplicates},
            )

            if page * page_size >= min(listing.total_results, self._settings.browse_max_offset):
                uow.crawl.update_day(
                    day, browse_exhausted=True, phase=CrawlPhase.EXHAUSTED.value
                )
                break

        return claimed

    # ------------------------------------------------------------------ reconciliation

    def reconcile_sitemap(self, uow: UnitOfWork, *, max_new: int | None = None) -> int:
        """Weekly completeness check against the sitemap.

        Offset pagination on the source is not deterministic — two identical requests
        returned sets differing by 12 slugs out of 24 — so a cursor alone cannot promise a
        complete catalogue. The sitemap is an independent, ordered enumeration (C-03).
        """
        limit = max_new if max_new is not None else self._settings.reconcile_max_new
        today = self._today()
        known = uow.games.known_slugs()
        added = 0

        for shard_url in self._provider.sitemap_shards():
            if added >= limit:
                break
            slugs = [s for s in self._provider.sitemap_slugs(shard_url) if s not in known]
            if not slugs:
                continue
            result = uow.crawl.claim_games(
                slugs,
                crawl_date=today,
                source=CrawlSource.SITEMAP.value,
                run_id=None,
                lease_ttl_s=self._settings.crawl_item_lease_ttl_s,
                budget=limit - added,
            )
            for item_id, slug in result.claimed:
                uow.jobs.enqueue(
                    job_type="game.sync",
                    idempotency_key=f"game.sync:{today.isoformat()}:{slug}",
                    queue="enrich",
                    payload={"slug": slug, "crawl_item_id": item_id},
                    priority=2,  # below the hourly cycle: reconciliation is background work
                )
            added += len(result.claimed)

        uow.events.emit("reconcile.finished", data={"added": added, "limit": limit})
        return added

    # ------------------------------------------------------------------ seed

    def seed(self, uow: UnitOfWork, *, limit: int, page_size: int = 24) -> int:
        """Backfill well-known games (ADR-014).

        The SAME pipeline with a different sort order — not a second write path, and not a
        hardcoded list of titles.
        """
        today = self._today()
        added = 0
        offset = 0

        while added < limit:
            listing = self._provider.browse_top(offset=offset, limit=page_size)
            if not listing.items:
                break
            result = uow.crawl.claim_games(
                [i.slug for i in listing.items],
                crawl_date=today,
                source=CrawlSource.SEED.value,
                run_id=None,
                lease_ttl_s=self._settings.crawl_item_lease_ttl_s,
                budget=limit - added,
            )
            for item_id, slug in result.claimed:
                uow.jobs.enqueue(
                    job_type="game.sync",
                    idempotency_key=f"game.sync:{today.isoformat()}:{slug}",
                    queue="enrich",
                    payload={"slug": slug, "crawl_item_id": item_id},
                    priority=3,
                )
            added += len(result.claimed)
            offset += len(listing.items)
            if offset >= listing.total_results:
                break

        uow.events.emit("seed.finished", data={"added": added, "limit": limit})
        return added

    # ------------------------------------------------------------------ reaper

    def reap(self, uow: UnitOfWork) -> dict[str, int]:
        """Recover work abandoned by a dead worker."""
        revived, failed = uow.crawl.reap_expired_items(
            max_attempts=self._settings.crawl_item_max_attempts
        )
        for item_id, slug, crawl_date in revived:
            uow.crawl.take_pending_item(
                item_id, lease_ttl_s=self._settings.crawl_item_lease_ttl_s
            )
            uow.jobs.enqueue(
                job_type="game.sync",
                idempotency_key=f"game.sync:{crawl_date.isoformat()}:{slug}:retry{item_id}",
                queue="enrich",
                payload={"slug": slug, "crawl_item_id": item_id},
            )
        jobs_revived = uow.jobs.reap_expired_jobs()

        stale_cutoff = self._clock() - dt.timedelta(seconds=self._settings.job_lease_ttl_s * 4)
        stale_runs = uow.crawl.stale_running_runs(stale_cutoff)
        for run in stale_runs:
            uow.crawl.finish_run(
                run, status=RunStatus.FAILED,
                error=RuntimeError("run abandoned; no worker heartbeat"),
            )

        if revived or failed or jobs_revived or stale_runs:
            uow.events.emit(
                "reaper.ran", level="warning" if failed else "info",
                data={"items_revived": len(revived), "items_failed": failed,
                      "jobs_revived": jobs_revived, "runs_closed": len(stale_runs)},
            )
        return {
            "items_revived": len(revived),
            "items_failed": failed,
            "jobs_revived": jobs_revived,
            "runs_closed": len(stale_runs),
        }
