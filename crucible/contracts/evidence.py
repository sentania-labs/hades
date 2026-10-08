"""EvidenceV1 (11): a typed, verified observation with a pointer to an artifact or an
observed fact. Gates consume only `verified: true` evidence that no worker asserted."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from crucible.contracts.common import SCHEMA_VERSION, Rfc3339, StrictModel, check_major_version


class EvidenceKind(StrEnum):
    GATE_PROBE = "gate_probe"
    EXIT_INFO = "exit_info"
    DIFF_PATHS = "diff_paths"
    BUNDLE_HEAD = "bundle_head"
    REMOTE_HEAD = "remote_head"
    PR_STATE = "pr_state"
    REVIEW_RECEIVED = "review_received"
    CHECK_RUN = "check_run"
    SCANNER_RESULT = "scanner_result"
    # Crucible's own re-run of a required_verification command in a verifier container,
    # and the provider's answer to "is anything of this attempt still running" (11, C3).
    VERIFICATION_RUN = "verification_run"
    WORKSPACE_STATE = "workspace_state"
    ARTIFACT_PRESENT = "artifact_present"
    TRANSCRIPT_MATCH = "transcript_match"
    FALSE_CLAIM = "false_claim"


class EvidenceSource(StrEnum):
    CRUCIBLE = "crucible"
    GITHUB = "github"
    WORKER = "worker"


# Roles that narrow `artifact_present`, so the fixed kind list of 11 stays fixed.
ROLE_COMPLETION_CLAIM = "completion_claim"
ROLE_WORKER_CLAIM = "worker_claim"
# hades #498: the completion record Hades composed from its own evidence.
ROLE_COMPLETION_RECORD = "completion_record"
ROLE_RUN_EVIDENCE = "run_evidence"
ROLE_REVIEW_REPORT = "review_report"
# hades #344: the collector's `git diff <base>...HEAD` for the internal review. Its own
# role, so a gate that wants the worker's run evidence never counts it, and its own
# name, outside the worker's `report/` namespace.
ROLE_REVIEW_DIFF = "review_diff"
REVIEW_DIFF_NAME = "crucible/diff.patch"
REVIEW_DIFF_TYPE = "diff"
# hades #370: the preparer's stdout and stderr, kept when the attempt died before its
# worker ran, so a failed clone or resume says why without a cluster to look at.
ROLE_PREPARER_LOG = "preparer_log"
PREPARER_LOG_NAME = "crucible/preparer.log"
PREPARER_LOG_TYPE = "preparer_log"


class EvidenceV1(StrictModel):
    schema_version: str = SCHEMA_VERSION
    id: int
    attempt_id: str | None = None
    pull_request_id: str | None = None
    kind: EvidenceKind
    observed_at: Rfc3339
    source: EvidenceSource
    verified: bool
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return check_major_version(value)

    @property
    def admissible(self) -> bool:
        """What 11 permits a gate to consume."""
        return self.verified and self.source is not EvidenceSource.WORKER


def verified_for(source: EvidenceSource) -> bool:
    """Crucible's own observations are verified; a worker assertion never is (11)."""
    return source is not EvidenceSource.WORKER


def observed(value: datetime) -> datetime:
    return value
