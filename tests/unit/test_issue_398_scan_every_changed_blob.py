"""hades #398: every byte the worker added or changed is scanned.

The scanner used to read the first 8 MB of diff.patch, so a large file ahead of a secret
hid it, and a path the worker's .gitattributes marked binary, or a file git called binary
for a NUL byte, showed only "Binary files differ". The collector now exports every added
or changed blob by object id, and the service streams each one, and the whole patch,
through the scanner in chunks that overlap."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import collected
from crucible.adapters.execution import kubernetes as kubernetes_module
from crucible.adapters.execution.collected import BlobTarScan, read_outputs, scan_changed_content
from crucible.adapters.execution.kubernetes import CollectionFailedError
from crucible.adapters.execution.scripts import CHANGED_BLOBS_DIR, collector_script
from crucible.application.evidence import _scanner_findings
from crucible.domain.gates import GateResult, no_secrets
from crucible.domain.secrets import SCAN_OVERLAP, scan_chunks, scan_text
from crucible.ports.execution import WORK_MOUNT, CollectedOutputs, LaunchSpec, ObservationState
from tests.collector_tools import collector_env
from tests.unit.kubernetes_fixtures import build, spec

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
    assert any(
        f == {"where": "diff:z.txt", "pattern": "github_token", "excerpt": "ghp...AAA"}
        for f in findings
    )
    assert not any(f["where"] == "diff:a.txt" for f in findings)
    # The gate fails and says where.
    gate = no_secrets(_Input({"findings": findings, "diff_scanned": True}))  # type: ignore[arg-type]
    assert gate.result is GateResult.FAIL
    assert "diff:z.txt" in gate.detail


# Binary files have no added diff lines, so they are outside the added-line scope.


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
    assert findings == []


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


def test_scanning_finds_an_added_line_after_a_large_line(tmp_path: Path) -> None:
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
    assert {m.path for m in found} == {"diff:z.txt"}
    assert peak < total * 2


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


# Review of the first attempt: a match that touches a chunk boundary waits for the next
# character, as `scan_text` sees it, instead of being taken when it is long.


def test_a_long_run_that_ends_at_a_chunk_boundary_waits_for_the_next_character() -> None:
    run = "gh" + "p_" + "A" * (SCAN_OVERLAP + 100)
    # A letter after the run: no trailing word boundary, so no match, whole or chunked.
    assert scan_text(run + "é") is None
    assert scan_chunks([run, "é" + "x" * 10]) is None
    assert scan_chunks([run, "_" + "x" * 10]) is None
    # A space, the end of the text, or more of the run and then a space: a match.
    assert scan_chunks([run, " x"]) == "github_token"
    assert scan_chunks([run]) == "github_token"
    assert scan_chunks([run, "BBBB", "BBBB", " x"]) == "github_token"
    assert scan_chunks([run, "BBBB", "BBBB", "éx"]) is None
    # The same with the run split so that every boundary falls inside it.
    pieces = [run[i : i + 1000] for i in range(0, len(run), 1000)]
    assert scan_chunks([*pieces, "é"]) is None
    assert scan_chunks([*pieces, " "]) == "github_token"


def test_a_held_match_is_bounded_and_taken_as_it_stands_past_the_hold() -> None:
    # Under the hold the next character decides; past it the match stands (no secret is
    # that long, and the window must stay bounded).
    head = "gh" + "p_" + "A" * 100
    assert scan_chunks([head, "A" * 100, "é"], overlap=64, hold=256) is None
    assert scan_chunks([head, "A" * 100, " "], overlap=64, hold=256) == "github_token"
    assert scan_chunks([head, "A" * 200, "é"], overlap=64, hold=256) == "github_token"


# Review of the first attempt: the exported blobs are not a second copy of the bundle's
# content in the Kubernetes output archive; the reader streams them through the scanner.


def _blob_id(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def _raw_record(blob: str, path: str) -> bytes:
    return f":000000 100644 {'0' * 40} {blob} A\0{path}\0".encode()


def test_the_reader_scripts_keep_the_blobs_out_of_the_archive_and_stream_them_alone(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    (work / "output" / CHANGED_BLOBS_DIR).mkdir(parents=True)
    (work / "output" / "tree").mkdir()
    (work / "verify").mkdir()
    content = b"\0\0binary " + KEY.encode() + b"\n"
    blob = _blob_id(content)
    (work / "output" / CHANGED_BLOBS_DIR / blob).write_bytes(content)
    (work / "output" / "diff.patch").write_bytes(b"diff\n")
    (work / "output" / "tree" / "x").write_bytes(b"clone\n")
    (work / "verify" / "V1.log").write_bytes(b"ok\n")

    def run(script: str) -> bytes:
        done = subprocess.run(
            ["sh", "-c", script.replace(WORK_MOUNT, str(work))], capture_output=True, check=False
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    with tarfile.open(fileobj=io.BytesIO(run(kubernetes_module._OUTPUT_TAR_SCRIPT))) as tar:
        names = set(tar.getnames())
    assert "output/diff.patch" in names and "verify/V1.log" in names
    assert not any(
        name.startswith(("output/tree", f"output/{CHANGED_BLOBS_DIR}")) for name in names
    )

    stream = run(kubernetes_module._CHANGED_BLOBS_TAR_SCRIPT)
    with tarfile.open(fileobj=io.BytesIO(stream)) as tar:
        regular = {m.name for m in tar.getmembers() if m.isreg()}
    assert regular == {f"output/{CHANGED_BLOBS_DIR}/{blob}"}
    # The sink scans the stream as it arrives, in whatever pieces the exec hands it.
    scan = BlobTarScan(CHANGED_BLOBS_DIR)
    for at in range(0, len(stream), 777):
        scan.write(stream[at : at + 777])
    assert scan.close() == {blob: "github_token"}
    assert scan.error is None
    # Without the directory the second script has nothing to say.
    shutil.rmtree(work / "output" / CHANGED_BLOBS_DIR)
    assert run(kubernetes_module._CHANGED_BLOBS_TAR_SCRIPT) == b""


def test_the_blob_sink_keeps_verdicts_not_bytes_and_refuses_what_is_not_the_blob() -> None:
    clean = b"x" * (12 * collected.SCAN_CHUNK) + b"\n"
    keyed = b"y" * (8 * collected.SCAN_CHUNK) + b" " + KEY.encode() + b"\n"
    swapped = b"not what the id says\n"
    members = {
        _blob_id(clean): clean,
        _blob_id(keyed): keyed,
        _blob_id(b"the real content\n"): swapped,
        "not-a-blob-id": b"ignored\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"output/{CHANGED_BLOBS_DIR}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"output/{CHANGED_BLOBS_DIR}/{'f' * 40}")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    stream = buffer.getvalue()
    tracemalloc.start()
    try:
        scan = BlobTarScan(CHANGED_BLOBS_DIR)
        for at in range(0, len(stream), 64 * 1024):
            scan.write(stream[at : at + 64 * 1024])
        results = scan.close()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert results == {
        _blob_id(clean): None,
        _blob_id(keyed): "github_token",
        _blob_id(b"the real content\n"): False,
        "f" * 40: False,
    }
    # A few chunks, whatever the stream's size: the bytes are never kept.
    assert peak < 8 * collected.SCAN_CHUNK
    assert peak < len(stream) // 2
    # A stream cut short: the blob it cut does not hash and is not scanned, and the ones
    # that never arrived are absent, so the gate waits on both.
    scan = BlobTarScan(CHANGED_BLOBS_DIR)
    scan.write(stream[: len(stream) // 2])
    cut = scan.close()
    assert cut.get(_blob_id(clean), False) is False
    assert _blob_id(keyed) not in cut


def test_results_a_provider_scanned_itself_stand_in_for_files_on_disk(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "diff.patch").write_bytes(b"diff\n")
    keyed, clean, gone = "a" * 40, "b" * 40, "c" * 40
    (output / "diff-raw.txt").write_bytes(
        _raw_record(keyed, "z.txt") + _raw_record(clean, "a.txt") + _raw_record(gone, "lost.txt")
    )
    found, unscanned = scan_changed_content(
        output, {keyed: "github_token", clean: None, "d" * 40: "jwt"}
    )
    assert found == ()
    assert unscanned == ("lost.txt",)
    assert not (output / CHANGED_BLOBS_DIR).exists()


async def test_kubernetes_scans_a_large_blob_without_a_second_copy_in_the_archive(
    monkeypatch: Any,
) -> None:
    """The bundle carries a new blob once. A 130 MiB clean binary used to fit the 256 MiB
    archive; a second copy under changed-blobs pushed it over before no_secrets ran."""
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    observation = await provider.observe(handle)
    while observation.state is ObservationState.RUNNING:
        observation = await provider.observe(handle)
    content = bytes(range(256)) * 2400 + KEY.encode() + b"\n"
    blob = _blob_id(content)
    claim = api.claims["ws-01attempt0000000000000000a"]
    claim["output/work_branch.bundle"] = b"\xff" * len(content)
    claim[f"output/{CHANGED_BLOBS_DIR}/{blob}"] = content
    claim["output/diff-raw.txt"] = _raw_record(blob, "big.bin")
    # The archive holds the bundle and the small files; the archive and the blob
    # together would not fit.
    monkeypatch.setattr(kubernetes_module, "OUTPUT_READ_LIMIT", len(content) + 64 * 1024)
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.diff_unscanned == ()
    assert outputs.diff_findings == ()
    # The blob stream has its own bound, and a stream cut at it fails the collection.
    monkeypatch.setattr(kubernetes_module, "CHANGED_BLOBS_READ_LIMIT", len(content) // 2)
    with pytest.raises(CollectionFailedError, match="changed blobs exceeded"):
        await provider.collect(handle, workspace, launch)
