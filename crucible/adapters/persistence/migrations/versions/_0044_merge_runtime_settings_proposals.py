"""Merge the credential setting and proposed task migration heads.

Revision ID: 0044_merge_423_424
Revises: 0043_credential_mount_mode, 0043_proposed_tasks
"""

from __future__ import annotations

from alembic import op

from crucible.adapters.persistence.migrations.versions._0043_credential_mount_mode import (
    _event_kinds as _credential_event_kinds,
)
from crucible.adapters.persistence.migrations.versions._0043_proposed_tasks import (
    EVENT_KINDS as PROPOSAL_EVENT_KINDS,
)

revision = "0044_merge_423_424"
down_revision = ("0043_credential_mount_mode", "0043_proposed_tasks")
branch_labels = None
depends_on = None


def _event_kinds() -> list[str]:
    return [*_credential_event_kinds(), *PROPOSAL_EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())


def downgrade() -> None:
    pass
