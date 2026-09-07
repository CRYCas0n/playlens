"""Domain enumerations.

Stored as plain strings with CHECK constraints rather than native database enums
(ADR-018): portable between PostgreSQL and SQLite, and adding a value is an ordinary
migration instead of a painful one.
"""

from __future__ import annotations

from enum import StrEnum


class ScoreStatus(StrEnum):
    VALID = "valid"
    UNAVAILABLE = "unavailable"  # data absent. NEVER rendered as 0.
    NOT_APPLICABLE = "n/a"


class Tier(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    MIXED = "mixed"
    POOR = "poor"
    NONE = "none"


class ReviewKind(StrEnum):
    CRITIC = "critic"
    USER = "user"


class Audience(StrEnum):
    CRITIC = "critic"
    USER = "user"
    LETSPLAY = "letsplay"


class SummaryStatus(StrEnum):
    FRESH = "fresh"
    SKIPPED_NO_DATA = "skipped_no_data"
    REJECTED = "rejected"
    FAILED = "failed"


class ClaimSide(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class ClaimType(StrEnum):
    DESCRIPTIVE = "descriptive"
    COMPARATIVE = "comparative"
    TEMPORAL = "temporal"


class ClaimValidation(StrEnum):
    ACCEPTED = "accepted"
    REJECTED_MISSING_REF = "rejected_missing_ref"
    REJECTED_LOW_SUPPORT = "rejected_low_support"
    REJECTED_VAGUE = "rejected_vague"
    REJECTED_TEMPORAL_UNSUPPORTED = "rejected_temporal_unsupported"
    REJECTED_QUOTE_NOT_FOUND = "rejected_quote_not_found"
    REJECTED_ASPECT_UNSUPPORTED = "rejected_aspect_unsupported"


class CrawlPhase(StrEnum):
    NEW_RELEASES = "new_releases"
    BROWSE = "browse"
    EXHAUSTED = "exhausted"


class CrawlItemStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"
    SEED = "seed"
    RECONCILE = "reconcile"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"      # can be retried by hand
    RETRYING = "retrying"
    DEAD = "dead"          # attempts exhausted; needs a human
    SKIPPED = "skipped"    # precondition not met -- NOT an error


class CrawlSource(StrEnum):
    NEW_RELEASES = "new_releases"
    BROWSE = "browse"
    SITEMAP = "sitemap"
    SEED = "seed"
    MANUAL = "manual"


class TranscriptStatus(StrEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class TranscriptSource(StrEnum):
    YTDLP = "ytdlp"
    HOSTED_API = "hosted_api"
    ASR = "asr"
    METADATA_ONLY = "metadata_only"


class SimilarityMethod(StrEnum):
    METADATA = "metadata"
    HYBRID = "hybrid"


class Aspect(StrEnum):
    """Fixed rubric. Three payoffs (BLUEPRINT 10.2): summaries are comparable between
    games, the verdict vector doubles as a similarity signal (ADR-011), and the UI can
    render stable badges."""

    GAMEPLAY = "gameplay"
    GRAPHICS = "graphics"
    STORY = "story"
    MECHANICS = "mechanics"
    PERFORMANCE = "performance"
    SOUND_MUSIC = "sound_music"
    INNOVATION = "innovation"
    REPLAYABILITY = "replayability"
    CONTENT_AMOUNT = "content_amount"
    DIFFICULTY = "difficulty"
    UI_UX = "ui_ux"
    PRICE_VALUE = "price_value"
    BUGS = "bugs"
    MULTIPLAYER = "multiplayer"
    OTHER = "other"


ASPECT_ORDER: tuple[Aspect, ...] = tuple(Aspect)


class AspectVerdict(StrEnum):
    POSITIVE = "positive"
    MIXED = "mixed"
    NEGATIVE = "negative"
    ABSENT = "absent"
