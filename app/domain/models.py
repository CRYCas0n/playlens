"""Domain DTOs.

These are what the parsers produce and the repositories consume. They are plain frozen
dataclasses on purpose: no ORM, no HTTP, no database — so the parsing and normalisation
layers stay testable against real fixtures without any infrastructure at all.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.domain.enums import ReviewKind


@dataclass(frozen=True, slots=True)
class ListingItem:
    """One row of a ``finder`` listing.

    The description is deliberately absent: the listing's descriptions do not match their
    games (verified during research — "Water Margin Heroes" carried a basketball game's
    text), so the only safe source of a description is the composer endpoint.
    """

    slug: str
    title: str
    mc_title_id: int | None = None
    release_date: dt.date | None = None
    premiere_year: int | None = None
    metascore: int | None = None
    metascore_count: int | None = None
    userscore: float | None = None
    cover_path: str | None = None
    rating: str | None = None


@dataclass(frozen=True, slots=True)
class Listing:
    items: tuple[ListingItem, ...]
    total_results: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class PlatformScores:
    slug: str
    name: str
    mc_platform_id: int | None
    is_lead: bool
    release_date: dt.date | None
    metascore: int | None
    metascore_count: int
    metascore_positive: int = 0
    metascore_neutral: int = 0
    metascore_negative: int = 0
    critic_sentiment: str | None = None
    mc_related_game_id: int | None = None

    # Filled in by a separate stats call: the composer endpoint carries per-platform
    # Metascore but no per-platform Userscore, and ``?platform=`` is ignored (C-04).
    userscore: float | None = None
    userscore_count: int = 0
    userscore_positive: int = 0
    userscore_neutral: int = 0
    userscore_negative: int = 0
    user_sentiment: str | None = None
    critic_reviews_with_text: int = 0
    user_reviews_with_text: int = 0


@dataclass(frozen=True, slots=True)
class CompanyRef:
    name: str
    slug: str
    role: str
    mc_company_id: int | None = None


@dataclass(frozen=True, slots=True)
class GenreRef:
    name: str
    slug: str
    mc_genre_id: int | None = None


@dataclass(frozen=True, slots=True)
class FranchiseRef:
    name: str
    slug: str
    mc_franchise_id: int | None = None


@dataclass(frozen=True, slots=True)
class GameDetail:
    """A fully normalised game, ready to be persisted."""

    slug: str
    title: str
    title_norm: str
    mc_url: str
    mc_title_id: int | None = None
    mc_family_id: int | None = None
    description: str | None = None
    cover_url: str | None = None
    card_url: str | None = None
    video_url: str | None = None
    video_title: str | None = None
    video_duration_s: int | None = None
    release_date: dt.date | None = None
    premiere_year: int | None = None
    esrb_rating: str | None = None
    must_play: bool = False
    genres: tuple[GenreRef, ...] = ()
    companies: tuple[CompanyRef, ...] = ()
    franchise: FranchiseRef | None = None
    platforms: tuple[PlatformScores, ...] = ()
    source_fingerprint: str = ""

    @property
    def developers(self) -> tuple[CompanyRef, ...]:
        return tuple(c for c in self.companies if c.role == "developer")

    @property
    def publishers(self) -> tuple[CompanyRef, ...]:
        return tuple(c for c in self.companies if c.role == "publisher")

    @property
    def lead_platform(self) -> PlatformScores | None:
        for platform in self.platforms:
            if platform.is_lead:
                return platform
        return self.platforms[0] if self.platforms else None


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """One normalised review.

    ``dedupe_key`` is the natural key: a UUID for user reviews, and for critics a hash of
    publication + date + body, because the source gives critic reviews no id at all and
    leaves ``url`` and ``author`` empty more often than not.
    """

    kind: ReviewKind
    dedupe_key: str
    body: str | None
    body_hash: str
    score: float | None
    score_max: int
    score_normalized: float | None
    published_on: dt.date | None
    source_review_id: str | None = None
    author: str | None = None
    publication_name: str | None = None
    publication_slug: str | None = None
    url: str | None = None
    is_spoiler: bool = False
    source_version: int | None = None
    thumbs_up: int | None = None
    thumbs_down: int | None = None
    char_count: int = 0
    language_hint: str | None = None
    sentiment_bucket: str | None = None  # a SAMPLING stratum, never ground truth (C-07)


@dataclass(frozen=True, slots=True)
class ReviewPage:
    items: tuple[ReviewRecord, ...]
    total_results: int
    offset: int


@dataclass(frozen=True, slots=True)
class ScoreStats:
    """Aggregate for one (game, platform, audience)."""

    score: float | None
    scale_max: int
    review_count: int
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    sentiment: str | None = None


@dataclass(frozen=True, slots=True)
class VideoCandidate:
    video_id: str
    title: str
    channel_id: str | None
    channel_title: str | None
    description: str | None
    duration_s: int | None
    view_count: int | None
    like_count: int | None
    published_at: dt.datetime | None
    has_captions: bool | None
    default_audio_language: str | None
    live_broadcast: str | None
    thumbnail_url: str | None
    embeddable: bool | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    source: str
    language: str | None = None
    is_auto_generated: bool | None = None
    segments: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def char_count(self) -> int:
        return len(self.text)
