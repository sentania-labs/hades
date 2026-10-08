"""hades #476: record the change class on a CI certification.

Revision ID: 0052_cert_change_class
Revises: 0051_routing_model_references
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_cert_change_class"
down_revision = "0051_routing_model_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ci_certifications",
        sa.Column("change_class", sa.String(16), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("ci_certifications", "change_class")
