"""Record the schema a pull request's migrations touch (hades #447).

Revision ID: 0052_pull_request_schema_overlap
Revises: 0051_routing_model_references

This revision's number and down_revision are provisional: Hades assigns them when the
pull request merges (hades #447).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_pull_request_schema_overlap"
down_revision = "0051_routing_model_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column("schema_tables", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pull_requests",
        sa.Column("schema_columns", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pull_requests",
        sa.Column("schema_models", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pull_requests", "schema_models")
    op.drop_column("pull_requests", "schema_columns")
    op.drop_column("pull_requests", "schema_tables")
