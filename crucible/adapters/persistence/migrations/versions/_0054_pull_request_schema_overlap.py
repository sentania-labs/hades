"""Record the schema a pull request's migrations touch (hades #447).

Revision ID: 0054_pull_request_schema_overlap
Revises: 0053_cert_change_class

This revision was written as 0052 on 0051_routing_model_references, renumbered to 0054
on 0053_cert_change_class when main was first merged into the branch, and renumbered
again to 0055 on 0054_digest_commit (hades #443) at the next merge-main, as hades #447
describes: a branch's migration numbers are provisional until the pull request merges.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054_pull_request_schema_overlap"
down_revision = "0053_cert_change_class"
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
