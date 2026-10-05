"""Record the shape and the reason of a stall Crucible ended early.

Revision ID: 0044_attempt_stall_shape
Revises: 0043_proposed_tasks

hades #278: a worker running the same command over and over, or making no tool call
before its first-response deadline, is ended as a stall before the time-based limit.
The attempt keeps the shape (loop:wait, loop:empty_command, loop:command, no_activity)
so quality feedback can count it, and the reason, which names the repeated command.
Attempts that exist when this runs keep NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_attempt_stall_shape"
down_revision = "0043_proposed_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("stall_shape", sa.String(32), nullable=True))
    op.add_column("attempts", sa.Column("termination_detail", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("attempts", "termination_detail")
    op.drop_column("attempts", "stall_shape")
