"""hades #398: every byte the worker added or changed is scanned.

The scanner used to read the first 8 MB of diff.patch, so a large file ahead of a secret
hid it, and a path the worker's .gitattributes marked binary, or a file git called binary
for a NUL byte, showed only "Binary files differ". The collector now exports every added
or changed blob by object id, and the service streams each one, and the whole patch,
through the scanner in chunks that overlap."""

from __future__ import annotations

import subprocess
import tracemalloc
from pathlib import Path
from typing import Any

from crucible.adapters.execution import collected
from crucible.adapters.execution.collected import read_outputs, scan_changed_content
from crucible.adapters.execution.scripts import CHANGED_BLOBS_DIR, collector_script
from crucible.application.evidence import _scanner_findings
from crucible.domain.gates import GateResult, no_secrets
from crucible.domain.secrets import SCAN_OVERLAP, scan_chunks, scan_text
from crucible.ports.execution import CollectedOutputs, LaunchSpec
from tests.collector_tools import collector_env

GIT_USER = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
# Built here at runtime; no secret-shaped literal is ever committed (12).
KEY = "gh" + "p_" + "A" * 36


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *GIT_USER, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _repository(tmp_path: Path, files: dict[str, bytes]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "crucible/test")
    for name, content in files.items():
        (repo / name).write_bytes(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


def _collect(tmp_path: Path, repo: Path) -> Path:
    """Run the real collector script against `repo`, as test_issue_344 does."""
    report = tmp_path / "worker-report"
    output = tmp_path / "output"
    report.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    (output / "prepared-base.txt").write_text(_git(repo, "rev-parse", "main"))
    script = collector_script(
        base_ref="main", work_branch="crucible/test", size_cap_bytes=1024 * 1024
    )
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


def _findings(tmp_path: Path, output: Path) -> list[dict[str, str]]:
    read = read_outputs(
        output,
        tmp_path / "verify",
        spec=_spec(),
        bundle_verified=False,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )
    assert read.diff_unscanned == ()
    outputs = CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        diff_paths=read.diff_paths,
        diff_findings=read.diff_findings,
        diff_unscanned=read.diff_unscanned,
    )
    return _scanner_findings(outputs, None)


class _Item:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.id = "E1"
        self.payload = payload


class _Input:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.item = _Item(payload)

    def one(self, kind: str) -> _Item | None:
        return self.item if kind == "scanner_result" else None


# AC1: a large file ahead of the secret no longer hides it.


