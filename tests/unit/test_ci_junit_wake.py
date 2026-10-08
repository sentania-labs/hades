"""hades #558, #85: a CI failure wake for a test job names the failing tests from the
junit artifact, and says so when no artifact exists instead of guessing from the log.

The parsing is the domain's (tests/unit/test_issue_196_flake_scan.py keeps the tool's
copy honest); the reading goes through the GitHub client's optional artifact
capability; the finding lands on the certification's failure and on the wake's
summary and payload. The fakes here stand in for GitHub and the database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from crucible.application.ci_junit import (
    JUNIT_ABSENT,
    JUNIT_NO_ARTIFACT_FOR_JOB,
    JUNIT_PARSED,
    JUNIT_UNREADABLE,
    JUNIT_UNSUPPORTED,
    JUnitFindings,
    JUnitReport,
    WorkflowArtifactReader,
    junit_sentence,
    read_junit_findings,
)
from crucible.application.observation import ObservationResult, advance_delivery, certify_head
from crucible.contracts.wake import WakeReason
from crucible.domain.certification import CertificationState
from crucible.domain.entities import CICertification
from crucible.domain.junit import JUnitParseError, failing_test_ids, junit_artifact_name, node_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.github import CheckRecord, GitHubError, InstallationToken, Observation

HEAD = "a" * 40
REPORT = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="1" errors="1">
    <testcase classname="tests.integration.test_api" name="test_submit" time="0.1"/>
    <testcase classname="tests.integration.test_api" name="test_start" time="0.1">
      <failure message="assert 1 == 2">traceback</failure>
    </testcase>
    <testcase classname="tests.integration.test_db.TestMigrations" name="test_upgrade">
      <error message="boom">traceback</error>
    </testcase>
    <testcase classname="tests.integration.test_api" name="test_start">
      <failure message="again">rerun</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


# ----- the domain parser ------------------------------------------------------------


def test_failing_and_erroring_cases_become_pytest_node_ids_once_each() -> None:
    assert failing_test_ids(REPORT) == [
        "tests/integration/test_api.py::test_start",
        "tests/integration/test_db.py::TestMigrations::test_upgrade",
    ]


def test_a_report_with_no_failure_names_nothing() -> None:
    assert failing_test_ids(b'<testsuite><testcase classname="a.b" name="c"/></testsuite>') == []


def test_bytes_that_are_not_xml_are_a_parse_error_not_no_failures() -> None:
    with pytest.raises(JUnitParseError):
        failing_test_ids(b"not xml at all")


def test_node_id_follows_the_repositorys_module_and_class_convention() -> None:
    assert node_id("tests.unit.test_foo", "test_bar") == "tests/unit/test_foo.py::test_bar"
    assert (
        node_id("tests.unit.test_x.TestA.TestB", "test_c")
        == "tests/unit/test_x.py::TestA::TestB::test_c"
    )
    assert node_id("", "test_alone") == "test_alone"


@pytest.mark.parametrize(
    ("job", "attempt", "expected"),
    [
        ("test", 1, "junit-test-1"),
        ("e2e", 2, "junit-e2e-2"),
        ("e2e-kind (3)", 1, "junit-e2e-kind-3-1"),
        ("lint", 1, None),
        ("images", 1, None),
        ("e2e-kind", 1, None),
    ],
)
def test_the_artifact_name_is_ci_ymls_per_job(job: str, attempt: int, expected: str | None) -> None:
    assert junit_artifact_name(job, attempt) == expected


# ----- the reader -------------------------------------------------------------------


def _token() -> InstallationToken:
    return InstallationToken(
        "ghs_" + "x" * 30,
        expires_at=datetime(2026, 10, 8, tzinfo=UTC),
        repository="acme/example",
    )


class PlainGitHub:
    """A client with the port's issue 435 lookups and no artifact capability."""

    def __init__(self, *, run_id: int | None = 77, run_attempt: int = 1) -> None:
        self.run_id = run_id
        self.run_attempt = run_attempt
        self.asked: list[dict[str, Any]] = []

    def workflow_run_for_job(self, token: Any, *, repository: str, job_id: int) -> int | None:
        self.asked.append({"job_id": job_id})
        return self.run_id

    def get_workflow_run(self, token: Any, *, repository: str, run_id: int) -> dict[str, Any]:
        return {"id": run_id, "run_attempt": self.run_attempt}


