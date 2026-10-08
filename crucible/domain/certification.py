"""CI certification as a pure decision over observed runs (23, ADR 0009).

The rule that matters most is the one an "is everything green" check gets wrong: an
empty required-check set is **pending**, never green. Before GitHub has created any run,
and on a repository with no CI at all, the task waits and the timeout wakes Foundry. A
repository that intentionally has no CI needs `allow_no_ci`, which makes the gate
`skipped` rather than passed, and that is an operator-recorded policy decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

SUCCESS = "success"
NEUTRAL = "neutral"
SKIPPED = "skipped"
FAILING_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}
)
# Only success certifies; neutral stays pending and skipped runs are excluded.
PASSING_CONCLUSIONS: frozenset[str] = frozenset({SUCCESS})


class CertificationState(StrEnum):
    PENDING = "pending"
    GREEN = "green"
    FAILED = "failed"
    SKIPPED = "skipped"


class CheckSource(StrEnum):
    CHECK_RUN = "check_run"
    WORKFLOW_RUN = "workflow_run"
    # A suite is a container for check runs, not a check. It is observed and recorded
    # (23 asks for both), but it never becomes a member of the required set: an App that
    # creates a suite on every head and never runs anything in it would otherwise hold
    # every task in `pending` for ever.
    CHECK_SUITE = "check_suite"


@dataclass(frozen=True, slots=True)
class ObservedCheck:
    """One check run or workflow run on a head, as GitHub reported it."""

    name: str
    status: str
    conclusion: str | None
    head_sha: str
    source: CheckSource = CheckSource.CHECK_RUN
    url: str = ""
    external_id: str = ""
    workflow: str = ""
    job: str = ""
    completed_at: datetime | None = None

    @property
    def concluded(self) -> bool:
        return self.status == "completed" and self.conclusion is not None

    @property
    def succeeded(self) -> bool:
        return self.concluded and (self.conclusion or "") in PASSING_CONCLUSIONS

    @property
    def failed(self) -> bool:
        return self.concluded and (self.conclusion or "") in FAILING_CONCLUSIONS


@dataclass(frozen=True, slots=True)
class Certification:
    state: CertificationState
    detail: str
    required: tuple[str, ...] = ()
    source: str = ""
    failures: tuple[ObservedCheck, ...] = ()
    pending: tuple[str, ...] = ()
    observed: tuple[ObservedCheck, ...] = field(default=())
    # hades #476: the class `crucible.domain.change_class.classify` assigned to this
    # run's changed paths, recorded here for the record, not read by `certify` itself.
    # A job the classifier's `if:` skipped is excluded from `counted` by `_counts`
    # below, the same as any other skipped run, so it is never a missing job; this
    # field only names which classification explains that skip. Empty when the caller
    # does not know it (every caller before #476, and a certification with nothing
    # observed yet).
    change_class: str = ""


def required_checks_from_policy(policy: dict[str, object]) -> tuple[str, ...]:
    section = policy.get("ci_certification")
    raw = section.get("required_checks") if isinstance(section, dict) else None
    if not raw or not isinstance(raw, list):
        return ()
    return tuple(str(name) for name in raw if str(name))


def allow_no_ci(policy: dict[str, object]) -> bool:
    section = policy.get("ci_certification")
    return bool(section.get("allow_no_ci", False)) if isinstance(section, dict) else False


def wait_timeout_hours(policy: dict[str, object], section_name: str, default: int) -> int:
    section = policy.get(section_name)
    if not isinstance(section, dict):
        return default
    return int(section.get("wait_timeout_hours", default))


def resolve_required(
    *,
    observed: Sequence[ObservedCheck],
) -> tuple[tuple[str, ...], str]:
    """Name every observed non-skipped run; suites are only containers."""
    return tuple(dict.fromkeys(check.name for check in observed if _counts(check))), "observed"


def _counts(check: ObservedCheck) -> bool:
    return check.source is not CheckSource.CHECK_SUITE and not (
        check.concluded and check.conclusion == SKIPPED
    )


def certify(
    policy: dict[str, object],
    *,
    head_sha: str,
    observed: Sequence[ObservedCheck],
    change_class: str = "",
) -> Certification:
    """Green, failed, pending, or skipped for one head. Never green on an empty set.

    `change_class` (hades #476) is recorded on the returned `Certification` as-is; it
    never changes which runs count or which state results, so the caller may pass the
    empty default when it has not computed one."""
    on_head = tuple(check for check in observed if check.head_sha == head_sha)
    narrowing = required_checks_from_policy(policy)
    counted = tuple(
        check for check in on_head if _counts(check) and (not narrowing or check.name in narrowing)
    )
    required, source = resolve_required(observed=counted)
    if not required:
        if allow_no_ci(policy):
            return Certification(
                CertificationState.SKIPPED,
                "the policy records that this repository intentionally has no CI "
                "(ci_certification.allow_no_ci)",
                source=source,
                observed=on_head,
                change_class=change_class,
            )
        return Certification(
            CertificationState.PENDING,
            f"no eligible check run or workflow job has been observed on {head_sha}; an empty "
            "required-check set is pending, never green (23)",
            source=source,
            observed=on_head,
            change_class=change_class,
        )
    failures = [check for check in counted if check.failed]
    pending = [check.name for check in counted if not check.failed and not check.succeeded]
    total = len(counted)
    succeeded = sum(check.succeeded for check in counted)
    progress = f"{succeeded} of {total} jobs succeeded on {head_sha}"
    if failures:
        names = ", ".join(sorted({check.name for check in failures}))
        return Certification(
            CertificationState.FAILED,
            f"{progress}; {len(failures)} of {total} jobs failed: {names}",
            required=required,
            source=source,
            failures=tuple(failures),
            pending=tuple(pending),
            observed=on_head,
            change_class=change_class,
        )
    if pending:
        return Certification(
            CertificationState.PENDING,
            f"{progress}; {len(pending)} of {total} jobs still running or awaiting success: "
            f"{', '.join(sorted(pending))}",
            required=required,
            source=source,
            pending=tuple(pending),
            observed=on_head,
            change_class=change_class,
        )
    return Certification(
        CertificationState.GREEN,
        progress,
        required=required,
        source=source,
        observed=on_head,
        change_class=change_class,
    )
