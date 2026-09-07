"""Score model — the single place in the product that decides what a number means.

Two rules live here and nowhere else (ADR-002):

1. ``null != 0``. Metacritic returns ``userScore: 0`` for games with one or two ratings
   and no text. All four such games in the Release 0 corpus have ``reviewCount <= 3``;
   the lowest genuine low score observed is 0.5 at 6 382 ratings. Rendering that 0 as a
   score tells the user something false about a game whose Metascore is 79.

2. One comparison axis. Metascore is 0-100, Userscore is published 0-10. The native
   value is what gets displayed; the normalised value is what gets compared, sorted and
   turned into a gap. The UI always carries a caption saying so.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import ScoreStatus, Tier

# Tier thresholds. These are the design package's thresholds (design/DESIGN_SYSTEM.md 2.3
# and prototype/assets/app.js -> tier()). They exist in exactly one place in the product.
TIER_EXCELLENT_MIN = 85
TIER_GOOD_MIN = 70
TIER_MIXED_MIN = 50

#: Shown to the reader, so Russian. The Tier *values* stay English -- they are stored in
#: the database, checked by a constraint and used as CSS class names.
TIER_LABELS: dict[Tier, str] = {
    Tier.EXCELLENT: "Отлично",
    Tier.GOOD: "Хорошо",
    Tier.MIXED: "Смешанно",
    Tier.POOR: "Плохо",
    Tier.NONE: "Без оценки",
}


def tier_of(normalized: int | float | None) -> Tier:
    """Tier for a value already on the 0-100 axis. ``None`` is not ``0``."""
    if normalized is None:
        return Tier.NONE
    if normalized >= TIER_EXCELLENT_MIN:
        return Tier.EXCELLENT
    if normalized >= TIER_GOOD_MIN:
        return Tier.GOOD
    if normalized >= TIER_MIXED_MIN:
        return Tier.MIXED
    return Tier.POOR


def normalize(raw: float | None, scale_max: int) -> int | None:
    """Project a raw score onto the shared 0-100 axis. ``8.9`` on a 0-10 scale -> ``89``."""
    if raw is None:
        return None
    if scale_max == 100:
        return round(raw)
    return round(raw * (100.0 / scale_max))


@dataclass(frozen=True, slots=True)
class ScoreValue:
    """A score plus everything needed to present it honestly.

    ``raw`` is always what the source said. The status is derived, so changing the
    threshold is a config change, not a data migration.
    """

    raw: float | None
    scale_max: int
    review_count: int
    status: ScoreStatus
    low_sample: bool

    @property
    def normalized(self) -> int | None:
        if self.status is not ScoreStatus.VALID:
            return None
        return normalize(self.raw, self.scale_max)

    @property
    def value(self) -> float | None:
        """Native value for display, or ``None`` when there is nothing to display."""
        return self.raw if self.status is ScoreStatus.VALID else None

    @property
    def tier(self) -> Tier:
        return tier_of(self.normalized)

    @property
    def tier_label(self) -> str:
        return TIER_LABELS[self.tier]

    @property
    def is_available(self) -> bool:
        return self.status is ScoreStatus.VALID

    def as_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "scale_max": self.scale_max,
            "normalized": self.normalized,
            "status": self.status.value,
            "tier": self.tier.value,
            "tier_label": self.tier_label,
            "review_count": self.review_count,
            "low_sample": self.low_sample,
        }


def make_score(
    raw: float | None,
    *,
    scale_max: int,
    review_count: int | None,
    zero_min_ratings: int = 20,
    low_sample_threshold: int = 20,
) -> ScoreValue:
    """Build a :class:`ScoreValue`, applying the availability rule.

    ``unavailable`` when:
      * there are no ratings at all, or
      * the source said ``None``, or
      * the source said exactly ``0`` with fewer than ``zero_min_ratings`` ratings.

    The last clause is the one that matters. It is deliberately conservative: hiding a
    genuine 0.0 backed by twenty people is a far smaller error than telling a user that a
    game with Metascore 79 has a player score of zero.
    """
    count = review_count or 0

    if raw is None or count <= 0 or (raw == 0 and count < zero_min_ratings):
        status = ScoreStatus.UNAVAILABLE
    else:
        status = ScoreStatus.VALID

    return ScoreValue(
        raw=raw,
        scale_max=scale_max,
        review_count=count,
        status=status,
        low_sample=status is ScoreStatus.VALID and count < low_sample_threshold,
    )


def critic_score(
    raw: float | None, review_count: int | None, *, low_sample_threshold: int = 20
) -> ScoreValue:
    return make_score(
        raw,
        scale_max=100,
        review_count=review_count,
        # A critic score of 0 is not a thing Metacritic produces the way userScore 0 is;
        # the guard is harmless and keeps one rule for both audiences.
        zero_min_ratings=1,
        low_sample_threshold=low_sample_threshold,
    )


def user_score(
    raw: float | None,
    review_count: int | None,
    *,
    zero_min_ratings: int = 20,
    low_sample_threshold: int = 20,
) -> ScoreValue:
    return make_score(
        raw,
        scale_max=10,
        review_count=review_count,
        zero_min_ratings=zero_min_ratings,
        low_sample_threshold=low_sample_threshold,
    )


NO_SCORE_100 = ScoreValue(None, 100, 0, ScoreStatus.UNAVAILABLE, False)
NO_SCORE_10 = ScoreValue(None, 10, 0, ScoreStatus.UNAVAILABLE, False)


def sort_key(score: ScoreValue | None, *, descending: bool) -> tuple[int, float]:
    """Sort key that keeps unavailable scores at the end in BOTH directions.

    Without this, sorting ascending by player score puts every game that has no player
    score at the top of the list -- which is exactly the failure Release 0 warned about.
    """
    if score is None or not score.is_available:
        return (1, 0.0)
    value = float(score.normalized or 0)
    return (0, -value if descending else value)
