"""What the publisher container is, and what it can never be (12, 23, S10).

Two properties that would be a serious defect to lose and that nothing else asserts: the
token is not in anything the daemon records about the container, and the push cannot be
a force push. Both are checked against the real objects the provider sends and the real
script text it runs, not against a description of them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.publisher import DockerPublisher, PublisherConfig
from crucible.ports.github import InstallationToken
from crucible.ports.publish import MergeMainRequest, PublishRequest
from tests.integration.fake_github import installation_token_value

HEAD = "a" * 40


def _request(**kw: Any) -> PublishRequest:
    base: dict[str, Any] = {
        "attempt_id": "01ATTEMPT",
        "task_id": "01TASK",
        "owner": "FDY-0042",
        "repository_url": "https://github.com/owner/repo.git",
        "work_branch": "crucible/FDY-0042",
        "base_ref": "main",
        "expected_head": HEAD,
        "bundle_path": "/var/lib/crucible/artifacts/workspaces/01ATTEMPT/output/work_branch.bundle",
        "bundle_sha256": "b" * 64,
        "image": "crucible-worker:script-harness-1.0.0",
        "policy": {"resources": {"cpus": 1, "memory": "512m", "pids": 128}},
    }
    base.update(kw)
    return PublishRequest(**base)


@pytest.fixture
def publisher() -> DockerPublisher:
    provider = DockerProvider(
        DockerConfig(endpoint="tcp://127.0.0.1:1", artifact_root="/var/lib/crucible/artifacts")
    )
    return DockerPublisher(provider, PublisherConfig())


def test_the_create_body_carries_no_token_anywhere_the_daemon_records(
    publisher: DockerPublisher,
) -> None:
    """S10's absence proof, at the one place Crucible controls: the create request.

    `docker inspect` shows `Config.Env`, `Cmd`, and every mount. If the token were in any
    of them it would be on disk in the daemon's own state for the life of the container.
    """
    value = installation_token_value()
    request = _request()
    body = publisher._body(
        request,
        script=scripts.publisher_script(
            clone_url=request.repository_url,
            work_branch=request.work_branch,
            base_ref=request.base_ref,
            expected_head=request.expected_head,
            author_name="crucible-worker",
            author_email="crucible-worker@users.noreply.github.com",
        ),
        env={"HOME": "/home/worker"},
    )
    assert value not in json.dumps(body)
    assert "ghs_" not in json.dumps(body)
    for entry in body["Env"]:
        assert "token" not in str(entry).lower() or str(entry).lower().startswith(
            "crucible_token_file="
        )
    for mount in body["HostConfig"]["Mounts"]:
        assert "token" not in json.dumps(mount).lower()
    # The one way in is stdin, which is why these three have to be set.
    assert body["OpenStdin"] and body["StdinOnce"] and body["AttachStdin"]
    # And the tmpfs it lands on is not exported, not executable, and owned by the uid
    # the container runs as.
    tmpfs = body["HostConfig"]["Tmpfs"][scripts.TOKEN_MOUNT]
    assert "noexec" in tmpfs and "mode=0700" in tmpfs and "uid=1000" in tmpfs


def test_the_publisher_refuses_a_bundle_changed_after_collection(
    publisher: DockerPublisher, tmp_path: Path
) -> None:
    bundle = tmp_path / "work_branch.bundle"
    bundle.write_bytes(b"sealed branch bundle")
    sealed_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    bundle.write_bytes(b"changed branch bundle")

    with pytest.raises(ValueError, match="sealed sha256"):
        publisher._stage(tmp_path / "publish", str(bundle), sealed_sha256)


def test_the_publisher_script_can_never_force_push(publisher: DockerPublisher) -> None:
    """23: Crucible never force-pushes. A remote head that is not an ancestor fails the
    push and is recorded; it is never overwritten."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head=HEAD,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
    )
    # No force flag anywhere in the script, in any spelling.
    for forbidden in ("--force", "--force-with-lease", "--mirror", "+refs/", ":+refs/"):
        assert forbidden not in script, forbidden
    pushes = [line for line in script.splitlines() if "git push" in line]
    assert len(pushes) == 1, pushes
    push_line = pushes[0]
    # One push, one refspec, neither forced nor a delete.
    assert "refs/heads/crucible-publish:refs/heads/$WORK_BRANCH" in push_line
    for forbidden in ("-f", "--delete", "--prune", "--tags"):
        assert f" {forbidden}" not in push_line, forbidden


