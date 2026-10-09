"""Catalog loader: reads config/catalog.yaml, validates schema and values.

Refuses unknown kinds, credential_ref values that look like secrets, and
any field that does not match the documented schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from crucible.domain.secrets import scan_text

CATALOG_PATH = Path(__file__).resolve().parents[3] / "config" / "catalog.yaml"

VALID_KINDS = frozenset({"mcp_server", "cli", "script"})


@dataclass(frozen=True, slots=True)
class CatalogError:
    """A validation failure that stopped the catalog from loading."""

    path: str
    message: str


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One catalog skill entry."""

    name: str
    summary: str
    owner: str
    instructions_path: str


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One catalog tool entry."""

    name: str
    kind: str
    endpoint: str | None
    command: str | None
    credential_ref: str
    allowed_for: list[str]


@dataclass
class Catalog:
    """The loaded and validated catalog."""

    skills: list[SkillEntry]
    tools: list[ToolEntry]
    errors: list[CatalogError] = field(default_factory=list)


def _check_secret(value: str, field: str) -> CatalogError | None:
    """Return an error if ``value`` looks like a secret."""
    hit = scan_text(value)
    if hit is not None:
        return CatalogError(
            path=field,
            message=f"{field} looks like a {hit} and is refused",
        )
    return None


def _validate_skill(data: dict[str, Any]) -> list[CatalogError]:
    """Return errors for one skill dict."""
    errors: list[CatalogError] = []
    for key in ("name", "summary", "owner", "instructions_path"):
        if key not in data:
            errors.append(CatalogError(path=key, message="required field missing"))
    if errors:
        return errors
    if not isinstance(data["name"], str):
        errors.append(CatalogError(path="name", message="must be a string"))
    if not isinstance(data["summary"], str):
        errors.append(CatalogError(path="summary", message="must be a string"))
    if not isinstance(data["owner"], str):
        errors.append(CatalogError(path="owner", message="must be a string"))
    if not isinstance(data["instructions_path"], str):
        errors.append(CatalogError(path="instructions_path", message="must be a string"))
    return errors


def _validate_tool(data: dict[str, Any]) -> list[CatalogError]:
    """Return errors for one tool dict."""
    errors: list[CatalogError] = []
    for key in ("name", "kind", "credential_ref"):
        if key not in data:
            errors.append(CatalogError(path=key, message="required field missing"))
    if errors:
        return errors
    kind = data.get("kind")
    if kind not in VALID_KINDS:
        errors.append(
            CatalogError(
                path="kind",
                message=f"{kind!r} is not a valid kind; must be one of {sorted(VALID_KINDS)}",
            )
        )
    if not isinstance(data["name"], str) or not data["name"]:
        errors.append(CatalogError(path="name", message="must be a non-empty string"))
    if not isinstance(data["credential_ref"], str) or not data["credential_ref"]:
        errors.append(CatalogError(path="credential_ref", message="must be a non-empty string"))
    else:
        secret_err = _check_secret(data["credential_ref"], "credential_ref")
        if secret_err:
            errors.append(secret_err)
    endpoint = data.get("endpoint")
    command = data.get("command")
    if kind == "mcp_server":
        if endpoint is None or not isinstance(endpoint, str) or not endpoint:
            errors.append(CatalogError(path="endpoint", message="required when kind is mcp_server"))
        if command is not None:
            errors.append(
                CatalogError(
                    path="command",
                    message="must not be set when kind is mcp_server",
                )
            )
    else:
        if command is None or not isinstance(command, str) or not command:
            errors.append(
                CatalogError(path="command", message="required when kind is cli or script")
            )
        if endpoint is not None:
            errors.append(
                CatalogError(
                    path="endpoint",
                    message="must not be set when kind is cli or script",
                )
            )
    # credential_ref must look like a name (no whitespace, no colons)
    cr = data["credential_ref"]
    if re.search(r"\s", cr) or ":" in cr:
        errors.append(
            CatalogError(
                path="credential_ref",
                message="must be a plain name without spaces or colons",
            )
        )
    allowed = data.get("allowed_for")
    if allowed is not None:
        if not isinstance(allowed, list):
            errors.append(
                CatalogError(path="allowed_for", message="must be a list of role names or absent")
            )
        else:
            for item in allowed:
                if not isinstance(item, str):
                    errors.append(
                        CatalogError(
                            path="allowed_for",
                            message="every item must be a role name string",
                        )
                    )
    return errors


def load_catalog(path: Path | None = None) -> Catalog:
    """Read, validate, and return the catalog from *path*.

    The path defaults to ``config/catalog.yaml`` relative to the project
    root (the directory that contains ``crucible/``).
    """
    loc = path or CATALOG_PATH
    raw = loc.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if data is None:
        return Catalog(skills=[], tools=[], errors=[CatalogError("", "catalog is empty")])
    errors: list[CatalogError] = []
    skills_raw = data.get("skills", [])
    tools_raw = data.get("tools", [])
    skills: list[SkillEntry] = []
    for _i, item in enumerate(skills_raw):
        item_errors = _validate_skill(item)
        if item_errors:
            errors.extend(item_errors)
        else:
            skills.append(
                SkillEntry(
                    name=str(item["name"]),
                    summary=str(item["summary"]),
                    owner=str(item["owner"]),
                    instructions_path=str(item["instructions_path"]),
                )
            )
    tools: list[ToolEntry] = []
    for _i, item in enumerate(tools_raw):
        item_errors = _validate_tool(item)
        if item_errors:
            errors.extend(item_errors)
        else:
            tools.append(
                ToolEntry(
                    name=str(item["name"]),
                    kind=str(item["kind"]),
                    endpoint=item.get("endpoint"),
                    command=item.get("command"),
                    credential_ref=str(item["credential_ref"]),
                    allowed_for=list(item.get("allowed_for", [])),
                )
            )
    return Catalog(skills=skills, tools=tools, errors=errors)
