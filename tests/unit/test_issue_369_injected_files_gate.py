"""hades #369: `no_injected_files` fails on a shim the branch adds or fills with the
injected text, and passes a branch that edits or deletes the repository's own CLAUDE.md
or AGENTS.md. Evidence collected before #369 carries no status and is judged as before.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.collected import read_outputs, read_path_changes
from crucible.application.evidence import record_collection_evidence
from crucible.domain.entities import Attempt, EvidenceRecord, Task
from crucible.domain.gates import (
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    evaluate_gate,
    injected_shim_text,
)
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPORT_MOUNT,
    WORK_MOUNT,
    CollectedOutputs,
    LaunchSpec,
)
from tests.fixtures import contract_document

ZERO = "0" * 40
OTHER = "b" * 40
SHIM_BLOB = hashlib.sha1(
    b"blob %d\0" % len(injected_shim_text() + "\n") + (injected_shim_text() + "\n").encode()
).hexdigest()


def _change(path: str, status: str, blob: str = OTHER) -> dict[str, str]:
    return {"path": path, "status": status, "blob": ZERO if status == "D" else blob}


def _outcome(
    diff: dict[str, Any], commit_paths: list[str], commit_changes: list[dict[str, str]] | None
) -> GateResult:
    bundle: dict[str, Any] = {"head_sha": "a" * 40, "commits": 1, "commit_paths": commit_paths}
    if commit_changes is not None:
        bundle["commit_changes"] = commit_changes
    evidence = (
        EvidenceItem(id=1, kind="diff_paths", source="crucible", verified=True, payload=diff),
        EvidenceItem(id=2, kind="bundle_head", source="crucible", verified=True, payload=bundle),
    )
    gi = GateInput(contract=contract_document(), policy={}, head_sha="a" * 40, evidence=evidence)
    return evaluate_gate(GateName.NO_INJECTED_FILES, gi).result


def _judge(changes: list[dict[str, str]]) -> GateResult:
    """The same changes as the diff and as one commit, as the collector records them."""
    paths = [c["path"] for c in changes]
    return _outcome({"paths": paths, "changes": changes}, sorted(paths), changes)


def test_deleting_the_bases_claude_md_and_agents_md_passes() -> None:
    assert _judge([_change("CLAUDE.md", "D"), _change("AGENTS.md", "D")]) is GateResult.PASS


def test_editing_the_bases_claude_md_and_agents_md_passes() -> None:
    changes = [_change("CLAUDE.md", "M"), _change("docs/AGENTS.md", "M")]
    assert _judge(changes) is GateResult.PASS


def test_adding_a_claude_md_the_base_lacks_fails() -> None:
    assert _judge([_change("CLAUDE.md", "A")]) is GateResult.FAIL
    assert _judge([_change("pkg/AGENTS.md", "A")]) is GateResult.FAIL


def test_a_file_whose_content_equals_the_injected_shim_fails() -> None:
    assert _judge([_change("AGENTS.md", "M", SHIM_BLOB)]) is GateResult.FAIL


def test_a_shim_committed_and_then_removed_still_fails() -> None:
    """Every commit is read, not only the diff: a shim added in one commit and deleted in
    the next leaves the diff empty but is still in the branch's history."""
    commits = [_change("AGENTS.md", "D"), _change("AGENTS.md", "A", SHIM_BLOB)]
    assert _outcome({"paths": [], "changes": []}, ["AGENTS.md"], commits) is GateResult.FAIL


def test_deleting_and_restoring_the_bases_file_passes() -> None:
    commits = [_change("CLAUDE.md", "A"), _change("CLAUDE.md", "D")]
    assert _outcome({"paths": [], "changes": []}, ["CLAUDE.md"], commits) is GateResult.PASS


def test_a_crucible_path_fails_whatever_its_status() -> None:
    for status in ("A", "M", "D"):
        assert _judge([_change(".crucible/identity.md", status)]) is GateResult.FAIL


