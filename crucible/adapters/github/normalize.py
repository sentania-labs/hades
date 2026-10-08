"""Turning GitHub JSON into the fields Crucible keeps (23).

The same normalization runs over polled text and over webhook payloads, which is the
point: a webhook only shortens latency, so it must not be a second way in with different
rules. Every user-controlled text field (review bodies, comment bodies, titles) goes
through the secret scanner and redaction before it is kept, and nothing else of the
payload survives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from crucible.domain.secrets import redact, scan_text
from crucible.ports.github import (
    CheckRecord,
    CommentRecord,
    PullRequestRef,
    ReactionRecord,
    ReviewRecord,
)

MAX_TEXT = 60_000


def parse_time(value: object) -> datetime:
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def clean_text(value: object) -> tuple[str, str | None]:
    """Redacted text and the name of the pattern that matched, if any.

    The pattern name is kept so the event can say a secret was removed from a body
    without the body or the value ever being stored."""
    text = str(value or "")[:MAX_TEXT]
    hit = scan_text(text)
    return (redact(text) if hit else text), hit


def login_of(node: Any) -> str:
    if isinstance(node, dict):
        user = node.get("user") or node.get("sender") or node.get("owner")
        if isinstance(user, dict):
            return str(user.get("login", ""))
        if isinstance(node.get("login"), str):
            return str(node["login"])
    return ""


def pull_request(payload: Any) -> PullRequestRef:
    assert isinstance(payload, dict)
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    merged_by = payload.get("merged_by")
    title, _ = clean_text(payload.get("title"))
    return PullRequestRef(
        number=int(payload.get("number", 0)),
        url=str(payload.get("html_url", "")),
        head_sha=str(head.get("sha", "")),
        base_ref=str(base.get("ref", "")),
        state=str(payload.get("state", "")),
        merged=bool(payload.get("merged", False)),
        merged_at=parse_time(payload["merged_at"]) if payload.get("merged_at") else None,
        merge_commit_sha=payload.get("merge_commit_sha") or None,
        merged_by=str(merged_by.get("login")) if isinstance(merged_by, dict) else None,
        closed_at=parse_time(payload["closed_at"]) if payload.get("closed_at") else None,
        closed_by=None,
        mergeable_state=str(payload.get("mergeable_state", "")),
        mergeable=(bool(payload["mergeable"]) if payload.get("mergeable") is not None else None),
        title=title,
        draft=bool(payload.get("draft", False)),
    )


def review(payload: Any) -> ReviewRecord:
    assert isinstance(payload, dict)
    body, _ = clean_text(payload.get("body"))
    return ReviewRecord(
        github_id=str(payload.get("id", "")),
        login=login_of(payload),
        state=str(payload.get("state", "")),
        body=body,
        commit_id=str(payload.get("commit_id")) if payload.get("commit_id") else None,
        submitted_at=parse_time(payload.get("submitted_at") or payload.get("created_at")),
    )


def reaction_count(payload: dict[str, Any]) -> int | None:
    """The comment's own reaction total, or None when the payload does not carry it.

    GitHub puts a `reactions` summary on every comment, so the client fetches a
    comment's reactions only when there is something to fetch (hades FDY-0139)."""
    summary = payload.get("reactions")
    if not isinstance(summary, dict):
        return None
    total = summary.get("total_count")
    return total if isinstance(total, int) else None


def review_comment(payload: Any) -> CommentRecord:
    assert isinstance(payload, dict)
    body, _ = clean_text(payload.get("body"))
    line = payload.get("line")
    review_id = payload.get("pull_request_review_id")
    # `commit_id` follows the pull request forward when a later head leaves the line
    # alone; `original_commit_id` is the head the reviewer actually commented on, which
    # is what decides whether a correction has since replaced it (hades FDY-0139).
    commit_id = payload.get("original_commit_id") or payload.get("commit_id")
    return CommentRecord(
        github_id=str(payload.get("id", "")),
        login=login_of(payload),
        body=body,
        created_at=parse_time(payload.get("created_at")),
        updated_at=parse_time(payload.get("updated_at") or payload.get("created_at")),
        kind="review_comment",
        path=str(payload["path"]) if payload.get("path") else None,
        line=int(line) if isinstance(line, int) else None,
        commit_id=str(commit_id) if commit_id else None,
        review_id=str(review_id) if review_id else None,
        reaction_count=reaction_count(payload),
    )


def issue_comment(payload: Any) -> CommentRecord:
    assert isinstance(payload, dict)
    body, _ = clean_text(payload.get("body"))
    return CommentRecord(
        github_id=str(payload.get("id", "")),
        login=login_of(payload),
        body=body,
        created_at=parse_time(payload.get("created_at")),
        updated_at=parse_time(payload.get("updated_at") or payload.get("created_at")),
        kind="issue_comment",
        reaction_count=reaction_count(payload),
    )


def reaction(payload: Any, *, subject_kind: str, subject_github_id: str) -> ReactionRecord:
    assert isinstance(payload, dict)
    return ReactionRecord(
        github_id=str(payload.get("id", "")),
        login=login_of(payload),
        content=str(payload.get("content", "")),
        created_at=parse_time(payload.get("created_at")),
        subject_kind=subject_kind,
        subject_github_id=subject_github_id,
    )


def check_run(payload: Any) -> CheckRecord:
    assert isinstance(payload, dict)
    app = payload.get("app") or {}
    return CheckRecord(
        name=str(payload.get("name", "")),
        status=str(payload.get("status", "")),
        conclusion=str(payload["conclusion"]) if payload.get("conclusion") else None,
        head_sha=str(payload.get("head_sha", "")),
        url=str(payload.get("html_url") or payload.get("details_url") or ""),
        external_id=str(payload.get("id", "")),
        workflow=str(app.get("slug", "")),
        job=str(payload.get("name", "")),
        source="check_run",
        completed_at=parse_time(payload["completed_at"]) if payload.get("completed_at") else None,
    )


def workflow_run(payload: Any) -> CheckRecord:
    assert isinstance(payload, dict)
    return CheckRecord(
        name=str(payload.get("name", "")),
        status=str(payload.get("status", "")),
        conclusion=str(payload["conclusion"]) if payload.get("conclusion") else None,
        head_sha=str(payload.get("head_sha", "")),
        url=str(payload.get("html_url", "")),
        external_id=str(payload.get("id", "")),
        workflow=str(payload.get("path") or payload.get("name") or ""),
        job="",
        source="workflow_run",
        # A re-run keeps the run id; its `updated_at` is when this attempt last changed.
        completed_at=(
            parse_time(payload["updated_at"])
            if payload.get("conclusion") and payload.get("updated_at")
            else None
        ),
        run_attempt=_attempt(payload.get("run_attempt")),
    )


def _attempt(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def check_suite(payload: Any) -> CheckRecord:
    assert isinstance(payload, dict)
    app = payload.get("app") or {}
    slug = str(app.get("slug", "suite"))
    return CheckRecord(
        name=f"suite:{slug}",
        status=str(payload.get("status", "")),
        conclusion=str(payload["conclusion"]) if payload.get("conclusion") else None,
        head_sha=str(payload.get("head_sha", "")),
        url=str(payload.get("url", "")),
        external_id=str(payload.get("id", "")),
        workflow=slug,
        source="check_suite",
    )
