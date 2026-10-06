"""Record why a worker stopped: the reason line of `blocked.md` and its statement.

Revision ID: 0046_blocked_reason
Revises: 0045_merge_0044_heads

hades #393: a worker that cannot do the task as written stops and writes `blocked.md`
with a reason line, `missing_capability` (a program or capability the image does not
have) or `ambiguous_contract` (the contract reads more than one way and the readings
differ in result), followed by its statement in its own words. The attempt keeps the
reason and the statement verbatim, and the escalation it opens keeps the reason beside
the question, so the person who answers sees what the worker saw. Rows that exist when
this runs keep NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_blocked_reason"
down_revision = "0045_merge_0044_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("blocked_reason", sa.String(32), nullable=True))
    op.add_column("attempts", sa.Column("blocked_statement", sa.Text, nullable=True))
    op.add_column("escalations", sa.Column("reason", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("escalations", "reason")
    op.drop_column("attempts", "blocked_statement")
    op.drop_column("attempts", "blocked_reason")
