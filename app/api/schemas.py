"""Response schemas.

These are the contract for BOTH the JSON API and the HTML templates (ADR-017). Rendering
the page from the same objects the API returns is what makes it impossible for the two to
drift apart, and it means an edge case fixed in one is fixed in the other.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class ScoreOut(BaseModel):
    """A score, never a bare number.

    ``value`` is the native figure a reader recognises (``8.9``); ``normalized`` is the
    0-100 axis everything is compared on; ``status`` is what stops a missing score being
    rendered as zero (ADR-002).
    """

    model_config = ConfigDict(frozen=True)

    value: float | None
    scale_max: int
    normalized: int | None
    status: str
    tier: str
    tier_label: str
    review_count: int
    low_sample: bool


class PlatformOut(BaseModel):
    slug: str
    name: str
    code: str


class GenreFacetOut(BaseModel):
    """A genre with the number of games carrying it. Distinct from ``GenreOut``, which
    labels a game and has no count to report."""

    slug: str
    name: str
    count: int


class PlatformScoreOut(BaseModel):
    platform: PlatformOut
    is_lead: bool
    release_date: dt.date | None = None
    metascore: ScoreOut
    userscore: ScoreOut
    critic_reviews_with_text: int = 0
    user_reviews_with_text: int = 0


class GenreOut(BaseModel):
    slug: str
    name: str


class CompanyOut(BaseModel):
    slug: str
    name: str
    role: str


class VerdictOut(BaseModel):
    line: str
    kind: str
    delta: int | None = None
    platform_line: str | None = None
    strengths: list[str] = Field(default_factory=list)
    watch_outs: list[str] = Field(default_factory=list)
    #: Always true: the sentence is arithmetic, so it carries no AI attribution (ADR-012).
    derived: bool = True


class ConsensusOut(BaseModel):
    note: str
    tone: str
    delta: int | None
    axis_caption: str


class ClaimOut(BaseModel):
    aspect: str
    #: The English claim -- the wording that was checked against the cited reviews.
    claim: str
    #: Its Russian rendering, absent on summaries written before the site was Russian.
    claim_ru: str | None = None
    claim_type: str
    evidence: list[str]
    strength: str | None = None

    @property
    def text(self) -> str:
        """What to show a reader: Russian when there is one, the verified English if not.

        A missing translation costs the reader a language, never the finding itself.
        """
        return self.claim_ru or self.claim


class ProvenanceOut(BaseModel):
    """What the UI prints under a summary instead of an "AI-powered" badge."""

    kind: str = "ai_summary"
    reviews_used: int
    reviews_candidates: int
    snapshot_id: int | None
    model: str | None
    prompt_version: str | None
    generated_at: dt.datetime | None
    source_url: str | None = None


class SummaryOut(BaseModel):
    audience: str
    platform: PlatformOut | None
    status: str
    heading: str | None
    overall: str | None
    #: May be EMPTY. An empty negative list is a valid, informative result (ADR-008).
    positive: list[ClaimOut] = Field(default_factory=list)
    negative: list[ClaimOut] = Field(default_factory=list)
    empty_positive_note: str | None = None
    empty_negative_note: str | None = None
    aspect_verdicts: dict[str, str] = Field(default_factory=dict)
    provenance: ProvenanceOut | None = None


class GapOut(BaseModel):
    gap_points: int
    explanation: str
    evidence: list[str]
    model: str | None = None
    generated_at: dt.datetime | None = None


class SimilarGameOut(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    metascore: ScoreOut
    #: ``None`` when no component was strong enough to name. Never invented.
    reason: str | None
    score: float
    components: dict[str, float | None]


class GameListItemOut(BaseModel):
    slug: str
    title: str
    developer: str | None
    cover_url: str | None
    release_year: int | None
    metascore: ScoreOut
    userscore: ScoreOut
    platforms: list[PlatformOut]
    gap: int | None = None
    updated_at: dt.datetime | None = None


class VideoOut(BaseModel):
    url: str
    title: str | None
    duration_s: int | None


class LetsPlayOut(BaseModel):
    video_id: str
    url: str
    title: str
    channel: str | None
    views: int | None
    published_at: dt.datetime | None
    duration_s: int | None
    thumbnail_url: str | None
    transcript_status: str
    transcript_source: str | None
    summary: SummaryOut | None = None


class GameDetailOut(BaseModel):
    slug: str
    title: str
    #: The source's own description, in the source's language.
    description: str | None
    #: Its Russian rendering, absent until `game.translate` has run for this game.
    description_ru: str | None = None
    cover_url: str | None
    source_url: str
    developers: list[CompanyOut]
    publishers: list[CompanyOut]
    genres: list[GenreOut]
    esrb_rating: str | None
    release_date: dt.date | None
    release_year: int | None
    updated_at: dt.datetime | None

    metascore: ScoreOut
    userscore: ScoreOut
    verdict: VerdictOut
    consensus: ConsensusOut
    platforms: list[PlatformScoreOut]
    platform_scores_pending: bool

    critic_summary: SummaryOut | None
    user_summary: SummaryOut | None
    gap: GapOut | None
    similar: list[SimilarGameOut]
    lets_play: LetsPlayOut | None
    video: VideoOut | None


class FacetOut(BaseModel):
    slug: str
    name: str
    code: str
    count: int


class GameListOut(BaseModel):
    items: list[GameListItemOut]
    total: int
    limit: int
    offset: int
    has_more: bool
    facets: list[FacetOut] = Field(default_factory=list)


class SuggestionOut(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    release_year: int | None
    metascore: ScoreOut


class StatsOut(BaseModel):
    games_total: int
    games_with_metascore: int
    games_with_summaries: int
    reviews_total: int
    last_crawl_at: dt.datetime | None


class ProblemOut(BaseModel):
    """RFC 7807."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
