"""Merge 0052_task_notes and 0052_cert_change_class (hades #476 and #489).

Revision ID: 0053_merge_0052
Revises: 0052_cert_change_class, 0052_task_notes

Two branches both numbered their migration 0052 on top of 0051_routing_model_references.
This merge revision is the single head again. Both 0052s registered their own event kinds
in their upgrade(), so the merge just restores the line.
"""

from __future__ import annotations

from crucible.adapters.persistence.migrations.versions._0052_task_notes import (
    _event_kinds as _all_kinds,
)
from crucible.adapters.persistence.migrations.versions._0052_task_notes import (
    _replace_event_kinds,
)

revision = "0053_merge_0052"
down_revision = ("0052_cert_change_class", "0052_task_notes")
branch_labels = None
depends_on = None


def upgrade() -> None:
    _replace_event_kinds(_all_kinds())


def downgrade() -> None:
    _replace_event_kinds(_all_kinds())
