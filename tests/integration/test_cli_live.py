"""`crucible` against the real API on a real port: the orchestrator verbs and the admin
group's remote mode, with a token of each role, so the envelope, the role the API's own
guards show, and every `next` action are checked against the server rather than a fake.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.admin.context import AdminContext
from crucible.cli.main import run
from crucible.client import next as nx
from tests.fixtures import contract_document
from tests.integration.test_admin import seed_credentials
from tests.wait import wait_until

pytestmark = pytest.mark.integration


@pytest.fixture
def admin_ctx(ctx: AppContext, provider: FakeProvider, tmp_path: Path) -> AdminContext:
    """The administrative surface over the tier's database, with shape-valid credentials."""
    assert ctx.harnesses is not None
    root = tmp_path / "credentials"
    root.mkdir()
    ctx.admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
        credential_sources=seed_credentials(root),
        artifact_root=str(tmp_path / "artifacts"),
        lease_ttl_seconds=30,
    )
    return ctx.admin


@pytest.fixture
def api_url(ctx: AppContext, admin_ctx: AdminContext) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_config=None)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until(lambda: server.started, timeout=20, describe="the API server to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Cli:
    """`crucible` in process, as the principal whose token is in the environment."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        url: str,
        tokens: dict[str, str],
        tmp_path: Path,
    ) -> None:
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.tokens = tokens
        monkeypatch.setenv("CRUCIBLE_URL", url)
        monkeypatch.setenv("CRUCIBLE_CLIENT_CONFIG", str(tmp_path / "no-client.toml"))
        monkeypatch.delenv("CRUCIBLE_ADMIN_TOKEN", raising=False)
        self.examples = os.environ.get("CRUCIBLE_CLI_EXAMPLES")

    def __call__(self, role: str, *argv: str, code: int = 0) -> dict[str, Any]:
        self.monkeypatch.setenv("CRUCIBLE_TOKEN", self.tokens[role])
        assert run(list(argv)) == code
        captured = self.capsys.readouterr()
        for token in self.tokens.values():
            assert token not in captured.out + captured.err
        lines = captured.out.strip().splitlines()
        assert len(lines) == 1, captured.out
        document: dict[str, Any] = json.loads(lines[0])
        if self.examples:
            words = [a for a in argv if not a.startswith(("-", "/")) and "://" not in a]
            name = "-".join([role, *words][:4])
            Path(self.examples, f"{name}.json").write_text(json.dumps(document, indent=2) + "\n")
        return document


@pytest.fixture
def cli(
    api_url: str,
    tokens: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> Cli:
    return Cli(monkeypatch, capsys, api_url, tokens, tmp_path)


def _fill(entry: dict[str, Any], **values: str) -> list[str]:
    """A `next` action's argv with its `{name}` tokens filled."""
    return [re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], arg) for arg in entry["command"]]


def _submit(cli: Cli, tmp_path: Path) -> dict[str, Any]:
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(contract_document()), encoding="utf-8")
    return cli("orchestrator", "submit", str(contract), "--reason", "live client test")


def test_an_orchestrator_follows_next_from_submit_to_start(cli: Cli, tmp_path: Path) -> None:
    submitted = _submit(cli, tmp_path)
    assert submitted["ok"] is True and submitted["kind"] == "task"
    assert submitted["state"] == "submitted" == submitted["data"]["state"]
    assert submitted["principal_role"] == "orchestrator"
    actions = {entry["action"]: entry for entry in submitted["next"]}
    assert set(actions) == {"start", "cancel"}
    assert actions["cancel"]["requires"]["owner"] == "orchestrator-principal"

    # The offered command, run as offered, is one the API takes.
    argv = _fill(actions["start"], reason="the live test follows next")
    started = cli("orchestrator", *argv[1:])
    assert started["state"] == "scheduled"
    assert [entry["action"] for entry in started["next"]] == ["cancel"]

    shown = cli("orchestrator", "task", submitted["data"]["id"])
    assert shown["data"]["id"] == submitted["data"]["id"] and shown["state"] == "scheduled"
    listed = cli("orchestrator", "tasks")
    assert listed["kind"] == "task_list" and len(listed["data"]["items"]) == 1


@pytest.mark.parametrize(
    ("role", "shown_role", "actions"),
    [
        ("admin", "admin", {"start", "cancel"}),
        ("operator", "orchestrator", {"start", "cancel"}),
        ("observer", "observer", set()),
    ],
)
def test_the_token_decides_the_role_and_next(
    cli: Cli, tmp_path: Path, role: str, shown_role: str, actions: set[str]
) -> None:
    task_id = _submit(cli, tmp_path)["data"]["id"]
    shown = cli(role, "task", task_id)
    assert shown["principal_role"] == shown_role
    assert {entry["action"] for entry in shown["next"]} == actions


def test_an_action_for_the_wrong_principal_comes_back_as_the_apis_refusal(
    cli: Cli, tmp_path: Path
) -> None:
    task_id = _submit(cli, tmp_path)["data"]["id"]
    refused = cli("observer", "cancel", task_id, "--verbatim", "stop", "--reason", "r", code=1)
    assert refused["ok"] is False and refused["error"]["code"] == "forbidden"
    assert refused["error"]["status"] == 403
    assert refused["error"]["problem"]["type"] == "urn:crucible:problem:forbidden"
    wrong_state = cli("orchestrator", "republish", task_id, "--reason", "r", code=1)
    assert wrong_state["error"]["code"] == "transition-not-allowed"
    assert "publish_failed" in wrong_state["error"]["message"]


def test_the_admin_group_in_remote_mode(cli: Cli, api_url: str) -> None:
    base = ["admin", "--api-url", api_url]
    status = cli("admin", *base, "status")
    assert status["ok"] is True and status["kind"] == "admin_status"
    assert status["principal_role"] == "admin"
    report = cli("admin", *base, "credentials", "status", "--harness", "codex")
    # The harness rows outlive the tier's truncation, so an earlier test's recorded auth
    # failure can leave codex `invalid`; remote mode offers login either way, since the
    # remote admin API runs it in a promoted worker image.
    assert report["kind"] == "credential_state" and report["state"] in ("configured", "invalid")
    expected = set(nx.CREDENTIAL_ACTIONS[report["state"]])
    assert {e["action"] for e in report["next"]} == expected
    assert all(e["command"][:4] == ["crucible", *base] for e in report["next"])
    refused = cli("orchestrator", *base, "status", code=1)
    assert refused["error"]["code"] == "forbidden"
    assert refused["error"]["message"] == "admin role required"


def test_health_needs_no_role(cli: Cli) -> None:
    health = cli("observer", "health")
    assert health["kind"] == "health" and health["data"]["status"] == "ok"
    assert health["next"] == []
