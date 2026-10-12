"""The work bundle travels through the artifact store (ADR 0032, FDY-0627).

Collection stores the branch bundle as the attempt's `work_bundle` artifact and the
`bundle_head` row names it next to the seal; the Kubernetes publisher and a correction's
preparer read it from the store, delivered over exec, and no Pod mounts another
attempt's workspace claim. A resume whose bundle does not match its seal still refuses.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec, scripts
from crucible.adapters.execution.collected import read_outputs
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.publisher import DockerPublisher, PublisherConfig
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.evidence import record_collection_evidence, work_bundle_artifact
from crucible.contracts.evidence import (
    WORK_BUNDLE_CONTENT_TYPE,
    WORK_BUNDLE_NAME,
    WORK_BUNDLE_TYPE,
    EvidenceKind,
)
from crucible.domain.entities import Artifact, Attempt, EvidenceRecord, Task
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.execution import BranchBundle, CollectedOutputs, PrepareFailedError
from crucible.ports.publish import PublishRequest
from tests.integration.fake_github import installation_token_value
from tests.unit.kubernetes_fixtures import build, pod_of, spec
from tests.unit.test_publisher_shape import _Daemon, _token

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
BUNDLE = b"# v2 git bundle\nthe worker's commits, sealed by the collector\n"
SEAL = hashlib.sha256(BUNDLE).hexdigest()
HEAD = "a" * 40
PREVIOUS = "01PREVIOUSATTEMPT000000000"


class _Evidence:
    def __init__(self) -> None:
        self.rows: list[EvidenceRecord] = []

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        self.rows.append(record)
        return record

    def list_for_attempt(self, attempt_id: str) -> list[EvidenceRecord]:
        return [r for r in self.rows if r.attempt_id == attempt_id]


class _Artifacts:
    def __init__(self) -> None:
        self.rows: dict[str, Artifact] = {}

    def add(self, artifact: Artifact) -> None:
        self.rows[artifact.id] = artifact

    def get(self, artifact_id: str) -> Artifact | None:
        return self.rows.get(artifact_id)

    def find_by_sha256(self, sha256: str, attempt_id: str | None) -> Artifact | None:
        return next(
            (a for a in self.rows.values() if a.sha256 == sha256 and a.attempt_id == attempt_id),
            None,
        )


class _Events:
    def append(self, event: Any) -> Any:
        return event


def _task() -> Task:
    return Task(
        id="T1",
        external_id="X1",
        principal_id="P1",
        project="hades",
        title="t",
        state=TaskState.RUNNING,
        contract_version=1,
        policy_name="default",
        policy_version=1,
        repository_id="R1",
        created_at=NOW,
        updated_at=NOW,
    )


def _attempt() -> Attempt:
    return Attempt(
        id="A1",
        execution_id="E1",
        task_id="T1",
        number=1,
        state=AttemptState.RUNNING,
        created_at=NOW,
        exit_code=0,
    )


def _collect(tmp_path: Path, bundle: BranchBundle) -> tuple[Any, DiskArtifactStore]:
    uow: Any = SimpleNamespace(evidence=_Evidence(), artifacts=_Artifacts(), events=_Events())
    store = DiskArtifactStore(tmp_path / "store")
    record_collection_evidence(
        uow,
        SimpleNamespace(now=lambda: NOW),
        store,
        attempt=_attempt(),
        task=_task(),
        outputs=CollectedOutputs(report=None, report_raw=None, blocked_md=None, bundle=bundle),
        claim=None,
        claim_parsed_ok=False,
        parse_errors=[],
    )
    return uow, store


def _bundle(content: bytes = BUNDLE) -> BranchBundle:
    return BranchBundle(
        head_sha=HEAD,
        base_ref="main",
        work_branch="crucible/X1",
        commits=1,
        verified=True,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


# ----- collection --------------------------------------------------------------------


def test_collection_reads_the_bundle_bytes_the_seal_is_computed_from(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "head.txt").write_text(HEAD + "\n")
    (output / "commits.txt").write_text("1\n")
    (output / "work_branch.bundle").write_bytes(BUNDLE)
    collected = read_outputs(
        output,
        tmp_path / "verify",
        spec=spec(),
        bundle_verified=True,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )
    assert collected.bundle is not None
    assert collected.bundle.content == BUNDLE
    assert collected.bundle.sha256 == SEAL


def test_the_collector_stores_the_bundle_as_a_work_bundle_artifact(tmp_path: Path) -> None:
    uow, store = _collect(tmp_path, _bundle())

    bundles = [a for a in uow.artifacts.rows.values() if a.type == WORK_BUNDLE_TYPE]
    assert len(bundles) == 1
    artifact = bundles[0]
    assert (artifact.filename, artifact.content_type) == (
        WORK_BUNDLE_NAME,
        WORK_BUNDLE_CONTENT_TYPE,
    )
    assert (artifact.attempt_id, artifact.task_id, artifact.created_by) == ("A1", "T1", "crucible")
    # The stored blob is the sealed bundle, byte for byte.
    assert artifact.sha256 == SEAL
    assert hashlib.sha256(store.get(artifact.path)).hexdigest() == SEAL
    assert store.get(artifact.path) == BUNDLE


def test_the_bundle_head_row_names_the_artifact_next_to_its_seal(tmp_path: Path) -> None:
    uow, _store = _collect(tmp_path, _bundle())

    artifact = next(a for a in uow.artifacts.rows.values() if a.type == WORK_BUNDLE_TYPE)
    rows = [r for r in uow.evidence.rows if r.kind == EvidenceKind.BUNDLE_HEAD.value]
    assert len(rows) == 1
    row = rows[0]
    assert row.artifact_id == artifact.id
    assert row.payload["bundle_artifact_id"] == artifact.id
    assert row.payload["bundle_sha256"] == SEAL == artifact.sha256
    assert row.verified and row.source == "crucible"
    assert work_bundle_artifact(uow, row) == artifact


def test_a_bundle_the_scanner_refuses_is_not_stored_and_is_a_finding(tmp_path: Path) -> None:
    # A credential shape built at runtime, never written out (CONTRIBUTING rule 1).
    secret = ("gh" + "p_") + "A1b2" * 9
    content = b"pack " + secret.encode() + b"\n"
    uow, _store = _collect(tmp_path, _bundle(content))

    assert not [a for a in uow.artifacts.rows.values() if a.type == WORK_BUNDLE_TYPE]
    row = next(r for r in uow.evidence.rows if r.kind == EvidenceKind.BUNDLE_HEAD.value)
    assert row.artifact_id is None and row.payload["bundle_artifact_id"] is None
    scanner = next(r for r in uow.evidence.rows if r.kind == EvidenceKind.SCANNER_RESULT.value)
    assert any(f["where"] == f"artifact:{WORK_BUNDLE_NAME}" for f in scanner.payload["findings"])


def test_a_collection_with_no_bundle_file_stores_nothing(tmp_path: Path) -> None:
    uow, _store = _collect(tmp_path, _bundle(b""))
    assert not uow.artifacts.rows
    row = next(r for r in uow.evidence.rows if r.kind == EvidenceKind.BUNDLE_HEAD.value)
    assert row.artifact_id is None
    scanner = next(r for r in uow.evidence.rows if r.kind == EvidenceKind.SCANNER_RESULT.value)
    assert f"artifact:{WORK_BUNDLE_NAME}" not in scanner.payload["scanned"]


def test_the_scanner_coverage_names_the_bundle_it_scanned(tmp_path: Path) -> None:
    """The bundle is scanned when it is stored, though it is collected apart from the
    other artifacts: the scanner row's coverage names it, as for any artifact."""
    uow, _store = _collect(tmp_path, _bundle())
    scanner = next(r for r in uow.evidence.rows if r.kind == EvidenceKind.SCANNER_RESULT.value)
    assert f"artifact:{WORK_BUNDLE_NAME}" in scanner.payload["scanned"]


