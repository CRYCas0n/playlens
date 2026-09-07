"""Review persistence.

The interesting part is telling insertions from updates without PostgreSQL's ``xmax``
trick (ADR-005/018). It matters because "how many reviews are genuinely new" is the
input to the decision of whether to spend money on a new summary — an inflated count
means paying for a regenerated summary that says the same thing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import Review
from app.domain.enums import ReviewKind
from app.domain.models import ReviewRecord
from app.repositories.base import insert_many_ignore, utc_now


@dataclass(frozen=True, slots=True)
class UpsertReviewsResult:
    inserted: int
    updated: int
    unchanged: int

    @property
    def touched(self) -> int:
        return self.inserted + self.updated


class ReviewRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_batch(
        self,
        records: list[ReviewRecord],
        *,
        game_id: int,
        game_platform_id: int,
    ) -> UpsertReviewsResult:
        if not records:
            return UpsertReviewsResult(0, 0, 0)

        # Deduplicate inside the batch: the source can repeat a review across pages when
        # its ordering shifts under us, and a batch with duplicate keys aborts the insert.
        by_key: dict[tuple[str, str], ReviewRecord] = {}
        for record in records:
            by_key[(record.kind.value, record.dedupe_key)] = record
        unique = list(by_key.values())

        now = utc_now()
        rows = [
            {
                "game_id": game_id,
                "game_platform_id": game_platform_id,
                "kind": r.kind.value,
                "source_review_id": r.source_review_id,
                "dedupe_key": r.dedupe_key,
                "author": r.author,
                "publication_name": r.publication_name,
                "publication_slug": r.publication_slug,
                "score": r.score,
                "score_max": r.score_max,
                "score_normalized": r.score_normalized,
                "body": r.body,
                "body_hash": r.body_hash,
                "char_count": r.char_count,
                "language_hint": r.language_hint,
                "url": r.url,
                "published_on": r.published_on,
                "is_spoiler": r.is_spoiler,
                "source_version": r.source_version,
                "thumbs_up": r.thumbs_up,
                "thumbs_down": r.thumbs_down,
                "first_seen_at": now,
                "created_at": now,
                "updated_at": now,
            }
            for r in unique
        ]

        inserted_rows = insert_many_ignore(
            self.session,
            Review.__table__,
            rows,
            index_elements=["game_platform_id", "kind", "dedupe_key"],
            returning=[Review.__table__.c.dedupe_key],
        )
        inserted_keys = {str(r[0]) for r in inserted_rows}

        updated = 0
        for record in unique:
            if record.dedupe_key in inserted_keys:
                continue
            # Only touch a row whose CONTENT actually changed. Without the guard,
            # updated_at churns on every pass and "new since last summary" becomes noise.
            result = self.session.execute(
                sa.update(Review)
                .where(
                    Review.game_platform_id == game_platform_id,
                    Review.kind == record.kind.value,
                    Review.dedupe_key == record.dedupe_key,
                    sa.or_(
                        Review.body_hash.is_distinct_from(record.body_hash),
                        Review.source_version.is_distinct_from(record.source_version),
                    ),
                )
                .values(
                    body=record.body,
                    body_hash=record.body_hash,
                    char_count=record.char_count,
                    score=record.score,
                    score_normalized=record.score_normalized,
                    thumbs_up=record.thumbs_up,
                    thumbs_down=record.thumbs_down,
                    # An empty url from the source must not erase one we already have.
                    url=sa.func.coalesce(sa.literal(record.url or None), Review.url),
                    source_version=record.source_version,
                    updated_at=now,
                )
            )
            updated += int(result.rowcount or 0)

        self.session.flush()
        return UpsertReviewsResult(
            inserted=len(inserted_keys),
            updated=updated,
            unchanged=len(unique) - len(inserted_keys) - updated,
        )

    # ------------------------------------------------------------------ reads

    def corpus_candidates(
        self, *, game_platform_id: int, kind: ReviewKind, min_chars: int
    ) -> list[Review]:
        """Reviews eligible to enter a snapshot: right platform, right audience, has text."""
        return list(
            self.session.execute(
                sa.select(Review)
                .where(
                    Review.game_platform_id == game_platform_id,
                    Review.kind == kind.value,
                    Review.body.is_not(None),
                    Review.char_count >= min_chars,
                )
                .order_by(Review.published_on.asc(), Review.dedupe_key.asc())
            ).scalars()
        )

    def count(self, *, game_platform_id: int, kind: ReviewKind) -> int:
        return int(
            self.session.execute(
                sa.select(sa.func.count())
                .select_from(Review)
                .where(Review.game_platform_id == game_platform_id, Review.kind == kind.value)
            ).scalar_one()
        )

    def count_with_text(self, *, game_platform_id: int, kind: ReviewKind, min_chars: int) -> int:
        return int(
            self.session.execute(
                sa.select(sa.func.count())
                .select_from(Review)
                .where(
                    Review.game_platform_id == game_platform_id,
                    Review.kind == kind.value,
                    Review.body.is_not(None),
                    Review.char_count >= min_chars,
                )
            ).scalar_one()
        )

    def newest_first(
        self, *, game_id: int, kind: ReviewKind, limit: int = 20, offset: int = 0
    ) -> tuple[list[Review], int]:
        base = sa.select(Review).where(Review.game_id == game_id, Review.kind == kind.value)
        total = self.session.execute(
            sa.select(sa.func.count()).select_from(base.subquery())
        ).scalar_one()
        rows = list(
            self.session.execute(
                base.order_by(Review.published_on.desc(), Review.id.desc())
                .limit(limit)
                .offset(offset)
            ).scalars()
        )
        return rows, int(total)

    def total_count(self) -> int:
        return int(
            self.session.execute(sa.select(sa.func.count()).select_from(Review)).scalar_one()
        )

    def by_ids(self, ids: list[int]) -> dict[int, Review]:
        if not ids:
            return {}
        rows = self.session.execute(sa.select(Review).where(Review.id.in_(ids))).scalars()
        return {row.id: row for row in rows}

    def date_range(self, *, game_platform_id: int, kind: ReviewKind) -> tuple[Any, Any]:
        row = self.session.execute(
            sa.select(sa.func.min(Review.published_on), sa.func.max(Review.published_on)).where(
                Review.game_platform_id == game_platform_id, Review.kind == kind.value
            )
        ).first()
        return (row[0], row[1]) if row else (None, None)

    def new_since(self, *, game_platform_id: int, kind: ReviewKind, since: dt.datetime) -> int:
        return int(
            self.session.execute(
                sa.select(sa.func.count())
                .select_from(Review)
                .where(
                    Review.game_platform_id == game_platform_id,
                    Review.kind == kind.value,
                    Review.first_seen_at > since,
                )
            ).scalar_one()
        )
