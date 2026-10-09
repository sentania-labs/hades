"""Application-layer catalog service.

Used by the API router, the admin UI page, and the CLI.  No
persistence layer: the catalog lives in a YAML file and is
re-read on every request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crucible.adapters.catalog.loader import (
    load_catalog,
)


@dataclass(frozen=True, slots=True)
class SkillView:
    """A serializable skill record."""

    name: str
    summary: str
    owner: str
    instructions_path: str
    used_by: int


@dataclass(frozen=True, slots=True)
class ToolView:
    """A serializable tool record."""

    name: str
    kind: str
    endpoint: str | None
    command: str | None
    credential_ref: str
    allowed_for: list[str]
    used_by: int


def _serialize_skill(entry: SkillView) -> dict[str, Any]:
    return {
        "name": entry.name,
        "summary": entry.summary,
        "owner": entry.owner,
        "instructions_path": entry.instructions_path,
        "used_by": entry.used_by,
    }


def _serialize_tool(entry: ToolView) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": entry.name,
        "kind": entry.kind,
        "credential_ref": entry.credential_ref,
        "allowed_for": entry.allowed_for,
        "used_by": entry.used_by,
    }
    if entry.kind == "mcp_server":
        result["endpoint"] = entry.endpoint
    else:
        result["command"] = entry.command
    return result


def view(path: Path | None = None) -> dict[str, Any]:
    """Return the catalog as a serializable mapping."""
    catalog = load_catalog(path)
    skills_out = [
        _serialize_skill(
            SkillView(
                name=s.name,
                summary=s.summary,
                owner=s.owner,
                instructions_path=s.instructions_path,
                used_by=0,
            )
        )
        for s in catalog.skills
    ]
    tools_out = [
        _serialize_tool(
            ToolView(
                name=t.name,
                kind=t.kind,
                endpoint=t.endpoint,
                command=t.command,
                credential_ref=t.credential_ref,
                allowed_for=t.allowed_for,
                used_by=0,
            )
        )
        for t in catalog.tools
    ]
    return {
        "skills": skills_out,
        "tools": tools_out,
        "errors": [{"path": e.path, "message": e.message} for e in catalog.errors],
    }
