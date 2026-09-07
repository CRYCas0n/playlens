"""ORM models — the schema of FINAL_SPEC section 8.

Two conventions worth stating once:

* **Every guarantee that can be a constraint is a constraint** (ADR-006). Application
  logic can contain a bug; ``UNIQUE`` cannot. The partial unique indexes at the bottom of
  this module are load-bearing, not decorative.
* **Scores are stored raw.** ``metascore_raw`` / ``userscore_raw`` hold exactly what the
  source said. Whether a value is presentable is decided on read by
  ``app.domain.scores`` (ADR-002), so changing the rule is a config change rather than a
  data migration.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigInt, JSONColumn, TZDateTime, utcnow
from app.domain import enums


def _enum_check(column: str, enum_cls: type[enums.StrEnum], name: str) -> sa.CheckConstraint:
    values = ", ".join(f"'{m.value}'" for m in enum_cls)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)


def _ts(**kw: object) -> Mapped[dt.datetime]:
    return mapped_column(TZDateTime, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- reference


class Platform(Base):
    __tablename__ = "platforms"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    mc_platform_id: Mapped[int | None] = mapped_column(BigInt, unique=True)
    slug: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    #: Short monospace code used by the design system (design/COMPONENTS.md -> PlatformBadge).
    code: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    family: Mapped[str | None] = mapped_column(sa.String(32))
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)


class Genre(Base):
    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String(96), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    mc_company_id: Mapped[int | None] = mapped_column(BigInt, unique=True)
    slug: Mapped[str] = mapped_column(sa.String(160), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)


class Franchise(Base):
    __tablename__ = "franchises"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    mc_franchise_id: Mapped[int | None] = mapped_column(BigInt, unique=True)
    slug: Mapped[str] = mapped_column(sa.String(160), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)


# --------------------------------------------------------------------------- core


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (
        sa.CheckConstraint(
            "best_metascore IS NULL OR (best_metascore >= 0 AND best_metascore <= 100)",
            name="ck_games_best_metascore_range",
        ),
        sa.CheckConstraint(
            "best_userscore IS NULL OR (best_userscore >= 0 AND best_userscore <= 10)",
            name="ck_games_best_userscore_range",
        ),
        # Portable ordering indexes. SQLite accepts DESC in an index but not NULLS LAST,
        # so the NULLS LAST variants PostgreSQL needs to match
        # ``ORDER BY x DESC NULLS LAST`` are created additively by the migration
        # (ADR-018: PostgreSQL may receive extra objects, never different logic).
        sa.Index("ix_games_best_meta_desc", sa.text("best_metascore DESC"), sa.text("id DESC")),
        sa.Index("ix_games_best_user_desc", sa.text("best_userscore DESC"), sa.text("id DESC")),
        sa.Index("ix_games_release_desc", sa.text("release_date DESC"), sa.text("id DESC")),
        sa.Index("ix_games_updated_desc", sa.text("updated_at DESC"), sa.text("id DESC")),
        sa.Index("ix_games_title_norm", "title_norm"),
        sa.Index("ix_games_reviews_synced", "reviews_synced_at"),
        sa.Index("ix_games_similarity_synced", "similarity_synced_at"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)

    # identity: slug is the natural key, title id is the durable surrogate. A rename
    # changes the slug but not the id, so we look up by id first (ADR-006).
    mc_slug: Mapped[str] = mapped_column(sa.String(255), unique=True, nullable=False)
    mc_title_id: Mapped[int | None] = mapped_column(BigInt, unique=True)
    mc_family_id: Mapped[int | None] = mapped_column(BigInt, index=True)
    mc_url: Mapped[str] = mapped_column(sa.String(512), nullable=False)

    title: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    title_norm: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    #: The source's description in Russian. Nullable: the original is what was fetched,
    #: this is a translation of it, and a game keeps showing the source's own words until
    #: one exists. Written by `game.translate`, invalidated when `description` changes.
    description_ru: Mapped[str | None] = mapped_column(sa.Text)
    cover_url: Mapped[str | None] = mapped_column(sa.String(1024))
    card_url: Mapped[str | None] = mapped_column(sa.String(1024))
    # Not necessarily a trailer: the field is whatever video the source attached, often a
    # guide. Rendered as a link with its real title, never labelled "trailer" (C-27).
    video_url: Mapped[str | None] = mapped_column(sa.String(1024))
    video_title: Mapped[str | None] = mapped_column(sa.String(512))
    video_duration_s: Mapped[int | None] = mapped_column(sa.Integer)

    release_date: Mapped[dt.date | None] = mapped_column(sa.Date)
    premiere_year: Mapped[int | None] = mapped_column(sa.Integer)
    esrb_rating: Mapped[str | None] = mapped_column(sa.String(16))
    must_play: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    franchise_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("franchises.id", ondelete="SET NULL")
    )

    # rollups, refreshed inside the same transaction as game_platforms
    lead_platform_id: Mapped[int | None] = mapped_column(
        sa.Integer, sa.ForeignKey("platforms.id", ondelete="SET NULL")
    )
    lead_metascore: Mapped[int | None] = mapped_column(sa.Integer)
    lead_metascore_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    lead_userscore: Mapped[float | None] = mapped_column(sa.Float)
    lead_userscore_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    best_metascore: Mapped[int | None] = mapped_column(sa.Integer)
    best_metascore_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    best_userscore: Mapped[float | None] = mapped_column(sa.Float)
    best_userscore_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    platform_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    critic_reviews_total: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    user_reviews_total: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    source_fingerprint: Mapped[str | None] = mapped_column(sa.String(64))
    detail_synced_at: Mapped[dt.datetime | None] = _ts()
    reviews_synced_at: Mapped[dt.datetime | None] = _ts()
    summaries_synced_at: Mapped[dt.datetime | None] = _ts()
    youtube_searched_at: Mapped[dt.datetime | None] = _ts()
    similarity_synced_at: Mapped[dt.datetime | None] = _ts()
    first_seen_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)

    platforms: Mapped[list[GamePlatform]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    genres: Mapped[list[GameGenre]] = relationship(cascade="all, delete-orphan")
    companies: Mapped[list[GameCompany]] = relationship(cascade="all, delete-orphan")
    franchise: Mapped[Franchise | None] = relationship()


class GameGenre(Base):
    __tablename__ = "game_genres"
    __table_args__ = (sa.Index("ix_game_genres_genre", "genre_id", "game_id"),)

    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    genre_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True
    )
    genre: Mapped[Genre] = relationship()


class GameCompany(Base):
    __tablename__ = "game_companies"
    __table_args__ = (
        sa.CheckConstraint("role IN ('developer','publisher')", name="ck_game_companies_role"),
        sa.Index("ix_game_companies_company", "company_id", "role"),
    )

    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(sa.String(16), primary_key=True)
    company: Mapped[Company] = relationship()


class GamePlatform(Base):
    __tablename__ = "game_platforms"
    __table_args__ = (
        sa.UniqueConstraint("game_id", "platform_id", name="uq_game_platforms"),
        sa.CheckConstraint(
            "metascore_raw IS NULL OR (metascore_raw >= 0 AND metascore_raw <= 100)",
            name="ck_gp_metascore_range",
        ),
        sa.CheckConstraint(
            "userscore_raw IS NULL OR (userscore_raw >= 0 AND userscore_raw <= 10)",
            name="ck_gp_userscore_range",
        ),
        sa.Index("ix_gp_platform_game", "platform_id", "game_id"),
        sa.Index("ix_gp_game", "game_id"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    platform_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("platforms.id", ondelete="RESTRICT"), nullable=False
    )
    mc_related_game_id: Mapped[int | None] = mapped_column(BigInt)
    is_lead: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    release_date: Mapped[dt.date | None] = mapped_column(sa.Date)

    metascore_raw: Mapped[int | None] = mapped_column(sa.Integer)
    metascore_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    metascore_positive: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    metascore_neutral: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    metascore_negative: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    critic_sentiment: Mapped[str | None] = mapped_column(sa.String(64))

    userscore_raw: Mapped[float | None] = mapped_column(sa.Float)
    #: Total ratings, INCLUDING those without text. Kept apart from
    #: ``user_reviews_with_text`` because the source aggregates over the larger number and
    #: the UI must not conflate the two (PRODUCT_BLUEPRINT journey 8).
    userscore_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    userscore_positive: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    userscore_neutral: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    userscore_negative: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    user_sentiment: Mapped[str | None] = mapped_column(sa.String(64))

    critic_reviews_with_text: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    user_reviews_with_text: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    critic_reviews_synced_at: Mapped[dt.datetime | None] = _ts()
    user_reviews_synced_at: Mapped[dt.datetime | None] = _ts()

    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)

    game: Mapped[Game] = relationship(back_populates="platforms")
    platform: Mapped[Platform] = relationship()


# --------------------------------------------------------------------------- reviews


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        sa.UniqueConstraint("game_platform_id", "kind", "dedupe_key", name="uq_reviews_dedupe"),
        _enum_check("kind", enums.ReviewKind, "ck_reviews_kind"),
        sa.Index("ix_reviews_game_kind_date", "game_id", "kind", "published_on", "id"),
        sa.Index("ix_reviews_gp_kind", "game_platform_id", "kind"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    game_platform_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("game_platforms.id", ondelete="CASCADE"), nullable=False
    )
    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)

    #: UUID for user reviews; NULL for critic reviews, which the source does not identify.
    source_review_id: Mapped[str | None] = mapped_column(sa.String(64))
    dedupe_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)

    author: Mapped[str | None] = mapped_column(sa.String(255))
    publication_name: Mapped[str | None] = mapped_column(sa.String(255))
    publication_slug: Mapped[str | None] = mapped_column(sa.String(255))
    score: Mapped[float | None] = mapped_column(sa.Float)
    score_max: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    score_normalized: Mapped[float | None] = mapped_column(sa.Float)
    body: Mapped[str | None] = mapped_column(sa.Text)
    body_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    language_hint: Mapped[str | None] = mapped_column(sa.String(8))
    url: Mapped[str | None] = mapped_column(sa.String(1024))
    published_on: Mapped[dt.date | None] = mapped_column(sa.Date)
    is_spoiler: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    source_version: Mapped[int | None] = mapped_column(BigInt)
    thumbs_up: Mapped[int | None] = mapped_column(sa.Integer)
    thumbs_down: Mapped[int | None] = mapped_column(sa.Integer)

    first_seen_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)


class ReviewSnapshot(Base):
    """An immutable, versioned corpus (ADR-007).

    Release 0 rebuilt a corpus, the numbering shifted, and summary citations silently
    started pointing at different reviews. A snapshot makes that impossible: references
    are scoped to a hash of the exact set and order they were built from.
    """

    __tablename__ = "review_snapshots"
    __table_args__ = (
        sa.UniqueConstraint(
            "game_platform_id", "kind", "content_hash", name="uq_snapshot_content"
        ),
        _enum_check("kind", enums.ReviewKind, "ck_snapshot_kind"),
        sa.Index("ix_snapshot_gp_kind_built", "game_platform_id", "kind", "built_at"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    game_platform_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("game_platforms.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)

    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    ordering_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    sampling_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    review_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: {"too_short": 12, "score_text_mismatch": 8, "duplicate": 3, "off_topic": 2}
    rejected: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    date_min: Mapped[dt.date | None] = mapped_column(sa.Date)
    date_max: Mapped[dt.date | None] = mapped_column(sa.Date)
    built_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)

    entries: Mapped[list[SnapshotReview]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class SnapshotReview(Base):
    __tablename__ = "snapshot_reviews"
    __table_args__ = (
        sa.UniqueConstraint("snapshot_id", "review_id", name="uq_snapshot_review"),
        sa.Index("ix_snapshot_reviews_position", "snapshot_id", "position"),
    )

    snapshot_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("review_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    #: ``C07`` / ``U31``. Assigned once, never reassigned.
    evidence_ref: Mapped[str] = mapped_column(sa.String(8), primary_key=True)
    review_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    snapshot: Mapped[ReviewSnapshot] = relationship(back_populates="entries")
    review: Mapped[Review] = relationship()


# --------------------------------------------------------------------------- summaries


class Summary(Base):
    __tablename__ = "summaries"
    __table_args__ = (
        sa.UniqueConstraint(
            "game_platform_id", "audience", "input_fingerprint", name="uq_summary_fingerprint"
        ),
        _enum_check("audience", enums.Audience, "ck_summary_audience"),
        _enum_check("status", enums.SummaryStatus, "ck_summary_status"),
        sa.Index("ix_summaries_history", "game_platform_id", "audience", "version"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    game_platform_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("game_platforms.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("review_snapshots.id", ondelete="SET NULL")
    )
    audience: Mapped[str] = mapped_column(sa.String(16), nullable=False)

    heading: Mapped[str | None] = mapped_column(sa.String(255))
    overall: Mapped[str | None] = mapped_column(sa.Text)
    aspect_verdicts: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)

    #: sha256(snapshot.content_hash + prompt_version + model + params_version).
    #: The unique constraint above makes a repeat call with identical input impossible.
    input_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    reviews_used: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    reviews_candidates: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: The audience's score on the 0-100 axis when this summary was written. Stored so a
    #: later run can ask "has the verdict moved since?" -- a handful of new reviews below
    #: the regeneration threshold can still shift a score several points, and that is the
    #: case worth catching (AI_SCORE_MOVE_POINTS).
    score_at_generation: Mapped[int | None] = mapped_column(sa.Integer)

    llm_provider: Mapped[str | None] = mapped_column(sa.String(32))
    llm_model: Mapped[str | None] = mapped_column(sa.String(64))
    prompt_version: Mapped[str | None] = mapped_column(sa.String(32))
    params_version: Mapped[str | None] = mapped_column(sa.String(32))
    tokens_in: Mapped[int | None] = mapped_column(sa.Integer)
    tokens_out: Mapped[int | None] = mapped_column(sa.Integer)
    cost_usd: Mapped[float | None] = mapped_column(sa.Float)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)

    error_message: Mapped[str | None] = mapped_column(sa.Text)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    generated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)

    claims: Mapped[list[SummaryClaim]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )
    snapshot: Mapped[ReviewSnapshot | None] = relationship()


class SummaryClaim(Base):
    __tablename__ = "summary_claims"
    __table_args__ = (
        _enum_check("side", enums.ClaimSide, "ck_claim_side"),
        _enum_check("claim_type", enums.ClaimType, "ck_claim_type"),
        _enum_check("validation", enums.ClaimValidation, "ck_claim_validation"),
        _enum_check("aspect", enums.Aspect, "ck_claim_aspect"),
        sa.Index("ix_claims_summary", "summary_id", "side", "position"),
        sa.Index("ix_claims_validation", "validation"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    summary_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("summaries.id", ondelete="CASCADE"), nullable=False
    )
    side: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    aspect: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    #: The English claim, which is what the validator checked. Kept as the record of what
    #: was actually verified against the reviews.
    claim: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Its Russian rendering, which is what a reader sees. Nullable: summaries written
    #: before the site was Russian have none, and they still display -- in English.
    claim_ru: Mapped[str | None] = mapped_column(sa.Text)
    claim_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONColumn, nullable=False, default=list)
    strength: Mapped[str | None] = mapped_column(sa.String(16))
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: Rejected claims are KEPT. Without them the quality of the pipeline is unmeasurable
    #: and a prompt regression is invisible until a user notices it.
    validation: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    validation_detail: Mapped[str | None] = mapped_column(sa.Text)

    summary: Mapped[Summary] = relationship(back_populates="claims")


class GapExplanation(Base):
    __tablename__ = "gap_explanations"
    __table_args__ = (
        sa.UniqueConstraint(
            "game_platform_id",
            "critic_snapshot_id",
            "user_snapshot_id",
            name="uq_gap_snapshots",
        ),
        _enum_check("status", enums.SummaryStatus, "ck_gap_status"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    game_platform_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("game_platforms.id", ondelete="CASCADE"), nullable=False
    )
    critic_snapshot_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("review_snapshots.id", ondelete="SET NULL")
    )
    user_snapshot_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("review_snapshots.id", ondelete="SET NULL")
    )
    gap_points: Mapped[int | None] = mapped_column(sa.Integer)
    explanation: Mapped[str | None] = mapped_column(sa.Text)
    evidence_refs: Mapped[list] = mapped_column(JSONColumn, nullable=False, default=list)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    is_current: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    llm_model: Mapped[str | None] = mapped_column(sa.String(64))
    prompt_version: Mapped[str | None] = mapped_column(sa.String(32))
    generated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)


# --------------------------------------------------------------------------- similarity


class SimilarGame(Base):
    __tablename__ = "similar_games"
    __table_args__ = (
        sa.CheckConstraint("game_id <> similar_game_id", name="ck_similar_not_self"),
        _enum_check("method", enums.SimilarityMethod, "ck_similar_method"),
        sa.Index("ix_similar_lookup", "game_id", "rank"),
        sa.Index("ix_similar_reverse", "similar_game_id"),
    )

    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    similar_game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    method: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    components: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: NULL when no component was strong enough to name a reason. The UI renders nothing
    #: in that case and NEVER invents one (design/README.md non-negotiable 8).
    reason: Mapped[str | None] = mapped_column(sa.String(255))
    computed_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)


# --------------------------------------------------------------------------- youtube


class YoutubeVideo(Base):
    __tablename__ = "youtube_videos"
    __table_args__ = (
        sa.UniqueConstraint("game_id", "video_id", name="uq_youtube_video"),
        sa.Index("ix_youtube_rank", "game_id", "rank_score"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    video_id: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    url: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(sa.String(64))
    channel_title: Mapped[str | None] = mapped_column(sa.String(255))
    description: Mapped[str | None] = mapped_column(sa.Text)
    duration_s: Mapped[int | None] = mapped_column(sa.Integer)
    view_count: Mapped[int | None] = mapped_column(BigInt)
    like_count: Mapped[int | None] = mapped_column(BigInt)
    published_at: Mapped[dt.datetime | None] = _ts()
    has_captions: Mapped[bool | None] = mapped_column(sa.Boolean)
    default_audio_language: Mapped[str | None] = mapped_column(sa.String(16))
    live_broadcast: Mapped[str | None] = mapped_column(sa.String(16))
    thumbnail_url: Mapped[str | None] = mapped_column(sa.String(1024))

    rank_score: Mapped[float | None] = mapped_column(sa.Float)
    rank_components: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    #: Rejected candidates are stored too: without them the ranking cannot be debugged.
    rejected_reason: Mapped[str | None] = mapped_column(sa.String(64))
    is_selected: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    discovery_source: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="data_api")

    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)


class YoutubeTranscript(Base):
    __tablename__ = "youtube_transcripts"
    __table_args__ = (
        _enum_check("status", enums.TranscriptStatus, "ck_transcript_status"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        BigInt, sa.ForeignKey("youtube_videos.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    source: Mapped[str | None] = mapped_column(sa.String(24))
    language: Mapped[str | None] = mapped_column(sa.String(16))
    is_auto_generated: Mapped[bool | None] = mapped_column(sa.Boolean)
    text: Mapped[str | None] = mapped_column(sa.Text)
    char_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: [{"provider": "ytdlp", "ok": false, "error": "empty_or_too_short", "ts": "..."}]
    #: Mandatory for debugging: an empty HTTP 200 must be visible as a provider failure.
    attempts: Mapped[list] = mapped_column(JSONColumn, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    fetched_at: Mapped[dt.datetime | None] = _ts()
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- operations


class CrawlDay(Base):
    __tablename__ = "crawl_days"
    __table_args__ = (_enum_check("phase", enums.CrawlPhase, "ck_crawl_day_phase"),)

    crawl_date: Mapped[dt.date] = mapped_column(sa.Date, primary_key=True)
    phase: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="new_releases")
    new_releases_done: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    new_releases_seen: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    browse_page: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    browse_offset: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    browse_pages_done: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    browse_exhausted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    release_date_watermark: Mapped[dt.date | None] = mapped_column(sa.Date)
    games_claimed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        _enum_check("status", enums.RunStatus, "ck_run_status"),
        _enum_check("trigger", enums.RunTrigger, "ck_run_trigger"),
        sa.Index("ix_runs_started", "started_at"),
        sa.Index("ix_runs_by_date", "crawl_date", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    crawl_date: Mapped[dt.date] = mapped_column(
        sa.Date, sa.ForeignKey("crawl_days.crawl_date"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    triggered_by: Mapped[str | None] = mapped_column(sa.String(128))
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="running")
    phase_at_start: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(sa.String(128))
    #: Makes the eligibility rule visible in monitoring instead of hidden in code (ADR-015).
    source_params: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)

    pages_fetched: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    games_discovered: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    games_claimed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    games_skipped_dupe: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    games_succeeded: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    games_failed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    jobs_enqueued: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    started_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = _ts()
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    error_class: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)


class CrawlItem(Base):
    """The heart of deduplication.

    ``UNIQUE(crawl_date, game_slug)`` plus ``INSERT ... ON CONFLICT DO NOTHING RETURNING``
    is an atomic claim of a game for a day. It needs no lock and works unchanged with any
    number of workers (ADR-006).
    """

    __tablename__ = "crawl_items"
    __table_args__ = (
        sa.UniqueConstraint("crawl_date", "game_slug", name="uq_crawl_items_daily"),
        _enum_check("status", enums.CrawlItemStatus, "ck_crawl_item_status"),
        sa.Index("ix_crawl_items_status", "crawl_date", "status"),
        sa.Index("ix_crawl_items_lease", "lease_expires_at"),
        sa.Index("ix_crawl_items_run", "crawl_run_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    crawl_date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    game_slug: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    game_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="SET NULL")
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    lease_expires_at: Mapped[dt.datetime | None] = _ts()
    changed: Mapped[bool | None] = mapped_column(sa.Boolean)
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    claimed_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = _ts()


class Job(Base):
    """Queue and durable job registry in one table (ADR-003).

    The blueprint already required this table as the monitoring source of truth while
    also running Celery; keeping both meant two places that could disagree about whether
    a job had finished. This is the one place.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency"),
        _enum_check("status", enums.JobStatus, "ck_job_status"),
        sa.Index("ix_jobs_claimable", "status", "queue", "priority", "queued_at"),
        sa.Index("ix_jobs_lease", "lease_expires_at"),
        sa.Index("ix_jobs_by_game", "game_id", "job_type", "finished_at"),
        sa.Index("ix_jobs_recent", "queued_at"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    job_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="queued")
    queue: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=5)

    game_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("games.id", ondelete="CASCADE")
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )
    parent_job_id: Mapped[int | None] = mapped_column(
        BigInt, sa.ForeignKey("jobs.id", ondelete="SET NULL")
    )

    payload: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONColumn)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=5)
    next_retry_at: Mapped[dt.datetime | None] = _ts()
    lease_expires_at: Mapped[dt.datetime | None] = _ts()
    worker_id: Mapped[str | None] = mapped_column(sa.String(128))
    error_class: Mapped[str | None] = mapped_column(sa.String(64))
    error_message: Mapped[str | None] = mapped_column(sa.Text)

    queued_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    started_at: Mapped[dt.datetime | None] = _ts()
    finished_at: Mapped[dt.datetime | None] = _ts()
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)


