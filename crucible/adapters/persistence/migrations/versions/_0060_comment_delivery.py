"""Comment delivery states, minion questions and handoff events (hades #208 item 2).

Revision ID: 0060_comment_delivery
Revises: 0059_rooms

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

Written as 0057, the number Hades assigned to the task. 0058 (the memory store) and 0059
(the rooms) reached main while the branch was open, so this revision is numbered after
the highest on main and chains from 0059_rooms, as CONTRIBUTING's Migrations section
says: a revision placed below a head that databases have already reached never runs on
them, and a migration already on main is never edited. The number and `down_revision`
stay provisional until the pull request merges; Hades renumbers the file past main's
highest and points it at main's head at merge (hades #447, 23). The predecessor's event
kinds are therefore read from whatever `down_revision` names, not from a module named
here, so a renumber that lands this revision above another kinds-adding migration keeps
every kind in the CHECK constraint.
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import op

revision = "0060_comment_delivery"
down_revision = "0059_rooms"
branch_labels = None
depends_on = None

EVENT_KINDS = (
    "task_note_acknowledged",
    "task_note_acted_on",
    "minion_question_asked",
    "minion_question_answered",
    "handoff_recorded",
)
EVENT_ARCHIVE = "events_0060_archive"
NOTE_COLUMNS = (
    "acknowledged_attempt_id",
    "acknowledged_at",
    "acted_on_attempt_id",
    "acted_on_at",
    "acted_on_commit",
    "acted_on_event_seq",
    "delivery_state",
)


# The revisions live in this package; alembic loads this file under its own module name,
# so the package is named in full rather than read from __name__.
VERSIONS_PACKAGE = "crucible.adapters.persistence.migrations.versions"


def _previous_event_kinds() -> list[str]:
    """The kinds the revision below permits, read from the module `down_revision` names.

    Every kinds-adding revision has a `_event_kinds()` that lists the kinds its CHECK
    constraint allows (0001 onward). Reading the predecessor through `down_revision`
    rather than a module named here keeps the chain whole after hades #447 renumbers
    this revision and points it at a different head."""
    previous = importlib.import_module(f"{VERSIONS_PACKAGE}._{down_revision}")
    kinds: list[str] = list(previous._event_kinds())
    return kinds


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