def test_the_script_has_no_commit_policy_refusal() -> None:
    """FDY-0143 (operator decision, 2026-09-29): no refusal for a commit's author or
    trailer. The guarantees before the push are the seal and the accepted head."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head=HEAD,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
    )
    assert "commit policy refused" not in script
    assert "commit-policy" not in script
    assert "exit 6" not in script
    push = script.index("git push --quiet origin")
    assert script.index('"$ACTUAL" != "$SEAL"') < push
    assert script.index('"$HEAD_SHA" != "$EXPECTED"') < push


def test_the_publisher_outcome_is_redacted_before_it_is_recorded(
    publisher: DockerPublisher, tmp_path: Path
) -> None:
    """12: the publisher's own log and the remote's refusal are text that could carry a
    credential, and both are stored in an event."""
    value = installation_token_value()
    out = tmp_path / "out"
    out.mkdir()
    (out / "step.txt").write_text("push", encoding="utf-8")
    (out / "error.txt").write_text(f"remote refused: {value}", encoding="utf-8")
    (out / "publisher.log").write_text(f"Authorization: Bearer {value}", encoding="utf-8")
    outcome = publisher._read_outcome(out, 5)
    assert value not in outcome.detail
    assert value not in outcome.log_tail
    assert "[redacted:" in outcome.detail and "[redacted:" in outcome.log_tail


def test_the_publisher_runs_on_its_own_egress_network_by_default() -> None:
    """23 step 3: github.com and api.github.com only. The workers' proxy permits every
    model endpoint a harness needs, which is not what a container holding a GitHub
    credential should be able to reach."""
    assert PublisherConfig().network == "crucible-publish"
    assert PublisherConfig().network != "crucible-workers"


# ----- hades #411: merge-main ------------------------------------------------------


class _Daemon:
    """The daemon calls a merge-main run makes. The container 'runs' when it is waited
    on, by writing the outcome files the script would have left."""

    def __init__(self, out: Path, files: dict[str, str], exit_code: int) -> None:
        self.out = out
        self.files = files
        self.exit_code = exit_code
        self.bodies: list[dict[str, Any]] = []
        self.stdin: list[bytes] = []
        self.removed: list[str] = []

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        self.bodies.append(body)
        return "c1"

    def start_container(self, container_id: str) -> None:
        return None

    def write_stdin(self, container_id: str, data: bytes) -> None:
        self.stdin.append(data)

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        for name, text in self.files.items():
            (self.out / name).write_text(text, encoding="utf-8")
        return self.exit_code

    def remove_container(self, container_id: str, *, force: bool = False) -> None:
        self.removed.append(container_id)


def _merge_publisher(
    tmp_path: Path, files: dict[str, str], exit_code: int
) -> tuple[DockerPublisher, _Daemon]:
    out = tmp_path / "publish" / "01ATTEMPT" / "merge-main" / "out"
    daemon = _Daemon(out, files, exit_code)
    provider = DockerProvider(
        DockerConfig(endpoint="tcp://127.0.0.1:1", artifact_root=str(tmp_path)),
        client=daemon,  # type: ignore[arg-type]
    )
    return DockerPublisher(provider, PublisherConfig(network="none")), daemon


def _merge_request(**kw: Any) -> MergeMainRequest:
    base: dict[str, Any] = {
        "attempt_id": "01ATTEMPT",
        "task_id": "01TASK",
        "owner": "FDY-0042",
        "repository_url": "https://github.com/owner/repo.git",
        "work_branch": "crucible/FDY-0042",
        "base_ref": "main",
        "expected_head": HEAD,
        "image": "crucible-worker:script-harness-1.0.0",
        "policy": {"resources": {"cpus": 1, "memory": "512m", "pids": 128}},
    }
    base.update(kw)
    return MergeMainRequest(**base)


async def test_a_docker_merge_main_pushes_with_a_lease_and_no_bundle(tmp_path: Path) -> None:
    value = installation_token_value()
    publisher, daemon = _merge_publisher(
        tmp_path,
        {"step.txt": "done", "push.txt": "ok", "merge-head.txt": "e" * 40},
        0,
    )

    outcome = await publisher.merge_main(_merge_request(), _token(value))

    assert (outcome.merged, outcome.head_sha) == (True, "e" * 40)
    assert daemon.stdin == [value.encode("utf-8")]
    body = daemon.bodies[0]
    assert value not in json.dumps(body)
    script = body["Cmd"][-1]
    assert scripts.MERGE_MAIN_MARKER in script
    assert '--force-with-lease="refs/heads/$WORK_BRANCH:$EXPECTED"' in script
    targets = [m["Target"] for m in body["HostConfig"]["Mounts"]]
    assert targets == [scripts.PUBLISH_MOUNT]
    assert daemon.removed == ["c1"]


async def test_a_docker_merge_main_conflict_names_the_files(tmp_path: Path) -> None:
    publisher, _daemon = _merge_publisher(
        tmp_path,
        {"step.txt": "conflict", "conflicts.txt": "a.txt\nb/c.py\n", "error.txt": "conflicts"},
        scripts.MERGE_MAIN_CONFLICT,
    )

    outcome = await publisher.merge_main(_merge_request(), _token(installation_token_value()))

    assert not outcome.merged
    assert outcome.conflicting_files == ("a.txt", "b/c.py")
    assert outcome.head_sha == ""


def _token(value: str) -> InstallationToken:
    return InstallationToken(
        value, expires_at=datetime.now(UTC) + timedelta(hours=1), repository="r"
    )
