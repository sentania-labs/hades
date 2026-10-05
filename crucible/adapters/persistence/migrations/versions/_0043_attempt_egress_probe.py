"""Record the launch wrapper's egress probe on each attempt.

Revision ID: 0043_attempt_egress_probe
Revises: 0042_pull_request_mergeable

hades #425: before the harness starts, the launch wrapper tries every host of the
attempt's egress allowlist and writes one line to the worker log; the supervisor keeps the
parsed result here, per host, so a failed dependency install is read against what the
worker could reach. Attempts that exist when this runs keep NULL: nothing probed them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0043_attempt_egress_probe"
down_revision = "0042_pull_request_mergeable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("egress_probe", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("attempts", "egress_probe")
