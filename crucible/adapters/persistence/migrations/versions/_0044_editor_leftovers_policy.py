"""Add ``editor_leftovers`` to existing ``default-software`` pre_pr gates.

The gate was added to the seed in revision 0001; however, databases that have
already applied 0001 retain the policy version as-is.  Because
``configured_pre_pr_gates`` honours the policy's explicit list, the new gate
would never run on upgraded installations.  This migration inserts
``editor_leftovers`` into every ``default-software`` policy version that still
lacks it, so both fresh and upgraded deployments pick it up.

Revision ID: 0044_editor_leftovers_policy
Revises: 0043_proposed_tasks
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0044_editor_leftovers_policy"
down_revision = "0043_proposed_tasks"
branch_labels = None
depends_on = None

_POLICY_NAME = "default-software"
_GATE = "editor_leftovers"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT version, document FROM policies WHERE name = :name"),
        {"name": _POLICY_NAME},
    ).fetchall()
    for version, document_bytes in rows:
        if not isinstance(document_bytes, (bytes, bytearray)):
            continue
        doc = json.loads(document_bytes.decode("utf-8"))
        pre_pr: list[str] = list(doc.get("gates", {}).get("pre_pr", []))
        if _GATE in pre_pr:
            continue  # already present
        pre_pr.append(_GATE)
        pre_pr.sort(key=str.lower)
        doc["gates"]["pre_pr"] = pre_pr
        connection.execute(
            sa.text(
                "UPDATE policies SET document = CAST(:document AS jsonb) "
                "WHERE name = :name AND version = :version"
            ),
            {
                "document": json.dumps(doc),
                "name": _POLICY_NAME,
                "version": version,
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT version, document FROM policies WHERE name = :name"),
        {"name": _POLICY_NAME},
    ).fetchall()
    for version, document_bytes in rows:
        if not isinstance(document_bytes, (bytes, bytearray)):
            continue
        doc = json.loads(document_bytes.decode("utf-8"))
        pre_pr: list[str] = list(doc.get("gates", {}).get("pre_pr", []))
        if _GATE not in pre_pr:
            continue  # gate was not present before upgrade
        pre_pr.remove(_GATE)
        doc["gates"]["pre_pr"] = pre_pr
        connection.execute(
            sa.text(
                "UPDATE policies SET document = CAST(:document AS jsonb) "
                "WHERE name = :name AND version = :version"
            ),
            {
                "document": json.dumps(doc),
                "name": _POLICY_NAME,
                "version": version,
            },
        )
