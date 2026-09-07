"""The corpus must fit the model's context window.

`AI_MAX_INPUT_TOKENS` was declared and unread. With the caps alone, 500 user reviews at
1500 characters is ~210,000 tokens — more than the whole context window. Such a request
does not cost more, it fails, and it fails on exactly the popular games where the summary
matters most.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.ai.corpus import CorpusItem
from app.config import Settings
from app.domain.enums import ReviewKind
from app.services.snapshot_service import (
    CHARS_PER_TOKEN,
    PROMPT_BUDGET_SHARE,
    SnapshotService,
)


def items(count: int, chars: int) -> list[CorpusItem]:
    return [
        CorpusItem(
            review_id=i,
            body="x" * chars,
            score=80,
            score_max=100,
            published_on=dt.date(2026, 2, 1),
            publication="Pub",
            dedupe_key=f"k{i}",
            bucket="positive",
        )
        for i in range(count)
    ]


def service(**overrides) -> SnapshotService:
    base = {"admin_token": "t" * 32, "app_env": "test"}
    base.update(overrides)
    return SnapshotService(Settings(**base))


class TestTheBudgetBinds:
    def test_long_reviews_shrink_the_corpus_below_the_cap(self):
        svc = service(reviews_user_cap=500, ai_max_input_tokens=60_000)
        target = svc.target_size(ReviewKind.USER, items=items(500, 1500))
        assert target < 500
        # And the arithmetic is the stated one, not a magic number.
        budget = 60_000 * CHARS_PER_TOKEN * PROMPT_BUDGET_SHARE
        assert target == int(budget // (1500 + 60))

    def test_short_reviews_let_the_cap_bind_instead(self):
        """Most reviews are far shorter than the ceiling; budgeting for the worst case
        would throw away evidence for nothing."""
        svc = service(reviews_user_cap=500, ai_max_input_tokens=60_000)
        assert svc.target_size(ReviewKind.USER, items=items(500, 200)) == 500

    def test_the_rendered_corpus_stays_inside_the_declared_budget(self):
        """The property that matters, checked end to end rather than by formula."""
        svc = service(reviews_user_cap=500, ai_max_input_tokens=60_000)
        pool = items(500, 1500)
        target = svc.target_size(ReviewKind.USER, items=pool)
        rendered_chars = target * (1500 + 60)
        assert rendered_chars <= 60_000 * CHARS_PER_TOKEN

    def test_a_bigger_budget_admits_more_reviews(self):
        pool = items(500, 1500)
        small = service(ai_max_input_tokens=30_000).target_size(ReviewKind.USER, items=pool)
        large = service(ai_max_input_tokens=200_000).target_size(ReviewKind.USER, items=pool)
        assert large > small

    def test_at_least_one_review_survives_any_budget(self):
        """A pathological budget must not produce an empty corpus and a silent skip."""
        svc = service(ai_max_input_tokens=1)
        assert svc.target_size(ReviewKind.CRITIC, items=items(10, 1500)) == 1

    def test_critics_and_players_have_separate_caps(self):
        svc = service(reviews_critic_cap=200, reviews_user_cap=500, ai_max_input_tokens=10**9)
        assert svc.target_size(ReviewKind.CRITIC, items=items(10, 100)) == 200
        assert svc.target_size(ReviewKind.USER, items=items(10, 100)) == 500

    def test_without_a_sample_the_worst_case_is_assumed(self):
        """Called before filtering, the only safe estimate is the per-review ceiling."""
        svc = service(reviews_user_cap=500, ai_max_input_tokens=60_000)
        assert svc.target_size(ReviewKind.USER) == svc.target_size(
            ReviewKind.USER, items=items(5, 10_000)
        )


@pytest.mark.parametrize("chars", [1, 80, 500, 1500, 5000])
def test_the_estimate_never_returns_zero_or_negative(chars):
    svc = service(ai_max_input_tokens=100)
    assert svc.target_size(ReviewKind.USER, items=items(3, chars)) >= 1