class JobEvent(Base):
    """Append-only event log. Also the SSE stream: ``id`` IS the ``Last-Event-ID``.

    Writing events here first (and outside the business transaction) means a rollback of
    business logic cannot erase the record that it failed.
    """

    __tablename__ = "job_events"
    __table_args__ = (
        sa.Index("ix_events_run", "crawl_run_id", "id"),
        sa.Index("ix_events_level", "level", "id"),
        sa.Index("ix_events_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInt, primary_key=True)
    ts: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    level: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="info")
    event: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    stage: Mapped[str | None] = mapped_column(sa.String(32))
    message: Mapped[str | None] = mapped_column(sa.Text)
    crawl_run_id: Mapped[int | None] = mapped_column(BigInt)
    job_id: Mapped[int | None] = mapped_column(BigInt)
    game_id: Mapped[int | None] = mapped_column(BigInt)
    worker_id: Mapped[str | None] = mapped_column(sa.String(128))
    data: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    queues: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    started_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    heartbeat_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)
    active_jobs: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    version: Mapped[str | None] = mapped_column(sa.String(32))


class ApiBudget(Base):
    __tablename__ = "api_budgets"

    provider: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    usage_date: Mapped[dt.date] = mapped_column(sa.Date, primary_key=True)
    units_used: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0)
    units_limit: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0)
    calls: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)


