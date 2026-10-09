"""The Admin policies page's service (hades #606): read a delivery policy's versions,
show the version in force in groups, edit those groups as plain values, and publish the
edit as the next version of the policy, recorded with the operator and the reason.

Every group is the document's own fields flattened to dotted paths (`limits.grace_seconds`,
`concurrency.per_harness.codex`), so the page edits what the document holds and nothing
else; `put_policy` validates the result exactly as PUT /v1/policies/{name}/{version} does.
Services are keyed by kind, so a service the version does not declare can be added.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from typing import Any, get_args

from crucible.application.admin.context import AdminContext, guard_mutation
from crucible.application.errors import ContractValidationError, NotFoundError
from crucible.application.policies import put_policy
from crucible.contracts.policy import TestServiceDeclaration
from crucible.domain.entities import Policy, Principal
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork

# The groups the page shows and edits, in order: (key, title, the document paths in it).
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("routing", "Routing", ("routing",)),
    ("services", "Services", ("services",)),
    ("gates", "Gates", ("gates",)),
    ("network", "Network allowlist", ("network.egress_allowlist",)),
    ("limits", "Limits", ("limits",)),
    ("concurrency", "Concurrency", ("concurrency",)),
)

# Paths whose value may be absent (null) but is a list of names when set.
LIST_PATHS = frozenset({"gates.advisory"})

SERVICE_KINDS: tuple[str, ...] = get_args(TestServiceDeclaration.model_fields["kind"].annotation)

FIELD_PREFIX = "p."

Leaf = tuple[str, Any]


def _flatten(value: Any, path: str) -> Iterator[Leaf]:
    if isinstance(value, Mapping):
        for key in value:
            yield from _flatten(value[key], f"{path}.{key}" if path else str(key))
    elif isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
        for index, item in enumerate(value):
            yield from _flatten(item, f"{path}.{index}")
    else:
        yield path, value


def _get(document: Mapping[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _set(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = document
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _services(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every known service kind with the values the version declares, or the defaults
    and `declared` false when it declares none of that kind."""
    declared = {
        str(item.get("kind")): item
        for item in document.get("services") or []
        if isinstance(item, Mapping)
    }
    out: dict[str, dict[str, Any]] = {}
    for kind in SERVICE_KINDS:
        base = TestServiceDeclaration(kind=kind).model_dump(mode="json")
        found = declared.get(kind)
        values = copy.deepcopy(dict(found)) if found is not None else base
        values.pop("kind", None)
        out[kind] = {"declared": found is not None, **values}
    return out


def _input_kind(path: str, value: Any) -> str:
    if isinstance(value, bool):
        return "checkbox"
    if isinstance(value, int):
        return "number"
    if isinstance(value, list) or path in LIST_PATHS:
        return "list"
    return "text"


def _shown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _label(path: str) -> str:
    return path.replace("_", " ").replace(".", ": ", 1).replace(".", " ")


