"""Schema conformance.

The guarantees this project makes about deduplication and idempotency are constraints,
not code (ADR-006). A refactor that silently drops one would look green everywhere else,
so the constraint list itself is asserted.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.db.models import EXPECTED_PARTIAL_UNIQUE_INDEXES, EXPECTED_UNIQUE_CONSTRAINTS

pytestmark = pytest.mark.integration


def _unique_sets(inspector: sa.Inspector, table: str) -> set[frozenset[str]]:
    found: set[frozenset[str]] = set()
    for constraint in inspector.get_unique_constraints(table):
        found.add(frozenset(constraint["column_names"]))
    for index in inspector.get_indexes(table):
        if index.get("unique"):
            found.add(frozenset(c for c in index["column_names"] if c))
    # A single-column primary key is a uniqueness guarantee too.
    pk = inspector.get_pk_constraint(table)
    if pk and pk.get("constrained_columns"):
        found.add(frozenset(pk["constrained_columns"]))
    return found


@pytest.mark.parametrize(("table", "columns"), EXPECTED_UNIQUE_CONSTRAINTS)
def test_expected_unique_constraints_exist(engine: Engine, table: str, columns: tuple[str, ...]):
    inspector = sa.inspect(engine)
    assert frozenset(columns) in _unique_sets(inspector, table), (
        f"{table}{columns} lost its uniqueness guarantee"
    )


def test_partial_unique_indexes_exist(engine: Engine):
    inspector = sa.inspect(engine)
    names: set[str] = set()
    for table in inspector.get_table_names():
        names.update(i["name"] for i in inspector.get_indexes(table) if i.get("name"))
    for expected in EXPECTED_PARTIAL_UNIQUE_INDEXES:
        assert expected in names, f"missing partial unique index {expected}"


def test_all_tables_created(engine: Engine):
    tables = set(sa.inspect(engine).get_table_names())
    for required in (
        "games",
        "game_platforms",
        "reviews",
        "review_snapshots",
        "snapshot_reviews",
        "summaries",
        "summary_claims",
        "gap_explanations",
        "similar_games",
        "youtube_videos",
        "youtube_transcripts",
        "crawl_days",
        "crawl_runs",
        "crawl_items",
        "jobs",
        "job_events",
        "worker_heartbeats",
        "api_budgets",
        "rate_limits",
    ):
        assert required in tables


def test_foreign_keys_are_enforced(db):
    """SQLite ignores foreign keys unless the pragma is on; without it the tests lie."""
    from app.db.models import GamePlatform

    db.add(GamePlatform(game_id=999_999, platform_id=1))
    with pytest.raises(sa.exc.IntegrityError):
        db.flush()
    db.rollback()


def test_platform_seed_is_idempotent(db):
    from app.db.models import Platform
    from app.db.seed import seed_platforms

    before = db.execute(sa.select(sa.func.count()).select_from(Platform)).scalar_one()
    seed_platforms(db)
    seed_platforms(db)
    db.commit()
    after = db.execute(sa.select(sa.func.count()).select_from(Platform)).scalar_one()
    assert before == after


def test_platform_codes_match_the_design_badges(db):
    from app.db.models import Platform

    codes = dict(db.execute(sa.select(Platform.slug, Platform.code)).all())
    # design/COMPONENTS.md -> PlatformBadge
    assert codes["pc"] == "PC"
    assert codes["playstation-5"] == "PS5"
    assert codes["xbox-series-x"] == "XSX"
    assert codes["nintendo-switch"] == "NSW"
