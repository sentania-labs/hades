"""Fake execution provider (08): deterministic, no model, no containers.

The behavior is selected by the image tag on the launch spec: `<repo>:fake-<behavior>`
optionally followed by `-<n>` observations before the scripted exit. Behaviors:

- succeed            exit 0 with a valid CompletionClaimV1 report
- succeed-noreport   exit 0 with no report (completed_without_report)
- blocked            exit 75 with blocked.md
- blocked-nofile     exit 75 without blocked.md (a plain failure)
- crash              exit 1, no report
- oom                exit 137 with the kernel's OOM kill flagged (environment, S5)
- bad-report         exit 0 with a report.yaml that is not valid YAML (report_parse_failed)
- environment        exit 70
- hang               never exits; ignores drain, dies on kill
- immortal           ignores drain and the first kill, dies on the second kill
- vanish             disappears after launch (loss)
- review             exit 0 with a valid ReviewReportV1 (used by a `review` execution)
- review-disapprove  exit 0 with a ReviewReportV1 whose verdict is request_changes
- out-of-scope       exit 0 with a report, but the diff touches a path outside allowed_paths
                     (advisory by default, ADR 0024: the task goes on to its review)
- prohibited-path    exit 0 with a report, but the diff touches a path matching
                     prohibited_paths, which always stops the task (ADR 0024)
- injected           exit 0 with a report, but the branch carries a `.crucible/` path
- secret-leak        exit 0 with a report, but the scanner matches a credential shape
- no-commits         exit 0 with a report, but the collected branch has no commit
- quota              exit 1 with provider quota output and a synthetic WIP commit

A test may also script a behavior per external_id with `script()`, which wins over
the image tag. Handles survive as long as the provider instance does, which is how the
"supervisor restart mid-attempt" test keeps a worker alive across supervisors.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from crucible.contracts.evidence import REVIEW_DIFF_NAME, REVIEW_DIFF_TYPE
from crucible.ports.execution import (
    BranchBundle,
    CancelCheck,
    CleanupPolicy,
    CollectedArtifact,
    CollectedOutputs,
    CommitPolicyCheck,
    Handle,
    ImageInfo,
    IsolationLevel,
    LaunchCancelledError,
    LaunchSpec,
    LogChunk,
    LogOffset,
    Observation,
    ObservationState,
    ProbeRequest,
    ProbeResult,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    ProviderUnavailableError,
    VerificationRun,
    Workspace,
    WorkspaceState,
)
from crucible.ports.github import InstallationToken

PROVIDER_NAME = "fake"


@dataclass(frozen=True, slots=True)
class CheckoutTokenSeen:
    """A checkout token as `prepare` received it (ADR 0019). `token` is the object
    itself, so a test can check it was discarded once `prepare` returned."""

    repository: str
    permissions: dict[str, str]
    had_value: bool
    token: InstallationToken


_TAG = re.compile(r"^.*:fake-(?P<behavior>[a-z]+(?:-[a-z]+)*)(?:-(?P<n>\d+))?$")

Behavior = Literal[
    "succeed",
    "succeed-noreport",
    "blocked",
    "blocked-nofile",
    "crash",
    "oom",
    "bad-report",
    "environment",
    "hang",
    "immortal",
    "vanish",
    "prepare-fails",
    "review",
    "review-disapprove",
    "out-of-scope",
    "prohibited-path",
    "malformed-report",
    "injected",
    "secret-leak",
    "no-commits",
    "quota",
    "verification-fails",
    "dirty-workspace",
    "other-author",
]
BEHAVIORS: frozenset[str] = frozenset(
    {
        "succeed",
        "succeed-noreport",
        "blocked",
        "blocked-nofile",
        "crash",
        "oom",
        "bad-report",
        "environment",
        "hang",
        "immortal",
        "vanish",
        "prepare-fails",
        "review",
        "review-disapprove",
        "out-of-scope",
        "prohibited-path",
        "malformed-report",
        "injected",
        "secret-leak",
        "no-commits",
        "quota",
        "verification-fails",
        "dirty-workspace",
        "other-author",
    }
)

# Behaviors that exit 0 with a CompletionClaimV1; the gates then tell them apart.
REPORTING_BEHAVIORS: frozenset[str] = frozenset(
    {
        "succeed",
        "out-of-scope",
        "prohibited-path",
        "malformed-report",
        "injected",
        "secret-leak",
        "no-commits",
        "verification-fails",
        "dirty-workspace",
        "other-author",
    }
)
REVIEW_BEHAVIORS: frozenset[str] = frozenset({"review", "review-disapprove"})

OTHER_AUTHOR = "someone-else@example.test"
OUT_OF_SCOPE_PATH = "infrastructure/outside-the-contract.txt"
MALFORMED_REPORT = "summary: c5: live run\nrisks: none\n"
# Under the test contract's `.github/**` prohibition and outside every CI path.
PROHIBITED_PATH = ".github/CODEOWNERS"
INJECTED_PATH = ".crucible/identity.md"


def synthetic_head_sha(attempt_id: str) -> str:
    """A deterministic 40-hex stand-in for the head the collector would read (08)."""
    return hashlib.sha256(f"crucible-fake-head:{attempt_id}".encode()).hexdigest()[:40]


def fake_commit_policy(behavior: str, head: str) -> CommitPolicyCheck:
    """The collector's commit check as the fake reports it: `other-author` is a worker
    whose one commit is authored by someone other than the policy's author and carries
    no attempt trailer (hades FDY-0143); every other behavior commits as the policy
    asks."""
    if behavior == "other-author":
        return CommitPolicyCheck(author_problems=((head, OTHER_AUTHOR),))
    return CommitPolicyCheck()


def _concrete(pattern: str) -> str:
    """Turn an allowed_paths glob into one concrete path under it."""
    path = pattern.replace("/**", "/fake_change.py").replace("**", "fake_change.py")
    path = path.replace("*", "fake")
    return posixpath.normpath(path)


def changed_paths(contract: dict[str, Any], behavior: str) -> tuple[str, ...]:
    allowed = [str(p) for p in contract.get("scope", {}).get("allowed_paths", [])]
    paths = [_concrete(p) for p in allowed[:2]] or ["src/fake_change.py"]
    if behavior == "out-of-scope":
        paths.append(OUT_OF_SCOPE_PATH)
    if behavior == "prohibited-path":
        paths.append(PROHIBITED_PATH)
    if behavior == "injected":
        paths.append(INJECTED_PATH)
    return tuple(dict.fromkeys(paths))


def synthetic_diff(paths: tuple[str, ...], behavior: str) -> str:
    """A unified diff over the changed paths, so the scanner has content to read (11)."""
    chunks = []
    for path in paths:
        body = f"+# fake change for {path}\n"
        if behavior == "secret-leak" and path == paths[0]:
            # Built here at runtime; no secret-shaped literal is ever committed (12).
            body += '+TOKEN = "' + "gh" + "p_" + "A" * 36 + '"\n'
        chunks.append(
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{body.count(chr(10))} @@\n{body}"
        )
    return "".join(chunks)


def synthetic_review_diff(paths: tuple[str, ...], behavior: str) -> str:
    """The collector-owned review artifact: a stat header followed by the patch."""
    stat = "".join(f" {path} | 1 +\n" for path in paths)
    if paths:
        stat += f" {len(paths)} file(s) changed, {len(paths)} insertion(s)(+)\n"
    return stat + "\n" + synthetic_diff(paths, behavior)


def verification_runs(contract: dict[str, Any], behavior: str) -> tuple[VerificationRun, ...]:
    """Crucible's own re-run of every required command (11). The fake verifier agrees
    with the contract unless the behavior asks it not to."""
    runs: list[VerificationRun] = []
    for index, check in enumerate(contract.get("required_verification", [])):
        if str(check.get("kind", "command")) != "command":
            continue
        expect = int(check.get("expect_exit", 0))
        failed = behavior == "verification-fails" and index == 0
        runs.append(
            VerificationRun(
                id=str(check.get("id")),
                command=str(check.get("command")),
                expect_exit=expect,
                exit_code=expect + 1 if failed else expect,
                log_tail=f"fake verifier re-ran {check.get('command')!r}",
            )
        )
    return tuple(runs)


def workspace_state(behavior: str, attempt_id: str) -> WorkspaceState:
    if behavior == "dirty-workspace":
        return WorkspaceState(leftover=(f"crucible-{attempt_id}-leftover",))
    return WorkspaceState()


def default_review_report(spec: LaunchSpec, head_sha: str, verdict: str) -> dict[str, Any]:
    """A valid ReviewReportV1 from a `review` execution's attempt."""
    findings = (
        []
        if verdict == "approve"
        else [
            {
                "severity": "major",
                "path": "src/fake_change.py",
                "line": 1,
                "text": "The fake reviewer wants a narrower change.",
            }
        ]
    )
    return {
        "schema_version": "1.0",
        "task_external_id": spec.external_id,
        "reviewed_head_sha": head_sha,
        "reviewer": {"kind": "crucible_review_execution", "attempt_id": spec.attempt_id},
        "verdict": verdict,
        "findings": findings,
        "summary": f"Fake non-author review of {head_sha} for {spec.external_id}.",
    }


