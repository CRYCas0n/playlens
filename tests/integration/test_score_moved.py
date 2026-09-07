"""Regeneration when the score moves (`AI_SCORE_MOVE_POINTS`).

`GenerateReason.SCORE_MOVED` was declared and never reachable. The case it exists for is
narrow and real: five new reviews is below the "enough has changed" threshold, but five
reviews can move a Metascore four points, and a summary written against the old score now
describes a game the numbers no longer agree with.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.adapters.llm.fixture import FixtureLLMProvider
from app.config import Settings
from app.db.models import GamePlatform, Review, Summary
from app.domain.enums import Audience
from app.services.summary_service import GenerateReason, SkipReason, SummaryService

pytestmark = pytest.mark.integration


def settings(**overrides) -> Settings:
    base = {
        "admin_token": "t" * 32,
        "app_env": "test",
        "llm_enabled": True,
        "summary_min_critic_reviews": 5,
        "ai_min_new_reviews": 1000,   # the new-reviews trigger is out of the way
        "ai_min_new_ratio": 1.1,      # so is the ratio one
        "ai_score_move_points": 3,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def generated(db, uow, load_harvest):
    game_id, gp_id = load_harvest("nba-2k27", "playstation-5")
    service = SummaryService(FixtureLLMProvider(), settings())
    service.generate(uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC)
    db.flush()
    return game_id, gp_id, service


def move_score(db, uow, gp_id: int, *, by: int, new_reviews: int = 2) -> None:
    """What actually happens upstream: a few reviews land AND the score shifts.

    Moving the score alone is not a reachable state -- the corpus fingerprint would be
    unchanged and the identical-input guard would stop the run before any of this. So the
    test reproduces the real shape: too few new reviews to trigger on their own, enough
    to move the number.
    """
    import datetime as dt

    from app.normalizers.text import body_hash

    platform = uow.games.get_platform(gp_id)
    for index in range(new_reviews):
        body = f"A late review number {index} with enough words in it to clear the "
        body += "minimum length filter that the corpus builder applies to every entry."
        db.add(
            Review(
                game_id=platform.game_id,
                game_platform_id=gp_id,
                kind="critic",
                dedupe_key=f"late-{index}",
                body=body,
                body_hash=body_hash(body),
                score=90,
                score_max=100,
                published_on=dt.date(2026, 3, 1),
                publication_name=f"Late Publication {index}",
                score_normalized=90.0,
                char_count=len(body),
            )
        )
    db.execute(
        sa.update(GamePlatform)
        .where(GamePlatform.id == gp_id)
        .values(metascore_raw=platform.metascore_raw + by)
    )
    db.flush()


def decide(service, uow, game_id, gp_id):
    return service.should_generate(
        uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC
    )


class TestTheScoreIsRecorded:
    def test_a_summary_remembers_the_score_it_was_written_against(self, db, uow, generated):
        _, gp_id, _ = generated
        summary = uow.summaries.current(game_platform_id=gp_id, audience=Audience.CRITIC)
        platform = uow.games.get_platform(gp_id)
        assert summary.score_at_generation == platform.metascore_raw


class TestTheTrigger:
    def test_a_small_move_changes_nothing(self, db, uow, generated):
        game_id, gp_id, service = generated
        move_score(db, uow, gp_id, by=2)
        assert decide(service, uow, game_id, gp_id).reason == SkipReason.NOT_ENOUGH_CHANGE.value

    def test_a_move_at_the_threshold_forces_a_new_summary(self, db, uow, generated):
        game_id, gp_id, service = generated
        move_score(db, uow, gp_id, by=3)
        decision = decide(service, uow, game_id, gp_id)
        assert decision.generate is True
        assert decision.reason == GenerateReason.SCORE_MOVED.value

    def test_it_is_a_distance_not_a_direction(self, db, uow, generated):
        """A score falling four points is as much a change as one rising four."""
        game_id, gp_id, service = generated
        move_score(db, uow, gp_id, by=-4)
        assert decide(service, uow, game_id, gp_id).reason == GenerateReason.SCORE_MOVED.value

    def test_the_threshold_is_configurable(self, db, uow, generated):
        game_id, gp_id, _ = generated
        move_score(db, uow, gp_id, by=3)
        strict = SummaryService(FixtureLLMProvider(), settings(ai_score_move_points=10))
        assert decide(strict, uow, game_id, gp_id).reason == SkipReason.NOT_ENOUGH_CHANGE.value


class TestUnknownScores:
    def test_an_old_summary_with_no_recorded_score_never_triggers(self, db, uow, generated):
        """NULL means "unknown", never zero -- the same rule as everywhere else."""
        game_id, gp_id, service = generated
        db.execute(
            sa.update(Summary)
            .where(Summary.game_platform_id == gp_id)
            .values(score_at_generation=None)
        )
        db.flush()
        move_score(db, uow, gp_id, by=5)
        assert decide(service, uow, game_id, gp_id).reason == SkipReason.NOT_ENOUGH_CHANGE.value

    def test_a_score_that_disappeared_does_not_read_as_a_move_to_zero(self, db, uow, generated):
        game_id, gp_id, service = generated
        move_score(db, uow, gp_id, by=0)
        db.execute(
            sa.update(GamePlatform)
            .where(GamePlatform.id == gp_id)
            .values(metascore_raw=None, metascore_count=0)
        )
        db.flush()
        assert decide(service, uow, game_id, gp_id).reason == SkipReason.NOT_ENOUGH_CHANGE.value
