"""Reading what the collector, the bundle verifier, and the verifier wrote (08, 11).

The collector runs in a throwaway container and leaves its answers as plain files in the
attempt's output directory. Those files are the same on every provider, because the
script that writes them is the same script (`scripts.collector_script`); only how the
directory reaches Crucible differs, a shared volume on Docker and a tar off the
workspace claim through a reader Pod on Kubernetes (26).

Everything here treats what it reads as data: it is a tree a worker influenced.
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import json
import os
import re
import tarfile
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import yaml

from crucible.adapters.execution import scripts
from crucible.adapters.execution.injected_collection import classify_collected, nul_fields
from crucible.adapters.execution.workspace import harness_private_path
from crucible.contracts.evidence import REVIEW_DIFF_NAME, REVIEW_DIFF_TYPE
from crucible.domain.secrets import SecretMatch, match_text, scan_chunks
from crucible.ports.execution import (
    BranchBundle,
    CollectedArtifact,
    CommitPolicyCheck,
    LaunchSpec,
    PathChange,
    VerificationRun,
)

__all__ = [
    "BlobTarScan",
    "Outputs",
    "lists_over_limit",
    "read_base_paths",
    "read_commit_policy",
    "read_outputs",
    "read_path_changes",
    "read_path_list",
    "read_verifications",
    "scan_blob",
    "scan_changed_content",
    "tail",
    "text",
]


@dataclass(frozen=True, slots=True)
class Outputs:
    report: dict[str, Any] | None
    report_raw: str | None
    blocked_md: str | None
    stdout_tail: str
    stderr_tail: str
    diff_paths: tuple[str, ...]
    diff_findings: tuple[SecretMatch, ...] | None
    bundle: BranchBundle | None
    artifacts: tuple[CollectedArtifact, ...]
    verifications: tuple[VerificationRun, ...]
    copy_rejections: tuple[dict[str, str], ...]
    checkpoint_refusal: str | None
    leftover_committed: bool = False
    leftover_note: str | None = None
    diff_changes: tuple[PathChange, ...] | None = None
    base_paths: tuple[str, ...] | None = None
    over_limit: tuple[str, ...] = ()
    diff_unscanned: tuple[str, ...] = ()


TEXT_LIMIT = 8 * 1024 * 1024


def text(path: Path, limit: int = TEXT_LIMIT) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def tail(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def read_outputs(
    output: Path,
    verify: Path,
    *,
    spec: LaunchSpec,
    bundle_verified: bool,
    collector_exit: int,
    verifications: tuple[VerificationRun, ...],
    tail_bytes: int,
    changed_blobs: Mapping[str, str | bool | None] | None = None,
) -> Outputs:
    """Read what the collector left under `output` and the verifier under `verify`.

    `changed_blobs` is what a provider that scanned the exported blobs itself, as they
    streamed past, found for each object id (hades #398): a Kubernetes reader hands them
    over without ever writing them to local disk. None means the blobs are files under
    `output` and are scanned from there."""
    report_dir = output / "report"
    report: dict[str, Any] | None = None
    report_raw: str | None = None
    report_file = report_dir / "report.yaml"
    if report_file.is_file():
        report_raw = text(report_file)
        try:
            parsed = yaml.safe_load(report_raw)
            report = parsed if isinstance(parsed, dict) else None
        except yaml.YAMLError:
            report = None
    blocked = report_dir / "blocked.md"
    blocked_md = text(blocked) if blocked.is_file() else None

    changed = read_path_list(output / "changed.txt")
    diff_findings, diff_unscanned = scan_changed_content(output, changed_blobs)
    commit_paths = read_path_list(output / "commit-paths.txt")
    diff_changes: tuple[PathChange, ...] | None
    commit_changes: tuple[PathChange, ...] | None
    if (output / "injected-blobs").is_dir():
        diff_changes, commit_changes = classify_collected(output)
    else:
        diff_changes = read_path_changes(output / "diff-raw.txt")
        commit_changes = read_path_changes(output / "commit-raw.txt")
    base_paths = read_base_paths(output / "base-injected.txt")
    over_limit = lists_over_limit(output)
    commit_policy = read_commit_policy(output / "commit-policy")
    messages: list[str] = []
    for record in text(output / "log.txt").split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) >= 2 and parts[0]:
            messages.append(parts[1])
    head = text(output / "head.txt").strip()
    commits_text = text(output / "commits.txt").strip() or "0"
    repository = spec.contract.get("repository", {})
    bundle_path = output / "work_branch.bundle"
    bundle = None
    if head and collector_exit == 0:
        bundle = BranchBundle(
            head_sha=head,
            base_ref=str(repository.get("base_ref", "main")),
            work_branch=text(output / "branch.txt").strip()
            or str(repository.get("work_branch", "")),
            commits=int(commits_text) if commits_text.isdigit() else 0,
            verified=bundle_verified,
            sha256=(
                hashlib.sha256(bundle_path.read_bytes()).hexdigest()
                if bundle_path.is_file()
                else ""
            ),
            commit_paths=commit_paths,
            commit_messages=tuple(messages),
            commit_policy=commit_policy,
            commit_changes=commit_changes,
        )

    artifacts: list[CollectedArtifact] = []
    if report_dir.is_dir():
        for path in sorted(p for p in report_dir.rglob("*") if p.is_file()):
            name = f"report/{path.relative_to(report_dir)}"
            if path.name in ("report.yaml", "blocked.md"):
                continue
            artifacts.append(
                CollectedArtifact(
                    name=name,
                    type="run_evidence",
                    content=path.read_bytes()[: 4 * 1024 * 1024],
                    content_type="text/plain",
                )
            )
    # hades #344: the collector's review diff, from its own directory, never a file the
    # worker wrote under report/.
    review_diff = output / scripts.REVIEW_DIFF_DIR / "diff.patch"
    if review_diff.is_file() and not review_diff.is_symlink():
        artifacts.append(
            CollectedArtifact(
                name=REVIEW_DIFF_NAME,
                type=REVIEW_DIFF_TYPE,
                content=review_diff.read_bytes()[: 4 * 1024 * 1024],
                content_type="text/x-diff",
            )
        )
    for run in verifications:
        artifacts.append(
            CollectedArtifact(
                name=f"verify/{run.id}.log",
                type="verification_log",
                content=run.log_tail.encode("utf-8"),
                content_type="text/plain",
            )
        )
    rejections: list[dict[str, str]] = []
    for line in text(output / "copy-rejections.tsv").splitlines():
        if "\t" in line:
            reason, path_text = line.split("\t", 1)
            rejections.append({"reason": reason, "path": path_text})
    return Outputs(
        report=report,
        report_raw=report_raw,
        blocked_md=blocked_md,
        stdout_tail=tail(output / "collector.ok", tail_bytes),
        stderr_tail=tail(output / "bundle.log", tail_bytes),
        diff_paths=changed,
        diff_findings=diff_findings,
        diff_unscanned=diff_unscanned,
        diff_changes=diff_changes,
        base_paths=base_paths,
        over_limit=over_limit,
        bundle=bundle,
        artifacts=tuple(artifacts),
        verifications=verifications,
        copy_rejections=tuple(rejections),
        checkpoint_refusal=text(output / "checkpoint-refusal.txt").strip() or None,
        leftover_committed=(output / "leftover-committed.txt").is_file(),
        leftover_note=text(output / "leftover-refusal.txt").strip() or None,
    )


# hades #398: how much of a collected file the scanner holds at once.
SCAN_CHUNK = 1024 * 1024
# A raw diff header: the new mode, the new blob, and the status.
_CHANGED_META = re.compile(rb":[0-7]{6} ([0-7]{6}) [0-9a-f]{40,64} ([0-9a-f]{40,64}) ([A-Z])[0-9]*")
_BLOB_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _stream(handle: IO[bytes]) -> Iterator[bytes]:
    while block := handle.read(SCAN_CHUNK):
        yield block


def _file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        yield from _stream(handle)


def _decoded(chunks: Iterable[bytes]) -> Iterator[str]:
    """Bytes as text, a chunk at a time; a character split across two reads is decoded
    whole."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    for block in chunks:
        yield decoder.decode(block)
    yield decoder.decode(b"", final=True)


def scan_blob(blob: str, size: int, chunks: Iterable[bytes]) -> str | bool | None:
    """Stream one exported blob through the scanner, never holding it whole.

    Returns the pattern the content matches, None for none, or False when the bytes are
    not that blob: they do not hash, as `blob <size>\\0` plus the content, to its object
    id. A copy a worker could have replaced, or a stream cut short, is therefore never
    taken as scanned."""
    digest = hashlib.sha1() if len(blob) == 40 else hashlib.sha256()
    digest.update(b"blob %d\0" % size)

    def hashed() -> Iterator[bytes]:
        for block in chunks:
            digest.update(block)
            yield block

    text_chunks = _decoded(hashed())
    hit = scan_chunks(text_chunks)
    # Read what is left after a match, so the hash covers the whole blob.
    for _ in text_chunks:
        pass
    return hit if digest.hexdigest() == blob else False


def _scan_blob_file(path: Path, blob: str) -> str | bool | None:
    """`scan_blob` over a file the collector left on disk; False when it is missing, not
    a regular file, or unreadable."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        return scan_blob(blob, path.stat().st_size, _file_chunks(path))
    except OSError:
        return False


class BlobTarScan:
    """A sink for a tar stream of exported blobs that scans each as it arrives.

    The Kubernetes reader hands the collected output back as a tar (26). The blobs the
    worker added or changed are in the bundle already, so they are not in that archive
    a second time (hades #398 review): the reader streams `output/changed-blobs` on its
    own, into this sink, which keeps the verdict for each blob and none of the bytes.
    `write` is what the exec stream calls; `close` ends the stream and returns the
    results, by object id: the pattern matched, None for none, False for content that
    is not that blob. A member that never arrived is simply absent, and the gate waits
    on it."""

    def __init__(self, directory: str) -> None:
        self._prefix = f"output/{directory}/"
        self.results: dict[str, str | bool | None] = {}
        self.error: str | None = None
        read_fd, write_fd = os.pipe()
        self._reader: IO[bytes] = os.fdopen(read_fd, "rb")
        self._writer: IO[bytes] | None = os.fdopen(write_fd, "wb")
        self._thread = threading.Thread(target=self._scan, name="crucible-blob-scan", daemon=True)
        self._thread.start()

    def write(self, data: bytes) -> int:
        if self._writer is None:
            return len(data)
        try:
            self._writer.write(data)
        except (BrokenPipeError, ValueError):
            # The scanner stopped (a malformed stream); let the exec drain and finish.
            self._writer = None
        return len(data)

    def close(self) -> dict[str, str | bool | None]:
        if self._writer is not None:
            with contextlib.suppress(OSError, ValueError):
                self._writer.close()
            self._writer = None
        self._thread.join()
        return dict(self.results)

    def _scan(self) -> None:
        try:
            with tarfile.open(fileobj=self._reader, mode="r|") as tar:
                for member in tar:
                    if not member.name.startswith(self._prefix):
                        continue
                    blob = member.name[len(self._prefix) :]
                    if not _BLOB_ID.fullmatch(blob):
                        continue
                    handle = tar.extractfile(member) if member.isreg() else None
                    if handle is None:
                        self.results[blob] = False
                        continue
                    with handle:
                        self.results[blob] = scan_blob(blob, member.size, _stream(handle))
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            # Closing the read end turns a writer still blocked on the pipe loose.
            with contextlib.suppress(OSError):
                self._reader.close()


def scan_changed_content(
    output: Path, blobs: Mapping[str, str | bool | None] | None = None
) -> tuple[tuple[SecretMatch, ...] | None, tuple[str, ...]]:
    """Scan only lines added relative to the prepared base.

    A zero-context decision is made from the collected patch: context and deleted lines
    are repository content the attempt did not add. Exported blobs remain integrity
    coverage for newly added files, but their complete contents are not judged because
    doing so would make an edit beside old secret-shaped fixture data fail (#488).
    """
    patch = output / "diff.patch"
    if not patch.is_file():
        return None, ()
    found: list[SecretMatch] = []
    unscanned: list[str] = []
    current_path = "diff"
    excluded = False
    try:
        with patch.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("+++ b/"):
                    current_path = line[6:].rstrip("\n")
                    excluded = harness_private_path(current_path)
                elif line.startswith("+") and not line.startswith("+++") and not excluded:
                    hit = match_text(line[1:], path=f"diff:{current_path}")
                    if hit is not None:
                        found.append(hit)
    except OSError:
        return None, ()
    raw = output / "diff-raw.txt"
    if not raw.is_file():
        # A collector before #398 exported no blobs; the patch is what it gave.
        return tuple(found), ()
    exported = output / scripts.CHANGED_BLOBS_DIR
    results: dict[str, str | bool | None] = {}
    try:
        fields = nul_fields(raw)
        for raw_header in fields:
            header = raw_header.lstrip(b"\n")
            if not header:
                continue
            match = _CHANGED_META.fullmatch(header)
            raw_path = next(fields, None)
            if match is None or not raw_path:
                raise ValueError("malformed raw diff record")
            mode, blob_bytes, status = match.groups()
            if status == b"D" or mode == b"160000":
                continue
            path = raw_path.decode("utf-8", "surrogateescape")
            shown = path if path.isprintable() else ascii(path)
            if harness_private_path(path):
                continue
            blob = blob_bytes.decode("ascii")
            # Only a new path's blob consists entirely of worker-added lines. For an
            # edit, the patch above is the authority and the old parts must not count.
            if status != b"A":
                continue
            if blob not in results:
                results[blob] = (
                    blobs.get(blob, False)
                    if blobs is not None
                    else _scan_blob_file(exported / blob, blob)
                )
            result = results[blob]
            if result is False:
                unscanned.append(shown)
            # Findings come from added patch lines so they include a safe excerpt.
    except (OSError, ValueError):
        unscanned.append("diff-raw.txt")
    return tuple(found), tuple(unscanned)


_RAW_META = re.compile(r":[0-7]{6} [0-7]{6} ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) ([A-Z])[0-9]*")
# Larger than this and the records are not read at all: `git log` prints the oldest
# records last, and a cut tail would hide the add that says the base lacked a path. The
# legacy collector writes only injected-name records; lists_over_limit makes the gate
# fail on a list this large. Modern raw records are streamed by classify_collected.
_RAW_LIMIT = 8 * 1024 * 1024


def read_path_changes(path: Path) -> tuple[PathChange, ...] | None:
    """Read #400 JSON path/content classifications, preserving Git's history order.

    Legacy #369 raw NUL-separated records remain readable. Missing/oversized files
    return None; the gate checks missing statuses and list limits. Malformed modern
    records carry an explicit classification error instead of granting an exemption.
    """
    try:
        if not path.is_file() or path.stat().st_size > _RAW_LIMIT:
            return None
    except OSError:
        return None
    content = text(path, _RAW_LIMIT)
    if content.startswith("{"):
        try:
            payload = json.loads(content)
            if payload["version"] != 1 or not isinstance(payload["changes"], list):
                raise ValueError("unsupported classification records")
            changes = []
            for record in payload["changes"]:
                if not isinstance(record, dict) or not all(
                    isinstance(record.get(key), str)
                    for key in ("path", "status", "blob", "classification")
                ):
                    raise ValueError("malformed classification record")
                classification = record["classification"]
                if classification not in ("plain", "shim", "deleted") and not (
                    classification.startswith("error:")
                ):
                    raise ValueError("unknown content classification")
                changes.append(PathChange(**record))
            return tuple(changes)
        except (ValueError, KeyError, TypeError) as exc:
            return (PathChange("", "", "", f"error: unreadable classification records: {exc}"),)
    out: list[PathChange] = []
    fields = content.split("\0")
    at = 0
    while at < len(fields) - 1:
        match = _RAW_META.fullmatch(fields[at].lstrip("\n"))
        if match is None:
            at += 1
            continue
        # The path is the next field, whatever it looks like: never read as a record.
        if fields[at + 1]:
            out.append(PathChange(path=fields[at + 1], status=match.group(3), blob=match.group(2)))
        at += 2
    return tuple(out)


# The path lists `no_injected_files` reads and how much of each is read (hades #369).
_PATH_LISTS: tuple[tuple[str, int], ...] = (
    ("changed.txt", TEXT_LIMIT),
    ("commit-paths.txt", TEXT_LIMIT),
    ("diff-raw.txt", _RAW_LIMIT),
    ("commit-raw.txt", _RAW_LIMIT),
    ("base-injected.txt", TEXT_LIMIT),
)


def lists_over_limit(output: Path) -> tuple[str, ...]:
    """The path lists larger than what is read of them (hades #369): their tail is cut
    or they are not read at all, so the gate cannot see every path and fails closed."""
    over: list[str] = []
    for name, limit in _PATH_LISTS:
        if name in ("diff-raw.txt", "commit-raw.txt") and (output / "injected-blobs").is_dir():
            # Modern raw records are streamed in full by classify_collected.
            continue
        try:
            if (output / name).stat().st_size > limit:
                over.append(name)
        except OSError:
            continue
    return tuple(over)


def read_path_list(path: Path) -> tuple[str, ...]:
    """A path list the collector wrote with `-z` (hades #369), so a non-ASCII path is its
    bytes and not git's quoted form; a list without a NUL is read one path per line, as
    an older collector script and the fake cluster write it."""
    content = text(path)
    paths = content.split("\0") if "\0" in content else content.splitlines()
    return tuple(p for p in paths if p.strip())


def read_base_paths(path: Path) -> tuple[str, ...] | None:
    """The injected-name paths the merge base already has, NUL-separated as the collector
    lists them (hades #369), or None when it wrote no such file."""
    if not path.is_file():
        return None
    return tuple(p for p in text(path).split("\0") if p.strip())


def read_commit_policy(directory: Path) -> CommitPolicyCheck | None:
    """The collector's commit policy answer, or None when it did not finish the check.

    The emails are what the worker's commits say, so they are data: capped in count and
    length here, and echoed by the gate only when they look like an address."""
    if not (directory / "checked").is_file():
        return None
    authors: list[tuple[str, str]] = []
    for line in text(directory / "author-problems.txt").splitlines()[:1000]:
        sha, _, email = line.partition("\t")
        if sha.strip():
            authors.append((sha.strip()[:64], email.strip()[:200]))
    return CommitPolicyCheck(author_problems=tuple(authors))


def read_verifications(
    verify: Path, spec: LaunchSpec, checks: list[tuple[str, str]]
) -> tuple[VerificationRun, ...]:
    expected = {
        str(v.get("id")): int(v.get("expect_exit", 0))
        for v in spec.contract.get("required_verification", [])
    }
    runs: list[VerificationRun] = []
    for check_id, command in checks:
        safe = scripts.encode_check_id(check_id)
        exit_file = verify / f"{safe}.exit"
        log_file = verify / f"{safe}.log"
        if not exit_file.is_file():
            runs.append(
                VerificationRun(
                    id=check_id,
                    command=command,
                    expect_exit=expected.get(check_id, 0),
                    exit_code=-1,
                    log_tail=tail(log_file, 32 * 1024),
                    ran=False,
                    detail="the verifier container recorded no exit for this command",
                )
            )
            continue
        raw = text(exit_file).strip()
        seconds_file = verify / f"{safe}.seconds"
        seconds = text(seconds_file).strip() if seconds_file.is_file() else ""
        runs.append(
            VerificationRun(
                id=check_id,
                command=command,
                expect_exit=expected.get(check_id, 0),
                exit_code=int(raw) if raw.lstrip("-").isdigit() else -1,
                log_tail=tail(log_file, 32 * 1024),
                # ASCII digits and a sane length only: the verifier ran worker code,
                # which may have left anything in this file.
                seconds=int(seconds) if re.fullmatch(r"[0-9]{1,9}", seconds) else None,
            )
        )
    return tuple(runs)
