"""Score model tests.

The numbers in the parametrisations are the real ones from
docs/research-fixtures/release0/harvest/*.json -- see CONTRADICTIONS C-05.
"""

from __future__ import annotations

import pytest

from app.domain.enums import ScoreStatus, Tier
from app.domain.scores import (
    critic_score,
    make_score,
    normalize,
    sort_key,
    tier_of,
    user_score,
)


class TestTierThresholds:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (100, Tier.EXCELLENT),
            (85, Tier.EXCELLENT),
            (84, Tier.GOOD),
            (70, Tier.GOOD),
            (69, Tier.MIXED),
            (50, Tier.MIXED),
            (49, Tier.POOR),
            (0, Tier.POOR),
            (None, Tier.NONE),
        ],
    )
    def test_boundaries(self, value, expected):
        assert tier_of(value) is expected

    def test_zero_is_poor_not_not_rated(self):
        # design/EDGE_CASES.md section 3: tier(0) -> Poor, tier(None) -> Not rated.
        assert tier_of(0) is Tier.POOR
        assert tier_of(None) is Tier.NONE


class TestNormalisation:
    @pytest.mark.parametrize(
        ("raw", "scale", "expected"),
        [(8.9, 10, 89), (3.8, 10, 38), (10.0, 10, 100), (0.5, 10, 5), (86, 100, 86), (None, 10, None)],
    )
    def test_one_axis(self, raw, scale, expected):
        assert normalize(raw, scale) == expected


class TestUserscoreZeroRule:
    """The four false zeros and the two genuine low scores from the Release 0 harvest."""

    @pytest.mark.parametrize(
        ("game", "raw", "count"),
        [
            ("nba-2k27", 0, 2),
            ("bioeden", 0, 1),
            ("lous-lagoon", 0, 1),
            ("blood-dungeon", 0, 3),
        ],
    )
    def test_zero_with_tiny_sample_is_unavailable(self, game, raw, count):
        score = user_score(raw, count)
        assert score.status is ScoreStatus.UNAVAILABLE, game
        assert score.value is None
        assert score.normalized is None
        assert score.tier is Tier.NONE
        assert score.tier_label == "Not rated"

    @pytest.mark.parametrize(
        ("game", "raw", "count", "expected_norm"),
        [
            ("diablo-immortal", 0.5, 6382, 5),
            ("warcraft-iii-reforged", 0.6, 31276, 6),
            ("cyberpunk-2077-ps4", 3.8, 10876, 38),
            ("elden-ring-ps5", 8.4, 24375, 84),
        ],
    )
    def test_genuine_scores_survive(self, game, raw, count, expected_norm):
        score = user_score(raw, count)
        assert score.status is ScoreStatus.VALID, game
        assert score.normalized == expected_norm

    def test_null_is_unavailable(self):
        assert user_score(None, 0).status is ScoreStatus.UNAVAILABLE
        assert user_score(None, 500).status is ScoreStatus.UNAVAILABLE

    def test_zero_count_is_unavailable_even_with_a_score(self):
        assert user_score(7.5, 0).status is ScoreStatus.UNAVAILABLE

    def test_threshold_boundary(self):
        assert user_score(0, 19).status is ScoreStatus.UNAVAILABLE
        assert user_score(0, 20).status is ScoreStatus.VALID

    def test_threshold_is_configurable(self):
        assert user_score(0, 5, zero_min_ratings=3).status is ScoreStatus.VALID

    def test_raw_is_always_preserved(self):
        # The presentation rule must never destroy data (ADR-002).
        score = user_score(0, 2)
        assert score.raw == 0
        assert score.review_count == 2


class TestLowSample:
    def test_flagged_below_threshold(self):
        # Onimusha: a plausible-looking 8.7 backed by 77 ratings but only 10 texts.
        assert user_score(8.7, 12).low_sample is True
        assert user_score(8.7, 77).low_sample is False

    def test_unavailable_is_not_low_sample(self):
        assert user_score(0, 2).low_sample is False


class TestCriticScore:
    def test_scale_is_100(self):
        score = critic_score(96, 86)
        assert score.scale_max == 100
        assert score.normalized == 96
        assert score.tier is Tier.EXCELLENT

    def test_missing(self):
        # Xbox One row of Elden Ring: platform present, no critic score.
        assert critic_score(None, None).status is ScoreStatus.UNAVAILABLE


class TestSorting:
    def test_unavailable_sinks_in_both_directions(self):
        available = user_score(8.9, 1000)
        missing = user_score(0, 1)
        assert sort_key(available, descending=True) < sort_key(missing, descending=True)
        assert sort_key(available, descending=False) < sort_key(missing, descending=False)

    def test_ordering_within_available(self):
        high, low = critic_score(93, 100), critic_score(51, 100)
        assert sort_key(high, descending=True) < sort_key(low, descending=True)
        assert sort_key(low, descending=False) < sort_key(high, descending=False)

    def test_none_is_handled(self):
        assert sort_key(None, descending=True) == (1, 0.0)


class TestSerialisation:
    def test_dict_never_leaks_zero_for_missing(self):
        payload = user_score(0, 2).as_dict()
        assert payload["value"] is None
        assert payload["normalized"] is None
        assert payload["status"] == "unavailable"
        assert payload["tier_label"] == "Not rated"

    def test_dict_keeps_native_and_normalised(self):
        payload = user_score(8.9, 14204).as_dict()
        assert payload["value"] == 8.9
        assert payload["normalized"] == 89
        assert payload["scale_max"] == 10


def test_make_score_generic_path():
    score = make_score(42.0, scale_max=100, review_count=7, low_sample_threshold=10)
    assert score.status is ScoreStatus.VALID
    assert score.low_sample is True
