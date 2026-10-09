"""Unit tests for the catalog loader, API endpoint, and admin UI page.

These tests must fail on the unchanged tree (before the catalog files
are created) so that Hades can confirm they exist before the start of
the task (hades #354, FDY-0588 probes).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crucible.adapters.catalog.loader import (
    _validate_skill,
    _validate_tool,
    load_catalog,
)
from crucible.adapters.ui.pages import catalog as catalog_page_module
from crucible.application.admin.catalog import view


def _seed_catalog(path: Path) -> Path:
    """Write a valid seed catalog to *path* and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """skills:
  - name: example_analyzer
    summary: Analyze a text file.
    owner: platform
    instructions_path: docs/skills/example-analyzer.md
  - name: example_summarizer
    summary: Summarize a document.
    owner: platform
    instructions_path: docs/skills/example-summarizer.md
tools:
  - name: claude_code
    kind: cli
    command: claude
    credential_ref: claude_code_credential
    allowed_for: [operator, admin]
  - name: codex
    kind: cli
    command: codex
    credential_ref: codex_credential
    allowed_for: [operator, admin]
  - name: agy
    kind: cli
    command: agy
    credential_ref: agy_credential
    allowed_for: [operator, admin]
  - name: hermes
    kind: cli
    command: hermes
    credential_ref: hermes_credential
    allowed_for: [operator, admin]
  - name: hades_http_client
    kind: mcp_server
    endpoint: https://hades-mcp.internal:8080
    credential_ref: hades_mcp_credential
    allowed_for: [operator, admin, observer]
""",
        encoding="utf-8",
    )
    return path


def _secret_value() -> str:
    """Return a value that matches a secret pattern (sk-ant-*)."""
    return "sk-ant-api03-abc123def456ghi789jkl012mno345pqr678stuv012wx"


class TestValidateSkill:
    def test_valid_skill(self) -> None:
        errors = _validate_skill(
            {
                "name": "my_skill",
                "summary": "Does things",
                "owner": "team",
                "instructions_path": "path.md",
            }
        )
        assert errors == []

    def test_missing_fields(self) -> None:
        errors = _validate_skill({"name": "x"})
        assert len(errors) == 3
        for err in errors:
            assert err.message == "required field missing"

    def test_string_fields_must_be_strings(self) -> None:
        errors = _validate_skill(
            {"name": 123, "summary": "ok", "owner": "ok", "instructions_path": "ok.md"}
        )
        assert any(e.path == "name" for e in errors)


class TestValidateTool:
    def test_valid_cli_tool(self) -> None:
        errors = _validate_tool(
            {
                "name": "my_tool",
                "kind": "cli",
                "command": "mycli",
                "credential_ref": "my_cred",
            }
        )
        assert errors == []

    def test_valid_mcp_server(self) -> None:
        errors = _validate_tool(
            {
                "name": "mcp",
                "kind": "mcp_server",
                "endpoint": "https://mcp.internal:8080",
                "credential_ref": "mcp_cred",
            }
        )
        assert errors == []

    def test_unknown_kind_refused(self) -> None:
        errors = _validate_tool(
            {
                "name": "bad",
                "kind": "wizard",
                "command": "wizard",
                "credential_ref": "wiz_cred",
            }
        )
        assert any("wizard" in e.message and "not a valid kind" in e.message for e in errors)

    def test_missing_required_kind(self) -> None:
        errors = _validate_tool({"name": "x", "credential_ref": "y"})
        assert any(e.path == "kind" for e in errors)

    def test_mcp_server_requires_endpoint(self) -> None:
        errors = _validate_tool(
            {
                "name": "mcp",
                "kind": "mcp_server",
                "credential_ref": "c",
            }
        )
        assert any(e.path == "endpoint" for e in errors)

    def test_cli_requires_command(self) -> None:
        errors = _validate_tool(
            {
                "name": "cli",
                "kind": "cli",
                "credential_ref": "c",
            }
        )
        assert any(e.path == "command" for e in errors)

    def test_secret_credential_ref_refused(self) -> None:
        errors = _validate_tool(
            {
                "name": "x",
                "kind": "cli",
                "command": "x",
                "credential_ref": _secret_value(),
            }
        )
        assert len(errors) > 0
        assert any("refused" in e.message.lower() for e in errors)

    def test_whitespace_credential_ref_refused(self) -> None:
        errors = _validate_tool(
            {
                "name": "x",
                "kind": "cli",
                "command": "x",
                "credential_ref": "my cred",
            }
        )
        assert any("spaces" in e.message or "whitespace" in e.message for e in errors)

    def test_credential_ref_colon_refused(self) -> None:
        errors = _validate_tool(
            {
                "name": "x",
                "kind": "cli",
                "command": "x",
                "credential_ref": "foo:bar",
            }
        )
        assert any("colons" in e.message or "colon" in e.message for e in errors)

    def test_mcp_server_rejects_command(self) -> None:
        errors = _validate_tool(
            {
                "name": "mcp",
                "kind": "mcp_server",
                "endpoint": "https://mcp.internal:8080",
                "credential_ref": "c",
                "command": "x",
            }
        )
        assert any("must not be set when kind is mcp_server" in e.message for e in errors)

    def test_cli_rejects_endpoint(self) -> None:
        errors = _validate_tool(
            {
                "name": "cli",
                "kind": "cli",
                "command": "x",
                "credential_ref": "c",
                "endpoint": "https://mcp.internal:8080",
            }
        )
        assert any("must not be set when kind is cli" in e.message for e in errors)


