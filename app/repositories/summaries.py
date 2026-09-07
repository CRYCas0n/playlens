"""Snapshots, summaries and claims.

The two invariants enforced here are the ones Release 0 showed matter most:

* a snapshot is immutable, so a citation can never come to mean a different review;
* exactly one summary per (game platform, audience) is current, and a failed or rejected
  regeneration leaves the previous valid one in place.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    GapExplanation,
    Review,
    ReviewSnapshot,
    SnapshotReview,
    Summary,
    SummaryClaim,
)
from app.domain.enums import Audience, ClaimValidation, ReviewKind, SummaryStatus
from app.repositories.base import utc_now


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    evidence_ref: str
    review_id: int
    position: int


class SnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_hash(
        self, *, game_platform_id: int, kind: ReviewKind, content_hash: str
    ) -> ReviewSnapshot | None:
        return self.session.execute(
            sa.select(ReviewSnapshot).where(
                ReviewSnapshot.game_platform_id == game_platform_id,
                ReviewSnapshot.kind == kind.value,
                ReviewSnapshot.content_hash == content_hash,
            )
        ).scalar_one_or_none()

    def latest(self, *, game_platform_id: int, kind: ReviewKind) -> ReviewSnapshot | None:
        return self.session.execute(
            sa.select(ReviewSnapshot)
            .where(
                ReviewSnapshot.game_platform_id == game_platform_id,
                ReviewSnapshot.kind == kind.value,
            )
            .order_by(ReviewSnapshot.built_at.desc(), ReviewSnapshot.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def create(
        self,
        *,
        game_id: int,
        game_platform_id: int,
        kind: ReviewKind,
        content_hash: str,
        entries: list[SnapshotEntry],
        candidate_count: int,
        rejected: dict[str, int],
        date_min: dt.date | None,
        date_max: dt.date | None,
        ordering_version: int,
        sampling_version: int,
    ) -> ReviewSnapshot:
        """Create, or return the identical existing snapshot.

        Returning the existing row on a hash match is what makes "the corpus has not
        changed" cheap to detect and stops the LLM being paid for the same input twice.
        """
        existing = self.find_by_hash(
            game_platform_id=game_platform_id, kind=kind, content_hash=content_hash
        )
        if existing is not None:
            return existing

        snapshot = ReviewSnapshot(
            game_id=game_id,
            game_platform_id=game_platform_id,
            kind=kind.value,
            content_hash=content_hash,
            ordering_version=ordering_version,
            sampling_version=sampling_version,
            review_count=len(entries),
            candidate_count=candidate_count,
            rejected=rejected,
            date_min=date_min,
            date_max=date_max,
        )
        self.session.add(snapshot)
        self.session.flush()

        self.session.add_all(
            SnapshotReview(
                snapshot_id=snapshot.id,
                evidence_ref=entry.evidence_ref,
                review_id=entry.review_id,
                position=entry.position,
            )
            for entry in entries
        )
        self.session.flush()
        return snapshot

    def load_entries(self, snapshot_id: int) -> list[tuple[str, Review]]:
        rows = self.session.execute(
            sa.select(SnapshotReview)
            .where(SnapshotReview.snapshot_id == snapshot_id)
            .order_by(SnapshotReview.position)
            .options(selectinload(SnapshotReview.review))
        ).scalars()
        return [(row.evidence_ref, row.review) for row in rows]

    def evidence_refs(self, snapshot_id: int) -> set[str]:
        rows = self.session.execute(
            sa.select(SnapshotReview.evidence_ref).where(
                SnapshotReview.snapshot_id == snapshot_id
            )
        ).all()
        return {str(r[0]) for r in rows}

    def overlap(self, a_id: int, b_id: int) -> int:
        a = self.session.execute(
            sa.select(SnapshotReview.review_id).where(SnapshotReview.snapshot_id == a_id)
        ).all()
        b = self.session.execute(
            sa.select(SnapshotReview.review_id).where(SnapshotReview.snapshot_id == b_id)
        ).all()
        return len({r[0] for r in a} & {r[0] for r in b})

    def purge_unreferenced(self, older_than: dt.datetime) -> int:
        referenced = sa.select(Summary.snapshot_id).where(Summary.snapshot_id.is_not(None))
        result = self.session.execute(
            sa.delete(ReviewSnapshot).where(
                ReviewSnapshot.built_at < older_than,
                ReviewSnapshot.id.not_in(referenced),
            )
        )
        return int(result.rowcount or 0)


class SummaryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self, *, game_platform_id: int, audience: Audience) -> Summary | None:
        return self.session.execute(
            sa.select(Summary)
            .where(
                Summary.game_platform_id == game_platform_id,
                Summary.audience == audience.value,
                Summary.is_current.is_(True),
            )
            .options(selectinload(Summary.claims), selectinload(Summary.snapshot))
        ).scalar_one_or_none()

    def by_fingerprint(
        self, *, game_platform_id: int, audience: Audience, fingerprint: str
    ) -> Summary | None:
        return self.session.execute(
            sa.select(Summary).where(
                Summary.game_platform_id == game_platform_id,
                Summary.audience == audience.value,
                Summary.input_fingerprint == fingerprint,
            )
        ).scalar_one_or_none()

    def current_for_game(self, game_id: int) -> list[Summary]:
        return list(
            self.session.execute(
                sa.select(Summary)
                .where(Summary.game_id == game_id, Summary.is_current.is_(True))
                .options(selectinload(Summary.claims), selectinload(Summary.snapshot))
            ).scalars()
        )

    def save(
        self,
        *,
        game_id: int,
        game_platform_id: int,
        audience: Audience,
        snapshot_id: int | None,
        status: SummaryStatus,
        input_fingerprint: str,
        claims: list[dict[str, Any]],
        heading: str | None = None,
        overall: str | None = None,
        aspect_verdicts: dict[str, str] | None = None,
        confidence: float | None = None,
        reviews_used: int = 0,
        reviews_candidates: int = 0,
        score_at_generation: int | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        prompt_version: str | None = None,
        params_version: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        error_message: str | None = None,
    ) -> Summary:
        """Write a new version.

        A ``fresh`` result becomes current and demotes the previous one IN THE SAME
        transaction, so there is never a moment with two current summaries or none.
        A ``rejected`` or ``failed`` result is recorded but does NOT demote anything:
        the user keeps seeing the last summary that passed validation.
        """
        previous = self.current(game_platform_id=game_platform_id, audience=audience)
        version = (previous.version + 1) if previous else 1
        publishable = status is SummaryStatus.FRESH

        if publishable and previous is not None:
            previous.is_current = False
            self.session.flush()

        summary = Summary(
            game_id=game_id,
            game_platform_id=game_platform_id,
            snapshot_id=snapshot_id,
            audience=audience.value,
            heading=heading,
            overall=overall,
            aspect_verdicts=aspect_verdicts or {},
            confidence=confidence,
            status=status.value,
            input_fingerprint=input_fingerprint,
            reviews_used=reviews_used,
            reviews_candidates=reviews_candidates,
            score_at_generation=score_at_generation,
            llm_provider=llm_provider,
            llm_model=llm_model,
            prompt_version=prompt_version,
            params_version=params_version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            error_message=error_message,
            version=version,
            is_current=publishable,
        )
        self.session.add(summary)
        self.session.flush()

        for position, claim in enumerate(claims):
            self.session.add(
                SummaryClaim(
                    summary_id=summary.id,
                    side=claim["side"],
                    aspect=claim["aspect"],
                    claim=claim["claim"],
                    claim_ru=claim.get("claim_ru") or None,
                    claim_type=claim["claim_type"],
                    evidence_refs=claim.get("evidence_refs", []),
                    strength=claim.get("strength"),
                    position=position,
                    validation=claim["validation"],
                    validation_detail=claim.get("validation_detail"),
                )
            )
        self.session.flush()
        return summary

    def aspect_profiles(self, game_ids: list[int]) -> dict[int, dict[str, str]]:
        """The aspect verdicts of every current summary, merged per game.

        This is the similarity engine's semantic signal (ADR-011): a 15-dimensional
        profile the pipeline already produces, instead of a 380 MB embedding stack.
        """
        if not game_ids:
            return {}
        rows = self.session.execute(
            sa.select(Summary.game_id, Summary.aspect_verdicts).where(
                Summary.game_id.in_(game_ids),
                Summary.is_current.is_(True),
                Summary.status == SummaryStatus.FRESH.value,
            )
        ).all()
        profiles: dict[int, dict[str, str]] = {}
        for game_id, verdicts in rows:
            if not verdicts:
                continue
            merged = profiles.setdefault(int(game_id), {})
            # Critic and player summaries both contribute; a stated verdict beats "absent".
            for aspect, verdict in verdicts.items():
                if merged.get(aspect) in (None, "absent"):
                    merged[aspect] = verdict
        return profiles

    def accepted_claims(self, summary_id: int) -> list[SummaryClaim]:
        return list(
            self.session.execute(
                sa.select(SummaryClaim)
                .where(
                    SummaryClaim.summary_id == summary_id,
                    SummaryClaim.validation == ClaimValidation.ACCEPTED.value,
                )
                .order_by(SummaryClaim.side, SummaryClaim.position)
            ).scalars()
        )

    def claim_validation_counts(self, since: dt.datetime) -> dict[str, int]:
        """Quality telemetry: rising rejection rates mean a prompt or model regression."""
        rows = self.session.execute(
            sa.select(SummaryClaim.validation, sa.func.count())
            .join(Summary, Summary.id == SummaryClaim.summary_id)
            .where(Summary.generated_at >= since)
            .group_by(SummaryClaim.validation)
        ).all()
        return {str(k): int(v) for k, v in rows}

    def cost_since(self, since: dt.datetime) -> dict[str, float]:
        row = self.session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(Summary.cost_usd), 0.0),
                sa.func.coalesce(sa.func.sum(Summary.tokens_in), 0),
                sa.func.coalesce(sa.func.sum(Summary.tokens_out), 0),
                sa.func.count(),
            ).where(Summary.generated_at >= since, Summary.llm_model.is_not(None))
        ).first()
        return {
            "cost_usd": float(row[0] or 0),
            "tokens_in": int(row[1] or 0),
            "tokens_out": int(row[2] or 0),
            "calls": int(row[3] or 0),
        }

    def prune_history(self, *, game_platform_id: int, audience: Audience, keep: int) -> int:
        rows = self.session.execute(
            sa.select(Summary.id)
            .where(
                Summary.game_platform_id == game_platform_id,
                Summary.audience == audience.value,
                Summary.is_current.is_(False),
            )
            .order_by(Summary.version.desc())
            .offset(keep)
        ).all()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return 0
        self.session.execute(sa.delete(Summary).where(Summary.id.in_(ids)))
        return len(ids)


class GapRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self, game_platform_id: int) -> GapExplanation | None:
        return self.session.execute(
            sa.select(GapExplanation).where(
                GapExplanation.game_platform_id == game_platform_id,
                GapExplanation.is_current.is_(True),
            )
        ).scalar_one_or_none()

    def save(
        self,
        *,
        game_id: int,
        game_platform_id: int,
        critic_snapshot_id: int | None,
        user_snapshot_id: int | None,
        gap_points: int | None,
        explanation: str | None,
        evidence_refs: list[str],
        status: SummaryStatus,
        llm_model: str | None,
        prompt_version: str | None,
    ) -> GapExplanation:
        previous = self.current(game_platform_id)
        publishable = status is SummaryStatus.FRESH
        if publishable and previous is not None:
            previous.is_current = False
            self.session.flush()
        row = GapExplanation(
            game_id=game_id,
            game_platform_id=game_platform_id,
            critic_snapshot_id=critic_snapshot_id,
            user_snapshot_id=user_snapshot_id,
            gap_points=gap_points,
            explanation=explanation,
            evidence_refs=evidence_refs,
            status=status.value,
            is_current=publishable,
            llm_model=llm_model,
            prompt_version=prompt_version,
            generated_at=utc_now(),
        )
        self.session.add(row)
        self.session.flush()
        return row
