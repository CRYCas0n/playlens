"""Picking a playthrough out of a search result page (ADR-016 sections 3-4).

The candidate field below is modelled on what a real query returns: a trailer with twelve
million views at the top, a review, a Short, a no-commentary run, and two or three actual
playthroughs further down. Every test here asks the same question in a different way --
does popularity alone ever win?
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.adapters.youtube import ranking
from app.adapters.youtube.provider import parse_duration, search_query
from app.domain.models import VideoCandidate

GAME = "Ashen Veil"
RELEASE = dt.date(2026, 2, 10)


def video(
    video_id: str,
    title: str,
    *,
    duration_s: int | None = 4800,
    views: int | None = 100_000,
    likes: int | None = None,
    channel: str | None = "ch-a",
    published: dt.datetime | None = dt.datetime(2026, 2, 20, tzinfo=dt.UTC),
    captions: bool | None = True,
    language: str | None = "en",
    live: str | None = "none",
    embeddable: bool | None = True,
) -> VideoCandidate:
    # Likes track views by default (4%), because a fixture where a 70k video has the same
    # like count as a 150k one hands the smaller video a 2x engagement advantage that no
    # real result page would show -- and then the test measures the fixture, not the rule.
    if likes is None:
        likes = int((views or 0) * 0.04)
    return VideoCandidate(
        video_id=video_id,
        title=title,
        channel_id=channel,
        channel_title=channel,
        description="",
        duration_s=duration_s,
        view_count=views,
        like_count=likes,
        published_at=published,
        has_captions=captions,
        default_audio_language=language,
        live_broadcast=live,
        thumbnail_url=None,
        embeddable=embeddable,
    )


FIELD = [
    video("trailer", "Ashen Veil - Official Launch Trailer", duration_s=1500,
          views=12_000_000, channel="ch-publisher"),
    video("review", "Ashen Veil Review - Is It Worth It?", views=900_000,
          channel="ch-review"),
    video("short", "Ashen Veil #shorts", duration_s=45, views=3_000_000,
          channel="ch-clips"),
    video("silent", "Ashen Veil Full Playthrough - No Commentary", views=800_000,
          channel="ch-silent"),
    video("lp1", "Ashen Veil Let's Play - Part 1", views=150_000, channel="ch-lp"),
    video("lp2", "Ashen Veil Let's Play - Part 2", views=90_000, channel="ch-lp"),
    video("lp3", "Ashen Veil Let's Play - Part 3", views=70_000, channel="ch-lp"),
    video("other", "Northlight Drifters Walkthrough Part 1", views=500_000,
          channel="ch-other"),
]


def ranked():
    return ranking.rank(FIELD, game_title=GAME, release_date=RELEASE)


class TestHardFilters:
    def test_the_trailer_is_rejected_by_name(self):
        """Barrier 3. Twelve million views and nothing to summarise."""
        rejected = {r.candidate.video_id: r.rejected_reason for r in ranked()}
        assert rejected["trailer"] == "trailer"

    def test_a_review_is_not_a_playthrough(self):
        assert {r.candidate.video_id: r.rejected_reason for r in ranked()}["review"] == (
            "off_format"
        )

    def test_a_short_is_rejected_on_duration(self):
        assert {r.candidate.video_id: r.rejected_reason for r in ranked()}["short"] == (
            "too_short"
        )

    def test_a_video_about_a_different_game_is_rejected(self):
        """A wrong video is worse than no video: the section would confidently mislead."""
        assert {r.candidate.video_id: r.rejected_reason for r in ranked()}["other"] == (
            "title_mismatch"
        )

    def test_a_live_stream_is_rejected(self):
        results = ranking.rank(
            [video("live", "Ashen Veil Let's Play", live="live")],
            game_title=GAME,
            release_date=RELEASE,
        )
        assert results[0].rejected_reason == "live_broadcast"

    def test_an_unknown_duration_is_rejected_rather_than_treated_as_zero(self):
        results = ranking.rank(
            [video("x", "Ashen Veil Let's Play", duration_s=None)],
            game_title=GAME,
            release_date=RELEASE,
        )
        assert results[0].rejected_reason == "duration_unknown"

    def test_a_foreign_language_video_is_rejected(self):
        results = ranking.rank(
            [video("x", "Ashen Veil Let's Play", language="de")],
            game_title=GAME,
            release_date=RELEASE,
            allowed_languages=frozenset({"en"}),
        )
        assert results[0].rejected_reason == "language"

    def test_rejected_candidates_are_still_returned_with_their_reason(self):
        """A ranking whose rejects are discarded cannot be debugged afterwards."""
        results = ranked()
        assert len(results) == len(FIELD)
        assert all(r.score is None for r in results if r.rejected_reason)


class TestRanking:
    def test_the_most_viewed_survivor_does_not_automatically_win(self):
        """The whole point: 800k with no commentary loses to 150k with a real title."""
        winner = ranking.select(ranked(), min_score=0.0)
        assert winner.candidate.video_id == "lp1"

    def test_no_commentary_is_penalised_hardest(self):
        results = {r.candidate.video_id: r for r in ranked() if r.accepted}
        assert "penalty_no_commentary" in results["silent"].components
        assert results["silent"].components["penalty_no_commentary"] == -0.25

    def test_part_one_wins_a_tie_against_a_later_episode(self):
        """Episode one characterises the game; episode seven characterises one late area."""
        pair = [
            video("p1", "Ashen Veil Let's Play - Part 1", views=100_000),
            video("p7", "Ashen Veil Let's Play - Part 7", views=100_000),
        ]
        results = ranking.rank(pair, game_title=GAME, release_date=RELEASE)
        assert results[0].score == results[1].score
        assert results[0].candidate.video_id == "p1"

    def test_the_earliest_episode_of_the_winning_series_is_chosen(self):
        winner = ranking.select(ranked(), min_score=0.0)
        assert winner.candidate.video_id == "lp1"

    def test_a_channel_with_a_series_gets_a_signal(self):
        results = {r.candidate.video_id: r for r in ranked() if r.accepted}
        assert results["lp1"].components["series_signal"] == 1.0
        assert results["silent"].components["series_signal"] == 0.0

    def test_a_video_published_before_release_scores_zero_for_recency(self):
        """A preview build is a different game from the one that shipped."""
        early = video("pre", "Ashen Veil Let's Play - Part 1",
                      published=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        results = ranking.rank([early], game_title=GAME, release_date=RELEASE)
        assert results[0].components["recency_fit"] == 0.0

    def test_popularity_is_normalised_against_survivors_not_the_trailer(self):
        """Otherwise every real candidate is squashed to the bottom of the scale."""
        results = {r.candidate.video_id: r for r in ranked() if r.accepted}
        assert results["silent"].components["popularity"] == pytest.approx(1.0)

    def test_format_fit_peaks_between_thirty_minutes_and_three_hours(self):
        assert ranking._format_fit(1800) == 1.0
        assert ranking._format_fit(10800) == 1.0
        assert ranking._format_fit(900) < 1.0
        assert ranking._format_fit(20000) < 1.0

    def test_components_are_stored_so_a_choice_can_be_explained(self):
        winner = ranking.select(ranked(), min_score=0.0)
        assert set(winner.components) >= set(ranking.WEIGHTS)


class TestSelection:
    def test_nothing_is_selected_when_everything_is_weak(self):
        """No video is a supported outcome; padding the section is not."""
        assert ranking.select(ranked(), min_score=0.99) is None

    def test_nothing_is_selected_from_an_all_rejected_field(self):
        results = ranking.rank(
            [FIELD[0], FIELD[1], FIELD[2]], game_title=GAME, release_date=RELEASE
        )
        assert ranking.select(results, min_score=0.0) is None

    def test_an_empty_field_selects_nothing(self):
        assert ranking.select([], min_score=0.0) is None


class TestDurationParsing:
    @pytest.mark.parametrize(
        ("iso", "seconds"),
        [
            ("PT1H24M9S", 5049),
            ("PT48M", 2880),
            ("PT30S", 30),
            ("P1DT2H", 93600),
        ],
    )
    def test_iso8601(self, iso, seconds):
        assert parse_duration(iso) == seconds

    @pytest.mark.parametrize("bad", [None, "", "garbage", "P1Y"])
    def test_unparsable_is_none_not_zero(self, bad):
        """Zero would sail through a minimum-duration filter as a measurement."""
        assert parse_duration(bad) is None


def test_the_query_asks_for_a_playthrough_not_a_game():
    query = search_query("Ashen Veil")
    assert "Ashen Veil" in query
    assert "lets play" in query