def test_an_artifact_whose_digest_is_not_the_seal_is_never_the_bundle(tmp_path: Path) -> None:
    uow, _store = _collect(tmp_path, _bundle())
    row = next(r for r in uow.evidence.rows if r.kind == EvidenceKind.BUNDLE_HEAD.value)
    row.payload["bundle_sha256"] = "b" * 64
    assert work_bundle_artifact(uow, row) is None


# ----- a correction's preparer on Kubernetes ----------------------------------------


class _Store:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    def put(self, content: bytes) -> Any:
        raise NotImplementedError

    def get(self, path: str) -> bytes:
        if path not in self.blobs:
            raise FileNotFoundError(path)
        return self.blobs[path]

    def exists(self, path: str) -> bool:
        return path in self.blobs


def _resume(seal: str = SEAL, artifact: str | None = "blobs/bundle") -> Any:
    return replace(
        spec(),
        role="correct",
        resume_bundle_path=f"k8s://hades-workers/ws-{PREVIOUS.lower()}/output/work_branch.bundle",
        resume_bundle_attempt_id=PREVIOUS,
        resume_bundle_head=HEAD,
        resume_bundle_sha256=seal,
        resume_bundle_artifact=artifact,
    )


def _claims(pod: dict[str, Any]) -> list[str]:
    return [
        str(v["persistentVolumeClaim"]["claimName"])
        for v in pod["volumes"]
        if "persistentVolumeClaim" in v
    ]


