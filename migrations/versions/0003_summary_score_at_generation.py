"""Record the score a summary was written against.

Additive and reversible: one nullable column. Existing rows keep NULL, and the
regeneration rule treats NULL as "unknown, do not trigger" rather than as zero — the same
rule the rest of the system applies to a missing score.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("summaries", sa.Column("score_at_generation", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("summaries", "score_at_generation")