class RateLimit(Base):
    """Token bucket in the database.

    Process-local buckets would let N workers do N times the configured rate against the
    source, which is exactly the promise ADR-001 makes to Metacritic.
    """

    __tablename__ = "rate_limits"

    bucket_key: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    tokens: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0)
    updated_at: Mapped[dt.datetime] = _ts(nullable=False, default=utcnow)


# ---------------------------------------------------- partial unique indexes (ADR-006)
#
# Declared outside the classes so the intent is readable in one place. Every one of these
# is a guarantee the assignment asks for, expressed where it cannot be bypassed.

sa.Index(
    "uq_game_platforms_one_lead",
    GamePlatform.game_id,
    unique=True,
    sqlite_where=GamePlatform.is_lead,
    postgresql_where=GamePlatform.is_lead,
)

sa.Index(
    "uq_summaries_current",
    Summary.game_platform_id,
    Summary.audience,
    unique=True,
    sqlite_where=Summary.is_current,
    postgresql_where=Summary.is_current,
)

sa.Index(
    "uq_gap_current",
    GapExplanation.game_platform_id,
    unique=True,
    sqlite_where=GapExplanation.is_current,
    postgresql_where=GapExplanation.is_current,
)

sa.Index(
    "uq_youtube_one_selected",
    YoutubeVideo.game_id,
    unique=True,
    sqlite_where=YoutubeVideo.is_selected,
    postgresql_where=YoutubeVideo.is_selected,
)

