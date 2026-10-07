"""The completion record Hades composes from evidence it already holds (hades #498).

The operator's rule of 2026-10-06: the pre-PR gates judge whether the worker returned
work and whether it passes, never whether the paperwork is complete. So the record of
what an attempt delivered is Hades's own, composed from the commits on the collected
branch, the required_verification commands Hades re-ran in the verifier container with
their exit codes, and the diff against each review comment's path. The worker's report,
when there is one, adds to the record and never gates it.

Pure functions over plain data: no database, no file, no clock. The composed record is
the document the reviewer reads (`GET /v1/attempts/{id}/report`) and the one the PR
body carries.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from crucible.domain.exit_class import ExitClass
from crucible.domain.secrets import redact

COMPOSED_BY = "hades"
# The judgement fields the worker writes and the record keeps as the worker's own
# (contracts.completion_claim.JUDGEMENT_FIELDS, repeated here so the domain stays pure).
WORKER_FIELDS: tuple[str, ...] = (
    "summary",
    "self_review",
    "acceptance_mapping",
    "proposed_pull_request",
    "limitations",
    "risks",
    "blockers",
    "follow_ups",
    "finding_dispositions",
)
DISPOSITION_ADDRESSED = "addressed"
DISPOSITION_NOT_ADDRESSED = "not addressed"
WORKER_REPORT_PARSED = "parsed"
WORKER_REPORT_UNPARSED = "did not parse"
WORKER_REPORT_ABSENT = "absent"
WORKER_REPORT_REDACTED = "redacted"
_MESSAGE_LIMIT = 200
_MESSAGES_LIMIT = 50


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One review comment a correction addresses: its id and the path it was left on."""

    review_comment_id: str
    path: str | None


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One required_verification command as Hades re-ran it in the verifier container."""

    id: str
    command: str
    exit_code: int
    expect_exit: int = 0
    ran: bool = True
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BranchFacts:
    """What the collected bundle says about the branch."""

    head_sha: str
    commits: int
    work_branch: str = ""
    commit_messages: tuple[str, ...] = ()
    commit_paths: tuple[str, ...] = ()


def how_it_ended(
    exit_class: ExitClass | None,
    exit_code: int | None,
    *,
    termination_reason: str | None = None,
    limit_reached: str | None = None,
) -> str:
    """How the run ended, in plain words, for the record. Never a score."""
    if exit_class is ExitClass.ENDED_BY_BUDGET:
        if limit_reached:
            return f"ended on its budget: {limit_reached}"
        if termination_reason == "timeout":
            return "ended on its budget: the attempt's time limit"
        return "ended on its budget"
    if exit_class is ExitClass.COMPLETED:
        return "the worker ended its turn (exit 0)"
    if exit_class is ExitClass.COMPLETED_WITHOUT_REPORT:
        return "the worker ended its turn (exit 0) with no report and no commit"
    if exit_class is None:
        return f"exit code {exit_code!r}"
    return f"{exit_class.value} (exit code {exit_code!r})"


def problem_text(error: Mapping[str, Any]) -> str:
    """One parse problem as its field and message, as the record lists it."""
    loc = ".".join(str(part) for part in (error.get("loc") or []))
    message = str(error.get("msg") or "")
    return f"{loc}: {message}" if loc else message


def disposition_notes(
    expected: set[str], counts: Mapping[str, int], duplicates: Sequence[str]
) -> list[str]:
    """What a correction report's `finding_dispositions` left out or doubled, in plain
    words for the reviewer (hades #498). Never a failure: Hades reads the diff for the
    findings the worker did not disposition. Only review comment ids are echoed."""
    notes: list[str] = []
    missing = sorted(expected - set(counts))
    unknown = sorted(set(counts) - expected)
    if missing:
        notes.append(
            "the report gives no disposition for review finding(s) "
            + ", ".join(missing)
            + "; Hades read the diff for them"
        )
    if unknown:
        notes.append(
            "the report dispositions "
            + ", ".join(unknown)
            + ", which this correction does not address"
        )
    if duplicates:
        notes.append(
            "the report dispositions "
            + ", ".join(sorted(duplicates))
            + " more than once; Hades recorded none of those"
        )
    return notes


def _touches(path: str, changed: Sequence[str]) -> bool:
    wanted = posixpath.normpath(path)
    return any(posixpath.normpath(p) == wanted for p in changed)


def finding_coverage(
    findings: Sequence[ReviewFinding],
    *,
    changed_paths: Sequence[str],
    head_sha: str | None,
    worker_dispositions: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """One entry per review finding the correction addresses (hades #498).

    The default disposition is `addressed`, with the collected head as the commit, when
    the diff touches the finding's path, and `not addressed` when no commit touches it.
    A disposition the worker wrote for the same finding is kept beside it, as the
    worker's; it never replaces Hades's reading of the diff."""
    by_worker: dict[str, Mapping[str, Any]] = {}
    for item in worker_dispositions:
        if isinstance(item, Mapping) and isinstance(item.get("review_comment_id"), str):
            by_worker.setdefault(item["review_comment_id"], item)
    out: list[dict[str, Any]] = []
    for finding in findings:
        touched = finding.path is not None and _touches(finding.path, changed_paths)
        entry: dict[str, Any] = {
            "review_comment_id": finding.review_comment_id,
            "path": finding.path,
            "disposition": DISPOSITION_ADDRESSED if touched else DISPOSITION_NOT_ADDRESSED,
            "commit": head_sha if touched and head_sha else None,
            "source": "diff",
        }
        if finding.path is None:
            entry["note"] = "the review comment names no path, so the diff cannot cover it"
        worker = by_worker.get(finding.review_comment_id)
        if worker is not None:
            entry["worker"] = {
                "disposition": str(worker.get("disposition") or ""),
                "commit": worker.get("commit"),
                "reason": redact(str(worker.get("reason") or ""))[:_MESSAGE_LIMIT] or None,
            }
        out.append(entry)
    return out


def _check_entry(check: CheckRun) -> dict[str, Any]:
    return {
        "id": check.id,
        "command": check.command,
        "exit": check.exit_code if check.ran else None,
        "expect_exit": check.expect_exit,
        "ran": check.ran,
        "passed": check.ran and check.exit_code == check.expect_exit,
        **({"detail": redact(check.detail)[:_MESSAGE_LIMIT]} if check.detail else {}),
    }


def compose_completion_record(
    *,
    exit_class: ExitClass | None,
    exit_code: int | None,
    termination_reason: str | None = None,
    limit_reached: str | None = None,
    branch: BranchFacts | None,
    diff_paths: Sequence[str] = (),
    checks: Sequence[CheckRun] = (),
    findings: Sequence[ReviewFinding] = (),
    worker_report: Mapping[str, Any] | None = None,
    worker_report_status: str = WORKER_REPORT_ABSENT,
    worker_report_problems: Sequence[str] = (),
) -> dict[str, Any]:
    """The completion record (hades #498): Hades's own facts under `composed`, and the
    worker's judgement fields beside them when a report was there to take them from.

    `worker_report` is the worker's document with Crucible's facts in place (hades
    #215), or None when there was no report or it was redacted. Its judgement fields are
    copied as the worker's own; nothing in them is verified by being here."""
    changed = list(diff_paths)
    if branch is not None:
        changed.extend(p for p in branch.commit_paths if p not in changed)
    dispositions: list[Mapping[str, Any]] = []
    if worker_report is not None and isinstance(worker_report.get("finding_dispositions"), list):
        dispositions = [
            item for item in worker_report["finding_dispositions"] if isinstance(item, Mapping)
        ]
    coverage = finding_coverage(
        findings,
        changed_paths=changed,
        head_sha=branch.head_sha if branch is not None else None,
        worker_dispositions=dispositions,
    )
    passed = sum(1 for c in checks if c.ran and c.exit_code == c.expect_exit)
    composed: dict[str, Any] = {
        "by": COMPOSED_BY,
        "ended": {
            "exit_class": exit_class.value if exit_class is not None else None,
            "exit_code": exit_code,
            "termination_reason": termination_reason,
            "how": how_it_ended(
                exit_class,
                exit_code,
                termination_reason=termination_reason,
                limit_reached=limit_reached,
            ),
        },
        "branch": {
            "head_sha": branch.head_sha,
            "work_branch": branch.work_branch,
            "commits": branch.commits,
            "commit_messages": [
                redact(message.splitlines()[0] if message else "")[:_MESSAGE_LIMIT]
                for message in branch.commit_messages[:_MESSAGES_LIMIT]
            ],
            "changed_paths": sorted(changed),
        }
        if branch is not None
        else None,
        "checks": [_check_entry(c) for c in checks],
        "checks_passed": f"{passed} of {len(checks)}",
        "findings": coverage,
        "worker_report": {
            "status": worker_report_status,
            "problems": [redact(str(p))[:_MESSAGE_LIMIT] for p in worker_report_problems][:10],
        },
    }
    record: dict[str, Any] = {}
    if worker_report is not None:
        for name in WORKER_FIELDS:
            if name in worker_report and worker_report[name] is not None:
                record[name] = worker_report[name]
    if not isinstance(record.get("summary"), str) or not record.get("summary"):
        record["summary"] = composed_summary(composed)
    record["composed"] = composed
    return record


def composed_summary(composed: Mapping[str, Any]) -> str:
    """A summary written by Hades from its own facts, used when the worker wrote none."""
    branch = composed.get("branch") or {}
    commits = int(branch.get("commits") or 0) if isinstance(branch, Mapping) else 0
    findings = composed.get("findings") or []
    addressed = sum(1 for f in findings if f.get("disposition") == DISPOSITION_ADDRESSED)
    parts = [
        f"Composed by Hades: {commits} commit(s) on the branch",
        f"required checks passed {composed.get('checks_passed')}",
    ]
    if findings:
        parts.append(f"{addressed} of {len(findings)} review finding(s) touched by the diff")
    ended = composed.get("ended") or {}
    parts.append(str(ended.get("how") or ""))
    return "; ".join(p for p in parts if p) + "."
