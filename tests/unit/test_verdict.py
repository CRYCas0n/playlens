from __future__ import annotations

import pytest

from app.domain.scores import critic_score, user_score
from app.domain.verdict import (
    NOTHING_TO_SAY,
    VerdictKind,
    agreement_delta,
    consensus_note,
    platform_line,
    verdict_line,
)


class TestVerdictTable:
    def test_agreement(self):
        v = verdict_line(critic_score(93, 118), user_score(8.9, 14204))
        assert v.kind is VerdictKind.AGREE
        assert v.line == "Critics rate this among the year's best, and players agree."
        assert v.delta == 4
        assert v.derived is True

    def test_players_lower(self):
        # design/EDGE_CASES.md case 19: northlight-drifters, 88 vs 51.
        v = verdict_line(critic_score(88, 60), user_score(5.1, 3000))
        assert v.kind is VerdictKind.PLAYERS_LOWER
        assert "players rate it 37 points lower" in v.line
        assert v.delta == 37

    def test_players_higher(self):
        # case 20: neon-district-2087, 61 vs 79.
        v = verdict_line(critic_score(61, 40), user_score(7.9, 2000))
        assert v.kind is VerdictKind.PLAYERS_HIGHER
        assert "players rate it 18 points higher" in v.line
        assert v.delta == -18

    def test_no_critic_score(self):
        v = verdict_line(critic_score(None, None), user_score(8.2, 300))
        assert v.kind is VerdictKind.PLAYER_ONLY
        # 8.2 -> 82 -> Good tier, so the player sentence is the "reservations" one.
        assert v.line.startswith("Players recommend it with reservations")
        assert "No critic score yet." in v.line
        assert verdict_line(
            critic_score(None, None), user_score(8.9, 300)
        ).line.startswith("Players rate this among the year's best")

    def test_no_player_score(self):
        v = verdict_line(critic_score(74, 20), user_score(0, 1))
        assert v.kind is VerdictKind.CRITIC_ONLY
        assert "No player score yet." in v.line

    def test_neither(self):
        v = verdict_line(critic_score(None, None), user_score(None, 0))
        assert v.kind is VerdictKind.NONE
        assert v.line == NOTHING_TO_SAY

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [(6, VerdictKind.AGREE), (7, VerdictKind.PLAYERS_LOWER), (8, VerdictKind.PLAYERS_LOWER)],
    )
    def test_threshold_boundary(self, delta, expected):
        v = verdict_line(critic_score(80, 50), user_score((80 - delta) / 10, 500))
        assert v.kind is expected

    def test_threshold_is_configurable(self):
        v = verdict_line(
            critic_score(80, 50), user_score(7.2, 500), agreement_threshold=15
        )
        assert v.kind is VerdictKind.AGREE

    def test_never_contradicts_the_numbers(self):
        """A regression guard for the whole point of ADR-012."""
        for meta in range(0, 101, 7):
            for user_raw in [x / 10 for x in range(0, 101, 13)]:
                c, u = critic_score(meta, 50), user_score(user_raw, 500)
                v = verdict_line(c, u)
                if v.delta is not None:
                    assert v.delta == (c.normalized or 0) - (u.normalized or 0)
                    if v.kind is VerdictKind.PLAYERS_LOWER:
                        assert v.delta > 0
                    if v.kind is VerdictKind.PLAYERS_HIGHER:
                        assert v.delta < 0


class TestSignals:
    def test_signals_are_carried_verbatim(self):
        strengths = ("Map-as-progression turns exploration into the reward loop",)
        watch_outs = ("The first four hours withhold too much",)
        v = verdict_line(
            critic_score(93, 118), user_score(8.9, 14204),
            strengths=strengths, watch_outs=watch_outs,
        )
        assert v.strengths == strengths
        assert v.watch_outs == watch_outs


class TestPlatformLine:
    def test_real_cyberpunk_case(self):
        line = platform_line(
            selected_name="PlayStation 4",
            selected_metascore=critic_score(57, 38),
            best_name="PC",
            best_metascore=critic_score(86, 92),
        )
        assert line == "On PlayStation 4 critics rate this 29 points lower than on PC."

    def test_silent_when_gap_is_small(self):
        assert (
            platform_line(
                selected_name="Xbox Series X",
                selected_metascore=critic_score(87, 30),
                best_name="PC",
                best_metascore=critic_score(89, 40),
            )
            is None
        )

    @pytest.mark.parametrize(("gap", "expected"), [(9, False), (10, True), (11, True)])
    def test_boundary(self, gap, expected):
        line = platform_line(
            selected_name="PS4",
            selected_metascore=critic_score(80 - gap, 10),
            best_name="PC",
            best_metascore=critic_score(80, 10),
        )
        assert (line is not None) is expected

    def test_silent_for_the_best_platform_itself(self):
        assert (
            platform_line(
                selected_name="PC",
                selected_metascore=critic_score(86, 92),
                best_name="PC",
                best_metascore=critic_score(86, 92),
            )
            is None
        )

    def test_silent_without_scores(self):
        assert (
            platform_line(
                selected_name="PS5",
                selected_metascore=critic_score(None, None),
                best_name="PC",
                best_metascore=critic_score(86, 92),
            )
            is None
        )


class TestConsensusNote:
    def test_warning_tone_when_players_are_much_lower(self):
        note = consensus_note(critic_score(88, 60), user_score(5.1, 3000))
        assert note.tone == "warning"
        assert "37 points lower" in note.text

    def test_neutral_when_players_are_higher(self):
        note = consensus_note(critic_score(61, 40), user_score(7.9, 2000))
        assert note.tone == "neutral"
        assert "18 points higher" in note.text

    def test_neutral_when_agreeing(self):
        note = consensus_note(critic_score(93, 118), user_score(8.9, 14204))
        assert note.tone == "neutral"
        assert "broadly agree" in note.text

    def test_one_sided(self):
        note = consensus_note(critic_score(93, 118), user_score(0, 1))
        assert note.delta is None
        assert "nothing to compare" in note.text


def test_agreement_delta_requires_both_sides():
    assert agreement_delta(critic_score(90, 10), user_score(8.0, 100)) == 10
    assert agreement_delta(critic_score(None, None), user_score(8.0, 100)) is None