def test_old_evidence_without_status_behaves_as_today() -> None:
    assert _outcome({"paths": ["CLAUDE.md"]}, [], None) is GateResult.FAIL
    assert _outcome({"paths": []}, ["AGENTS.md"], None) is GateResult.FAIL
    assert _outcome({"paths": ["src/a.py"]}, ["src/a.py"], None) is GateResult.PASS
    # A status for the diff does not excuse a commit list that has none.
    diff = {"paths": ["CLAUDE.md"], "changes": [_change("CLAUDE.md", "M")]}
    assert _outcome(diff, ["CLAUDE.md"], None) is GateResult.FAIL


def test_the_preparer_writes_the_shim_text_the_gate_knows() -> None:
    script = scripts.preparer_script(
        url="https://github.com/o/r.git",
        base_ref="main",
        work_branch="crucible/test",
        from_remote_branch=False,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder="crucible-no-remote://nowhere",
        claude_md_wins=True,
        shims=("AGENTS.md",),
        exclude_entries=("/AGENTS.md",),
        identity_mount=IDENTITY_MOUNT,
    )
    assert f"SHIM_TEXT='{injected_shim_text(IDENTITY_MOUNT)}'" in script


# ----- through the real collector, read_outputs and record_collection_evidence ------

NOW = datetime(2026, 10, 2, tzinfo=UTC)
SHIM = injected_shim_text() + "\n"


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    ).stdout


def _write(repo: Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, date: str | None = None) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message, date=date)


def _repo(tmp_path: Path, *, agents_md: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "crucible-worker@users.noreply.github.com")
    _git(repo, "config", "user.name", "crucible-worker")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, "CLAUDE.md", "# the project's own\n")
    if agents_md:
        _write(repo, "AGENTS.md", "# also the project's own\n")
    _commit(repo, "base")
    output = tmp_path / "output"
    output.mkdir()
    (output / "prepared-base.txt").write_text(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "-b", "crucible/test")
    return repo


def _evidence(tmp_path: Path, repo: Path, *, expected_exit: int = 0) -> tuple[EvidenceItem, ...]:
    """Collect `repo` with the collector script, read it back as the providers do, and
    record it as the supervisor does; the gate reads what was recorded."""
    output, report = tmp_path / "output", tmp_path / "report"
    output.mkdir(exist_ok=True)
    report.mkdir(exist_ok=True)
    generated = scripts.collector_script(
        base_ref="main", work_branch="crucible/test", size_cap_bytes=1024
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == expected_exit, result.stderr
    spec = LaunchSpec(
        attempt_id="01ATTEMPT",
        task_id="01TASK",
        external_id="EX-0001",
        role="implement",
        harness="script-harness",
        model="none",
        image="crucible-worker:test",
        timeout_seconds=600,
        contract=contract_document(),
    )
    read = read_outputs(
        output,
        tmp_path / "verify",
        spec=spec,
        bundle_verified=True,
        collector_exit=result.returncode,
        verifications=(),
        tail_bytes=1024,
    )
    outputs = CollectedOutputs(
        report=read.report,
        report_raw=read.report_raw,
        blocked_md=read.blocked_md,
        stdout_tail=read.stdout_tail,
        stderr_tail=read.stderr_tail,
        diff_paths=read.diff_paths,
        diff_text=read.diff_text,
        diff_changes=read.diff_changes,
        base_paths=read.base_paths,
        over_limit=read.over_limit,
        bundle=read.bundle,
        artifacts=read.artifacts,
    )
    recorded: list[EvidenceRecord] = []

    def add(record: EvidenceRecord) -> EvidenceRecord:
        recorded.append(record)
        return record

    uow = MagicMock()
    uow.evidence.add.side_effect = add
    clock = MagicMock()
    clock.now.return_value = NOW
    attempt = Attempt(
        id="01ATTEMPT",
        execution_id="01EXEC",
        task_id="01TASK",
        number=1,
        state=AttemptState.RUNNING,
        created_at=NOW,
    )
    task = Task(
        id="01TASK",
        external_id="EX-0001",
        principal_id="01PRINCIPAL",
        project="hades",
        title="t",
        state=TaskState.RUNNING,
        contract_version=1,
        policy_name="p",
        policy_version=1,
        repository_id="01REPO",
        created_at=NOW,
        updated_at=NOW,
    )
    record_collection_evidence(
        uow,
        clock,
        MagicMock(),
        attempt=attempt,
        task=task,
        outputs=outputs,
        claim=None,
        claim_parsed_ok=False,
        parse_errors=[],
    )
    return tuple(
        EvidenceItem(id=n, kind=r.kind, source=r.source, verified=r.verified, payload=r.payload)
        for n, r in enumerate(recorded, start=1)
    )


def _collected(tmp_path: Path, repo: Path) -> tuple[GateResult, dict[str, Any], dict[str, Any]]:
    """The gate's answer on the recorded evidence, and the diff_paths and bundle_head
    payloads it read."""
    evidence = _evidence(tmp_path, repo)
    gi = GateInput(contract=contract_document(), policy={}, head_sha="a" * 40, evidence=evidence)
    payloads = {e.kind: e.payload for e in evidence}
    result = evaluate_gate(GateName.NO_INJECTED_FILES, gi).result
    return result, payloads["diff_paths"], payloads["bundle_head"]


def test_the_collector_lets_a_branch_delete_and_edit_its_own_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "rm", "-q", "AGENTS.md")
    _write(repo, "CLAUDE.md", "# edited\n")
    _commit(repo, "edit and delete")
    result, diff, _ = _collected(tmp_path, repo)
    assert {(c["path"], c["status"]) for c in diff["changes"]} == {
        ("AGENTS.md", "D"),
        ("CLAUDE.md", "M"),
    }
    assert result is GateResult.PASS


