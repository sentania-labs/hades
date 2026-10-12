"""The GitHub port (23, ADR 0007).

Crucible performs every routine GitHub mutation through this one interface: mint a
repository-scoped installation token, read everything observation needs, create the pull
request and update it, merge a certified head, post the configured external-review
trigger, and delete a ref at cleanup.

Nothing here returns a token. `installation_token` hands back an opaque object whose
value is readable exactly once by the publisher's hand-over, and whose `__repr__` and
`__str__` never show it, so a token cannot reach a log through an f-string.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class GitHubError(Exception):
    """A GitHub API call failed. Carries the status and a *class* of response, never a
    body that might echo a request header."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        path: str = "",
        response_class: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{status} on {path}: {message}" if path else f"{status}: {message}")
        self.status = status
        self.message = message
        self.path = path
        self.response_class = response_class or classify(status)
        # On a rate limit, how many seconds GitHub asked the caller to wait.
        self.retry_after = retry_after


def classify(status: int) -> str:
    """The response class recorded on a failure event (23 step 7). No body, no headers."""
    if status == 0:
        return "transport"
    if status in (401, 403):
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 422:
        return "unprocessable"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "client_error"
    if status >= 500:
        return "server_error"
    return "ok"


class UnobservableError(Exception):
    """A read the App's permission set does not reach.

    23: the App lacks Issues read until the operator adds it, and the PR-level reactions
    endpoint is the one place a clean external review appears. A 403 there is recorded as
    "reactions unobservable" and is not fatal."""

    def __init__(self, what: str, *, status: int = 403) -> None:
        super().__init__(f"{what} is not observable with the App's permissions ({status})")
        self.what = what
        self.status = status


class InstallationToken:
    """A short-lived installation token. In memory only, never stored, never printed.

    The value is behind a method rather than an attribute so that every read is a
    deliberate call and an accidental `f"{token}"` cannot leak it."""

    __slots__ = ("_value", "expires_at", "permissions", "repository")

    def __init__(
        self,
        value: str,
        *,
        expires_at: datetime,
        repository: str,
        permissions: dict[str, str] | None = None,
    ) -> None:
        self._value = value
        self.expires_at = expires_at
        self.repository = repository
        self.permissions = permissions or {}

    def reveal(self) -> str:
        return self._value

    def discard(self) -> None:
        self._value = ""

    def __repr__(self) -> str:
        return f"<InstallationToken repository={self.repository} expires_at={self.expires_at}>"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    number: int
    url: str
    head_sha: str
    base_ref: str
    state: str
    merged: bool = False
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    merged_by: str | None = None
    closed_at: datetime | None = None
    # `GET /pulls/{n}` carries no closer; the client fills this from the issue events
    # timeline when a pull request is observed closed and unmerged (23).
    closed_by: str | None = None
    mergeable_state: str = ""
    mergeable: bool | None = None
    title: str = ""
    draft: bool = False


@dataclass(frozen=True, slots=True)
class MergeResult:
    sha: str
    merged_at: datetime
    merged_by: str


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    github_id: str
    login: str
    state: str
    body: str
    commit_id: str | None
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class CommentRecord:
    github_id: str
    login: str
    body: str
    created_at: datetime
    updated_at: datetime
    kind: str = "review_comment"
    path: str | None = None
    line: int | None = None
    commit_id: str | None = None
    review_id: str | None = None
    # The comment's reaction total from its own payload; None when not reported.
    reaction_count: int | None = None


@dataclass(frozen=True, slots=True)
class ReactionRecord:
    github_id: str
    login: str
    content: str
    created_at: datetime
    subject_kind: str
    subject_github_id: str


@dataclass(frozen=True, slots=True)
class CheckRecord:
    name: str
    status: str
    conclusion: str | None
    head_sha: str
    url: str = ""
    external_id: str = ""
    workflow: str = ""
    job: str = ""
    source: str = "check_run"
    # When the run concluded, as GitHub reports it. A failure that concluded before a
    # re-run decision is the one the decision was about (hades FDY-0139).
    completed_at: datetime | None = None
    # A workflow run's attempt number: 1 for the first run, then one more per re-run
    # (issue 435). None for anything that is not a workflow run.
    run_attempt: int | None = None