@dataclass(slots=True)
class _Worker:
    spec: LaunchSpec
    behavior: str
    remaining: int
    state: ObservationState = ObservationState.RUNNING
    exit_code: int | None = None
    killed: bool = False
    observations: int = 0
    drains: int = 0
    kills: int = 0
    logs: list[LogChunk] = field(default_factory=list)
    oom_killed: bool = False


def default_report(
    spec: LaunchSpec, head_sha: str | None = None, behavior: str = "succeed"
) -> dict[str, Any]:
    """A valid CompletionClaimV1 for the contract the worker was given."""
    contract = spec.contract
    head = head_sha or synthetic_head_sha(spec.attempt_id)
    evidence_paths = [
        str(v["path"])
        for v in contract.get("required_verification", [])
        if v.get("kind") == "artifact"
    ]
    checks = [
        {
            "id": v["id"],
            "command": v["command"],
            "exit": int(v.get("expect_exit", 0)),
            "log": f"{v['id']}.log",
        }
        for v in contract.get("required_verification", [])
        if v.get("kind", "command") == "command"
    ]
    return {
        "schema_version": "1.0",
        "task_external_id": spec.external_id,
        "summary": f"Fake worker completed {spec.external_id}.",
        "self_review": {
            "documentation": ["No documentation changed: this is a synthetic worker run."],
            "acceptance_criteria": [
                {"id": c["id"], "status": "met", "evidence": "report/evidence.md"}
                for c in contract.get("acceptance_criteria", [])
            ],
            "omissions": [],
        },
        "changed_files": list(changed_paths(contract, behavior)),
        "refs": {
            "branch": contract.get("repository", {}).get("work_branch", "crucible/unknown"),
            "head_sha": head,
            "commits": 0 if behavior == "no-commits" else 1,
        },
        "checks": checks,
        "acceptance_mapping": [
            {"id": c["id"], "status": "met", "evidence": "report/evidence.md"}
            for c in contract.get("acceptance_criteria", [])
        ],
        "run_evidence": evidence_paths,
        "proposed_pull_request": {
            "title": contract.get("title", "Fake change"),
            "body": "Fake worker output.",
            "closes": [],
        },
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }


