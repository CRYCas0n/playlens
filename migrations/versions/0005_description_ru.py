"""The source's description, in Russian.

Additive and nullable. The English original stays: it is what was fetched, the page says
which of the two it is showing, and a game with no translation yet shows the source's own
words rather than nothing.

Not backfilled by the migration. Translating 179 descriptions is 179 model calls, which
belongs in the queue behind the daily cost ceiling, not in a schema change that has to
finish before the api can serve.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.add_column(sa.Column("description_ru", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.drop_column("description_ru")
