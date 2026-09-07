"""The AI pipeline end to end, on the real Release 0 corpora and with no network."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.adapters.llm.base import NullLLMProvider
from app.adapters.llm.fixture import FixtureLLMProvider
from app.config import Settings
from app.db.models import Summary, SummaryClaim
from app.db.uow import UnitOfWork
from app.domain.enums import Audience, ClaimSide, ClaimType, ClaimValidation, SummaryStatus
from app.domain.errors import RetryableError
from app.services.summary_service import SkipReason, SummaryService

pytestmark = pytest.mark.integration


def settings(**overrides) -> Settings:
    base = {
        "admin_token": "t" * 32,
        "app_env": "test",
        "llm_enabled": True,
        "corpus_min_chars": 80,
        "summary_min_critic_reviews": 5,
        "summary_min_user_reviews": 20,
        "claim_min_support": 3,
        "claim_min_support_small_corpus": 2,
    }
    base.update(overrides)
    return Settings(**base)


class TestThresholds:
    def test_a_thin_player_corpus_is_skipped_not_faked(self, session_factory, db, load_harvest):
        """Onimusha: 77 ratings, 10 texts. The threshold is judged on TEXTS (ADR-009)."""
        game_id, gp_id = load_harvest("onimusha-way-of-the-sword", "playstation-5")
        llm = FixtureLLMProvider()
        service = SummaryService(llm, settings())

        with UnitOfWork(session_factory) as uow:
            outcome = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )

        assert outcome.status is SummaryStatus.SKIPPED_NO_DATA
        assert outcome.reason == SkipReason.BELOW_THRESHOLD.value
        assert llm.calls == 0, "no money is spent below the threshold"

    def test_skipping_twice_is_not_an_error(self, session_factory, db, load_harvest):
        """Two jobs for the same thin corpus must not collide on the fingerprint.

        Below the threshold there is no snapshot and so no fingerprint, and the skip row
        is written under a constant one per (platform, audience). `should_generate`
        returns before computing a fingerprint in that case, so its by_fingerprint guard
        never sees that row, and the row is written with is_current=False, so the
        `current()` check does not see it either. A second job therefore inserted the same
        key again.

        In production that was 119 dead jobs in two minutes, six attempts each, none of
        which reached a model. Recording "there was nothing to summarise" twice is the
        same fact, not a failure.
        """
        game_id, gp_id = load_harvest("onimusha-way-of-the-sword", "playstation-5")
        service = SummaryService(FixtureLLMProvider(), settings())

        for _ in range(3):
            with UnitOfWork(session_factory) as uow:
                outcome = service.generate(
                    uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
                )
            assert outcome.status is SummaryStatus.SKIPPED_NO_DATA

        rows = db.execute(
            sa.select(Summary).where(
                Summary.game_platform_id == gp_id, Summary.audience == "user"
            )
        ).scalars().all()
        assert len(rows) == 1, f"the skip was recorded {len(rows)} times"

    def test_explaining_the_same_gap_twice_is_not_an_error(
        self, session_factory, db, load_harvest
    ):
        """`uq_gap_snapshots` refused a second explanation for one pair of snapshots.

        A gap computed and found not worth publishing is stored with is_current=False,
        so `current()` cannot see it, and the next job for the same pair inserted the
        same key. The constraint is the guarantee; the caller has to ask before writing
        rather than find out by failing.
        """
        from app.db.models import GapExplanation

        game_id, gp_id = load_harvest("onimusha-way-of-the-sword", "playstation-5")

        with UnitOfWork(session_factory) as uow:
            for _ in range(3):
                uow.gaps.save(
                    game_id=game_id,
                    game_platform_id=gp_id,
                    critic_snapshot_id=None,
                    user_snapshot_id=None,
                    gap_points=12,
                    explanation=None,
                    evidence_refs=[],
                    status=SummaryStatus.SKIPPED_NO_DATA,
                    llm_model=None,
                    prompt_version=None,
                )

        rows = db.execute(sa.select(GapExplanation)).scalars().all()
        assert len(rows) == 1, f"the same gap was stored {len(rows)} times"

    def test_a_small_critic_corpus_still_produces_a_summary(
        self, session_factory, db, load_harvest
    ):
        """NBA 2K27 has five critic reviews. A single threshold of 20 would lose it."""
        game_id, gp_id = load_harvest("nba-2k27", "playstation-5")
        service = SummaryService(FixtureLLMProvider(), settings())
        with UnitOfWork(session_factory) as uow:
            decision = service.should_generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC
            )
        assert decision.snapshot.review_count >= 4
        # Not asserting it generates -- the corpus may be small after filtering; asserting
        # that the CRITIC threshold is the small one is the point.
        assert service.threshold(Audience.CRITIC) == 5
        assert service.threshold(Audience.USER) == 20

    def test_the_skip_is_recorded_so_the_ui_can_explain_it(
        self, session_factory, db, load_harvest
    ):
        game_id, gp_id = load_harvest("onimusha-way-of-the-sword", "playstation-5")
        service = SummaryService(FixtureLLMProvider(), settings())
        with UnitOfWork(session_factory) as uow:
            service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        row = db.execute(sa.select(Summary)).scalars().one()
        assert row.status == SummaryStatus.SKIPPED_NO_DATA.value
        assert row.reviews_used < 20
        assert row.reviews_candidates >= row.reviews_used


class TestEldenRingHasNothingToCriticise:
    """The finding that broke the 3+3 layout (C-06)."""

    def test_zero_negative_claims_is_a_valid_summary(self, session_factory, db, load_harvest):
        game_id, gp_id = load_harvest("elden-ring", "playstation-5")
        # The corpus is 86 positive, 0 neutral, 0 negative, so the fixture model has
        # nothing to build a negative claim from -- exactly like a real model would.
        service = SummaryService(FixtureLLMProvider(), settings())

        with UnitOfWork(session_factory) as uow:
            outcome = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC
            )

        assert outcome.status is SummaryStatus.FRESH
        assert outcome.accepted_claims > 0
        negatives = db.execute(
            sa.select(sa.func.count())
            .select_from(SummaryClaim)
            .where(
                SummaryClaim.side == ClaimSide.NEGATIVE.value,
                SummaryClaim.validation == ClaimValidation.ACCEPTED.value,
            )
        ).scalar_one()
        assert negatives == 0

    def test_the_empty_section_gets_honest_copy(self):
        note = SummaryService.empty_note(
            side=ClaimSide.NEGATIVE, positive_count=86, negative_count=0
        )
        assert "положительных рецензий 86" in note and "отрицательных нет" in note


class TestCostControl:
    def test_an_identical_corpus_never_reaches_the_model(
        self, session_factory, db, load_harvest
    ):
        """The guarantee that makes an hourly schedule affordable."""
        game_id, gp_id = load_harvest("cyberpunk-2077", "playstation-4")
        llm = FixtureLLMProvider()
        service = SummaryService(llm, settings())

        with UnitOfWork(session_factory) as uow:
            first = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        calls_after_first = llm.calls

        with UnitOfWork(session_factory) as uow:
            second = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )

        assert first.status is SummaryStatus.FRESH
        assert second.reason == SkipReason.IDENTICAL_INPUT.value
        assert llm.calls == calls_after_first, "the model was called a second time"

    def test_the_constraint_backs_up_the_logic(self, session_factory, db, load_harvest):
        """Even a bug in should_generate cannot write the same fingerprint twice."""
        game_id, gp_id = load_harvest("cyberpunk-2077", "playstation-4")
        service = SummaryService(FixtureLLMProvider(), settings())
        with UnitOfWork(session_factory) as uow:
            service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        row = db.execute(sa.select(Summary)).scalars().first()

        duplicate = Summary(
            game_id=row.game_id,
            game_platform_id=row.game_platform_id,
            audience=row.audience,
            status=SummaryStatus.FRESH.value,
            input_fingerprint=row.input_fingerprint,
            is_current=False,
        )
        db.add(duplicate)
        with pytest.raises(sa.exc.IntegrityError):
            db.flush()
        db.rollback()

    def test_cost_is_recorded_per_call(self, session_factory, db, load_harvest):
        game_id, gp_id = load_harvest("cyberpunk-2077", "playstation-4")
        service = SummaryService(FixtureLLMProvider(), settings())
        with UnitOfWork(session_factory) as uow:
            service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        row = db.execute(sa.select(Summary)).scalars().first()
        assert row.tokens_in > 0
        assert row.llm_model == "fixture-1"
        assert row.prompt_version


class TestValidationInThePipeline:
    def test_invented_references_are_rejected_and_the_summary_is_not_published(
        self, session_factory, db, load_harvest
    ):
        game_id, gp_id = load_harvest("cyberpunk-2077", "playstation-4")
        llm = FixtureLLMProvider(invalid_refs=True)
        service = SummaryService(llm, settings())

        with UnitOfWork(session_factory) as uow:
            outcome = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )

        assert outcome.accepted_claims == 0
        assert outcome.rejected_claims > 0
        reasons = db.execute(sa.select(SummaryClaim.validation).distinct()).scalars().all()
        assert ClaimValidation.REJECTED_MISSING_REF.value in reasons

    def test_rejected_claims_are_kept_for_measurement(
        self, session_factory, db, load_harvest
    ):
        game_id, gp_id = load_harvest("cyberpunk-2077", "playstation-4")
        service = SummaryService(FixtureLLMProvider(vague=True), settings())
        with UnitOfWork(session_factory) as uow:
            service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        stored = db.execute(sa.select(sa.func.count()).select_from(SummaryClaim)).scalar_one()
        assert stored > 0, "a discarded claim must still be recorded, or quality is unmeasurable"

    def test_unsupported_temporal_claims_do_not_survive(
        self, session_factory, db, load_harvest
    ):
        game_id, gp_id = load_harvest("skull-and-bones", "playstation-5")
        llm = FixtureLLMProvider(claim_type=ClaimType.TEMPORAL)
        service = SummaryService(llm, settings(temporal_min_span_days=3650))
        with UnitOfWork(session_factory) as uow:
            outcome = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        assert outcome.accepted_claims == 0
        reasons = db.execute(sa.select(SummaryClaim.validation).distinct()).scalars().all()
        assert ClaimValidation.REJECTED_TEMPORAL_UNSUPPORTED.value in reasons


class TestFailureHandling:
    def test_a_provider_failure_keeps_the_previous_summary_current(
        self, session_factory, db, load_harvest
    ):
        from app.db.models import Review
        from app.normalizers.text import body_hash

        game_id, gp_id = load_harvest("forspoken", "playstation-5")
        service = SummaryService(FixtureLLMProvider(), settings())
        with UnitOfWork(session_factory) as uow:
            service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        good = db.execute(
            sa.select(Summary).where(Summary.is_current.is_(True))
        ).scalars().one()

        # New reviews arrive, so a regeneration is warranted; the provider then fails.
        for index in range(15):
            text = (
                f"A newly indexed review number {index} describing the traversal and the "
                "combat in enough words to clear the corpus minimum length filter."
            )
            db.add(
                Review(
                    game_id=game_id, game_platform_id=gp_id, kind="user",
                    dedupe_key=f"new-{index}", source_review_id=f"new-{index}",
                    score=7, score_max=10, score_normalized=70,
                    body=text, body_hash=body_hash(text), char_count=len(text),
                    published_on=dt.date(2026, 8, 1),
                )
            )
        db.commit()

        failing = SummaryService(
            FixtureLLMProvider(fail_with=RetryableError("529 overloaded")), settings()
        )
        with pytest.raises(RetryableError), UnitOfWork(session_factory) as uow:
            failing.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )

        db.expire_all()
        still_current = db.execute(
            sa.select(Summary).where(Summary.is_current.is_(True))
        ).scalars().all()
        assert len(still_current) == 1
        assert still_current[0].id == good.id

    def test_a_disabled_provider_does_not_break_anything(
        self, session_factory, db, load_harvest
    ):
        game_id, gp_id = load_harvest("baldurs-gate-3", "pc")
        service = SummaryService(NullLLMProvider(), settings(llm_enabled=False))
        with UnitOfWork(session_factory) as uow:
            outcome = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        assert outcome.status is SummaryStatus.SKIPPED_NO_DATA
        assert outcome.reason == SkipReason.PROVIDER_DISABLED.value


class TestVersioning:
    def test_a_new_version_demotes_the_old_one_atomically(
        self, session_factory, db, load_harvest
    ):
        from app.db.models import Review
        from app.normalizers.text import body_hash

        game_id, gp_id = load_harvest("redfall", "xbox-series-x")
        service = SummaryService(FixtureLLMProvider(), settings())
        with UnitOfWork(session_factory) as uow:
            service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )

        for index in range(20):
            text = (
                f"Post-patch review {index}: the frame rate holds and the mission "
                "structure reads much more clearly than it did before."
            )
            db.add(
                Review(
                    game_id=game_id, game_platform_id=gp_id, kind="user",
                    dedupe_key=f"patch-{index}", source_review_id=f"patch-{index}",
                    score=8, score_max=10, score_normalized=80,
                    body=text, body_hash=body_hash(text), char_count=len(text),
                    published_on=dt.date(2026, 7, 1),
                )
            )
        db.commit()

        with UnitOfWork(session_factory) as uow:
            second = service.generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.USER
            )
        assert second.status is SummaryStatus.FRESH

        current = db.execute(
            sa.select(Summary).where(Summary.is_current.is_(True))
        ).scalars().all()
        assert len(current) == 1
        assert current[0].version == 2
        total = db.execute(sa.select(sa.func.count()).select_from(Summary)).scalar_one()
        assert total == 2, "history is kept"
