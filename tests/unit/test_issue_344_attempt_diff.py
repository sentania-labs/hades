"""hades #344: the collector's review diff is an attempt artifact before publication.

It is the collector's own file, outside the worker's report namespace, recorded under
its own evidence role, scanned like every other artifact, and served on the UI only as
inert text."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from starlette.testclient import TestClient

from crucible.adapters.api.deps import app_context, unit_of_work
from crucible.adapters.execution.collected import read_outputs
from crucible.adapters.execution.scripts import REVIEW_DIFF_DIR, collector_script
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.adapters.ui import session as ui
from crucible.adapters.ui.pages.tasks import _attempt_diff_link
from crucible.adapters.ui.router import router
from crucible.application.evidence import (
    _scanner_findings,
    record_collection_evidence,
    store_artifact,
)
from crucible.application.gates import evidence_items
from crucible.contracts.evidence import REVIEW_DIFF_NAME, ROLE_REVIEW_DIFF
from crucible.domain.entities import (
    Artifact,
    Attempt,
    EvidenceRecord,
    Principal,
    Role,
    Task,
    UiSession,
)
from crucible.domain.gates import GateInput, GateResult, run_evidence_present
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.execution import CollectedArtifact, CollectedOutputs, LaunchSpec
from tests.collector_tools import collector_env

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
GIT_USER = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_USER, *args], cwd=repo, check=True)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _repository(tmp_path: Path, *, changed: str = "changed\n" + "x" * 4096) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "work.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    _git(repo, "checkout", "-qb", "crucible/test")
    (repo / "work.txt").write_text(changed, encoding="utf-8")
    _commit(repo, "change")
    return repo


def _collect(tmp_path: Path, repo: Path, *, cap: int = 1024 * 1024) -> Path:
    """Run the real collector script against `repo`, as the scripts tests do."""
    report = tmp_path / "worker-report"
    output = tmp_path / "output"
    report.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    # Stand in for the trusted record left by preparation in these collector fixtures.
    (output / "prepared-base.txt").write_text(
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "main"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    script = collector_script(base_ref="main", work_branch="crucible/test", size_cap_bytes=cap)
    script = (
        script.replace("/crucible/report", str(report))
        .replace("/crucible/repo", str(repo))
        .replace("/crucible/out", str(output))
    )
    result = subprocess.run(
        ["sh", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env=collector_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert (output / "collector.ok").is_file()
    return output


def _review_diff(output: Path) -> str:
    return (output / REVIEW_DIFF_DIR / "diff.patch").read_text(encoding="utf-8")


def _spec() -> LaunchSpec:
    return LaunchSpec(
        attempt_id="A1",
        task_id="T1",
        external_id="X1",
        role="implement",
        harness="test",
        model="test",
        image="test",
        timeout_seconds=1,
        contract={"repository": {"base_ref": "main", "work_branch": "crucible/test"}},
    )


def _read(tmp_path: Path, output: Path) -> Any:
    return read_outputs(
        output,
        tmp_path / "verify",
        spec=_spec(),
        bundle_verified=False,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )


# The collector script.


def test_a_small_diff_is_whole_with_its_stat_header_and_no_marker(tmp_path: Path) -> None:
    output = _collect(tmp_path, _repository(tmp_path, changed="changed\n"))
    review = _review_diff(output)
    stat = (output / "diffstat.txt").read_text(encoding="utf-8")
    patch = (output / "diff.patch").read_text(encoding="utf-8")
    assert review == stat + "\n" + patch
    assert "work.txt |" in review
    assert "+changed" in review
    assert "[crucible: diff truncated]" not in review
    # Written by the collector, not into the worker's report directory.
    assert not (output / "report" / "diff.patch").exists()


def test_a_large_diff_is_bounded_with_a_truncation_marker(tmp_path: Path) -> None:
    output = _collect(tmp_path, _repository(tmp_path), cap=512)
    review = _review_diff(output)
    assert review.startswith(" work.txt |")
    assert "diff --git" in review
    assert review.endswith("\n[crucible: diff truncated]\n")
    assert len(review.encode()) <= 512
    assert (output / "diff.patch").stat().st_size > len(review.encode())


def test_a_worker_report_diff_patch_is_kept_as_the_worker_wrote_it(tmp_path: Path) -> None:
    repo = _repository(tmp_path, changed="changed\n")
    (tmp_path / "worker-report").mkdir()
    (tmp_path / "worker-report" / "diff.patch").write_text("the worker's own\n")
    output = _collect(tmp_path, repo)
    assert (output / "report" / "diff.patch").read_text() == "the worker's own\n"
    outputs = _read(tmp_path, output)
    by_name = {a.name: a for a in outputs.artifacts}
    assert by_name["report/diff.patch"].type == "run_evidence"
    assert by_name["report/diff.patch"].content == b"the worker's own\n"
    assert by_name[REVIEW_DIFF_NAME].type == "diff"
    assert by_name[REVIEW_DIFF_NAME].content_type == "text/x-diff"
    assert b"+changed" in by_name[REVIEW_DIFF_NAME].content


def test_a_directory_at_the_target_does_not_fail_collection(tmp_path: Path) -> None:
    repo = _repository(tmp_path, changed="changed\n")
    blocker = tmp_path / "output" / REVIEW_DIFF_DIR / "diff.patch"
    blocker.mkdir(parents=True)
    (blocker / "inside").write_text("x")
    output = _collect(tmp_path, repo)
    assert "+changed" in _review_diff(output)


def test_a_failing_diff_writes_the_marker_and_collection_succeeds(tmp_path: Path) -> None:
    repo = _repository(tmp_path, changed="changed\n")
    blob = subprocess.run(
        ["git", "rev-parse", "HEAD:work.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    output = _collect(tmp_path, repo)
    review = _review_diff(output)
    assert review.startswith("diff unavailable: ")
    assert len(review) < 200


def test_a_textconv_attribute_neither_runs_nor_hides_a_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ran = tmp_path / "textconv-ran"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / ".gitattributes").write_text("*.txt diff=hide\n")
    (repo / "work.txt").write_text("base\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-qb", "crucible/test")
    (repo / "work.txt").write_text("base\nhidden-hunk-line\n")
    _commit(repo, "change")
    # The worker's own .git/config, which the collector leaves in place.
    _git(repo, "config", "diff.hide.textconv", f"sh -c 'touch {ran}; echo nothing' x")
    output = _collect(tmp_path, repo)
    assert not ran.exists()
    assert "+hidden-hunk-line" in (output / "diff.patch").read_text()
    assert "+hidden-hunk-line" in _review_diff(output)
    assert "work.txt | 1 +" in (output / "diffstat.txt").read_text()


SECRET = "gh" + "p_" + "b" * 36


def _scan(tmp_path: Path, output: Path) -> list[dict[str, str]]:
    collected = _read(tmp_path, output)
    return _scanner_findings(
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md=None,
            diff_paths=collected.diff_paths,
            diff_findings=collected.diff_findings,
            diff_unscanned=collected.diff_unscanned,
            artifacts=collected.artifacts,
        ),
        None,
    )


def _branch(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "work.txt").write_text("base\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-qb", "crucible/test")
    return repo


def test_a_worker_binary_threshold_cannot_hide_a_secret(tmp_path: Path) -> None:
    repo = _branch(tmp_path)
    _git(repo, "config", "core.bigFileThreshold", "1")
    (repo / "z.txt").write_text(f"token={SECRET}\n")
    _commit(repo, "secret with lowered binary threshold")
    output = _collect(tmp_path, repo)
    assert f"+token={SECRET}" in (output / "diff.patch").read_text()
    assert f"+token={SECRET}" in _review_diff(output)
    assert any(f["where"] == "diff:z.txt" for f in _scan(tmp_path, output))


def test_a_large_binary_does_not_push_a_later_secret_past_the_scanner(tmp_path: Path) -> None:
    repo = _branch(tmp_path)
    (repo / "a.bin").write_bytes(b"\0" * (9 * 1024 * 1024))
    (repo / "z.txt").write_text(f"token={SECRET}\n")
    _commit(repo, "binary then secret")
    output = _collect(tmp_path, repo)
    raw = (output / "diff.patch").read_bytes()
    assert b"Binary files" in raw and len(raw) < 64 * 1024
    assert any(f["where"] == "diff:z.txt" for f in _scan(tmp_path, output))
    assert len((output / REVIEW_DIFF_DIR / "diff.patch").read_bytes()) < 64 * 1024


def test_a_text_file_the_worker_marks_binary_is_text_in_the_review_copy(tmp_path: Path) -> None:
    repo = _branch(tmp_path)
    (repo / ".gitattributes").write_text("hidden.txt binary\n")
    (repo / "hidden.txt").write_text("hidden-by-attribute\n")
    (repo / "plain.txt").write_text("plain-line\n")
    _commit(repo, "attribute")
    output = _collect(tmp_path, repo)
    assert "Binary files /dev/null and b/hidden.txt differ" in (output / "diff.patch").read_text()
    review = _review_diff(output)
    assert "+hidden-by-attribute" in review and "+plain-line" in review
    assert review.index("+hidden-by-attribute") < review.index("[crucible: full diff]")
    assert not (output / "attr-text.patch").exists()


def test_a_replace_ref_changes_neither_the_diff_nor_the_scanned_content(tmp_path: Path) -> None:
    repo = _branch(tmp_path)
    (repo / "evil.txt").write_text(f"token={SECRET}\n")
    _commit(repo, "evil")
    evil_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    benign = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input="benign\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "replace", "HEAD:evil.txt", benign)
    # And a whole fabricated head commit, which would otherwise rewrite the diff.
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-qb", "decoy")
    (repo / "decoy.txt").write_text("nothing to see\n")
    _commit(repo, "decoy")
    _git(repo, "checkout", "-q", "crucible/test")
    _git(repo, "replace", "-f", evil_head, "decoy")
    output = _collect(tmp_path, repo)
    raw = (output / "diff.patch").read_text()
    assert f"+token={SECRET}" in raw and "benign" not in raw and "decoy" not in raw
    assert f"+token={SECRET}" in _review_diff(output)
    assert any(f["where"] == "diff:evil.txt" for f in _scan(tmp_path, output))
    assert (output / "head.txt").read_text().strip() == evil_head


def test_collection_does_not_log_graft_deprecation_hints(tmp_path: Path) -> None:
    output = _collect(tmp_path, _repository(tmp_path, changed="changed\n"))
    log = (output / "bundle.log").read_text()
    assert not any("graft" in line.lower() for line in log.splitlines()), log


def test_a_graft_does_not_change_the_merge_base(tmp_path: Path) -> None:
    repo = _branch(tmp_path)
    (repo / "z.txt").write_text("grafted\n")
    _commit(repo, "change")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text(f"{head}\n")
    output = _collect(tmp_path, repo)
    assert "+grafted" in (output / "diff.patch").read_text()
    assert "work.txt" not in (output / "changed.txt").read_text()


def test_a_second_collection_leaves_nothing_from_the_first(tmp_path: Path) -> None:
    repo = _repository(tmp_path, changed="changed\n")
    report = tmp_path / "worker-report"
    report.mkdir()
    (report / "first.txt").write_text("from the first run\n")
    output = _collect(tmp_path, repo)
    assert (output / "report" / "first.txt").is_file()
    (report / "first.txt").unlink()
    (output / "collection-failed.txt").write_text("stale\n")
    (output / "leftover-committed.txt").write_text("stale\n")
    (output / "prepared-head.txt").write_text("kept\n")
    output = _collect(tmp_path, repo)
    assert not (output / "report" / "first.txt").exists()
    assert not (output / "collection-failed.txt").exists()
    assert not (output / "leftover-committed.txt").exists()
    assert (output / "prepared-head.txt").read_text() == "kept\n"


# Evidence and the gate.


class _Evidence:
    def __init__(self) -> None:
        self.rows: list[EvidenceRecord] = []

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        record.id = len(self.rows) + 1
        self.rows.append(record)
        return record

    def list_for_attempt(self, attempt_id: str) -> list[EvidenceRecord]:
        return [r for r in self.rows if r.attempt_id == attempt_id]

    def list_for_task(self, task_id: str) -> list[EvidenceRecord]:
        return [r for r in self.rows if r.task_id == task_id]


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

    def list_for_attempt(self, attempt_id: str) -> list[Artifact]:
        return [a for a in self.rows.values() if a.attempt_id == attempt_id]


class _Events:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def append(self, event: Any) -> Any:
        self.rows.append(event)
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


def test_the_review_diff_has_its_own_role_and_is_not_run_evidence(tmp_path: Path) -> None:
    output = _collect(tmp_path, _repository(tmp_path, changed="changed\n"))
    collected = _read(tmp_path, output)
    outputs = CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        diff_paths=collected.diff_paths,
        diff_findings=collected.diff_findings,
        diff_unscanned=collected.diff_unscanned,
        artifacts=collected.artifacts,
    )
    uow: Any = SimpleNamespace(evidence=_Evidence(), artifacts=_Artifacts(), events=_Events())
    record_collection_evidence(
        uow,
        SimpleNamespace(now=lambda: NOW),
        DiskArtifactStore(tmp_path / "store"),
        attempt=_attempt(),
        task=_task(),
        outputs=outputs,
        claim=None,
        claim_parsed_ok=False,
        parse_errors=[],
    )
    stored = [a for a in uow.artifacts.rows.values() if a.filename == REVIEW_DIFF_NAME]
    assert len(stored) == 1
    assert stored[0].type == "diff" and stored[0].created_by == "crucible"
    present = [r for r in uow.evidence.rows if r.payload.get("path") == REVIEW_DIFF_NAME]
    assert [r.payload["role"] for r in present] == [ROLE_REVIEW_DIFF]
    assert present[0].artifact_id == stored[0].id
    for path in ("report/diff.patch", REVIEW_DIFF_NAME):
        contract = {"required_verification": [{"id": "V1", "kind": "artifact", "path": path}]}
        outcome = run_evidence_present(
            GateInput(
                contract=contract,
                policy={},
                head_sha=None,
                evidence=evidence_items(uow, "A1", "T1"),
            )
        )
        assert outcome.result is GateResult.FAIL, outcome.detail


def test_the_review_diff_is_not_scanned_a_second_time() -> None:
    secret = ("gh" + "p_" + "a" * 36).encode()
    outputs = CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        artifacts=(
            CollectedArtifact(
                name=REVIEW_DIFF_NAME,
                type="diff",
                content=b"+token=" + secret,
                content_type="text/x-diff",
            ),
        ),
    )
    assert _scanner_findings(outputs, None) == []


# The UI route, through the real router and an app test client.

PRINCIPAL = Principal("01K6H9ZH2J7F0X7M6C1Y8D3P4Q", "admin", Role.ADMIN, NOW)
SESSION_ID = "ab" * 32


class _Sessions:
    def get(self, session_id: str) -> UiSession | None:
        if session_id != SESSION_ID:
            return None
        return UiSession(SESSION_ID, PRINCIPAL.id, "csrf", NOW, NOW + timedelta(hours=1), NOW)


class _Principals:
    def get(self, principal_id: str) -> Principal | None:
        return PRINCIPAL if principal_id == PRINCIPAL.id else None


def _client(tmp_path: Path) -> tuple[TestClient, Any, Any]:
    store = DiskArtifactStore(tmp_path / "store")
    ctx: Any = SimpleNamespace(
        ui_signing_key=b"unit-test-signing-key",
        clock=SimpleNamespace(now=lambda: NOW),
        first_run=None,
        artifact_store=store,
    )
    uow: Any = SimpleNamespace(
        ui_sessions=_Sessions(),
        principals=_Principals(),
        artifacts=_Artifacts(),
        events=_Events(),
        commit=lambda: None,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[app_context] = lambda: ctx
    app.dependency_overrides[unit_of_work] = lambda: uow
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    client.cookies.set(ui.COOKIE, ui._serializer(ctx).dumps(SESSION_ID))
    return client, ctx, uow


def _store(ctx: Any, uow: Any, **kwargs: Any) -> Artifact:
    return store_artifact(uow, ctx.clock, ctx.artifact_store, attempt=_attempt(), **kwargs)


def test_the_route_serves_the_collector_diff_as_inert_text(tmp_path: Path) -> None:
    client, ctx, uow = _client(tmp_path)
    diff = _store(
        ctx,
        uow,
        name=REVIEW_DIFF_NAME,
        artifact_type="diff",
        content=b" a | 1 +\n\n<script>alert(1)</script>\n",
        content_type="text/html",
    )
    with client:
        response = client.get(f"/ui/artifacts/{diff.id}/content")
    assert response.status_code == 200
    assert response.content == b" a | 1 +\n\n<script>alert(1)</script>\n"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"


def test_the_route_refuses_any_other_artifact(tmp_path: Path) -> None:
    client, ctx, uow = _client(tmp_path)
    html = b"<svg onload=alert(1)></svg>"
    refused = [
        # An orchestrator's upload with the diff's own type and name.
        _store(
            ctx,
            uow,
            name=REVIEW_DIFF_NAME,
            artifact_type="diff",
            content=html,
            content_type="image/svg+xml",
            created_by="orchestrator",
        ),
        # The collector's, but not the review diff.
        _store(
            ctx,
            uow,
            name="report/page.html",
            artifact_type="run_evidence",
            content=html + b"2",
            content_type="text/html",
        ),
        # A worker-written file named like the old location.
        _store(
            ctx,
            uow,
            name="report/diff.patch",
            artifact_type="run_evidence",
            content=html + b"3",
            content_type="text/plain",
        ),
    ]
    with client:
        for artifact in refused:
            response = client.get(f"/ui/artifacts/{artifact.id}/content")
            assert response.status_code == 404, artifact.filename
            assert html not in response.content
        assert client.get("/ui/artifacts/missing/content").status_code == 404


def test_the_task_view_links_only_the_collector_diff(tmp_path: Path) -> None:
    _, ctx, uow = _client(tmp_path)
    _store(
        ctx,
        uow,
        name=REVIEW_DIFF_NAME,
        artifact_type="diff",
        content=b"uploaded",
        content_type="text/html",
        created_by="orchestrator",
    )
    assert _attempt_diff_link(uow, "A1") == "not collected"
    diff = _store(
        ctx,
        uow,
        name=REVIEW_DIFF_NAME,
        artifact_type="diff",
        content=b"collected",
        content_type="text/x-diff",
    )
    assert _attempt_diff_link(uow, "A1") == {
        "kind": "link",
        "href": f"/ui/artifacts/{diff.id}/content",
        "label": "diff.patch",
    }
