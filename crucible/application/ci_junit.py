"""The failing tests of a failed CI job, from its JUnit artifact (hades #558, #85).

A `ci_certification_failed` wake carries the job log's tail (23). For a test job it
also names the failing tests, read from the JUnit XML the workflow uploaded, so a
correction starts from the test ids and not from a log. When the run uploaded no such
artifact the wake says so; it never guesses from the log.

The artifact is read through an optional capability of the GitHub client,
`WorkflowArtifactReader`. The production REST client predates that optional port, so
this module also uses its existing REST transport to list and download Actions
artifacts. That keeps the capability available in production without widening the
long-lived GitHub port for this one derived CI observation.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from crucible.domain.junit import JUnitParseError, failing_test_ids, junit_artifact_name
from crucible.ports.github import GitHubClient, GitHubError, InstallationToken

# What `JUnitFindings.status` is.
JUNIT_PARSED = "parsed"
# The run uploaded no JUnit artifact under the name ci.yml gives this job's.
JUNIT_ABSENT = "absent"
# The job is not one that uploads JUnit XML (lint, the image builds, ...).
JUNIT_NO_ARTIFACT_FOR_JOB = "no_artifact_for_job"
# The artifact was there and could not be read or parsed.
JUNIT_UNREADABLE = "unreadable"
# The GitHub client cannot read workflow artifacts.
JUNIT_UNSUPPORTED = "unsupported"
# How many node ids the wake's summary names before "and N more".
SUMMARY_TEST_LIMIT = 8


class _UnsupportedRest:
    """The supplied client is not the production REST client."""


_UNSUPPORTED_REST = _UnsupportedRest()


@dataclass(frozen=True, slots=True)
class JUnitReport:
    """One JUnit XML file out of one workflow artifact."""

    artifact: str
    path: str
    content: bytes


@runtime_checkable
class WorkflowArtifactReader(Protocol):
    """The optional capability a GitHub client offers for reading workflow artifacts."""

    def junit_reports(
        self,
        token: InstallationToken,
        *,
        repository: str,
        run_id: int,
        artifact: str,
        limit_bytes: int,
    ) -> tuple[JUnitReport, ...] | None:
        """The XML files inside the run's artifact named `artifact` (Actions read), or
        None when the run has no such artifact. Raises `GitHubError` as the other calls
        do; anything it cannot read otherwise is an empty tuple."""
        ...


@dataclass(frozen=True, slots=True)
class JUnitFindings:
    """What was learned about a failed job's tests, recorded on the certification's
    failure under `junit` and carried on the wake."""

    status: str
    job: str = ""
    artifact: str = ""
    failing_tests: tuple[str, ...] = ()
    reports: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "job": self.job,
            "artifact": self.artifact,
            "failing_tests": list(self.failing_tests),
            "reports": self.reports,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> JUnitFindings | None:
        if not isinstance(value, Mapping) or not value.get("status"):
            return None
        return cls(
            status=str(value.get("status")),
            job=str(value.get("job") or ""),
            artifact=str(value.get("artifact") or ""),
            failing_tests=tuple(str(t) for t in (value.get("failing_tests") or [])),
            reports=int(value.get("reports") or 0),
            detail=str(value.get("detail") or ""),
        )


def junit_sentence(findings: JUnitFindings | Mapping[str, Any] | None) -> str:
    """The clause a `ci_certification_failed` wake's summary ends with: the failing
    tests, or why none are named. Empty when nothing was recorded."""
    found = findings if isinstance(findings, JUnitFindings) else JUnitFindings.from_dict(findings)
    if found is None:
        return ""
    job = f"the {found.job} job" if found.job else "the failed job"
    if found.status == JUNIT_PARSED:
        if not found.failing_tests:
            return (
                f"{job}'s junit artifact {found.artifact} records no failing test; "
                "the failure is outside the tests (read the log excerpt)"
            )
        shown = ", ".join(found.failing_tests[:SUMMARY_TEST_LIMIT])
        more = len(found.failing_tests) - SUMMARY_TEST_LIMIT
        suffix = f" and {more} more" if more > 0 else ""
        return f"failing tests ({found.artifact}): {shown}{suffix}"
    if found.status == JUNIT_ABSENT:
        return (
            f"{job} uploaded no junit artifact ({found.artifact} is not on the run), so "
            "the failing tests are not known from a report"
        )
    if found.status == JUNIT_NO_ARTIFACT_FOR_JOB:
        return f"{job} uploads no junit artifact, so there are no failing test ids to name"
    if found.status == JUNIT_UNREADABLE:
        return (
            f"{job}'s junit artifact {found.artifact} could not be read"
            + (f" ({found.detail})" if found.detail else "")
            + ", so the failing tests are not known from a report"
        )
    if found.status == JUNIT_UNSUPPORTED:
        return (
            "the junit artifact was not read: this GitHub client cannot fetch workflow "
            "artifacts, so the failing tests are not known from a report"
        )
    return ""


def read_junit_findings(
    github: GitHubClient,
    token: InstallationToken,
    *,
    repository: str,
    source: str,
    external_id: str,
    job: str,
    limit_bytes: int,
) -> JUnitFindings:
    """What the failed job's JUnit artifact says, for the certification and the wake.

    `source` and `external_id` are the failed check's (a check run from Actions is a
    job, a workflow run is the run itself, 23); the run and its attempt number come
    from the port's issue 435 lookups, since the artifact's name carries the attempt.
    A rate limit propagates (the poll defers); any other refusal is recorded as
    `unreadable` with the reason, never as "no failing tests"."""
    if not job:
        return JUnitFindings(JUNIT_NO_ARTIFACT_FOR_JOB, job=job)
    if junit_artifact_name(job, 1) is None:
        return JUnitFindings(JUNIT_NO_ARTIFACT_FOR_JOB, job=job)
    if not isinstance(github, WorkflowArtifactReader) and not _has_rest_artifact_transport(github):
        return JUnitFindings(JUNIT_UNSUPPORTED, job=job)
    try:
        run_id = _run_id(
            github, token, repository=repository, source=source, external_id=external_id
        )
        if run_id is None:
            return JUnitFindings(
                JUNIT_ABSENT, job=job, detail="the failed check is not an Actions run"
            )
        run = github.get_workflow_run(token, repository=repository, run_id=run_id)
        attempt = int(run.get("run_attempt") or 1) if isinstance(run, Mapping) else 1
        artifact = junit_artifact_name(job, attempt) or ""
        if isinstance(github, WorkflowArtifactReader):
            reports = github.junit_reports(
                token,
                repository=repository,
                run_id=run_id,
                artifact=artifact,
                limit_bytes=limit_bytes,
            )
        else:
            rest_reports = _rest_junit_reports(
                github,
                token,
                repository=repository,
                run_id=run_id,
                artifact=artifact,
                limit_bytes=limit_bytes,
            )
            if isinstance(rest_reports, _UnsupportedRest):
                return JUnitFindings(JUNIT_UNSUPPORTED, job=job)
            reports = rest_reports
    except GitHubError as exc:
        if exc.response_class == "rate_limited":
            raise
        return JUnitFindings(JUNIT_UNREADABLE, job=job, detail=f"GitHub answered {exc.status}")
    except Exception as exc:  # the report never fails the poll
        return JUnitFindings(JUNIT_UNREADABLE, job=job, detail=type(exc).__name__)
    if reports is None:
        return JUnitFindings(JUNIT_ABSENT, job=job, artifact=artifact)
    if not reports:
        return JUnitFindings(
            JUNIT_UNREADABLE, job=job, artifact=artifact, detail="the artifact held no XML"
        )
    failing: list[str] = []
    for report in reports:
        try:
            for node in failing_test_ids(report.content):
                if node not in failing:
                    failing.append(node)
        except JUnitParseError as exc:
            return JUnitFindings(
                JUNIT_UNREADABLE,
                job=job,
                artifact=artifact,
                reports=len(reports),
                detail=f"{report.path}: {exc}",
            )
    return JUnitFindings(
        JUNIT_PARSED,
        job=job,
        artifact=artifact,
        failing_tests=tuple(failing),
        reports=len(reports),
    )


def _rest_junit_reports(
    github: GitHubClient,
    token: InstallationToken,
    *,
    repository: str,
    run_id: int,
    artifact: str,
    limit_bytes: int,
) -> tuple[JUnitReport, ...] | _UnsupportedRest | None:
    """Read an Actions artifact with the production client's existing transport.

    GitHub returns a redirect for the archive endpoint. The transport already owns
    redirect downloads and their credential boundary for job logs, so no token is sent
    to the signed archive URL. Test doubles without that transport remain unsupported.
    """
    transport = getattr(github, "_http", None)
    if transport is None or not callable(getattr(transport, "paginate", None)):
        return _UNSUPPORTED_REST
    rows = transport.paginate(
        f"/repos/{repository}/actions/runs/{run_id}/artifacts",
        bearer=token.reveal(),
        key="artifacts",
    )
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("name") == artifact
        and not bool(row.get("expired", False))
    ]
    if not matches:
        return None
    chosen = max(matches, key=lambda row: int(row.get("id") or 0))
    artifact_id = int(chosen.get("id") or 0)
    if artifact_id <= 0:
        return ()
    path = f"/repos/{repository}/actions/artifacts/{artifact_id}/zip"
    status, payload, headers = transport.request("GET", path, bearer=token.reveal(), raw=True)
    if status in (301, 302, 303, 307, 308):
        location = headers.get("location", "")
        if not location or not callable(getattr(transport, "download", None)):
            return ()
        payload = transport.download(location, limit_bytes=limit_bytes)
        status = 200
    if status >= 400:
        raise GitHubError(status, "artifact download was refused", path=path)
    if not isinstance(payload, bytes) or len(payload) > limit_bytes:
        return ()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            reports: list[JUnitReport] = []
            total = 0
            for member in archive.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".xml"):
                    continue
                total += member.file_size
                if total > limit_bytes or member.flag_bits & 0x1:
                    return ()
                reports.append(JUnitReport(artifact, member.filename, archive.read(member)))
            return tuple(reports)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return ()


def _has_rest_artifact_transport(github: GitHubClient) -> bool:
    transport = getattr(github, "_http", None)
    return transport is not None and all(
        callable(getattr(transport, method, None)) for method in ("paginate", "request", "download")
    )


def _run_id(
    github: GitHubClient,
    token: InstallationToken,
    *,
    repository: str,
    source: str,
    external_id: str,
) -> int | None:
    if not external_id.isdigit():
        return None
    if source == "workflow_run":
        return int(external_id)
    return github.workflow_run_for_job(token, repository=repository, job_id=int(external_id))


__all__ = [
    "JUNIT_ABSENT",
    "JUNIT_NO_ARTIFACT_FOR_JOB",
    "JUNIT_PARSED",
    "JUNIT_UNREADABLE",
    "JUNIT_UNSUPPORTED",
    "SUMMARY_TEST_LIMIT",
    "JUnitFindings",
    "JUnitReport",
    "WorkflowArtifactReader",
    "junit_sentence",
    "read_junit_findings",
]
