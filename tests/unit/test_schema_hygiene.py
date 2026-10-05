"""No table ever holds a token, key, or secret (14): column names are checked."""

from __future__ import annotations

import re

from crucible.adapters.persistence.models import Base
from crucible.domain.events import EventKind

SECRET_NAME = re.compile(r"(secret|password|passwd|private_key|api_key|credential)|(^|_)token$")
# The fenced token is a monotonic counter on the lease row (10, 14), not a credential.
ALLOWED = {"leases.fenced_token"}


def test_no_secret_bearing_column_names() -> None:
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if SECRET_NAME.search(column.name) and f"{table.name}.{column.name}" not in ALLOWED
    ]
    assert offenders == []


def test_migration_event_kinds_match_enum() -> None:
    from crucible.adapters.persistence.migrations.versions import (  # noqa: PLC0415
        _0043_credential_mount_mode as m,
    )

    # The latest migration owns the current CHECK constraint (10).
    assert set(m._event_kinds()) == {k.value for k in EventKind}


def test_openapi_generates_from_the_pydantic_models() -> None:
    """04: OpenAPI is generated from crucible/contracts and published at /v1/openapi.json."""
    from crucible.adapters.api.app import create_app  # noqa: PLC0415
    from crucible.adapters.api.deps import AppContext  # noqa: PLC0415

    # The document is built from the route signatures alone; nothing here is called.
    context = AppContext(
        uow_factory=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        providers=[],
        database_url="",
        engine=None,  # type: ignore[arg-type]
        artifact_store=None,  # type: ignore[arg-type]
    )
    spec = create_app(context).openapi()
    paths = set(spec["paths"])
    assert {
        "/v1/tasks/{task_id}/review",
        "/v1/tasks/{task_id}/accept",
        "/v1/tasks/{task_id}/republish",
        "/v1/tasks/{task_id}/corrections",
        "/v1/attempts/{attempt_id}/gates",
        "/v1/attempts/{attempt_id}/logs",
        "/v1/wakes",
        "/v1/policies/{name}/{version}",
        "/v1/routing/usage",
        "/v1/tasks/{task_id}/pull-request",
        "/v1/tasks/{task_id}/ci-decision",
        "/v1/tasks/{task_id}/head-decision",
        "/v1/github/webhook",
    } <= paths
    assert all(p.startswith("/v1/") for p in paths)
