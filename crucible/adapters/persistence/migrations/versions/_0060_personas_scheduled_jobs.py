"""Personas and scheduled jobs (hades #208).

Revision ID: 0060_personas_scheduled_jobs
Revises: 0058_memory_and_decisions

The number is assigned for FDY-0591 and must not be renumbered in this branch. The
down revision remains provisional under hades #447.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060_personas_scheduled_jobs"
down_revision = "0058_memory_and_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personas",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("role_text", sa.Text(), nullable=False),
        sa.Column("skills", postgresql.JSONB(), nullable=False),
        sa.Column("tools", postgresql.JSONB(), nullable=False),
        sa.Column("default_harness", sa.String(64), nullable=False),
        sa.Column("default_model", sa.String(256), nullable=False),
        sa.Column("default_tier", sa.String(16), nullable=False),
        sa.Column("budget_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "default_tier IN ('trivial','standard','complex')", name="ck_personas_tier"
        ),
        sa.CheckConstraint("budget_usd >= 0", name="ck_personas_budget"),
    )
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("persona_id", sa.String(26), sa.ForeignKey("personas.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("task_kind", sa.String(16), nullable=False),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("cadence", sa.String(128), nullable=False),
        sa.Column("cadence_label", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Chicago"),
        sa.Column("results_to", sa.String(24), nullable=False),
        sa.Column(
            "carry_notes_forward", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("project", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.CheckConstraint("task_kind IN ('prompt','script')", name="ck_scheduled_jobs_kind"),
        sa.CheckConstraint(
            "results_to IN ('inbox_card','chat_message','report_only')",
            name="ck_scheduled_jobs_results",
        ),
        sa.CheckConstraint("timezone = 'America/Chicago'", name="ck_scheduled_jobs_timezone"),
    )
    op.create_index("ix_scheduled_jobs_due", "scheduled_jobs", ["enabled", "next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_jobs_due", table_name="scheduled_jobs")
    op.drop_table("scheduled_jobs")
    op.drop_table("personas")