class TestLoadCatalog:
    def test_load_seed_catalog(self, tmp_path: Path) -> None:
        p = _seed_catalog(tmp_path / "catalog.yaml")
        catalog = load_catalog(p)
        assert len(catalog.skills) == 2
        assert len(catalog.tools) == 5
        assert catalog.errors == []
        names = [t.name for t in catalog.tools]
        assert "claude_code" in names
        assert "codex" in names
        assert "agy" in names
        assert "hermes" in names
        assert "hades_http_client" in names

    def test_load_secret_credential_refused(self, tmp_path: Path) -> None:
        secret = _secret_value()
        tmp_path.mkdir(parents=True, exist_ok=True)
        p = tmp_path / "catalog.yaml"
        p.write_text(
            f"""skills: []
tools:
  - name: bad_tool
    kind: cli
    command: bad
    credential_ref: {secret}
""",
            encoding="utf-8",
        )
        catalog = load_catalog(p)
        assert len(catalog.errors) > 0
        assert all("refused" in e.message.lower() for e in catalog.errors)

    def test_load_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "catalog.yaml"
        p.write_text("", encoding="utf-8")
        catalog = load_catalog(p)
        assert len(catalog.skills) == 0
        assert len(catalog.tools) == 0
        assert len(catalog.errors) > 0


class TestApplicationView:
    def test_view_returns_struct(self, tmp_path: Path) -> None:
        p = _seed_catalog(tmp_path / "catalog.yaml")
        data = view(p)
        assert "skills" in data
        assert "tools" in data
        assert len(data["skills"]) == 2
        assert len(data["tools"]) == 5
        # Every skill has used_by
        for s in data["skills"]:
            assert "used_by" in s
            assert s["used_by"] == 0
        for t in data["tools"]:
            assert "used_by" in t
            assert t["used_by"] == 0
            assert "credential_ref" in t

    def test_mcp_tool_has_endpoint(self, tmp_path: Path) -> None:
        p = _seed_catalog(tmp_path / "catalog.yaml")
        data = view(p)
        for t in data["tools"]:
            if t["name"] == "hades_http_client":
                assert t["kind"] == "mcp_server"
                assert t["endpoint"] == "https://hades-mcp.internal:8080"
                assert "command" not in t

    def test_cli_tool_has_command(self, tmp_path: Path) -> None:
        p = _seed_catalog(tmp_path / "catalog.yaml")
        data = view(p)
        for t in data["tools"]:
            if t["name"] == "claude_code":
                assert t["kind"] == "cli"
                assert t["command"] == "claude"
                assert "endpoint" not in t


class TestAdminCatalogPage:
    def _make_request(self, path: str) -> Any:
        return SimpleNamespace(
            scope={"type": "http", "method": "GET", "path": path, "headers": []},
            query_params={},
            app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace())),
        )

    def test_catalog_page_renders(self, monkeypatch: Any, tmp_path: Path) -> None:
        _seed_catalog(tmp_path / "catalog.yaml")
        principal = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
        monkeypatch.setattr(catalog_page_module, "_require", lambda *_: (principal, "csrf"))
        uow_mock = SimpleNamespace()
        ctx: Any = SimpleNamespace()

        req = self._make_request("/ui/catalog")
        response = catalog_page_module.catalog_page(req, ctx, uow_mock)
        body = bytes(response.body).decode()

        assert "Catalog" in body
        assert "Skills" in body
        assert "Tools" in body
        assert "example_analyzer" in body
        assert "claude_code" in body
        assert "hades_http_client" in body
        assert "read-only" in body.lower()

    def test_catalog_page_shows_all_harnesses(self, monkeypatch: Any, tmp_path: Path) -> None:
        _seed_catalog(tmp_path / "catalog.yaml")
        principal = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
        monkeypatch.setattr(catalog_page_module, "_require", lambda *_: (principal, "csrf"))
        uow_mock = SimpleNamespace()
        ctx: Any = SimpleNamespace()

        req = self._make_request("/ui/catalog")
        response = catalog_page_module.catalog_page(req, ctx, uow_mock)
        body = bytes(response.body).decode()

        for harness in ("claude_code", "codex", "agy", "hermes"):
            assert harness in body