def test_the_collector_lets_a_branch_delete_and_restore_its_own_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "rm", "-q", "CLAUDE.md")
    _commit(repo, "delete")
    _write(repo, "CLAUDE.md", "# back\n")
    _commit(repo, "restore")
    assert _collected(tmp_path, repo)[0] is GateResult.PASS


def test_merging_a_base_that_added_agents_md_passes(tmp_path: Path) -> None:
    """A merge of the base shows the base's new AGENTS.md as an add against the merge's
    first parent; the merge base has it, so it is the repository's own."""
    repo = _repo(tmp_path, agents_md=False)
    _write(repo, "src/a.py", "a = 1\n")
    _commit(repo, "work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "AGENTS.md", "# the project's new own\n")
    _commit(repo, "base adds AGENTS.md")
    # Model a preparation that sees the newer upstream base.
    (tmp_path / "output/prepared-base.txt").write_text(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "crucible/test")
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert _collected(tmp_path, repo)[0] is GateResult.PASS


def test_the_collector_catches_an_added_or_shim_filled_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "AGENTS.md", SHIM)
    _write(repo, "sub/CLAUDE.md", "new\n")
    _commit(repo, "shim and new file")
    result, diff, _ = _collected(tmp_path, repo)
    assert {c["blob"] for c in diff["changes"] if c["path"] == "AGENTS.md"} == {SHIM_BLOB}
    assert result is GateResult.FAIL


def _side_merge(repo: Path) -> None:
    """Start a no-commit merge of a side branch, as a worker resolving a merge would."""
    _git(repo, "checkout", "-q", "-b", "side", "main")
    _write(repo, "side.txt", "side\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "crucible/test")
    _write(repo, "src/a.py", "a = 1\n")
    _commit(repo, "work")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")


def test_a_shim_a_merge_adds_and_a_later_commit_deletes_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, agents_md=False)
    _side_merge(repo)
    _write(repo, "AGENTS.md", SHIM)
    _commit(repo, "merge side")
    _git(repo, "rm", "-q", "AGENTS.md")
    _commit(repo, "drop it")
    assert _collected(tmp_path, repo)[0] is GateResult.FAIL


def test_a_merge_overwriting_the_bases_claude_md_with_the_shim_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _side_merge(repo)
    _write(repo, "CLAUDE.md", SHIM)
    _commit(repo, "merge side")
    _write(repo, "CLAUDE.md", "# the project's own, edited\n")
    _commit(repo, "edit it")
    assert _collected(tmp_path, repo)[0] is GateResult.FAIL


