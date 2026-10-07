"""Make routing entries (harness, model) references.

Revision ID: 0051_routing_model_references
Revises: 0050_status_cache
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0051_routing_model_references"
down_revision = "0050_status_cache"
branch_labels = None
depends_on = None


def _current(document: dict[str, Any]) -> dict[str, Any]:
    migrated = copy.deepcopy(document)
    for entry in migrated.get("models", []):
        model = entry.get("model_name") or entry.get("id")
        entry["model"] = model
        entry.pop("model_name", None)
        entry.pop("id", None)
    return migrated


def _legacy(document: dict[str, Any]) -> dict[str, Any]:
    migrated = copy.deepcopy(document)
    for entry in migrated.get("models", []):
        model = entry.pop("model")
        entry["id"] = (
            "qwen-coder" if entry.get("harness") == "qwen_code" and model == "coder" else model
        )
    return migrated


def _rewrite(transform: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT name, version, document FROM routing_policies"))
    for name, version, document in rows:
        updated = transform(document)
        connection.execute(
            sa.text(
                "UPDATE routing_policies SET document=CAST(:document AS jsonb) "
                "WHERE name=:name AND version=:version"
            ),
            {"name": name, "version": version, "document": json.dumps(updated)},
        )


def upgrade() -> None:
    _rewrite(_current)
    # This changes only the endpoint model spelling.  The attempt's recorded routing
    # version stays untouched, so an in-flight task is not moved to another policy.
    op.execute(
        "UPDATE executions SET model='coder' WHERE harness='qwen_code' AND model='qwen-coder'"
    )
    op.execute(
        "UPDATE attempts SET selected_model='coder' "
        "WHERE selected_harness='qwen_code' AND selected_model='qwen-coder'"
    )


def downgrade() -> None:
    _rewrite(_legacy)
    op.execute(
        "UPDATE executions SET model='qwen-coder' WHERE harness='qwen_code' AND model='coder'"
    )
    op.execute(
        "UPDATE attempts SET selected_model='qwen-coder' "
        "WHERE selected_harness='qwen_code' AND selected_model='coder'"
    )
