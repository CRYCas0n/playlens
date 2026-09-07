"""The transcript cascade, and the failure it exists to survive.

Release 0 traced YouTube's ``timedtext`` endpoint step by step and found it answers a
correctly signed request with HTTP 200 and a zero-byte body when no proof-of-origin token
is present. The whole cascade is built around refusing to read that as "no captions".
"""

from __future__ import annotations

import pytest

from app.adapters.youtube.transcripts.base import (
    TranscriptCascade,
    TranscriptTooShort,
)
from app.adapters.youtube.transcripts.providers import (
    MetadataOnlyProvider,
    clean_captions,
)
from app.domain.errors import ProviderError
from app.domain.models import TranscriptResult, VideoCandidate

VIDEO = VideoCandidate(
    video_id="abc123",
    title="Ashen Veil Let's Play - Part 1",
    channel_id="ch",
    channel_title="Channel",
    description="",
    duration_s=4800,
    view_count=150_000,
    like_count=6_000,
    published_at=None,
    has_captions=True,
    default_audio_language="en",
    live_broadcast="none",
    thumbnail_url=None,
)


class FakeProvider:
    def __init__(self, name, *, result=None, error=None, enabled=True):
        self.name = name
        self.enabled = enabled
        self._result = result
        self._error = error
        self.calls = 0

    def fetch(self, video):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


def cascade(providers, settings, minimum=2000):
    from dataclasses import replace as _replace  # noqa: F401

    settings.yt_min_transcript_chars = minimum
    return TranscriptCascade(providers, settings)