def test_a_backdated_add_edit_delete_of_a_new_agents_md_around_a_merge_fails(
    tmp_path: Path,
) -> None:
    """By commit date the add sorts before the edit, so the edit would look like the
    oldest record and the base would seem to own AGENTS.md; the graph order says not."""
    repo = _repo(tmp_path, agents_md=False)
    _write(repo, "AGENTS.md", "# notes, not a shim\n")
    _commit(repo, "add", date="2030-01-01T00:00:00Z")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "side.txt", "side\n")
    _commit(repo, "side", date="2030-01-02T00:00:00Z")
    _git(repo, "checkout", "-q", "crucible/test")
    _write(repo, "AGENTS.md", "# notes, edited\n")
    _commit(repo, "edit", date="2000-01-01T00:00:00Z")
    _git(repo, "merge", "-q", "--no-edit", "side", date="2030-01-03T00:00:00Z")
    _git(repo, "rm", "-q", "AGENTS.md")
    _commit(repo, "delete", date="2030-01-04T00:00:00Z")
    result, _, bundle = _collected(tmp_path, repo)
    assert [c["status"] for c in bundle["commit_changes"]][-1] == "A"
    assert result is GateResult.FAIL


def test_a_shim_in_a_non_ascii_directory_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "d\u00ef/CLAUDE.md", SHIM)
    _commit(repo, "non-ascii")
    result, diff, _ = _collected(tmp_path, repo)
    assert {c["path"] for c in diff["changes"]} == {"d\u00ef/CLAUDE.md"}
    assert result is GateResult.FAIL


def test_a_symlink_replacing_the_bases_claude_md_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").unlink()
    (repo / "CLAUDE.md").symlink_to(f"{IDENTITY_MOUNT}/IDENTITY.md")
    _commit(repo, "link it")
    result, diff, _ = _collected(tmp_path, repo)
    assert [(c["path"], c["status"]) for c in diff["changes"]] == [("CLAUDE.md", "T")]
    assert result is GateResult.FAIL


def test_the_raw_reader_never_reads_a_path_as_a_record(tmp_path: Path) -> None:
    """A path shaped like a raw record is still a path, and the next record still reads."""
    meta = f":000000 100644 {ZERO} {OTHER} A"
    raw = tmp_path / "raw.txt"
    raw.write_bytes(f"{meta}\0{meta}\0{meta}\0CLAUDE.md\0".encode())
    changes = read_path_changes(raw)
    assert changes is not None
    assert [c.path for c in changes] == [meta, "CLAUDE.md"]


def test_a_worker_set_log_diff_merges_does_not_change_the_verdict(tmp_path: Path) -> None:
    """`git log -m` follows log.diffMerges from the worker-writable .git/config, and
    `combined` prints a merge as `::` records the reader skips; the collector names its
    merge format, so the shim the merge wrote still fails."""
    repo = _repo(tmp_path, agents_md=False)
    _git(repo, "config", "log.diffMerges", "combined")
    _side_merge(repo)
    _write(repo, "AGENTS.md", SHIM)
    _commit(repo, "merge side")
    _git(repo, "rm", "-q", "AGENTS.md")
    _commit(repo, "drop it")
    result, _, bundle = _collected(tmp_path, repo)
    assert {c["status"] for c in bundle["commit_changes"]} == {"A", "D"}
    assert result is GateResult.FAIL


def test_an_inflated_listing_with_a_shim_past_the_read_limit_fails(tmp_path: Path) -> None:
    """40,000 long-named files make changed.txt and commit-paths.txt larger than what is
    read of them, sorted so zzz/CLAUDE.md falls past the cut. The raw records name only
    injected-name paths and still carry the shim, and the gate fails on the cut lists."""
    repo = _repo(tmp_path)
    empty = _git(repo, "hash-object", "-w", "--stdin").strip()
    shim = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=SHIM,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    entries = "".join(f"100644 {empty}\tpad/{n:05d}-{'x' * 240}\n" for n in range(40_000))
    entries += f"100644 {shim}\tzzz/CLAUDE.md\n"
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--index-info"],
        input=entries,
        text=True,
        check=True,
    )
    _git(repo, "commit", "-q", "--no-verify", "-m", "inflate")
    result, diff, bundle = _collected(tmp_path, repo)
    assert set(diff["over_limit"]) == {"changed.txt", "commit-paths.txt"}
    assert "zzz/CLAUDE.md" not in diff["paths"]
    assert "zzz/CLAUDE.md" not in bundle["commit_paths"]
    assert [(c["path"], c["blob"]) for c in diff["changes"]] == [("zzz/CLAUDE.md", shim)]
    assert result is GateResult.FAIL