class ArtifactGitHub(PlainGitHub):
    """The same client with `junit_reports`: what the adapter half of hades #558 adds."""

    def __init__(
        self,
        reports: tuple[JUnitReport, ...] | None,
        *,
        run_id: int | None = 77,
        run_attempt: int = 1,
        error: Exception | None = None,
    ) -> None:
        super().__init__(run_id=run_id, run_attempt=run_attempt)
        self.reports = reports
        self.error = error
        self.reads: list[dict[str, Any]] = []

    def junit_reports(
        self,
        token: Any,
        *,
        repository: str,
        run_id: int,
        artifact: str,
        limit_bytes: int,
    ) -> tuple[JUnitReport, ...] | None:
        self.reads.append({"repository": repository, "run_id": run_id, "artifact": artifact})
        if self.error is not None:
            raise self.error
        return self.reports


def _read(
    github: Any, *, source: str = "check_run", external_id: str = "123", job: str = "test"
) -> JUnitFindings:
    return read_junit_findings(
        github,
        _token(),
        repository="acme/example",
        source=source,
        external_id=external_id,
        job=job,
        limit_bytes=1024,
    )


def test_the_capability_is_a_runtime_checkable_protocol() -> None:
    assert isinstance(ArtifactGitHub(()), WorkflowArtifactReader)
    assert not isinstance(PlainGitHub(), WorkflowArtifactReader)


def test_a_client_without_the_capability_records_unsupported_and_asks_nothing() -> None:
    github = PlainGitHub()
    findings = _read(github)
    assert findings.status == JUNIT_UNSUPPORTED
    assert findings.job == "test"
    assert github.asked == []
    assert "cannot fetch workflow artifacts" in junit_sentence(findings)


def test_a_test_jobs_artifact_is_read_for_its_run_and_attempt() -> None:
    github = ArtifactGitHub(
        (JUnitReport("junit-test-2", "junit/integration.xml", REPORT),), run_attempt=2
    )
    findings = _read(github, job="test")
    assert github.asked == [{"job_id": 123}]
    assert github.reads == [
        {"repository": "acme/example", "run_id": 77, "artifact": "junit-test-2"}
    ]
    assert findings == JUnitFindings(
        JUNIT_PARSED,
        job="test",
        artifact="junit-test-2",
        failing_tests=(
            "tests/integration/test_api.py::test_start",
            "tests/integration/test_db.py::TestMigrations::test_upgrade",
        ),
        reports=1,
    )
    assert junit_sentence(findings) == (
        "failing tests (junit-test-2): tests/integration/test_api.py::test_start, "
        "tests/integration/test_db.py::TestMigrations::test_upgrade"
    )


def test_a_workflow_run_source_is_the_run_itself() -> None:
    github = ArtifactGitHub((JUnitReport("junit-e2e-1", "e2e.xml", REPORT),))
    findings = _read(github, source="workflow_run", external_id="5150", job="e2e")
    assert github.asked == []
    assert github.reads[0]["run_id"] == 5150
    assert findings.artifact == "junit-e2e-1"


def test_no_artifact_on_the_run_is_said_not_guessed() -> None:
    findings = _read(ArtifactGitHub(None), job="e2e-kind (2)")
    assert findings.status == JUNIT_ABSENT
    assert findings.artifact == "junit-e2e-kind-2-1"
    sentence = junit_sentence(findings)
    assert "uploaded no junit artifact" in sentence
    assert "junit-e2e-kind-2-1" in sentence
    assert "not known" in sentence


def test_a_job_that_uploads_no_junit_is_said_so_without_a_lookup() -> None:
    github = ArtifactGitHub(())
    findings = _read(github, job="lint")
    assert findings.status == JUNIT_NO_ARTIFACT_FOR_JOB
    assert github.asked == [] and github.reads == []
    assert junit_sentence(findings) == (
        "the lint job uploads no junit artifact, so there are no failing test ids to name"
    )


def test_a_check_that_is_not_an_actions_run_has_no_artifact() -> None:
    findings = _read(ArtifactGitHub((), run_id=None))
    assert findings.status == JUNIT_ABSENT
    assert findings.detail == "the failed check is not an Actions run"


def test_an_artifact_that_will_not_parse_is_unreadable_not_green() -> None:
    github = ArtifactGitHub((JUnitReport("junit-test-1", "junit/unit.xml", b"<broken"),))
    findings = _read(github)
    assert findings.status == JUNIT_UNREADABLE
    assert findings.detail.startswith("junit/unit.xml: ")
    assert findings.failing_tests == ()
    assert "could not be read" in junit_sentence(findings)