class FakeProvider:
    name = PROVIDER_NAME

    def __init__(self) -> None:
        # Attempts whose credential copy the supervisor asked to discard (12).
        self.gate_probe_calls: list[str] = []
        self.gate_probe_exits: dict[str, int] = {}
        self.gate_probe_error: str | None = None
        self.discarded: list[str] = []
        # Harnesses the administrative probe was run for (25).
        self.probes: list[str] = []
        # The whole request of each probe, so a test can see which model it would run.
        self.probe_requests: list[ProbeRequest] = []
        # "completed", "timeout" or "auth_failure": what the next probe reports.
        self.probe_outcome = "completed"
        # What `list_images` answers: whatever a test hands it (08, 25).
        self.images: list[ImageInfo] = []
        self._workers: dict[str, _Worker] = {}
        self._scripts: dict[str, tuple[str, int]] = {}
        self._reports: dict[str, dict[str, Any]] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._review_heads: dict[str, str] = {}
        self.cleaned: list[str] = []
        self.cleanup_policies: dict[str, CleanupPolicy] = {}
        # The attempts whose kept workspace the retention step released (16), in order.
        self.released: list[str] = []
        self.release_fails = False
        # The checkout token each private repository's prepare received (ADR 0019).
        self.checkout_tokens: dict[str, CheckoutTokenSeen] = {}
        # hades #189, #190: prepares a test holds open, by external id, until it
        # releases them; and the attempts whose prepare stopped for a cancel.
        self._prepare_holds: dict[str, asyncio.Event] = {}
        self._collect_holds: dict[str, asyncio.Event] = {}
        self._collect_outages: dict[str, int] = {}
        # Each collection started, by attempt id, retries included.
        self.collect_calls: list[str] = []
        self.prepare_cancelled: list[str] = []
        self.prepares_started: list[str] = []
        # hades #189: launches a test holds open, and those that stopped for a cancel.
        self._launch_holds: dict[tuple[str, str], asyncio.Event] = {}
        self.launch_cancelled: list[str] = []

    # test controls
    def script(self, external_id: str, behavior: str, *, after: int = 1) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown fake behavior {behavior!r}")
        self._scripts[external_id] = (behavior, after)

    def hold_prepare(self, external_id: str) -> None:
        """Hold the next prepare of this task open, as a refresher that cannot connect
        would, until `release_prepare` or a cancel ends it."""
        self._prepare_holds[external_id] = asyncio.Event()

    def release_prepare(self, external_id: str) -> None:
        hold = self._prepare_holds.pop(external_id, None)
        if hold is not None:
            hold.set()

    def hold_collect(self, external_id: str) -> asyncio.Event:
        """Hold the next collection of this task open until the returned event is set,
        as a collector, verifier and reader Pods that take their whole timeouts would."""
        hold = asyncio.Event()
        self._collect_holds[external_id] = hold
        return hold

    def collect_unavailable(self, external_id: str, times: int = 1) -> None:
        """The next `times` collections of this task fail as a cluster that could not
        answer (ProviderUnavailableError), with the workspace left in place."""
        self._collect_outages[external_id] = times

    def hold_launch(self, external_id: str, where: Literal["before", "after"]) -> asyncio.Event:
        """Hold the next launch of this task open until the returned event is set:
        `before` the launch's cancel look (a slow readiness gate or image resolution),
        or `after` the worker was created (a slow Pod start)."""
        hold = asyncio.Event()
        self._launch_holds[(external_id, where)] = hold
        return hold

    def set_report(self, external_id: str, report: dict[str, Any]) -> None:
        self._reports[external_id] = report

    def worker(self, attempt_id: str) -> _Worker | None:
        return self._workers.get(attempt_id)

    def _behavior_for(self, spec: LaunchSpec) -> tuple[str, int]:
        scripted = self._scripts.get(spec.external_id)
        if scripted is not None:
            return scripted
        match = _TAG.match(spec.image)
        if match is None:
            raise ProviderError(f"fake provider cannot run image {spec.image!r}")
        behavior = match.group("behavior")
        if behavior not in BEHAVIORS:
            raise ProviderError(f"unknown fake behavior {behavior!r} in image {spec.image!r}")
        return behavior, int(match.group("n") or 1)

    # provider contract
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            isolation=IsolationLevel.NONE,
            network_control=False,
            resource_limits=False,
            shared_disk=True,
            supports_harnesses=frozenset({"claude_code", "codex", "agy"}),
            max_concurrency=16,
        )

    async def credential_available(self, harness: str) -> bool:
        return False

    async def probe_checks(
        self,
        spec: LaunchSpec,
        checks: Sequence[dict[str, Any]],
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> tuple[VerificationRun, ...] | None:
        self.gate_probe_calls.append(spec.attempt_id)
        if checkout_token is not None:
            self.checkout_tokens[f"probe:{spec.attempt_id}"] = CheckoutTokenSeen(
                repository=checkout_token.repository,
                permissions=dict(checkout_token.permissions),
                had_value=bool(checkout_token.reveal()),
                token=checkout_token,
            )
        if self.gate_probe_error:
            raise ProviderError(self.gate_probe_error)
        return tuple(
            VerificationRun(
                id=check["id"],
                command=check["command"],
                expect_exit=int(check.get("expect_exit", 0)),
                exit_code=self.gate_probe_exits.get(check["id"], 1),
                log_tail="program: not found"
                if self.gate_probe_exits.get(check["id"]) == 127
                else "",
            )
            for check in checks
        )

    async def gate_probe_exists(self, attempt_id: str) -> bool:
        return False

    async def prepare(
        self,
        spec: LaunchSpec,
        checkout_token: InstallationToken | None = None,
        cancelled: CancelCheck | None = None,
    ) -> Workspace:
        behavior, _ = self._behavior_for(spec)
        self.prepares_started.append(spec.attempt_id)
        hold = self._prepare_holds.get(spec.external_id)
        while True:
            if cancelled is not None and await cancelled():
                self.prepare_cancelled.append(spec.attempt_id)
                raise LaunchCancelledError("the task was cancelled during prepare")
            if hold is None or hold.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(hold.wait(), timeout=0.02)
        if checkout_token is not None:
            # What a test needs to see about the token: which repository and which
            # permissions it was minted for, and that its value was there to hand over.
            self.checkout_tokens[spec.attempt_id] = CheckoutTokenSeen(
                repository=checkout_token.repository,
                permissions=dict(checkout_token.permissions),
                had_value=bool(checkout_token.reveal()),
                token=checkout_token,
            )
        if behavior == "prepare-fails":
            raise ProviderError("fake prepare failure")
        root = f"fake:///workspaces/{spec.attempt_id}"
        ws = Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=f"{root}/repo",
            identity_path=f"{root}/identity",
            report_path=f"{root}/report",
        )
        self._workspaces[spec.attempt_id] = ws
        return ws

    def review_head(self, attempt_id: str, head_sha: str) -> None:
        """Tell a review worker which head it is reviewing (the supervisor passes it)."""
        self._review_heads[attempt_id] = head_sha

    async def launch(
        self, ws: Workspace, spec: LaunchSpec, cancelled: CancelCheck | None = None
    ) -> Handle:
        behavior, after = self._behavior_for(spec)
        await self._held(self._launch_holds.pop((spec.external_id, "before"), None))
        if cancelled is not None and await cancelled():
            self.launch_cancelled.append(spec.attempt_id)
            raise LaunchCancelledError("the task was cancelled before the worker was created")
        # The image tag decides, for a review execution as for any other: a review that
        # crashes or vanishes is exactly what the review failure paths have to survive.
        review_head = spec.env.get("CRUCIBLE_REVIEW_HEAD_SHA")
        if review_head:
            self._review_heads[spec.attempt_id] = review_head
        worker = _Worker(spec=spec, behavior=behavior, remaining=after)
        worker.logs.append(LogChunk("stdout", f"fake worker {behavior} start\n".encode()))
        self._workers[spec.attempt_id] = worker
        # A worker that exists while its launch still waits, as on a Pod that is slow
        # to be scheduled: a cancel now is the supervisor's to act on.
        await self._held(self._launch_holds.pop((spec.external_id, "after"), None))
        return Handle(provider=self.name, ref=f"fake-{spec.attempt_id}", attempt_id=spec.attempt_id)

    @staticmethod
    async def _held(hold: asyncio.Event | None) -> None:
        if hold is not None:
            await hold.wait()

    async def observe(self, h: Handle) -> Observation:
        worker = self._workers.get(h.attempt_id)
        if worker is None:
            return Observation(ObservationState.LOST, detail="no such worker")
        if worker.state is not ObservationState.RUNNING:
            return Observation(worker.state, exit_code=worker.exit_code)
        worker.observations += 1
        if worker.behavior in ("hang", "immortal"):
            return Observation(ObservationState.RUNNING)
        if worker.observations < worker.remaining:
            return Observation(ObservationState.RUNNING)
        if worker.behavior == "vanish":
            del self._workers[h.attempt_id]
            return Observation(ObservationState.LOST, detail="worker vanished")
        worker.state = ObservationState.EXITED
        worker.exit_code = {
            "blocked": 75,
            "blocked-nofile": 75,
            "crash": 1,
            "oom": 137,
            "environment": 70,
            "quota": 1,
        }.get(worker.behavior, 0)
        worker.logs.append(LogChunk("stdout", f"fake worker exit {worker.exit_code}\n".encode()))
        worker.oom_killed = worker.behavior == "oom"
        return Observation(
            ObservationState.EXITED, exit_code=worker.exit_code, oom_killed=worker.oom_killed
        )

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]:
        worker = self._workers.get(h.attempt_id)
        return [] if worker is None else worker.logs[since.index :]

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs:
        worker = self._workers.get(h.attempt_id)
        if worker is None:
            return CollectedOutputs(report=None, report_raw=None, blocked_md=None)
        spec = worker.spec
        self.collect_calls.append(spec.attempt_id)
        await self._held(self._collect_holds.pop(spec.external_id, None))
        outages = self._collect_outages.get(spec.external_id, 0)
        if outages > 0:
            self._collect_outages[spec.external_id] = outages - 1
            raise ProviderUnavailableError("the fake cluster could not answer the collection")
        behavior = worker.behavior
        head = synthetic_head_sha(spec.attempt_id)
        if behavior == "quota":
            worker.logs.append(
                LogChunk(
                    "stderr",
                    b'{"type":"turn.failed","error":{"code":"usage_limit_reached"}}\n',
                )
            )
            quota_paths = ("src/ledger/quota-wip.txt",)
            bundle = BranchBundle(
                head_sha=head,
                base_ref=str(spec.contract.get("repository", {}).get("base_ref", "main")),
                work_branch=str(spec.contract.get("repository", {}).get("work_branch", "")),
                commits=1,
                verified=True,
                sha256=hashlib.sha256(f"fake-bundle:{head}".encode()).hexdigest(),
                commit_paths=quota_paths,
                commit_messages=(f"wip(crucible): attempt {spec.attempt_id}",),
                commit_policy=fake_commit_policy(behavior, head),
            )
            return CollectedOutputs(
                report=None,
                report_raw=None,
                blocked_md=None,
                diff_paths=quota_paths,
                diff_text=synthetic_diff(quota_paths, "succeed"),
                bundle=bundle,
                stdout_tail="",
                stderr_tail=('{"type":"turn.failed","error":{"code":"usage_limit_reached"}}'),
            )
        if behavior == "bad-report" and worker.exit_code == 0:
            # A file that exists and is not a YAML mapping: an unquoted colon in a value.
            return CollectedOutputs(
                report=None, report_raw="title: c5: live run\nsummary: x\n", blocked_md=None
            )
        if behavior in REVIEW_BEHAVIORS and worker.exit_code == 0:
            verdict = "approve" if behavior == "review" else "request_changes"
            head = self._review_heads.get(spec.attempt_id, head)
            report = self._reports.get(spec.external_id) or default_review_report(
                spec, head, verdict
            )
            return CollectedOutputs(report=report, report_raw=None, blocked_md=None)
        if behavior in REPORTING_BEHAVIORS and worker.exit_code == 0:
            report = self._reports.get(spec.external_id) or default_report(spec, head, behavior)
            paths = changed_paths(spec.contract, behavior)
            commits = 0 if behavior == "no-commits" else 1
            bundle = BranchBundle(
                head_sha=head,
                base_ref=str(spec.contract.get("repository", {}).get("base_ref", "main")),
                work_branch=str(spec.contract.get("repository", {}).get("work_branch", "")),
                commits=commits,
                verified=True,
                sha256=hashlib.sha256(f"fake-bundle:{head}".encode()).hexdigest(),
                commit_paths=paths,
                commit_messages=(f"Fake commit for {spec.external_id}",) if commits else (),
                commit_policy=fake_commit_policy(behavior, head),
            )
            artifacts = [
                CollectedArtifact(
                    name="report/completion-claim.json",
                    type="completion_claim",
                    content=b"",
                    content_type="application/json",
                )
            ]
            artifacts.append(
                CollectedArtifact(
                    name=REVIEW_DIFF_NAME,
                    type=REVIEW_DIFF_TYPE,
                    content=synthetic_review_diff(paths, behavior).encode(),
                    content_type="text/x-diff",
                )
            )
            for verification in spec.contract.get("required_verification", []):
                if verification.get("kind") == "artifact":
                    artifacts.append(
                        CollectedArtifact(
                            name=str(verification["path"]),
                            type="run_evidence",
                            content=(
                                f"# Run evidence for {spec.external_id}\n\n"
                                f"Produced by the fake provider at head {head}.\n"
                            ).encode(),
                            content_type="text/markdown",
                        )
                    )
            # malformed-report: the work is committed and the report file is there, but
            # it is not YAML, an unquoted colon in a value (ADR 0024).
            malformed = behavior == "malformed-report"
            return CollectedOutputs(
                report=None if malformed else report,
                report_raw=MALFORMED_REPORT if malformed else None,
                blocked_md=None,
                diff_paths=paths,
                diff_text=synthetic_diff(paths, behavior),
                bundle=bundle,
                artifacts=tuple(artifacts),
                verifications=verification_runs(spec.contract, behavior),
                workspace_state=workspace_state(behavior, spec.attempt_id),
            )
        if behavior == "blocked" and worker.exit_code == 75:
            return CollectedOutputs(
                report=None,
                report_raw=None,
                blocked_md=f"# Blocked\n\nFake worker for {spec.external_id} needs a decision.\n",
            )
        return CollectedOutputs(report=None, report_raw=None, blocked_md=None)

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None:
        worker = self._workers.get(h.attempt_id)
        if worker is None or worker.state is not ObservationState.RUNNING:
            return
        if mode == "drain":
            worker.drains += 1
            if worker.behavior not in ("hang", "immortal"):
                worker.state = ObservationState.EXITED
                worker.exit_code = 143
                worker.killed = True
            return
        worker.kills += 1
        if worker.behavior == "immortal" and worker.kills < 2:
            return
        worker.state = ObservationState.EXITED
        worker.exit_code = 137
        worker.killed = True

    async def cleanup(
        self, ws: Workspace, policy: CleanupPolicy, spec: LaunchSpec | None = None
    ) -> None:
        self.cleaned.append(ws.attempt_id)
        self.cleanup_policies[ws.attempt_id] = policy
        self._workspaces.pop(ws.attempt_id, None)

    async def release_workspace(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        if self.release_fails:
            raise ProviderError("the fake refuses to release a workspace")
        self.released.append(ws.attempt_id)

    async def list_images(self) -> list[ImageInfo]:
        """The fake provider runs behaviours, not images (08): it lists what a test
        handed it, and nothing by default."""
        return list(self.images)

    async def probe_credential(self, request: ProbeRequest) -> ProbeResult:
        """No image and no credential to run (08): the probe records a synthetic
        success so the administrative services can be exercised without a daemon.
        `probe_outcome` makes it report a timeout or an observed authentication failure
        instead, which is how a test sees the difference between a probe that decided
        nothing and one that decided the credential is bad."""
        self.probes.append(request.harness)
        self.probe_requests.append(request)
        if self.probe_outcome == "auth_failure":
            return ProbeResult(
                exit_code=1,
                image_digest="sha256:" + "f" * 64,
                harness_version="fake",
                duration_seconds=0.4,
                # A phrase all three adapters' auth patterns carry (S5).
                stdout_tail="Not logged in\n",
            )
        if self.probe_outcome == "timeout":
            return ProbeResult(
                exit_code=None,
                image_digest="sha256:" + "f" * 64,
                harness_version="fake",
                duration_seconds=float(request.timeout_seconds),
                timed_out=True,
                detail=f"the probe did not finish within {request.timeout_seconds}s",
            )
        return ProbeResult(
            exit_code=0,
            image_digest="sha256:" + "f" * 64,
            harness_version="fake",
            duration_seconds=0.0,
            stdout_tail="OK\n",
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth("ok", {"daemon": "fake"})

    async def discard(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """Nothing secret was placed; the call is recorded so a test can assert it."""
        self.discarded.append(ws.attempt_id)

    async def retention(self, keep: Sequence[str]) -> int:
        return 0

    async def reconcile(self) -> list[Handle]:
        return [
            Handle(provider=self.name, ref=f"fake-{attempt_id}", attempt_id=attempt_id)
            for attempt_id, worker in self._workers.items()
            if worker.state is ObservationState.RUNNING
        ]

    def remove_out_of_band(self, attempt_id: str) -> None:
        """Simulate an operator removing the worker behind Crucible's back."""
        self._workers.pop(attempt_id, None)
