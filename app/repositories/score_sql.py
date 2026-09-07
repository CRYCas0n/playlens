"""The score-validity rule, expressed in SQL.

``app.domain.scores.make_score`` decides whether a stored number is a score a user may
be shown. Sorting, filtering and the gap calculation happen in the database, where that
function cannot run — so the same rule is written once more, here, as a SQL expression.

Two statements of one rule is a drift risk, and the risk is answered rather than
accepted: ``tests/integration/test_score_sql.py`` runs both over the same matrix of
``(raw, count)`` pairs and fails if they ever disagree.

The rule that makes this necessary is ADR-002: a player score of 0 backed by two ratings
is not a score of zero, it is the absence of a score. Ordering by the raw column would
put such a game at the extreme of every ranking and compute a 79-point "disagreement"
out of nothing — precisely the fake score the brief forbids.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from app.db.models import Game

#: Mirrors ``Settings.userscore_zero_min_ratings``. Repeated rather than imported so this
#: module stays a pure expression builder; every caller that has a Settings passes it in.
DEFAULT_ZERO_MIN_RATINGS = 20


def effective_metascore(
    zero_min_ratings: int = DEFAULT_ZERO_MIN_RATINGS,
    column: Any = Game.best_metascore,
    count: Any = None,
) -> Any:
    """NULL unless a critic score is real.

    The false-zero rule applies on this side too. Metacritic does not publish a
    Metascore below four reviews, and the lowest ever recorded is in the single digits,
    so a stored 0 backed by a handful of counts is a parse artefact, not a verdict.
    """
    count = Game.best_metascore_count if count is None else count
    return sa.case(
        (count <= 0, sa.null()),
        (sa.and_(column == 0, count < zero_min_ratings), sa.null()),
        else_=column,
    )


def effective_userscore(
    zero_min_ratings: int = DEFAULT_ZERO_MIN_RATINGS,
    column: Any = Game.best_userscore,
    count: Any = None,
) -> Any:
    """NULL unless a player score is real, including the false-zero rule (ADR-002)."""
    count = Game.best_userscore_count if count is None else count
    return sa.case(
        (count <= 0, sa.null()),
        (sa.and_(column == 0, count < zero_min_ratings), sa.null()),
        else_=column,
    )


def gap_expr(zero_min_ratings: int = DEFAULT_ZERO_MIN_RATINGS) -> Any:
    """Critic-player disagreement in Metascore points, NULL when either side is absent.

    Both sides go through the validity rule first, so a game can only have a gap when
    both audiences actually scored it.
    """
    return sa.func.abs(
        effective_metascore(zero_min_ratings)
        - effective_userscore(zero_min_ratings) * 10
    )
