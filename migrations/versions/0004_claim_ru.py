"""The Russian rendering of each claim.

Additive and nullable, deliberately. Every summary written before the site was Russian
keeps its rows and keeps displaying — in English, which is the wording that was actually
verified against the reviews. Backfilling would mean re-running the model over history to
translate text nobody is waiting for.

The English `claim` stays: it is the record of what was checked. Rule 6 of the validator
intersects a claim's tokens with the reviews it cites, and a Russian sentence shares no
tokens with an English review, so validating the translation instead would reject every
claim ever made.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("summary_claims", schema=None) as batch_op:
        batch_op.add_column(sa.Column("claim_ru", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("summary_claims", schema=None) as batch_op:
        batch_op.drop_column("claim_ru")
