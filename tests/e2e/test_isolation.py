"""S4's completion condition, re-run on the real arrangement (18, 21).

The S4 probes ran against an empty environment: no control plane, no credential
mounted, no socket proxy. This runs the same list with PostgreSQL up, the socket
proxy up, the egress proxy filtering, a credential directory mounted for the
worker's own harness, and the other harnesses' credential directories present on
the host. Every probe must be refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.application.supervisor import Supervisor
from tests.e2e.conftest import (
    OriginFactory,
    e2e_contract,
    register,
    run_until,
    submit_and_start,
)

pytestmark = [
    pytest.mark.e2e,
    # Real containers, and the first case also pays for the session's stack; the waits
    # inside allow up to four minutes (issue 192).
    pytest.mark.timeout(600),
]

MUST_BE_REFUSED = (
    "docker-socket-unix",
    "docker-socket-run",
    "socket-proxy",
    "socket-proxy-ip",
    "database",
    "database-gateway",
    "crucible-api",
    "other-credential-codex",
    "other-credential-claude",
    "other-credential-agy",
    "other-credential-root",
    "git-push",
    "egress-not-allowlisted",
    "egress-direct-by-ip",
    "egress-direct-by-name",
    "write-root",
    "write-identity",
    "write-usr",
    "chown-report",
    "mknod",
)


async def test_a_worker_reaches_nothing_it_must_not(
    ctx: AppContext,
    client: TestClient,
    supervisor: Supervisor,
    origin: OriginFactory,
    artifact_root: Path,
    worker_image: str,
) -> None:
    # The other harnesses' credential directories exist on the host; the worker's own
    # harness has one too. Built at runtime: no secret-shaped fixture is ever committed.
    credentials = artifact_root / "credentials"
    for harness in ("claude_code", "codex", "agy", "script-harness"):
        directory = credentials / harness
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
        marker = directory / "auth.json"
        marker.write_text('{"placeholder": "' + "x" * 32 + '"}\n', encoding="utf-8")
        marker.chmod(0o644)

    url = origin("isolation", "isolation")
    register(ctx, "isolation", url)
    task_id = submit_and_start(client, e2e_contract("E2E-0003", "isolation", worker_image))
    await run_until(
        supervisor,
        client,
        task_id,
        {"accepted", "gates_passed", "pre_pr_gates_failed"},
    )

    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    artifacts = client.get(f"/v1/attempts/{attempt_id}/artifacts").json()["items"]
    probe_artifact = next(a for a in artifacts if a["filename"].endswith("isolation.tsv"))
    body = client.get(f"/v1/artifacts/{probe_artifact['id']}/content").text
    results = dict(line.split("\t", 1) for line in body.splitlines() if "\t" in line)
    assert set(MUST_BE_REFUSED) <= set(results), sorted(results)
    reached = sorted(name for name, outcome in results.items() if outcome != "refused")
    assert reached == [], f"a worker reached something it must not: {reached}"
