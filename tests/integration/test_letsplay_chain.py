"""The three Let's Play tasks have to hand the game to each other.

They existed, were registered, were individually tested, and were never linked.
`youtube.discover` selected a video and stopped there. In production that meant 180
successful discoveries, 2,285 videos ranked and scored, and not one transcript ever
attempted — the feature looked alive from every angle except the only one that counts.

The same shape as the crawl's follow-ups losing `game_id`: each handler did its own job
correctly and nothing carried the work forward. A test per handler cannot see it; only a
test of the joins can.
"""

from __future__ import annotations

import pytest

from app.db.uow import UnitOfWork
from app.tasks.registry import youtube_discover, youtube_transcript

pytestmark = pytest.mark.integration


class FakeLetsPlay:
    """Stands in for the service so the chain is tested, not the YouTube API."""

    def __init__(self, discover_result: dict, transcript_result: dict) -> None:
        self._discover = discover_result
        self._transcript = transcript_result

    def discover(self, uow, *, game_id: int) -> dict:
        return dict(self._discover)

    def fetch_transcript(self, uow, *, game_id: int) -> dict:
        return dict(self._transcript)


class FakeContainer:
    def __init__(self, letsplay: FakeLetsPlay) -> None:
        self.letsplay = letsplay


def queued_types(uow) -> list[str]:
    return [job.job_type for job in uow.jobs.recent(limit=50)]


class TestDiscoverHandsOverToTranscript:
    def test_a_selected_video_queues_its_transcript(self, session_factory, load_harvest):
        game_id, _ = load_harvest("elden-ring", "playstation-5")
        container = FakeContainer(
            FakeLetsPlay({"status": "selected", "video_id": "abc123"}, {})
        )

        with UnitOfWork(session_factory) as uow:
            result = youtube_discover(container, uow, {"game_id": game_id})
            types = queued_types(uow)

        assert result["queued_transcript"] is True
        assert "youtube.transcript" in types, types

    def test_no_suitable_video_queues_nothing(self, session_factory, load_harvest):
        """An absent Let's Play is a supported outcome, not a pipeline to keep feeding."""
        game_id, _ = load_harvest("elden-ring", "playstation-5")
        container = FakeContainer(FakeLetsPlay({"status": "no_match", "candidates": 12}, {}))

        with UnitOfWork(session_factory) as uow:
            youtube_discover(container, uow, {"game_id": game_id})
            types = queued_types(uow)

        assert "youtube.transcript" not in types, types

    def test_re_running_discovery_does_not_re_queue_the_same_video(
        self, session_factory, load_harvest
    ):
        game_id, _ = load_harvest("elden-ring", "playstation-5")
        container = FakeContainer(
            FakeLetsPlay({"status": "selected", "video_id": "abc123"}, {})
        )

        with UnitOfWork(session_factory) as uow:
            youtube_discover(container, uow, {"game_id": game_id})
        with UnitOfWork(session_factory) as uow:
            second = youtube_discover(container, uow, {"game_id": game_id})

        assert second["queued_transcript"] is False, "the key must be the video"


class TestTranscriptHandsOverToTheSummary:
    @pytest.mark.parametrize("status", ["available", "cached"])
    def test_a_transcript_queues_the_summary(self, session_factory, load_harvest, status):
        """`cached` counts too: a transcript we already hold still has no summary written
        from it the first time the chain runs to the end."""
        game_id, _ = load_harvest("elden-ring", "playstation-5")
        container = FakeContainer(FakeLetsPlay({}, {"status": status, "chars": 9000}))

        with UnitOfWork(session_factory) as uow:
            result = youtube_transcript(container, uow, {"game_id": game_id})
            types = queued_types(uow)

        assert result["queued_summary"] is True
        assert "youtube.summarise" in types, types

    def test_an_unavailable_transcript_writes_no_summary(
        self, session_factory, load_harvest
    ):
        """ADR-016: no transcript means no AI conclusion. Writing an impression from a
        video title would be invention at the highest reputational cost."""
        game_id, _ = load_harvest("elden-ring", "playstation-5")
        container = FakeContainer(
            FakeLetsPlay({}, {"status": "unavailable", "attempts": [{"ok": False}]})
        )

        with UnitOfWork(session_factory) as uow:
            youtube_transcript(container, uow, {"game_id": game_id})
            types = queued_types(uow)

        assert "youtube.summarise" not in types, types