def test_a_key_after_a_9_mb_text_file_is_found_and_names_its_file(tmp_path: Path) -> None:
    filler = b"".join(b"filler line %08d\n" % i for i in range(9 * 1024 * 1024 // 20))
    assert len(filler) > 9 * 1000 * 1000
    repo = _repository(tmp_path, {"a.txt": filler, "z.txt": f'TOKEN = "{KEY}"\n'.encode()})
    output = _collect(tmp_path, repo)
    # The old reader's window ends inside the filler.
    assert (output / "diff.patch").stat().st_size > collected.TEXT_LIMIT
    assert scan_text(collected.text(output / "diff.patch")) is None
    findings = _findings(tmp_path, output)
    assert {"where": "diff:z.txt", "pattern": "github_token"} in findings
    assert not any(f["where"] == "diff:a.txt" for f in findings)
    # The gate fails and says where.
    gate = no_secrets(_Input({"findings": findings, "diff_scanned": True}))  # type: ignore[arg-type]
    assert gate.result is GateResult.FAIL
    assert "diff:z.txt" in gate.detail


# AC2: attributes and NUL bytes do not keep content from the scanner.


def test_a_binary_attributed_file_and_a_nul_bearing_file_are_both_scanned(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {
            ".gitattributes": b"marked.txt binary\n",
            "marked.txt": f"{KEY}\n".encode(),
            "nul.bin": b"head\0\0\0" + KEY.encode() + b"\n",
        },
    )
    output = _collect(tmp_path, repo)
    patch = (output / "diff.patch").read_text(encoding="utf-8", errors="replace")
    # git itself shows neither as text, which is what the old scan read.
    assert KEY not in patch
    findings = _findings(tmp_path, output)
    wheres = {f["where"] for f in findings}
    assert "diff:marked.txt" in wheres
    assert "diff:nul.bin" in wheres


def test_a_textconv_driver_the_worker_set_does_not_change_what_is_scanned(
    tmp_path: Path,
) -> None:
    repo = _repository(
        tmp_path,
        {".gitattributes": b"*.txt diff=hide\n", "z.txt": f"{KEY}\n".encode()},
    )
    _git(repo, "config", "diff.hide.textconv", "true")
    output = _collect(tmp_path, repo)
    assert "diff:z.txt" in {f["where"] for f in _findings(tmp_path, output)}


def test_a_clean_change_has_no_findings_and_every_blob_is_exported(tmp_path: Path) -> None:
    repo = _repository(tmp_path, {"a.txt": b"nothing here\n", "b.txt": b"nor here\n"})
    output = _collect(tmp_path, repo)
    assert _findings(tmp_path, output) == []
    exported = {p.name for p in (output / CHANGED_BLOBS_DIR).iterdir()}
    assert exported == {_git(repo, "rev-parse", f"HEAD:{n}").strip() for n in ("a.txt", "b.txt")}


def test_a_changed_blob_the_collector_did_not_export_keeps_the_gate_waiting(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path, {"z.txt": b"plain\n"})
    output = _collect(tmp_path, repo)
    for blob in (output / CHANGED_BLOBS_DIR).iterdir():
        blob.write_bytes(b"not the blob\n")
    found, unscanned = scan_changed_content(output)
    assert found == ()
    assert unscanned == ("z.txt",)
    gate = no_secrets(
        _Input({"findings": [], "diff_scanned": False, "unscanned": list(unscanned)})  # type: ignore[arg-type]
    )
    assert gate.result is GateResult.PENDING
    assert "z.txt" in gate.detail


# AC3: the content is streamed in bounded chunks, never held whole.


def test_scanning_holds_a_bounded_window_not_the_whole_diff(tmp_path: Path) -> None:
    filler = b"x" * (12 * 1024 * 1024) + b"\n"
    repo = _repository(tmp_path, {"a.txt": filler, "z.txt": f"{KEY}\n".encode()})
    output = _collect(tmp_path, repo)
    total = (output / "diff.patch").stat().st_size + sum(
        p.stat().st_size for p in (output / CHANGED_BLOBS_DIR).iterdir()
    )
    assert total > 24 * 1024 * 1024
    tracemalloc.start()
    try:
        found, unscanned = scan_changed_content(output)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found is not None
    assert unscanned == ()
    assert {m.path for m in found} == {"diff", "diff:z.txt"}
    assert peak < 8 * collected.SCAN_CHUNK
    assert peak < total // 3


def test_a_match_split_across_chunks_is_found() -> None:
    text = "x" * 1000 + " " + KEY + " " + "y" * 1000
    for at in range(990, 1050):
        assert scan_chunks([text[:at], text[at:]], overlap=64) == "github_token", at
    pieces = [text[i : i + 7] for i in range(0, len(text), 7)]
    assert scan_chunks(pieces, overlap=64) == "github_token"


def test_a_chunk_boundary_does_not_invent_a_word_boundary() -> None:
    # "x" right before the key's prefix: no match whole, so none in chunks either.
    glued = "x" * 20 + KEY + " "
    assert scan_text(glued) is None
    for at in range(1, len(glued)):
        assert scan_chunks([glued[:at], glued[at:]], overlap=4) is None, at
    # A key that runs on past the end of a chunk is judged with what follows it.
    run_on = "AKIA" + "A" * 17 + " "
    assert scan_text(run_on) is None
    assert scan_chunks([run_on[:20], run_on[20:]], overlap=SCAN_OVERLAP) is None
