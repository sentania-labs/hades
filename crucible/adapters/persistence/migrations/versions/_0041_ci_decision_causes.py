"""Add implementation_defect and missing_worker_tooling CI decision causes.

Revision ID: 0041_ci_decision_causes
Revises: 0040_merge_auto_merge_settings
"""

from __future__ import annotations

from alembic import op

revision = "0041_ci_decision_causes"
down_revision = "0040_merge_auto_merge_settings"
branch_labels = None
depends_on = None

OLD_CAUSES = (
    "false_pre_pr_evidence",
    "wrong_sha_checked",
    "correction_without_checks",
    "environment_drift",
    "flaky_test",
    "crucible_verification_defect",
    "ci_infrastructure",
    "other",
)

NEW_CAUSES = (*OLD_CAUSES, "implementation_defect", "missing_worker_tooling")


def _replace(causes: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{cause}'" for cause in causes)
    op.execute("ALTER TABLE ci_decisions DROP CONSTRAINT ck_ci_decisions_cause")
    op.execute(
        f"ALTER TABLE ci_decisions ADD CONSTRAINT ck_ci_decisions_cause CHECK (cause IN ({allowed}))"
    )


def upgrade() -> None:
    _replace(NEW_CAUSES)


def downgrade() -> None:
    _replace(OLD_CAUSES)
