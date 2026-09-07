"""Building an immutable review snapshot.

Release 0 rebuilt a corpus, the numbering shifted, and four citations pointed at
non-existent reviews while the rest quietly pointed at DIFFERENT ones. The second half of
that sentence is why a checksum was not enough: a citation must be scoped to the exact set
and order it was written against (ADR-007).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.ai.corpus import (
    CorpusItem,
    content_hash,
    evidence_ref,
    filter_corpus,
    order_deterministically,
    render_corpus,
    stratified_sample,
)
from app.config import Settings
from app.domain.enums import ReviewKind
from app.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

log = get_logger(__name__)

REF_PREFIX = {ReviewKind.CRITIC: "C", ReviewKind.USER: "U"}


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    snapshot_id: int
    content_hash: str
    review_count: int
    candidate_count: int
    rejected: dict[str, int]
    created: bool
    entries: tuple[tuple[str, CorpusItem], ...]

    def rendered(self, *, max_review_chars: int) -> str:
        return render_corpus(list(self.entries), max_review_chars=max_review_chars)


#: Characters per token for English prose. Real tokenisers land between 3.5 and 4.5; the
#: low end is used deliberately, because underestimating means a smaller corpus and
#: overestimating means a request that fails.
CHARS_PER_TOKEN = 3.5

#: Share of the input budget the review corpus may occupy. The rest is the instructions,
#: the game's metadata and headroom for the model's own reply.
PROMPT_BUDGET_SHARE = 0.75

#: ``[C03] 88 | 2026-02-14 | (Publication) | `` and the newline around it.
CORPUS_LINE_OVERHEAD_CHARS = 60


class SnapshotService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def target_size(self, kind: ReviewKind, *, items: list[CorpusItem] | None = None) -> int:
        """How many reviews to sample: the smaller of the cap and what the context fits.

        The cap alone is not enough. 500 user reviews at up to 1500 characters each is
        750,000 characters -- roughly 210,000 tokens, more than the model's whole context
        window. The request would not be expensive, it would simply always fail, and it
        would fail exactly on the popular games where a summary matters most.

        ``items`` lets the estimate use the corpus at hand instead of the worst case:
        most reviews are far shorter than the per-review ceiling, and budgeting as if
        every one were maximal would discard evidence for no reason.
        """
        cap = (
            self._settings.reviews_critic_cap
            if kind is ReviewKind.CRITIC
            else self._settings.reviews_user_cap
        )
        if items:
            lengths = [min(len(i.body), self._settings.corpus_max_review_chars) for i in items]
            per_review = sum(lengths) / len(lengths)
        else:
            per_review = float(self._settings.corpus_max_review_chars)
        per_review = max(1.0, per_review + CORPUS_LINE_OVERHEAD_CHARS)

        budget_chars = (
            self._settings.ai_max_input_tokens * CHARS_PER_TOKEN * PROMPT_BUDGET_SHARE
        )
        return max(1, min(cap, int(budget_chars // per_review)))

    def build(
        self, uow: UnitOfWork, *, game_id: int, game_platform_id: int, kind: ReviewKind
    ) -> SnapshotResult:
        rows = uow.reviews.corpus_candidates(
            game_platform_id=game_platform_id,
            kind=kind,
            min_chars=1,  # length filtering belongs to the corpus filter, with a counter
        )
        candidates = [
            CorpusItem(
                review_id=row.id,
                body=row.body or "",
                score=row.score,
                score_max=row.score_max,
                published_on=row.published_on,
                publication=row.publication_name,
                dedupe_key=row.dedupe_key,
                bucket="",
            )
            for row in rows
        ]

        filtered = filter_corpus(candidates, min_chars=self._settings.corpus_min_chars)
        sampled = stratified_sample(
            filtered.kept,
            target=self.target_size(kind, items=filtered.kept),
            min_stratum_share=self._settings.corpus_min_stratum_share,
        )
        ordered = order_deterministically(sampled)

        digest = content_hash(
            ordered,
            ordering_version=self._settings.corpus_ordering_version,
            sampling_version=self._settings.corpus_sampling_version,
        )

        existing = uow.snapshots.find_by_hash(
            game_platform_id=game_platform_id, kind=kind, content_hash=digest
        )

        entries = tuple(
            (evidence_ref(REF_PREFIX[kind], index), item) for index, item in enumerate(ordered)
        )

        if existing is not None:
            # Identical corpus: reuse it. This is the cheapest possible answer to
            # "should we pay for a new summary?" -- no.
            stored = uow.snapshots.load_entries(existing.id)
            by_id = {item.review_id: item for item in ordered}
            entries = tuple(
                (ref, by_id[review.id]) for ref, review in stored if review.id in by_id
            )
            return SnapshotResult(
                snapshot_id=existing.id,
                content_hash=digest,
                review_count=existing.review_count,
                candidate_count=existing.candidate_count,
                rejected=dict(existing.rejected or {}),
                created=False,
                entries=entries,
            )

        from app.repositories.summaries import SnapshotEntry

        dates = [item.published_on for _ref, item in entries if item.published_on]
        snapshot = uow.snapshots.create(
            game_id=game_id,
            game_platform_id=game_platform_id,
            kind=kind,
            content_hash=digest,
            entries=[
                SnapshotEntry(evidence_ref=ref, review_id=item.review_id, position=index)
                for index, (ref, item) in enumerate(entries)
            ],
            candidate_count=filtered.candidate_count,
            rejected=filtered.rejected,
            date_min=min(dates) if dates else None,
            date_max=max(dates) if dates else None,
            ordering_version=self._settings.corpus_ordering_version,
            sampling_version=self._settings.corpus_sampling_version,
        )

        uow.events.emit(
            "snapshot.built",
            game_id=game_id,
            stage="snapshot_built",
            data={
                "kind": kind.value,
                "reviews": len(entries),
                "candidates": filtered.candidate_count,
                "rejected": filtered.rejected,
                "hash": digest[:12],
            },
        )

        return SnapshotResult(
            snapshot_id=snapshot.id,
            content_hash=digest,
            review_count=len(entries),
            candidate_count=filtered.candidate_count,
            rejected=filtered.rejected,
            created=True,
            entries=entries,
        )
