"""Device tokens (hades #576, U9) and the prompt-cache reads of each attempt (hades #604).

Revision ID: 0062_device_tokens_usage
Revises: 0061_comment_delivery

`devices` is one row per device token an administrator mints for a phone or the iOS
app. The token itself is the token of the device's own principal (`device:<name>`):
its salted hash stays on `principals`, so nothing in this table is a credential. The row
records who minted it, when and with what user agent it was last used, the one time it
was exchanged for a UI session, and its revocation. Three event kinds join the audit:
`device_token_minted`, `device_token_used` and `device_token_revoked`.

`attempt_metrics.tokens_cache_read` is the input the harness read from its prompt cache,
where its own usage report gives it, beside the tokens in, tokens out and cost already
there.

The number and `down_revision` are provisional until the pull request merges; Hades
renumbers the file past main's highest and points it at main's head at merge (hades
#447). The predecessor's event kinds are read by walking down from whatever
`down_revision` names, as 0061 does, so a renumber keeps every kind in the CHECK
constraint.
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import op

revision = "0062_device_tokens_usage"
down_revision = "0061_comment_delivery"
branch_labels = None
depends_on = None

EVENT_KINDS = (
    "device_token_minted",
    "device_token_used",
    "device_token_revoked",
)
EVENT_ARCHIVE = "events_0062_archive"

# The revisions live in this package; alembic loads this file under its own module name,
# so the package is named in full rather than read from __name__.
VERSIONS_PACKAGE = "crucible.adapters.persistence.migrations.versions"


def _previous_event_kinds() -> list[str]:
    """The kinds the revisions below permit, read by walking down from `down_revision`
    to the nearest revision with an `_event_kinds()` (see 0061)."""
    below: str | None = down_revision
    while below is not None:
        previous = importlib.import_module(f"{VERSIONS_PACKAGE}._{below}")
        if hasattr(previous, "_event_kinds"):
            kinds: list[str] = list(previous._event_kinds())
            return kinds
        below = previous.down_revision
    raise RuntimeError("no revision below sets the event kinds")


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column(
            "principal_id",
            sa.String(length=26),
            sa.ForeignKey("principals.id"),
            primary_key=True,
        ),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_user_agent", sa.String(length=512), nullable=True),
        sa.Column("exchanged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=128), nullable=True),
    )
    op.add_column("attempt_metrics", sa.Column("tokens_cache_read", sa.BigInteger(), nullable=True))
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
    op.drop_column("attempt_metrics", "tokens_cache_read")
    op.drop_table("devices")
