"""Add the proposal event kinds (hades #424).

Revision ID: 0043_proposed_tasks
Revises: 0042_pull_request_mergeable

A proposed task and the operator's answers to it are events. Task states and wake
reasons are not constrained in the schema, so only the event-kind CHECK changes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0039_auto_merge_refusals import (
    _event_kinds as _previous_event_kinds,
)

revision = "0043_proposed_tasks"
down_revision = "0042_pull_request_mergeable"
branch_labels = None
depends_on = None

EVENT_KINDS = (
    "task_proposed",
    "task_approved",
    "task_sent_back",
    "task_proposal_rejected",
)
EVENT_ARCHIVE = "events_0043_archive"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    _replace_event_kinds(_event_kinds())
    connection = op.get_bind()
    archive = connection.execute(
        sa.text("SELECT to_regclass(:name)"), {"name": f"public.{EVENT_ARCHIVE}"}
    ).scalar()
    if archive:
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
        op.execute(f"INSERT INTO events SELECT * FROM {EVENT_ARCHIVE}")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
        op.execute(f"DROP TABLE {EVENT_ARCHIVE}")


def downgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in EVENT_KINDS)
    # `events` is append-only, so its rows of the new kinds are archived, not discarded,
    # with the triggers stood down for exactly these statements; upgrade() restores them.
    # A task left in `proposed` or `sent_back` is not a state the older code knows; it is
    # cancelled, as the downgrade of 0011 settles `awaiting_quota`.
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    op.execute("UPDATE tasks SET state='cancelled' WHERE state IN ('proposed', 'sent_back')")
    _replace_event_kinds(_previous_event_kinds())