#: At most one active crawl run in the whole system. This is the second of the three
#: independent barriers against a double run (ADR-004).
sa.Index(
    "uq_crawl_runs_single_active",
    CrawlRun.status,
    unique=True,
    sqlite_where=CrawlRun.status == enums.RunStatus.RUNNING.value,
    postgresql_where=CrawlRun.status == enums.RunStatus.RUNNING.value,
)


#: Consulted by the schema-conformance test so a refactor cannot quietly drop a guarantee.
EXPECTED_UNIQUE_CONSTRAINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("games", ("mc_slug",)),
    ("games", ("mc_title_id",)),
    ("game_platforms", ("game_id", "platform_id")),
    ("reviews", ("game_platform_id", "kind", "dedupe_key")),
    ("review_snapshots", ("game_platform_id", "kind", "content_hash")),
    ("snapshot_reviews", ("snapshot_id", "review_id")),
    ("summaries", ("game_platform_id", "audience", "input_fingerprint")),
    ("crawl_items", ("crawl_date", "game_slug")),
    ("jobs", ("idempotency_key",)),
    ("youtube_videos", ("game_id", "video_id")),
    ("platforms", ("slug",)),
    ("genres", ("slug",)),
    ("companies", ("slug",)),
)

EXPECTED_PARTIAL_UNIQUE_INDEXES: tuple[str, ...] = (
    "uq_game_platforms_one_lead",
    "uq_summaries_current",
    "uq_gap_current",
    "uq_youtube_one_selected",
    "uq_crawl_runs_single_active",
)
