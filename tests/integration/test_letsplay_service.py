"""Let's Play enrichment against a real database.

The assignment's scenario 3 says a YouTube quota exhaustion must not mark a game failed.
That is a claim about state after a failure, so it is tested here rather than reasoned
about in a document.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.adapters.youtube.transcripts.base import TranscriptCascade
from app.db.models import Game, GamePlatform, Platform, YoutubeVideo
from app.domain.errors import BudgetExhausted, ProviderError
from app.domain.models import TranscriptResult, VideoCandidate
from app.normalizers.text import normalize_title
from app.services.letsplay_service import YOUTUBE_BUDGET, LetsPlayService

pytestmark = pytest.mark.integration


@pytest.fixture
def yt_settings(db_settings, monkeypatch):
    monkeypatch.setattr(db_settings, "youtube_enabled", True)
    monkeypatch.setattr(db_settings, "yt_min_score", 60)
    return db_settings


@pytest.fixture
def game(db):
    platform = db.execute(sa.select(Platform).where(Platform.slug == "pc")).scalar_one()
    row = Game(
        mc_slug="ashen-veil",
        mc_url="https://www.metacritic.com/game/ashen-veil/",
        title="Ashen Veil",
        title_norm=normalize_title("Ashen Veil"),
        best_metascore=93,
        best_metascore_count=118,
        release_date=dt.date(2026, 2, 10),
    )
    db.add(row)
    db.flush()
    db.add(GamePlatform(game_id=row.id, platform_id=platform.id, is_lead=True))
    db.commit()
    return row


def candidate(video_id: str, title: str, **kwargs) -> VideoCandidate:
    defaults = {
        "channel_id": "ch",
        "channel_title": "Channel",
        "description": "",
        "duration_s": 4800,
        "view_count": 150_000,
        "like_count": 6_000,
        "published_at": dt.datetime(2026, 2, 20, tzinfo=dt.UTC),
        "has_captions": True,
        "default_audio_language": "en",
        "live_broadcast": "none",
        "thumbnail_url": None,
        "embeddable": True,
    }
    defaults.update(kwargs)
    return VideoCandidate(video_id=video_id, title=title, **defaults)


class FakeYoutube:
    name = "fake"
    enabled = True

    def __init__(self, candidates=None, error=None):
        self._candidates = candidates or []
        self._error = error
        self.calls = 0

    def search_units(self):
        return 101

    def discover(self, query, *, max_results):
        self.calls += 1
        if self._error:
            raise self._error
        return self._candidates


class FakeProvider:
    def __init__(self, name, result=None):
        self.name = name
        self.enabled = True
        self._result = result

    def fetch(self, video):
        return self._result


def service(provider, settings, *, cascade=None, llm=None):
    from app.adapters.llm.base import NullLLMProvider

    return LetsPlayService(provider, llm or NullLLMProvider(), settings, cascade=cascade)


class TestDiscovery:
    def test_a_playthrough_is_selected_and_the_trailer_is_kept_as_a_reject(
        self, db, uow, game, yt_settings
    ):
        provider = FakeYoutube(
            [
                candidate("trailer", "Ashen Veil - Official Launch Trailer",
                          view_count=12_000_000, duration_s=1400),
                candidate("lp1", "Ashen Veil Let's Play - Part 1"),
            ]
        )
        result = service(provider, yt_settings).discover(uow, game_id=game.id)
        db.commit()

        assert result["status"] == "selected"
        assert result["video_id"] == "lp1"

        rows = {
            r.video_id: r
            for r in db.execute(sa.select(YoutubeVideo)).scalars()
        }
        assert rows["lp1"].is_selected is True
        # The reject is stored WITH its reason, which is the only way to answer
        # "why did it not pick the video with twelve million views" later.
        assert rows["trailer"].is_selected is False
        assert rows["trailer"].rejected_reason == "trailer"

    def test_no_suitable_video_selects_nothing(self, db, uow, game, yt_settings):
        provider = FakeYoutube(
            [candidate("t", "Ashen Veil - Official Trailer", duration_s=1400)]
        )
        result = service(provider, yt_settings).discover(uow, game_id=game.id)
        db.commit()
        assert result["status"] == "no_match"
        assert uow.youtube.selected(game.id) is None

    def test_the_search_is_spent_once_per_game(self, db, uow, game, yt_settings):
        provider = FakeYoutube([candidate("lp1", "Ashen Veil Let's Play - Part 1")])
        svc = service(provider, yt_settings)
        svc.discover(uow, game_id=game.id)
        db.commit()
        second = svc.discover(uow, game_id=game.id)
        assert second == {"status": "skipped", "reason": "already_searched"}
        assert provider.calls == 1

    def test_quota_is_charged_even_when_the_response_is_useless(
        self, db, uow, game, yt_settings
    ):
        """Counting only successful searches is how a daily budget silently overruns."""
        provider = FakeYoutube([])
        service(provider, yt_settings).discover(uow, game_id=game.id)
        db.commit()
        remaining = uow.budgets.remaining(
            YOUTUBE_BUDGET, dt.date.today(),
            limit=yt_settings.youtube_daily_unit_budget,
        )
        assert remaining == yt_settings.youtube_daily_unit_budget - 101

    def test_an_exhausted_budget_defers_instead_of_searching(
        self, db, uow, game, yt_settings
    ):
        uow.budgets.spend(
            YOUTUBE_BUDGET, dt.date.today(), units=9_499, call="test",
            limit=yt_settings.youtube_daily_unit_budget,
        )
        db.commit()
        provider = FakeYoutube([candidate("lp1", "Ashen Veil Let's Play - Part 1")])
        result = service(provider, yt_settings).discover(uow, game_id=game.id)
        assert result["status"] == "deferred"
        assert provider.calls == 0

    def test_a_quota_error_does_not_fail_the_game(self, db, uow, game, yt_settings):
        """Assignment scenario 3, asserted as state rather than described in prose."""
        provider = FakeYoutube(error=BudgetExhausted("quotaExceeded"))
        result = service(provider, yt_settings).discover(uow, game_id=game.id)
        db.commit()

        assert result["status"] == "deferred"
        refreshed = db.get(Game, game.id)
        assert refreshed.best_metascore == 93
        assert refreshed.detail_synced_at == game.detail_synced_at

    def test_a_provider_error_is_reported_without_raising(self, db, uow, game, yt_settings):
        provider = FakeYoutube(error=ProviderError("boom"))
        assert service(provider, yt_settings).discover(uow, game_id=game.id)["status"] == (
            "failed"
        )

    def test_a_low_interest_game_never_costs_a_search(self, db, uow, game, yt_settings):
        db.execute(sa.update(Game).where(Game.id == game.id).values(best_metascore=41))
        db.commit()
        provider = FakeYoutube([candidate("lp1", "Ashen Veil Let's Play - Part 1")])
        result = service(provider, yt_settings).discover(uow, game_id=game.id)
        assert result == {"status": "skipped", "reason": "below_interest_threshold"}
        assert provider.calls == 0

    def test_everything_is_skipped_when_the_feature_is_off(self, db, uow, game, db_settings):
        provider = FakeYoutube([candidate("lp1", "Ashen Veil Let's Play - Part 1")])
        result = service(provider, db_settings).discover(uow, game_id=game.id)
        assert result == {"status": "skipped", "reason": "youtube_disabled"}
        assert provider.calls == 0


class TestTranscript:
    @pytest.fixture
    def selected(self, db, uow, game, yt_settings):
        provider = FakeYoutube([candidate("lp1", "Ashen Veil Let's Play - Part 1")])
        service(provider, yt_settings).discover(uow, game_id=game.id)
        db.commit()
        return uow.youtube.selected(game.id)

    def test_a_transcript_is_stored_with_its_source(
        self, db, uow, game, yt_settings, selected
    ):
        cascade = TranscriptCascade(
            [FakeProvider("ytdlp", TranscriptResult(text="w " * 3000, source="ytdlp"))],
            yt_settings,
        )
        result = service(
            FakeYoutube(), yt_settings, cascade=cascade
        ).fetch_transcript(uow, game_id=game.id)
        db.commit()

        assert result["status"] == "available"
        row = uow.youtube.transcript(selected.id)
        assert row.status == "available"
        assert row.source == "ytdlp"
        assert row.attempts[0]["ok"] is True

    def test_a_failed_cascade_records_every_attempt(
        self, db, uow, game, yt_settings, selected
    ):
        """Without the attempt log, 'no captions' and 'all providers broke' look alike."""
        cascade = TranscriptCascade(
            [FakeProvider("ytdlp", None), FakeProvider("hosted_api", None)], yt_settings
        )
        result = service(
            FakeYoutube(), yt_settings, cascade=cascade
        ).fetch_transcript(uow, game_id=game.id)
        db.commit()

        assert result["status"] == "unavailable"
        row = uow.youtube.transcript(selected.id)
        assert row.status == "unavailable"
        assert [a["provider"] for a in row.attempts] == ["ytdlp", "hosted_api"]
        assert "empty_or_too_short" in row.error_message

    def test_no_selected_video_is_a_skip_not_an_error(self, db, uow, game, yt_settings):
        result = service(FakeYoutube(), yt_settings).fetch_transcript(uow, game_id=game.id)
        assert result == {"status": "skipped", "reason": "no_selected_video"}


class TestSummary:
    def test_without_a_transcript_no_summary_is_ever_written(
        self, db, uow, game, yt_settings
    ):
        """The one thing ADR-016 forbids outright: an impression from a video title."""
        provider = FakeYoutube([candidate("lp1", "Ashen Veil Let's Play - Part 1")])
        svc = service(provider, yt_settings)
        svc.discover(uow, game_id=game.id)
        db.commit()

        result = svc.summarise(uow, game_id=game.id)
        assert result == {"status": "skipped", "reason": "no_transcript"}
        assert uow.summaries.current_for_game(game.id) == []

    def test_a_disabled_llm_leaves_the_section_showing_metadata_only(
        self, db, uow, game, yt_settings
    ):
        provider = FakeYoutube([candidate("lp1", "Ashen Veil Let's Play - Part 1")])
        cascade = TranscriptCascade(
            [FakeProvider("ytdlp", TranscriptResult(text="w " * 3000, source="ytdlp"))],
            yt_settings,
        )
        svc = service(provider, yt_settings, cascade=cascade)
        svc.discover(uow, game_id=game.id)
        svc.fetch_transcript(uow, game_id=game.id)
        db.commit()

        assert svc.summarise(uow, game_id=game.id) == {
            "status": "skipped",
            "reason": "llm_disabled",
        }