def test_an_empty_artifact_is_unreadable() -> None:
    findings = _read(ArtifactGitHub(()))
    assert findings.status == JUNIT_UNREADABLE
    assert findings.detail == "the artifact held no XML"


def test_a_github_refusal_is_recorded_and_a_rate_limit_propagates() -> None:
    refused = _read(ArtifactGitHub((), error=GitHubError(403, "nope", response_class="forbidden")))
    assert refused.status == JUNIT_UNREADABLE
    assert refused.detail == "GitHub answered 403"
    limited = GitHubError(403, "slow down", response_class="rate_limited")
    with pytest.raises(GitHubError):
        _read(ArtifactGitHub((), error=limited))


def test_a_parsed_report_with_no_failing_test_points_at_the_log() -> None:
    clean = b'<testsuite><testcase classname="tests.unit.test_a" name="test_b"/></testsuite>'
    findings = _read(ArtifactGitHub((JUnitReport("junit-test-1", "unit.xml", clean),)))
    assert findings.status == JUNIT_PARSED and findings.failing_tests == ()
    assert "records no failing test" in junit_sentence(findings)


def test_the_summary_names_at_most_eight_tests_and_counts_the_rest() -> None:
    findings = JUnitFindings(
        JUNIT_PARSED,
        job="test",
        artifact="junit-test-1",
        failing_tests=tuple(f"tests/unit/test_{i}.py::test_{i}" for i in range(11)),
    )
    sentence = junit_sentence(findings)
    assert sentence.endswith("tests/unit/test_7.py::test_7 and 3 more")
    assert "test_8" not in sentence


def test_findings_round_trip_through_the_stored_dict() -> None:
    findings = JUnitFindings(
        JUNIT_PARSED, job="test", artifact="a", failing_tests=("x::y",), reports=2
    )
    assert JUnitFindings.from_dict(findings.as_dict()) == findings
    assert JUnitFindings.from_dict(None) is None
    assert JUnitFindings.from_dict({}) is None
    assert junit_sentence(None) == ""


# ----- the certification record and the wake ----------------------------------------


def _uow(previous: CICertification | None = None) -> MagicMock:
    uow = MagicMock()
    uow.ci_certifications.get_for_head.return_value = previous
    uow.ci_certifications.put.side_effect = lambda certification: certification
    return uow


def _clock() -> MagicMock:
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    return clock


def _failed_observation() -> Observation:
    return Observation(
        pull_request=MagicMock(),
        checks=(
            CheckRecord(
                name="test",
                status="completed",
                conclusion="failure",
                head_sha=HEAD,
                external_id="123",
                job="test",
                workflow="CI",
                url="https://github.com/acme/example/actions/runs/77/job/123",
            ),
        ),
    )


def _certify(uow: MagicMock, junit: JUnitFindings | None, *, log_fetched: bool) -> CICertification:
    with (
        patch("crucible.application.observation.pending_rerun", return_value=None),
        patch("crucible.application.observation.task_waivers", return_value={}),
    ):
        return certify_head(
            uow,
            _clock(),
            task=MagicMock(),
            pull_request=MagicMock(),
            observation=_failed_observation(),
            policy={"ci_certification": {"required_checks": []}},
            head_sha=HEAD,
            log_excerpt="FAILED tests/integration/test_api.py::test_start" if log_fetched else "",
            log_fetched=log_fetched,
            junit=junit,
        )


def test_the_certification_failure_records_the_junit_finding_once_per_run() -> None:
    findings = JUnitFindings(
        JUNIT_PARSED,
        job="test",
        artifact="junit-test-1",
        failing_tests=("tests/integration/test_api.py::test_start",),
        reports=1,
    )
    first = _certify(_uow(), findings, log_fetched=True)
    assert first.state == CertificationState.FAILED.value
    assert first.failure["junit"] == findings.as_dict()
    # The next poll of the same failed run fetches nothing and keeps the record.
    second = _certify(_uow(previous=first), None, log_fetched=False)
    assert second.failure["junit"] == findings.as_dict()
    assert second.failure["log_excerpt"] == first.failure["log_excerpt"]