@dataclass(frozen=True, slots=True)
class CommitDiffRecord:
    """A single changed-file summary from ``git diff`` (hades #443)."""

    path: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True, slots=True)
class Observation:
    """One poll of a pull request: everything 23 asks the supervisor to fetch."""

    pull_request: PullRequestRef
    reviews: tuple[ReviewRecord, ...] = ()
    review_comments: tuple[CommentRecord, ...] = ()
    issue_comments: tuple[CommentRecord, ...] = ()
    reactions: tuple[ReactionRecord, ...] = ()
    reactions_observable: bool = True
    reactions_detail: str = ""
    checks: tuple[CheckRecord, ...] = ()
    required_checks: tuple[str, ...] = ()
    head_commit_author: str = ""
    head_commit_message: str = ""
    head_commit_diff: tuple[CommitDiffRecord, ...] = ()
    observed_at: datetime | None = None
    rate_limit_remaining: int | None = None
    notes: tuple[str, ...] = field(default=())


class GitHubClient(Protocol):
    """What the application layer may ask of GitHub."""

    def installation_token(
        self, *, installation_id: int, repository: str, permissions: dict[str, str] | None = None
    ) -> InstallationToken: ...

    def checkout_token(self, *, installation_id: int, repository: str) -> InstallationToken:
        """ADR 0019: a fresh token for one private repository's preparation step, scoped
        to that repository with `contents: read` only, never cached."""
        ...

    def revoke_token(self, token: InstallationToken) -> bool:
        """End a token before it expires. True when GitHub says it is gone."""
        ...

    def authenticated_login(self, token: InstallationToken) -> str:
        """Login GitHub attributes to this installation token."""
        ...

    def remote_head(self, token: InstallationToken, *, repository: str, ref: str) -> str | None:
        """The SHA at `refs/heads/<ref>`, or None when the ref does not exist."""
        ...

    def find_pull_request(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> PullRequestRef | None: ...

    def get_pull_request(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> PullRequestRef: ...

    def open_pull_requests(
        self, token: InstallationToken, *, repository: str, head_branch: str
    ) -> Sequence[PullRequestRef]:
        """Every open pull request from the work branch, lowest number first (hades
        #379): a reopened older one beside the task's own is seen too."""
        ...

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
    ) -> PullRequestRef: ...

    def update_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        title: str | None = None,
        body: str | None = None,
        base_ref: str | None = None,
    ) -> PullRequestRef: ...

    def merge_pull_request(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        expected_head_sha: str,
    ) -> MergeResult:
        """Squash-merge only when the pull request still has the expected head."""
        ...

    def observe(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        base_ref: str,
        with_reactions: bool = True,
    ) -> Observation: ...

    def issue_comments(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[CommentRecord, ...]:
        """Issue comments on the pull request, including their author identities."""
        ...

    def reactions_for(
        self, token: InstallationToken, *, repository: str, number: int
    ) -> tuple[ReactionRecord, ...]:
        """Reactions on the PR itself and on each review comment and issue comment.

        Raises `UnobservableError` when the App lacks Issues read for the PR-level call."""
        ...

    def ci_failure_log(
        self,
        token: InstallationToken,
        *,
        repository: str,
        source: str,
        external_id: str,
        limit_bytes: int,
    ) -> bytes:
        """The tail of the failed job's log for a failed check run or workflow run
        (Actions read). Empty when it cannot be read."""
        ...

    def post_issue_comment(
        self, token: InstallationToken, *, repository: str, number: int, body: str
    ) -> CommentRecord:
        """Post one pull request issue comment under the App's identity."""
        ...

    def reply_to_review_comment(
        self,
        token: InstallationToken,
        *,
        repository: str,
        number: int,
        comment_id: str,
        body: str,
    ) -> CommentRecord:
        """Reply on an inline review finding under the App's identity."""
        ...

    def closed_by(self, token: InstallationToken, *, repository: str, number: int) -> str | None:
        """Who closed the pull request, or None when it is not observable."""
        ...

    def delete_ref(self, token: InstallationToken, *, repository: str, ref: str) -> None:
        """Cleanup only; never a branch a task is delivering and never a default branch."""
        ...

    def list_required_checks(
        self, token: InstallationToken, *, repository: str, branch: str
    ) -> Sequence[str]: ...

    def checks_for_commit(
        self, token: InstallationToken, *, repository: str, head_sha: str
    ) -> Sequence[CheckRecord]: ...

    def rerun_failed_jobs(
        self, token: InstallationToken, *, repository: str, run_id: int
    ) -> dict[str, Any]:
        """Re-run all failed jobs of a workflow run (Actions write, issue 435).

        GitHub answers 201 with no body; the new attempt number is read afterwards
        with ``get_workflow_run``.
        """

    def get_installation_permissions(self, *, installation_id: int) -> dict[str, str]:
        """The permissions the installation grants (`GET /app/installations/{id}`).

        Read on every rerun decision to decide whether Hades can act, never assumed.
        Returns ``{"actions": "write", ...}`` when the installation grants Actions write,
        and an empty mapping when the grant cannot be read.
        """

    def workflow_run_for_job(
        self, token: InstallationToken, *, repository: str, job_id: int
    ) -> int | None:
        """The id of the workflow run an Actions job belongs to, or None when the
        check run is not an Actions job (issue 435)."""

    def get_workflow_run(
        self, token: InstallationToken, *, repository: str, run_id: int
    ) -> dict[str, Any]:
        """GET /repos/{owner}/{repo}/actions/runs/{run_id} (issue 435).

        Returns the workflow run object which carries the current ``run_attempt``
        after a rerun.
        """

    def diff_commits(
        self,
        token: InstallationToken,
        *,
        repository: str,
        base_sha: str,
        head_sha: str,
    ) -> Sequence[CommitDiffRecord]:
        """The diff stats between ``base_sha`` and ``head_sha`` (contents read).

        Returns a list of changed-file summaries so the observer can verify that only
        the expected lines in a file changed (hades #443).  Returns an empty list when
        the two SHAs are identical.
        """

        ...


# ----- the App credential the service owns (ADR 0017) ----------------------------------


class GitHubAppStoreError(Exception):
    """The App credential's store refused a read or a write. The message names the
    store and the status, never a value."""


@dataclass(frozen=True, slots=True)
class AppCredential:
    """The App's id and its private key, read for one signature and dropped. `repr`
    never shows the key."""

    app_id: int
    private_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ManifestConversion:
    """What GitHub hands back once for a manifest code (crucible#168): the new App's
    public identity, its first private key and its webhook secret, if it made one.
    `repr` never shows either secret. GitHub's OAuth client secret is not kept: Crucible
    signs as the App and never as a user, so it is dropped where the answer is read."""

    app_id: int
    slug: str
    name: str
    owner: str | None
    html_url: str
    private_key: bytes = field(repr=False)
    webhook_secret: bytes | None = field(default=None, repr=False)


class GitHubAppCredentials(Protocol):
    """Where the App credential lives and the one writer of it (ADR 0017).

    On Kubernetes that is the `hades-github-app` Secret in the service's namespace;
    with the Docker provider it is the files beside `github.app.private_key_path`. A
    credential counts as configured when it has an App id and a key and either the
    service wrote it (the Connect GitHub flow) or `github.enabled` says a deployment
    placed it there on purpose."""

    def describe(self) -> dict[str, Any]:
        """Where it lives and what is there: never a value."""
        ...

    def read(self) -> AppCredential | None:
        """The credential when it is configured, else None. Blocking."""
        ...

    def write(
        self, *, app_id: int, private_key: bytes, webhook_secret: bytes | None
    ) -> dict[str, Any]:
        """Replace the credential whole. Returns what was done, never a value."""
        ...


class GitHubAppDirectory(Protocol):
    """What the App itself can see (crucible#120): its own identity, the accounts it is
    installed on, and the repositories each installation covers. Every token minted to
    list them is discarded before the call returns."""

    def app(self, credential: AppCredential | None = None) -> dict[str, Any]:
        """`GET /app`, signed with `credential` when one is given (to check an id and key
        before they are stored), else with the stored one."""
        ...

    def installations(self) -> list[dict[str, Any]]:
        """`GET /app/installations`: id, account login and type, repository selection."""
        ...

    def installation_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        """`GET /installation/repositories` under that installation."""
        ...

    def convert_manifest(self, code: str) -> ManifestConversion:
        """`POST /app-manifests/{code}/conversions`, unauthenticated: the code GitHub
        redirected the operator's browser back with, good once, for the App it made."""
        ...
