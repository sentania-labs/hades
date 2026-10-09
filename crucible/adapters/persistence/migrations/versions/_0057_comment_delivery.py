"""Comment delivery states, minion questions and handoff events (hades #208 item 2).

Revision ID: 0057_comment_delivery
Revises: 0056_pull_request_schema_overlap

A note on a task now carries a delivery state the supervisor sets from evidence:
`awaiting` (written, no attempt has been given it), `acknowledged` (the note was in an
attempt's IDENTITY.md; the attempt and the time are recorded) and `acted_on` (that
attempt's report referenced the note; the collected commit and the event are recorded).
Every existing note starts `awaiting`. A worker's question becomes a record in
`minion_questions`: the words, the attempt that asked, when, and once answered who
answered, when, the answer and how it reached the worker. Five event kinds join the
audit: `task_note_acknowledged`, `task_note_acted_on`, `minion_question_asked`,
`minion_question_answered` and `handoff_recorded`, the last for a decision or an action
(accept, merge, cancel, reroute) Foundry hands to Hades or Hades hands to Foundry during
bootstrap, with the principal, the local time and the words.

The number and `down_revision` are provisional until the pull request merges (hades #447).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0055_task_notes import (
    _event_kinds as _previous_event_kinds,
)

revision = "0057_comment_delivery"
down_revision = "0056_pull_request_schema_overlap"
branch_labels = None
depends_on = None

EVENT_KINDS = (
    "task_note_acknowledged",
    "task_note_acted_on",
    "minion_question_asked",
    "minion_question_answered",
    "handoff_recorded",
)
EVENT_ARCHIVE = "events_0057_archive"
NOTE_COLUMNS = (
    "acknowledged_attempt_id",
    "acknowledged_at",
    "acted_on_attempt_id",
    "acted_on_at",
    "acted_on_commit",
    "acted_on_event_seq",
    "delivery_state",
)


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    op.add_column(
        "task_notes",
        sa.Column(
            "delivery_state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'awaiting'"),
        ),
    )
    op.add_column(
        "task_notes",
        sa.Column(
            "acknowledged_attempt_id",
            sa.String(length=26),
            sa.ForeignKey("attempts.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "task_notes", sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "task_notes",
        sa.Column(
            "acted_on_attempt_id",
            sa.String(length=26),
            sa.ForeignKey("attempts.id"),
            nullable=True,
        ),
    )
    op.add_column("task_notes", sa.Column("acted_on_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task_notes", sa.Column("acted_on_commit", sa.String(length=64), nullable=True))
    op.add_column("task_notes", sa.Column("acted_on_event_seq", sa.BigInteger(), nullable=True))
    op.create_table(
        "minion_questions",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("task_id", sa.String(length=26), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column(
            "asked_by_attempt_id",
            sa.String(length=26),
            sa.ForeignKey("attempts.id"),
            nullable=False,
        ),
        sa.Column(
            "escalation_id",
            sa.String(length=26),
            sa.ForeignKey("escalations.id"),
            nullable=True,
        ),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "answered_by", sa.String(length=26), sa.ForeignKey("principals.id"), nullable=True
        ),
        sa.Column("answered_by_name", sa.String(length=128), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("answer_action", sa.String(length=16), nullable=True),
        sa.Column("answer_contract_version", sa.Integer(), nullable=True),
    )
    op.create_index("ix_minion_questions_task_asked", "minion_questions", ["task_id", "asked_at"])
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
    op.drop_index("ix_minion_questions_task_asked", table_name="minion_questions")
    op.drop_table("minion_questions")
    for column in NOTE_COLUMNS:
        op.drop_column("task_notes", column)
