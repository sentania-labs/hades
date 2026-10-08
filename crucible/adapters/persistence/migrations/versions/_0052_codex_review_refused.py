"""hades #343: remember a repository's own Codex connector refusal.

The refusal ("To use Codex here, create a Codex account..." / "...create an
environment for this repo") is the provider's repository configuration, not a fact
about one task, so it is recorded on ``repositories`` rather than on the task that
first observed it. While set, Crucible stops posting the App's trigger comment on
that repository and wakes the orchestrator instead.

Revision ID: 0052_codex_review_refused
Revises: 0051_routing_model_references
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_codex_review_refused"
down_revision = "0051_routing_model_references"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("codex_review_refused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("repositories", "codex_review_refused_at")
