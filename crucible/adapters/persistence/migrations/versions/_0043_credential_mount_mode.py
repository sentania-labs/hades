"""Allow the credential mount-mode audit event.

Revision ID: 0043_credential_mount_mode
Revises: 0042_pull_request_mergeable

0043_proposed_tasks revises 0042 as well, and alembic applies the two siblings in either
order. A database that applied the sibling first (one standing on 0043_proposed_tasks,
0044_attempt_stall_shape or 0044_editor_leftovers_policy) already permits and may hold
proposal events, which a CHECK rebuilt from the 0039 kinds alone would reject, so this
revision keeps every kind the live constraint permits and adds its own (FDY-0385).
0044_merge_423_424 then settles the union of both siblings.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

from crucible.adapters.persistence.migrations.versions._0039_auto_merge_refusals import (
    _event_kinds as _previous_event_kinds,
)

revision = "0043_credential_mount_mode"
down_revision = "0042_pull_request_mergeable"
branch_labels = None
depends_on = None

EVENT_KINDS = ("credential_mount_mode_set",)
EVENT_ARCHIVE = "events_0043_archive"
_QUOTED = re.compile(r"'([^']*)'")


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _kinds_in(definition: str | None) -> list[str]:
    """The kinds a `ck_events_kind` definition permits, as pg_get_constraintdef renders a
    `kind IN (...)` CHECK on a varchar column:
    `CHECK (((kind)::text = ANY ((ARRAY['a'::character varying, ...])::text[])))`."""
    return _QUOTED.findall(definition or "")


def _permitted_event_kinds(connection: sa.Connection) -> list[str]:
    return _kinds_in(
        connection.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = to_regclass('public.events') AND conname = 'ck_events_kind'"
            )
        ).scalar()
    )


def _kinds_to_permit(permitted: list[str]) -> list[str]:
    """This revision's kinds, then every kind the live constraint permits beyond them.

    Where only 0042 has been applied the live constraint permits the 0039 kinds and the
    result is exactly `_event_kinds()`. Where 0043_proposed_tasks has been applied the
    proposal kinds stay permitted, so the rows of those kinds survive the rebuild."""
    kinds = _event_kinds()
    return [*kinds, *(kind for kind in permitted if kind not in kinds)]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    connection = op.get_bind()
    _replace_event_kinds(_kinds_to_permit(_permitted_event_kinds(connection)))
    archive = connection.execute(
        sa.text("SELECT to_regclass(:name)"),
        {"name": f"public.{EVENT_ARCHIVE}"},
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
