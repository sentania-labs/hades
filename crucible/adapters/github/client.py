"""The GitHub client (23, ADR 0007).

Every call the delivery half makes, and no others. There is no force push and no ref
write other than the delete used at test cleanup. The issue-comment write is used for
the configured external-review trigger under the App's identity.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from crucible.adapters.github import normalize
from crucible.adapters.github.appauth import AppAuthenticator
from crucible.adapters.github.transport import RestTransport
from crucible.domain.refs import check_ref
from crucible.ports.github import (
    CheckRecord,
    CommentRecord,
    GitHubError,
    InstallationToken,
    MergeResult,
    Observation,
    PullRequestRef,
    ReactionRecord,
    ReviewRecord,
    UnobservableError,
)

log = logging.getLogger("crucible.github")

LOG_EXCERPT_BYTES = 64 * 1024


def _refused(exc: GitHubError) -> bool:
    """A permission or not-found refusal that the caller may treat as unobservable.

    GitHub reports a rate limit as a 403 too. That one is not a refusal: it must reach the
    delivery tick so the whole poll is deferred, rather than being read as "no protected
    checks" or "no reactions" (hades FDY-0139)."""
    return exc.status in (403, 404) and exc.response_class != "rate_limited"


class RestGitHubClient:
    """The `GitHubClient` port over `api.github.com`."""

    def __init__(
        self,
        authenticator: AppAuthenticator,
        transport: RestTransport,
        *,
        allow_issue_comments: bool = False,
    ) -> None:
        self._auth = authenticator
        self._http = transport
        self.allow_issue_comments = allow_issue_comments
        # 23: a 403 on the PR-level reactions endpoint means the App lacks Issues read.
        # It is recorded once per repository rather than on every poll.
        self.reactions_unobservable: set[str] = set()

    # ----- auth ---------------------------------------------------------

    def configured(self) -> bool:
        """Whether an App credential is in place right now (ADR 0017). A client wired
        for a store the operator has not filled yet says no, and delivery waits."""
        return self._auth.configured()

    def installation_token(
        self, *, installation_id: int, repository: str, permissions: dict[str, str] | None = None
    ) -> InstallationToken:
        return self._auth.installation_token(
            installation_id=installation_id, repository=repository, permissions=permissions
        )

    def checkout_token(self, *, installation_id: int, repository: str) -> InstallationToken:
        return self._auth.checkout_token(installation_id=installation_id, repository=repository)

    def revoke_token(self, token: InstallationToken) -> bool:
        return self._auth.revoke(token)

    def authenticated_login(self, token: InstallationToken) -> str:
        del token
        payload = self._http.get("/app", bearer=self._auth.app_jwt())
        if not isinstance(payload, dict) or not isinstance(payload.get("slug"), str):
            raise GitHubError(502, "authenticated App response has no slug", path="/app")
        return f"{payload['slug']}[bot]"

    # ----- reads --------------------------------------------------------

    def remote_head(self, token: InstallationToken, *, repository: str, ref: str) -> str | None:
        check_ref(ref, field="ref")
        try:
            payload = self._http.get(
                f"/repos/{repository}/git/ref/heads/{ref}", bearer=token.reveal()
            )
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        if isinstance(payload, dict):
            obj = payload.get("object") or {}
            return str(obj.get("sha", "")) or None
        return None

    def find_pull_request(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> PullRequestRef | None:
        owner = repository.split("/", maxsplit=1)[0]
        rows = self._http.paginate(
            f"/repos/{repository}/pulls",
            bearer=token.reveal(),
            params={"state": "all", "head": f"{owner}:{head_branch}"},
        )
        if not rows:
            return None
        # Newest first: a branch reused after a closed PR must not resolve to the old one.
        open_rows = [r for r in rows if isinstance(r, dict) and r.get("state") == "open"]
        chosen = max(
            open_rows or [r for r in rows if isinstance(r, dict)],
            key=lambda r: int(r.get("number", 0)),
        )
        return self.get_pull_request(token, repository=repository, number=int(chosen["number"]))

    def get_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> PullRequestRef:
        payload = self._http.get(f"/repos/{repository}/pulls/{number}", bearer=token.reveal())
        return normalize.pull_request(payload)

    def merge_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        expected_head_sha: str,
    ) -> MergeResult:
        path = f"/repos/{repository}/pulls/{number}/merge"
        status, payload, _ = self._http.request(
            "PUT",
            path,
            bearer=token.reveal(),
            body={"merge_method": "squash", "sha": expected_head_sha},
        )
        if status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise GitHubError(status, str(message or "merge refused"), path=path)
        if not isinstance(payload, dict) or not payload.get("merged") or not payload.get("sha"):
            message = payload.get("message") if isinstance(payload, dict) else None
            raise GitHubError(409, str(message or "merge refused"), path=path)

        # GitHub's merge response supplies the merge SHA but not its actor or time. Read
        # the resulting PR as part of this operation so callers record GitHub's values,
        # rather than waiting for a later observation.
        merged = self.get_pull_request(token, repository=repository, number=number)
        if not merged.merged or merged.merged_at is None or not merged.merged_by:
            raise GitHubError(502, "merged pull request response is incomplete", path=path)
        return MergeResult(
            sha=str(payload["sha"]),
            merged_at=merged.merged_at,
            merged_by=merged.merged_by,
        )

    def open_pull_requests(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> Sequence[PullRequestRef]:
        owner = repository.split("/", maxsplit=1)[0]
        rows = self._http.paginate(
            f"/repos/{repository}/pulls",
            bearer=token.reveal(),
            params={"state": "open", "head": f"{owner}:{head_branch}"},
        )
        found = [
            normalize.pull_request(row)
            for row in rows or []
            if isinstance(row, dict) and row.get("state") == "open"
        ]
        return sorted(found, key=lambda ref: ref.number)

    def list_required_checks(
        self, token: InstallationToken, *, repository: str, branch: str
    ) -> Sequence[str]:
        """Branch protection first, then rulesets. Neither is a failure when absent: a
        repository with no protection simply contributes an empty set, and 23's next step
        is every observed run."""
        names: list[str] = []
        try:
            payload = self._http.get(
                f"/repos/{repository}/branches/{branch}/protection/required_status_checks",
                bearer=token.reveal(),
            )
            if isinstance(payload, dict):
                names.extend(str(c) for c in (payload.get("contexts") or []))
                for check in payload.get("checks") or []:
                    if isinstance(check, dict) and check.get("context"):
                        names.append(str(check["context"]))
        except GitHubError as exc:
            if not _refused(exc):
                raise
        try:
            rules = self._http.get(
                f"/repos/{repository}/rules/branches/{branch}", bearer=token.reveal()
            )
            for rule in rules if isinstance(rules, list) else []:
                if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                    continue
                parameters = rule.get("parameters") or {}
                for check in parameters.get("required_status_checks") or []:
                    if isinstance(check, dict) and check.get("context"):
                        names.append(str(check["context"]))
        except GitHubError as exc:
            if not _refused(exc):
                raise
        return tuple(dict.fromkeys(names))

    def reactions_for(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[ReactionRecord, ...]:
        """Reactions on the PR itself. Needs Issues read (S12); a 403 is `UnobservableError`."""
        try:
            rows = self._http.paginate(
                f"/repos/{repository}/issues/{number}/reactions", bearer=token.reveal()
            )
        except GitHubError as exc:
            if _refused(exc):
                self.reactions_unobservable.add(repository)
                raise UnobservableError(
                    f"reactions on {repository}#{number}", status=exc.status
                ) from exc
            raise
        self.reactions_unobservable.discard(repository)
        return tuple(
            normalize.reaction(row, subject_kind="pull_request", subject_github_id=str(number))
            for row in rows
            if isinstance(row, dict)
        )

    def closed_by(self, token: InstallationToken, *, repository: str, number: int) -> str | None:
        """Who closed the pull request, from the issue events timeline.

        `GET /pulls/{n}` carries `merged_by` but no closer, so 23's "with the closer
        recorded" needs this second call. It is best effort: on a permission refusal the
        close is still recorded, with no actor, rather than the observation failing."""
        try:
            rows = self._http.paginate(
                f"/repos/{repository}/issues/{number}/events", bearer=token.reveal()
            )
        except GitHubError as exc:
            if _refused(exc):
                return None
            raise
        for row in reversed(rows):
            if isinstance(row, dict) and row.get("event") == "closed":
                actor = row.get("actor")
                if isinstance(actor, dict) and actor.get("login"):
                    return str(actor["login"])
                return None
        return None

    def _comment_reactions(
        self, token: InstallationToken, *, repository: str, comments: Sequence[CommentRecord]
    ) -> list[ReactionRecord]:
        out: list[ReactionRecord] = []
        for comment in comments:
            if comment.reaction_count == 0:
                # The comment says it has none, so there is nothing to fetch; one call per
                # comment per poll adds up on a long review (hades FDY-0139).
                continue
            endpoint = (
                f"/repos/{repository}/pulls/comments/{comment.github_id}/reactions"
                if comment.kind == "review_comment"
                else f"/repos/{repository}/issues/comments/{comment.github_id}/reactions"
            )
            try:
                rows = self._http.paginate(endpoint, bearer=token.reveal())
            except GitHubError as exc:
                if _refused(exc):
                    continue
                raise
            out.extend(
                normalize.reaction(
                    row, subject_kind=comment.kind, subject_github_id=comment.github_id
                )
                for row in rows
                if isinstance(row, dict)
            )
        return out

    def observe(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        base_ref: str,
        with_reactions: bool = True,
    ) -> Observation:
        """One complete poll: 23's whole list, in one place, so the webhook path can be
        an accelerator rather than a second source of truth."""
        notes: list[str] = []
        pr = self.get_pull_request(token, repository=repository, number=number)
        if pr.state == "closed" and not pr.merged:
            closer = self.closed_by(token, repository=repository, number=number)
            pr = replace(pr, closed_by=closer)
        reviews = tuple(
            normalize.review(row)
            for row in self._http.paginate(
                f"/repos/{repository}/pulls/{number}/reviews", bearer=token.reveal()
            )
            if isinstance(row, dict)
        )
        review_comments = tuple(
            normalize.review_comment(row)
            for row in self._http.paginate(
                f"/repos/{repository}/pulls/{number}/comments", bearer=token.reveal()
            )
            if isinstance(row, dict)
        )
        issue_comments = self.issue_comments(token, repository=repository, number=number)
        reactions: tuple[ReactionRecord, ...] = ()
        observable = True
        detail = ""
        if with_reactions:
            try:
                reactions = self.reactions_for(token, repository=repository, number=number)
            except UnobservableError as exc:
                observable = False
                detail = str(exc)
                notes.append(detail)
            reactions = reactions + tuple(
                self._comment_reactions(
                    token,
                    repository=repository,
                    comments=[*review_comments, *issue_comments],
                )
            )
        checks = self._checks_for(token, repository=repository, head_sha=pr.head_sha)
        required = tuple(
            self.list_required_checks(token, repository=repository, branch=base_ref or pr.base_ref)
        )
        return Observation(
            pull_request=pr,
            reviews=reviews,
            review_comments=review_comments,
            issue_comments=issue_comments,
            reactions=reactions,
            reactions_observable=observable,
            reactions_detail=detail,
            checks=checks,
            required_checks=required,
            observed_at=datetime.now(UTC),
            rate_limit_remaining=self._http.rate_limit_remaining,
            notes=tuple(notes),
        )

    def issue_comments(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[CommentRecord, ...]:
        return tuple(
            normalize.issue_comment(row)
            for row in self._http.paginate(
                f"/repos/{repository}/issues/{number}/comments", bearer=token.reveal()
            )
            if isinstance(row, dict)
        )

    def _checks_for(
        self, token: InstallationToken, *, repository: str, head_sha: str
    ) -> tuple[CheckRecord, ...]:
        if not head_sha:
            return ()
        # Every page of each: a repository with a wide matrix has more than one page of
        # check runs, and a required check on the second page must not read as missing.
        out: list[CheckRecord] = [
            normalize.check_run(row)
            for row in self._http.paginate(
                f"/repos/{repository}/commits/{head_sha}/check-runs",
                bearer=token.reveal(),
                key="check_runs",
            )
            if isinstance(row, dict)
        ]
        out.extend(
            normalize.check_suite(row)
            for row in self._http.paginate(
                f"/repos/{repository}/commits/{head_sha}/check-suites",
                bearer=token.reveal(),
                key="check_suites",
            )
            if isinstance(row, dict)
        )
        out.extend(
            normalize.workflow_run(row)
            for row in self._http.paginate(
                f"/repos/{repository}/actions/runs",
                bearer=token.reveal(),
                params={"head_sha": head_sha},
                key="workflow_runs",
            )
            if isinstance(row, dict)
        )
        return tuple(out)

    def checks_for_commit(
        self, token: InstallationToken, *, repository: str, head_sha: str
    ) -> tuple[CheckRecord, ...]:
        """Public form used to watch main after Crucible merges a pull request."""
        return self._checks_for(token, repository=repository, head_sha=head_sha)

    def ci_failure_log(
        self,
        token: InstallationToken,
        *,
        repository: str,
        source: str,
        external_id: str,
        limit_bytes: int,
    ) -> bytes:
        """The log excerpt captured on a CI failure (Actions read, 23).

        A check run from Actions is a job, and its id is the job id, so its log is the
        job's own plain-text log. A failed workflow run is resolved to its first failed
        job the same way. Either endpoint answers with a redirect to a signed URL on
        another host, which is followed without the token. What is kept is the tail, where
        a failing job prints its failure (hades FDY-0139). Best effort: anything that
        cannot be read leaves the excerpt empty rather than failing the poll."""
        try:
            job_id = external_id
            if source == "workflow_run":
                jobs = self._http.paginate(
                    f"/repos/{repository}/actions/runs/{external_id}/jobs",
                    bearer=token.reveal(),
                    key="jobs",
                )
                failed = [
                    job
                    for job in jobs
                    if isinstance(job, dict)
                    and job.get("conclusion") not in (None, "success", "skipped", "neutral")
                ]
                if not failed:
                    return b""
                job_id = str(failed[0].get("id", ""))
            if not job_id:
                return b""
            status, payload, headers = self._http.request(
                "GET",
                f"/repos/{repository}/actions/jobs/{job_id}/logs",
                bearer=token.reveal(),
                raw=True,
            )
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                return self._http.download(headers["location"], limit_bytes=limit_bytes)
        except GitHubError as exc:
            if exc.response_class == "rate_limited":
                raise
            return b""
        except Exception as exc:  # best effort: an excerpt never fails the poll
            log.warning("ci failure log unreadable", extra={"error": type(exc).__name__})
            return b""
        if status >= 400 or not isinstance(payload, bytes):
            return b""
        return payload[-max(1, limit_bytes) :]

    # ----- mutations ----------------------------------------------------

    def create_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        title: str,
        head_branch: str,
        base_ref: str,
        body: str,
        draft: bool = False,
    ) -> PullRequestRef:
        check_ref(head_branch, field="head_branch")
        check_ref(base_ref, field="base_ref")
        status, payload, _ = self._http.request(
            "POST",
            f"/repos/{repository}/pulls",
            bearer=token.reveal(),
            body={
                "title": title,
                "head": head_branch,
                "base": base_ref,
                "body": body,
                "draft": draft,
            },
        )
        if status != 201 or not isinstance(payload, dict):
            raise GitHubError(status, _message(payload), path=f"/repos/{repository}/pulls")
        return normalize.pull_request(payload)

    def update_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        base_ref: str | None = None,
    ) -> PullRequestRef:
        """Title, body, and base. `draft` is deliberately absent: GitHub does not accept
        it on this endpoint, so a draft mismatch on a reused pull request is reported and
        refused rather than silently tolerated (see the publisher)."""
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if body is not None:
            fields["body"] = body
        if base_ref is not None:
            check_ref(base_ref, field="base_ref")
            fields["base"] = base_ref
        if not fields:
            return self.get_pull_request(token, repository=repository, number=number)
        status, payload, _ = self._http.request(
            "PATCH", f"/repos/{repository}/pulls/{number}", bearer=token.reveal(), body=fields
        )
        if status >= 400 or not isinstance(payload, dict):
            raise GitHubError(status, _message(payload), path=f"/repos/{repository}/pulls/{number}")
        return normalize.pull_request(payload)

    def post_issue_comment(
        self, token: InstallationToken, *, repository: str, number: int, body: str
    ) -> CommentRecord:
        """Post the configured external-review trigger under the App's identity."""
        status, payload, _ = self._http.request(
            "POST",
            f"/repos/{repository}/issues/{number}/comments",
            bearer=token.reveal(),
            body={"body": body},
        )
        if status != 201 or not isinstance(payload, dict):
            raise GitHubError(
                status, _message(payload), path=f"/repos/{repository}/issues/{number}/comments"
            )
        return normalize.issue_comment(payload)

    def reply_to_review_comment(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        comment_id: str,
        body: str,
    ) -> CommentRecord:
        path = f"/repos/{repository}/pulls/{number}/comments/{comment_id}/replies"
        status, payload, _ = self._http.request(
            "POST", path, bearer=token.reveal(), body={"body": body}
        )
        if status != 201 or not isinstance(payload, dict):
            raise GitHubError(status, _message(payload), path=path)
        return normalize.review_comment(payload)

    def delete_ref(self, token: InstallationToken, *, repository: str, ref: str) -> None:
        """Cleanup only (the live test tier). A default branch is refused here as well as
        by policy, because a delete is the one call with no undo."""
        check_ref(ref, field="ref")
        if ref in ("main", "master") or ref.startswith("release/"):
            raise GitHubError(
                403, f"refusing to delete {ref!r}", path=f"/repos/{repository}/git/refs"
            )
        status, payload, _ = self._http.request(
            "DELETE", f"/repos/{repository}/git/refs/heads/{ref}", bearer=token.reveal()
        )
        if status not in (204, 404, 422):
            raise GitHubError(
                status, _message(payload), path=f"/repos/{repository}/git/refs/heads/{ref}"
            )

    def close_pull_request(self, token: InstallationToken, *, repository: str, number: int) -> None:
        """Live-tier cleanup only: the tests close what they opened. Crucible's own
        delivery path never closes a PR, because that is a person's act (23)."""
        self._http.request(
            "PATCH",
            f"/repos/{repository}/pulls/{number}",
            bearer=token.reveal(),
            body={"state": "closed"},
        )


def _message(payload: Any) -> str:
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return "request failed"


__all__ = [
    "CheckRecord",
    "CommentRecord",
    "RestGitHubClient",
    "ReviewRecord",
]
