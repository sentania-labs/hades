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
import stat
import tarfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import yaml

from crucible.adapters.execution import scripts
from crucible.adapters.execution.injected_collection import classify_collected, nul_fields
from crucible.adapters.execution.workspace import harness_private_path
from crucible.contracts.evidence import REVIEW_DIFF_NAME, REVIEW_DIFF_TYPE
from crucible.domain.acceptance_checks import verifier_checks
from crucible.domain.secret_fixtures import BaseMatch, SecretDeclarations, declarations
from crucible.domain.secrets import SecretMatch, match_line, scan_chunks
from crucible.ports.execution import (
    BranchBundle,
    CollectedArtifact,
    CommitPolicyCheck,
    LaunchSpec,
    PathChange,
    VerificationRun,
)

__all__ = [
    "VERIFICATION_LOG_LIMIT",
    "BlobTarScan",
    "Outputs",
    "head_and_tail",
    "lists_over_limit",
    "read_base_paths",
    "read_commit_policy",
    "read_outputs",
    "read_path_changes",
    "read_path_list",
    "read_secret_declarations",
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
    secret_declarations: SecretDeclarations | None = None


TEXT_LIMIT = 8 * 1024 * 1024
ARTIFACT_LIMIT = 4 * 1024 * 1024


@contextlib.contextmanager
def _open_regular(path: Path) -> Iterator[IO[bytes]]:
    """Open without following links or waiting on a FIFO.

    Check the opened descriptor, so replacing a checked path cannot make the read
    block.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular collected file")
        yield handle


def _read_regular(path: Path, limit: int) -> bytes:
    """A growing file cannot extend the read beyond the byte budget."""
    with _open_regular(path) as handle:
        return handle.read(limit)


def text(path: Path, limit: int = TEXT_LIMIT) -> str:
    try:
        return _read_regular(path, limit).decode("utf-8", "replace")
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


# hades #608: what one verification log artifact keeps, at most.
VERIFICATION_LOG_LIMIT = 64 * 1024


def head_and_tail(path: Path, limit: int = VERIFICATION_LOG_LIMIT) -> str:
    """A log bounded at `limit` bytes that keeps both ends (hades #608): a check's first
    error is near the head, and the summary its runner prints last is at the tail. A
    longer log loses its middle, and a line in its place says how many bytes went."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= limit:
                whole = handle.read(limit).decode("utf-8", "replace")
                half = len(whole) // 2
                return _bounded(whole[:half], "", whole[half:], limit)
            marker = (
                f"\n[... {size} bytes in all: the middle is omitted, "
                "the head and the tail are kept ...]\n"
            )
            half = (limit - len(marker.encode("utf-8"))) // 2
            head = handle.read(half)
            handle.seek(size - half)
            last = handle.read(half)
    except OSError:
        return ""
    return _bounded(head.decode("utf-8", "replace"), marker, last.decode("utf-8", "replace"), limit)


def _bounded(head: str, marker: str, last: str, limit: int) -> str:
    """`head`, `marker` and `last` within `limit` bytes once encoded. A byte that decoded
    as a replacement character grows to three, so the budget is counted in encoded bytes
    and shared between the two ends, each keeping at least half of what it may: the first
    error and the runner's summary both survive malformed output. A cut that splits a
    character drops that character."""
    head_bytes = head.encode("utf-8")
    marker_bytes = marker.encode("utf-8")
    last_bytes = last.encode("utf-8")
    if len(head_bytes) + len(marker_bytes) + len(last_bytes) <= limit:
        return head + marker + last
    budget = max(0, limit - len(marker_bytes))
    keep_last = min(len(last_bytes), budget - min(len(head_bytes), budget // 2))
    keep_head = min(len(head_bytes), budget - keep_last)
    head = head_bytes[:keep_head].decode("utf-8", "ignore")
    last = last_bytes[len(last_bytes) - keep_last :].decode("utf-8", "ignore")
    return head + marker + last


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
    declared = read_secret_declarations(output)
    diff_findings, diff_unscanned = scan_changed_content(output, changed_blobs, declared)
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
            attempt_commit_paths=read_path_list(output / "attempt-commit-paths.txt"),
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
            try:
                content = _read_regular(path, ARTIFACT_LIMIT)
            except OSError:
                continue
            artifacts.append(
                CollectedArtifact(
                    name=name,
                    type="run_evidence",
                    content=content,
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
                content=_read_regular(review_diff, ARTIFACT_LIMIT),
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
        secret_declarations=declared,
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


_BASE_RECORD = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64}):(.*)", re.DOTALL)


def _base_matches(directory: Path) -> Iterator[BaseMatch]:
    """The `git grep -z -n -o` records the collector wrote for each rule: the merge
    base's tree, the path, the line and the value."""
    if not directory.is_dir() or directory.is_symlink():
        return
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        rule = path.name
        for record in text(path, scripts.SECRET_DECLARATION_CAP_BYTES).split("\n"):
            fields = record.split("\0")
            if len(fields) != 3 or not fields[1].isdigit():
                continue
            named = _BASE_RECORD.fullmatch(fields[0])
            if named is None:
                continue
            yield BaseMatch(path=named.group(1), pattern=rule, line=int(fields[1]), value=fields[2])


def read_secret_declarations(output: Path) -> SecretDeclarations | None:
    """What the merge base declares about secret-shaped text (FDY-0618): its
    `.gitleaksignore`, `.gitleaks.toml`, and the digests of the values it holds at the
    places those allow. None for a collector that exported nothing of it."""
    directory = output / scripts.SECRET_DECLARATIONS_DIR
    if not directory.is_dir() or directory.is_symlink():
        return None

    def read(name: str) -> str:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            return ""
        return text(path, scripts.SECRET_DECLARATION_CAP_BYTES)

    return declarations(
        read("gitleaksignore"), read("gitleaks.toml"), _base_matches(directory / "base")
    )


def _allowed_by(
    declared: SecretDeclarations | None, path: str, line: int
) -> Callable[[str, str, str], bool] | None:
    """What `match_line` asks of each match on one diff line: would `make scan` allow
    it, by its fingerprint or an allowlist the merge base declares."""
    if declared is None:
        return None

    def skip(rule: str, value: str, whole: str) -> bool:
        return declared.allowed(path, rule, line, value, whole)

    return skip


_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# Refuse oversized inputs explicitly rather than silently accepting an unscanned tail.
DIFF_SCAN_LIMIT = 256 * 1024 * 1024
DIFF_LINE_LIMIT = 16 * 1024 * 1024
DIFF_FINDING_LIMIT = 10_000


def scan_changed_content(
    output: Path,
    blobs: Mapping[str, str | bool | None] | None = None,
    declared: SecretDeclarations | None = None,
) -> tuple[tuple[SecretMatch, ...] | None, tuple[str, ...]]:
    """Scan only lines added relative to the prepared base.

    A zero-context decision is made from the collected patch: context and deleted lines
    are repository content the attempt did not add. Exported blobs remain integrity
    coverage for newly added files, but their complete contents are not judged because
    doing so would make an edit beside old secret-shaped fixture data fail (#488).

    FDY-0618: each match names the new file's line. What the merge base declares
    (`declared`, read from `output` when not given) is honoured as `make scan` honours
    it: a path an allowlist names and a listed `path:rule:line` fingerprint are skipped,
    and a value the repository declares as a fixture is reported as advisory.
    """
    patch = output / "diff.patch"
    if not patch.is_file():
        return None, ()
    if declared is None:
        declared = read_secret_declarations(output)
    found: list[SecretMatch] = []
    unscanned: list[str] = []
    current_path = "diff"
    in_hunk = False
    excluded = False
    line_number = 0
    fixture = declared.is_fixture if declared is not None else None
    try:
        with _open_regular(patch) as handle:
            remaining = os.fstat(handle.fileno()).st_size
            if remaining > DIFF_SCAN_LIMIT:
                return None, ("diff.patch",)
            while remaining:
                raw_line = handle.readline(min(remaining, DIFF_LINE_LIMIT) + 1)
                if not raw_line or len(raw_line) > min(remaining, DIFF_LINE_LIMIT):
                    return tuple(found), ("diff.patch",)
                remaining -= len(raw_line)
                line = raw_line.decode("utf-8", "replace")
                del raw_line
                # Inside a hunk, even +++ b/ is source content, not a path header.
                if line.startswith("diff --git "):
                    in_hunk = False
                elif line.startswith("@@ "):
                    in_hunk = True
                    hunk = _HUNK.match(line)
                    line_number = int(hunk.group(1)) if hunk else 0
                elif not in_hunk and line.startswith("+++ b/"):
                    current_path = line[6:].rstrip("\n")
                    excluded = harness_private_path(current_path) or (
                        declared is not None and declared.path_allowed(current_path)
                    )
                elif in_hunk and line.startswith("+"):
                    if not excluded:
                        found.extend(
                            match_line(
                                line[1:],
                                path=f"diff:{current_path}",
                                line=line_number,
                                fixture=fixture,
                                skip=_allowed_by(declared, current_path, line_number),
                            )
                        )
                    line_number += 1
                elif in_hunk and line.startswith(" "):
                    line_number += 1
                if len(found) > DIFF_FINDING_LIMIT:
                    return tuple(found[:DIFF_FINDING_LIMIT]), ("diff.patch",)
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
    expected = {c["id"]: c["expect_exit"] for c in verifier_checks(spec.contract)}
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
                    log_tail=head_and_tail(log_file),
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
                log_tail=head_and_tail(log_file),
                # ASCII digits and a sane length only: the verifier ran worker code,
                # which may have left anything in this file.
                seconds=int(seconds) if re.fullmatch(r"[0-9]{1,9}", seconds) else None,
            )
        )
    return tuple(runs)