def test_lists_over_their_read_limit_fail_the_gate() -> None:
    changes = [_change("src/a.py", "M")]
    diff = {"paths": ["src/a.py"], "changes": [], "over_limit": ["changed.txt"]}
    assert _outcome(diff, ["src/a.py"], changes) is GateResult.FAIL
    del diff["over_limit"]
    assert _outcome(diff, ["src/a.py"], []) is GateResult.PASS


def test_a_shim_in_an_orphan_root_hidden_by_log_show_root_fails(tmp_path: Path) -> None:
    """log.showRoot=false hides a root commit's adds; an orphan root holding the shim,
    merged and deleted in the merge, would show only the deletion as if the base had it."""
    repo = _repo(tmp_path, agents_md=False)
    _git(repo, "config", "log.showRoot", "false")
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    _git(repo, "rm", "-rq", "--cached", ".")
    (repo / "CLAUDE.md").unlink()
    _write(repo, "AGENTS.md", SHIM)
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-q", "--no-verify", "-m", "orphan root")
    _git(repo, "checkout", "-q", "-f", "crucible/test")
    _git(repo, "merge", "-q", "--no-commit", "--allow-unrelated-histories", "orphan")
    _git(repo, "rm", "-q", "-f", "AGENTS.md")
    _commit(repo, "merge orphan")
    result, _, bundle = _collected(tmp_path, repo)
    assert ("AGENTS.md", "A") in {(c["path"], c["status"]) for c in bundle["commit_changes"]}
    assert result is GateResult.FAIL


def _replace_with_fake(repo: Path, real: str, tree_of: str) -> None:
    """`git replace` the commit `real` with one carrying the tree of `tree_of` and the
    same parents, as a worker hiding a commit from the collector would."""
    parents = _git(repo, "rev-parse", f"{real}^@").split()
    args = [arg for parent in parents for arg in ("-p", parent)]
    fake = _git(repo, "commit-tree", f"{tree_of}^{{tree}}", *args, "-m", "fake").strip()
    _git(repo, "replace", real, fake)


def test_a_replaced_tip_hiding_a_shim_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, agents_md=False)
    _write(repo, "AGENTS.md", SHIM)
    _commit(repo, "shim")
    _replace_with_fake(repo, _git(repo, "rev-parse", "HEAD").strip(), "main")
    result, diff, _ = _collected(tmp_path, repo)
    assert "AGENTS.md" in diff["paths"]
    assert result is GateResult.FAIL


def test_a_replaced_commit_hiding_a_shim_in_the_history_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, agents_md=False)
    _write(repo, "AGENTS.md", SHIM)
    _commit(repo, "shim")
    shim_commit = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "rm", "-q", "AGENTS.md")
    _commit(repo, "drop it")
    _replace_with_fake(repo, shim_commit, "main")
    result, _, bundle = _collected(tmp_path, repo)
    assert "AGENTS.md" in bundle["commit_paths"]
    assert result is GateResult.FAIL


def test_the_diff_evidence_keeps_only_injected_name_records(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "CLAUDE.md", "# edited\n")
    _write(repo, "src/a.py", "a = 1\n")
    _commit(repo, "edit")
    result, diff, _ = _collected(tmp_path, repo)
    assert [c["path"] for c in diff["changes"]] == ["CLAUDE.md"]
    assert set(diff["paths"]) == {"CLAUDE.md", "src/a.py"}
    assert result is GateResult.PASS


def test_a_graft_cannot_hide_a_committed_shim(tmp_path: Path) -> None:
    repo = _repo(tmp_path, agents_md=False)
    base = _git(repo, "rev-parse", "main").strip()
    _write(repo, "AGENTS.md", SHIM)
    _commit(repo, "shim")
    shim = _git(repo, "rev-parse", "HEAD").strip()
    _write(repo, "w", "work\n")
    _commit(repo, "work")
    _write(repo, ".git/info/grafts", f"{base} {shim}\n")
    result, diff, bundle = _collected(tmp_path, repo)
    assert "AGENTS.md" in diff["paths"]
    assert "AGENTS.md" in bundle["commit_paths"]
    assert result is GateResult.FAIL


