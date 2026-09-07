"""Pydantic models of Metacritic's responses.

Two properties are deliberate:

* ``extra="ignore"`` — the source ADDING a field must never break us. It happens.
* Required fields are declared required — the source REMOVING one must break us loudly
  with :class:`SchemaDriftError` rather than quietly producing a game with no title.

Everything else is optional with a default, because the research showed ``video``,
``rating``, ``description`` and ``criticScoreSummary.score`` are all regularly ``null``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.errors import SchemaDriftError


class McBase(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class McImage(McBase):
    bucket_path: str | None = Field(None, alias="bucketPath")
    type_name: str | None = Field(None, alias="typeName")
    width: int | None = None
    height: int | None = None


class McScoreSummary(McBase):
    score: float | None = None
    max: int | None = None
    review_count: int | None = Field(None, alias="reviewCount")
    positive_count: int | None = Field(None, alias="positiveCount")
    neutral_count: int | None = Field(None, alias="neutralCount")
    negative_count: int | None = Field(None, alias="negativeCount")
    sentiment: str | None = None
    url: str | None = None


class McUserScore(McBase):
    score: float | None = None


class McNamed(McBase):
    id: int | None = None
    name: str


class McCompany(McBase):
    id: int | None = None
    type_name: str | None = Field(None, alias="typeName")
    name: str


class McProduction(McBase):
    companies: list[McCompany] = []


class McVideo(McBase):
    title: str | None = None
    video_title: str | None = Field(None, alias="videoTitle")
    duration: int | None = None
    embed_url: str | None = Field(None, alias="embedUrl")
    manifest_url: str | None = Field(None, alias="manifestUrl")


class McPlatform(McBase):
    id: int | None = None
    name: str
    slug: str | None = None
    critic_score_summary: McScoreSummary | None = Field(None, alias="criticScoreSummary")
    related_game_id: int | None = Field(None, alias="relatedGameId")
    is_lead_platform: bool = Field(False, alias="isLeadPlatform")
    release_date: dt.date | None = Field(None, alias="releaseDate")


class McTaxonomy(McBase):
    franchises: list[McNamed] = []
    family: McNamed | None = None
    title: McNamed | None = None
    game: McNamed | None = None
    platform: McNamed | None = None


class McProduct(McBase):
    """The composer product block. ``id``, ``slug`` and ``title`` are load-bearing."""

    id: int
    slug: str
    title: str
    description: str | None = None
    rating: str | None = None
    must_play: bool = Field(False, alias="mustPlay")
    premiere_year: int | None = Field(None, alias="premiereYear")
    release_date: dt.date | None = Field(None, alias="releaseDate")
    production: McProduction | None = None
    images: list[McImage] = []
    video: McVideo | None = None
    genres: list[McNamed] = []
    platforms: list[McPlatform] = []
    game_taxonomy: McTaxonomy | None = Field(None, alias="gameTaxonomy")
    critic_score_summary: McScoreSummary | None = Field(None, alias="criticScoreSummary")

    @field_validator("release_date", mode="before")
    @classmethod
    def _empty_date_is_none(cls, value: Any) -> Any:
        return value or None


class McListingItem(McBase):
    id: int | None = None
    title: str
    slug: str
    premiere_year: int | None = Field(None, alias="premiereYear")
    release_date: dt.date | None = Field(None, alias="releaseDate")
    rating: str | None = None
    image: McImage | None = None
    critic_score_summary: McScoreSummary | None = Field(None, alias="criticScoreSummary")
    user_score: McUserScore | None = Field(None, alias="userScore")

    @field_validator("release_date", mode="before")
    @classmethod
    def _empty_date_is_none(cls, value: Any) -> Any:
        return value or None


class McListingData(McBase):
    total_results: int = Field(0, alias="totalResults")
    items: list[McListingItem] = []


class McCriticReview(McBase):
    quote: str | None = None
    score: float | None = None
    url: str | None = None
    date: dt.date | None = None
    author: str | None = None
    publication_name: str | None = Field(None, alias="publicationName")
    publication_slug: str | None = Field(None, alias="publicationSlug")
    platform: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _empty_date_is_none(cls, value: Any) -> Any:
        return value or None


class McUserReview(McBase):
    id: str | None = None
    quote: str | None = None
    score: float | None = None
    thumbs_up: int | None = Field(None, alias="thumbsUp")
    thumbs_down: int | None = Field(None, alias="thumbsDown")
    date: dt.date | None = None
    author: str | None = None
    version: int | None = None
    spoiler: bool = False
    platform: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _empty_date_is_none(cls, value: Any) -> Any:
        return value or None


class McReviewData(McBase):
    total_results: int = Field(0, alias="totalResults")
    items: list[dict[str, Any]] = []


def validate(model: type[BaseModel], payload: Any, *, context: str) -> Any:
    """Validate, converting a Pydantic failure into a domain-level schema-drift error.

    The distinction matters operationally: schema drift is permanent for the item (retry
    will not help) but is an alert for the system, and it drives the automatic fallback
    to the HTML source once it crosses a threshold (ADR-001).
    """
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise SchemaDriftError(f"{context}: {exc.error_count()} field error(s): {exc}") from exc
