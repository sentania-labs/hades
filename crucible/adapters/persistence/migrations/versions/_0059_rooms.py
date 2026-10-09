"""Rooms and their transcripts (hades #208, ADR 0031).

Revision ID: 0059_rooms
Revises: 0058_memory_and_decisions

A room is one conversation with Hades: the principal room, or a room about one card.
Hades owns the transcript. `rooms` holds the room: its kind, the card it is about (a
task, for a card room), the harness and model it runs on, its state (idle, starting,
warm, interrupted, closed), when it was created and last active, the runner's handle and
harness session while one is up, who created it, the tasks its runner token may act on
(`scope_task_ids`), the salted digest of that token (never the token), the inbox cursor,
the instruction the runner reads next, and when the runner last polled. `room_turns` is the transcript: one row per
turn, unique by room and seq, with its role (user, assistant, system), its words, the
tool calls the runner's PreToolUse hook recorded, when it started and ended, whether it
was interrupted, and the ledger decision recorded from it. Seven event kinds join the
audit.

The number is the one Hades assigned (0059); the down_revision is provisional and Hades
points it at main's head at merge (hades #447).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from crucible.adapters.persistence.migrations.versions._0058_memory_and_decisions import (
    _event_kinds as _previous_event_kinds,
)

revision = "0059_rooms"
down_revision = "0058_memory_and_decisions"
branch_labels = None
depends_on = None

EVENT_KINDS = (
    "room_created",
    "room_runner_launched",
    "room_runner_stopped",
    "room_interrupted",
    "room_switched",
    "room_closed",
    "room_idle_timeout_updated",
)
EVENT_ARCHIVE = "events_0059_archive"
ROOMS = "rooms"
TURNS = "room_turns"
KINDS = ("principal", "card")
STATES = ("idle", "starting", "warm", "interrupted", "closed")
ROLES = ("user", "assistant", "system")


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def _among(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        ROOMS,
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("card_task_id", sa.String(length=26), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("harness", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runner_handle", sa.String(length=256), nullable=True),
        sa.Column("session_id", sa.String(length=256), nullable=True),
        sa.Column(
            "created_by", sa.String(length=26), sa.ForeignKey("principals.id"), nullable=False
        ),
        sa.Column(
            "scope_task_ids",
            postgresql.ARRAY(sa.String(length=26)),
            nullable=False,
            server_default=sa.text("'{}'::character varying[]"),
        ),
        sa.Column("runner_key_salt", sa.LargeBinary(), nullable=True),
        sa.Column("runner_key_digest", sa.LargeBinary(), nullable=True),
        sa.Column("inbox_cursor", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pending_control", sa.String(length=16), nullable=True),
        sa.Column("runner_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_among("kind", KINDS), name="ck_rooms_kind"),
        sa.CheckConstraint(_among("state", STATES), name="ck_rooms_state"),
        sa.CheckConstraint(
            "(kind = 'card') = (card_task_id IS NOT NULL)", name="ck_rooms_card_names_a_task"
        ),
    )
    op.create_index("ix_rooms_state", ROOMS, ["state"])
    op.create_index("ix_rooms_last_activity", ROOMS, ["last_activity_at"])
    op.create_table(
        TURNS,
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("room_id", sa.String(length=26), sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interrupted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "decision_id",
            sa.String(length=26),
            sa.ForeignKey("decision_ledger.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("room_id", "seq", name="uq_room_turns_room_seq"),
        sa.CheckConstraint(_among("role", ROLES), name="ck_room_turns_role"),
    )
    _replace_event_kinds(_event_kinds())
    if op.get_context().as_sql:
        # Offline SQL rendering cannot ask the database whether an archive exists.
        return
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
    op.drop_table(TURNS)
    op.drop_index("ix_rooms_last_activity", table_name=ROOMS)
    op.drop_index("ix_rooms_state", table_name=ROOMS)
    op.drop_table(ROOMS)