async def test_a_correction_preparer_resumes_from_the_stored_bundle_without_the_old_claim() -> None:
    api, _registry, provider = build()
    provider.artifact_store = _Store({"blobs/bundle": BUNDLE})
    launch = _resume()

    await provider.prepare(launch)

    pod = pod_of(api, "prepare")
    previous_claim = k8sspec.object_name("ws", PREVIOUS)
    assert previous_claim not in _claims(pod)
    assert _claims(pod) == [k8sspec.object_name("ws", launch.attempt_id)]
    script = pod["containers"][0]["command"][-1]
    assert scripts.RESUME_DELIVERY_MARKER in script
    # The bundle arrived over exec into the new attempt's own workspace, and the
    # script's own seal check still ran over it.
    claim = api.claims[k8sspec.object_name("ws", launch.attempt_id)]
    assert claim[scripts.RESUME_BUNDLE_LEAF] == BUNDLE
    assert f"'{SEAL}'" in script
    # No Pod of the attempt mounted the previous attempt's claim.
    for row in api.created:
        if row["kind"] == "jobs":
            assert previous_claim not in _claims(row["body"]["spec"]["template"]["spec"])


async def test_a_resume_whose_bundle_does_not_match_its_seal_still_refuses() -> None:
    api, _registry, provider = build()
    provider.artifact_store = _Store({"blobs/bundle": b"a bundle somebody changed"})

    with pytest.raises(PrepareFailedError, match="does not match its seal"):
        await provider.prepare(_resume())
    pod = pod_of(api, "prepare")
    assert k8sspec.object_name("ws", PREVIOUS) not in _claims(pod)


async def test_a_resume_with_no_stored_bundle_is_refused_before_any_pod() -> None:
    api, _registry, provider = build()
    provider.artifact_store = _Store({})

    with pytest.raises(PrepareFailedError, match="previous attempt bundle is gone"):
        await provider.prepare(_resume(artifact=None))
    with pytest.raises(PrepareFailedError, match="previous attempt bundle is gone"):
        await provider.prepare(_resume())
    assert not [
        row for row in api.created if row["kind"] == "jobs" and row["name"].startswith("prepare")
    ]


# ----- the Docker publisher reads the same artifact ----------------------------------


async def test_the_docker_publisher_stages_the_stored_bundle_not_the_workspace_copy(
    tmp_path: Path,
) -> None:
    store = DiskArtifactStore(tmp_path)
    stored = store.put(BUNDLE)
    workspace = tmp_path / "workspaces" / "01ATTEMPT" / "output"
    workspace.mkdir(parents=True)
    (workspace / "work_branch.bundle").write_bytes(b"a workspace copy nobody reads")
    daemon = _Daemon(tmp_path / "publish" / "01ATTEMPT" / "out", {"step.txt": "done"}, 0)
    provider = DockerProvider(
        DockerConfig(endpoint="tcp://127.0.0.1:1", artifact_root=str(tmp_path)),
        client=daemon,  # type: ignore[arg-type]
    )
    staged: list[bytes] = []
    publisher = DockerPublisher(provider, PublisherConfig(network="none"))
    stage = publisher._stage

    def recording(root: Path, source: str, seal: str) -> None:
        stage(root, source, seal)
        staged.append((root / "bundle" / "work_branch.bundle").read_bytes())

    publisher._stage = recording  # type: ignore[method-assign,assignment]
    await publisher.push(
        PublishRequest(
            attempt_id="01ATTEMPT",
            task_id="01TASK",
            owner="FDY-0042",
            repository_url="https://github.com/owner/repo.git",
            work_branch="crucible/FDY-0042",
            base_ref="main",
            expected_head=HEAD,
            bundle_path=str(workspace / "work_branch.bundle"),
            bundle_sha256=SEAL,
            bundle_artifact=stored.path,
            image="crucible-worker:script-harness-1.0.0",
            policy={"resources": {"cpus": 1, "memory": "512m", "pids": 128}},
        ),
        _token(installation_token_value()),
    )
    assert staged == [BUNDLE]