def transcript(chars: int, source: str = "ytdlp") -> TranscriptResult:
    return TranscriptResult(text="word " * (chars // 5), source=source, language="en")


class TestTheEmptyTwoHundred:
    def test_an_empty_result_is_a_provider_failure_not_an_answer(self, test_settings):
        """The exact defect from BLUEPRINT section 2.2.2, step 3."""
        first = FakeProvider("ytdlp", result=None)
        second = FakeProvider("hosted_api", result=transcript(5000, "hosted_api"))
        outcome = cascade([first, second], test_settings).fetch(VIDEO)

        assert outcome.ok
        assert outcome.result.source == "hosted_api"
        # The point: the cascade CONTINUED. Reading the empty result as "no captions"
        # would have stopped here and recorded a permanent false negative.
        assert second.calls == 1
        assert outcome.attempts[0] == {
            **outcome.attempts[0],
            "provider": "ytdlp",
            "ok": False,
        }
        assert "empty_or_too_short" in outcome.attempts[0]["error"]

    def test_a_short_result_is_treated_the_same_way(self, test_settings):
        """A 200-character 'transcript' is a truncated response, not a short video."""
        first = FakeProvider("ytdlp", result=transcript(200))
        second = FakeProvider("hosted_api", result=transcript(5000, "hosted_api"))
        outcome = cascade([first, second], test_settings).fetch(VIDEO)
        assert outcome.result.source == "hosted_api"
        assert "need 2000" in outcome.attempts[0]["error"]

    def test_the_hard_case_every_provider_returns_nothing(self, test_settings):
        outcome = cascade(
            [FakeProvider("ytdlp", result=None), MetadataOnlyProvider()], test_settings
        ).fetch(VIDEO)
        assert not outcome.ok
        assert [a["provider"] for a in outcome.attempts] == ["ytdlp", "metadata_only"]
        assert all(a["ok"] is False for a in outcome.attempts)


class TestCascadeBehaviour:
    def test_the_first_good_result_stops_the_cascade(self, test_settings):
        first = FakeProvider("ytdlp", result=transcript(5000))
        second = FakeProvider("hosted_api", result=transcript(9000, "hosted_api"))
        outcome = cascade([first, second], test_settings).fetch(VIDEO)
        assert outcome.result.source == "ytdlp"
        assert second.calls == 0

    def test_a_disabled_provider_is_recorded_rather_than_skipped_silently(
        self, test_settings
    ):
        outcome = cascade(
            [
                FakeProvider("ytdlp", enabled=False),
                FakeProvider("hosted_api", result=transcript(5000, "hosted_api")),
            ],
            test_settings,
        ).fetch(VIDEO)
        assert outcome.attempts[0] == {**outcome.attempts[0], "error": "disabled"}

    def test_a_provider_error_does_not_stop_the_cascade(self, test_settings):
        outcome = cascade(
            [
                FakeProvider("ytdlp", error=ProviderError("network is on fire")),
                FakeProvider("hosted_api", result=transcript(5000, "hosted_api")),
            ],
            test_settings,
        ).fetch(VIDEO)
        assert outcome.ok
        assert "network is on fire" in outcome.attempts[0]["error"]

    def test_a_provider_that_crashes_outright_cannot_kill_the_job(self, test_settings):
        """YouTube is isolated: a third-party client must never fail a game (ADR-016)."""
        outcome = cascade(
            [
                FakeProvider("ytdlp", error=RuntimeError("yt-dlp exploded")),
                FakeProvider("hosted_api", result=transcript(5000, "hosted_api")),
            ],
            test_settings,
        ).fetch(VIDEO)
        assert outcome.ok
        assert "RuntimeError" in outcome.attempts[0]["error"]

    def test_a_too_short_error_is_distinguishable_from_a_network_error(
        self, test_settings
    ):
        """Different failures need different fixes, so they are recorded differently."""
        outcome = cascade(
            [FakeProvider("ytdlp", error=TranscriptTooShort("empty body with HTTP 200"))],
            test_settings,
        ).fetch(VIDEO)
        assert "empty body with HTTP 200" in outcome.attempts[0]["error"]

    def test_every_attempt_is_timestamped(self, test_settings):
        outcome = cascade(
            [FakeProvider("ytdlp", result=transcript(5000))], test_settings
        ).fetch(VIDEO)
        assert outcome.attempts[0]["ts"]
        assert outcome.attempts[0]["chars"] == pytest.approx(5000, abs=5)


class TestMetadataOnly:
    def test_it_never_produces_a_transcript(self):
        """Its job is to make 'nothing to read' an explicit, recorded outcome."""
        assert MetadataOnlyProvider().fetch(VIDEO) is None
        assert MetadataOnlyProvider().enabled is True


class TestCaptionCleaning:
    def test_markup_and_sound_tags_are_removed(self):
        raw = "<c>Hello</c> [Music] there [Applause] friends"
        assert clean_captions(raw) == "Hello there friends"

    def test_webvtt_timing_lines_are_dropped(self):
        raw = "1\n00:00:01.000 --> 00:00:03.000\nhello there\n"
        assert clean_captions(raw) == "hello there"

    def test_rolling_duplicate_lines_are_collapsed(self):
        """YouTube repeats each line with one more word appended; naive joining triples it."""
        raw = "so I walk\nso I walk into\nso I walk into the room"
        assert clean_captions(raw) == "so I walk into the room"

    def test_empty_input_gives_empty_output_not_whitespace(self):
        assert clean_captions("   \n \n ") == ""


class TestAManifestIsNotATranscript:
    """Found on a live video, not in a fixture.

    yt-dlp offered an `m3u8` caption track for a real Elden Ring playthrough. The
    download was a 3,791-character HLS manifest — a list of URLs pointing at the actual
    subtitle segments. It passed the minimum-length check and would have been handed to
    the model as a transcript, which is precisely the failure ADR-016 exists to prevent:
    a response that looks like success and is not.
    """

    MANIFEST = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-TARGETDURATION:600\n#EXTINF:600,\n"
        "https://www.youtube.com/api/timedtext?caps=asr&v=abc&lang=en-US&expire=1788\n"
        "#EXTINF:600,\n"
        "https://www.youtube.com/api/timedtext?caps=asr&v=abc&lang=en-US&expire=1789\n"
    ) * 12

    def test_a_manifest_is_rejected(self):
        from app.adapters.youtube.transcripts.providers import looks_like_subtitles

        assert len(self.MANIFEST) > 2000, "the point is that it is long enough to pass"
        assert looks_like_subtitles(self.MANIFEST) is False

    def test_a_body_that_is_mostly_urls_is_rejected(self):
        from app.adapters.youtube.transcripts.providers import looks_like_subtitles

        assert looks_like_subtitles("https://example.com/a " * 200) is False

    def test_real_speech_is_accepted(self):
        from app.adapters.youtube.transcripts.providers import looks_like_subtitles

        speech = (
            "okay so we're heading into the castle now and honestly the framerate "
            "here has been rough since the patch but the combat still feels great "
        ) * 20
        assert looks_like_subtitles(speech) is True

    def test_speech_that_mentions_a_url_is_still_speech(self):
        """A false rejection costs a transcript; the threshold is share, not presence."""
        from app.adapters.youtube.transcripts.providers import looks_like_subtitles

        text = (
            "check the description or go to https://example.com/guide for the build "
            "but honestly you can work it out from the item descriptions themselves "
        ) * 20
        assert looks_like_subtitles(text) is True

    def test_only_readable_subtitle_formats_are_offered(self):
        """The `formats[0]` fallback is what picked the m3u8 track."""
        from app.adapters.youtube.transcripts.providers import (
            SUBTITLE_FORMATS,
            YtDlpProvider,
        )

        assert "m3u8" not in SUBTITLE_FORMATS
        assert YtDlpProvider._pick_format([{"ext": "m3u8", "url": "http://x/1"}]) is None
        assert YtDlpProvider._pick_format([]) is None
        assert (
            YtDlpProvider._pick_format(
                [{"ext": "m3u8", "url": "http://x/1"}, {"ext": "json3", "url": "http://x/2"}]
            )
            == "http://x/2"
        )

    def test_the_cascade_treats_it_as_a_provider_failure_and_continues(self, test_settings):
        """The whole point: one provider returning rubbish must not end the search."""
        from app.adapters.youtube.transcripts.providers import NotSubtitles

        first = FakeProvider("ytdlp", error=NotSubtitles("m3u8 manifest, not subtitles"))
        second = FakeProvider("hosted_api", result=transcript(5000, "hosted_api"))
        outcome = cascade([first, second], test_settings).fetch(VIDEO)

        assert outcome.ok
        assert outcome.result.source == "hosted_api"
        assert "not subtitles" in outcome.attempts[0]["error"]
