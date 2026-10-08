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


def _legacy_ids(documents: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Use one mapping across versions, keeping old unambiguous names when possible."""
    pairs = sorted(
        {(entry["harness"], entry["model"]) for doc in documents for entry in doc.get("models", [])}
    )
    preferred = {
        pair: "qwen-coder" if pair == ("qwen_code", "coder") else pair[1] for pair in pairs
    }
    reserved = set(preferred.values())
    assigned: set[str] = set()
    result = {}
    for pair in pairs:
        candidate = preferred[pair]
        if candidate in assigned:
            base = f"{pair[0]}:{pair[1]}"
            candidate = base
            suffix = 2
            while candidate in reserved or candidate in assigned:
                candidate = f"{base}:{suffix}"
                suffix += 1
        result[pair] = candidate
        assigned.add(candidate)
    return result


def _legacy(
    document: dict[str, Any], ids: dict[tuple[str, str], str] | None = None
) -> dict[str, Any]:
    migrated = copy.deepcopy(document)
    ids = _legacy_ids([document]) if ids is None else ids
    for entry in migrated.get("models", []):
        model = entry.pop("model")
        entry["id"] = ids[(entry["harness"], model)]
        # Legacy entries named model_name only when it differed from the id; writing it
        # always would leave rows an earlier revision's downgrade no longer recognizes.
        if entry["id"] != model:
            entry["model_name"] = model
        entry.pop("vanished_at", None)
    return migrated


def _rename_references(names: dict[tuple[str, str], str]) -> None:
    # CASE evaluates against the original row, so aliases that are themselves another
    # route's model cannot cascade through a second rename. Versions are untouched.
    changed = {pair: name for pair, name in names.items() if pair[1] != name}
    if not changed:
        return
    for table_name, harness_column, model_column in (
        ("executions", "harness", "model"),
        ("attempts", "selected_harness", "selected_model"),
    ):
        table = sa.table(table_name, sa.column(harness_column), sa.column(model_column))
        model = table.c[model_column]
        cases = [
            (sa.and_(table.c[harness_column] == harness, model == before), after)
            for (harness, before), after in changed.items()
        ]
        op.get_bind().execute(table.update().values({model_column: sa.case(*cases, else_=model)}))


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
    documents = op.get_bind().execute(sa.text("SELECT document FROM routing_policies")).scalars()
    aliases = {
        (entry["harness"], entry["id"]): entry.get("model_name") or entry["id"]
        for document in documents
        for entry in document.get("models", [])
    }
    _rewrite(_current)
    op.execute("ALTER TABLE executions DISABLE TRIGGER trg_executions_fenced")
    op.execute("ALTER TABLE attempts DISABLE TRIGGER trg_attempts_fenced")
    _rename_references(aliases)
    op.execute("ALTER TABLE executions ENABLE TRIGGER trg_executions_fenced")
    op.execute("ALTER TABLE attempts ENABLE TRIGGER trg_attempts_fenced")


def downgrade() -> None:
    documents = list(
        op.get_bind().execute(sa.text("SELECT document FROM routing_policies")).scalars()
    )
    ids = _legacy_ids(documents)
    _rewrite(lambda document: _legacy(document, ids))
    op.execute("ALTER TABLE executions DISABLE TRIGGER trg_executions_fenced")
    op.execute("ALTER TABLE attempts DISABLE TRIGGER trg_attempts_fenced")
    _rename_references(ids)
    op.execute("ALTER TABLE executions ENABLE TRIGGER trg_executions_fenced")
    op.execute("ALTER TABLE attempts ENABLE TRIGGER trg_attempts_fenced")
