"""Is the database actually usable, or merely reachable?

``SELECT 1`` succeeds against a database with no tables in it, which is exactly the state
a first run produces: SQLite creates the file on connect, so the application starts
happily and then fails on every single request.

The failure it produced was worse than useless. It came out as a generic 500 saying "we
have logged it and it usually resolves within a minute or two" — which is false. It never
resolves. The person is one command away from a working service and the service is
telling them to wait.

So "reachable" and "ready" are separated here, and "not ready" carries the command.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError, ProgrammingError

#: One table is enough to tell an empty database from a migrated one, and ``games`` is
#: the one nothing works without.
SENTINEL_TABLE = "games"

MIGRATE_COMMAND = "python -m alembic upgrade head"

NOT_MIGRATED_MESSAGE = (
    "The database has no tables yet. Create them with:  " + MIGRATE_COMMAND
)

#: What a missing table looks like on each dialect we support.
_MISSING_TABLE_PATTERNS = (
    re.compile(r"no such table", re.I),           # SQLite
    re.compile(r"relation .* does not exist", re.I),  # PostgreSQL
    re.compile(r"undefined_?table", re.I),        # PostgreSQL, by error code name
)


class DatabaseNotMigrated(RuntimeError):
    """The database is reachable but its schema was never created."""

    def __init__(self, detail: str = NOT_MIGRATED_MESSAGE) -> None:
        super().__init__(detail)


def is_missing_table_error(exc: BaseException) -> bool:
    """Does this exception mean "the schema is not there" rather than "the query is wrong"?

    Matched on the message rather than the exception class, because SQLAlchemy wraps the
    driver's error and the classes differ per dialect. A false positive here would tell
    someone to run migrations they have already run, which is harmless; a false negative
    puts them back in front of an unexplained 500.
    """
    if isinstance(exc, DatabaseNotMigrated):
        return True
    if not isinstance(exc, (OperationalError, ProgrammingError, DatabaseError)):
        return False
    text = str(exc)
    return any(pattern.search(text) for pattern in _MISSING_TABLE_PATTERNS)


def schema_exists(engine: Engine) -> bool:
    """True when the schema has been created. Never raises."""
    try:
        return sa.inspect(engine).has_table(SENTINEL_TABLE)
    except Exception:
        return False


def check(engine: Engine) -> None:
    """Raise :class:`DatabaseNotMigrated` if the schema is missing."""
    if not schema_exists(engine):
        raise DatabaseNotMigrated
