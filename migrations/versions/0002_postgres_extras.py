"""PostgreSQL-only additive objects.

ADR-018: one migration chain serves both dialects; PostgreSQL may receive EXTRA objects
(never different logic). Two things live here:

* ``pg_trgm`` plus a GIN index, so title search uses similarity instead of a scan.
  SQLite falls back to prefix + substring matching in the same repository method.
* ``NULLS LAST`` ordering indexes. PostgreSQL defaults DESC to NULLS FIRST, so an index
  on ``x DESC`` does not serve ``ORDER BY x DESC NULLS LAST`` -- which is the ordering the
  catalogue actually uses, because a game without a score must never sort first.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NULLS_LAST_INDEXES = (
    ("ix_games_best_meta_nl", "games", "best_metascore DESC NULLS LAST, id DESC"),
    ("ix_games_best_user_nl", "games", "best_userscore DESC NULLS LAST, id DESC"),
    ("ix_games_release_nl", "games", "release_date DESC NULLS LAST, id DESC"),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_games_title_trgm "
        "ON games USING gin (title_norm gin_trgm_ops)"
    )
    for name, table, expr in _NULLS_LAST_INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({expr})")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, _table, _expr in _NULLS_LAST_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS ix_games_title_trgm")
