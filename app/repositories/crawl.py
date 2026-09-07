"""Crawl state: day cursor, runs, and the daily claim.

Three independent barriers stop a game being processed twice in a day (ADR-004/006):

1. the row lock taken while opening a run,
2. ``uq_crawl_runs_single_active`` — at most one run in status ``running``,
3. ``uq_crawl_items_daily`` — at most one claim per (date, slug).

Losing any one of them costs efficiency, not correctness.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import CrawlDay, CrawlItem, CrawlRun
from app.domain.enums import CrawlItemStatus, CrawlPhase, RunStatus, RunTrigger
from app.domain.errors import ConflictError
from app.repositories.base import insert_ignore_returning, utc_now


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claimed: list[tuple[int, str]]      # (crawl_item_id, slug)
    skipped_duplicates: int


class CrawlRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ day cursor

    def get_or_create_day(self, crawl_date: dt.date) -> CrawlDay:
        day = self.session.get(CrawlDay, crawl_date)
        if day is not None:
            return day
        insert_ignore_returning(
            self.session,
            CrawlDay.__table__,
            {"crawl_date": crawl_date, "phase": CrawlPhase.NEW_RELEASES.value},
            index_elements=["crawl_date"],
            returning=[CrawlDay.__table__.c.crawl_date],
        )
        self.session.flush()
        day = self.session.get(CrawlDay, crawl_date)
        assert day is not None
        return day

    def update_day(self, day: CrawlDay, **fields: object) -> CrawlDay:
        for key, value in fields.items():
            setattr(day, key, value)
        day.updated_at = utc_now()
        self.session.flush()
        return day

    # ------------------------------------------------------------------ runs

    def open_run(
        self,
        *,
        crawl_date: dt.date,
        trigger: RunTrigger,
        triggered_by: str | None,
        phase: CrawlPhase,
        worker_id: str | None,
        source_params: dict | None = None,
    ) -> CrawlRun:
        """Open a run, or raise ``ConflictError`` if one is already active.

        The uniqueness is enforced by a partial index, so two processes racing here
        cannot both win — one of them gets an IntegrityError and is told to stand down.
        """
        run = CrawlRun(
            crawl_date=crawl_date,
            trigger=trigger.value,
            triggered_by=triggered_by,
            status=RunStatus.RUNNING.value,
            phase_at_start=phase.value,
            worker_id=worker_id,
            source_params=source_params or {},
        )
        self.session.add(run)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            active = self.active_run()
            raise ConflictError(
                f"A crawl run is already active (id={active.id if active else 'unknown'})"
            ) from exc
        return run

    def active_run(self) -> CrawlRun | None:
        return self.session.execute(
            sa.select(CrawlRun).where(CrawlRun.status == RunStatus.RUNNING.value).limit(1)
        ).scalar_one_or_none()

    def finish_run(
        self,
        run: CrawlRun,
        *,
        status: RunStatus,
        error: BaseException | None = None,
        **counters: int,
    ) -> CrawlRun:
        now = utc_now()
        run.status = status.value
        run.finished_at = now
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        if error is not None:
            run.error_class = type(error).__name__
            run.error_message = str(error)[:2000]
        for key, value in counters.items():
            setattr(run, key, value)
        self.session.flush()
        return run

    def recent_runs(self, limit: int = 20) -> list[CrawlRun]:
        return list(
            self.session.execute(
                sa.select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(limit)
            ).scalars()
        )

    def stale_running_runs(self, older_than: dt.datetime) -> list[CrawlRun]:
        """Runs left in ``running`` by a process that died before finishing."""
        return list(
            self.session.execute(
                sa.select(CrawlRun).where(
                    CrawlRun.status == RunStatus.RUNNING.value,
                    CrawlRun.started_at < older_than,
                )
            ).scalars()
        )

    # ------------------------------------------------------------------ daily claim

    def claim_games(
        self,
        slugs: list[str],
        *,
        crawl_date: dt.date,
        source: str,
        run_id: int | None,
        lease_ttl_s: int,
        budget: int,
    ) -> ClaimResult:
        """Claim up to ``budget`` games for ``crawl_date``.

        A returned row means the claim is ours. No row means the game was already taken
        today — by this run, by an earlier hour, or by another worker. That is the entire
        implementation of the assignment's "at most 20 games not yet processed today".
        """
        claimed: list[tuple[int, str]] = []
        duplicates = 0
        lease_until = utc_now() + dt.timedelta(seconds=lease_ttl_s)

        for slug in slugs:
            if len(claimed) >= budget:
                break
            row = insert_ignore_returning(
                self.session,
                CrawlItem.__table__,
                {
                    "crawl_date": crawl_date,
                    "game_slug": slug,
                    "source": source,
                    "crawl_run_id": run_id,
                    "status": CrawlItemStatus.PROCESSING.value,
                    "lease_expires_at": lease_until,
                    "attempts": 1,
                    "claimed_at": utc_now(),
                },
                index_elements=["crawl_date", "game_slug"],
                returning=[CrawlItem.__table__.c.id, CrawlItem.__table__.c.game_slug],
            )
            if row is None:
                duplicates += 1
            else:
                claimed.append((int(row[0]), str(row[1])))

        return ClaimResult(claimed=claimed, skipped_duplicates=duplicates)

    def mark_item(
        self,
        item_id: int,
        *,
        status: CrawlItemStatus,
        game_id: int | None = None,
        changed: bool | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, object] = {
            "status": status.value,
            "finished_at": utc_now(),
            "lease_expires_at": None,
        }
        if game_id is not None:
            values["game_id"] = game_id
        if changed is not None:
            values["changed"] = changed
        if error is not None:
            values["error_message"] = error[:2000]
        self.session.execute(
            sa.update(CrawlItem).where(CrawlItem.id == item_id).values(**values)
        )

    def reap_expired_items(
        self, *, max_attempts: int
    ) -> tuple[list[tuple[int, str, dt.date]], int]:
        """Return expired leases to the queue; give up after ``max_attempts``.

        This is what makes a worker crash a non-event: the claim survives, the lease does
        not, and the next reaper pass re-queues the work idempotently.
        """
        now = utc_now()
        expired = list(
            self.session.execute(
                sa.select(CrawlItem).where(
                    CrawlItem.status == CrawlItemStatus.PROCESSING.value,
                    CrawlItem.lease_expires_at.is_not(None),
                    CrawlItem.lease_expires_at < now,
                )
            ).scalars()
        )
        revived: list[tuple[int, str, dt.date]] = []
        failed = 0
        for item in expired:
            if item.attempts >= max_attempts:
                item.status = CrawlItemStatus.FAILED.value
                item.error_message = "lease expired; attempts exhausted"
                item.finished_at = now
                item.lease_expires_at = None
                failed += 1
            else:
                item.status = CrawlItemStatus.PENDING.value
                item.lease_expires_at = None
                item.error_message = "lease expired"
                revived.append((item.id, item.game_slug, item.crawl_date))
        self.session.flush()
        return revived, failed

    def take_pending_item(self, item_id: int, *, lease_ttl_s: int) -> CrawlItem | None:
        item = self.session.get(CrawlItem, item_id)
        if item is None:
            return None
        item.status = CrawlItemStatus.PROCESSING.value
        item.attempts += 1
        item.lease_expires_at = utc_now() + dt.timedelta(seconds=lease_ttl_s)
        self.session.flush()
        return item

    def counts_for_date(self, crawl_date: dt.date) -> dict[str, int]:
        rows = self.session.execute(
            sa.select(CrawlItem.status, sa.func.count())
            .where(CrawlItem.crawl_date == crawl_date)
            .group_by(CrawlItem.status)
        ).all()
        return {str(status): int(count) for status, count in rows}

    def items_for_run(self, run_id: int) -> dict[str, int]:
        rows = self.session.execute(
            sa.select(CrawlItem.status, sa.func.count())
            .where(CrawlItem.crawl_run_id == run_id)
            .group_by(CrawlItem.status)
        ).all()
        return {str(status): int(count) for status, count in rows}

    def purge_old_items(self, older_than: dt.date) -> int:
        result = self.session.execute(
            sa.delete(CrawlItem).where(CrawlItem.crawl_date < older_than)
        )
        return int(result.rowcount or 0)
