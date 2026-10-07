"""A fake GitHub API server for the integration tier (18, 23).

Real HTTP over loopback, so `RestTransport` and `RestGitHubClient` are exercised rather
than stubbed: the pagination, the `Link` header, the rate-limit wait, the 403 on the
PR-level reactions endpoint, and the token hand-over all go through the code that runs
against api.github.com. CI never touches live GitHub (23).

The shapes come from the endpoints S10 and S12 actually exercised, with the fields 23
names. Anything Crucible does not read is absent on purpose: a fake that is richer than
the reader hides a missing field.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def installation_token_value() -> str:
    """A token of the shape S10 measured: `ghs_` plus about 390 characters with dots.

    Built at run time from `secrets`, never checked in, so no fixture in this repository
    is secret-shaped on disk."""
    body = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(380))
    return f"ghs_{body[:120]}.{body[120:260]}_{body[260:]}"


def now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PullRequestState:
    number: int
    head_branch: str
    base_ref: str
    head_sha: str
    title: str = ""
    body: str = ""
    state: str = "open"
    merged: bool = False
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    merged_by: str | None = None
    closed_at: str | None = None
    draft: bool = False
    mergeable_state: str = "clean"
    # hades #411: GitHub's nullable mergeable flag, false on a conflicting pull request.
    mergeable: bool | None = True
    reviews: list[dict[str, Any]] = field(default_factory=list)
    review_comments: list[dict[str, Any]] = field(default_factory=list)
    issue_comments: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RepositoryState:
    full_name: str
    default_branch: str = "main"
    branches: dict[str, str] = field(default_factory=lambda: {"main": "0" * 40})
    pulls: dict[int, PullRequestState] = field(default_factory=dict)
    check_runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    workflow_runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    required_checks: list[str] = field(default_factory=list)
    # hades #443: commits on the work branch, keyed by SHA.
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_number: int = 1
    # Object ids never repeat, even after a delete: GitHub's do not either, and a reused
    # id would make a new reaction look like one Crucible had already recorded.
    next_object_id: int = 700001


class FakeGitHub:
    """The server's state and the operations tests drive it with."""

    def __init__(self) -> None:
        self.repositories: dict[str, RepositoryState] = {}
        self.tokens: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        # 23: the App lacks Issues read until the operator adds it, and the PR-level
        # reactions endpoint is the one call that needs it. Both paths are tested.
        self.issues_read = True
        self.rate_limit_once = False
        self.mint_failure_once = False
        self.mint_calls = 0
        self.merge_calls: list[tuple[str, int, str]] = []
        self.merge_refusal: tuple[int, str] | None = None
        self.merge_response_failure_once = False
        self.workflow_log = b"fake workflow log: the required check failed\n"
        # The signed log URLs GitHub redirects to, and whether any request for one carried
        # an Authorization header (it must not: the URL is its own credential).
        self.log_downloads: list[str] = []
        self.log_download_authorized = False
        self.lock = threading.Lock()
        # A real git remote standing in for the repository's branches (the kind tier's
        # pushable git host): when set, a branch head is read from it, so what Crucible
        # confirms after a push is what the push actually left there.
        self.ref_source: Callable[[str, str], str | None] | None = None

    def head_of(self, repo: RepositoryState, branch: str) -> str | None:
        if self.ref_source is not None:
            return self.ref_source(repo.full_name, branch)
        return repo.branches.get(branch)

    # ----- test-facing helpers ------------------------------------------

    def add_repository(self, full_name: str, **kw: Any) -> RepositoryState:
        repo = RepositoryState(full_name=full_name, **kw)
        self.repositories[full_name] = repo
        return repo

    def push(self, full_name: str, branch: str, sha: str, *, by: str = "crucible") -> None:
        repo = self.repositories[full_name]
        repo.branches[branch] = sha
        for pull in repo.pulls.values():
            if pull.head_branch == branch and pull.state == "open":
                pull.head_sha = sha
        _ = by

    def add_review(
        self,
        full_name: str,
        number: int,
        *,
        login: str,
        state: str = "COMMENTED",
        body: str = "",
        commit_id: str | None = None,
        comments: list[dict[str, Any]] | None = None,
    ) -> str:
        repo = self.repositories[full_name]
        pull = repo.pulls[number]
        review_id = str(repo.next_object_id)
        repo.next_object_id += 1
        pull.reviews.append(
            {
                "id": review_id,
                "user": {"login": login, "type": "Bot"},
                "state": state,
                "body": body,
                "commit_id": commit_id or pull.head_sha,
                "submitted_at": now_iso(),
            }
        )
        for index, comment in enumerate(comments or []):
            pull.review_comments.append(
                {
                    "id": str(repo.next_object_id + index),
                    "user": {"login": login, "type": "Bot"},
                    "body": comment.get("body", ""),
                    "path": comment.get("path"),
                    "line": comment.get("line"),
                    "commit_id": commit_id or pull.head_sha,
                    "pull_request_review_id": review_id,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "reactions": {"total_count": 0},
                }
            )
        repo.next_object_id += len(comments or [])
        return review_id

    def add_reaction(self, full_name: str, number: int, *, login: str, content: str) -> str:
        repo = self.repositories[full_name]
        pull = repo.pulls[number]
        reaction_id = str(repo.next_object_id)
        repo.next_object_id += 1
        pull.reactions.append(
            {
                "id": reaction_id,
                "user": {"login": login, "type": "Bot"},
                "content": content,
                "created_at": now_iso(),
            }
        )
        return reaction_id

    def add_review_comment(
        self,
        full_name: str,
        number: int,
        *,
        review_id: str,
        login: str,
        body: str,
        path: str,
        line: int,
    ) -> str:
        repo = self.repositories[full_name]
        pull = repo.pulls[number]
        comment_id = str(repo.next_object_id)
        repo.next_object_id += 1
        pull.review_comments.append(
            {
                "id": comment_id,
                "user": {"login": login, "type": "Bot"},
                "body": body,
                "path": path,
                "line": line,
                "commit_id": pull.head_sha,
                "pull_request_review_id": review_id,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "reactions": {"total_count": 0},
            }
        )
        return comment_id

    def remove_reaction(self, full_name: str, number: int, reaction_id: str) -> None:
        pull = self.repositories[full_name].pulls[number]
        pull.reactions = [r for r in pull.reactions if r["id"] != reaction_id]

    def add_issue_comment(self, full_name: str, number: int, *, login: str, body: str) -> str:
        repo = self.repositories[full_name]
        pull = repo.pulls[number]
        comment_id = str(repo.next_object_id)
        repo.next_object_id += 1
        pull.issue_comments.append(
            {
                "id": comment_id,
                "user": {"login": login, "type": "Bot"},
                "body": body,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "reactions": {"total_count": 0},
            }
        )
        return comment_id

    def set_check(
        self,
        full_name: str,
        sha: str,
        *,
        name: str,
        status: str = "completed",
        conclusion: str | None = "success",
        run_id: str = "9001",
        completed_at: str | None = None,
    ) -> None:
        repo = self.repositories[full_name]
        runs = repo.check_runs.setdefault(sha, [])
        done = completed_at or (now_iso() if status == "completed" else None)
        for run in runs:
            if run["name"] == name:
                run.update({"status": status, "conclusion": conclusion, "completed_at": done})
                return
        runs.append(
            {
                "id": run_id,
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "head_sha": sha,
                "html_url": f"https://github.com/{full_name}/runs/{run_id}",
                "app": {"slug": "github-actions"},
                "completed_at": done,
            }
        )

    def rerun_check(
        self,
        full_name: str,
        sha: str,
        *,
        name: str,
        run_id: str,
        status: str = "completed",
        conclusion: str | None = "success",
        completed_at: str | None = None,
    ) -> None:
        """A re-run of a job: GitHub makes a new check run with a new id, and the
        check-runs endpoint (filter=latest) shows only the newest run of each name."""
        repo = self.repositories[full_name]
        repo.check_runs[sha] = [r for r in repo.check_runs.get(sha, []) if r["name"] != name]
        self.set_check(
            full_name,
            sha,
            name=name,
            status=status,
            conclusion=conclusion,
            run_id=run_id,
            completed_at=completed_at,
        )

    def set_workflow_run(
        self,
        full_name: str,
        sha: str,
        *,
        name: str,
        conclusion: str | None,
        run_id: str = "5150",
    ) -> None:
        repo = self.repositories[full_name]
        repo.workflow_runs.setdefault(sha, []).append(
            {
                "id": run_id,
                "name": name,
                "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion,
                "head_sha": sha,
                "html_url": f"https://github.com/{full_name}/actions/runs/{run_id}",
                "path": ".github/workflows/ci.yml",
            }
        )

    def merge(self, full_name: str, number: int, *, by: str, sha: str) -> None:
        pull = self.repositories[full_name].pulls[number]
        pull.state = "closed"
        pull.merged = True
        pull.merged_at = now_iso()
        pull.merge_commit_sha = sha
        pull.merged_by = by
        pull.closed_at = pull.merged_at

    def close(self, full_name: str, number: int, *, by: str = "") -> None:
        pull = self.repositories[full_name].pulls[number]
        pull.state = "closed"
        pull.closed_at = now_iso()
        if by:
            # GitHub records the closer on the issue timeline, not on the pull request.
            pull.events.append(
                {"event": "closed", "actor": {"login": by}, "created_at": pull.closed_at}
            )

    # ----- hades #443: commit helpers for the fake -----------------------

    def record_commit(
        self,
        full_name: str,
        sha: str,
        *,
        author_login: str = "crucible",
        message: str = "",
        files: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record a commit so the compare endpoint can answer diff requests."""
        repo = self.repositories[full_name]
        repo.commits[sha] = {
            "sha": sha,
            "author": {"login": author_login},
            "commit": {"message": message},
            "files": files or [],
            "parents": [{"sha": repo.branches.get("main", "0" * 40)}],
        }
        # Make the commit on the default branch so remote_head returns it.
        repo.branches["main"] = sha

    def diff_commits(
        self, full_name: str, base_sha: str, head_sha: str
    ) -> list[dict[str, Any]]:
        """Return the compare diff between two commits (hades #443)."""
        repo = self.repositories[full_name]
        if base_sha == head_sha:
            return []
        head_commit = repo.commits.get(head_sha)
        if head_commit is None:
            return []
        return head_commit.get("files", [])

    # ----- hades #443: compare endpoint for the test server ---------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-github/1.0"

    @property
    def state(self) -> FakeGitHub:
        state: FakeGitHub = self.server.state  # type: ignore[attr-defined]
        return state

    def log_message(self, *args: Any) -> None:  # keep the test output readable
        return

    def _send(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-ratelimit-remaining", "4999")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and len(header) > 12

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ----- routing ------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        with self.state.lock:
            self.state.calls.append((method, path))
        if path.startswith("/_signed-logs/"):
            # GitHub's signed log URL, on another host in real life: no token needed, and
            # none may be sent.
            self.state.log_downloads.append(path)
            if self.headers.get("Authorization"):
                self.state.log_download_authorized = True
            payload = self.state.workflow_log
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if not self._authorized():
            self._send(401, {"message": "Bad credentials"})
            return
        if self.state.rate_limit_once:
            self.state.rate_limit_once = False
            self._send(
                403,
                {"message": "API rate limit exceeded"},
                {"x-ratelimit-remaining": "0", "retry-after": "1"},
            )
            return
        parts = [p for p in path.split("/") if p]
        try:
            self._route(method, parts, query)
        except KeyError:
            self._send(404, {"message": "Not Found"})

    def _route(self, method: str, parts: list[str], query: dict[str, list[str]]) -> None:
        state = self.state
        if method == "GET" and parts == ["app"]:
            self._send(200, {"slug": "crucible-spike"})
            return
        if parts[:2] == ["app", "installations"] and parts[-1] == "access_tokens":
            state.mint_calls += 1
            if state.mint_failure_once:
                state.mint_failure_once = False
                self._send(503, {"message": "Service Unavailable"})
                return
            token = installation_token_value()
            state.tokens.add(token)
            body = self._body() or {}
            self._send(
                201,
                {
                    "token": token,
                    "expires_at": now_iso(3600),
                    "repository_selection": "selected",
                    "repositories": [
                        {"full_name": f"owner/{name}"} for name in body.get("repositories", [])
                    ],
                    "permissions": body.get(
                        "permissions",
                        {
                            "actions": "read",
                            "checks": "read",
                            "contents": "write",
                            "metadata": "read",
                            "pull_requests": "write",
                        },
                    ),
                },
            )
            return
        if parts[0] != "repos" or len(parts) < 3:
            self._send(404, {"message": "Not Found"})
            return
        full_name = f"{parts[1]}/{parts[2]}"
        repo = state.repositories[full_name]
        rest = parts[3:]
        if rest[:2] == ["git", "ref"] and rest[2:3] == ["heads"]:
            branch = "/".join(rest[3:])
            sha = state.head_of(repo, branch)
            if sha is None:
                self._send(404, {"message": "Not Found"})
                return
            self._send(200, {"ref": f"refs/heads/{branch}", "object": {"sha": sha}})
            return
        if rest[:2] == ["git", "refs"] and method == "DELETE":
            branch = "/".join(rest[3:])
            repo.branches.pop(branch, None)
            self._send(204, None)
            return
        if rest[:1] == ["pulls"] and len(rest) == 1:
            if method == "POST":
                self._create_pull(repo)
                return
            self._list_pulls(repo, query)
            return
        if rest[:1] == ["pulls"] and rest[1:2] == ["comments"] and rest[3:] == ["reactions"]:
            self._send(200, [])
            return
        if (
            method == "POST"
            and rest[:1] == ["pulls"]
            and len(rest) == 5
            and rest[2:3] == ["comments"]
            and rest[4:] == ["replies"]
        ):
            number = int(rest[1])
            body = self._body() or {}
            comment_id = str(repo.next_object_id)
            repo.next_object_id += 1
            row = {
                "id": comment_id,
                "user": {"login": "crucible-spike[bot]", "type": "Bot"},
                "body": str(body.get("body", "")),
                "path": None,
                "line": None,
                "commit_id": repo.pulls[number].head_sha,
                "pull_request_review_id": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "reactions": {"total_count": 0},
            }
            repo.pulls[number].review_comments.append(row)
            self._send(201, row)
            return
        if rest[:1] == ["pulls"] and len(rest) >= 2 and rest[1].isdigit():
            number = int(rest[1])
            pull = repo.pulls[number]
            tail = rest[2:]
            if tail == ["merge"] and method == "PUT":
                body = self._body() or {}
                expected_sha = str(body.get("sha", ""))
                merge_method = str(body.get("merge_method", ""))
                state.merge_calls.append((full_name, number, merge_method))
                if expected_sha != pull.head_sha:
                    self._send(409, {"message": "Head branch was modified"})
                    return
                if state.merge_refusal is not None:
                    status, message = state.merge_refusal
                    self._send(status, {"message": message})
                    return
                merge_sha = "f" * 40
                state.merge(full_name, number, by="crucible-spike[bot]", sha=merge_sha)
                if state.merge_response_failure_once:
                    state.merge_response_failure_once = False
                    self._send(502, {"message": "response lost after merge"})
                    return
                self._send(
                    200,
                    {
                        "merged": True,
                        "message": "Pull Request successfully merged",
                        "sha": merge_sha,
                    },
                )
                return
            if not tail and method == "GET":
                self._send(200, self._pull_json(repo, pull))
                return
            if not tail and method == "PATCH":
                body = self._body() or {}
                if "title" in body:
                    pull.title = str(body["title"])
                if "body" in body:
                    pull.body = str(body["body"])
                if body.get("state") == "closed":
                    pull.state = "closed"
                    pull.closed_at = now_iso()
                self._send(200, self._pull_json(repo, pull))
                return
            if tail == ["reviews"]:
                self._send(200, pull.reviews)
                return
            if tail == ["comments"]:
                self._send(200, pull.review_comments)
                return
        if rest[:1] == ["issues"] and rest[1:2] == ["comments"] and rest[3:] == ["reactions"]:
            self._send(200, [])
            return
        if rest[:1] == ["issues"] and len(rest) >= 2 and rest[1].isdigit():
            number = int(rest[1])
            pull = repo.pulls[number]
            tail = rest[2:]
            if tail == ["comments"]:
                if method == "POST":
                    body = self._body() or {}
                    comment_id = self.state.add_issue_comment(
                        repo.full_name,
                        number,
                        login="crucible-spike[bot]",
                        body=str(body.get("body", "")),
                    )
                    comment = next(
                        row for row in pull.issue_comments if str(row["id"]) == comment_id
                    )
                    self._send(201, comment)
                    return
                self._send(200, pull.issue_comments)
                return
            if tail == ["events"]:
                self._send(200, pull.events)
                return
            if tail == ["reactions"]:
                # S12: this is the one call that needs Issues read, and the App does not
                # hold it until the operator adds it.
                if not self.state.issues_read:
                    self._send(
                        403,
                        {"message": "Resource not accessible by integration"},
                        {"x-accepted-github-permissions": "issues=read"},
                    )
                    return
                self._send(200, pull.reactions)
                return
        if rest[:1] == ["commits"] and len(rest) == 3:
            sha = rest[1]
            if rest[2] == "check-runs":
                # Paginated like GitHub: `per_page` (default 30) and `page`, with a `Link`
                # header while there is more.
                runs = repo.check_runs.get(sha, [])
                per_page = int((query.get("per_page") or ["30"])[0])
                page = int((query.get("page") or ["1"])[0])
                chunk = runs[(page - 1) * per_page : page * per_page]
                headers = {}
                if page * per_page < len(runs):
                    headers["Link"] = f'<{self.path}&page={page + 1}>; rel="next"'
                self._send(200, {"total_count": len(runs), "check_runs": chunk}, headers)
                return
            if rest[2] == "check-suites":
                self._send(200, {"total_count": 0, "check_suites": []})
                return
        if rest[:2] == ["actions", "runs"] and len(rest) == 2:
            sha = (query.get("head_sha") or [""])[0]
            runs = repo.workflow_runs.get(sha, [])
            self._send(200, {"total_count": len(runs), "workflow_runs": runs})
            return
        if rest[:2] == ["actions", "runs"] and rest[3:] == ["jobs"]:
            run_id = rest[2]
            jobs = [
                {"id": f"{run_id}01", "name": "build", "conclusion": "failure"},
            ]
            self._send(200, {"total_count": len(jobs), "jobs": jobs})
            return
        if rest[:2] == ["actions", "jobs"] and rest[3:] == ["logs"]:
            host = self.headers.get("Host", "")
            self.send_response(302)
            self.send_header("Location", f"http://{host}/_signed-logs/job/{rest[2]}?sig=x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rest[:2] == ["actions", "runs"] and rest[3:] == ["logs"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            payload = self.state.workflow_log
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if rest[:1] == ["branches"] and rest[2:] == [
            "protection",
            "required_status_checks",
        ]:
            if not repo.required_checks:
                self._send(404, {"message": "Branch not protected"})
                return
            self._send(200, {"contexts": repo.required_checks, "checks": []})
            return
        if rest[:2] == ["rules", "branches"]:
            self._send(200, [])
            return
        # hades #443: /repos/{owner}/{repo}/compare/{base}...{head}
        if rest[:1] == ["compare"] and len(rest) >= 3:
            base_part = rest[-1] if len(rest) == 3 else rest[-1]
            # Handle ... separator: rest[1] might be "base...head" or "base" with rest[2] as "head"
            compare_ref = parts[-1]  # Everything after /repos/owner/repo/
            if "..." in compare_ref:
                base_sha, head_sha = compare_ref.split("...", 1)
            else:
                base_sha = rest[1] if len(rest) >= 2 else ""
                head_sha = rest[2] if len(rest) >= 3 else ""
            diff = self.state.diff_commits(full_name, base_sha, head_sha)
            head_commit = repo.commits.get(head_sha, {})
            base_commit_sha = ""
            if head_commit.get("parents"):
                base_commit_sha = head_commit["parents"][0].get("sha", "")
            self._send(
                200,
                {
                    "base_commit": {
                        "sha": base_commit_sha,
                        "author": {"login": "crucible-spike[bot]"},
                        "commit": {"message": ""},
                    },
                    "head_commit": {
                        "sha": head_sha,
                        "author": head_commit.get("author", {"login": "crucible"}),
                        "commit": {"message": head_commit.get("commit", {}).get("message", "")},
                    },
                    "files": diff,
                    "total_commits": 1,
                },
            )
            return
        self._send(404, {"message": "Not Found"})

    def _create_pull(self, repo: RepositoryState) -> None:
        body = self._body() or {}
        head = str(body.get("head", ""))
        number = repo.next_number
        repo.next_number += 1
        pull = PullRequestState(
            number=number,
            head_branch=head,
            base_ref=str(body.get("base", repo.default_branch)),
            head_sha=self.state.head_of(repo, head) or "0" * 40,
            title=str(body.get("title", "")),
            body=str(body.get("body", "")),
            draft=bool(body.get("draft", False)),
        )
        repo.pulls[number] = pull
        self._send(201, self._pull_json(repo, pull))

    def _list_pulls(self, repo: RepositoryState, query: dict[str, list[str]]) -> None:
        wanted = (query.get("head") or [""])[0]
        branch = wanted.split(":", 1)[-1] if wanted else ""
        rows = [
            self._pull_json(repo, pull)
            for pull in repo.pulls.values()
            if not branch or pull.head_branch == branch
        ]
        self._send(200, rows)

    def _pull_json(self, repo: RepositoryState, pull: PullRequestState) -> dict[str, Any]:
        return {
            "number": pull.number,
            "html_url": f"https://github.com/{repo.full_name}/pull/{pull.number}",
            "state": pull.state,
            "title": pull.title,
            "body": pull.body,
            "draft": pull.draft,
            "merged": pull.merged,
            "merged_at": pull.merged_at,
            "merge_commit_sha": pull.merge_commit_sha,
            "merged_by": {"login": pull.merged_by} if pull.merged_by else None,
            "closed_at": pull.closed_at,
            "mergeable_state": pull.mergeable_state,
            "mergeable": pull.mergeable,
            "head": {"sha": pull.head_sha, "ref": pull.head_branch},
            "base": {"ref": pull.base_ref},
        }


class FakeGitHubServer:
    """A running fake on loopback. `url` is what `RestTransport` is pointed at."""

    def __init__(self) -> None:
        self.state = FakeGitHub()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeGitHubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"
