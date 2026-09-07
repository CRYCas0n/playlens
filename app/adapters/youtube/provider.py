"""YouTube Data API v3 access, on a budget.

``search.list`` costs 100 quota units and ``videos.list`` costs 1 for up to fifty ids, so
one discovery is 101 units and the practical ceiling is ~94 games a day against the
default 10,000. That arithmetic is the reason discovery is gated (ADR-016 section 2) and
budgeted per day rather than merely retried.

Search alone does not return duration, view count, caption availability or
``liveBroadcastContent`` — every signal the ranker needs. So discovery is always two
calls, and a provider that returned search results alone would be returning nothing
useful.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Protocol

from app.adapters.http.transport import HttpTransport
from app.config import Settings
from app.domain.errors import BudgetExhausted, ProviderError
from app.domain.models import VideoCandidate
from app.logging import get_logger

log = get_logger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
SEARCH_UNITS = 100
VIDEOS_UNITS = 1

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def parse_duration(value: str | None) -> int | None:
    """``PT1H24M9S`` -> 5049. Returns ``None`` rather than 0 for anything unparsable.

    Zero would pass a "shorter than ten minutes" filter as a real measurement; ``None``
    is rejected explicitly as ``duration_unknown``.
    """
    if not value:
        return None
    match = _ISO_DURATION.match(value)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return parts["days"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _published(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class YoutubeProvider(Protocol):
    name: str
    enabled: bool

    def search_units(self) -> int: ...

    def discover(self, query: str, *, max_results: int) -> list[VideoCandidate]: ...


class DisabledYoutubeProvider:
    """The default.

    Not a stub that returns fake videos: it refuses, loudly, and the task that called it
    records a skip. A disabled feature must be visibly disabled, never quietly empty.
    """

    name = "disabled"
    enabled = False

    def search_units(self) -> int:
        return 0

    def discover(self, query: str, *, max_results: int) -> list[VideoCandidate]:
        raise ProviderError(
            "YouTube enrichment is disabled (YOUTUBE_ENABLED=false or no API key)."
        )


class YoutubeDataApiProvider:
    """The real client. Two calls per discovery, both counted against the day's budget."""

    name = "data_api"
    enabled = True

    def __init__(self, settings: Settings, transport: HttpTransport) -> None:
        self._settings = settings
        self._transport = transport
        self._key = settings.youtube_api_key.get_secret_value()

    def search_units(self) -> int:
        return SEARCH_UNITS + VIDEOS_UNITS

    def discover(self, query: str, *, max_results: int) -> list[VideoCandidate]:
        ids = self._search(query, max_results=max_results)
        if not ids:
            return []
        return self._videos(ids)

    # ------------------------------------------------------------------ calls

    def _search(self, query: str, *, max_results: int) -> list[str]:
        response = self._transport.get(
            f"{API_BASE}/search",
            params={
                "key": self._key,
                "q": query,
                "part": "id",
                "type": "video",
                # Barrier 1, and it is free: the API filters to >20 minutes before we are
                # charged for a single result, which removes Shorts and trailers wholesale.
                "videoDuration": "long",
                "order": "relevance",
                "maxResults": min(50, max_results),
                "relevanceLanguage": next(iter(self._settings.allowed_transcript_langs), "en"),
                "safeSearch": "none",
            },
            allow_status=frozenset({403}),
        )
        if response.status_code == 403:
            raise BudgetExhausted(f"YouTube refused the search: {response.text[:200]}")
        payload = response.json()
        return [
            item["id"]["videoId"]
            for item in payload.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

    def _videos(self, video_ids: list[str]) -> list[VideoCandidate]:
        response = self._transport.get(
            f"{API_BASE}/videos",
            params={
                "key": self._key,
                "id": ",".join(video_ids[:50]),
                "part": "snippet,contentDetails,statistics,status",
            },
            allow_status=frozenset({403}),
        )
        if response.status_code == 403:
            raise BudgetExhausted(f"YouTube refused the lookup: {response.text[:200]}")
        return [self._candidate(item) for item in response.json().get("items", [])]

    def _candidate(self, item: dict) -> VideoCandidate:
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        stats = item.get("statistics") or {}
        status = item.get("status") or {}
        thumbnails = snippet.get("thumbnails") or {}
        best = (
            thumbnails.get("maxres")
            or thumbnails.get("standard")
            or thumbnails.get("high")
            or thumbnails.get("medium")
            or {}
        )
        return VideoCandidate(
            video_id=item["id"],
            title=snippet.get("title", ""),
            channel_id=snippet.get("channelId"),
            channel_title=snippet.get("channelTitle"),
            description=snippet.get("description"),
            duration_s=parse_duration(details.get("duration")),
            view_count=_int(stats.get("viewCount")),
            like_count=_int(stats.get("likeCount")),
            published_at=_published(snippet.get("publishedAt")),
            # The API reports only whether captions exist, never their content, which is
            # why a transcript cascade is needed at all (ADR-016).
            has_captions=(details.get("caption") == "true")
            if "caption" in details
            else None,
            default_audio_language=snippet.get("defaultAudioLanguage")
            or snippet.get("defaultLanguage"),
            live_broadcast=snippet.get("liveBroadcastContent"),
            thumbnail_url=best.get("url"),
            embeddable=status.get("embeddable"),
        )


def build_youtube_provider(settings: Settings, transport: HttpTransport) -> YoutubeProvider:
    """Enabled only when both the flag and a key are present.

    A flag without a key would fail on every call and paint the monitoring page red for a
    feature the operator never configured.
    """
    if not settings.youtube_enabled or not settings.youtube_api_key.get_secret_value():
        return DisabledYoutubeProvider()
    return YoutubeDataApiProvider(settings, transport)


def search_query(title: str) -> str:
    """What we ask for, in the words that find a playthrough rather than a trailer."""
    return f"{title} lets play walkthrough gameplay"
