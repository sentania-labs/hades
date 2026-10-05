"""Issue 128 through the supervisor: the launch carries the resolved per-command
timeout, submission holds a contract's value to the policy's bounds, and a harness
that exits with a command it was waiting on cut off is recorded `incomplete`, not
completed. Issue 153: a background process the worker chose to leave running is not."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.harness.base import TRANSCRIPT_NAME
from crucible.ports.execution import CollectedOutputs, Handle, LaunchSpec, Workspace
from crucible.ports.github import InstallationToken
from tests.fixtures import contract_document
from tests.integration.conftest import make_supervisor, run_to_settled, submit_and_start

pytestmark = pytest.mark.integration

# The trap (issue 128): Claude Code moved a Bash call to the background on its own and
# the exit killed it after the final result.
CUT_OFF_TRANSCRIPT: list[dict[str, Any]] = [
    {
        "type": "assistant",
        "message": {
            "id": "m1",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
        },
    },
    {
        "type": "system",
        "subtype": "task_started",
        "task_id": "b1",
        "tool_use_id": "t1",
        "is_backgrounded": True,
    },
    {"type": "result", "subtype": "success"},
    {"type": "system", "subtype": "task_updated", "task_id": "b1", "patch": {"status": "killed"}},
]
# Issue 153: what a worker leaves running on purpose. A Claude Code task the model asked
# for with `run_in_background`, and a Codex unified-exec session never completed (both
# transcript lines, so either harness reads its own).
BACKGROUND_TRANSCRIPT: list[dict[str, Any]] = [
    {
        "type": "assistant",
        "message": {
            "id": "m1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "make serve", "run_in_background": True},
                }
            ],
        },
    },
    {
        "type": "system",
        "subtype": "task_started",
        "task_id": "b1",
        "tool_use_id": "t1",
        "is_backgrounded": True,
    },
    {"type": "item.started", "item": {"id": "item_1", "type": "command_execution"}},
    {"type": "result", "subtype": "success"},
]
# The default route picks Claude Code. A contract that names another harness pins it,
# which only an operator may submit.
PINNED_CODEX = {
    "harness": "codex",
    "model": "gpt-5.6-luna",
    "pin_reason": "the transcript under test is Codex's",
}


@pytest.fixture
def operator(ctx: AppContext, tokens: dict[str, str]) -> Iterator[TestClient]:
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['operator']}"}
    ) as c:
        yield c


class InFlightProvider(FakeProvider):
    """A fake whose workspaces are real directories, so the adapter reads the report."""

    def __init__(
        self,
        root: Path,
        only_image: str | None = None,
        transcript: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.root = root
        # When set, only attempts launched from this image leave the transcript.
        self.only_image = only_image
        self.transcript = list(CUT_OFF_TRANSCRIPT if transcript is None else transcript)

    async def prepare(
        self,
        spec: LaunchSpec,
        checkout_token: InstallationToken | None = None,
        cancelled: Any = None,
    ) -> Workspace:
        base = self.root / spec.attempt_id
        (base / "repo").mkdir(parents=True)
        ws = Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=str(base / "repo"),
            identity_path=str(base / "identity"),
            report_path=str(base / "report"),
        )
        self._workspaces[spec.attempt_id] = ws
        return ws

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs:
        if self.only_image is not None and (spec is None or spec.image != self.only_image):
            return await super().collect(h, ws, spec)
        report = Path(ws.checkout_path).parent / "output" / "report"
        report.mkdir(parents=True, exist_ok=True)
        (report / TRANSCRIPT_NAME).write_text(
            "\n".join(json.dumps(line) for line in self.transcript) + "\n",
            encoding="utf-8",
        )
        return await super().collect(h, ws, spec)


def _attempts(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    view = client.get(f"/v1/tasks/{task_id}").json()
    return [a for e in view["executions"] for a in e["attempts"]]


async def test_the_launch_carries_the_contracts_command_timeout(
    client: TestClient, ctx: AppContext, provider: FakeProvider
) -> None:
    supervisor = make_supervisor(ctx, provider)
    request = {**contract_document()["execution_request"], "command_timeout_ms": 600_000}
    narrowed = submit_and_start(
        client, "crucible-worker:fake-succeed", "EX-0128A", execution_request=request
    )
    default = submit_and_start(client, "crucible-worker:fake-succeed", "EX-0128B")
    await run_to_settled(supervisor, client, narrowed)
    await run_to_settled(supervisor, client, default)
    specs = {
        worker.spec.external_id: worker.spec
        for attempt in (*_attempts(client, narrowed), *_attempts(client, default))
        if (worker := provider.worker(str(attempt["id"]))) is not None
    }
    assert specs["EX-0128A"].command_timeout_ms == 600_000
    # The policy predates the field: the default of 60 minutes, within the 3600 s attempt.
    assert specs["EX-0128B"].command_timeout_ms == 3_600_000


def test_submission_holds_the_command_timeout_to_the_policy_bounds(client: TestClient) -> None:
    doc = contract_document(external_id="EX-0128C")
    doc["repository"]["work_branch"] = "crucible/EX-0128C"
    doc["execution_request"]["command_timeout_ms"] = 500
    refused = client.post("/v1/tasks", json=doc)
    assert refused.status_code == 422
    paths = {e["path"] for e in refused.json()["errors"]}
    assert "execution_request.command_timeout_ms" in paths
    doc["execution_request"]["command_timeout_ms"] = 3_600_001
    over = client.post("/v1/tasks", json=doc)
    assert over.status_code == 422
    assert "must not exceed timeout_seconds" in over.text


async def test_a_harness_that_exits_with_a_command_cut_off_is_incomplete(
    client: TestClient, ctx: AppContext, tmp_path: Path
) -> None:
    provider = InFlightProvider(tmp_path)
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-0128D")
    await run_to_settled(supervisor, client, task_id)
    (attempt,) = _attempts(client, task_id)
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["harness"] == "claude_code"
    assert attempt["exit_class"] == "incomplete"
    assert attempt["state"] == "failed"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    collected = next(e for e in events if e["kind"] == "attempt_collected")
    assert collected["payload"]["work_in_flight"]
    # The report is valid, yet exit_clean must not pass on the zero exit code alone.
    gates = client.get(f"/v1/attempts/{attempt['id']}/gates").json()["items"]
    assert all(g["result"] != "pass" for g in gates if g["gate"] == "exit_clean")


@pytest.mark.parametrize("request_overrides", [{}, PINNED_CODEX], ids=["claude_code", "codex"])
async def test_a_background_process_left_running_is_a_completion(
    operator: TestClient, ctx: AppContext, tmp_path: Path, request_overrides: dict[str, str]
) -> None:
    """Issue 153: a process the worker left running dies with the sandbox. The attempt
    completes and the collection records no warning about it."""
    client = operator
    provider = InFlightProvider(tmp_path, transcript=BACKGROUND_TRANSCRIPT)
    supervisor = make_supervisor(ctx, provider)
    request = {**contract_document()["execution_request"], **request_overrides}
    task_id = submit_and_start(
        client, "crucible-worker:fake-succeed", "EX-0153A", execution_request=request
    )
    await run_to_settled(supervisor, client, task_id)
    (attempt,) = _attempts(client, task_id)
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["harness"] == request_overrides.get("harness", "claude_code")
    assert attempt["exit_class"] == "completed"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    collected = next(e for e in events if e["kind"] == "attempt_collected")
    assert "work_in_flight" not in collected["payload"]
    gates = client.get(f"/v1/attempts/{attempt['id']}/gates").json()["items"]
    assert [g["result"] for g in gates if g["gate"] == "exit_clean"] == ["pass"]
