"""The AI cost ceiling, enforced rather than displayed.

A limit that only appears on a dashboard is not a limit. These tests spend against the
recorded cost of past summaries -- the same number every worker reads -- and check that
the next call is refused rather than merely reported.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.db.models import Game, GamePlatform, Platform, Summary
from app.domain.errors import BudgetExhausted
from app.normalizers.text import normalize_title
from app.services.summary_service import ai_budget_exceeded

pytestmark = pytest.mark.integration


@pytest.fixture
def lead(db):
    platform = db.execute(sa.select(Platform).where(Platform.slug == "pc")).scalar_one()
    game = Game(
        mc_slug="ashen-veil",
        mc_url="https://www.metacritic.com/game/ashen-veil/",
        title="Ashen Veil",
        title_norm=normalize_title("Ashen Veil"),
    )
    db.add(game)
    db.flush()
    row = GamePlatform(game_id=game.id, platform_id=platform.id, is_lead=True)
    db.add(row)
    db.flush()
    db.commit()
    return row


def spend(db, lead, *, usd: float, calls: int = 1, ago_hours: float = 0.5) -> None:
    when = dt.datetime.now(dt.UTC) - dt.timedelta(hours=ago_hours)
    for index in range(calls):
        db.add(
            Summary(
                game_id=lead.game_id,
                game_platform_id=lead.id,
                audience="critic",
                status="fresh",
                input_fingerprint=f"fp-{ago_hours}-{index}-{usd}",
                version=index + 1,
                is_current=False,
                llm_provider="anthropic",
                llm_model="claude-sonnet-5",
                cost_usd=usd / calls,
                generated_at=when,
            )
        )
    db.flush()


def test_under_the_limit_nothing_is_refused(db, uow, db_settings, lead):
    spend(db, lead, usd=1.5)
    assert ai_budget_exceeded(uow, db_settings) is None


def test_the_daily_cost_ceiling_stops_generation(db, uow, db_settings, lead, monkeypatch):
    monkeypatch.setattr(db_settings, "ai_daily_cost_limit_usd", 10.0)
    spend(db, lead, usd=10.01)
    result = ai_budget_exceeded(uow, db_settings)
    assert result is not None
    reason, retry_after_s = result
    assert "$10.01 из $10.00" in reason
    # The delay is the half that makes deferral possible: without it a caller can only
    # fail or pretend to succeed, and this one used to pretend.
    assert 300 <= retry_after_s <= 86_400


def test_the_hourly_call_ceiling_stops_a_runaway_loop(
    db, uow, db_settings, lead, monkeypatch
):
    """A loop can reach a daily cost cap in minutes; the call cap is the faster brake."""
    monkeypatch.setattr(db_settings, "ai_max_calls_per_hour", 5)
    spend(db, lead, usd=0.01, calls=6, ago_hours=0.1)
    result = ai_budget_exceeded(uow, db_settings)
    assert result is not None
    reason, retry_after_s = result
    assert "Обращений к модели за час: 6" in reason
    assert 60 <= retry_after_s <= 3600, "an hourly window resets within the hour"


def test_yesterdays_spend_does_not_count_against_today(
    db, uow, db_settings, lead, monkeypatch
):
    monkeypatch.setattr(db_settings, "ai_daily_cost_limit_usd", 10.0)
    spend(db, lead, usd=50.0, ago_hours=30)
    assert ai_budget_exceeded(uow, db_settings) is None


def test_a_zero_limit_disables_the_ceiling_rather_than_blocking_everything(
    db, uow, db_settings, lead, monkeypatch
):
    """0 has to mean "no limit"; the alternative is a config typo that stops all AI."""
    monkeypatch.setattr(db_settings, "ai_daily_cost_limit_usd", 0.0)
    monkeypatch.setattr(db_settings, "ai_max_calls_per_hour", 0)
    spend(db, lead, usd=999.0, calls=500, ago_hours=0.1)
    assert ai_budget_exceeded(uow, db_settings) is None


def test_a_summary_that_cost_nothing_is_not_counted_as_a_call(
    db, uow, db_settings, lead, monkeypatch
):
    """Skips and rejections are recorded as summaries too, without a model behind them."""
    monkeypatch.setattr(db_settings, "ai_max_calls_per_hour", 2)
    for index in range(5):
        db.add(
            Summary(
                game_id=lead.game_id,
                game_platform_id=lead.id,
                audience="user",
                status="skipped_no_data",
                input_fingerprint=f"skip-{index}",
                version=index + 1,
                is_current=False,
                llm_model=None,
                generated_at=dt.datetime.now(dt.UTC),
            )
        )
    db.flush()
    assert ai_budget_exceeded(uow, db_settings) is None


class TestTheGuardIsActuallyWired:
    """On a real corpus, because the guard sits after the has-anything-to-say checks.

    That ordering is deliberate: a game with no reviews should skip as
    ``below_threshold``, not raise a budget alarm it was never going to spend against.
    """

    def test_generate_refuses_before_calling_the_model(
        self, db, uow, load_harvest, monkeypatch
    ):
        from app.adapters.llm.fixture import FixtureLLMProvider
        from app.config import Settings
        from app.domain.enums import Audience
        from app.services.summary_service import SummaryService

        game_id, gp_id = load_harvest("nba-2k27", "playstation-5")
        settings = Settings(
            admin_token="t" * 32,
            app_env="test",
            llm_enabled=True,
            ai_daily_cost_limit_usd=1.0,
            summary_min_critic_reviews=5,
        )
        lead = uow.games.get_platform(gp_id)
        spend(db, lead, usd=5.0)

        llm = FixtureLLMProvider()
        # Raised, not returned. A returned outcome completes the job and spends its
        # idempotency key -- a deferral in name only, and how a whole catalogue kept its
        # old summaries while every job reported success. BudgetExhausted is what the
        # worker already understands as "come back later".
        with pytest.raises(BudgetExhausted) as caught:
            SummaryService(llm, settings).generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC
            )

        assert caught.value.retry_after_s > 0, "a deferral needs a time to come back at"
        assert llm.calls == 0, "the ceiling has to stop the call, not report it afterwards"

    def test_the_refusal_is_visible_to_an_operator(self, db, uow, load_harvest):
        from app.adapters.llm.fixture import FixtureLLMProvider
        from app.config import Settings
        from app.domain.enums import Audience
        from app.services.summary_service import SummaryService

        game_id, gp_id = load_harvest("nba-2k27", "playstation-5")
        settings = Settings(
            admin_token="t" * 32,
            app_env="test",
            llm_enabled=True,
            ai_daily_cost_limit_usd=1.0,
            summary_min_critic_reviews=5,
        )
        spend(db, uow.games.get_platform(gp_id), usd=5.0)

        with pytest.raises(BudgetExhausted):
            SummaryService(FixtureLLMProvider(), settings).generate(
                uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC
            )
        db.flush()

        assert "ai.budget_exhausted" in [e.event for e in uow.events.recent(limit=20)]

    def test_the_game_keeps_the_summary_it_already_had(self, db, uow, load_harvest):
        """A budget stop must not demote a good summary to a skip."""
        from app.adapters.llm.fixture import FixtureLLMProvider
        from app.config import Settings
        from app.domain.enums import Audience
        from app.services.summary_service import SummaryService

        game_id, gp_id = load_harvest("nba-2k27", "playstation-5")
        settings = Settings(
            admin_token="t" * 32, app_env="test", llm_enabled=True,
            summary_min_critic_reviews=5,
        )
        SummaryService(FixtureLLMProvider(), settings).generate(
            uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC
        )
        db.flush()
        before = uow.summaries.current(game_platform_id=gp_id, audience=Audience.CRITIC)
        assert before is not None

        spend(db, uow.games.get_platform(gp_id), usd=5.0)
        broke = settings.model_copy(update={"ai_daily_cost_limit_usd": 1.0})
        SummaryService(FixtureLLMProvider(), broke).generate(
            uow, game_id=game_id, game_platform_id=gp_id, audience=Audience.CRITIC
        )
        db.flush()

        after = uow.summaries.current(game_platform_id=gp_id, audience=Audience.CRITIC)
        assert after is not None
        assert after.id == before.id, "the reader keeps the summary that was already there"
        # And no placeholder row was written for the refused attempt: a budget stop is a
        # deferral, not a result, so nothing is recorded as skipped either.
        skipped = db.execute(
            sa.select(sa.func.count())
            .select_from(Summary)
            .where(
                Summary.game_platform_id == gp_id,
                Summary.audience == "critic",
                Summary.status == "skipped_no_data",
            )
        ).scalar_one()
        assert skipped == 0
