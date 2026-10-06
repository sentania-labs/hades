"""Repositories page: stored policy names as a select (hades #144)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.ui.pages import repositories as ui_repositories
from crucible.domain.entities import (
    Role,
)


def test_repository_form_offers_stored_policy_names_as_a_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hades #144: the Repositories register form renders policy_name as a select of
    stored policy names defaulting to ``default-software``."""

    sections_captured: list[dict[str, Any]] = []

    def _fake_require(*args: Any, **kwargs: Any) -> tuple[Any, str]:
        principal = SimpleNamespace(name="admin", role=Role.ADMIN)
        return principal, "fixture-csrf"

    monkeypatch.setattr(ui_repositories, "_require", _fake_require)

    def _fake_page(*args: Any, sections: list[dict[str, Any]], **kwargs: Any) -> Any:
        sections_captured.extend(sections)
        return None

    monkeypatch.setattr(ui_repositories, "_page", _fake_page)

    repo_ns = SimpleNamespace(
        name="example/repo",
        url="https://github.com/example/repo",
        default_branch="main",
        policy_name="default-software",
        installation_id=42,
        private=False,
        external_review_attested=False,
    )

    uow = SimpleNamespace(
        repositories=SimpleNamespace(list_all=lambda: [repo_ns]),
        policies=SimpleNamespace(list_names=lambda: ["audit", "default-software", "restricted"]),
    )

    ctx = SimpleNamespace(admin=object())

    ui_repositories.repositories_page(
        Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/ui/repositories",
                "headers": [],
                "query_string": b"",
            }
        ),
        cast(Any, ctx),
        cast(Any, uow),
    )

    # Find the "Register or update" section
    for section in sections_captured:
        if section.get("title") == "Register or update":
            break
    else:
        raise AssertionError("Register or update section not found")

    fields = section["form"]["fields"]

    # Find the policy_name field
    policy_field = None
    for f in fields:
        if f.get("name") == "policy_name":
            policy_field = f
            break

    assert policy_field is not None, "policy_name field not found"
    assert policy_field["kind"] == "select"
    assert policy_field["value"] == "default-software"
    assert policy_field["options"] == [
        ("audit", "audit"),
        ("default-software", "default-software"),
        ("restricted", "restricted"),
    ]
