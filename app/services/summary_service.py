"""Summary generation.

The expensive decision in this file is the one NOT to call the model. Two independent
guards exist because the cost of getting it wrong is money spent for no change:

* ``should_generate`` reasons about whether anything meaningful moved;
* ``UNIQUE(game_platform_id, audience, input_fingerprint)`` makes a repeat call on an
  identical input impossible even if that reasoning has a bug.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from app.adapters.llm.base import LLMProvider, LLMUnavailable
from app.ai import prompts
from app.ai.schemas import GapOut, SummaryOut
from app.ai.validator import (
    EvidenceItem,
    ValidationReport,
    empty_section_note,
    validate_summary,
)
from app.config import Settings
from app.domain.enums import Audience, ClaimSide, ReviewKind, SummaryStatus
from app.domain.errors import RetryableError
from app.domain.scores import critic_score, user_score
from app.logging import correlate, get_logger
from app.normalizers.text import sha256_text
from app.services.snapshot_service import SnapshotResult, SnapshotService

if TYPE_CHECKING:  # pragma: no cover
    from app.db.uow import UnitOfWork

log = get_logger(__name__)


class SkipReason(StrEnum):
    BELOW_THRESHOLD = "below_threshold"
    IDENTICAL_INPUT = "identical_input"
    NOT_ENOUGH_CHANGE = "not_enough_change"
    PROVIDER_DISABLED = "provider_disabled"
    BUDGET_EXHAUSTED = "budget_exhausted"


class GenerateReason(StrEnum):
    FIRST = "first"
    NEW_REVIEWS = "new_reviews"
    NEW_RATIO = "new_ratio"
    SCORE_MOVED = "score_moved"
    RETRY_AFTER_FAILURE = "retry_after_failure"
    STALENESS = "staleness"


@dataclass(frozen=True, slots=True)
class Decision:
    generate: bool
    reason: str
    snapshot: SnapshotResult | None = None
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class SummaryOutcome:
    audience: Audience
    status: SummaryStatus
    reason: str
    accepted_claims: int = 0
    rejected_claims: int = 0
    summary_id: int | None = None
    cost_usd: float = 0.0


AUDIENCE_KIND = {Audience.CRITIC: ReviewKind.CRITIC, Audience.USER: ReviewKind.USER}


def ai_budget_exceeded(
    uow: UnitOfWork, settings: Settings, *, now: dt.datetime | None = None
) -> str | None:
    """The reason AI spending must stop right now, or ``None``.

    Two independent ceilings, because they fail differently: the daily cost cap bounds the
    bill, and the hourly call cap bounds a runaway loop that would otherwise reach the
    daily cap in four minutes. Both are measured from what was recorded on past summaries
    rather than from a counter in this process, so every worker and every restart reads
    the same number -- an in-memory counter would let N workers spend N times the limit.

    A module function rather than a method: the Let's Play service draws on the same
    budget and should not have to construct a SummaryService to ask.
    """
    now = now or dt.datetime.now(dt.UTC)

    day = uow.summaries.cost_since(now - dt.timedelta(days=1))
    limit = settings.ai_daily_cost_limit_usd
    if limit > 0 and day["cost_usd"] >= limit:
        return f"AI spend in the last 24h is ${day['cost_usd']:.2f} of ${limit:.2f}"

    hour = uow.summaries.cost_since(now - dt.timedelta(hours=1))
    max_calls = settings.ai_max_calls_per_hour
    if max_calls > 0 and hour["calls"] >= max_calls:
        return f"{hour['calls']} model calls in the last hour, limit {max_calls}"
    return None


class SummaryService:
    def __init__(
        self,
        llm: LLMProvider,
        settings: Settings,
        *,
        snapshots: SnapshotService | None = None,
        clock=None,
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._snapshots = snapshots or SnapshotService(settings)
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    # ------------------------------------------------------------------ thresholds

    def threshold(self, audience: Audience) -> int:
        """Asymmetric, and counted on TEXTS after filtering (ADR-009).

        Five edited publication reviews are a usable corpus; twenty one-line player posts
        are not. Onimusha has 77 player ratings and ten texts, which is why the count that
        matters is the corpus size, not the rating count.
        """
        return (
            self._settings.summary_min_critic_reviews
            if audience is Audience.CRITIC
            else self._settings.summary_min_user_reviews
        )

    def fingerprint(self, snapshot: SnapshotResult, audience: Audience) -> str:
        return sha256_text(
            "|".join(
                [
                    snapshot.content_hash,
                    self._prompt_version(audience),
                    self._llm.model,
                    self._settings.params_version,
                ]
            )
        )

    def _prompt_version(self, audience: Audience) -> str:
        return {
            Audience.CRITIC: self._settings.prompt_version_critic,
            Audience.USER: self._settings.prompt_version_user,
            Audience.LETSPLAY: self._settings.prompt_version_letsplay,
        }[audience]

    # ------------------------------------------------------------------ decision

    def should_generate(
        self, uow: UnitOfWork, *, game_id: int, game_platform_id: int, audience: Audience
    ) -> Decision:
        snapshot = self._snapshots.build(
            uow,
            game_id=game_id,
            game_platform_id=game_platform_id,
            kind=AUDIENCE_KIND[audience],
        )

        if snapshot.review_count < self.threshold(audience):
            return Decision(False, SkipReason.BELOW_THRESHOLD.value, snapshot)

        fingerprint = self.fingerprint(snapshot, audience)

        # The hard guard. Even a bug in everything below cannot spend money twice on the
        # same input, because the row cannot be written twice.
        if uow.summaries.by_fingerprint(
            game_platform_id=game_platform_id, audience=audience, fingerprint=fingerprint
        ):
            return Decision(False, SkipReason.IDENTICAL_INPUT.value, snapshot, fingerprint)

        current = uow.summaries.current(
            game_platform_id=game_platform_id, audience=audience
        )
        if current is None:
            return Decision(True, GenerateReason.FIRST.value, snapshot, fingerprint)

        if current.status in (SummaryStatus.FAILED.value, SummaryStatus.REJECTED.value):
            return Decision(
                True, GenerateReason.RETRY_AFTER_FAILURE.value, snapshot, fingerprint
            )

        new_reviews = snapshot.review_count
        if current.snapshot_id is not None:
            overlap = uow.snapshots.overlap(snapshot.snapshot_id, current.snapshot_id)
            new_reviews = max(0, snapshot.review_count - overlap)

        if new_reviews >= self._settings.ai_min_new_reviews:
            return Decision(True, GenerateReason.NEW_REVIEWS.value, snapshot, fingerprint)
        if (
            snapshot.review_count
            and new_reviews / snapshot.review_count >= self._settings.ai_min_new_ratio
        ):
            return Decision(True, GenerateReason.NEW_RATIO.value, snapshot, fingerprint)

        # A handful of new reviews below the threshold above can still move the score
        # several points, and a moved score is exactly the case a stale summary gets
        # wrong. NULL means "we do not know what it was" and never triggers -- the same
        # rule the rest of the system applies to a missing score (ADR-002).
        moved = self._score_move(uow, game_platform_id, audience, current)
        if moved is not None and moved >= self._settings.ai_score_move_points:
            return Decision(True, GenerateReason.SCORE_MOVED.value, snapshot, fingerprint)

        age = self._clock() - current.generated_at
        if age > dt.timedelta(days=self._settings.ai_max_summary_age_days) and new_reviews:
            return Decision(True, GenerateReason.STALENESS.value, snapshot, fingerprint)

        return Decision(
            False, SkipReason.NOT_ENOUGH_CHANGE.value, snapshot, fingerprint
        )

    def current_score(
        self, uow: UnitOfWork, game_platform_id: int, audience: Audience
    ) -> int | None:
        """The audience's score on the shared 0-100 axis, or ``None`` if it has none."""
        row = uow.games.get_platform(game_platform_id)
        if row is None:
            return None
        if audience is Audience.CRITIC:
            score = critic_score(row.metascore_raw, row.metascore_count)
        else:
            score = user_score(
                row.userscore_raw,
                row.userscore_count,
                zero_min_ratings=self._settings.userscore_zero_min_ratings,
            )
        return score.normalized

    def _score_move(
        self, uow: UnitOfWork, game_platform_id: int, audience: Audience, current
    ) -> int | None:
        if current.score_at_generation is None:
            return None
        now = self.current_score(uow, game_platform_id, audience)
        if now is None:
            return None
        return abs(now - current.score_at_generation)

    # ------------------------------------------------------------------ generation

    def over_budget(self, uow: UnitOfWork, *, now: dt.datetime | None = None) -> str | None:
        return ai_budget_exceeded(uow, self._settings, now=now)

    def generate(
        self, uow: UnitOfWork, *, game_id: int, game_platform_id: int, audience: Audience
    ) -> SummaryOutcome:
        with correlate(game_id=game_id, stage="summary_pending"):
            decision = self.should_generate(
                uow, game_id=game_id, game_platform_id=game_platform_id, audience=audience
            )

            if not decision.generate:
                if decision.reason == SkipReason.BELOW_THRESHOLD.value:
                    # An explicit product state, not a failure: the UI says how many
                    # reviews exist and when to expect a summary.
                    self._record_skip(
                        uow,
                        game_id=game_id,
                        game_platform_id=game_platform_id,
                        audience=audience,
                        decision=decision,
                    )
                return SummaryOutcome(
                    audience=audience,
                    status=SummaryStatus.SKIPPED_NO_DATA
                    if decision.reason == SkipReason.BELOW_THRESHOLD.value
                    else SummaryStatus.FRESH,
                    reason=decision.reason,
                )

            if not self._llm.enabled:
                return SummaryOutcome(
                    audience=audience,
                    status=SummaryStatus.SKIPPED_NO_DATA,
                    reason=SkipReason.PROVIDER_DISABLED.value,
                )

            over = self.over_budget(uow)
            if over is not None:
                # Deferred, not failed, and NOT recorded as a summary: the game keeps the
                # summary it already has and the job retries tomorrow. A cost ceiling that
                # only appears on a dashboard is not a ceiling.
                uow.events.emit(
                    "ai.budget_exhausted",
                    level="warning",
                    game_id=game_id,
                    message=over,
                )
                return SummaryOutcome(
                    audience=audience,
                    status=SummaryStatus.SKIPPED_NO_DATA,
                    reason=SkipReason.BUDGET_EXHAUSTED.value,
                )

            snapshot = decision.snapshot
            assert snapshot is not None
            prompt = self._build_prompt(uow, game_platform_id, audience, snapshot)

            try:
                result = self._llm.complete_structured(
                    prompt=prompt,
                    schema=SummaryOut,
                    max_tokens=self._settings.llm_max_output_tokens,
                )
            except LLMUnavailable:
                return SummaryOutcome(
                    audience=audience,
                    status=SummaryStatus.SKIPPED_NO_DATA,
                    reason=SkipReason.PROVIDER_DISABLED.value,
                )
            except Exception as exc:
                # The previous valid summary stays current: a user keeps seeing the last
                # summary that passed validation (FINAL_SPEC section 23).
                uow.summaries.save(
                    game_id=game_id,
                    game_platform_id=game_platform_id,
                    audience=audience,
                    snapshot_id=snapshot.snapshot_id,
                    status=SummaryStatus.FAILED,
                    input_fingerprint=decision.fingerprint,
                    claims=[],
                    error_message=str(exc)[:2000],
                    llm_provider=self._llm.name,
                    llm_model=self._llm.model,
                    prompt_version=self._prompt_version(audience),
                    params_version=self._settings.params_version,
                )
                uow.events.emit(
                    "summary.failed", level="error", game_id=game_id,
                    data={"audience": audience.value, "error": str(exc)[:500]},
                )
                if isinstance(exc, RetryableError):
                    raise
                return SummaryOutcome(
                    audience=audience, status=SummaryStatus.FAILED, reason=str(exc)[:200]
                )

            return self._store(
                uow,
                game_id=game_id,
                game_platform_id=game_platform_id,
                audience=audience,
                decision=decision,
                snapshot=snapshot,
                result=result,
            )

    # ------------------------------------------------------------------ internals

    def _build_prompt(self, uow, game_platform_id: int, audience: Audience, snapshot):
        game_platform = uow.games.get_platform(game_platform_id)
        assert game_platform is not None
        game = uow.games.get_by_slug(game_platform.game.mc_slug)
        assert game is not None
        critic = critic_score(game_platform.metascore_raw, game_platform.metascore_count)
        player = user_score(
            game_platform.userscore_raw,
            game_platform.userscore_count,
            zero_min_ratings=self._settings.userscore_zero_min_ratings,
        )
        return prompts.build_summary_prompt(
            audience=audience,
            title=game.title,
            platform_name=game_platform.platform.name,
            genres=[gg.genre.name for gg in game.genres],
            release_year=game.premiere_year,
            critic_score=critic.normalized,
            user_score=player.value,
            review_count=snapshot.review_count,
            candidate_count=snapshot.candidate_count,
            corpus=snapshot.rendered(
                max_review_chars=self._settings.corpus_max_review_chars
            ),
        )

    def _store(
        self,
        uow: UnitOfWork,
        *,
        game_id: int,
        game_platform_id: int,
        audience: Audience,
        decision: Decision,
        snapshot: SnapshotResult,
        result,
    ) -> SummaryOutcome:
        payload: SummaryOut = result.value
        evidence = {
            ref: EvidenceItem(ref=ref, text=item.body, published_on=item.published_on)
            for ref, item in snapshot.entries
        }

        report = validate_summary(
            positive=payload.positive,
            negative=payload.negative,
            evidence=evidence,
            corpus_size=snapshot.review_count,
            min_support=self._settings.claim_min_support,
            min_support_small_corpus=self._settings.claim_min_support_small_corpus,
            small_corpus_threshold=self._settings.small_corpus_threshold,
            temporal_min_span_days=self._settings.temporal_min_span_days,
        )

        accepted = report.accepted
        # A summary with nothing left after validation is not published. It is recorded,
        # so the failure is measurable, and the previous valid summary stays current.
        status = SummaryStatus.FRESH if accepted or payload.overall else SummaryStatus.REJECTED
        if not accepted and not payload.overall:
            status = SummaryStatus.REJECTED

        summary = uow.summaries.save(
            game_id=game_id,
            game_platform_id=game_platform_id,
            audience=audience,
            snapshot_id=snapshot.snapshot_id,
            status=status,
            input_fingerprint=decision.fingerprint,
            claims=self._claim_rows(report),
            heading=payload.heading or None,
            overall=payload.overall or None,
            aspect_verdicts={k.value: v.value for k, v in payload.aspect_verdicts.items()},
            confidence=payload.confidence,
            reviews_used=snapshot.review_count,
            reviews_candidates=snapshot.candidate_count,
            score_at_generation=self.current_score(uow, game_platform_id, audience),
            llm_provider=result.provider,
            llm_model=result.model,
            prompt_version=self._prompt_version(audience),
            params_version=self._settings.params_version,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )
        uow.summaries.prune_history(
            game_platform_id=game_platform_id,
            audience=audience,
            keep=self._settings.summary_history_keep,
        )
        uow.games.touch(game_id, summaries_synced_at=self._clock())

        uow.events.emit(
            "summary.generated" if status is SummaryStatus.FRESH else "summary.rejected",
            level="info" if status is SummaryStatus.FRESH else "warning",
            game_id=game_id,
            stage="validated",
            data={
                "audience": audience.value,
                "reason": decision.reason,
                "accepted": len(accepted),
                "rejected": report.rejection_counts,
                # Quality telemetry: a drifting acceptance rate is a prompt or model
                # regression, visible before a user sees it.
                "acceptance_rate": round(report.acceptance_rate, 3),
                "cost_usd": result.cost_usd,
                "model": result.model,
            },
        )

        return SummaryOutcome(
            audience=audience,
            status=status,
            reason=decision.reason,
            accepted_claims=len(accepted),
            rejected_claims=len(report.claims) - len(accepted),
            summary_id=summary.id,
            cost_usd=result.cost_usd,
        )

    @staticmethod
    def _claim_rows(report: ValidationReport) -> list[dict]:
        rows: list[dict] = []
        for item in report.claims:
            rows.append(
                {
                    "side": item.side.value,
                    "aspect": item.claim.aspect.value,
                    "claim": item.claim.claim,
                    "claim_ru": item.claim.claim_ru,
                    "claim_type": item.claim.claim_type.value,
                    "evidence_refs": list(item.claim.evidence),
                    "strength": item.claim.strength,
                    "validation": item.validation.value,
                    "validation_detail": item.detail,
                }
            )
        return rows

    def _record_skip(
        self,
        uow: UnitOfWork,
        *,
        game_id: int,
        game_platform_id: int,
        audience: Audience,
        decision: Decision,
    ) -> None:
        existing = uow.summaries.current(
            game_platform_id=game_platform_id, audience=audience
        )
        if existing is not None:
            return  # never demote a good summary because the corpus shrank
        uow.summaries.save(
            game_id=game_id,
            game_platform_id=game_platform_id,
            audience=audience,
            snapshot_id=decision.snapshot.snapshot_id if decision.snapshot else None,
            status=SummaryStatus.SKIPPED_NO_DATA,
            input_fingerprint=decision.fingerprint
            or sha256_text(f"skip:{game_platform_id}:{audience.value}"),
            claims=[],
            reviews_used=decision.snapshot.review_count if decision.snapshot else 0,
            reviews_candidates=decision.snapshot.candidate_count if decision.snapshot else 0,
        )

    # ------------------------------------------------------------------ gap

    def explain_gap(
        self, uow: UnitOfWork, *, game_id: int, game_platform_id: int
    ) -> str | None:
        """Only when both sides are solid and the gap is real (ADR-008 section 5)."""
        game_platform = uow.games.get_platform(game_platform_id)
        assert game_platform is not None
        critic = critic_score(game_platform.metascore_raw, game_platform.metascore_count)
        player = user_score(
            game_platform.userscore_raw,
            game_platform.userscore_count,
            zero_min_ratings=self._settings.userscore_zero_min_ratings,
        )
        if critic.normalized is None or player.normalized is None:
            return None
        gap = critic.normalized - player.normalized
        if abs(gap) < self._settings.gap_min_points:
            return None

        critic_snap = self._snapshots.build(
            uow, game_id=game_id, game_platform_id=game_platform_id, kind=ReviewKind.CRITIC
        )
        user_snap = self._snapshots.build(
            uow, game_id=game_id, game_platform_id=game_platform_id, kind=ReviewKind.USER
        )
        if critic_snap.review_count < self.threshold(
            Audience.CRITIC
        ) or user_snap.review_count < self.threshold(Audience.USER):
            return None
        if not self._llm.enabled:
            return None
        if self.over_budget(uow) is not None:
            # The gap explanation is the least essential of the three AI outputs: the
            # verdict sentence and the warning note are both derived, so the page still
            # says clearly that the two audiences disagree.
            return None

        game = uow.games.get(game_id)
        prompt = prompts.build_gap_prompt(
            title=game.title,
            platform_name=game_platform.platform.name,
            critic_score=critic.normalized,
            user_score_normalized=player.normalized,
            critic_corpus=critic_snap.rendered(
                max_review_chars=self._settings.corpus_max_review_chars
            ),
            user_corpus=user_snap.rendered(
                max_review_chars=self._settings.corpus_max_review_chars
            ),
        )
        result = self._llm.complete_structured(
            prompt=prompt, schema=GapOut, max_tokens=self._settings.llm_max_output_tokens
        )
        payload: GapOut = result.value

        known = uow.snapshots.evidence_refs(critic_snap.snapshot_id) | uow.snapshots.evidence_refs(
            user_snap.snapshot_id
        )
        refs = [ref for ref in payload.evidence if ref in known]
        # No supported explanation is a correct answer; an invented one is not.
        if not payload.explanation.strip() or len(refs) < self._settings.claim_min_support:
            return None

        uow.gaps.save(
            game_id=game_id,
            game_platform_id=game_platform_id,
            critic_snapshot_id=critic_snap.snapshot_id,
            user_snapshot_id=user_snap.snapshot_id,
            gap_points=gap,
            explanation=payload.explanation,
            evidence_refs=refs,
            status=SummaryStatus.FRESH,
            llm_model=result.model,
            prompt_version=self._settings.prompt_version_gap,
        )
        return payload.explanation

    # ------------------------------------------------------------------ presentation

    @staticmethod
    def empty_note(*, side: ClaimSide, positive_count: int, negative_count: int) -> str:
        return empty_section_note(
            side=side, positive_count=positive_count, negative_count=negative_count
        )
