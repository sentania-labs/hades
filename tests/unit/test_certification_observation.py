"""Certification records observed jobs without consulting protected check names."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from crucible.application.observation import certify_head
from crucible.contracts.evidence import EvidenceKind
from crucible.domain.entities import EvidenceRecord
from crucible.ports.github import CheckRecord, Observation

HEAD = "a" * 40


@pytest.mark.parametrize(
    ("checks", "narrowing", "state", "detail"),
    [
        (("success", "success"), [], "green", "2 of 2 jobs succeeded"),
        (("success", "failure"), [], "failed", "1 of 2 jobs succeeded"),
        (("success", None), [], "pending", "1 of 2 jobs still running"),
        ((), [], "pending", "never green"),
        (("success", "failure"), ["job-0", "absent"], "green", "1 of 1 jobs succeeded"),
    ],
)
def test_observation_counts_jobs_despite_branch_protection(
    checks: tuple[str | None, ...], narrowing: list[str], state: str, detail: str
) -> None:
    uow = MagicMock()
    uow.ci_certifications.get_for_head.return_value = None
    uow.ci_certifications.put.side_effect = lambda certification: certification
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 9, 30, tzinfo=UTC)
    observation = Observation(
        pull_request=MagicMock(),
        required_checks=("never-observed",),
        checks=tuple(
            CheckRecord(
                name=f"job-{index}",
                status="completed" if conclusion else "in_progress",
                conclusion=conclusion,
                head_sha=HEAD,
            )
            for index, conclusion in enumerate(checks)
        ),
    )
    with (
        patch("crucible.application.observation.pending_rerun", return_value=None),
        patch("crucible.application.observation.task_waivers", return_value={}),
    ):
        certification = certify_head(
            uow,
            clock,
            task=MagicMock(),
            pull_request=MagicMock(),
            observation=observation,
            policy={"ci_certification": {"required_checks": narrowing}},
            head_sha=HEAD,
        )
    assert certification.state == state
    assert detail in certification.detail
    assert "never-observed" not in certification.required_checks
    assert uow.events.append.call_args.args[0].payload["required_from"] == "observed"


@pytest.mark.parametrize(
    ("narrowing", "conclusion", "state"),
    [
        ([], None, "skipped"),
        (["build"], None, "pending"),
        ([], "skipped", "skipped"),
        (["build"], "skipped", "pending"),
        ([], "in_progress", "pending"),
        (["build"], "in_progress", "pending"),
    ],
)
def test_no_ci_waiver_requires_no_runs_and_no_policy_narrowing(
    narrowing: list[str], conclusion: str | None, state: str
) -> None:
    uow = MagicMock()
    uow.ci_certifications.get_for_head.return_value = None
    uow.ci_certifications.put.side_effect = lambda certification: certification
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 9, 30, tzinfo=UTC)
    checks = (
        (
            CheckRecord(
                name="other-job",
                status="in_progress" if conclusion == "in_progress" else "completed",
                conclusion=None if conclusion == "in_progress" else conclusion,
                head_sha=HEAD,
            ),
        )
        if conclusion is not None
        else ()
    )
    waiver = MagicMock(verbatim="This repository has no CI.")
    with patch(
        "crucible.application.observation.task_waivers", return_value={"accept_no_ci": waiver}
    ):
        certification = certify_head(
            uow,
            clock,
            task=MagicMock(),
            pull_request=MagicMock(),
            observation=Observation(pull_request=MagicMock(), checks=checks),
            policy={"ci_certification": {"required_checks": narrowing}},
            head_sha=HEAD,
        )
    assert certification.state == state


def _diff_paths_evidence(paths: list[str]) -> EvidenceRecord:
    return EvidenceRecord(
        id=1,
        attempt_id="attempt-1",
        task_id="task-1",
        kind=EvidenceKind.DIFF_PATHS.value,
        observed_at=datetime(2026, 10, 6, tzinfo=UTC),
        source="crucible",
        verified=True,
        payload={"paths": paths},
    )


def test_certify_head_records_the_change_class_from_the_attempts_diff() -> None:
    """hades #476, finding 01M4CG0K1Z72QKBVMXJRHWK5KK: the live caller must obtain
    the change class and carry it onto the stored certification, not just the direct
    unit calls onto `certify()`."""
    uow = MagicMock()
    uow.ci_certifications.get_for_head.return_value = None
    uow.ci_certifications.put.side_effect = lambda certification: certification
    uow.evidence.list_for_attempt.return_value = [
        _diff_paths_evidence(["images/worker/Dockerfile"])
    ]
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 10, 6, tzinfo=UTC)
    observation = Observation(
        pull_request=MagicMock(),
        checks=(CheckRecord(name="lint", status="completed", conclusion="success", head_sha=HEAD),),
    )
    with (
        patch("crucible.application.observation.pending_rerun", return_value=None),
        patch("crucible.application.observation.task_waivers", return_value={}),
    ):
        certification = certify_head(
            uow,
            clock,
            task=MagicMock(),
            pull_request=MagicMock(),
            observation=observation,
            policy={},
            head_sha=HEAD,
            attempt_id="attempt-1",
        )
    assert certification.change_class == "images"
    uow.evidence.list_for_attempt.assert_called_once_with("attempt-1")


def test_certify_head_leaves_change_class_empty_without_an_attempt_id() -> None:
    """A caller that does not know the attempt (or an attempt that collected no diff)
    gets the pre-#476 default, never a guess."""
    uow = MagicMock()
    uow.ci_certifications.get_for_head.return_value = None
    uow.ci_certifications.put.side_effect = lambda certification: certification
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 10, 6, tzinfo=UTC)
    observation = Observation(
        pull_request=MagicMock(),
        checks=(CheckRecord(name="lint", status="completed", conclusion="success", head_sha=HEAD),),
    )
    with (
        patch("crucible.application.observation.pending_rerun", return_value=None),
        patch("crucible.application.observation.task_waivers", return_value={}),
    ):
        certification = certify_head(
            uow,
            clock,
            task=MagicMock(),
            pull_request=MagicMock(),
            observation=observation,
            policy={},
            head_sha=HEAD,
        )
    assert certification.change_class == ""
    uow.evidence.list_for_attempt.assert_not_called()
