"""The SQL score rule must agree with the Python one, forever.

``app.domain.scores.make_score`` and ``app.repositories.score_sql`` state the same rule
in two languages because ordering happens in the database. Two statements of one rule
drift silently; this test is what stops that. It is not a test of either implementation
in isolation — it is a test that they are the same rule.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.db.models import Game
from app.domain.enums import ScoreStatus
from app.domain.scores import make_score
from app.normalizers.text import normalize_title
from app.repositories.score_sql import effective_metascore, effective_userscore

pytestmark = pytest.mark.integration

ZERO_MIN = 20

#: Every shape a stored player score can take, including the ones Release 0 found in the
#: wild: 0 from 2 ratings (false), 0.5 from 6382 (genuine), NULL with a count of 0.
USER_CASES = [
    (None, 0),
    (None, 5),
    (0, 0),
    (0, 1),
    (0, 2),
    (0, 19),
    (0, 20),
    (0, 21),
    (0.5, 6382),
    (1.0, 3),
    (7.4, 1),
    (8.9, 14204),
    (10.0, 25),
]

METASCORE_CASES = [(None, 0), (None, 12), (0, 0), (0, 4), (55, 0), (55, 4), (93, 118)]


def _make(db, index: int, *, meta, meta_count, user, user_count) -> int:
    title = f"Case {index}"
    game = Game(
        mc_slug=f"case-{index}",
        mc_url=f"https://www.metacritic.com/game/case-{index}/",
        title=title,
        title_norm=normalize_title(title),
        best_metascore=meta,
        best_metascore_count=meta_count,
        best_userscore=user,
        best_userscore_count=user_count,
    )
    db.add(game)
    db.flush()
    return game.id


def test_userscore_rule_is_identical_in_python_and_sql(db):
    ids = {
        _make(db, i, meta=80, meta_count=10, user=raw, user_count=count): (raw, count)
        for i, (raw, count) in enumerate(USER_CASES)
    }
    db.flush()

    rows = db.execute(
        sa.select(Game.id, effective_userscore(ZERO_MIN))
    ).all()

    assert len(rows) == len(USER_CASES)
    for game_id, sql_value in rows:
        raw, count = ids[game_id]
        expected = make_score(
            raw, scale_max=10, review_count=count, zero_min_ratings=ZERO_MIN
        )
        if expected.status is ScoreStatus.VALID:
            assert sql_value == pytest.approx(expected.value), (raw, count)
        else:
            assert sql_value is None, (raw, count)


def test_metascore_rule_is_identical_in_python_and_sql(db):
    ids = {
        _make(db, 100 + i, meta=raw, meta_count=count, user=None, user_count=0): (raw, count)
        for i, (raw, count) in enumerate(METASCORE_CASES)
    }
    db.flush()

    rows = db.execute(sa.select(Game.id, effective_metascore())).all()

    for game_id, sql_value in rows:
        raw, count = ids[game_id]
        expected = make_score(
            raw, scale_max=100, review_count=count, zero_min_ratings=ZERO_MIN
        )
        if expected.status is ScoreStatus.VALID:
            assert sql_value == expected.value, (raw, count)
        else:
            assert sql_value is None, (raw, count)


def test_the_release_zero_boundary_is_the_one_that_is_enforced(db):
    """0 from 2 ratings is absence; 0.5 from 6382 is a verdict (ADR-002)."""
    false_zero = _make(db, 900, meta=79, meta_count=40, user=0, user_count=2)
    genuine_low = _make(db, 901, meta=45, meta_count=40, user=0.5, user_count=6382)
    db.flush()

    values = dict(db.execute(sa.select(Game.id, effective_userscore(ZERO_MIN))).all())
    assert values[false_zero] is None
    assert values[genuine_low] == pytest.approx(0.5)
