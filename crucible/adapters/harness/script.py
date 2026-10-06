"""The e2e script harness (18) as the fourth adapter.

A script implementing the launch contract: it reads the identity bundle, does what the
repository's `e2e-behavior` file asks, writes a CompletionClaimV1, and exits with the
code the case wants. No model, no credential, no subscription, so the whole provider
path can be proven in CI. It lived inside the execution adapter in C3; it is behind the
port now so the registry has one shape for every harness (07).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.domain.exit_class import ExitClass
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY
from crucible.domain.infrastructure import Interruption
from crucible.ports.harness import (
    AdapterLaunch,
    CommandTracker,
    CredentialSpec,
    ExitInfo,
    HarnessCapabilities,
    LaunchContext,
    ParsedReport,
    ProviderQuotaEvent,
    ReportMetrics,
    TranscriptFormat,
    VersionRange,
)

NAME = "script-harness"
QUOTA_PATTERNS = base.patterns("scripted_quota_exhausted")


def _provider_quota_refusal(document: Mapping[str, Any]) -> bool:
    return document.get("error") == "scripted_quota_exhausted"


def _probe_launch(ctx: LaunchContext) -> AdapterLaunch:
    """A harness test of the fixture (crucible#118). Routed to a local endpoint, it makes
    the one minimal model call a real harness would, from the worker Pod and through its
    egress, and fails when the endpoint does not answer; otherwise there is nothing to
    call and it exits 0."""
    if ctx.endpoint != "local" or not ctx.endpoint_url:
        return AdapterLaunch(argv=("sh", "-c", "exit 0"))
    body = json.dumps(
        {
            "model": ctx.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 1,
        }
    )
    script = (
        'curl -sS -f --max-time 60 -H "content-type: application/json" '
        '--data "$CRUCIBLE_TEST_BODY" "$CRUCIBLE_TEST_ENDPOINT/chat/completions" >/dev/null'
    )
    return AdapterLaunch(
        argv=("sh", "-c", script),
        env={
            "CRUCIBLE_TEST_ENDPOINT": ctx.endpoint_url.rstrip("/"),
            "CRUCIBLE_TEST_BODY": body,
        },
    )


class ScriptHarnessAdapter:
    name = NAME
    concurrency = HARNESS_CONCURRENCY[NAME]
    parallel_attempts_safe = concurrency.parallel_attempts_safe
    supported_versions = VersionRange("1.0.0", "2.0.0")
    # The e2e tiers' harness, never a prerequisite for real work: the Status page's
    # readiness list leaves it out (crucible#123).
    test_fixture = True

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return base.quota_reset_at(stdout_tail, stderr_tail, quota=QUOTA_PATTERNS)

    def provider_quota_event(
        self, stdout_tail: str, stderr_tail: str, now: datetime | None = None
    ) -> ProviderQuotaEvent | None:
        return base.provider_quota_event(
            stdout_tail, stderr_tail, predicate=_provider_quota_refusal, now=now
        )

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return self.provider_quota_event(stdout_tail, stderr_tail) is not None

    def command_tracker(self) -> CommandTracker | None:
        return None

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=False,
            effort_flag=False,
            transcript_format=TranscriptFormat.NONE,
            endpoints=(),
            shim=None,
        )

    def credential_spec(self) -> CredentialSpec | None:
        return None

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        if ctx.probe:
            return _probe_launch(ctx)
        if ctx.model == "a-scripted-quota":
            script = (
                "mkdir -p src; printf 'quota checkpoint\\n' > src/quota-checkpoint.txt; "
                "printf 'succeed\\n' > e2e-behavior; "
                'printf \'{"error":"scripted_quota_exhausted"}\\n\' >&2; exit 1'
            )
            return AdapterLaunch(argv=("sh", "-c", script), workdir=ctx.repo_mount)
        if ctx.model == "b-script-success":
            script = (
                "test -f src/quota-checkpoint.txt || { "
                "printf 'rerouted checkout is missing quota checkpoint\\n' >&2; exit 1; }; "
                "exec crucible-script-harness"
            )
            return AdapterLaunch(argv=("sh", "-c", script), workdir=ctx.repo_mount)
        return AdapterLaunch(argv=("crucible-script-harness",), workdir=ctx.repo_mount)

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        return base.parse_report_dir(report_dir, exit, metrics=ReportMetrics(), transcript_lines=0)

    def classify_exit(
        self, exit: ExitInfo, stdout_tail: str, stderr_tail: str, report_dir: Path | None = None
    ) -> ExitClass:
        return base.classify_with_patterns(
            exit,
            stdout_tail,
            stderr_tail,
            auth=(),
            quota=QUOTA_PATTERNS,
        )

    def interruption(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> Interruption | None:
        # The scripted harness calls no model, so no model call can fail under it.
        return None
