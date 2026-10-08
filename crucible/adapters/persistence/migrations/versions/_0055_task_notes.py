"""Operator notes on a task, and the board card's phase actions (hades #489).

Revision ID: 0055_task_notes
Revises: 0054_digest_commit

A note is an operator's words on one task: who wrote it, when, the text as typed, and
whether it is verbatim. Hades shows notes on the board card newest first and puts them
at the top of the next attempt's or correction's IDENTITY.md, so the worker reads them
before the contract. Two event kinds join the audit: `task_note_recorded` for the note
itself and `task_phase_action_applied` for a move applied from the card, which carries
the note's text as the verbatim of that decision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0054_digest_commit import (
    _event_kinds as _previous_event_kinds,
)

revision = "0055_task_notes"
down_revision = "0054_digest_commit"
branch_labels = None
depends_on = None

EVENT_KINDS = ("task_note_recorded", "task_phase_action_applied")
EVENT_ARCHIVE = "events_0055_archive"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    op.create_table(
        "task_notes",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("task_id", sa.String(length=26), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column(
            "principal_id",
            sa.String(length=26),
            sa.ForeignKey("principals.id"),
            nullable=False,
        ),
        sa.Column("author", sa.String(length=128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("verbatim", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_notes_task_created", "task_notes", ["task_id", "created_at"])
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
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({kinds})")
    op.execute(f"DELETE FROM events WHERE kind IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    _replace_event_kinds(_previous_event_kinds())
    op.drop_index("ix_task_notes_task_created", table_name="task_notes")
    op.drop_table("task_notes")
