"""Repository helpers.

The repositories are the only layer that knows about tables. Everything dialect-specific
in the project lives in this file plus ``games.search_titles`` — two places, both small,
both covered by tests on SQLite and by the same tests on PostgreSQL when
``TEST_DATABASE_URL`` is set (ADR-018).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session


def dialect_name(session: Session) -> str:
    return session.get_bind().dialect.name


def upsert_stmt(session: Session, table: Any):
    """Dialect-appropriate INSERT that supports ``on_conflict_*``."""
    return sqlite_insert(table) if dialect_name(session) == "sqlite" else pg_insert(table)


def insert_ignore_returning(
    session: Session,
    table: Any,
    values: dict[str, Any],
    *,
    index_elements: Sequence[str],
    returning: Sequence[Any],
) -> Any | None:
    """``INSERT ... ON CONFLICT DO NOTHING RETURNING`` — the atomic claim.

    Returns the row when we inserted it, ``None`` when someone else already had it.
    This is the whole of the daily deduplication guarantee (ADR-006): no lock, no
    read-then-write race, works unchanged with any number of workers.
    """
    stmt = (
        upsert_stmt(session, table)
        .values(**values)
        .on_conflict_do_nothing(index_elements=list(index_elements))
        .returning(*returning)
    )
    return session.execute(stmt).first()


def insert_many_ignore(
    session: Session,
    table: Any,
    rows: list[dict[str, Any]],
    *,
    index_elements: Sequence[str],
    returning: Sequence[Any],
) -> list[Any]:
    """Batch insert that skips conflicts and reports what was actually inserted.

    Used together with a follow-up UPDATE to tell insertions from updates without
    PostgreSQL's ``xmax`` trick, which SQLite has no equivalent for.
    """
    if not rows:
        return []
    stmt = (
        upsert_stmt(session, table)
        .values(rows)
        .on_conflict_do_nothing(index_elements=list(index_elements))
        .returning(*returning)
    )
    return list(session.execute(stmt).all())


def upsert_returning_id(
    session: Session,
    table: Any,
    values: dict[str, Any],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str],
    id_column: Any,
) -> int:
    """``INSERT ... ON CONFLICT DO UPDATE ... RETURNING id`` for reference tables."""
    stmt = upsert_stmt(session, table).values(**values)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=list(index_elements),
        set_={c: getattr(excluded, c) for c in update_columns},
    ).returning(id_column)
    row = session.execute(stmt).first()
    assert row is not None
    return int(row[0])


def claim_one(
    session: Session,
    table: Any,
    *,
    where: Any,
    order_by: Sequence[Any],
    values: dict[str, Any],
    returning: Sequence[Any],
) -> Any | None:
    """Claim exactly one row for this worker.

    PostgreSQL uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers step over each
    other's rows instead of blocking. SQLite has no such clause and does not need one:
    it serialises writers, so the plain ``UPDATE ... WHERE id = (SELECT ... LIMIT 1)``
    is equally correct there.
    """
    pk = table.c.id if hasattr(table, "c") else table.id
    subq = sa.select(pk).where(where).order_by(*order_by).limit(1)
    if dialect_name(session) != "sqlite":
        subq = subq.with_for_update(skip_locked=True)

    stmt = (
        sa.update(table)
        .where(pk == subq.scalar_subquery())
        .values(**values)
        .returning(*returning)
        .execution_options(synchronize_session=False)
    )
    return session.execute(stmt).first()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def nulls_last(column: Any, *, descending: bool) -> Any:
    """Ordering that keeps missing scores at the end in BOTH directions.

    Sorting ascending by player score must not float every unrated game to the top —
    the failure Release 0 called out explicitly (section 5.5).
    """
    ordered = sa.desc(column) if descending else sa.asc(column)
    return sa.nullslast(ordered)
