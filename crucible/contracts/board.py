"""Public board resource contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from crucible.contracts.common import SCHEMA_VERSION


class BoardAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    method: Literal["POST"] = "POST"
    path: str
    body: dict[str, Any]
    confirm: bool = False
    note_optional: bool = False


class BoardCard(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    external_id: str
    project: str
    title: str
    issues: list[dict[str, str]]
    pull_request: dict[str, Any] | None
    harness: str | None
    model: str | None
    tier: str
    waiting_on: str
    age: dict[str, Any]
    actions: list[BoardAction] = Field(default_factory=list)


class BoardLane(BaseModel):
    model_config = ConfigDict(extra="allow")
    key: str
    name: str
    meaning: str
    collapsed: bool
    count: int
    cards: list[BoardCard]


class BoardResource(BaseModel):
    """Seven-lane operator board. Observer cards never contain actions."""

    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION
    generated_at: str
    needs_me: int
    counts: dict[str, int]
    lanes: list[BoardLane]
