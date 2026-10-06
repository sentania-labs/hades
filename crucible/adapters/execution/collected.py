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
import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from crucible.adapters.execution import scripts
from crucible.adapters.execution.injected_collection import classify_collected, nul_fields
from crucible.contracts.evidence import REVIEW_DIFF_NAME, REVIEW_DIFF_TYPE
from crucible.domain.secrets import SecretMatch, scan_chunks
from crucible.ports.execution import (
    BranchBundle,
    CollectedArtifact,
    CommitPolicyCheck,
    LaunchSpec,
    PathChange,
    VerificationRun,
)

__all__ = [
    "Outputs",
    "lists_over_limit",
    "read_base_paths",
    "read_commit_policy",
    "read_outputs",
    "read_path_changes",
    "read_path_list",
    "read_verifications",
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
) -> Outputs:
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
    diff_findings, diff_unscanned = scan_changed_content(output)
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


def _file_chunks(path: Path, digest: Any = None) -> Iterator[str]:
    """A file as text, a chunk at a time; a character split across two reads is decoded
    whole. `digest`, when given, is fed every byte read."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with path.open("rb") as handle:
        while block := handle.read(SCAN_CHUNK):
            if digest is not None:
                digest.update(block)
            yield decoder.decode(block)
    yield decoder.decode(b"", final=True)


def _scan_blob(path: Path, blob: str) -> str | bool | None:
    """The pattern a collected blob matches, None for none, or False when the file is
    not that blob: missing, not a regular file, or content that does not hash to it."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        digest = hashlib.sha1() if len(blob) == 40 else hashlib.sha256()
        digest.update(b"blob %d\0" % path.stat().st_size)
        chunks = _file_chunks(path, digest)
        hit = scan_chunks(chunks)
        # Read what is left after a match, so the hash covers the whole file.
        for _ in chunks:
            pass
    except OSError:
        return False
    return hit if digest.hexdigest() == blob else False


def scan_changed_content(
    output: Path,
) -> tuple[tuple[SecretMatch, ...] | None, tuple[str, ...]]:
    """Scan every byte the worker added or changed (hades #398), never the whole at once.

    The whole diff.patch, and every blob the raw diff against the merge base names as
    added or changed, which the collector exported by object id regardless of the
    worker's attributes. A match is named by the path (`diff` for the patch). Returns
    None for the matches when there is no diff.patch, and the changed paths whose
    content could not be read, so the gate does not claim coverage it lacks."""
    patch = output / "diff.patch"
    if not patch.is_file():
        return None, ()
    found: list[SecretMatch] = []
    unscanned: list[str] = []
    try:
        hit = scan_chunks(_file_chunks(patch))
    except OSError:
        return None, ()
    if hit is not None:
        found.append(SecretMatch(path="diff", pattern=hit))
    raw = output / "diff-raw.txt"
    if not raw.is_file():
        # A collector before #398 exported no blobs; the patch is what it gave.
        return tuple(found), ()
    blobs = output / scripts.CHANGED_BLOBS_DIR
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
            blob = blob_bytes.decode("ascii")
            if blob not in results:
                results[blob] = _scan_blob(blobs / blob, blob)
            result = results[blob]
            if result is False:
                unscanned.append(shown)
            elif isinstance(result, str):
                found.append(SecretMatch(path=f"diff:{shown}", pattern=result))
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
