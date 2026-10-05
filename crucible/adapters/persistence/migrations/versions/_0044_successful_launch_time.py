"""hades #389: track the last successful launch time for credential-state recovery.

When an auth failure occurs followed by a successful launch, the old comparison
against ``last_launch_at`` (which moves on every launch) was not enough because
``last_launch_at`` also changes on failures.  A new column
``harnesses.last_successful_launch_at`` is updated only on non-failure outcomes,
so the credential-state check can use it to tell whether a later success cleared
the failure.

Revision ID: 0044_successful_launch_time
Revises: 0043_proposed_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_successful_launch_time"
down_revision = "0043_proposed_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harnesses",
        sa.Column("last_successful_launch_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("harnesses", "last_successful_launch_at")