def test_the_event_payload_carries_the_finding_but_never_the_log() -> None:
    uow = _uow()
    findings = JUnitFindings(JUNIT_ABSENT, job="test", artifact="junit-test-1")
    _certify(uow, findings, log_fetched=True)
    payload = uow.events.append.call_args.args[0].payload
    assert payload["failure"]["junit"]["status"] == JUNIT_ABSENT
    assert "log_excerpt" not in payload["failure"]


def _task() -> MagicMock:
    task = MagicMock()
    task.id = "task-1"
    task.principal_id = "principal-1"
    task.state = TaskState.AWAITING_CI_CERTIFICATION
    task.head_sha = HEAD
    return task


def _wake_for(junit: dict[str, Any] | None) -> Any:
    uow = MagicMock()
    uow.ci_decisions.list_for_task.return_value = []
    certification = CICertification(
        id="cert-1",
        pull_request_id="pr-1",
        task_id="task-1",
        head_sha=HEAD,
        state=CertificationState.FAILED.value,
        required_checks=["test"],
        check_runs=[],
        failure={
            "check": "test",
            "workflow": "CI",
            "job": "test",
            "conclusion": "failure",
            "url": "https://github.com/acme/example/actions/runs/77/job/123",
            "run_id": "123",
            "source": "check_run",
            "log_excerpt": "FAILED something in the log",
            "log_fetched": True,
            **({"junit": junit} if junit is not None else {}),
        },
        detail="1 of 2 jobs succeeded",
        evaluated_at=datetime(2026, 10, 8, 9, 0, tzinfo=UTC),
    )
    pull_request = MagicMock()
    pull_request.number = 9
    with (
        patch("crucible.application.observation.pending_rerun", return_value=None),
        patch("crucible.application.observation.move_task"),
    ):
        advance_delivery(
            uow,
            _clock(),
            task=_task(),
            pull_request=pull_request,
            policy={"gates": {"skipped": []}},
            gates={"results": {}},
            certification=certification,
            result=ObservationResult(),
        )
    assert uow.wakes.add.call_count == 1
    return uow.wakes.add.call_args.args[0]


def test_the_wake_names_the_failing_tests_in_its_summary_and_payload() -> None:
    wake = _wake_for(
        JUnitFindings(
            JUNIT_PARSED,
            job="test",
            artifact="junit-test-1",
            failing_tests=(
                "tests/integration/test_api.py::test_start",
                "tests/integration/test_db.py::TestMigrations::test_upgrade",
            ),
            reports=2,
        ).as_dict()
    )
    assert wake.reason == WakeReason.CI_CERTIFICATION_FAILED.value
    assert wake.payload["summary"] == (
        f"required CI failed on {HEAD}: 1 of 2 jobs succeeded; failing tests (junit-test-1): "
        "tests/integration/test_api.py::test_start, "
        "tests/integration/test_db.py::TestMigrations::test_upgrade"
    )
    assert wake.payload["junit"]["status"] == JUNIT_PARSED
    assert wake.payload["junit"]["failing_tests"] == [
        "tests/integration/test_api.py::test_start",
        "tests/integration/test_db.py::TestMigrations::test_upgrade",
    ]
    assert wake.payload["failed_check"] == {
        "check": "test",
        "workflow": "CI",
        "job": "test",
        "conclusion": "failure",
        "url": "https://github.com/acme/example/actions/runs/77/job/123",
        "run_id": "123",
    }
    # The log stays on the certification; the wake carries names, never the log.
    assert "log_excerpt" not in str(wake.payload)
    assert wake.payload["links"]["ci_decision"] == "/v1/tasks/task-1/ci-decision"


def test_the_wake_says_no_junit_artifact_exists_instead_of_guessing() -> None:
    wake = _wake_for(JUnitFindings(JUNIT_ABSENT, job="test", artifact="junit-test-1").as_dict())
    summary = wake.payload["summary"]
    assert summary.startswith(f"required CI failed on {HEAD}: 1 of 2 jobs succeeded; ")
    assert "the test job uploaded no junit artifact (junit-test-1 is not on the run)" in summary
    assert "failing tests (" not in summary
    assert wake.payload["junit"]["status"] == JUNIT_ABSENT
    assert wake.payload["junit"]["failing_tests"] == []


def test_a_wake_without_a_finding_reads_as_before() -> None:
    wake = _wake_for(None)
    assert wake.payload["summary"] == f"required CI failed on {HEAD}: 1 of 2 jobs succeeded"
    assert "junit" not in wake.payload
    assert "failed_check" not in wake.payload
