"""Engine, session factory and the portable column types.

Portability (ADR-018) is deliberate and narrow: PostgreSQL 16 is the production target,
SQLite is the development and test dialect. The schema therefore stays inside the
intersection of the two -- no native enums, no arrays, no vector extension. Everything
the correctness guarantees rest on (partial unique indexes, ``ON CONFLICT DO NOTHING
RETURNING``, ``UPDATE ... RETURNING``, ``NULLS LAST``) exists in both.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


class TZDateTime(sa.types.TypeDecorator):
    """Always store UTC, always return an aware datetime.

    SQLite has no timezone-aware type, so without this a round trip silently turns an
    aware timestamp into a naive one and every comparison against ``now()`` becomes a
    coin flip.
    """

    impl = sa.DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


#: SQLite only auto-increments a column declared exactly ``INTEGER PRIMARY KEY``; a
#: ``BIGINT`` primary key silently fails to generate ids. The variant keeps PostgreSQL on
#: bigint (the catalogue is ~18.5k games but reviews run to millions) while letting SQLite
#: use its rowid alias.
BigInt = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

#: JSON works on both dialects. On PostgreSQL SQLAlchemy maps it to ``json``; upgrading to
#: ``jsonb`` later is an additive migration, not a schema redesign.
JSONColumn = sa.JSON


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def make_engine(settings: Settings) -> Engine:
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}
    if settings.is_sqlite:
        # check_same_thread=False so the queue's worker threads can share the engine;
        # the pool still hands out one connection per thread.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_pre_ping"] = True

    engine = sa.create_engine(settings.database_url, **kwargs)

    if settings.is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            # Without this SQLite silently ignores foreign keys and the tests lie.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    else:
        statement_timeout = settings.db_statement_timeout_ms

        @event.listens_for(engine, "connect")
        def _pg_settings(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute(f"SET statement_timeout = {int(statement_timeout)}")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
