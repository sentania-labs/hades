"""The shared memory store and the decision ledger (hades #208).

Revision ID: 0058_memory_and_decisions
Revises: 0057_comment_delivery

Transcripts stay per channel. Decisions and memory are shared by every channel and every
persona. `memory_items` holds the facts every Hades channel recalls: the text, where it
came from, when it was observed, the scope tags a recall matches, and who promoted it
and when. An item is never edited in place: a change is a new item that supersedes it
(`superseded_by`), and a forget retires it with no replacement, so `superseded_at` alone
marks an item that is no longer current. `decision_ledger` is the append-only ledger of
the principal's words: who said them, in which channel, when, the words verbatim, the
transcript position, what they apply to, and who acted on them and when. The name
`decisions` was taken by Foundry's per-task decisions in 0004, which are mirrored into
the ledger from this revision on; the ledger table is therefore `decision_ledger`. The
ledger gets the append-only trigger 0001 gives events. Four event kinds join the audit:
`memory_promoted`, `memory_superseded`, `memory_forgotten` and
`ledger_decision_recorded`.

The number is the one Hades assigned (0058); the down_revision is provisional until merge
(hades #447) and now follows 0057_comment_delivery, hades #208 item 2's revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from crucible.adapters.persistence.migrations.versions._0057_comment_delivery import (
    _event_kinds as _previous_event_kinds,
)

revision = "0058_memory_and_decisions"
down_revision = "0057_comment_delivery"
branch_labels = None
depends_on = None

EVENT_KINDS = (
    "memory_promoted",
    "memory_superseded",
    "memory_forgotten",
    "ledger_decision_recorded",
)
EVENT_ARCHIVE = "events_0058_archive"
LEDGER = "decision_ledger"
MEMORY = "memory_items"


def _event_kinds() -> list[str]:
    return [*_previous_event_kinds(), *EVENT_KINDS]


def _replace_event_kinds(kinds: list[str]) -> None:
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute("ALTER TABLE events DROP CONSTRAINT ck_events_kind")
    op.execute(f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed}))")


def upgrade() -> None:
    op.create_table(
        MEMORY,
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "scope_tags",
            postgresql.ARRAY(sa.String(length=64)),
            nullable=False,
            server_default=sa.text("'{}'::character varying[]"),
        ),
        sa.Column("promoted_by", sa.String(length=128), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "superseded_by",
            sa.String(length=26),
            sa.ForeignKey("memory_items.id"),
            nullable=True,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_memory_items_observed", MEMORY, ["observed_at"])
    op.create_index("ix_memory_items_scope_tags", MEMORY, ["scope_tags"], postgresql_using="gin")
    op.create_table(
        LEDGER,
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column("principal", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("said_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verbatim", sa.Text(), nullable=False),
        sa.Column("transcript_ref", sa.Text(), nullable=True),
        sa.Column(
            "applies_to",
            postgresql.ARRAY(sa.String(length=128)),
            nullable=False,
            server_default=sa.text("'{}'::character varying[]"),
        ),
        sa.Column("acted_by", sa.String(length=128), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_decision_ledger_said", LEDGER, ["said_at"])
    op.create_index("ix_decision_ledger_channel_said", LEDGER, ["channel", "said_at"])
    # The ledger is append-only, the way 0001 made events: crucible_reject_mutation()
    # refuses every UPDATE and DELETE.
    op.execute(
        f"CREATE TRIGGER trg_{LEDGER}_append_only BEFORE UPDATE OR DELETE ON {LEDGER} "
        "FOR EACH ROW EXECUTE FUNCTION crucible_reject_mutation();"
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
    op.execute(f"DROP TRIGGER IF EXISTS trg_{LEDGER}_append_only ON {LEDGER};")
    op.drop_index("ix_decision_ledger_channel_said", table_name=LEDGER)
    op.drop_index("ix_decision_ledger_said", table_name=LEDGER)
    op.drop_table(LEDGER)
    op.drop_index("ix_memory_items_scope_tags", table_name=MEMORY)
    op.drop_index("ix_memory_items_observed", table_name=MEMORY)
    op.drop_table(MEMORY)