@pytest.mark.parametrize("resume", [False, True])
def test_moving_main_cannot_hide_a_shim_after_real_preparation(
    tmp_path: Path, resume: bool
) -> None:
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    origin = _repo(origin_dir, agents_md=False)
    base = _git(origin, "rev-parse", "main").strip()
    work = tmp_path / "work"
    script = scripts.preparer_script(
        url=str(origin),
        base_ref="main",
        work_branch="crucible/test",
        from_remote_branch=resume,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder="crucible-no-remote://nowhere",
        claude_md_wins=False,
        shims=("AGENTS.md",),
        exclude_entries=("/AGENTS.md",),
        identity_mount=IDENTITY_MOUNT,
    ).replace(WORK_MOUNT, str(work))
    prepared = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    assert prepared.returncode == 0, prepared.stderr
    assert (work / "output/prepared-base.txt").read_text().strip() == base
    repo = work / "repo"
    assert (repo / "AGENTS.md").read_text() == SHIM
    _git(repo, "add", "-f", "AGENTS.md")
    _commit(repo, "shim")
    _git(repo, "branch", "-f", "main", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _write(repo, "w", "work\n")
    _commit(repo, "work")
    result, diff, bundle = _collected(work, repo)
    assert result is GateResult.FAIL
    assert "AGENTS.md" in diff["paths"]
    assert "AGENTS.md" in bundle["commit_paths"]
    assert (work / "output/base.txt").read_text().strip() == base


@pytest.mark.parametrize("record", [None, "", "main", "0" * 40, "tree"])
def test_missing_or_invalid_prepared_base_fails_closed(tmp_path: Path, record: str | None) -> None:
    repo = _repo(tmp_path)
    _write(repo, "w", "work\n")
    _commit(repo, "work")
    assert _collected(tmp_path, repo)[0] is GateResult.PASS
    prepared_base = tmp_path / "output/prepared-base.txt"
    if record is None:
        prepared_base.unlink()
    else:
        prepared_base.write_text(
            _git(repo, "rev-parse", "main^{tree}") if record == "tree" else record
        )
    evidence = _evidence(tmp_path, repo, expected_exit=1)
    gi = GateInput(contract=contract_document(), policy={}, head_sha="a" * 40, evidence=evidence)
    assert evaluate_gate(GateName.NO_INJECTED_FILES, gi).result is GateResult.FAIL
    assert "prepared base commit" in (tmp_path / "output/collection-failed.txt").read_text()
    assert not (tmp_path / "output/collector.ok").exists()
    for name in ("diff-raw.txt", "commit-raw.txt", "base-injected.txt"):
        assert not (tmp_path / "output" / name).exists()


@pytest.mark.parametrize(
    "name",
    [
        "Agents.md",
        "AGENTS.MD",
        "\u0410GENTS.md",
        "AGENT\u0405.md",
        "AGE\u039dTS.md",
        "AGENTS\u0410.md",
        "CLAUDE\u200b.md",
        "AGENTS.override.md",
        "CLAUDE.local.md",
        "GEMINI.md",
        ".claude/settings.json",
        ".codex/config.toml",
        ".hermes/SOUL.md",
        ".gemini/settings.json",
        ".CRUCIBLE/x",
        "crucible/Identity/x",
        "nested/Agents.md",
        "nested/.ClAuDe/settings.json",
        "nested/CLAUDE\u2060.local.MD",
        "nested/GEMINI\ufe0f.md",
        "AGENTS\n.local.md",
        ".claude/.\u200b./settings.json",
    ],
)
@pytest.mark.parametrize("history_only", [False, True])
def test_issue_400_instruction_names_through_collector(
    tmp_path: Path,
    name: str,
    history_only: bool,
) -> None:
    repo = _repo(tmp_path)
    _write(repo, name, "# instructions\n")
    _commit(repo, "add instruction")
    if history_only:
        _git(repo, "rm", "-q", name)
        _commit(repo, "remove instruction")
    assert _collected(tmp_path, repo)[0] is GateResult.FAIL


@pytest.mark.parametrize("name", [".claude", "nested/.CoDeX", ".hermes", ".gemini", ".crucible"])
def test_issue_400_directory_symlink_through_collector(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path)
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to("elsewhere", target_is_directory=True)
    _commit(repo, "directory symlink")
    assert _collected(tmp_path, repo)[0] is GateResult.FAIL


@pytest.mark.parametrize("suffix", ["", "\r\n", " \t\n", "\t\r\n\r\n"])
@pytest.mark.parametrize("history_only", [False, True])
def test_issue_400_near_shim_edit_through_collector(
    tmp_path: Path,
    suffix: str,
    history_only: bool,
) -> None:
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_bytes((injected_shim_text() + suffix).encode())
    _commit(repo, "near shim")
    if history_only:
        _write(repo, "CLAUDE.md", "# ordinary project instructions\n")
        _commit(repo, "ordinary edit")
    result, _, bundle = _collected(tmp_path, repo)
    assert result is GateResult.FAIL
    assert any(c["classification"] == "shim" for c in bundle["commit_changes"])


def test_issue_400_plain_edit_through_collector(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "CLAUDE.md", "# Follow the project conventions.\n")
    _commit(repo, "plain edit")
    assert _collected(tmp_path, repo)[0] is GateResult.PASS


@pytest.mark.parametrize("bad_name", [False, True])
def test_issue_400_unclassifiable_input_fails_with_reason(tmp_path: Path, bad_name: bool) -> None:
    repo = _repo(tmp_path)
    if bad_name:
        name = os.fsdecode(b"ordinary-\xff.txt")
        (repo / name).write_bytes(b"ordinary file")
        reason = "undecodable name"
    else:
        (repo / "CLAUDE.md").write_bytes(b"\xff invalid UTF-8")
        reason = "unreadable blob"
    _commit(repo, "unclassifiable input")
    gi = GateInput(
        contract=contract_document(),
        policy={},
        head_sha="a" * 40,
        evidence=_evidence(tmp_path, repo),
    )
    outcome = evaluate_gate(GateName.NO_INJECTED_FILES, gi)
    assert outcome.result is GateResult.FAIL
    assert reason in outcome.detail


def test_issue_400_missing_blob_fails_with_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "CLAUDE.md", "# a new blob to remove\n")
    _commit(repo, "edit")
    blob = _git(repo, "rev-parse", "HEAD:CLAUDE.md").strip()
    (repo / ".git/objects" / blob[:2] / blob[2:]).unlink()
    gi = GateInput(
        contract=contract_document(),
        policy={},
        head_sha="a" * 40,
        evidence=_evidence(tmp_path, repo),
    )
    outcome = evaluate_gate(GateName.NO_INJECTED_FILES, gi)
    assert outcome.result is GateResult.FAIL
    assert "unreadable blob" in outcome.detail
    assert blob in outcome.detail


def test_issue_400_spelling_variant_cannot_borrow_base_exemption(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "mv", "CLAUDE.md", "claude.md")
    _commit(repo, "rename")
    assert _collected(tmp_path, repo)[0] is GateResult.FAIL


@pytest.mark.parametrize("name", ["d\u00ef/CLAUDE.local.md", "di\u0308/CLAUDE.local.md"])
def test_issue_400_existing_unicode_path_plain_edit_passes(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "main")
    _write(repo, name, "# existing instructions\n")
    _commit(repo, "base instructions")
    (tmp_path / "output/prepared-base.txt").write_text(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "crucible/test")
    _git(repo, "merge", "-q", "main")
    _write(repo, name, "# plain edit\n")
    _commit(repo, "edit")
    assert _collected(tmp_path, repo)[0] is GateResult.PASS


def test_issue_400_unknown_classification_fails_closed(tmp_path: Path) -> None:
    raw = tmp_path / "diff-raw.txt"
    raw.write_text(
        '{"version":1,"changes":[{"path":"CLAUDE.md","status":"M",'
        '"blob":"abc","classification":"unknown"}]}'
    )
    changes = read_path_changes(raw)
    assert changes is not None
    assert "unknown content classification" in changes[0].classification
