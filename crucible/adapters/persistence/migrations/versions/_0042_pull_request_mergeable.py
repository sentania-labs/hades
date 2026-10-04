"""Persist GitHub's nullable pull-request mergeable flag.

Revision ID: 0042_pull_request_mergeable
Revises: 0041_ci_decision_causes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_pull_request_mergeable"
down_revision = "0041_ci_decision_causes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pull_requests", sa.Column("mergeable", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("pull_requests", "mergeable")