def group_fields(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The editable fields of `document`, group by group, each with its dotted path, its
    input kind and its current value (a list shown comma separated)."""
    view = copy.deepcopy(dict(document))
    ref = (view.get("routing") or {}).get("policy")
    if isinstance(ref, dict):
        ref.setdefault("pinned", False)
    view["services"] = _services(document)
    gates = view.get("gates")
    if isinstance(gates, dict):
        gates.setdefault("advisory", None)
    groups: list[dict[str, Any]] = []
    for key, title, paths in GROUPS:
        fields: list[dict[str, Any]] = []
        for root in paths:
            for path, value in _flatten(_get(view, root), root):
                fields.append(
                    {
                        "path": path,
                        "label": _label(path),
                        "kind": _input_kind(path, value),
                        "value": value,
                        "shown": _shown(value),
                    }
                )
        groups.append({"key": key, "title": title, "fields": fields})
    return groups


def _parse(field: Mapping[str, Any], form: Mapping[str, str]) -> Any:
    name = FIELD_PREFIX + field["path"]
    if field["kind"] == "checkbox":
        return form.get(name) == "true"
    raw = form.get(name, "").strip()
    original = field["value"]
    if field["kind"] == "number":
        try:
            return int(raw)
        except ValueError:
            raise ContractValidationError(
                f"{field['path']} must be a whole number",
                errors=[{"path": field["path"], "message": "a whole number"}],
            ) from None
    if field["kind"] == "list":
        if not raw and original is None:
            return None
        items = [item.strip() for item in raw.split(",") if item.strip()]
        if original and all(isinstance(item, int) for item in original):
            try:
                return [int(item) for item in items]
            except ValueError:
                raise ContractValidationError(
                    f"{field['path']} must be whole numbers, comma separated",
                    errors=[{"path": field["path"], "message": "whole numbers"}],
                ) from None
        return items
    if isinstance(original, float):
        try:
            return float(raw)
        except ValueError:
            raise ContractValidationError(
                f"{field['path']} must be a number",
                errors=[{"path": field["path"], "message": "a number"}],
            ) from None
    if original is None and not raw:
        return None
    return raw


def apply_form(document: Mapping[str, Any], form: Mapping[str, str]) -> dict[str, Any]:
    """`document` with every group field set from the submitted form. A field the form
    leaves out keeps nothing of its old value: an unticked box is false, as browsers
    send it."""
    edited = copy.deepcopy(dict(document))
    services: dict[str, dict[str, Any]] = {}
    for group in group_fields(document):
        for field in group["fields"]:
            value = _parse(field, form)
            path = field["path"]
            if path.startswith("services."):
                _, kind, rest = path.split(".", 2)
                _set(services.setdefault(kind, {}), rest, value)
            else:
                _set(edited, path, value)
    declared = [
        {"kind": kind, **{k: v for k, v in values.items() if k != "declared"}}
        for kind, values in services.items()
        if values.get("declared") is True
    ]
    if declared or "services" in edited:
        edited["services"] = declared
    return edited


def document_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each dotted path whose value differs between two policy documents, with both
    values; the version number itself is left out."""
    old, new = dict(_flatten(before, "")), dict(_flatten(after, ""))
    return [
        {"path": path, "before": old.get(path), "after": new.get(path)}
        for path in sorted(set(old) | set(new))
        if path != "version" and old.get(path) != new.get(path)
    ]


def _publications(uow: UnitOfWork, name: str) -> dict[int, Any]:
    """The newest upload event of each version of policy `name`, by version."""
    found: dict[int, Any] = {}
    after = 0
    while True:
        batch = uow.events.list_global(
            after_seq=after, kind=EventKind.POLICY_UPLOADED.value, since=None, limit=500
        )
        for event in batch:
            ref = event.payload.get("policy") or {}
            if ref.get("name") == name:
                found[int(ref.get("version", 0))] = event
        if len(batch) < 500:
            break
        after = int(batch[-1].seq or 0)
    return found


def in_force(versions: list[Policy]) -> Policy | None:
    """The newest version not retired: the one new tasks are admitted against."""
    live = [item for item in versions if item.retired_at is None]
    return max(live, key=lambda item: item.version) if live else None


def policies_view(uow: UnitOfWork, name: str | None, version: int | None) -> dict[str, Any]:
    """Every policy name with its version in force; for `name` (default the first), its
    versions with who published each and why, the version in force in groups, and the
    selected version (default the one in force) against the version before it."""
    names = list(uow.policies.list_names())
    summary = []
    for item in names:
        versions = list(uow.policies.list_versions(item))
        current = in_force(versions)
        summary.append(
            {
                "name": item,
                "versions": len(versions),
                "in_force": current.version if current is not None else None,
            }
        )
    selected = name if name in names else (names[0] if names else None)
    view: dict[str, Any] = {"policies": summary, "name": selected}
    if selected is None:
        return view
    versions = sorted(uow.policies.list_versions(selected), key=lambda item: item.version)
    current = in_force(versions)
    events = _publications(uow, selected)
    view["versions"] = [
        {
            "version": item.version,
            "created_at": item.created_at.isoformat(),
            "retired": item.retired_at is not None,
            "in_force": current is not None and item.version == current.version,
            "published_by": events[item.version].principal if item.version in events else None,
            "reason": (
                events[item.version].payload.get("reason") if item.version in events else None
            )
            or None,
        }
        for item in reversed(versions)
    ]
    view["current"] = (
        {"version": current.version, "groups": group_fields(current.document)}
        if current is not None
        else None
    )
    shown = next((item for item in versions if item.version == version), None) or current
    if shown is not None:
        earlier = [item for item in versions if item.version < shown.version]
        previous = earlier[-1] if earlier else None
        view["diff"] = {
            "version": shown.version,
            "previous": previous.version if previous is not None else None,
            "changes": document_diff(previous.document, shown.document)
            if previous is not None
            else [],
        }
    return view


def publish_policy(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    name: str,
    form: Mapping[str, str],
    reason: str | None,
    concurrency_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Publish the edited groups as the next version of policy `name`, built on the
    version in force. The reason is required; the upload event records it with the
    operator, as PUT /v1/policies/{name}/{version} records an admin's upload."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="policy publish", reason_required=True
    )
    versions = list(uow.policies.list_versions(name))
    current = in_force(versions)
    if current is None:
        raise NotFoundError(f"policy {name} has no version in force")
    document = apply_form(current.document, form)
    version = max(item.version for item in versions) + 1
    document["version"] = version
    changes = document_diff(current.document, document)
    if not changes:
        raise ContractValidationError(
            "nothing was changed, so no version was published",
            errors=[{"path": "document", "message": "no setting changed"}],
        )
    put_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=name,
        version=version,
        document=document,
        reason=reason,
        concurrency_modes=concurrency_modes,
    )
    return {
        "name": name,
        "version": version,
        "previous": current.version,
        "published_by": principal.name,
        "reason": reason,
        "changes": changes,
    }
